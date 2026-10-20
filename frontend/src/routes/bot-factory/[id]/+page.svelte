<script lang="ts">
	function ruleSummary(b: { bot_type?: string | null; rule_params?: Record<string, any> | null }): string {
		const p = b.rule_params || {};
		if (b.bot_type === 'grid')
			return `Grid bot · ${p.pair ?? '?'} · ${p.levels ?? '?'} levels from $${p.lower ?? '?'} to $${p.upper ?? '?'}`;
		if (b.bot_type === 'dca')
			return `DCA bot · ${p.pair ?? '?'} · ${p.safety_orders ?? 0} safety orders · take profit ${p.take_profit_pct ?? 2}%`;
		if (b.bot_type === 'signal')
			return `Signal follower · copies ${p.strategy_id ?? '?'}${p.ai_approval ? ' · AI approves entries' : ''}`;
		return '';
	}
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		getBot,
		getBotTrades,
		getBotDecisions,
		getBotVersions,
		getBotMemory,
		getBotPositions,
		getBotStats,
		diffBotVersions,
		startBot,
		stopBot,
		useDefaultModel,
		goLiveBot,
		goPaperBot,
		closeBotPosition,
		listWallets,
		getBotDecision,
		getBotPerformance,
		getBotChart,
		applyRecommendedBotSettings,
		type BotDecisionDetail,
		type BotPerformance,
		type BotChart,
		type BotConfig,
		type BotTrade,
		type BotDecision,
		type BotConfigVersion,
		type BotMemoryEntry,
		type BotPositionsSnapshot,
		type BotStats,
		type BotVersionDiff,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import CandlestickChart from '$lib/components/CandlestickChart.svelte';
	import { forvenLivePrices } from '$lib/stores/forvenWebSocket';
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from '$lib/utils/realtime';

	let bot: BotConfig | null = null;
	let trades: BotTrade[] = [];
	let decisions: BotDecision[] = [];
	let versions: BotConfigVersion[] = [];
	let memoryEntries: BotMemoryEntry[] = [];
	let positions: BotPositionsSnapshot | null = null;
	let stats: BotStats | null = null;
	let loading = true;
	let error: string | null = null;
	let confirmStop = false;
	let activeView: 'performance' | 'activity' | 'positions' | 'trades' | 'versions' | 'memory' = 'performance';
	let botPerf: BotPerformance | null = null;
	let chart: BotChart | null = null;
	let chartPair: string | null = null;

	// Decision inspector: what one decision was shown and answered.
	let inspecting: BotDecisionDetail | null = null;
	let inspectLoading = false;
	let inspectError: string | null = null;
	let applyingRecommended = false;
	let realtime: RealtimeRefreshController | null = null;

	// Live marks come from the app's own /ws/live feed (daemon prices, keyed by
	// coin e.g. "BTC"), with the positions snapshot's mark as fallback — no
	// third-party sockets.
	$: livePrices = $forvenLivePrices;

	function coinOf(symbol: string | null | undefined): string {
		const raw = String(symbol || '').toUpperCase();
		return raw.includes('/') ? raw.split('/')[0] : raw;
	}

	function getLivePrice(symbol: string, prices: Record<string, number>): number | null {
		const p = prices[coinOf(symbol)];
		return typeof p === 'number' && p > 0 ? p : null;
	}

	function computePnl(trade: BotTrade, prices: Record<string, number>): { pnl: number; pnlPct: number } | null {
		if (trade.status !== 'OPEN') return trade.pnl != null ? { pnl: trade.pnl, pnlPct: trade.pnl_pct ?? 0 } : null;
		const current = getLivePrice(trade.symbol || trade.asset, prices);
		if (!current || !trade.entry_price) return null;
		const size = trade.size || 1;
		const pnl = trade.direction === 'long'
			? (current - trade.entry_price) * size
			: (trade.entry_price - current) * size;
		const pnlPct = trade.direction === 'long'
			? ((current - trade.entry_price) / trade.entry_price) * 100
			: ((trade.entry_price - current) / trade.entry_price) * 100;
		return { pnl, pnlPct };
	}

	$: botId = $page.params.id;
	$: statusLabel = bot?.runtime_status || bot?.status || 'unknown';
	$: isRunning = statusLabel === 'running';
	$: isLiveMode = bot?.execution_mode === 'live';

	// Live unrealized P&L summed across currently-open positions: WS mark first,
	// then the snapshot's mark (daemon KV). Positions with no mark at all
	// contribute 0 (we don't fabricate a mark from the entry price).
	$: unrealizedPnl = (() => {
		if (!positions) return 0;
		return positions.open_positions.reduce((sum, pos) => {
			const current = getLivePrice(pos.ticker, livePrices) ?? pos.current_price;
			if (current == null || !pos.entry_price) return sum;
			const delta = pos.direction === 'long'
				? (current - pos.entry_price)
				: (pos.entry_price - current);
			return sum + delta * pos.qty;
		}, 0);
	})();

	// Drawdown gauge mirrors the backend's session-scoped enforcement: it works
	// off the positions snapshot's peak_equity and realized_pnl (which already
	// reset when the bot starts), plus live unrealized when a mark is available.
	// If peak_equity is null (no peak recorded yet), fall back to starting
	// capital as the peak.
	$: currentEquity = positions
		? positions.starting_capital + positions.realized_pnl + unrealizedPnl
		: (bot?.capital_allocation ?? 0);
	$: peakEquity = positions
		? (positions.peak_equity ?? positions.starting_capital)
		: (bot?.capital_allocation ?? 0);
	$: drawdownPct = peakEquity > 0
		? Math.max(0, ((peakEquity - currentEquity) / peakEquity) * 100)
		: 0;
	$: drawdownColor = (() => {
		const limit = bot?.max_drawdown_pct ?? 3;
		if (drawdownPct >= limit) return 'text-red-400';
		if (drawdownPct >= limit * 0.5) return 'text-yellow-400';
		return 'text-sc-ink';
	})();

	// Displayed equity: a LIVE bot shows the REAL balance of the wallet it
	// trades (from the daemon snapshot); paper shows the simulated sandbox
	// equity. Drawdown math above stays on the session-scoped currentEquity.
	$: liveWalletEquity = positions?.live_wallet_equity ?? null;
	// Live positions that stopping will close (reduce-only) — drives the
	// stop-confirmation warning so closing real positions is never a surprise.
	$: liveOpenCount = (positions?.open_positions ?? []).filter(
		(p) => p.execution_type === 'live'
	).length;

	function statusColor(s: string): string {
		if (s === 'running') return 'text-emerald-400';
		if (s === 'error') return 'text-red-400';
		if (s === 'paused') return 'text-yellow-400';
		return 'text-sc-ink3';
	}

	async function load() {
		const currentBotId = botId;
		if (!currentBotId) {
			error = 'Missing bot id';
			loading = false;
			return;
		}

		try {
			[bot, trades, decisions, versions, memoryEntries, positions, stats] = await Promise.all([
				getBot(currentBotId),
				getBotTrades(currentBotId, 50),
				getBotDecisions(currentBotId, 100),
				getBotVersions(currentBotId),
				getBotMemory(currentBotId, 50).catch(() => []),
				getBotPositions(currentBotId).catch(() => null),
				getBotStats(currentBotId).catch(() => null),
			]);
			// Cockpit views are extra: the page still works without them.
			[botPerf, chart] = await Promise.all([
				getBotPerformance(currentBotId).catch(() => botPerf),
				getBotChart(currentBotId, chartPair).catch(() => chart),
			]);
			if (chart && !chartPair) chartPair = chart.pair;
			error = null;
		} catch (e: any) {
			error = e.message || 'Failed to load bot';
		} finally {
			loading = false;
		}
	}

	// Start refusals (a retired model, missing live arming) stay on the page
	// with their fix, instead of vanishing in a toast.
	let startError: string | null = null;
	let fixingModel = false;

	async function handleStart() {
		if (!botId) return;
		startError = null;
		try {
			await startBot(botId);
			addToast('Bot started', 'success');
			await load();
		} catch (e: any) {
			startError = e.message || 'Start failed';
		}
	}

	async function handleUseDefaultModel() {
		if (!botId) return;
		fixingModel = true;
		try {
			bot = await useDefaultModel(botId);
			startError = null;
			addToast(`Model switched to ${bot.model}`, 'success');
		} catch (e: any) {
			addToast(`Couldn't switch model: ${e.message}`, 'error');
		} finally {
			fixingModel = false;
		}
	}

	async function selectChartPair(pair: string) {
		if (!botId) return;
		chartPair = pair;
		try {
			chart = await getBotChart(botId, pair);
		} catch (e: any) {
			addToast(`Couldn't load the ${pair} chart: ${e.message}`, 'error');
		}
	}

	async function openInspector(decisionId: number) {
		if (!botId) return;
		inspectLoading = true;
		inspectError = null;
		inspecting = null;
		try {
			inspecting = await getBotDecision(botId, decisionId);
		} catch (e: any) {
			inspectError = e.message || 'Could not load this decision';
		} finally {
			inspectLoading = false;
		}
	}

	function closeInspector() {
		inspecting = null;
		inspectError = null;
		inspectLoading = false;
	}

	async function handleApplyRecommended() {
		if (!botId) return;
		applyingRecommended = true;
		try {
			bot = await applyRecommendedBotSettings(botId);
			addToast('Bot now decides once per new candle', 'success');
		} catch (e: any) {
			addToast(`Change failed: ${e.message}`, 'error');
		} finally {
			applyingRecommended = false;
		}
	}

	function fmtSignedPct(v: number | null | undefined, digits = 2): string {
		if (v == null || !Number.isFinite(v)) return '—';
		return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
	}

	function toneOf(v: number | null | undefined): string {
		if (v == null) return 'text-sc-ink';
		return v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-sc-ink';
	}

	$: chartSignals = chart
		? {
				entries: chart.entries.map((m) => ({
					timestamp: m.timestamp,
					price: m.price,
					type: 'entry' as const,
					label: m.direction === 'short' ? 'SHORT' : 'LONG',
				})),
				exits: chart.exits.map((m) => ({
					timestamp: m.timestamp,
					price: m.price,
					type: 'exit' as const,
					label: m.pnl_usd != null ? `EXIT ${m.pnl_usd >= 0 ? '+' : ''}${m.pnl_usd.toFixed(2)}` : 'EXIT',
				})),
			}
		: null;

	function fmtCost(value: number | null | undefined): string {
		if (value == null) return '—';
		if (value < 0.01) return `$${value.toFixed(4)}`;
		return `$${value.toFixed(2)}`;
	}

	async function handleStop() {
		confirmStop = false;
		if (!botId) return;
		try {
			const res = await stopBot(botId);
			const flattened = res.flattened ?? [];
			const closed = flattened.filter((f) => f.state === 'closed').length;
			const pending = flattened.filter((f) => f.state === 'pending').length;
			const failed = flattened.filter((f) => f.state === 'failed').length;
			if (failed > 0) {
				addToast(`Bot stopped — ${failed} live close(s) FAILED, check positions`, 'error', undefined, 12_000);
			} else if (closed || pending) {
				const parts = [];
				if (closed) parts.push(`${closed} position(s) closed`);
				if (pending) parts.push(`${pending} pending exchange reconcile`);
				addToast(`Bot stopped — ${parts.join(', ')}`, 'success', undefined, 8000);
			} else {
				addToast('Bot stopped', 'success');
			}
			await load();
		} catch (e: any) {
			addToast(`Stop failed: ${e.message}`, 'error');
		}
	}

	// ── Execution mode (paper / live) ────────────────────────────────
	let goLiveOpen = false;
	let goLiveConfirm = '';
	let goLiveCeiling: number | null = null;
	let goLiveWallet: string = ''; // '' = master / direction books (shared)
	let walletOptions: string[] = [];
	let walletOptionsLoading = false;
	let walletOptionsError: string | null = null;
	let confirmGoPaper = false;
	let modeBusy = false;

	async function openGoLiveDrawer() {
		goLiveOpen = !goLiveOpen;
		if (!goLiveOpen) return;
		walletOptionsLoading = true;
		walletOptionsError = null;
		try {
			// light=true: labels only, no exchange round-trips — the select must
			// populate instantly and never hang on a slow balance read.
			const snapshot = await listWallets(true);
			walletOptions = snapshot.registered.map((w) => w.label);
			// Preselect the bot's previously armed wallet if it still exists.
			const prev = bot?.live_wallet ?? '';
			goLiveWallet = prev && walletOptions.includes(prev) ? prev : '';
		} catch (e: any) {
			walletOptions = [];
			walletOptionsError = String(e.message || e);
		} finally {
			walletOptionsLoading = false;
		}
	}

	async function handleGoLive() {
		if (!botId || modeBusy) return;
		modeBusy = true;
		try {
			bot = await goLiveBot(
				botId,
				goLiveConfirm.trim(),
				goLiveCeiling ?? 0,
				goLiveWallet || null
			);
			addToast('Bot armed for LIVE execution', 'success');
			goLiveOpen = false;
			goLiveConfirm = '';
			goLiveCeiling = null;
			await load();
		} catch (e: any) {
			addToast(`Go live failed: ${e.message}`, 'error');
		} finally {
			modeBusy = false;
		}
	}

	async function handleGoPaper() {
		if (!botId || modeBusy) return;
		confirmGoPaper = false;
		modeBusy = true;
		try {
			bot = await goPaperBot(botId);
			addToast('Bot returned to paper mode', 'success');
			await load();
		} catch (e: any) {
			addToast(`Go paper failed: ${e.message}`, 'error');
		} finally {
			modeBusy = false;
		}
	}

	// ── Manual position close ────────────────────────────────────────
	let confirmClosePos: string | null = null;
	let closeBusy = new Set<string>();

	async function handleClosePosition(tradeId: string) {
		if (!botId || closeBusy.has(tradeId)) return;
		confirmClosePos = null;
		closeBusy = new Set(closeBusy).add(tradeId);
		try {
			const result = await closeBotPosition(botId, tradeId);
			if (result.status === 'pending') {
				addToast('Close order sent — awaiting exchange reconcile', 'info');
			} else {
				const pnl = result.net_pnl;
				addToast(
					pnl != null ? `Position closed (${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)})` : 'Position closed',
					pnl != null && pnl < 0 ? 'warning' : 'success'
				);
			}
			await load();
		} catch (e: any) {
			addToast(`Close failed: ${e.message}`, 'error');
		} finally {
			const next = new Set(closeBusy);
			next.delete(tradeId);
			closeBusy = next;
		}
	}

	function actionTypeIcon(type: string): string {
		if (type === 'trade') return '💰';
		if (type === 'observation') return '👁';
		if (type === 'pass') return '➖';
		if (type === 'error') return '⚠️';
		if (type === 'paused') return '⏸';
		if (type === 'skipped') return '⏭';
		if (type === 'blocked') return '⛔';
		return '•';
	}

	function actionTypeColor(type: string): string {
		if (type === 'trade') return 'text-emerald-400';
		if (type === 'observation') return 'text-sc-ink2';
		if (type === 'pass') return 'text-sc-ink2';
		if (type === 'error') return 'text-red-400';
		if (type === 'paused') return 'text-yellow-400';
		if (type === 'skipped') return 'text-yellow-400';
		if (type === 'blocked') return 'text-yellow-400';
		return 'text-sc-ink';
	}

	// BUY/SELL/SHORT/COVER from a trade decision's action_data, if present.
	function tradeAction(d: BotDecision): string | null {
		const a = d.action_data?.action;
		return typeof a === 'string' ? a.toUpperCase() : null;
	}

	function tradeDirection(d: BotDecision): string | null {
		const dir = d.action_data?.direction;
		return typeof dir === 'string' ? dir : null;
	}

	function actionBadgeColor(action: string): string {
		if (action === 'BUY' || action === 'COVER') return 'border border-emerald-900 bg-emerald-500/10 text-emerald-400';
		if (action === 'SELL' || action === 'SHORT') return 'border border-red-900 bg-red-500/10 text-red-400';
		return 'border border-sc-line2 text-sc-ink2';
	}

	function formatTime(ts: string | null): string {
		if (!ts) return '';
		try {
			return new Date(ts).toLocaleString();
		} catch {
			return ts;
		}
	}

	function relativeTime(ts: string | null): string {
		if (!ts) return '';
		try {
			const diff = Date.now() - new Date(ts).getTime();
			const mins = Math.floor(diff / 60000);
			if (mins < 1) return 'just now';
			if (mins < 60) return `${mins}m ago`;
			const hours = Math.floor(mins / 60);
			if (hours < 24) return `${hours}h ago`;
			return `${Math.floor(hours / 24)}d ago`;
		} catch {
			return '';
		}
	}

	// ── Config-version diff (Versions tab) ────────────────────────────
	// The diff endpoint can return either a BotVersionDiff or, when a version
	// id is unknown, an error shape { error, available }. We model both.
	type VersionDiffError = { error: string; available: number[] };
	let compareV1: number | null = null;
	let compareV2: number | null = null;
	let versionDiff: BotVersionDiff | null = null;
	let versionDiffError: string | null = null;
	let diffLoading = false;
	let expandedVersion: number | null = null;

	function isDiffError(d: BotVersionDiff | VersionDiffError): d is VersionDiffError {
		return (d as VersionDiffError).error !== undefined;
	}

	// Pretty/compact stringification of an arbitrary snapshot value for the
	// diff table. null/undefined collapse to an em dash; objects/arrays are
	// compact-JSON; everything else is its string form.
	function formatValue(v: unknown): string {
		if (v === null || v === undefined) return '—';
		if (typeof v === 'object') {
			try {
				return JSON.stringify(v);
			} catch {
				return String(v);
			}
		}
		return String(v);
	}

	function prettySnapshot(snapshot: Record<string, unknown>): string {
		try {
			return JSON.stringify(snapshot, null, 2);
		} catch {
			return String(snapshot);
		}
	}

	function toggleVersionExpand(version: number) {
		expandedVersion = expandedVersion === version ? null : version;
	}

	async function runVersionDiff() {
		const id = botId;
		if (!id || compareV1 == null || compareV2 == null) return;
		diffLoading = true;
		versionDiff = null;
		versionDiffError = null;
		try {
			const result = (await diffBotVersions(id, compareV1, compareV2)) as
				| BotVersionDiff
				| VersionDiffError;
			if (isDiffError(result)) {
				const avail = result.available?.length ? ` Available: ${result.available.join(', ')}.` : '';
				versionDiffError = `${result.error}.${avail}`;
			} else {
				versionDiff = result;
			}
		} catch (e: any) {
			versionDiffError = e?.message || 'Failed to compare versions';
		} finally {
			diffLoading = false;
		}
	}

	// Seed the two selectors with the two most recent versions whenever the
	// version list changes (and the user hasn't already picked something).
	// Versions arrive newest-first from the API.
	$: if (versions.length >= 2 && compareV1 == null && compareV2 == null) {
		compareV1 = versions[1].version; // older of the two most recent
		compareV2 = versions[0].version; // newest
	}

	onMount(() => {
		load();
		// WS-driven refresh with polling only as an offline fallback; live marks
		// stream reactively from forvenLivePrices (no per-page price sockets).
		realtime = createRealtimeRefresh(load, {
			fallbackMs: 15_000,
			wsDebounceMs: 1000,
			wsEvents: ['trade', 'kill_switch_activated', 'kill_switch_cleared', 'risk_alert'],
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
	});
</script>

<svelte:head>
	<title>{bot?.name || 'Bot'} | Bot Factory | Forven</title>
</svelte:head>

<div class="mx-auto max-w-6xl px-4 py-6">
	{#if loading}
		<div class="py-20 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading…</div>
	{:else if error || !bot}
		<div class="py-20 text-center text-sm text-red-400">{error || 'Bot not found'}</div>
	{:else}
		<!-- Header -->
		<div class="mb-6 border-b border-sc-line pb-4">
			<button on:click={() => goto('/bot-factory')} class="mb-1 text-[11px] text-sc-ink3 hover:text-sc-ink">&larr; Bot Factory</button>
			<div class="flex items-start justify-between">
				<div>
					<div class="flex items-center gap-2.5">
						<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">{bot.name}</h1>
						{#if isLiveMode}
							<span class="border border-red-900 bg-red-500/10 px-2 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-400">Live</span>
							{#if bot.live_wallet}
								<span class="border border-sc-line2 px-2 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2" title="All live orders route to this isolated sub-account">⌂ {bot.live_wallet}</span>
							{/if}
						{:else}
							<span class="border border-sc-line2 px-2 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Paper</span>
							{#if bot.live_wallet}
								<span class="border border-sc-line px-2 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" title="Live orders will route to this sub-account once armed">⌂ {bot.live_wallet}</span>
							{/if}
						{/if}
					</div>
					<div class="mt-1.5 flex items-center gap-3 text-xs text-sc-ink3">
						{#if bot.bot_type && bot.bot_type !== 'ai'}
							<span>{ruleSummary(bot)}</span>
						{:else}
							<span class={bot.model_check && !bot.model_check.ok ? 'text-red-400' : ''} title={bot.model_check?.reason ?? ''}>
								{bot.model}{#if bot.model_check && !bot.model_check.ok} (unavailable){/if}
							</span>
							<span class="inline-block h-0.5 w-0.5 rounded-full bg-sc-line2"></span>
							<span>{bot.timeframe ?? '1h'} candles · {bot.decision_cadence === 'candle_close' ? 'decides on each new candle' : `decides every ${bot.cooldown_seconds}s`}</span>
						{/if}
						{#if bot.run_id && isRunning}
							<span class="inline-block h-0.5 w-0.5 rounded-full bg-sc-line2"></span>
							<span class="font-mono" title="This run's id; every Activity entry it writes carries it">run {bot.run_id}</span>
						{/if}
						<span class="inline-block h-0.5 w-0.5 rounded-full bg-sc-line2"></span>
						<span class="{statusColor(statusLabel)} font-bold uppercase tracking-wider">{statusLabel}</span>
						{#if bot.started_at && isRunning}
							<span class="inline-block h-0.5 w-0.5 rounded-full bg-sc-line2"></span>
							<span>started {relativeTime(bot.started_at)}</span>
						{/if}
					</div>
				</div>
				<div class="flex gap-2">
					{#if isRunning}
						{#if confirmStop}
							<button on:click={handleStop} class="terminal-button-danger text-[12px]">
								{isLiveMode && liveOpenCount > 0 ? `Confirm Stop + Close ${liveOpenCount}` : 'Confirm Stop'}
							</button>
							<button on:click={() => (confirmStop = false)} class="terminal-button text-[12px]">Cancel</button>
						{:else}
							<button on:click={() => (confirmStop = true)} class="terminal-button-danger text-[12px]">Stop</button>
						{/if}
					{:else}
						<button on:click={handleStart} class="terminal-button text-[12px] hover:border-emerald-500 hover:bg-emerald-500 hover:text-black">Start</button>
						{#if isLiveMode}
							{#if confirmGoPaper}
								<button on:click={handleGoPaper} disabled={modeBusy} class="terminal-button-primary text-[12px]">Confirm Paper</button>
								<button on:click={() => (confirmGoPaper = false)} class="terminal-button text-[12px]">Cancel</button>
							{:else}
								<button on:click={() => (confirmGoPaper = true)} disabled={modeBusy} class="terminal-button text-[12px]">Go Paper</button>
							{/if}
						{:else}
							<button on:click={openGoLiveDrawer} disabled={modeBusy} class="terminal-button-danger text-[12px]">Go Live…</button>
						{/if}
					{/if}
					<button on:click={() => goto(`/bot-factory/editor?id=${bot?.id ?? ''}`)} class="terminal-button text-[12px]">Edit</button>
				</div>
			</div>
		</div>

		{#if startError || (bot.model_check && !bot.model_check.ok && !isRunning)}
			<div class="mb-6 flex flex-wrap items-center justify-between gap-3 border border-red-900 bg-red-500/5 px-4 py-3 text-xs text-red-400">
				<span>{startError ?? `${bot.model_check?.reason} Start will refuse it until you pick another model.`}</span>
				{#if bot.model_check && !bot.model_check.ok && bot.model_check.default_model}
					<button on:click={handleUseDefaultModel} disabled={fixingModel} class="terminal-button text-[12px]">
						{fixingModel ? 'Switching…' : `Use ${bot.model_check.default_model}`}
					</button>
				{/if}
			</div>
		{/if}

		{#if (bot.bot_type ?? 'ai') === 'ai' && bot.decision_cadence !== 'candle_close'}
			<div class="mb-6 flex flex-wrap items-center justify-between gap-3 border border-sc-line bg-sc-panel px-4 py-3 text-xs text-sc-ink2">
				<span>
					This bot asks its model every {bot.cooldown_seconds}s, but its {bot.timeframe ?? '1h'} candles only change once per candle.
					Deciding once per candle uses far fewer AI calls; stops, targets and drawdown are still checked every minute.
					{#if isRunning}A bot started before this update needs a restart to pick it up.{/if}
				</span>
				<button on:click={handleApplyRecommended} disabled={applyingRecommended} class="terminal-button text-[12px]">
					{applyingRecommended ? 'Saving…' : 'Decide once per candle'}
				</button>
			</div>
		{/if}

		{#if confirmStop && isLiveMode && liveOpenCount > 0}
			<div class="mb-6 border border-red-900 bg-red-500/5 px-4 py-3 text-xs text-red-400">
				Stopping will close {liveOpenCount} open live position{liveOpenCount === 1 ? '' : 's'} on Hyperliquid
				({bot.live_wallet || 'master'}) with reduce-only market order{liveOpenCount === 1 ? '' : 's'}. This is real and irreversible — confirm to proceed.
			</div>
		{/if}

		{#if goLiveOpen && !isRunning && !isLiveMode}
			<div class="mb-6 border border-red-900 bg-red-500/5 p-4">
				<h3 class="text-[13px] font-semibold text-red-400">Arm live execution</h3>
				<p class="mt-2 text-xs leading-relaxed text-sc-ink2">
					This bot will place <span class="font-bold text-red-400">real Hyperliquid orders</span> sized off its
					allocated capital, admission-checked against the account's live risk budget (portfolio caps, correlation
					gate, liquidity guard, kill switch) on every open. Every live order carries a protective stop from the bot's
					stop-loss setting{bot.stop_loss_pct == null ? ' — set stop_loss_pct in the editor first' : ` (${bot.stop_loss_pct}%)`}.
				</p>
				<div class="mt-4 flex flex-wrap items-end gap-3">
					<div class="flex flex-col gap-1">
						<label for="go-live-wallet" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							Wallet{#if walletOptionsLoading}<span class="ml-1 normal-case text-sc-ink3">(loading…)</span>{/if}
						</label>
						<select id="go-live-wallet" bind:value={goLiveWallet} disabled={walletOptionsLoading} class="terminal-select w-52">
							<option value="">Master / shared (default)</option>
							{#each walletOptions as label}
								<option value={label}>{label} (isolated sub-account)</option>
							{/each}
						</select>
						{#if walletOptionsError}
							<span class="text-[10px] text-red-400">Wallet list failed: {walletOptionsError}</span>
						{/if}
					</div>
					<div class="flex flex-col gap-1">
						<label for="go-live-ceiling" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Per-order notional ceiling (USD)</label>
						<input
							id="go-live-ceiling"
							type="number"
							min="1"
							bind:value={goLiveCeiling}
							placeholder="e.g. 250"
							class="terminal-input w-44 focus:border-red-500"
						/>
					</div>
					<div class="flex flex-col gap-1">
						<label for="go-live-confirm" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Type <span class="font-bold text-red-400">GO LIVE</span> to confirm</label>
						<input
							id="go-live-confirm"
							type="text"
							bind:value={goLiveConfirm}
							placeholder="GO LIVE"
							class="terminal-input w-44 focus:border-red-500"
						/>
					</div>
					<button
						on:click={handleGoLive}
						disabled={modeBusy || goLiveConfirm.trim().toUpperCase() !== 'GO LIVE' || !goLiveCeiling || goLiveCeiling <= 0 || bot.stop_loss_pct == null}
						class="terminal-button-danger text-[12px]"
					>
						{modeBusy ? 'Arming…' : 'Arm Live'}
					</button>
					<button on:click={() => (goLiveOpen = false)} class="terminal-button text-[12px]">Cancel</button>
				</div>
				{#if bot.stop_loss_pct == null}
					<p class="mt-2 text-[11px] text-yellow-500">Live mode requires a stop-loss % — set it in the editor before arming.</p>
				{/if}
				{#if goLiveWallet}
					<p class="mt-2 text-[11px] text-sc-ink2">
						All live orders route to the <span class="font-bold text-sc-ink">{goLiveWallet}</span> sub-account —
						sized and budgeted against ITS balance only, fully isolated from the pipeline's wallet.
					</p>
				{:else if walletOptions.length === 0}
					<p class="mt-2 text-[11px] text-sc-ink3">
						No named wallets registered — create one in
						<a href="/settings#hyperliquid" class="text-sc-ink2 underline hover:text-sc-ink">Settings › HyperLiquid</a>
						to isolate this bot's capital from the pipeline.
					</p>
				{/if}
			</div>
		{/if}

		{#if bot.error_message && !isRunning}
			<div class="mb-4 border border-red-900 bg-red-500/5 p-3 text-xs text-red-400">
				{bot.error_message}
			</div>
		{/if}

		<!-- Stats bar. Paper shows Capital (the sim sandbox); live drops it — the
		     real Wallet balance is the only meaningful figure there. -->
		<div class="rounded-md mb-6 grid grid-cols-2 gap-px border border-sc-line bg-sc-raise {isLiveMode ? 'md:grid-cols-5' : 'md:grid-cols-6'}">
			{#if !isLiveMode}
				<div class="bg-sc-panel px-4 py-3">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Capital</div>
					<div class="mt-1 text-base font-bold text-sc-ink">${bot.capital_allocation?.toLocaleString()}</div>
				</div>
			{/if}
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{isLiveMode ? 'Wallet balance' : 'Equity'}</div>
				{#if isLiveMode}
					{#if liveWalletEquity == null}
						<div class="mt-1 text-base font-bold text-sc-ink3" title="Live wallet balance unavailable — daemon snapshot pending or wallet unfunded">—</div>
					{:else}
						<div class="mt-1 text-base font-bold text-sc-ink">${liveWalletEquity.toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>
					{/if}
				{:else}
					<div class="mt-1 text-base font-bold text-sc-ink">${currentEquity.toLocaleString(undefined, { maximumFractionDigits: 2 })}</div>
				{/if}
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Max Position</div>
				<div class="mt-1 text-base font-bold text-sc-ink">{bot.max_position_pct}%</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Drawdown</div>
				<div class="mt-1 text-base font-bold {drawdownColor}">{drawdownPct.toFixed(2)}% <span class="text-sc-ink3">/ {bot.max_drawdown_pct}%</span></div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">AI Today</div>
				<div class="mt-1 text-base font-bold text-sc-ink">{bot.llm_calls_today ?? 0} <span class="text-sc-ink3">/ {bot.max_llm_calls_per_day}</span></div>
				<div class="text-[11px] text-sc-ink3" title="Estimated from prompt and reply length">
					{fmtCost(bot.llm_cost_today ?? 0)}{bot.max_llm_cost_per_day ? ` / $${bot.max_llm_cost_per_day}` : ''} est.
				</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Errors</div>
				<div class="mt-1 text-base font-bold {(bot.consecutive_errors || 0) > 0 ? 'text-yellow-400' : 'text-sc-ink'}">{bot.consecutive_errors ?? 0} <span class="text-sc-ink3">/ {bot.max_consecutive_errors}</span></div>
			</div>
		</div>

		<!-- Headline trade summary — sourced from getBotStats (ALL trades, not the
		     last-N slice). -->
		{#if stats}
			<div class="rounded-md mb-6 flex flex-wrap items-center gap-x-6 gap-y-2 border border-sc-line bg-sc-panel px-4 py-3 text-sm">
				<div class="flex items-center gap-1.5">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Trades</span>
					<span class="font-bold text-sc-ink">{stats.total}</span>
					<span class="text-[11px] text-sc-ink3">({stats.open_count} open)</span>
				</div>
				<span class="h-4 w-px bg-sc-raise"></span>
				<div class="flex items-center gap-1.5">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Win rate</span>
					<span class="font-bold text-sc-ink">
						{(stats.wins + stats.losses) > 0 ? `${(stats.win_rate * 100).toFixed(0)}%` : '—'}
					</span>
					<span class="text-[11px] text-sc-ink3">({stats.wins}W / {stats.losses}L)</span>
				</div>
				<span class="h-4 w-px bg-sc-raise"></span>
				<div class="flex items-center gap-1.5">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Total P&L</span>
					<span class="font-bold {stats.total_pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}">
						{stats.total_pnl_usd >= 0 ? '+' : ''}${stats.total_pnl_usd.toFixed(2)}
					</span>
				</div>
				{#if stats.ai_priced_calls}
					<span class="h-4 w-px bg-sc-raise"></span>
					<div class="flex items-center gap-1.5" title="Estimated AI spend over {stats.ai_priced_calls} recorded calls">
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">AI cost</span>
						<span class="font-bold text-sc-ink">{fmtCost(stats.ai_cost_usd)}</span>
						<span class="text-[11px] text-sc-ink3">est.</span>
					</div>
				{/if}
			</div>
		{/if}

		<!-- View tabs -->
		<div class="mb-0 flex gap-0 border-b border-sc-line">
			{#each [['performance', 'Performance'], ['activity', 'Activity'], ['positions', 'Positions'], ['trades', 'Trades'], ['memory', 'Memory'], ['versions', 'Versions']] as [key, label]}
				<button
					on:click={() => (activeView = key as typeof activeView)}
					aria-pressed={activeView === key}
					class="border-b-2 px-4 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] transition-colors {activeView === key ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink2'}"
				>
					{label}
				</button>
			{/each}
		</div>

		<!-- View content -->
		<div class="rounded-md border border-t-0 border-sc-line bg-sc-panel">
			{#if activeView === 'performance'}
				{#if !botPerf}
					<div class="p-8 text-center text-sm text-sc-ink3">Performance is not available right now.</div>
				{:else}
					{@const ps = botPerf.summary}
					{@const benchCoin = ps.benchmark_symbol.split('/')[0]}
					<div class="grid grid-cols-2 gap-px border-b border-sc-line bg-sc-raise sm:grid-cols-4 lg:grid-cols-8">
						<div class="bg-sc-panel px-4 py-3" title="Since {formatTime(ps.start_at)}, including open positions and funding">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Return</div>
							<div class="mt-1 text-base font-bold {toneOf(ps.return_pct)}">{fmtSignedPct(ps.return_pct)}</div>
							<div class="text-[11px] text-sc-ink3">{ps.pnl_usd >= 0 ? '+' : '-'}${Math.abs(ps.pnl_usd).toFixed(2)}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Holding {ps.benchmark_symbol} with the same capital over the same time">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">vs {benchCoin} hold</div>
							<div class="mt-1 text-base font-bold {toneOf(ps.excess_return_pct)}">{fmtSignedPct(ps.excess_return_pct)}</div>
							<div class="text-[11px] text-sc-ink3">{benchCoin} {fmtSignedPct(ps.benchmark_return_pct)}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Annualised, from daily equity; needs a week of history">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Sharpe</div>
							<div class="mt-1 text-base font-bold text-sc-ink">{ps.sharpe != null ? ps.sharpe.toFixed(2) : '—'}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Max drawdown</div>
							<div class="mt-1 text-base font-bold text-sc-ink">{ps.max_drawdown_pct.toFixed(2)}%</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Gross wins divided by gross losses">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Profit factor</div>
							<div class="mt-1 text-base font-bold text-sc-ink">{ps.profit_factor != null ? ps.profit_factor.toFixed(2) : '—'}</div>
							<div class="text-[11px] text-sc-ink3">{ps.closed_trades} closed · {ps.win_rate != null ? `${(ps.win_rate * 100).toFixed(0)}% won` : 'none yet'}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Trading fees per closed trade">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Fees / trade</div>
							<div class="mt-1 text-base font-bold text-sc-ink">{ps.fees_per_trade_usd != null ? `$${ps.fees_per_trade_usd.toFixed(2)}` : '—'}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Estimated AI spend since the bot first ran">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">AI cost</div>
							<div class="mt-1 text-base font-bold text-sc-ink">{fmtCost(ps.ai_cost_usd)}</div>
							<div class="text-[11px] text-sc-ink3">{ps.ai_cost_pct_of_pnl != null ? `${ps.ai_cost_pct_of_pnl < 1 ? '<1' : ps.ai_cost_pct_of_pnl.toFixed(0)}% of profit` : 'est.'}</div>
						</div>
						<div class="bg-sc-panel px-4 py-3" title="Profit or loss after the estimated AI spend">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">P&L after AI</div>
							<div class="mt-1 text-base font-bold {toneOf(ps.pnl_after_ai_usd)}">{ps.pnl_after_ai_usd >= 0 ? '+' : '-'}${Math.abs(ps.pnl_after_ai_usd).toFixed(2)}</div>
						</div>
					</div>
					<div class="border-b border-sc-line p-4">
						<div class="mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Equity and drawdown, against holding {benchCoin}</div>
						{#if botPerf.curve.length >= 2}
							<EquityChart data={botPerf.curve} benchmarkData={botPerf.benchmark_curve} benchmarkTitle="{benchCoin} hold" height={260} />
						{:else}
							<div class="py-8 text-center text-sm text-sc-ink3">The curve starts once the bot has run.</div>
						{/if}
					</div>
					<div class="p-4">
						<div class="mb-2 flex flex-wrap items-center gap-2">
							<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Price and trades</span>
							{#if chart}
								<select
									value={chartPair}
									on:change={(e) => selectChartPair(e.currentTarget.value)}
									class="terminal-input w-auto text-[12px]"
									aria-label="Coin"
								>
									{#each chart.pairs as p}
										<option value={p}>{p}</option>
									{/each}
								</select>
								<span class="text-[11px] text-sc-ink3">{chart.timeframe} candles · {chart.entries.length} entries, {chart.exits.length} exits shown</span>
							{/if}
						</div>
						{#if chart && chart.candles.length > 0}
							<CandlestickChart data={chart.candles} signals={chartSignals} height={320} showVolume={false} />
						{:else}
							<div class="py-8 text-center text-sm text-sc-ink3">No stored {chart?.timeframe ?? ''} candles for {chartPair ?? 'this coin'} yet.</div>
						{/if}
					</div>
				{/if}
			{:else if activeView === 'activity'}
				{#if decisions.length === 0}
					<div class="p-8 text-center text-sm text-sc-ink3">
						No decisions yet. {isRunning ? 'Waiting for market events...' : 'Start the bot to begin trading.'}
					</div>
				{:else}
					<div class="divide-y divide-sc-line">
						{#each decisions as decision}
							{@const action = decision.action_type === 'trade' ? tradeAction(decision) : null}
							{@const direction = decision.action_type === 'trade' ? tradeDirection(decision) : null}
							<div class="p-4 {decision.action_type === 'error' ? 'border-l-2 border-red-500/60 bg-red-500/5' : ''}">
								<div class="flex items-center justify-between">
									<div class="flex flex-wrap items-center gap-2">
										<span class="text-base">{actionTypeIcon(decision.action_type)}</span>
										<span class="text-sm font-medium capitalize {actionTypeColor(decision.action_type)}">{decision.action_type}</span>
										{#if action}
											<span class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {actionBadgeColor(action)}">{action}</span>
										{/if}
										{#if direction}
											<span class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {direction === 'long' ? 'border border-emerald-900 bg-emerald-500/10 text-emerald-400' : 'border border-red-900 bg-red-500/10 text-red-400'}">{direction}</span>
										{/if}
									</div>
									<span class="flex items-center gap-2 text-xs text-sc-ink3">
										{#if decision.cost_usd != null}
											<span title="Estimated: {decision.tokens_in ?? 0} tokens in, {decision.tokens_out ?? 0} out">{fmtCost(decision.cost_usd)}</span>
										{/if}
										{#if decision.run_id}
											<span class="font-mono {bot.run_id && decision.run_id !== bot.run_id ? 'text-yellow-500' : ''}" title={bot.run_id && decision.run_id !== bot.run_id ? 'Written by an earlier run' : 'Run that made this decision'}>run {decision.run_id}</span>
										{/if}
										<span>{formatTime(decision.timestamp)}</span>
										<button
											on:click={() => openInspector(decision.id)}
											class="terminal-button px-1.5 py-0.5 text-[11px]"
											title={decision.has_details ? 'See what the bot was shown, what it answered and what the risk check did' : 'See the market data this decision used'}
										>Inspect</button>
									</span>
								</div>
								{#if decision.reasoning}
									<p class="mt-2 text-sm leading-relaxed {decision.action_type === 'error' ? 'text-red-300' : 'text-sc-ink2'}">{decision.reasoning}</p>
								{/if}
								{#if decision.action_data && (decision.action_type === 'trade' || decision.action_type === 'error')}
									<div class="mt-2 overflow-x-auto bg-sc-bg p-2 text-xs {decision.action_type === 'error' ? 'text-red-400' : 'text-sc-ink2'} font-mono whitespace-pre">{JSON.stringify(decision.action_data, null, 2)}</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			{:else if activeView === 'positions'}
				{#if !positions || positions.open_positions.length === 0}
					<div class="p-8 text-center text-sm text-sc-ink3">
						No open positions. {isRunning ? 'The bot will open positions when it finds a setup.' : 'Start the bot to begin trading.'}
					</div>
				{:else}
					{@const equity = positions.starting_capital + positions.realized_pnl + unrealizedPnl}
					<div class="flex flex-wrap items-center gap-4 border-b border-sc-line px-4 py-3 text-xs">
						<div class="flex items-center gap-1.5">
							<span class="text-sc-ink3">Realized P&L</span>
							<span class="font-bold {positions.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
								{positions.realized_pnl >= 0 ? '+' : ''}${positions.realized_pnl.toFixed(2)}
							</span>
						</div>
						<span class="h-4 w-px bg-sc-raise"></span>
						<div class="flex items-center gap-1.5">
							<span class="text-sc-ink3">Unrealized</span>
							<span class="font-bold {unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
								{unrealizedPnl >= 0 ? '+' : ''}${unrealizedPnl.toFixed(2)}
							</span>
						</div>
						<span class="h-4 w-px bg-sc-raise"></span>
						<div class="flex items-center gap-1.5">
							<span class="text-sc-ink3">Equity</span>
							<span class="font-bold text-sc-ink">${equity.toFixed(2)}</span>
						</div>
						{#if positions.peak_equity}
							<span class="h-4 w-px bg-sc-raise"></span>
							<div class="flex items-center gap-1.5">
								<span class="text-sc-ink3">Peak equity</span>
								<span class="font-bold text-sc-ink">${positions.peak_equity.toFixed(2)}</span>
							</div>
						{/if}
						<div class="ml-auto text-sc-ink3">{positions.open_positions.length} open</div>
					</div>
					<div class="overflow-x-auto">
						<table class="w-full text-sm">
							<thead>
								<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
									<th class="px-4 py-3">Trade</th>
									<th class="px-4 py-3">Pair</th>
									<th class="px-4 py-3">Mode</th>
									<th class="px-4 py-3">Direction</th>
									<th class="px-4 py-3">Qty</th>
									<th class="px-4 py-3">Entry</th>
									<th class="px-4 py-3">Current</th>
									<th class="px-4 py-3">Unrealized</th>
									<th class="px-4 py-3">Stop Loss</th>
									<th class="px-4 py-3">Take Profit</th>
									<th class="px-4 py-3">Opened</th>
									<th class="px-4 py-3"></th>
								</tr>
							</thead>
							<tbody>
								{#each positions.open_positions as pos}
									{@const liveCur = getLivePrice(pos.ticker, livePrices) ?? pos.current_price}
									{@const upnl = (liveCur != null && pos.entry_price)
										? (pos.direction === 'long' ? (liveCur - pos.entry_price) : (pos.entry_price - liveCur)) * pos.qty
										: null}
									{@const isClosing = closeBusy.has(pos.trade_id)}
									<tr class="border-b border-sc-line hover:bg-sc-panel2 transition-colors">
										<td class="px-4 py-3 font-mono text-xs text-sc-ink2">{pos.trade_id}</td>
										<td class="px-4 py-3 text-sc-ink">{pos.ticker}</td>
										<td class="px-4 py-3">
											{#if pos.execution_type === 'live'}
												<span class="border border-red-900 bg-red-500/10 px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-400">Live</span>
											{:else}
												<span class="border border-sc-line2 px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Paper</span>
											{/if}
										</td>
										<td class="px-4 py-3">
											<span class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {pos.direction === 'long' ? 'border border-emerald-900 bg-emerald-500/10 text-emerald-400' : 'border border-red-900 bg-red-500/10 text-red-400'}">{pos.direction}</span>
										</td>
										<td class="px-4 py-3 text-sc-ink2">{pos.qty}</td>
										<td class="px-4 py-3 text-sc-ink2">${pos.entry_price?.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) ?? '—'}</td>
										<td class="px-4 py-3 text-sc-ink font-medium">
											{#if liveCur != null}
												${liveCur.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
											{:else}
												<span class="text-sc-ink3" title="No live price feed">—</span>
											{/if}
										</td>
										<td class="px-4 py-3 font-medium {upnl == null ? '' : upnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
											{#if upnl != null}
												{upnl >= 0 ? '+' : ''}${upnl.toFixed(2)}
											{:else}
												<span class="text-sc-ink3" title="No live price to mark against">—</span>
											{/if}
										</td>
										<td class="px-4 py-3 {pos.stop_loss_price ? 'text-red-400' : 'text-sc-ink3'}">
											{pos.stop_loss_price ? `$${pos.stop_loss_price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : 'none'}
										</td>
										<td class="px-4 py-3 {pos.take_profit_price ? 'text-emerald-400' : 'text-sc-ink3'}">
											{pos.take_profit_price ? `$${pos.take_profit_price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : 'none'}
										</td>
										<td class="px-4 py-3 text-xs text-sc-ink3">{formatTime(pos.opened_at)}</td>
										<td class="px-4 py-3 text-right">
											{#if confirmClosePos === pos.trade_id}
												<button on:click={() => handleClosePosition(pos.trade_id)} disabled={isClosing} class="terminal-button-danger px-2 py-1 text-[12px]">{isClosing ? 'Closing…' : 'Confirm'}</button>
												<button on:click={() => (confirmClosePos = null)} disabled={isClosing} class="terminal-button px-2 py-1 text-[12px]">Cancel</button>
											{:else}
												<button on:click={() => (confirmClosePos = pos.trade_id)} disabled={isClosing} class="terminal-button px-2 py-1 text-[12px] hover:text-red-400">Close</button>
											{/if}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			{:else if activeView === 'trades'}
				{#if trades.length === 0}
					<div class="p-8 text-center text-sm text-sc-ink3">No trades yet.</div>
				{:else}
					<!-- Overall P&L Summary — aggregate figures come from getBotStats
					     (ALL trades), not the last-N slice below. Unrealized is the only
					     value derived from the live slice since it needs live marks. -->
					{@const openTrades = trades.filter(t => t.status === 'OPEN')}
					{@const openPnl = openTrades.reduce((sum, t) => {
						const live = computePnl(t, livePrices);
						return sum + (live?.pnl ?? 0);
					}, 0)}
					{@const totalPnl = stats ? stats.total_pnl_usd : trades.filter(t => t.status !== 'OPEN').reduce((s, t) => s + (t.pnl ?? 0), 0)}
					{@const wins = stats?.wins ?? 0}
					{@const losses = stats?.losses ?? 0}
					{@const winRate = stats ? stats.win_rate * 100 : 0}
					{@const totalTrades = stats?.total ?? trades.length}
					<div class="flex flex-wrap items-center gap-4 border-b border-sc-line px-4 py-3">
						<div class="flex items-center gap-1.5">
							<span class="text-xs text-sc-ink3">Total P&L</span>
							<span class="text-sm font-bold {totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
								{totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
							</span>
						</div>
						{#if openTrades.length > 0}
							<span class="h-4 w-px bg-sc-raise"></span>
							<div class="flex items-center gap-1.5">
								<span class="text-xs text-sc-ink3">Unrealized</span>
								<span class="text-sm font-bold {openPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
									{openPnl >= 0 ? '+' : ''}${openPnl.toFixed(2)}
								</span>
							</div>
						{/if}
						{#if stats}
							<span class="h-4 w-px bg-sc-raise"></span>
							<div class="flex items-center gap-1.5">
								<span class="text-xs text-sc-ink3">Best</span>
								<span class="text-sm font-bold text-emerald-400">{stats.best_pnl_usd >= 0 ? '+' : ''}${stats.best_pnl_usd.toFixed(2)}</span>
							</div>
							<span class="h-4 w-px bg-sc-raise"></span>
							<div class="flex items-center gap-1.5">
								<span class="text-xs text-sc-ink3">Worst</span>
								<span class="text-sm font-bold text-red-400">${stats.worst_pnl_usd.toFixed(2)}</span>
							</div>
						{/if}
						<span class="h-4 w-px bg-sc-raise"></span>
						<div class="flex items-center gap-1.5 text-xs text-sc-ink3">
							<span class="text-emerald-400">{wins}W</span>
							<span>/</span>
							<span class="text-red-400">{losses}L</span>
							{#if stats && (stats.wins + stats.losses) > 0}
								<span class="text-sc-ink2">({winRate.toFixed(0)}%)</span>
							{/if}
						</div>
						<div class="ml-auto text-xs text-sc-ink3">{totalTrades} trade{totalTrades !== 1 ? 's' : ''}{stats ? ` · ${stats.open_count} open` : ''}</div>
					</div>
					<div class="px-4 pt-3 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Recent (last {trades.length})</div>
					<div class="overflow-x-auto">
						<table class="w-full text-sm">
							<thead>
								<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
									<th class="px-4 py-3">ID</th>
									<th class="px-4 py-3">Pair</th>
									<th class="px-4 py-3">Direction</th>
									<th class="px-4 py-3">Size</th>
									<th class="px-4 py-3">Entry</th>
									<th class="px-4 py-3">Current</th>
									<th class="px-4 py-3">Exit</th>
									<th class="px-4 py-3">P&L</th>
									<th class="px-4 py-3">Status</th>
									<th class="px-4 py-3">Opened</th>
								</tr>
							</thead>
							<tbody>
								{#each trades as trade}
									{@const live = computePnl(trade, livePrices)}
									{@const currentPrice = getLivePrice(trade.symbol || trade.asset, livePrices)}
									<tr class="border-b border-sc-line hover:bg-sc-panel2 transition-colors">
										<td class="px-4 py-3 font-mono text-xs text-sc-ink2">{trade.id}</td>
										<td class="px-4 py-3 text-sc-ink">{trade.symbol || trade.asset}</td>
										<td class="px-4 py-3">
											<span class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {trade.direction === 'long' ? 'border border-emerald-900 bg-emerald-500/10 text-emerald-400' : 'border border-red-900 bg-red-500/10 text-red-400'}">
												{trade.direction}
											</span>
										</td>
										<td class="px-4 py-3 text-sc-ink2">{trade.size}</td>
										<td class="px-4 py-3 text-sc-ink2">${trade.entry_price?.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) ?? '—'}</td>
										<td class="px-4 py-3 text-sc-ink font-medium">
											{#if trade.status === 'OPEN' && currentPrice}
												${currentPrice.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
											{:else}
												<span class="text-sc-ink3">—</span>
											{/if}
										</td>
										<td class="px-4 py-3 text-sc-ink2">{trade.exit_price ? `$${trade.exit_price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : '—'}</td>
										<td class="px-4 py-3 font-medium {live && live.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
											{#if live}
												${live.pnl.toFixed(2)} ({live.pnlPct > 0 ? '+' : ''}{live.pnlPct.toFixed(2)}%)
											{:else}
												<span class="text-sc-ink3">—</span>
											{/if}
										</td>
										<td class="px-4 py-3">
											<span class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {trade.status === 'OPEN' ? 'border border-sc-line2 text-sc-ink2' : 'border border-sc-line text-sc-ink3'}">
												{trade.status}
											</span>
										</td>
										<td class="px-4 py-3 text-xs text-sc-ink3">{formatTime(trade.opened_at)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			{:else if activeView === 'memory'}
				{#if memoryEntries.length === 0}
					<div class="p-8 text-center text-sm text-sc-ink3">
						No memories yet. {isRunning ? "The bot will store observations and trade outcomes as it runs." : 'Start the bot to begin building memory.'}
					</div>
				{:else}
					<div class="divide-y divide-sc-line">
						{#each memoryEntries as entry}
							{@const meta = entry.metadata || {}}
							{@const entryType = (meta.type as string) || 'memory'}
							<div class="p-4">
								<div class="flex items-center justify-between">
									<div class="flex items-center gap-2">
										<span
											class="px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]
												{entryType === 'trade_outcome' && meta.outcome === 'win' ? 'border border-emerald-900 bg-emerald-500/10 text-emerald-400' : ''}
												{entryType === 'trade_outcome' && meta.outcome === 'loss' ? 'border border-red-900 bg-red-500/10 text-red-400' : ''}
												{entryType === 'trade_outcome' && meta.outcome === 'flat' ? 'border border-sc-line2 text-sc-ink2' : ''}
												{entryType === 'trade_entry' ? 'border border-sc-line2 text-sc-ink2' : ''}
												{entryType === 'observation' ? 'border border-yellow-900 bg-yellow-500/10 text-yellow-400' : ''}
												{entryType === 'trade_exit' ? 'border border-sc-line2 text-sc-ink2' : ''}
												{entryType === 'memory' ? 'border border-sc-line2 text-sc-ink2' : ''}"
										>{entryType.replace('_', ' ')}</span>
										{#if meta.ticker}
											<span class="text-xs text-sc-ink2">{meta.ticker}</span>
										{/if}
										{#if entryType === 'trade_outcome' && typeof meta.pnl === 'number'}
											<span class="text-xs font-medium {meta.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}">
												{meta.pnl >= 0 ? '+' : ''}${meta.pnl.toFixed(2)}
												{#if typeof meta.pnl_pct === 'number'}
													({meta.pnl_pct >= 0 ? '+' : ''}{meta.pnl_pct.toFixed(2)}%)
												{/if}
											</span>
										{/if}
									</div>
									<span class="text-xs text-sc-ink3">{formatTime((meta.timestamp as string) || null)}</span>
								</div>
								<p class="mt-2 text-sm text-sc-ink2 leading-relaxed">{entry.text}</p>
							</div>
						{/each}
					</div>
				{/if}
			{:else if activeView === 'versions'}
				{#if versions.length === 0}
					<div class="p-8 text-center text-sm text-sc-ink3">No config changes recorded yet.</div>
				{:else}
					<!-- Compare controls -->
					<div class="border-b border-sc-line p-4">
						{#if versions.length < 2}
							<p class="text-xs text-sc-ink3">Record at least two config versions to compare changes.</p>
						{:else}
							<div class="flex flex-wrap items-end gap-3">
								<div class="flex flex-col gap-1">
									<label for="diff-v1" class="text-xs text-sc-ink3">From version</label>
									<select
										id="diff-v1"
										bind:value={compareV1}
										class="rounded-md border border-sc-line2 bg-sc-bg px-3 py-1.5 text-sm text-sc-ink focus:border-sc-ink focus:outline-none"
									>
										{#each versions as v}
											<option value={v.version}>v{v.version} — {formatTime(v.created_at)}</option>
										{/each}
									</select>
								</div>
								<span class="pb-2 text-sc-ink3">→</span>
								<div class="flex flex-col gap-1">
									<label for="diff-v2" class="text-xs text-sc-ink3">To version</label>
									<select
										id="diff-v2"
										bind:value={compareV2}
										class="rounded-md border border-sc-line2 bg-sc-bg px-3 py-1.5 text-sm text-sc-ink focus:border-sc-ink focus:outline-none"
									>
										{#each versions as v}
											<option value={v.version}>v{v.version} — {formatTime(v.created_at)}</option>
										{/each}
									</select>
								</div>
								<button
									on:click={runVersionDiff}
									disabled={diffLoading || compareV1 == null || compareV2 == null || compareV1 === compareV2}
									class="terminal-button-primary px-4 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40"
								>
									{diffLoading ? 'Comparing…' : 'Compare'}
								</button>
							</div>
							{#if compareV1 != null && compareV2 != null && compareV1 === compareV2}
								<p class="mt-2 text-xs text-yellow-400">Pick two different versions to compare.</p>
							{/if}

							<!-- Diff result -->
							{#if versionDiffError}
								<div class="mt-4 border border-yellow-900 bg-yellow-500/5 p-3 text-sm text-yellow-300">
									{versionDiffError}
								</div>
							{:else if versionDiff}
								{@const changeEntries = Object.entries(versionDiff.changes)}
								<div class="mt-4">
									<div class="mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
										Changes from v{versionDiff.v1} to v{versionDiff.v2}
									</div>
									{#if changeEntries.length === 0}
										<p class="rounded-md border border-sc-line bg-sc-panel p-3 text-sm text-sc-ink3">
											No differences between these versions.
										</p>
									{:else}
										<div class="overflow-x-auto border border-sc-line">
											<table class="w-full text-sm">
												<thead>
													<tr class="border-b border-sc-line text-left text-xs text-sc-ink3">
														<th class="px-4 py-2">Field</th>
														<th class="px-4 py-2">v{versionDiff.v1}</th>
														<th class="px-4 py-2">v{versionDiff.v2}</th>
													</tr>
												</thead>
												<tbody>
													{#each changeEntries as [field, change]}
														<tr class="border-b border-sc-line last:border-b-0">
															<td class="px-4 py-2 font-mono text-xs text-sc-ink2">{field}</td>
															<td class="px-4 py-2 font-mono text-xs text-red-400 break-all">{formatValue(change.v1)}</td>
															<td class="px-4 py-2 font-mono text-xs text-emerald-300 break-all">{formatValue(change.v2)}</td>
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
									{/if}
								</div>
							{/if}
						{/if}
					</div>

					<!-- Version list (expandable to view full snapshot) -->
					<div class="divide-y divide-sc-line">
						{#each versions as version}
							<div class="p-4">
								<div class="flex items-center justify-between">
									<div class="flex items-center gap-3">
										<span class="text-sm font-medium text-sc-ink">Version {version.version}</span>
										<button
											on:click={() => toggleVersionExpand(version.version)}
											class="text-[12px] text-sc-ink2 hover:text-sc-ink"
											aria-expanded={expandedVersion === version.version}
										>
											{expandedVersion === version.version ? 'Hide config' : 'View config'}
										</button>
									</div>
									<span class="text-xs text-sc-ink3">{formatTime(version.created_at)}</span>
								</div>
								{#if expandedVersion === version.version}
									<pre class="mt-3 overflow-x-auto bg-sc-panel p-3 text-xs text-sc-ink2 whitespace-pre">{prettySnapshot(version.config_snapshot)}</pre>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

{#if inspecting || inspectLoading || inspectError}
	<div class="fixed inset-0 z-40 bg-black/50" on:click={closeInspector} role="presentation"></div>
	<aside
		class="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-sc-line bg-sc-panel shadow-xl"
		aria-label="Decision inspector"
	>
		<div class="flex items-center justify-between border-b border-sc-line px-4 py-3">
			<div>
				<div class="text-[14px] font-semibold text-sc-ink">Why did it do that?</div>
				{#if inspecting}
					<div class="text-[11px] text-sc-ink3">
						{formatTime(inspecting.timestamp)} · {inspecting.action_type}{#if inspecting.run_id} · run {inspecting.run_id}{/if}
					</div>
				{/if}
			</div>
			<button on:click={closeInspector} class="terminal-button text-[12px]" aria-label="Close inspector">Close</button>
		</div>
		<div class="flex-1 space-y-5 overflow-y-auto p-4 text-xs">
			{#if inspectLoading}
				<div class="py-10 text-center text-sc-ink3">Loading…</div>
			{:else if inspectError}
				<div class="text-red-400">{inspectError}</div>
			{:else if inspecting}
				{@const det = inspecting.details}
				<section>
					<h3 class="mb-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Outcome</h3>
					<div class="text-sc-ink2">
						<span class="capitalize {actionTypeColor(inspecting.action_type)}">{inspecting.action_type}</span>{#if inspecting.reasoning}<span>: {inspecting.reasoning}</span>{/if}
					</div>
					{#if det?.risk_check}
						<div class="mt-2 border px-2 py-1.5 {det.risk_check.allowed ? 'border-emerald-900 bg-emerald-500/5 text-emerald-300' : 'border-red-900 bg-red-500/5 text-red-300'}">
							Risk check: {det.risk_check.reason}
						</div>
					{/if}
					{#if inspecting.action_data}
						<pre class="mt-2 overflow-x-auto bg-sc-bg p-2 font-mono text-sc-ink2 whitespace-pre">{JSON.stringify(inspecting.action_data, null, 2)}</pre>
					{/if}
				</section>
				<section>
					<h3 class="mb-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Cost</h3>
					<div class="text-sc-ink2">
						{det?.model ?? bot?.model ?? '—'} ·
						{inspecting.cost_usd != null ? `${fmtCost(inspecting.cost_usd)} est.` : 'no AI cost recorded'}
						{#if inspecting.tokens_in != null} · {inspecting.tokens_in} tokens in, {inspecting.tokens_out ?? 0} out{/if}
					</div>
				</section>
				{#if det}
					<section>
						<h3 class="mb-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">What it answered</h3>
						{#if det.response}
							<pre class="overflow-x-auto bg-sc-bg p-2 font-mono text-sc-ink whitespace-pre-wrap break-words">{det.response}</pre>
						{:else}
							<div class="text-red-300">No answer: the call failed ({inspecting.reasoning ?? 'unknown error'}).</div>
						{/if}
					</section>
					<section>
						<h3 class="mb-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">What it was shown</h3>
						{#each det.prompt ?? [] as message, i}
							<details class="mb-2 border border-sc-line" open={i > 0}>
								<summary class="cursor-pointer px-2 py-1.5 font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink2">
									{message.role === 'system' ? 'Instructions (soul, rules, answer format)' : message.role === 'user' ? 'Situation (strategy, portfolio, market data)' : message.role}
									<span class="text-sc-ink3">· {message.content.length.toLocaleString()} characters</span>
								</summary>
								<pre class="max-h-[50vh] overflow-auto border-t border-sc-line bg-sc-bg p-2 font-mono text-sc-ink2 whitespace-pre-wrap break-words">{message.content}</pre>
							</details>
						{/each}
					</section>
				{:else}
					<section>
						<h3 class="mb-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">What it was shown</h3>
						<p class="mb-2 text-sc-ink3">The full prompt wasn't kept for this entry (older entries, and entries that aren't AI decisions, only keep the market data).</p>
						{#if inspecting.event_trigger}
							<pre class="max-h-[50vh] overflow-auto bg-sc-bg p-2 font-mono text-sc-ink2 whitespace-pre">{JSON.stringify(inspecting.event_trigger, null, 2)}</pre>
						{/if}
					</section>
				{/if}
			{/if}
		</div>
	</aside>
{/if}
