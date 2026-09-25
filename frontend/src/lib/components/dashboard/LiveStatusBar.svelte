<script lang="ts">
	/**
	 * One line that says whether real money is trading and whether it is safe.
	 * LIVE comes from live-stage strategies and live-armed bots — the global
	 * execution_mode setting is only a label, so it is never read here.
	 */
	import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import { formatAge, killSwitchDisarmed, liveNetwork, parseTime } from '$lib/utils/liveDashboard';

	export let dashboard: ForvenDashboardResponse | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let fleet: LiveFleet | null = null;
	export let now = Date.now();

	$: countsKnown = fleet !== null || typeof dashboard?.live_strategy_count === 'number';
	$: strategyCount = fleet ? fleet.strategies.length : (dashboard?.live_strategy_count ?? 0);
	$: botCount = fleet ? fleet.live_bots_armed : (dashboard?.live_bot_count ?? 0);
	$: live = strategyCount + botCount > 0;
	$: network = liveNetwork(dashboard);
	$: armedLabel = [
		strategyCount > 0 ? `${strategyCount} ${strategyCount === 1 ? 'strategy' : 'strategies'}` : '',
		botCount > 0 ? `${botCount} ${botCount === 1 ? 'bot' : 'bots'}` : '',
	].filter(Boolean).join(' + ');
	$: tradingAllowed = dashboard?.trading_allowed !== false;
	$: killActive = Boolean(risk?.kill_switch_active);
	$: killDisarmed = killSwitchDisarmed(risk);
	$: openBreakers = Object.entries(dashboard?.circuit_breakers ?? {}).filter(
		([, state]) => state && String(state).toLowerCase() !== 'closed',
	);
	$: recovery = dashboard?.recovery ?? null;
	$: staleAfterMs = (fleet?.stale_after_seconds ?? 1800) * 1000;
	$: lastScanTs = parseTime(dashboard?.last_scan ?? null);
	$: scanStale = lastScanTs === null || now - lastScanTs > staleAfterMs;

	const chip = 'border px-2 py-0.5 whitespace-nowrap';
	const calm = 'border-[#333] text-gray-400';
	const good = 'border-emerald-900 text-emerald-400';
	const warn = 'border-amber-800 bg-amber-500/5 text-amber-300';
	const bad = 'border-red-800 bg-red-500/10 text-red-300';
</script>

<div
	class="flex flex-wrap items-center gap-x-2 gap-y-1.5 border border-[#222] bg-[#050505] px-3 py-2 text-[11px] uppercase tracking-wider"
	data-testid="live-status-bar"
>
	{#if !dashboard}
		<span class="{chip} {calm}">Loading live status…</span>
	{:else if live}
		<span
			class="{chip} flex items-center gap-2 font-bold {network === 'mainnet' ? bad : good}"
			title="Strategies at the live stage and live-armed bots send real orders"
			data-testid="live-mode"
		>
			<span class="relative flex h-2 w-2">
				<span class="absolute inline-flex h-full w-full animate-ping rounded-full {network === 'mainnet' ? 'bg-red-400' : 'bg-emerald-400'} opacity-50"></span>
				<span class="relative inline-flex h-2 w-2 rounded-full {network === 'mainnet' ? 'bg-red-400' : 'bg-emerald-400'}"></span>
			</span>
			Live · {network ?? 'network unknown'} · {armedLabel}
		</span>
	{:else if countsKnown}
		<span class="{chip} {calm} font-bold" data-testid="live-mode">Paper only · no live strategies</span>
	{:else}
		<span class="{chip} {warn}" data-testid="live-mode">Live status unknown</span>
	{/if}

	{#if dashboard}
		<a href="/risk" class="{chip} {tradingAllowed ? good : bad}" title={dashboard.trading_reason ?? ''}>
			{tradingAllowed ? 'Trading allowed' : `Trading halted: ${dashboard.trading_reason ?? 'unknown'}`}
		</a>
	{/if}

	{#if killActive}
		<a href="/risk" class="{chip} {bad} font-bold">Kill switch tripped</a>
	{:else if killDisarmed}
		<a href="/risk" class="{chip} {warn}" title="Auto-trigger is off: reaching the max-drawdown limit will not halt trading or close positions.">
			Kill switch disarmed
		</a>
	{:else if risk}
		<span class="{chip} {calm}">Kill switch armed</span>
	{/if}

	{#if risk?.daily_loss_halt}
		<a href="/risk" class="{chip} {bad}">Daily-loss halt</a>
	{/if}

	{#if dashboard}
		{#if openBreakers.length > 0}
			<span class="{chip} {warn}" title="Hyperliquid circuit breakers">
				Breaker open: {openBreakers.map(([name]) => name.replace(/^hl_/, '')).join(', ')}
			</span>
		{:else if live}
			<span class="{chip} {calm}" title="Hyperliquid price, trade and account circuit breakers">Exchange ok</span>
		{/if}

		{#if recovery?.requires_operator}
			<a href="/live-trades" class="{chip} {bad}">Recovery needs you</a>
		{:else if recovery?.active}
			<span class="{chip} {warn}">Recovery running</span>
		{:else if live && recovery?.last_checked_at}
			<span class="{chip} {calm}" title={recovery.summary ?? ''}>
				Reconciled {formatAge(recovery.last_checked_at, now)} ago
			</span>
		{/if}

		<span
			class="{chip} {scanStale ? warn : calm}"
			title="Last scanner pass over the live strategies"
		>
			{lastScanTs === null ? 'No scan yet' : `Scan ${formatAge(dashboard.last_scan, now)} ago`}
		</span>
	{/if}
</div>
