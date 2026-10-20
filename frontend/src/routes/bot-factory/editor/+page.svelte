<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		getBot,
		getTemplate,
		createBot,
		updateBot,
		listTemplates,
		listWallets,
		setBotWallet,
		goLiveBot,
		goPaperBot,
		getSettings,
		createBotFromStrategy,
		createTemplate,
		deleteTemplate,
		getBotCostEstimate,
		testBotDecision,
		listFollowableStrategies,
		type BotConfig,
		type BotTestDecision,
		type BotInput,
		type BotType,
		type BotFollowableStrategy,
		type BotCostEstimate,
		type BotDecisionCadence,
		type BotTimeframe,
		type BotTemplate,
		type BotSessionHours,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';

	const WEEKDAYS = [
		'monday',
		'tuesday',
		'wednesday',
		'thursday',
		'friday',
		'saturday',
		'sunday',
	] as const;

	let loading = true;
	let saving = false;
	let error: string | null = null;
	let editId: string | null = null;
	let templates: BotTemplate[] = [];
	let showTemplateSelector = false;
	let editExecutionMode: 'paper' | 'live' = 'paper';

	// Form state
	let name = 'Untitled Bot';
	let model = ''; // empty = server resolves the operator's configured default
	let soul = '';
	let context = '';
	let strategy = '';
	let guardrails = '';
	let capitalAllocation = 100000;
	let maxPositionPct = 10;
	let maxConcurrentPositions = 5;
	let maxDrawdownPct = 3;
	let stopLossPct: number | null = null;
	let takeProfitPct: number | null = null;
	let takerFeeBps = 0;
	let slippageBps = 0;
	let cooldownSeconds = 60;
	let reasoningVerbosity = 'standard';
	let assetMode: 'free_roam' | 'locked' = 'free_roam';
	let lockedPairsText = '';
	let maxLlmCallsPerDay = 200;
	let maxConsecutiveErrors = 5;
	// Daily AI spend cap in USD; null = only the call cap.
	let maxLlmCostPerDay: number | null = null;
	// New bots decide once per closed 1h candle (the data they read changes
	// hourly); 'interval' decides every cooldownSeconds.
	let decisionCadence: BotDecisionCadence = 'candle_close';
	let timeframe: BotTimeframe = '1h';
	let dataSources: BotInput[] = [];

	// ── Bot type: AI, or a rule bot that decides in code ──────────────
	let botType: BotType = 'ai';
	const BOT_TYPES: { value: BotType; label: string; hint: string }[] = [
		{ value: 'ai', label: 'AI trader', hint: 'A model reads the market and decides. Costs an AI call per decision.' },
		{ value: 'grid', label: 'Grid', hint: 'Buys each time the price falls through a level in a band, sells one level higher. No AI cost.' },
		{ value: 'dca', label: 'DCA', hint: 'Buys on a schedule, adds safety buys on dips, takes profit above the average. No AI cost.' },
		{ value: 'signal', label: 'Signal follower', hint: "Copies one of your paper or live strategies' trades with its own risk settings." },
	];
	let rulePair = 'BTC/USDT';
	let gridLower: number | null = null;
	let gridUpper: number | null = null;
	let gridLevels = 10;
	let dcaOrderUsd: number | null = null;
	let dcaIntervalHours = 24;
	let dcaSafetyOrders = 3;
	let dcaSafetyStepPct = 2;
	let dcaSafetyScale = 1.5;
	let dcaTakeProfitPct = 2;
	let signalStrategyId = '';
	let signalJoinOpen = false;
	let signalAiApproval = false;
	let followable: BotFollowableStrategy[] = [];
	$: aiNeeded = botType === 'ai' || (botType === 'signal' && signalAiApproval);
	$: if (botType === 'signal' && followable.length === 0) loadFollowable();
	async function loadFollowable() {
		try {
			followable = await listFollowableStrategies();
		} catch {
			followable = [];
		}
	}
	$: gridStepPct =
		gridLower && gridUpper && gridUpper > gridLower && gridLevels >= 2
			? ((gridUpper - gridLower) / gridLevels / gridLower) * 100
			: null;

	function buildRuleParams(): Record<string, unknown> | null {
		if (botType === 'grid') return { pair: rulePair.trim(), lower: gridLower, upper: gridUpper, levels: gridLevels };
		if (botType === 'dca')
			return {
				pair: rulePair.trim(),
				order_usd: dcaOrderUsd,
				interval_hours: dcaIntervalHours,
				safety_orders: dcaSafetyOrders,
				safety_step_pct: dcaSafetyStepPct,
				safety_scale: dcaSafetyScale,
				take_profit_pct: dcaTakeProfitPct,
			};
		if (botType === 'signal')
			return { strategy_id: signalStrategyId, join_open_trades: signalJoinOpen, ai_approval: signalAiApproval };
		return null;
	}

	function loadRuleParams(config: Record<string, unknown>) {
		const kind = config.bot_type as string | undefined;
		botType = kind === 'grid' || kind === 'dca' || kind === 'signal' ? kind : 'ai';
		const rp = (config.rule_params as Record<string, any> | null) || {};
		if (rp.pair) rulePair = String(rp.pair);
		if (botType === 'grid') {
			gridLower = rp.lower ?? null;
			gridUpper = rp.upper ?? null;
			gridLevels = rp.levels ?? 10;
		} else if (botType === 'dca') {
			dcaOrderUsd = rp.order_usd ?? null;
			dcaIntervalHours = rp.interval_hours ?? 24;
			dcaSafetyOrders = rp.safety_orders ?? 3;
			dcaSafetyStepPct = rp.safety_step_pct ?? 2;
			dcaSafetyScale = rp.safety_scale ?? 1.5;
			dcaTakeProfitPct = rp.take_profit_pct ?? 2;
		} else if (botType === 'signal') {
			signalStrategyId = String(rp.strategy_id ?? '');
			signalJoinOpen = Boolean(rp.join_open_trades);
			signalAiApproval = Boolean(rp.ai_approval);
		}
	}
	const INPUT_OPTIONS: { value: BotInput; label: string; hint: string }[] = [
		{ value: 'indicators', label: 'Indicators', hint: 'RSI, ATR, EMA 20/50, ADX, volume vs average' },
		{ value: 'higher_timeframe', label: 'Higher timeframe', hint: 'Last 5 candles and trend of the next timeframe up' },
		{ value: 'regime', label: 'Market regime', hint: "Forven's trend / range / high-volatility label" },
		{ value: 'funding', label: 'Funding rate', hint: 'Latest and 24h average' },
		{ value: 'open_interest', label: 'Open interest', hint: 'Latest and 24h change' },
		{ value: 'strategy_signals', label: 'Strategy signals', hint: 'Entries and exits your paper and live strategies fired on these coins in the last 24h' },
	];
	function toggleInput(value: BotInput, on: boolean) {
		dataSources = on ? [...new Set([...dataSources, value])] : dataSources.filter((v) => v !== value);
	}
	const TIMEFRAMES: { value: BotTimeframe; label: string }[] = [
		{ value: '15m', label: '15 minutes' },
		{ value: '1h', label: '1 hour' },
		{ value: '4h', label: '4 hours' },
		{ value: '1d', label: '1 day' },
	];

	// Session hours — leaving unset (sessionHoursEnabled = false) = always active
	let sessionHoursEnabled = false;
	let sessionTimezone = 'America/New_York';
	let sessionDays: string[] = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'];
	let sessionStart = '09:30';
	let sessionEnd = '16:00';

	// Wallet the bot's LIVE orders route to ('' = master / direction books).
	// Saved via the dedicated wallet endpoint (locked while the bot is
	// live-armed — routing changes then go through GO LIVE re-arming).
	let liveWallet = '';
	let savedLiveWallet = '';
	let walletOptions: string[] = [];
	let walletOptionsError: string | null = null;

	// Execution-mode selection. `editExecutionMode` is the bot's CURRENT armed
	// state; `execMode` is what the operator wants after save. Going live still
	// requires the typed "GO LIVE" + per-order notional ceiling (server-enforced
	// GO-LIVE-1) — the arming fields render inline when Live is picked.
	let execMode: 'paper' | 'live' = 'paper';
	let armCeiling: number | null = null;
	let armConfirm = '';
	$: needsArming = execMode === 'live' && editExecutionMode !== 'live';
	$: armReady =
		!needsArming ||
		(armConfirm.trim().toUpperCase() === 'GO LIVE' && !!armCeiling && armCeiling > 0);

	async function loadWalletOptions() {
		try {
			const snapshot = await listWallets(true);
			walletOptions = snapshot.registered.map((w) => w.label);
		} catch (e: any) {
			walletOptions = [];
			walletOptionsError = String(e.message || e);
		}
	}

	// Model options are the operator's enabled models (agent_model_keys, e.g.
	// "openai:gpt-4.1-mini"), grouped by provider for the dropdown. The bot
	// model field accepts a "provider:model" key directly (resolver auto-detects
	// the provider), so the keys drop in as-is.
	let modelKeys: string[] = [];
	$: modelsByProvider = (() => {
		const groups = new Map<string, { key: string; label: string }[]>();
		for (const key of modelKeys) {
			const idx = key.indexOf(':');
			const provider = idx > 0 ? key.slice(0, idx) : 'other';
			const label = idx > 0 ? key.slice(idx + 1) : key;
			if (!groups.has(provider)) groups.set(provider, []);
			groups.get(provider)!.push({ key, label });
		}
		return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
	})();
	// A saved bot may carry a plain model id or a key no longer enabled — keep it
	// selectable so opening the editor never silently drops the current model.
	$: modelIsCustom = model.trim().length > 0 && !modelKeys.includes(model.trim());

	async function loadModelOptions() {
		try {
			const settings = await getSettings();
			const keys = (settings as { agent_model_keys?: string[] }).agent_model_keys;
			modelKeys = Array.isArray(keys) ? [...keys].sort() : [];
		} catch {
			modelKeys = [];
		}
	}

	function toggleSessionDay(day: string) {
		sessionDays = sessionDays.includes(day)
			? sessionDays.filter((d) => d !== day)
			: [...sessionDays, day];
	}

	// Live summary rail derivations
	$: maxPositionUsd = (capitalAllocation || 0) * (maxPositionPct || 0) / 100;
	// Every position at full size: above 100% the bot can deploy more than its
	// equity (paper leverage — allowed, but worth knowing before saving).
	$: maxExposurePct = (maxPositionPct || 0) * (maxConcurrentPositions || 0);

	// Projected AI calls and spend per day, refreshed as the inputs change.
	let costEstimate: BotCostEstimate | null = null;
	let costEstimateTimer: ReturnType<typeof setTimeout> | null = null;
	let costEstimateSeq = 0;
	$: scheduleCostEstimate(
		model, decisionCadence, timeframe, cooldownSeconds, assetMode, lockedPairsText,
		reasoningVerbosity, maxLlmCallsPerDay, maxLlmCostPerDay, dataSources,
	);
	function scheduleCostEstimate(..._deps: unknown[]) {
		if (costEstimateTimer) clearTimeout(costEstimateTimer);
		costEstimateTimer = setTimeout(async () => {
			const seq = ++costEstimateSeq;
			try {
				const estimate = await getBotCostEstimate({
					model: model.trim(),
					decision_cadence: decisionCadence,
					timeframe,
					cooldown_seconds: Math.max(Number(cooldownSeconds) || 60, 1),
					asset_mode: assetMode,
					locked_pairs: lockedPairsText.split(',').map((s) => s.trim()).filter(Boolean),
					reasoning_verbosity: reasoningVerbosity,
					max_llm_calls_per_day: Math.max(Number(maxLlmCallsPerDay) || 1, 1),
					max_llm_cost_per_day: maxLlmCostPerDay && maxLlmCostPerDay > 0 ? maxLlmCostPerDay : null,
					bot_id: editId,
					data_sources: dataSources,
				});
				if (seq === costEstimateSeq) costEstimate = estimate;
			} catch {
				if (seq === costEstimateSeq) costEstimate = null;
			}
		}, 400);
	}
	function fmtUsdSmall(value: number | null | undefined): string {
		if (value == null) return '—';
		if (value < 0.01) return `$${value.toFixed(4)}`;
		return `$${value.toFixed(2)}`;
	}
	$: cadenceLabel = decisionCadence === 'candle_close'
		? `each new ${timeframe} candle`
		: cooldownSeconds >= 3600
			? `every ${(cooldownSeconds / 3600).toFixed(1)}h`
			: cooldownSeconds >= 60
				? `every ${Math.round(cooldownSeconds / 60)}m`
				: `every ${cooldownSeconds}s`;
	$: universeLabel = assetMode === 'locked'
		? (lockedPairsText.split(',').map((s) => s.trim()).filter(Boolean).join(', ') || '— pick pairs —')
		: 'Top perps by volume (auto)';

	function populateFromConfig(config: Partial<BotConfig> | Record<string, unknown>) {
		name = (config.name as string) || name;
		// Empty/null model is valid — server resolves the configured default.
		model = (config.model as string) ?? model ?? '';
		soul = (config.soul as string) || '';
		context = (config.context as string) || '';
		strategy = (config.strategy as string) || '';
		guardrails = (config.guardrails as string) || '';
		capitalAllocation = (config.capital_allocation as number) ?? capitalAllocation;
		maxPositionPct = (config.max_position_pct as number) ?? maxPositionPct;
		maxConcurrentPositions = (config.max_concurrent_positions as number) ?? maxConcurrentPositions;
		maxDrawdownPct = (config.max_drawdown_pct as number) ?? maxDrawdownPct;
		stopLossPct = (config.stop_loss_pct as number | null) ?? null;
		takeProfitPct = (config.take_profit_pct as number | null) ?? null;
		takerFeeBps = (config.taker_fee_bps as number) ?? takerFeeBps;
		slippageBps = (config.slippage_bps as number) ?? slippageBps;
		cooldownSeconds = (config.cooldown_seconds as number) ?? cooldownSeconds;
		reasoningVerbosity = (config.reasoning_verbosity as string) || 'standard';
		assetMode = ((config.asset_mode as string) || 'free_roam') as 'free_roam' | 'locked';
		const lp = config.locked_pairs;
		lockedPairsText = Array.isArray(lp) ? lp.join(', ') : '';
		maxLlmCallsPerDay = (config.max_llm_calls_per_day as number) ?? 200;
		maxConsecutiveErrors = (config.max_consecutive_errors as number) ?? 5;
		maxLlmCostPerDay = (config.max_llm_cost_per_day as number | null) ?? null;
		// A saved bot without the field predates cadences and ran on an interval;
		// templates and strategy bridges without it get the new default.
		const cadence = config.decision_cadence as string | undefined;
		if (cadence === 'candle_close' || cadence === 'interval') decisionCadence = cadence;
		loadRuleParams(config);
		const ds = config.data_sources;
		dataSources = Array.isArray(ds)
			? (ds as string[]).filter((v): v is BotInput => INPUT_OPTIONS.some((o) => o.value === v))
			: [];
		const tf = config.timeframe as string | undefined;
		if (tf === '15m' || tf === '1h' || tf === '4h' || tf === '1d') timeframe = tf;
		else if ('id' in config && config.id) decisionCadence = 'interval';
		if (config.execution_mode === 'live' || config.execution_mode === 'paper') {
			editExecutionMode = config.execution_mode;
			execMode = config.execution_mode;
		}
		if ('live_wallet' in config) {
			liveWallet = (config.live_wallet as string | null) ?? '';
			savedLiveWallet = liveWallet;
		}

		const sh = config.session_hours as Partial<BotSessionHours> | null | undefined;
		if (sh && typeof sh === 'object') {
			sessionHoursEnabled = true;
			sessionTimezone = sh.timezone || sessionTimezone;
			sessionDays = Array.isArray(sh.days) && sh.days.length ? sh.days : sessionDays;
			sessionStart = sh.start || sessionStart;
			sessionEnd = sh.end || sessionEnd;
		} else {
			sessionHoursEnabled = false;
		}
	}

	async function loadTemplateById(templateId: string) {
		try {
			const template = await getTemplate(templateId);
			if (template?.config_snapshot) {
				populateFromConfig(template.config_snapshot);
				name = template.name;
			}
		} catch {}
	}

	function buildConfig(): Record<string, unknown> {
		return {
			name,
			// Empty string → null so the server resolves the configured default model.
			model: model.trim() || null,
			soul: soul || null,
			context: context || null,
			strategy: strategy || null,
			guardrails: guardrails || null,
			capital_allocation: capitalAllocation,
			max_position_pct: maxPositionPct,
			max_concurrent_positions: maxConcurrentPositions,
			max_drawdown_pct: maxDrawdownPct,
			stop_loss_pct: stopLossPct,
			take_profit_pct: takeProfitPct,
			taker_fee_bps: takerFeeBps,
			slippage_bps: slippageBps,
			cooldown_seconds: cooldownSeconds,
			session_hours: sessionHoursEnabled
				? {
						timezone: sessionTimezone.trim() || 'America/New_York',
						days: sessionDays,
						start: sessionStart,
						end: sessionEnd,
					}
				: null,
			reasoning_verbosity: reasoningVerbosity,
			asset_mode: assetMode,
			locked_pairs:
				assetMode === 'locked'
					? lockedPairsText.split(',').map((s) => s.trim()).filter(Boolean)
					: null,
			max_llm_calls_per_day: maxLlmCallsPerDay,
			max_consecutive_errors: maxConsecutiveErrors,
			// 0 clears a saved dollar cap (null would mean "unchanged" on update).
			max_llm_cost_per_day: maxLlmCostPerDay && maxLlmCostPerDay > 0 ? maxLlmCostPerDay : editId ? 0 : null,
			decision_cadence: decisionCadence,
			timeframe,
			data_sources: dataSources,
			bot_type: botType,
			rule_params: buildRuleParams(),
		};
	}

	async function handleSave() {
		saving = true;
		error = null;
		try {
			const config = buildConfig();
			let savedId = editId;
			if (editId) {
				await updateBot(editId, config as Partial<BotConfig>);
			} else {
				const created = await createBot(config as Partial<BotConfig>);
				savedId = created.id;
			}
			// Execution mode + wallet are armed separately (never through the
			// generic config PUT). Config saves FIRST so a stop-loss entered in
			// this same save satisfies the go-live requirement.
			if (savedId && needsArming) {
				await goLiveBot(savedId, armConfirm.trim(), armCeiling ?? 0, liveWallet || null);
				addToast('Bot saved and armed for LIVE execution', 'success');
			} else if (savedId && execMode === 'paper' && editExecutionMode === 'live') {
				await goPaperBot(savedId);
				addToast('Bot saved and returned to paper mode', 'success');
			} else {
				if (savedId && editExecutionMode !== 'live' && liveWallet !== savedLiveWallet) {
					await setBotWallet(savedId, liveWallet || null);
				}
				addToast(editId ? 'Bot saved' : 'Bot created', 'success');
			}
			goto('/bot-factory');
		} catch (e: any) {
			error = e.message || 'Failed to save';
		} finally {
			saving = false;
		}
	}

	// ── Test one decision: a real AI call on live candles, nothing traded ──
	let testing = false;
	let testResult: BotTestDecision | null = null;
	let testError: string | null = null;
	let showTestPrompt = false;

	async function handleTestDecision() {
		testing = true;
		testError = null;
		testResult = null;
		showTestPrompt = false;
		try {
			testResult = await testBotDecision(buildConfig() as Partial<BotConfig>, editId);
		} catch (e: any) {
			testError = e.message || 'Test failed';
		} finally {
			testing = false;
		}
	}

	$: perCallUsd =
		costEstimate && costEstimate.usd_per_day != null && costEstimate.calls_per_day > 0
			? costEstimate.usd_per_day / costEstimate.calls_per_day
			: null;

	// ── Save-as-template (inline form — no window.prompt) ─────────────
	let templateFormOpen = false;
	let templateName = '';
	let templateDesc = '';
	let savingTemplate = false;
	let deletingTemplateId: string | null = null;
	let confirmDeleteTemplateId: string | null = null;

	async function loadTemplates() {
		templates = await listTemplates();
	}

	async function handleSaveAsTemplate() {
		const trimmed = templateName.trim();
		if (!trimmed) return;
		savingTemplate = true;
		error = null;
		try {
			await createTemplate(trimmed, templateDesc.trim() || null, buildConfig());
			await loadTemplates();
			addToast(`Template "${trimmed}" saved`, 'success');
			templateFormOpen = false;
			templateName = '';
			templateDesc = '';
		} catch (e: any) {
			error = e.message || 'Failed to save template';
		} finally {
			savingTemplate = false;
		}
	}

	async function handleDeleteTemplate(template: BotTemplate) {
		if (template.is_builtin) return;
		confirmDeleteTemplateId = null;
		deletingTemplateId = template.id;
		error = null;
		try {
			await deleteTemplate(template.id);
			await loadTemplates();
			addToast('Template deleted', 'success');
		} catch (e: any) {
			error = e.message || 'Failed to delete template';
		} finally {
			deletingTemplateId = null;
		}
	}

	onMount(async () => {
		void loadWalletOptions();
		void loadModelOptions();
		try {
			await loadTemplates();
			const params = $page.url.searchParams;
			editId = params.get('id');
			const templateId = params.get('template');

			const strategyId = params.get('strategy');

			if (editId) {
				const bot = await getBot(editId);
				if (bot) populateFromConfig(bot);
			} else if (strategyId) {
				try {
					const result = await createBotFromStrategy(strategyId);
					if (result?.config) populateFromConfig(result.config);
				} catch (e: any) {
					error = `Failed to load strategy: ${e.message}`;
				}
			} else if (templateId) {
				await loadTemplateById(templateId);
			} else {
				showTemplateSelector = true;
			}
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	});
</script>

<svelte:head>
	<title>{editId ? 'Edit Bot' : 'New Bot'} | Bot Factory | Forven</title>
</svelte:head>

<div class="mx-auto max-w-6xl px-4 py-6">
	<!-- Header -->
	<div class="mb-6 flex items-end justify-between border-b border-sc-line pb-4">
		<div>
			<button on:click={() => goto('/bot-factory')} class="mb-1 text-[11px] text-sc-ink3 hover:text-sc-ink">&larr; Bot Factory</button>
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">{editId ? 'Edit Bot' : 'New Bot'}</h1>
		</div>
		<div class="flex items-center gap-2">
			<button
				on:click={() => (templateFormOpen = !templateFormOpen)}
				disabled={saving || savingTemplate || !name.trim()}
				class="terminal-button text-[12px]"
			>
				Save as Template
			</button>
			<button
				on:click={handleSave}
				disabled={saving || savingTemplate || !name.trim() || !armReady}
				class="terminal-button-primary text-[12px]"
			>
				{saving ? 'Saving…' : needsArming ? 'Save + Arm Live' : editId ? 'Save Changes' : 'Create Bot'}
			</button>
		</div>
	</div>

	{#if error}
		<div class="mb-4 border border-red-900 bg-red-500/5 p-3 text-sm text-red-400">{error}</div>
	{/if}

	{#if templateFormOpen}
		<div class="rounded-md mb-6 border border-sc-line2 bg-sc-panel p-4">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Save current config as template</div>
			<div class="mt-3 flex flex-wrap items-end gap-3">
				<div class="flex flex-col gap-1">
					<label for="tpl-name" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Name</label>
					<input id="tpl-name" bind:value={templateName} placeholder={name} class="terminal-input w-56" />
				</div>
				<div class="flex min-w-0 flex-1 flex-col gap-1">
					<label for="tpl-desc" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Description (optional)</label>
					<input id="tpl-desc" bind:value={templateDesc} class="terminal-input" />
				</div>
				<button on:click={handleSaveAsTemplate} disabled={savingTemplate || !templateName.trim()} class="terminal-button-primary text-[12px]">
					{savingTemplate ? 'Saving…' : 'Save'}
				</button>
				<button on:click={() => (templateFormOpen = false)} class="terminal-button text-[12px]">Cancel</button>
			</div>
		</div>
	{/if}

	{#if loading}
		<div class="py-20 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading…</div>
	{:else}
		<!-- Template gallery (only on create, initially) -->
		{#if showTemplateSelector && !editId && templates.length > 0}
			<div class="rounded-md mb-6 border border-sc-line bg-sc-panel p-4">
				<div class="mb-3 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Start from a template</div>
				<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
					{#each templates as template}
						<div class="relative">
							<button
								on:click={() => { populateFromConfig(template.config_snapshot); name = template.name; showTemplateSelector = false; }}
								class="rounded-md w-full border border-sc-line bg-sc-bg p-3 text-left transition-colors hover:border-sc-line2"
							>
								<div class="pr-8 text-sm font-bold text-sc-ink">{template.name}</div>
								<div class="mt-0.5 text-xs text-sc-ink3">{template.description}</div>
							</button>
							{#if !template.is_builtin}
								{#if confirmDeleteTemplateId === template.id}
									<div class="absolute right-2 top-2 flex gap-1">
										<button
											type="button"
											on:click={() => handleDeleteTemplate(template)}
											disabled={deletingTemplateId === template.id}
											class="rounded-md border border-red-900 px-1.5 py-0.5 text-[11px] font-medium text-red-400 hover:bg-red-500 hover:text-sc-ink"
										>{deletingTemplateId === template.id ? '…' : 'Delete'}</button>
										<button type="button" on:click={() => (confirmDeleteTemplateId = null)} class="rounded-md border border-sc-line2 px-1.5 py-0.5 text-[11px] font-medium text-sc-ink2">×</button>
									</div>
								{:else}
									<button
										type="button"
										on:click={() => (confirmDeleteTemplateId = template.id)}
										aria-label={`Delete template ${template.name}`}
										title="Delete template"
										class="absolute right-2 top-2 flex h-6 w-6 items-center justify-center text-sc-ink3 transition-colors hover:text-red-400"
									>&times;</button>
								{/if}
							{/if}
						</div>
					{/each}
				</div>
				<button on:click={() => (showTemplateSelector = false)} class="mt-3 text-[11px] text-sc-ink3 hover:text-sc-ink">
					or start from scratch &rarr;
				</button>
			</div>
		{/if}

		<div class="grid grid-cols-1 gap-6 lg:grid-cols-3">
			<!-- Form column -->
			<div class="space-y-6 lg:col-span-2">
				<!-- Bot type -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Bot type</div>
					<div class="grid grid-cols-1 gap-2 p-4 sm:grid-cols-2">
						{#each BOT_TYPES as option}
							<button
								type="button"
								on:click={() => (botType = option.value)}
								class="rounded-md border p-3 text-left transition-colors {botType === option.value
									? 'border-sc-ink2 bg-sc-bg'
									: 'border-sc-line hover:border-sc-line2'}"
							>
								<div class="text-[13px] font-medium text-sc-ink">{option.label}</div>
								<div class="mt-0.5 text-[11px] text-sc-ink3">{option.hint}</div>
							</button>
						{/each}
					</div>
				</section>

				{#if botType !== 'ai'}
					<section class="terminal-card">
						<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">
							{botType === 'grid' ? 'Grid' : botType === 'dca' ? 'DCA' : 'Signal follower'} settings
						</div>
						<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
							{#if botType === 'grid' || botType === 'dca'}
								<div>
									<label for="rulePair" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Pair</label>
									<input id="rulePair" bind:value={rulePair} class="terminal-input" placeholder="BTC/USDT" />
								</div>
							{/if}
							{#if botType === 'grid'}
								<div>
									<label for="gridLevels" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Levels</label>
									<input id="gridLevels" type="number" bind:value={gridLevels} min="2" max="100" step="1" class="terminal-input" />
								</div>
								<div>
									<label for="gridLower" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Lower price ($)</label>
									<input id="gridLower" type="number" bind:value={gridLower} min="0" step="any" class="terminal-input" />
								</div>
								<div>
									<label for="gridUpper" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Upper price ($)</label>
									<input id="gridUpper" type="number" bind:value={gridUpper} min="0" step="any" class="terminal-input" />
								</div>
								<p class="col-span-full text-[11px] text-sc-ink3">
									Each buy uses your max position size and sells one level higher{gridStepPct != null
										? ` (about ${gridStepPct.toFixed(2)}% per level)`
										: ''}. Set max concurrent positions to at least the number of levels you want filled at once.
								</p>
							{:else if botType === 'dca'}
								<div>
									<label for="dcaOrder" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Base order ($)</label>
									<input id="dcaOrder" type="number" bind:value={dcaOrderUsd} min="0" step="any" class="terminal-input" placeholder="max position" />
								</div>
								<div>
									<label for="dcaInterval" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Hours between deals</label>
									<input id="dcaInterval" type="number" bind:value={dcaIntervalHours} min="1" step="1" class="terminal-input" />
								</div>
								<div>
									<label for="dcaSafety" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Safety orders</label>
									<input id="dcaSafety" type="number" bind:value={dcaSafetyOrders} min="0" max="20" step="1" class="terminal-input" />
								</div>
								<div>
									<label for="dcaStep" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Safety step (% below last buy)</label>
									<input id="dcaStep" type="number" bind:value={dcaSafetyStepPct} min="0.1" step="0.1" class="terminal-input" />
								</div>
								<div>
									<label for="dcaScale" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Safety size multiplier</label>
									<input id="dcaScale" type="number" bind:value={dcaSafetyScale} min="1" step="0.1" class="terminal-input" />
								</div>
								<div>
									<label for="dcaTp" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Take profit (% above average)</label>
									<input id="dcaTp" type="number" bind:value={dcaTakeProfitPct} min="0.1" step="0.1" class="terminal-input" />
								</div>
								<p class="col-span-full text-[11px] text-sc-ink3">
									No order is ever larger than your max position size. Set max concurrent positions to at least {dcaSafetyOrders + 1} so every safety order can fill.
								</p>
							{:else if botType === 'signal'}
								<div class="col-span-full">
									<label for="sigStrategy" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Strategy to follow</label>
									<select id="sigStrategy" bind:value={signalStrategyId} class="terminal-select">
										<option value="">Pick a paper or live strategy</option>
										{#each followable as s}
											<option value={s.id}>{s.id} · {s.name} · {s.symbol ?? '?'} {s.timeframe ?? ''} · {s.stage}{s.open_trades ? ` · ${s.open_trades} open` : ''}</option>
										{/each}
									</select>
								</div>
								<label class="col-span-full flex items-start gap-2 text-[12px] text-sc-ink2">
									<input type="checkbox" class="mt-0.5" bind:checked={signalJoinOpen} />
									<span>Join trades the strategy already has open when the bot starts</span>
								</label>
								<label class="col-span-full flex items-start gap-2 text-[12px] text-sc-ink2">
									<input type="checkbox" class="mt-0.5" bind:checked={signalAiApproval} />
									<span>Hybrid: ask the AI to approve or veto each entry <span class="text-sc-ink3">(one AI call per entry, using the model and persona below)</span></span>
								</label>
								<p class="col-span-full text-[11px] text-sc-ink3">Each trade uses your max position size. The bot closes when the strategy closes.</p>
							{/if}
						</div>
					</section>
				{/if}

				<!-- Identity -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Identity</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="name" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Name</label>
							<input id="name" bind:value={name} class="terminal-input" />
						</div>
						{#if aiNeeded}
						<div>
							<label for="model" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Model</label>
							<select id="model" bind:value={model} class="terminal-select">
								<option value="">Default provider (recommended)</option>
								{#each modelsByProvider as [provider, entries]}
									<optgroup label={provider}>
										{#each entries as entry}
											<option value={entry.key}>{entry.label}</option>
										{/each}
									</optgroup>
								{/each}
								{#if modelIsCustom}
									<option value={model}>{model} (custom)</option>
								{/if}
							</select>
							<p class="mt-1 text-[11px] text-sc-ink3">
								{#if modelKeys.length === 0}
									Blank = your configured default provider. Enable models on the <a href="/agents" class="text-sc-ink2 underline hover:text-sc-ink">Agents</a> page to pick a specific one.
								{:else}
									Blank = your configured default provider. List = models you've enabled on the Agents page.
								{/if}
							</p>
						</div>
						{/if}
					</div>
				</section>

				<!-- Persona -->
				{#if aiNeeded}
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Persona &amp; Strategy</div>
					<div class="space-y-4 p-4">
						<div>
							<label for="soul" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Soul <span class="normal-case text-sc-ink3">— personality, temperament, decision style</span></label>
							<textarea id="soul" bind:value={soul} rows="4" class="terminal-input resize-y" placeholder="You are an aggressive momentum trader who thrives on volatility..."></textarea>
						</div>
						<div>
							<label for="strategy" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Strategy <span class="normal-case text-sc-ink3">— trading approach, broad or narrow</span></label>
							<textarea id="strategy" bind:value={strategy} rows="4" class="terminal-input resize-y" placeholder="Trade momentum breakouts on high-volume assets..."></textarea>
						</div>
						<div>
							<label for="context" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Context <span class="normal-case text-sc-ink3">— seed knowledge, research notes, market thesis</span></label>
							<textarea id="context" bind:value={context} rows="3" class="terminal-input resize-y" placeholder="BTC tends to correlate with macro risk-on sentiment..."></textarea>
						</div>
						<div>
							<label for="guardrails" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Guardrails <span class="normal-case text-sc-ink3">— behavioral rules (best-effort, LLM-interpreted)</span></label>
							<textarea id="guardrails" bind:value={guardrails} rows="3" class="terminal-input resize-y" placeholder="Never hold a position for more than 2 hours..."></textarea>
						</div>
					</div>
				</section>
				{/if}

				<!-- Risk & sizing -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Risk &amp; Sizing</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="capital" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Capital Allocation ($)</label>
							<input id="capital" type="number" bind:value={capitalAllocation} min="0.01" step="any" class="terminal-input" />
						</div>
						<div>
							<label for="maxPos" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Max Position Size (%)</label>
							<input id="maxPos" type="number" bind:value={maxPositionPct} min="0.01" max="100" step="0.5" class="terminal-input" />
						</div>
						<div>
							<label for="maxConcurrent" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Max Concurrent Positions</label>
							<input id="maxConcurrent" type="number" bind:value={maxConcurrentPositions} min="1" max="100" step="1" class="terminal-input" />
						</div>
						<div>
							<label for="maxDrawdown" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Max Drawdown (%)</label>
							<input id="maxDrawdown" type="number" bind:value={maxDrawdownPct} min="0.01" max="100" step="0.5" class="terminal-input" />
						</div>
						<div>
							<label for="stopLoss" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Stop Loss (%) <span class="normal-case text-sc-ink3">— required for live</span></label>
							<input id="stopLoss" type="number" bind:value={stopLossPct} min="0.01" max="100" step="0.5" placeholder="none" class="terminal-input" />
						</div>
						<div>
							<label for="takeProfit" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Take Profit (%) <span class="normal-case text-sc-ink3">— optional</span></label>
							<input id="takeProfit" type="number" bind:value={takeProfitPct} min="0.01" step="0.5" placeholder="none" class="terminal-input" />
						</div>
					</div>
					{#if maxExposurePct > 100}
						<div class="border-t border-yellow-900/50 bg-yellow-500/5 px-4 py-2.5 text-[11px] text-yellow-500">
							With every position at full size this bot can deploy {maxExposurePct.toFixed(0)}% of its equity
							({maxPositionPct}% × {maxConcurrentPositions} positions). That is leverage: allowed on paper,
							but a bad streak hits the drawdown limit much faster. 10% × 5 keeps it at 50%.
						</div>
					{/if}
				</section>

				<!-- Execution -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Execution</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="takerFee" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Taker Fee (bps) <span class="normal-case text-sc-ink3">— paper cost model</span></label>
							<input id="takerFee" type="number" bind:value={takerFeeBps} min="0" step="0.1" class="terminal-input" />
						</div>
						<div>
							<label for="slippage" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Slippage (bps) <span class="normal-case text-sc-ink3">— paper cost model</span></label>
							<input id="slippage" type="number" bind:value={slippageBps} min="0" step="0.1" class="terminal-input" />
						</div>
						<div>
							<label for="timeframe" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Candles</label>
							<select id="timeframe" bind:value={timeframe} class="terminal-select">
								{#each TIMEFRAMES as tf}
									<option value={tf.value}>{tf.label}</option>
								{/each}
							</select>
							<p class="mt-1 text-[11px] text-sc-ink3">The bot is shown the last 10 candles of this size for each coin.</p>
						</div>
						{#if aiNeeded}
						<fieldset class="col-span-full">
							<legend class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Also show the bot</legend>
							<div class="grid gap-1.5 sm:grid-cols-2">
								{#each INPUT_OPTIONS as option}
									<label class="flex items-start gap-2 text-[12px] text-sc-ink2">
										<input
											type="checkbox"
											class="mt-0.5"
											checked={dataSources.includes(option.value)}
											on:change={(e) => toggleInput(option.value, e.currentTarget.checked)}
										/>
										<span><span class="text-sc-ink">{option.label}</span> <span class="text-sc-ink3">· {option.hint}</span></span>
									</label>
								{/each}
							</div>
							<p class="mt-1 text-[11px] text-sc-ink3">Computed by Forven, not guessed by the model. Each one makes the prompt a little longer.</p>
						</fieldset>
						{/if}
						{#if botType === 'ai'}
						<div>
							<label for="cadence" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Decide</label>
							<select id="cadence" bind:value={decisionCadence} class="terminal-select">
								<option value="candle_close">On each new {timeframe} candle (recommended)</option>
								<option value="interval">On a fixed interval</option>
							</select>
							<p class="mt-1 text-[11px] text-sc-ink3">
								{decisionCadence === 'candle_close'
									? `Asks the model once per new ${timeframe} candle. Stops, targets and drawdown are still checked every minute.`
									: 'Asks the model every interval, even when no new candle has closed.'}
							</p>
						</div>
						{/if}
						{#if botType === 'ai' && decisionCadence === 'interval'}
							<div>
								<label for="cooldown" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Interval (seconds)</label>
								<input id="cooldown" type="number" bind:value={cooldownSeconds} min="1" step="1" class="terminal-input" />
							</div>
						{/if}
						{#if aiNeeded}
						<div>
							<label for="verbosity" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Reasoning Verbosity</label>
							<select id="verbosity" bind:value={reasoningVerbosity} class="terminal-select">
								<option value="minimal">Minimal</option>
								<option value="standard">Standard</option>
								<option value="verbose">Verbose</option>
							</select>
						</div>
						{/if}
					</div>
				</section>

				<!-- Universe -->
				{#if botType === 'ai'}
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Universe</div>
					<div class="space-y-4 p-4">
						<div class="flex gap-2">
							<button
								on:click={() => (assetMode = 'free_roam')}
								class="{assetMode === 'free_roam' ? 'terminal-button-primary' : 'terminal-button'} text-[12px]"
							>Free Roam</button>
							<button
								on:click={() => (assetMode = 'locked')}
								class="{assetMode === 'locked' ? 'terminal-button-primary' : 'terminal-button'} text-[12px]"
							>Locked Pairs</button>
						</div>
						{#if assetMode === 'free_roam'}
							<p class="text-[11px] text-sc-ink3">Watches the top perps by 24h volume from the research universe (up to 6 pairs, refreshed hourly). Use Locked Pairs to pin specific symbols.</p>
						{:else}
							<div>
								<label for="pairs" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Locked Pairs (comma-separated)</label>
								<input id="pairs" bind:value={lockedPairsText} class="terminal-input" placeholder="BTC/USDT, ETH/USDT, SOL/USDT" />
							</div>
						{/if}
					</div>
				</section>
				{/if}

				<!-- Execution: paper vs live + wallet -->
				<section class="terminal-card {execMode === 'live' ? 'border-red-900' : ''}">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Execution</div>
					<div class="space-y-4 p-4">
						<div>
							<span class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Mode</span>
							<div class="flex gap-2">
								<button
									on:click={() => (execMode = 'paper')}
									class="{execMode === 'paper' ? 'terminal-button-primary' : 'terminal-button'} text-[12px]"
								>Paper</button>
								<button
									on:click={() => (execMode = 'live')}
									disabled={botType !== 'ai'}
									title={botType !== 'ai' ? 'Grid, DCA and Signal-follower bots run on paper only for now' : ''}
									class="rounded-md text-[12px] disabled:opacity-40 {execMode === 'live' ? 'border border-red-500 bg-red-500 px-3 py-1.5 font-medium text-sc-ink' : 'terminal-button-danger'}"
								>Live</button>
							</div>
							{#if botType !== 'ai'}
								<p class="mt-1.5 text-[11px] text-sc-ink3">Rule bots run on paper only for now.</p>
							{:else if execMode === 'paper'}
								<p class="mt-1.5 text-[11px] text-sc-ink3">Simulated fills against live market data — no real orders. {editExecutionMode === 'live' ? 'Saving will DISARM live execution (refused while real positions are open).' : ''}</p>
							{:else if editExecutionMode === 'live'}
								<p class="mt-1.5 text-[11px] text-red-400">Armed LIVE — real Hyperliquid orders through the full risk gate stack.</p>
							{/if}
						</div>

						{#if needsArming}
							<div class="border border-red-900 bg-red-500/5 p-3">
								<p class="text-[11px] leading-relaxed text-sc-ink2">
									Going live places <span class="font-bold text-red-400">real Hyperliquid orders</span>, admission-checked against the
									account's live risk budget on every open. A stop-loss % is required (set it under Risk &amp; Sizing — this save can include it).
								</p>
								<div class="mt-3 flex flex-wrap items-end gap-3">
									<div class="flex flex-col gap-1">
										<label for="arm-ceiling" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Per-order notional ceiling (USD)</label>
										<input id="arm-ceiling" type="number" min="1" bind:value={armCeiling} placeholder="e.g. 250" class="terminal-input w-44 focus:border-red-500" />
									</div>
									<div class="flex flex-col gap-1">
										<label for="arm-confirm" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Type <span class="font-bold text-red-400">GO LIVE</span> to confirm</label>
										<input id="arm-confirm" type="text" bind:value={armConfirm} placeholder="GO LIVE" class="terminal-input w-44 focus:border-red-500" />
									</div>
								</div>
								{#if stopLossPct == null}
									<p class="mt-2 text-[11px] text-yellow-500">No stop-loss set yet — going live will be refused until one is entered under Risk &amp; Sizing.</p>
								{/if}
								{#if !armReady}
									<p class="mt-2 text-[11px] text-sc-ink3">Save unlocks once the ceiling and typed confirmation are filled.</p>
								{/if}
							</div>
						{/if}

						<div>
							<label for="live-wallet" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Live orders route to</label>
							<select id="live-wallet" bind:value={liveWallet} disabled={editExecutionMode === 'live'} class="terminal-select w-72">
								<option value="">Master / shared (default)</option>
								{#each walletOptions as label}
									<option value={label}>{label} (isolated sub-account)</option>
								{/each}
								{#if liveWallet && !walletOptions.includes(liveWallet)}
									<option value={liveWallet}>{liveWallet} (unregistered!)</option>
								{/if}
							</select>
						</div>
						{#if editExecutionMode === 'live'}
							<p class="text-[11px] text-yellow-500">Bot is LIVE-armed — its wallet is locked. Switch to Paper (or re-arm via GO LIVE) to change it.</p>
						{:else if liveWallet}
							<p class="text-[11px] text-sc-ink3">Once armed live, ALL of this bot's orders trade on the <span class="font-bold text-sc-ink">{liveWallet}</span> sub-account — sized and budgeted against its own balance, fully isolated from the pipeline. Paper trading is unaffected.</p>
						{:else}
							<p class="text-[11px] text-sc-ink3">
								Master / shared: live orders use the main wallet (with direction books when enabled). Register an isolated sub-account under
								<a href="/settings#hyperliquid" class="text-sc-ink2 underline hover:text-sc-ink">Settings › HyperLiquid</a> to appear here.
							</p>
						{/if}
						{#if walletOptionsError}
							<p class="text-[11px] text-red-400">Wallet list failed: {walletOptionsError}</p>
						{/if}
					</div>
				</section>

				<!-- Schedule -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Schedule</div>
					<div class="space-y-4 p-4">
						<label class="flex items-center gap-2 text-xs text-sc-ink2">
							<input type="checkbox" bind:checked={sessionHoursEnabled} class="h-3.5 w-3.5 rounded-none border-sc-line2 bg-sc-bg" />
							Restrict to session hours
						</label>
						<p class="text-[11px] text-sc-ink3">Off = always active. On = the bot only trades within the window below.</p>

						{#if sessionHoursEnabled}
							<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
								<div>
									<label for="sessionTz" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Timezone</label>
									<input id="sessionTz" bind:value={sessionTimezone} placeholder="America/New_York" class="terminal-input" />
								</div>
								<div class="grid grid-cols-2 gap-4">
									<div>
										<label for="sessionStart" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Start</label>
										<input id="sessionStart" type="time" bind:value={sessionStart} class="terminal-input" />
									</div>
									<div>
										<label for="sessionEnd" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">End</label>
										<input id="sessionEnd" type="time" bind:value={sessionEnd} class="terminal-input" />
									</div>
								</div>
							</div>
							<div>
								<span class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Active Days</span>
								<div class="flex flex-wrap gap-1.5">
									{#each WEEKDAYS as day}
										<button
											type="button"
											on:click={() => toggleSessionDay(day)}
											class="{sessionDays.includes(day) ? 'terminal-button-primary' : 'terminal-button'} px-2.5 py-1 text-[12px]"
										>{day.slice(0, 3)}</button>
									{/each}
								</div>
								<p class="mt-1 text-[11px] text-sc-ink3">An end earlier than start is treated as an overnight window.</p>
							</div>
						{/if}
					</div>
				</section>

				<!-- Safety limits -->
				<section class="terminal-card">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Safety Limits</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="llmCap" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Daily LLM Call Cap</label>
							<input id="llmCap" type="number" bind:value={maxLlmCallsPerDay} min="1" step="1" class="terminal-input" />
						</div>
						<div>
							<label for="llmCostCap" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Daily AI Spend Cap ($) <span class="normal-case text-sc-ink3">— estimated, optional</span></label>
							<input id="llmCostCap" type="number" bind:value={maxLlmCostPerDay} min="0" step="0.5" placeholder="none" class="terminal-input" />
						</div>
						<div>
							<label for="maxErrors" class="mb-1 block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Circuit Breaker (max consecutive errors)</label>
							<input id="maxErrors" type="number" bind:value={maxConsecutiveErrors} min="1" step="1" class="terminal-input" />
						</div>
					</div>
				</section>
			</div>

			<!-- Summary rail -->
			<div class="lg:col-span-1">
				<div class="terminal-card sticky top-6">
					<div class="border-b border-sc-line px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Summary</div>
					<div class="space-y-0 text-xs">
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Bot</span>
							<span class="max-w-[60%] truncate font-bold text-sc-ink">{name || '—'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Mode</span>
							{#if execMode === 'live'}
								<span class="border border-red-900 bg-red-500/10 px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-400">
									Live{#if needsArming}<span class="text-sc-ink2"> — arms on save</span>{/if}
								</span>
							{:else}
								<span class="border border-sc-line2 px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">
									Paper{#if editExecutionMode === 'live'}<span class="text-yellow-500"> — disarms on save</span>{/if}
								</span>
							{/if}
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">{aiNeeded ? 'Model' : 'Type'}</span>
							<span class="max-w-[60%] truncate text-sc-ink">{aiNeeded ? model.trim() || 'default' : BOT_TYPES.find((t) => t.value === botType)?.label}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Capital</span>
							<span class="text-sc-ink">${(capitalAllocation || 0).toLocaleString()}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Max per position</span>
							<span class="text-sc-ink">${maxPositionUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })} <span class="text-sc-ink3">({maxPositionPct}%)</span></span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Concurrent / Drawdown</span>
							<span class="text-sc-ink">{maxConcurrentPositions} / {maxDrawdownPct}%</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Stop / Target</span>
							<span class="text-sc-ink">
								<span class={stopLossPct != null ? 'text-red-400' : 'text-sc-ink3'}>{stopLossPct != null ? `${stopLossPct}%` : 'none'}</span>
								<span class="text-sc-ink3"> / </span>
								<span class={takeProfitPct != null ? 'text-emerald-400' : 'text-sc-ink3'}>{takeProfitPct != null ? `${takeProfitPct}%` : 'none'}</span>
							</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Decides</span>
							<span class="text-sc-ink">{botType === 'ai' ? cadenceLabel : 'checks every minute'}</span>
						</div>
						<div class="flex items-start justify-between gap-3 border-b border-sc-line px-4 py-2.5">
							<span class="shrink-0 text-sc-ink3">Universe</span>
							<span class="text-right text-sc-ink">{botType === 'ai' ? universeLabel : botType === 'signal' ? (signalStrategyId ? `${signalStrategyId}'s coins` : 'pick a strategy') : rulePair}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Wallet</span>
							<span class="text-sc-ink">{liveWallet || 'master / shared'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">Session</span>
							<span class="text-sc-ink">{sessionHoursEnabled ? `${sessionStart}–${sessionEnd}` : 'always on'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-sc-line px-4 py-2.5">
							<span class="text-sc-ink3">AI budget</span>
							<span class="text-sc-ink">{maxLlmCallsPerDay} calls{maxLlmCostPerDay ? ` / $${maxLlmCostPerDay}` : ''} a day</span>
						</div>
						<div class="flex items-center justify-between px-4 py-2.5">
							<span class="text-sc-ink3">Projected AI use</span>
							<span class="text-right text-sc-ink">
								{#if !aiNeeded}
									None (decides in code)
								{:else if botType === 'signal'}
									One call per entry
								{:else if costEstimate}
									{Math.round(costEstimate.calls_per_day)} calls · {fmtUsdSmall(costEstimate.usd_per_day)}/day
								{:else}
									—
								{/if}
							</span>
						</div>
					</div>
					{#if aiNeeded && costEstimate && !costEstimate.model_check.ok}
						<div class="border-t border-red-900/50 bg-red-500/5 px-4 py-2.5 text-[11px] text-red-400">
							{costEstimate.model_check.reason} Start will refuse this model{costEstimate.model_check.default_model
								? `; your default is ${costEstimate.model_check.default_model}.`
								: '.'}
						</div>
					{/if}
					{#if botType === 'ai' && costEstimate?.hours_until_call_cap != null}
						<div class="border-t border-yellow-900/50 bg-yellow-500/5 px-4 py-2.5 text-[11px] text-yellow-500">
							At this cadence the bot uses its {maxLlmCallsPerDay} daily calls in about
							{costEstimate.hours_until_call_cap} hours, then pauses until UTC midnight.
							{decisionCadence === 'interval' ? 'Deciding on each new candle needs 24 calls a day.' : ''}
						</div>
					{:else if costEstimate?.hours_until_spend_cap != null}
						<div class="border-t border-yellow-900/50 bg-yellow-500/5 px-4 py-2.5 text-[11px] text-yellow-500">
							The spend cap pauses this bot after about {costEstimate.hours_until_spend_cap} hours a day.
						</div>
					{/if}
					{#if botType === 'ai' && costEstimate && !costEstimate.priced && costEstimate.usd_per_day != null}
						<div class="border-t border-sc-line px-4 py-2 text-[11px] text-sc-ink3">
							This model has no price entry; the cost uses {costEstimate.provider}'s highest known rate.
						</div>
					{/if}
					{#if stopLossPct == null}
						<div class="border-t border-yellow-900/50 bg-yellow-500/5 px-4 py-2.5 text-[11px] text-yellow-500">
							No stop-loss set — fine for paper, but GO LIVE will require one.
						</div>
					{/if}
					<div class="border-t border-sc-line p-3">
						{#if botType === 'ai'}
						<button
							on:click={handleTestDecision}
							disabled={testing || saving}
							class="terminal-button mb-2 w-full text-[12px]"
							title="Runs one real AI call on the latest candles. Nothing is traded or logged."
						>
							{testing ? 'Asking the model…' : `Test one decision${perCallUsd != null ? ` (~${fmtUsdSmall(perCallUsd)})` : ''}`}
						</button>
						{#if testError}
							<div class="mb-2 rounded-md border border-red-900/50 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">{testError}</div>
						{/if}
						{#if testResult}
							<div class="mb-2 rounded-md border border-sc-line2 bg-sc-bg px-3 py-2.5 text-[11px]">
								{#if !testResult.ran}
									<div class="text-yellow-500">{testResult.reason}</div>
								{:else}
									<div class="flex items-center justify-between">
										<span class="font-semibold text-sc-ink">{testResult.action}</span>
										<span class="text-sc-ink3">
											{testResult.model}{testResult.cost_usd != null ? ` · ${fmtUsdSmall(testResult.cost_usd)}` : ''}
										</span>
									</div>
									{#if testResult.parsed?.reasoning}
										<p class="mt-1.5 text-sc-ink2">{testResult.parsed.reasoning}</p>
									{/if}
									{#if testResult.risk_check}
										<p class="mt-1.5 {testResult.risk_check.allowed ? 'text-emerald-400' : 'text-red-400'}">
											{testResult.risk_check.reason}
										</p>
									{/if}
									<p class="mt-1.5 text-sc-ink3">
										Read {testResult.pairs?.join(', ')}{Object.keys(testResult.stale_pairs || {}).length
											? `; skipped stale ${Object.keys(testResult.stale_pairs).join(', ')}`
											: ''}. Nothing was traded or logged.
									</p>
									<button class="mt-1.5 text-sc-ink3 underline hover:text-sc-ink" on:click={() => (showTestPrompt = !showTestPrompt)}>
										{showTestPrompt ? 'Hide' : 'Show'} what the model saw
									</button>
									{#if showTestPrompt}
										{#each testResult.prompt || [] as message}
											<div class="mt-2 text-[10px] uppercase tracking-wide text-sc-ink3">{message.role}</div>
											<pre class="mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[10.5px] text-sc-ink2">{message.content}</pre>
										{/each}
										<div class="mt-2 text-[10px] uppercase tracking-wide text-sc-ink3">answer</div>
										<pre class="mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[10.5px] text-sc-ink2">{testResult.response}</pre>
									{/if}
								{/if}
							</div>
						{/if}
						{/if}
						<button
							on:click={handleSave}
							disabled={saving || savingTemplate || !name.trim() || !armReady}
							class="terminal-button-primary w-full text-[12px]"
						>
							{saving ? 'Saving…' : needsArming ? 'Save + Arm Live' : editId ? 'Save Changes' : 'Create Bot'}
						</button>
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>
