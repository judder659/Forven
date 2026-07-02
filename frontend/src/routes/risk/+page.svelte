<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getForvenDashboard,
		getForvenRisk,
		resetTradingHalt,
		type ForvenDashboardResponse,
		type ForvenRiskStatus,
	} from '$lib/api';
	import { rebaselineEquityAnchors, setLiveNotionalCeiling, triggerEmergencyHalt } from '$lib/api/forven';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';

	let dashboard: ForvenDashboardResponse | null = null;
	let risk: ForvenRiskStatus | null = null;
	let loading = true;
	let error = '';
	let actionMessage = '';
	let resetBusy = false;
	let haltBusy = false;
	let realtime: RealtimeRefreshController | null = null;

	$: limits = risk?.limits ?? {};
	$: portfolio = risk?.portfolio ?? {};
	$: groups = portfolio?.groups ?? {};
	$: accountValue = Number(dashboard?.account?.accountValue ?? dashboard?.daily_risk?.current_equity ?? 0);
	$: highWaterMark = Number(risk?.high_water_mark ?? dashboard?.risk?.high_water_mark ?? 0);
	$: dailyStartEquity = Number(risk?.daily_start_equity ?? dashboard?.daily_risk?.start_equity ?? 0);
	$: currentDrawdown = highWaterMark > 0 ? Math.max(0, (highWaterMark - accountValue) / highWaterMark) : 0;
	$: dailyLoss = dailyStartEquity > 0 ? Math.max(0, (dailyStartEquity - accountValue) / dailyStartEquity) : 0;
	$: portfolioRisk = Number(portfolio?.total_net_risk ?? 0);
	// Largest single-trade risk currently committed across open positions. Backed by
	// the additive `current_per_trade_risk` field on get_risk_status (display-only).
	$: perTradeRisk = Number(risk?.current_per_trade_risk ?? 0);
	$: tradingAllowed = dashboard?.trading_allowed ?? true;
	$: tradingReason = dashboard?.trading_reason || 'OK';
	$: killSwitchActive = Boolean(risk?.kill_switch_active || dashboard?.risk?.kill_switch_active);
	$: dailyLossHalt = Boolean(risk?.daily_loss_halt || dashboard?.risk?.daily_loss_halt);
	$: systemPaused = Boolean(dashboard?.paused);
	$: haltBannerTitle = killSwitchActive
		? 'Kill Switch Active'
		: dailyLossHalt
			? 'Daily Loss Halt Active'
			: systemPaused
				? 'System Paused'
				: 'Trading Halted';
	$: dailyPnlUsd = dailyStartEquity > 0 ? accountValue - dailyStartEquity : 0;

	$: recovery = dashboard?.recovery ?? null;
	$: recoveryActive = Boolean(recovery?.active || risk?.recovery_active);
	$: recoverySummary = recovery?.summary || risk?.recovery_summary || '';
	$: recoveryRequiresOperator = Boolean(recovery?.requires_operator);

	// PORT-1: the LIVE account-level portfolio budget (dollar risk-to-stop and net
	// exposure vs real equity) — the admission gate every new live position passes.
	$: liveBudget = risk?.portfolio_budget_live ?? null;
	$: liveBudgetRiskUsed = Number(liveBudget?.total_open_risk_usd ?? 0);
	$: liveBudgetRiskLimit = Number(liveBudget?.total_open_risk_limit_usd ?? 0);
	$: liveBudgetAssets = Object.entries(liveBudget?.per_asset ?? {});
	$: liveBudgetGroups = Object.entries(liveBudget?.per_group ?? {});
	// BOOK-BUDGET-1: per-wallet (direction book) capacity vs usage.
	$: liveBudgetBooks = Object.entries(liveBudget?.per_book ?? {});
	// GO-LIVE-1: per-strategy notional ceilings accepted at go-live.
	$: liveCeilings = Object.entries(liveBudget?.strategy_ceilings ?? {});
	$: ceilingsMissing = liveBudget?.ceilings_missing ?? [];
	// LIQ-1: order-time liquidity guard state + recent admit/block decisions.
	$: liquidityGuard = risk?.liquidity_guard_live ?? null;
	$: liquidityDecisions = liquidityGuard?.recent_decisions ?? [];

	function formatBudgetUsd(value: number): string {
		return `$${Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
	}

	$: circuitBreakers = dashboard?.circuit_breakers
		? [
				{ label: 'Price Feed', state: dashboard.circuit_breakers.hl_price },
				{ label: 'Trade', state: dashboard.circuit_breakers.hl_trade },
				{ label: 'Account', state: dashboard.circuit_breakers.hl_account },
			].filter((cb) => cb.state)
		: [];

	// Distinguish "no telemetry yet" from a genuine all-zero/safe reading. Both
	// upstream calls populate `dashboard`/`risk`; if neither resolved we have no data.
	$: hasRiskData = dashboard !== null || risk !== null;

	$: gauges = [
		{ label: 'Drawdown', value: currentDrawdown, max: Number(limits.max_drawdown ?? 0.1) },
		{ label: 'Daily Loss', value: dailyLoss, max: Number(limits.daily_loss_limit ?? 0.05) },
		{ label: 'Portfolio Risk', value: portfolioRisk, max: Number(limits.portfolio_budget ?? 0.02) },
	];

	$: limitBars = [
		{ label: 'Max Drawdown', current: currentDrawdown, max: Number(limits.max_drawdown ?? 0.1) },
		{ label: 'Daily Loss Limit', current: dailyLoss, max: Number(limits.daily_loss_limit ?? 0.05) },
		{ label: 'Portfolio Budget', current: portfolioRisk, max: Number(limits.portfolio_budget ?? 0.02) },
		{ label: 'Per-Trade Max', current: perTradeRisk, max: Number(limits.max_risk_per_trade ?? 0.02) },
	];

	function breakerColor(state?: string): string {
		const s = (state ?? '').toLowerCase();
		if (s === 'open' || s === 'tripped' || s === 'error') return 'text-red-400 border-red-800';
		if (s === 'half_open' || s === 'half-open' || s === 'degraded' || s === 'warning')
			return 'text-yellow-400 border-yellow-800';
		return 'text-emerald-400 border-emerald-800';
	}

	function clampPercent(value: number): number {
		return Math.max(0, Math.min(100, value));
	}

	function gaugeRatio(value: number, max: number): number {
		if (!max || max <= 0) return 0;
		return clampPercent((value / max) * 100);
	}

	function gaugeColor(value: number, max: number): string {
		if (!max || max <= 0) return '#666';
		const ratio = value / max;
		if (ratio >= 1) return '#ef4444';
		if (ratio >= 0.75) return '#eab308';
		return '#10b981';
	}

	function formatPct(value: number): string {
		return `${(value * 100).toFixed(2)}%`;
	}

	function formatUsd(value: number): string {
		return `${value >= 0 ? '+' : '-'}$${Math.abs(value).toFixed(2)}`;
	}

	function getExposureWidth(value: number, budget: number): number {
		const base = budget > 0 ? budget : 0.02;
		const ratio = Math.abs(value) / base;
		return Math.max(2, Math.min(50, ratio * 50));
	}

	async function loadRiskData() {
		error = '';
		const [dashboardResult, riskResult] = await Promise.allSettled([
			getForvenDashboard(),
			getForvenRisk(),
		]);

		if (dashboardResult.status === 'fulfilled') {
			dashboard = dashboardResult.value;
		}

		if (riskResult.status === 'fulfilled') {
			risk = riskResult.value;
		}

		if (dashboardResult.status === 'rejected' && riskResult.status === 'rejected') {
			error = 'Risk telemetry unavailable.';
		}

		loading = false;
	}

	// EQ-BASIS-3: re-anchor HWM / daily start to a fresh books-aware live read —
	// the explicit confirmation for a poisoned anchor or a genuine large deposit
	// that the fail-closed equity jump guard refuses to accept on its own.
	let rebaselineBusy = false;
	async function handleRebaseline() {
		if (rebaselineBusy) return;
		const confirmed = window.confirm(
			'Re-baseline the equity anchors?\n\nHigh-water mark, daily start, and last equity will be re-anchored to a fresh live wallet reading (sum of the trading wallets). Drawdown and daily PnL restart from there. Use this after a bad reading poisoned the anchors, or to confirm a large deposit.'
		);
		if (!confirmed) return;
		rebaselineBusy = true;
		error = '';
		try {
			const result = await rebaselineEquityAnchors();
			actionMessage = `Equity anchors re-baselined to $${Number(result.equity).toLocaleString(undefined, { maximumFractionDigits: 2 })} (was HWM $${Number(result.previous_high_water_mark).toLocaleString(undefined, { maximumFractionDigits: 2 })}).`;
			await loadRiskData();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Equity re-baseline failed';
		} finally {
			rebaselineBusy = false;
		}
	}

	// GO-LIVE-1: adjust (or add) a live strategy's per-asset notional ceiling
	// after go-live. 0 clears it — only the account-wide budget caps remain.
	async function editCeiling(strategyId: string, current?: number) {
		const raw = window.prompt(
			`Per-asset live notional ceiling (USD) for ${strategyId} — the largest live position it may hold, enforced on every order.\n\nEnter 0 to clear the ceiling.`,
			String(current && current > 0 ? current : 1000)
		);
		if (raw === null) return;
		const value = Number(raw);
		if (!Number.isFinite(value) || value < 0) {
			error = 'Ceiling must be a number ≥ 0 (0 clears it).';
			return;
		}
		error = '';
		try {
			await setLiveNotionalCeiling(strategyId, value === 0 ? null : value);
			actionMessage =
				value === 0
					? `Ceiling cleared for ${strategyId}.`
					: `Ceiling for ${strategyId} set to $${value.toLocaleString()}.`;
			await loadRiskData();
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to update ceiling for ${strategyId}`;
		}
	}

	async function handleTradingReset() {
		if (resetBusy) return;
		const confirmed = typeof window === 'undefined'
			? true
			: window.confirm('Reset the current trading halt and resume the runtime?');
		if (!confirmed) return;
		resetBusy = true;
		error = '';
		actionMessage = '';
		try {
			const result = await resetTradingHalt();
			await loadRiskData();
			actionMessage = result.trading_allowed
				? 'Trading reset complete. New entries are allowed again.'
				: `Trading reset completed, but entries are still blocked: ${result.trading_reason}`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Trading halt reset failed';
		} finally {
			resetBusy = false;
		}
	}

	async function handleEmergencyHalt() {
		if (haltBusy) return;
		const confirmed = typeof window === 'undefined'
			? false
			: window.confirm(
					'EMERGENCY HALT will immediately close ALL open positions at market and stop trading. This cannot be undone. Continue?',
				);
		if (!confirmed) return;
		haltBusy = true;
		error = '';
		actionMessage = '';
		try {
			const result = await triggerEmergencyHalt();
			if (result.ok === false) {
				throw new Error(result.error || 'Emergency halt failed');
			}
			await loadRiskData();
			const closedCount = Array.isArray(result.closed) ? result.closed.length : 0;
			actionMessage = `Emergency halt triggered. Closed ${closedCount} position${closedCount === 1 ? '' : 's'}.`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Emergency halt failed';
		} finally {
			haltBusy = false;
		}
	}

	onMount(() => {
		void loadRiskData();
		realtime = createRealtimeRefresh(loadRiskData, {
			fallbackMs: 20_000,
			wsDebounceMs: 1200,
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
		realtime = null;
	});
</script>

<svelte:head>
	<title>Risk Command | Forven</title>
	<meta name="description" content="Monitor drawdown, kill-switch state, and live portfolio risk guardrails." />
</svelte:head>

<div class="h-full overflow-y-auto p-4 space-y-4">
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-3">
			<svg class="w-6 h-6 text-red-400" viewBox="0 0 24 24" fill="currentColor">
				<path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 11H5V6.3l7-3.11v8.8h7c-.53 4.12-3.28 7.79-7 8.94V12z" />
			</svg>
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">Risk Command</h1>
		</div>
		<div class="flex items-center gap-2">
			<button
				type="button"
				class="text-xs border border-red-800 bg-red-950/30 px-3 py-1.5 font-bold uppercase tracking-wider text-red-300 hover:bg-red-900/50 hover:text-red-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
				on:click={handleEmergencyHalt}
				disabled={haltBusy}
				title="Immediately close all open positions and halt trading"
			>
				{haltBusy ? 'Halting...' : 'Emergency Halt'}
			</button>
			<a href="/settings" class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-[#555] transition-colors">
				Open Settings
			</a>
		</div>
	</div>

	{#if !tradingAllowed}
		<div class="border p-4 flex items-start justify-between gap-4 {killSwitchActive ? 'border-red-800 bg-red-950/20' : dailyLossHalt ? 'border-yellow-800 bg-yellow-950/20' : 'border-yellow-700/60 bg-yellow-950/10'}">
			<div>
				<div class="text-sm font-bold tracking-wider uppercase {killSwitchActive ? 'text-red-300' : 'text-yellow-300'}">
					{haltBannerTitle}
				</div>
				<div class="text-xs mt-1 {killSwitchActive ? 'text-red-200/90' : 'text-yellow-100/90'}">
					{tradingReason}
					{#if killSwitchActive && risk?.kill_switch_triggered_at}
						Triggered at {new Date(risk.kill_switch_triggered_at).toLocaleString()}.
					{/if}
				</div>
			</div>
			<button
				class="px-3 py-1.5 text-xs border border-[#333] text-[#888] hover:text-white hover:border-[#555] transition-colors disabled:opacity-60"
				on:click={handleTradingReset}
				disabled={resetBusy}
			>
				{resetBusy ? 'Resetting...' : 'Reset Trading Halt'}
			</button>
		</div>
	{/if}

	{#if recoveryActive}
		<div class="border p-4 {recoveryRequiresOperator ? 'border-red-800 bg-red-950/20' : 'border-yellow-700/60 bg-yellow-950/10'}">
			<div class="flex items-center gap-2">
				<span class="text-sm font-bold tracking-wider uppercase {recoveryRequiresOperator ? 'text-red-300' : 'text-yellow-300'}">
					Position Recovery {recoveryRequiresOperator ? '— Operator Intervention Required' : 'In Progress'}
				</span>
				{#if recovery?.status}
					<span class="text-[10px] uppercase tracking-wider border px-2 py-0.5 {recoveryRequiresOperator ? 'text-red-200 border-red-800' : 'text-yellow-200 border-yellow-800'}">
						{recovery.status}
					</span>
				{/if}
			</div>
			{#if recoverySummary}
				<div class="text-xs mt-1 {recoveryRequiresOperator ? 'text-red-200/90' : 'text-yellow-100/90'}">
					{recoverySummary}
				</div>
			{/if}
			<div class="mt-2 flex flex-wrap gap-4 text-[11px] text-[#888]">
				<span>Positions: <span class="text-white">{recovery?.position_count ?? 0}</span></span>
				<span>Discrepancies: <span class="text-white">{recovery?.discrepancy_count ?? 0}</span></span>
				<span>Open orders: <span class="text-white">{recovery?.open_order_count ?? 0}</span></span>
				{#if recovery?.last_checked_at}
					<span>Checked: <span class="text-white">{new Date(recovery.last_checked_at).toLocaleString()}</span></span>
				{/if}
			</div>
		</div>
	{/if}

	{#if actionMessage}
		<div class="border border-[#3a3220] bg-[#16130d] px-4 py-3 text-sm text-yellow-200">
			{actionMessage}
		</div>
	{/if}

	{#if circuitBreakers.length > 0}
		<div class="flex flex-wrap items-center gap-2">
			<span class="text-[10px] uppercase tracking-wider text-[#666]">Circuit Breakers</span>
			{#each circuitBreakers as cb}
				<span class={`text-[11px] px-2 py-1 border ${breakerColor(cb.state)}`}>
					{cb.label}: {cb.state}
				</span>
			{/each}
		</div>
	{/if}

	{#if hasRiskData}
	<div class="grid grid-cols-1 md:grid-cols-3 gap-4">
		{#each gauges as gauge}
			{@const ratio = gaugeRatio(gauge.value, gauge.max)}
			{@const color = gaugeColor(gauge.value, gauge.max)}
			<div class="border border-[#222] bg-[#050505] p-4">
				<div class="flex items-center gap-4">
					<div class="relative w-20 h-20 rounded-full" style={`background: conic-gradient(${color} ${ratio * 3.6}deg, #222 0deg);`}>
						<div class="absolute inset-2 rounded-full bg-[#050505] flex items-center justify-center text-[11px] font-bold text-[#888]">
							{formatPct(gauge.value)}
						</div>
					</div>
					<div class="min-w-0">
						<div class="text-[11px] uppercase tracking-wider text-[#666]">{gauge.label}</div>
						<div class={`text-lg font-bold ${ratio >= 100 ? 'text-red-400' : ratio >= 75 ? 'text-yellow-400' : 'text-emerald-400'}`}>
							{formatPct(gauge.value)}
						</div>
						<div class="text-[10px] text-[#666]">Limit: {formatPct(gauge.max)}</div>
					</div>
				</div>
			</div>
		{/each}
	</div>

	<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
		<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-bold uppercase tracking-wider text-white">Trading Status</h2>
				<span class={`text-xs px-2 py-1 border ${tradingAllowed ? 'text-emerald-400 border-emerald-800' : 'text-red-400 border-red-800'}`}>
					{tradingAllowed ? 'Allowed' : 'Halted'}
				</span>
			</div>
			<div class="text-xs text-[#888]">{tradingReason}</div>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2">
				<div class="border border-[#222] bg-[#050505] p-3">
					<div class="text-[10px] uppercase tracking-wider text-[#666] mb-1">Daily PnL</div>
					<div class={`text-base font-bold ${dailyPnlUsd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{formatUsd(dailyPnlUsd)}</div>
				</div>
				<div class="border border-[#222] bg-[#050505] p-3">
					<div class="flex items-center justify-between mb-1">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Equity Anchors</div>
						<button
							type="button"
							disabled={rebaselineBusy}
							class="border border-[#2b2b2b] px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[#666] transition hover:text-white disabled:opacity-50"
							title="Re-anchor HWM / daily start to a fresh live wallet reading"
							on:click={() => void handleRebaseline()}
						>{rebaselineBusy ? 'Re-baselining…' : 'Re-baseline'}</button>
					</div>
					<div class="text-xs text-[#888]">HWM: ${highWaterMark.toFixed(2)}</div>
					<div class="text-xs text-[#888]">Daily Start: ${dailyStartEquity.toFixed(2)}</div>
				</div>
			</div>
		</div>

		<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Risk Limits</h2>
			{#each limitBars as bar}
				<div class="space-y-1">
					<div class="flex items-center justify-between text-[11px]">
						<span class="text-[#888]">{bar.label}</span>
						<span class={bar.current > bar.max ? 'text-red-400' : 'text-[#888]'}>
							{formatPct(bar.current)} / {formatPct(bar.max)}
						</span>
					</div>
					<div class="h-2 bg-[#1a1a1a] overflow-hidden">
						<div
							class={`h-full ${bar.current > bar.max ? 'bg-red-500' : 'bg-emerald-500'}`}
							style={`width: ${clampPercent(bar.max > 0 ? (bar.current / bar.max) * 100 : 0)}%;`}
						></div>
					</div>
				</div>
			{/each}
		</div>
	</div>
	{:else if !loading}
		<div class="border border-[#3a2f1a] bg-[#161208] p-4 text-sm text-yellow-200">
			Risk telemetry is unavailable. Gauges and limits cannot be displayed — the values below are not safe-zero readings.
		</div>
	{/if}

	{#if liveBudget}
	<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
		<div class="flex items-center justify-between">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Live Portfolio Budget</h2>
			<div class="flex items-center gap-2">
				<span class={`text-xs px-2 py-1 border ${liveBudget.enabled ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}>
					{liveBudget.enabled ? 'Enforcing' : 'Disabled'}
				</span>
				<a href="/settings#trading/risk.live_max_total_open_risk_pct" class="text-[10px] uppercase tracking-wider text-[#666] hover:text-[#888] transition-colors">Edit caps</a>
			</div>
		</div>
		<p class="text-[11px] text-[#666]">
			Account-level admission gate for new LIVE positions — total dollars at risk to stops, plus net
			exposure per asset and per correlated group, all against real account equity. Paper strategies
			keep their own isolated $10k sandboxes and are not counted here.
		</p>

		{#if !liveBudget.equity_available}
			<div class="border border-red-800 bg-red-950/20 px-3 py-2 text-xs text-red-200">
				Account equity snapshot unavailable — the budget gate is FAILING CLOSED (new live opens are blocked)
				until the daemon equity feed recovers.
			</div>
		{/if}
		{#if (liveBudget.stops_missing ?? 0) > 0}
			<div class="border border-yellow-800 bg-yellow-950/20 px-3 py-2 text-xs text-yellow-200">
				{liveBudget.stops_missing} open live position(s) carry no recorded stop — their risk is counted at a
				conservative 3% of notional.
			</div>
		{/if}
		{#if ceilingsMissing.length > 0}
			<div class="border border-yellow-800 bg-yellow-950/20 px-3 py-2 text-xs text-yellow-200">
				<div>
					{ceilingsMissing.length} live strategy(ies) have no go-live notional ceiling recorded — only the
					account-wide caps bound them.
				</div>
				<div class="mt-1.5 flex flex-wrap gap-1.5">
					{#each ceilingsMissing as sid}
						<button
							type="button"
							class="border border-yellow-700 bg-yellow-950/40 px-2 py-0.5 text-[11px] text-yellow-100 transition hover:bg-yellow-900/40"
							on:click={() => void editCeiling(sid)}
						>Set ceiling for {sid}</button>
					{/each}
				</div>
			</div>
		{/if}
		{#if liveCeilings.length > 0}
			<div class="pt-1">
				<div class="text-[10px] uppercase tracking-wider text-[#666] mb-1">Go-live notional ceilings</div>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
					{#each liveCeilings as [sid, ceiling]}
						<div class="border border-[#222] bg-[#050505] px-3 py-2 flex items-center justify-between text-[11px]">
							<a href={`/lab/strategy/${sid}`} class="font-mono text-white hover:text-[#888]">{sid}</a>
							<span class="flex items-center gap-2 text-[#888]">
								{formatBudgetUsd(Number(ceiling.ceiling_usd ?? 0))} max/asset
								<button
									type="button"
									class="border border-[#2b2b2b] px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[#666] transition hover:text-white"
									on:click={() => void editCeiling(sid, Number(ceiling.ceiling_usd ?? 0))}
								>Edit</button>
							</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<div class="space-y-1">
			<div class="flex items-center justify-between text-[11px]">
				<span class="text-[#888]">Total open risk (to stops)</span>
				<span class={liveBudgetRiskLimit > 0 && liveBudgetRiskUsed > liveBudgetRiskLimit ? 'text-red-400' : 'text-[#888]'}>
					{formatBudgetUsd(liveBudgetRiskUsed)} / {liveBudgetRiskLimit > 0 ? formatBudgetUsd(liveBudgetRiskLimit) : '—'}
					{#if liveBudget.limits_pct?.live_max_total_open_risk_pct}
						<span class="text-[#666]">({liveBudget.limits_pct.live_max_total_open_risk_pct}% of equity)</span>
					{/if}
				</span>
			</div>
			<div class="h-2 bg-[#1a1a1a] overflow-hidden">
				<div
					class={`h-full ${liveBudgetRiskLimit > 0 && liveBudgetRiskUsed / liveBudgetRiskLimit >= 1 ? 'bg-red-500' : liveBudgetRiskLimit > 0 && liveBudgetRiskUsed / liveBudgetRiskLimit >= 0.75 ? 'bg-yellow-500' : 'bg-emerald-500'}`}
					style={`width: ${clampPercent(liveBudgetRiskLimit > 0 ? (liveBudgetRiskUsed / liveBudgetRiskLimit) * 100 : 0)}%;`}
				></div>
			</div>
		</div>

		{#if liveBudgetBooks.length > 0}
			<div class="space-y-2 pt-1">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Per-wallet capacity (direction books)</div>
				{#each liveBudgetBooks as [bookName, b]}
					{@const used = Number(b.gross_notional_usd ?? 0)}
					{@const bookCap = Number(b.limit_usd ?? 0)}
					{@const bookEq = Number(b.equity_usd ?? 0)}
					<div class="space-y-1">
						<div class="flex items-center justify-between text-[11px]">
							<span class="text-[#888] capitalize">{bookName} wallet
								{#if bookEq > 0}<span class="text-[#555]">(${bookEq.toLocaleString(undefined, { maximumFractionDigits: 0 })} equity, {b.positions ?? 0} pos)</span>{/if}
							</span>
							<span class={bookCap > 0 && used > bookCap ? 'text-red-400' : 'text-[#888]'}>
								{formatBudgetUsd(used)} / {bookCap > 0 ? formatBudgetUsd(bookCap) : '—'} notional
							</span>
						</div>
						<div class="h-1.5 bg-[#1a1a1a] overflow-hidden">
							<div
								class={`h-full ${bookCap > 0 && used / bookCap >= 1 ? 'bg-red-500' : bookCap > 0 && used / bookCap >= 0.75 ? 'bg-yellow-500' : 'bg-emerald-600'}`}
								style={`width: ${clampPercent(bookCap > 0 ? (used / bookCap) * 100 : 0)}%;`}
							></div>
						</div>
					</div>
				{/each}
			</div>
		{/if}

		{#if liveBudgetGroups.length > 0}
			<div class="space-y-2 pt-1">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Correlated-group net exposure</div>
				{#each liveBudgetGroups as [name, g]}
					{@const net = Number(g.net_notional_usd ?? 0)}
					{@const cap = Number(g.limit_usd ?? 0)}
					<div class="space-y-1">
						<div class="flex items-center justify-between text-[11px]">
							<span class="text-[#888]">{name} <span class="text-[#555]">({g.positions} pos)</span></span>
							<span class={cap > 0 && Math.abs(net) > cap ? 'text-red-400' : net >= 0 ? 'text-emerald-400' : 'text-red-300'}>
								{net >= 0 ? 'net long' : 'net short'} {formatBudgetUsd(net)} / {cap > 0 ? formatBudgetUsd(cap) : '—'}
							</span>
						</div>
						<div class="h-1.5 bg-[#1a1a1a] overflow-hidden">
							<div
								class={`h-full ${cap > 0 && Math.abs(net) / cap >= 1 ? 'bg-red-500' : net >= 0 ? 'bg-emerald-600' : 'bg-red-600'}`}
								style={`width: ${clampPercent(cap > 0 ? (Math.abs(net) / cap) * 100 : 0)}%;`}
							></div>
						</div>
					</div>
				{/each}
			</div>
		{/if}

		{#if liveBudgetAssets.length > 0}
			<div class="pt-1">
				<div class="text-[10px] uppercase tracking-wider text-[#666] mb-1">Per-asset</div>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
					{#each liveBudgetAssets as [assetName, a]}
						{@const anet = Number(a.net_notional_usd ?? 0)}
						<div class="border border-[#222] bg-[#050505] px-3 py-2 flex items-center justify-between text-[11px]">
							<span class="font-bold text-[#888]">{assetName} <span class="font-normal text-[#555]">({a.positions})</span></span>
							<span class="text-[#888]">
								<span class={anet >= 0 ? 'text-emerald-400' : 'text-red-300'}>{anet >= 0 ? '+' : '−'}{formatBudgetUsd(anet)}</span>
								<span class="text-[#555]"> · risk {formatBudgetUsd(Number(a.risk_usd ?? 0))}</span>
							</span>
						</div>
					{/each}
				</div>
			</div>
		{:else}
			<div class="text-xs text-[#666]">No open live positions — full budget available.</div>
		{/if}
	</div>
	{/if}

	{#if liquidityGuard}
	<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
		<div class="flex items-center justify-between">
			<h2 class="text-sm font-bold uppercase tracking-wider text-white">Liquidity Guard</h2>
			<div class="flex items-center gap-2">
				<span class={`text-xs px-2 py-1 border ${liquidityGuard.enabled ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}>
					{liquidityGuard.enabled ? 'Enforcing' : liquidityGuard.enabled === false ? 'Disabled' : 'Unavailable'}
				</span>
				<a href="/settings#trading/risk.live_liquidity_guard_enabled" class="text-[10px] uppercase tracking-wider text-[#666] hover:text-[#888] transition-colors">Edit limits</a>
			</div>
		</div>
		<p class="text-[11px] text-[#666]">
			Pre-trade microstructure checks on every live OPEN order — 24h volume floor, max spread, max share
			of near-mid book depth, and max estimated price impact, measured against the mainnet book. Fails
			closed when market data is unavailable; closes are never blocked.
		</p>
		{#if liquidityGuard.limits}
			<div class="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px]">
				<div class="border border-[#222] bg-[#050505] px-3 py-2">
					<div class="text-[#666]">Min 24h volume</div>
					<div class="text-[#888]">{formatBudgetUsd(Number(liquidityGuard.limits.live_min_daily_volume_usd ?? 0))}</div>
				</div>
				<div class="border border-[#222] bg-[#050505] px-3 py-2">
					<div class="text-[#666]">Max spread</div>
					<div class="text-[#888]">{Number(liquidityGuard.limits.live_max_spread_bps ?? 0)} bps</div>
				</div>
				<div class="border border-[#222] bg-[#050505] px-3 py-2">
					<div class="text-[#666]">Depth window</div>
					<div class="text-[#888]">{Number(liquidityGuard.limits.live_book_depth_window_bps ?? 0)} bps</div>
				</div>
				<div class="border border-[#222] bg-[#050505] px-3 py-2">
					<div class="text-[#666]">Max depth share</div>
					<div class="text-[#888]">{Number(liquidityGuard.limits.live_max_book_participation_pct ?? 0)}%</div>
				</div>
				<div class="border border-[#222] bg-[#050505] px-3 py-2">
					<div class="text-[#666]">Max price impact</div>
					<div class="text-[#888]">{Number(liquidityGuard.limits.live_max_price_impact_bps ?? 0)} bps</div>
				</div>
			</div>
		{/if}
		{#if liquidityDecisions.length > 0}
			<div class="pt-1">
				<div class="text-[10px] uppercase tracking-wider text-[#666] mb-1">Recent order checks</div>
				<div class="space-y-1">
					{#each liquidityDecisions.slice(0, 8) as decision}
						<div class="border border-[#222] bg-[#050505] px-3 py-1.5 flex items-center gap-2 text-[11px]">
							<span class={`px-1.5 py-0.5 border text-[10px] uppercase ${decision.allowed ? 'text-emerald-400 border-emerald-800' : 'text-red-400 border-red-800'}`}>
								{decision.allowed ? 'Pass' : 'Block'}
							</span>
							<span class="font-bold text-[#888]">{decision.asset}</span>
							<span class="text-[#666] uppercase text-[10px]">{decision.side}</span>
							<span class="text-[#666] truncate" title={decision.reason}>{decision.reason}</span>
						</div>
					{/each}
				</div>
			</div>
		{:else}
			<div class="text-xs text-[#666]">No live orders checked since the backend started.</div>
		{/if}
	</div>
	{/if}

	<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
		<h2 class="text-sm font-bold uppercase tracking-wider text-white">Correlation Groups</h2>
		{#if Object.entries(groups).length === 0}
			<div class="text-xs text-[#666]">No active position groups.</div>
		{:else}
			<div class="space-y-3">
				{#each Object.entries(groups) as [name, group]}
					{@const budget = Number(limits.portfolio_budget ?? 0.02)}
					{@const longValue = Number(group.gross_long ?? 0)}
					{@const shortValue = Number(group.gross_short ?? 0)}
					{@const netValue = Number(group.net ?? 0)}
					<div class="border border-[#222] bg-[#050505] p-3">
						<div class="flex items-center justify-between text-xs mb-2">
							<span class="font-bold text-[#888]">{name}</span>
							<span class={netValue >= 0 ? 'text-emerald-400' : 'text-red-400'}>
								Net {formatPct(netValue)}
							</span>
						</div>
						<div class="h-2 bg-[#141414] overflow-hidden flex">
							<div class="bg-emerald-600" style={`width: ${getExposureWidth(longValue, budget)}%;`}></div>
							<div class="bg-red-600" style={`width: ${getExposureWidth(shortValue, budget)}%;`}></div>
						</div>
						<div class="mt-2 text-[10px] text-[#666] flex gap-4">
							<span>Long {formatPct(longValue)}</span>
							<span>Short {formatPct(shortValue)}</span>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	{#if loading}
		<LoadingState message="Loading risk telemetry..." />
	{/if}

	{#if error}
		<ErrorBanner message={error} tone="error" />
	{/if}
</div>
