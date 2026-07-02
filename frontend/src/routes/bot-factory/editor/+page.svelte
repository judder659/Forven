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
		type BotConfig,
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
	$: cadenceLabel = cooldownSeconds >= 3600
		? `${(cooldownSeconds / 3600).toFixed(1)}h`
		: cooldownSeconds >= 60
			? `${Math.round(cooldownSeconds / 60)}m`
			: `${cooldownSeconds}s`;
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
	<div class="mb-6 flex items-end justify-between border-b border-[#222] pb-4">
		<div>
			<button on:click={() => goto('/bot-factory')} class="mb-1 text-[11px] uppercase tracking-wider text-[#555] hover:text-white">&larr; Bot Factory</button>
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">{editId ? 'Edit Bot' : 'New Bot'}</h1>
		</div>
		<div class="flex items-center gap-2">
			<button
				on:click={() => (templateFormOpen = !templateFormOpen)}
				disabled={saving || savingTemplate || !name.trim()}
				class="terminal-button text-xs"
			>
				Save as Template
			</button>
			<button
				on:click={handleSave}
				disabled={saving || savingTemplate || !name.trim() || !armReady}
				class="terminal-button-primary text-xs"
			>
				{saving ? 'Saving…' : needsArming ? 'Save + Arm Live' : editId ? 'Save Changes' : 'Create Bot'}
			</button>
		</div>
	</div>

	{#if error}
		<div class="mb-4 border border-red-900 bg-red-500/5 p-3 text-sm text-red-400">{error}</div>
	{/if}

	{#if templateFormOpen}
		<div class="mb-6 border border-[#333] bg-[#050505] p-4">
			<div class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Save current config as template</div>
			<div class="mt-3 flex flex-wrap items-end gap-3">
				<div class="flex flex-col gap-1">
					<label for="tpl-name" class="text-[10px] uppercase tracking-wider text-[#666]">Name</label>
					<input id="tpl-name" bind:value={templateName} placeholder={name} class="terminal-input w-56" />
				</div>
				<div class="flex min-w-0 flex-1 flex-col gap-1">
					<label for="tpl-desc" class="text-[10px] uppercase tracking-wider text-[#666]">Description (optional)</label>
					<input id="tpl-desc" bind:value={templateDesc} class="terminal-input" />
				</div>
				<button on:click={handleSaveAsTemplate} disabled={savingTemplate || !templateName.trim()} class="terminal-button-primary text-xs">
					{savingTemplate ? 'Saving…' : 'Save'}
				</button>
				<button on:click={() => (templateFormOpen = false)} class="terminal-button text-xs">Cancel</button>
			</div>
		</div>
	{/if}

	{#if loading}
		<div class="py-20 text-center text-xs uppercase tracking-widest text-[#555]">Loading…</div>
	{:else}
		<!-- Template gallery (only on create, initially) -->
		{#if showTemplateSelector && !editId && templates.length > 0}
			<div class="mb-6 border border-[#222] bg-[#050505] p-4">
				<div class="mb-3 text-[10px] font-bold uppercase tracking-widest text-[#888]">Start from a template</div>
				<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
					{#each templates as template}
						<div class="relative">
							<button
								on:click={() => { populateFromConfig(template.config_snapshot); name = template.name; showTemplateSelector = false; }}
								class="w-full border border-[#222] bg-black p-3 text-left transition-colors hover:border-[#555]"
							>
								<div class="pr-8 text-sm font-bold text-white">{template.name}</div>
								<div class="mt-0.5 text-xs text-[#777]">{template.description}</div>
							</button>
							{#if !template.is_builtin}
								{#if confirmDeleteTemplateId === template.id}
									<div class="absolute right-2 top-2 flex gap-1">
										<button
											type="button"
											on:click={() => handleDeleteTemplate(template)}
											disabled={deletingTemplateId === template.id}
											class="border border-red-900 px-1.5 py-0.5 text-[9px] font-bold uppercase text-red-400 hover:bg-red-500 hover:text-white"
										>{deletingTemplateId === template.id ? '…' : 'Delete'}</button>
										<button type="button" on:click={() => (confirmDeleteTemplateId = null)} class="border border-[#333] px-1.5 py-0.5 text-[9px] font-bold uppercase text-[#888]">×</button>
									</div>
								{:else}
									<button
										type="button"
										on:click={() => (confirmDeleteTemplateId = template.id)}
										aria-label={`Delete template ${template.name}`}
										title="Delete template"
										class="absolute right-2 top-2 flex h-6 w-6 items-center justify-center text-[#555] transition-colors hover:text-red-400"
									>&times;</button>
								{/if}
							{/if}
						</div>
					{/each}
				</div>
				<button on:click={() => (showTemplateSelector = false)} class="mt-3 text-[11px] uppercase tracking-wider text-[#666] hover:text-white">
					or start from scratch &rarr;
				</button>
			</div>
		{/if}

		<div class="grid grid-cols-1 gap-6 lg:grid-cols-3">
			<!-- Form column -->
			<div class="space-y-6 lg:col-span-2">
				<!-- Identity -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Identity</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="name" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Name</label>
							<input id="name" bind:value={name} class="terminal-input" />
						</div>
						<div>
							<label for="model" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Model</label>
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
							<p class="mt-1 text-[11px] text-[#555]">
								{#if modelKeys.length === 0}
									Blank = your configured default provider. Enable models on the <a href="/agents" class="text-[#999] underline hover:text-white">Agents</a> page to pick a specific one.
								{:else}
									Blank = your configured default provider. List = models you've enabled on the Agents page.
								{/if}
							</p>
						</div>
					</div>
				</section>

				<!-- Persona -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Persona &amp; Strategy</div>
					<div class="space-y-4 p-4">
						<div>
							<label for="soul" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Soul <span class="normal-case text-[#555]">— personality, temperament, decision style</span></label>
							<textarea id="soul" bind:value={soul} rows="4" class="terminal-input resize-y" placeholder="You are an aggressive momentum trader who thrives on volatility..."></textarea>
						</div>
						<div>
							<label for="strategy" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Strategy <span class="normal-case text-[#555]">— trading approach, broad or narrow</span></label>
							<textarea id="strategy" bind:value={strategy} rows="4" class="terminal-input resize-y" placeholder="Trade momentum breakouts on high-volume assets..."></textarea>
						</div>
						<div>
							<label for="context" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Context <span class="normal-case text-[#555]">— seed knowledge, research notes, market thesis</span></label>
							<textarea id="context" bind:value={context} rows="3" class="terminal-input resize-y" placeholder="BTC tends to correlate with macro risk-on sentiment..."></textarea>
						</div>
						<div>
							<label for="guardrails" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Guardrails <span class="normal-case text-[#555]">— behavioral rules (best-effort, LLM-interpreted)</span></label>
							<textarea id="guardrails" bind:value={guardrails} rows="3" class="terminal-input resize-y" placeholder="Never hold a position for more than 2 hours..."></textarea>
						</div>
					</div>
				</section>

				<!-- Risk & sizing -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Risk &amp; Sizing</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="capital" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Capital Allocation ($)</label>
							<input id="capital" type="number" bind:value={capitalAllocation} min="0.01" step="any" class="terminal-input" />
						</div>
						<div>
							<label for="maxPos" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Max Position Size (%)</label>
							<input id="maxPos" type="number" bind:value={maxPositionPct} min="0.01" max="100" step="0.5" class="terminal-input" />
						</div>
						<div>
							<label for="maxConcurrent" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Max Concurrent Positions</label>
							<input id="maxConcurrent" type="number" bind:value={maxConcurrentPositions} min="1" max="100" step="1" class="terminal-input" />
						</div>
						<div>
							<label for="maxDrawdown" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Max Drawdown (%)</label>
							<input id="maxDrawdown" type="number" bind:value={maxDrawdownPct} min="0.01" max="100" step="0.5" class="terminal-input" />
						</div>
						<div>
							<label for="stopLoss" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Stop Loss (%) <span class="normal-case text-[#555]">— required for live</span></label>
							<input id="stopLoss" type="number" bind:value={stopLossPct} min="0.01" max="100" step="0.5" placeholder="none" class="terminal-input" />
						</div>
						<div>
							<label for="takeProfit" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Take Profit (%) <span class="normal-case text-[#555]">— optional</span></label>
							<input id="takeProfit" type="number" bind:value={takeProfitPct} min="0.01" step="0.5" placeholder="none" class="terminal-input" />
						</div>
					</div>
				</section>

				<!-- Execution -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Execution</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="takerFee" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Taker Fee (bps) <span class="normal-case text-[#555]">— paper cost model</span></label>
							<input id="takerFee" type="number" bind:value={takerFeeBps} min="0" step="0.1" class="terminal-input" />
						</div>
						<div>
							<label for="slippage" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Slippage (bps) <span class="normal-case text-[#555]">— paper cost model</span></label>
							<input id="slippage" type="number" bind:value={slippageBps} min="0" step="0.1" class="terminal-input" />
						</div>
						<div>
							<label for="cooldown" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Decision Cadence (seconds)</label>
							<input id="cooldown" type="number" bind:value={cooldownSeconds} min="1" step="1" class="terminal-input" />
						</div>
						<div>
							<label for="verbosity" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Reasoning Verbosity</label>
							<select id="verbosity" bind:value={reasoningVerbosity} class="terminal-select">
								<option value="minimal">Minimal</option>
								<option value="standard">Standard</option>
								<option value="verbose">Verbose</option>
							</select>
						</div>
					</div>
				</section>

				<!-- Universe -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Universe</div>
					<div class="space-y-4 p-4">
						<div class="flex gap-2">
							<button
								on:click={() => (assetMode = 'free_roam')}
								class="{assetMode === 'free_roam' ? 'terminal-button-primary' : 'terminal-button'} text-xs"
							>Free Roam</button>
							<button
								on:click={() => (assetMode = 'locked')}
								class="{assetMode === 'locked' ? 'terminal-button-primary' : 'terminal-button'} text-xs"
							>Locked Pairs</button>
						</div>
						{#if assetMode === 'free_roam'}
							<p class="text-[11px] text-[#555]">Watches the top perps by 24h volume from the research universe (up to 6 pairs, refreshed hourly). Use Locked Pairs to pin specific symbols.</p>
						{:else}
							<div>
								<label for="pairs" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Locked Pairs (comma-separated)</label>
								<input id="pairs" bind:value={lockedPairsText} class="terminal-input" placeholder="BTC/USDT, ETH/USDT, SOL/USDT" />
							</div>
						{/if}
					</div>
				</section>

				<!-- Execution: paper vs live + wallet -->
				<section class="terminal-card {execMode === 'live' ? 'border-red-900' : ''}">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Execution</div>
					<div class="space-y-4 p-4">
						<div>
							<span class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Mode</span>
							<div class="flex gap-2">
								<button
									on:click={() => (execMode = 'paper')}
									class="{execMode === 'paper' ? 'terminal-button-primary' : 'terminal-button'} text-xs"
								>Paper</button>
								<button
									on:click={() => (execMode = 'live')}
									class="text-xs {execMode === 'live' ? 'border border-red-500 bg-red-500 px-3 py-1.5 font-bold uppercase tracking-wide text-white' : 'terminal-button-danger'}"
								>Live</button>
							</div>
							{#if execMode === 'paper'}
								<p class="mt-1.5 text-[11px] text-[#555]">Simulated fills against live market data — no real orders. {editExecutionMode === 'live' ? 'Saving will DISARM live execution (refused while real positions are open).' : ''}</p>
							{:else if editExecutionMode === 'live'}
								<p class="mt-1.5 text-[11px] text-red-400">Armed LIVE — real Hyperliquid orders through the full risk gate stack.</p>
							{/if}
						</div>

						{#if needsArming}
							<div class="border border-red-900 bg-red-500/5 p-3">
								<p class="text-[11px] leading-relaxed text-[#aaa]">
									Going live places <span class="font-bold text-red-400">real Hyperliquid orders</span>, admission-checked against the
									account's live risk budget on every open. A stop-loss % is required (set it under Risk &amp; Sizing — this save can include it).
								</p>
								<div class="mt-3 flex flex-wrap items-end gap-3">
									<div class="flex flex-col gap-1">
										<label for="arm-ceiling" class="text-[10px] uppercase tracking-wider text-[#666]">Per-order notional ceiling (USD)</label>
										<input id="arm-ceiling" type="number" min="1" bind:value={armCeiling} placeholder="e.g. 250" class="terminal-input w-44 focus:border-red-500" />
									</div>
									<div class="flex flex-col gap-1">
										<label for="arm-confirm" class="text-[10px] uppercase tracking-wider text-[#666]">Type <span class="font-bold text-red-400">GO LIVE</span> to confirm</label>
										<input id="arm-confirm" type="text" bind:value={armConfirm} placeholder="GO LIVE" class="terminal-input w-44 focus:border-red-500" />
									</div>
								</div>
								{#if stopLossPct == null}
									<p class="mt-2 text-[11px] text-yellow-500">No stop-loss set yet — going live will be refused until one is entered under Risk &amp; Sizing.</p>
								{/if}
								{#if !armReady}
									<p class="mt-2 text-[11px] text-[#555]">Save unlocks once the ceiling and typed confirmation are filled.</p>
								{/if}
							</div>
						{/if}

						<div>
							<label for="live-wallet" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Live orders route to</label>
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
							<p class="text-[11px] text-[#555]">Once armed live, ALL of this bot's orders trade on the <span class="font-bold text-white">{liveWallet}</span> sub-account — sized and budgeted against its own balance, fully isolated from the pipeline. Paper trading is unaffected.</p>
						{:else}
							<p class="text-[11px] text-[#555]">
								Master / shared: live orders use the main wallet (with direction books when enabled). Register an isolated sub-account under
								<a href="/settings#hyperliquid" class="text-[#999] underline hover:text-white">Settings › HyperLiquid</a> to appear here.
							</p>
						{/if}
						{#if walletOptionsError}
							<p class="text-[11px] text-red-400">Wallet list failed: {walletOptionsError}</p>
						{/if}
					</div>
				</section>

				<!-- Schedule -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Schedule</div>
					<div class="space-y-4 p-4">
						<label class="flex items-center gap-2 text-xs text-[#aaa]">
							<input type="checkbox" bind:checked={sessionHoursEnabled} class="h-3.5 w-3.5 rounded-none border-[#333] bg-black" />
							Restrict to session hours
						</label>
						<p class="text-[11px] text-[#555]">Off = always active. On = the bot only trades within the window below.</p>

						{#if sessionHoursEnabled}
							<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
								<div>
									<label for="sessionTz" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Timezone</label>
									<input id="sessionTz" bind:value={sessionTimezone} placeholder="America/New_York" class="terminal-input" />
								</div>
								<div class="grid grid-cols-2 gap-4">
									<div>
										<label for="sessionStart" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Start</label>
										<input id="sessionStart" type="time" bind:value={sessionStart} class="terminal-input" />
									</div>
									<div>
										<label for="sessionEnd" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">End</label>
										<input id="sessionEnd" type="time" bind:value={sessionEnd} class="terminal-input" />
									</div>
								</div>
							</div>
							<div>
								<span class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Active Days</span>
								<div class="flex flex-wrap gap-1.5">
									{#each WEEKDAYS as day}
										<button
											type="button"
											on:click={() => toggleSessionDay(day)}
											class="{sessionDays.includes(day) ? 'terminal-button-primary' : 'terminal-button'} px-2.5 py-1 text-[10px]"
										>{day.slice(0, 3)}</button>
									{/each}
								</div>
								<p class="mt-1 text-[11px] text-[#555]">An end earlier than start is treated as an overnight window.</p>
							</div>
						{/if}
					</div>
				</section>

				<!-- Safety limits -->
				<section class="terminal-card">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Safety Limits</div>
					<div class="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
						<div>
							<label for="llmCap" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Daily LLM Call Cap</label>
							<input id="llmCap" type="number" bind:value={maxLlmCallsPerDay} min="1" step="1" class="terminal-input" />
						</div>
						<div>
							<label for="maxErrors" class="mb-1 block text-[10px] uppercase tracking-wider text-[#666]">Circuit Breaker (max consecutive errors)</label>
							<input id="maxErrors" type="number" bind:value={maxConsecutiveErrors} min="1" step="1" class="terminal-input" />
						</div>
					</div>
				</section>
			</div>

			<!-- Summary rail -->
			<div class="lg:col-span-1">
				<div class="terminal-card sticky top-6">
					<div class="border-b border-[#1a1a1a] px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#888]">Summary</div>
					<div class="space-y-0 text-xs">
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Bot</span>
							<span class="max-w-[60%] truncate font-bold text-white">{name || '—'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Mode</span>
							{#if execMode === 'live'}
								<span class="border border-red-900 bg-red-500/10 px-1.5 py-px text-[9px] font-bold uppercase tracking-widest text-red-400">
									Live{#if needsArming}<span class="text-[#888]"> — arms on save</span>{/if}
								</span>
							{:else}
								<span class="border border-[#333] px-1.5 py-px text-[9px] font-bold uppercase tracking-widest text-[#888]">
									Paper{#if editExecutionMode === 'live'}<span class="text-yellow-500"> — disarms on save</span>{/if}
								</span>
							{/if}
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Model</span>
							<span class="max-w-[60%] truncate text-white">{model.trim() || 'default'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Capital</span>
							<span class="text-white">${(capitalAllocation || 0).toLocaleString()}</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Max per position</span>
							<span class="text-white">${maxPositionUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })} <span class="text-[#555]">({maxPositionPct}%)</span></span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Concurrent / Drawdown</span>
							<span class="text-white">{maxConcurrentPositions} / {maxDrawdownPct}%</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Stop / Target</span>
							<span class="text-white">
								<span class={stopLossPct != null ? 'text-red-400' : 'text-[#555]'}>{stopLossPct != null ? `${stopLossPct}%` : 'none'}</span>
								<span class="text-[#555]"> / </span>
								<span class={takeProfitPct != null ? 'text-emerald-400' : 'text-[#555]'}>{takeProfitPct != null ? `${takeProfitPct}%` : 'none'}</span>
							</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Cadence</span>
							<span class="text-white">every {cadenceLabel}</span>
						</div>
						<div class="flex items-start justify-between gap-3 border-b border-[#111] px-4 py-2.5">
							<span class="shrink-0 text-[#666]">Universe</span>
							<span class="text-right text-white">{universeLabel}</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Wallet</span>
							<span class="text-white">{liveWallet || 'master / shared'}</span>
						</div>
						<div class="flex items-center justify-between border-b border-[#111] px-4 py-2.5">
							<span class="text-[#666]">Session</span>
							<span class="text-white">{sessionHoursEnabled ? `${sessionStart}–${sessionEnd}` : 'always on'}</span>
						</div>
						<div class="flex items-center justify-between px-4 py-2.5">
							<span class="text-[#666]">LLM budget</span>
							<span class="text-white">{maxLlmCallsPerDay}/day</span>
						</div>
					</div>
					{#if stopLossPct == null}
						<div class="border-t border-yellow-900/50 bg-yellow-500/5 px-4 py-2.5 text-[11px] text-yellow-500">
							No stop-loss set — fine for paper, but GO LIVE will require one.
						</div>
					{/if}
					<div class="border-t border-[#1a1a1a] p-3">
						<button
							on:click={handleSave}
							disabled={saving || savingTemplate || !name.trim() || !armReady}
							class="terminal-button-primary w-full text-xs"
						>
							{saving ? 'Saving…' : needsArming ? 'Save + Arm Live' : editId ? 'Save Changes' : 'Create Bot'}
						</button>
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>
