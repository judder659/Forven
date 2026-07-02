<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { getForvenDashboard, getForvenEquityHistory, getForvenRecentTrades, getForvenOpenTrades, getForvenScannerState } from '$lib/api';
	import type { ForvenDashboardResponse, ForvenEquityHistory, ForvenScannerState, ForvenTrade } from '$lib/api';
	import EquityChart from '$lib/components/EquityChart.svelte';

	let dashboard: ForvenDashboardResponse | null = null;
	let equityData: ForvenEquityHistory = { base: 0, curve: [] };
	let recentTrades: ForvenTrade[] = [];
	let openTrades: ForvenTrade[] = [];
	let scannerState: ForvenScannerState | null = null;
	let loading = true;
	let dashboardError = '';
	let realtime: RealtimeRefreshController | null = null;
	let fetchInFlight = false;
	let fetchQueued = false;

	function asRecord(value: unknown): Record<string, unknown> | null {
		if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
		return value as Record<string, unknown>;
	}

	function normalizeTradesPayload(payload: unknown): ForvenTrade[] {
		if (Array.isArray(payload)) return payload as ForvenTrade[];
		const record = asRecord(payload);
		if (!record) return [];
		if (Array.isArray(record.trades)) return record.trades as ForvenTrade[];
		if (Array.isArray(record.open_trades)) return record.open_trades as ForvenTrade[];
		if (Array.isArray(record.recent_trades)) return record.recent_trades as ForvenTrade[];
		if (Array.isArray(record.items)) return record.items as ForvenTrade[];
		return [];
	}

	function normalizeEquityPayload(payload: unknown): ForvenEquityHistory {
		const fallback: ForvenEquityHistory = { base: 0, curve: [] };
		const record = asRecord(payload);
		if (!record) return fallback;
		const nestedCurve = asRecord(record.equity_curve);
		const curve = Array.isArray(record.curve)
			? record.curve
			: Array.isArray(nestedCurve?.equity_curve)
				? nestedCurve.equity_curve
				: [];
		return {
			base: Number(record.base ?? 0),
			curve: curve as ForvenEquityHistory['curve'],
		};
	}

	async function runFetchData() {
		try {
			const [dash, equity, recent, open, scanner] = await Promise.allSettled([
				getForvenDashboard(),
				getForvenEquityHistory(),
				getForvenRecentTrades(10),
				getForvenOpenTrades(),
				getForvenScannerState(),
			]);

			if (dash.status === 'fulfilled') {
				const normalizedDashboard = asRecord(dash.value);
				dashboard = normalizedDashboard ? (dash.value as ForvenDashboardResponse) : null;
				dashboardError = '';
			} else {
				dashboard = null;
				dashboardError = dash.reason instanceof Error
					? dash.reason.message
					: 'Unable to fetch HyperLiquid wallet balance';
			}
			if (equity.status === 'fulfilled') equityData = normalizeEquityPayload(equity.value);
			if (recent.status === 'fulfilled') recentTrades = normalizeTradesPayload(recent.value).slice(0, 40);
			if (open.status === 'fulfilled') openTrades = normalizeTradesPayload(open.value).slice(0, 40);
			if (scanner.status === 'fulfilled') {
				const normalizedScanner = asRecord(scanner.value);
				scannerState = normalizedScanner ? (scanner.value as ForvenScannerState) : null;
			}
		} catch (err) {
			console.error('Failed to fetch live trading data', err);
		} finally {
			loading = false;
		}
	}

	async function fetchData() {
		if (fetchInFlight) {
			fetchQueued = true;
			return;
		}
		fetchInFlight = true;
		try {
			await runFetchData();
		} finally {
			fetchInFlight = false;
			if (fetchQueued) {
				fetchQueued = false;
				void fetchData();
			}
		}
	}

	onMount(() => {
		realtime = createRealtimeRefresh(fetchData, {
			fallbackMs: 60_000,
			wsDebounceMs: 6000,
			wsEvents: ['trade', 'strategy_promoted', 'kill_switch_activated', 'kill_switch_cleared'],
			pollWhenWsOfflineOnly: false,
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
		realtime = null;
		fetchInFlight = false;
		fetchQueued = false;
	});

	$: activeStratCount = Array.isArray(scannerState?.strategies) ? scannerState.strategies.length : 0;
	$: activeStratNames = Array.isArray(scannerState?.strategies) ? (scannerState.strategies as string[]) : [];
	$: prices = asRecord(dashboard?.prices) ?? {};
	$: priceEntries = Object.entries(prices).slice(0, 16);
	$: hiddenPriceCount = Math.max(0, Object.keys(prices).length - priceEntries.length);
	$: executionMode = String(dashboard?.execution_mode ?? 'paper').trim().toLowerCase();
	$: simulationActive = Boolean(dashboard?.simulation_active);
	$: dailyRiskStartEquity = toNumber(dashboard?.daily_risk?.start_equity);
	$: dailyRiskCurrentEquity = toNumber(dashboard?.daily_risk?.current_equity);
	$: accountValue = (() => {
		const accountEquity = toNumber(dashboard?.account?.accountValue);
		if (accountEquity !== null && accountEquity > 0) return accountEquity;
		if (dailyRiskCurrentEquity !== null && dailyRiskCurrentEquity > 0) return dailyRiskCurrentEquity;
		if (executionMode === 'paper' && !simulationActive) {
			if (dailyRiskStartEquity !== null && dailyRiskStartEquity > 0) return dailyRiskStartEquity;
			return 10_000;
		}
		return 0;
	})();
	$: availableToTrade = toNumber(dashboard?.account?.withdrawable) ?? 0;
	$: marginUsed = toNumber(dashboard?.account?.totalMarginUsed) ?? 0;
	$: accountNetworkLabel = String(dashboard?.account?.network ?? dashboard?.recovery?.network ?? '').trim().toUpperCase();
	$: accountSourceLabel = String(dashboard?.account?.source ?? '').trim().toUpperCase();
	$: equityCurve = Array.isArray(equityData?.curve)
		? equityData.curve
			.map((point) => ({
				timestamp: String(point.time ?? ''),
				equity: Number(point.value ?? accountValue),
			}))
			.filter((point: { timestamp: string; equity: number }) => point.timestamp && Number.isFinite(point.equity))
		: [];

	function toNumber(value: unknown): number | null {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}

	function formatUsd(value: unknown): string {
		const parsed = toNumber(value) ?? 0;
		return parsed.toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	}

	function formatPrice(value: unknown): string {
		const parsed = toNumber(value);
		if (parsed === null) return '--';
		return `$${parsed.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	}

	function tradeDirection(trade: ForvenTrade): string {
		return String(trade.direction ?? '').trim().toLowerCase() || 'long';
	}

	function tradeStatus(trade: ForvenTrade): string {
		return String(trade.status ?? '').trim().toUpperCase() || 'UNKNOWN';
	}

	function tradeStrategyLabel(trade: ForvenTrade): string {
		// The open/recent-trades API can omit the `strategy` label (older backends
		// only selected strategy_id), which showed '--'. Fall back to name/id.
		const t = trade as Record<string, unknown>;
		const pick = (v: unknown) => String(v ?? '').trim();
		return pick(t.strategy) || pick(t.strategy_name) || pick(t.strategy_id) || '--';
	}

	type TradeBadge = {
		label: string;
		className: string;
	};

	function tradeSignalDataRecord(trade: ForvenTrade): Record<string, unknown> {
		return asRecord(trade.signal_data) ?? {};
	}

	function tradeBadges(trade: ForvenTrade): TradeBadge[] {
		const signalData = tradeSignalDataRecord(trade);
		const badges: TradeBadge[] = [];
		const source = String(trade.source ?? '').trim().toLowerCase();
		const exchangeSource = String(signalData.source ?? '').trim().toLowerCase();
		const protectionStatus = String(signalData.recovery_protection_status ?? '').trim().toLowerCase();

		// Only flag "Recovered" while the recovery is UNFINISHED (not yet attributed
		// to a real strategy + protected). Keeps the dashboard in sync with the
		// trades page; backend `source` stays 'exchange_recovered' for PnL logic.
		const recovered = source === 'exchange_recovered' || Boolean(signalData.recovery_reason);
		const strategyAttr = String(trade.strategy ?? '').trim().toLowerCase();
		const recoveryHealthy = Boolean(strategyAttr) && strategyAttr !== 'exchange_recovered' && protectionStatus === 'protected';
		if (recovered && !recoveryHealthy) {
			badges.push({
				label: 'Recovered',
				className: 'border-amber-700/60 bg-amber-950/40 text-amber-300',
			});
		}
		if (source === 'exchange' || exchangeSource === 'exchange_sync') {
			badges.push({
				label: 'Exchange-backed',
				className: 'border-[#555] bg-[#111] text-[#aaa]',
			});
		}
		if (protectionStatus === 'missing') {
			badges.push({
				label: 'Needs protection',
				className: 'border-red-700/60 bg-red-950/40 text-red-300',
			});
		} else if (protectionStatus === 'partial') {
			badges.push({
				label: 'Partial protection',
				className: 'border-orange-700/60 bg-orange-950/40 text-orange-300',
			});
		}
		return badges;
	}

	function isExchangeBackedTrade(trade: ForvenTrade): boolean {
		const src = String((trade as any).source ?? '').toLowerCase();
		if (src === 'exchange' || src === 'exchange_sync' || src === 'exchange_recovered') return true;
		let sd: any = (trade as any).signal_data;
		if (typeof sd === 'string') {
			try { sd = JSON.parse(sd); } catch { sd = null; }
		}
		if (sd && typeof sd === 'object') {
			const sdSrc = String(sd.source ?? '').toLowerCase();
			if (sdSrc === 'exchange_sync' || sdSrc === 'exchange_recovered') return true;
			if (sd.recovery_reason) return true;
		}
		return false;
	}

	function computeLivePnlUsd(trade: ForvenTrade): number | null {
		const fallback = toNumber(trade.pnl_usd);
		// FE-LIVE-1: for exchange-backed / recovered positions the exchange-reported
		// pnl_usd is authoritative. Recomputing size*move off possibly-stale dashboard
		// prices (and a book sub-account entry the price feed may not match) would show
		// the operator a wrong live PnL — exactly what they watch during the soak.
		if (isExchangeBackedTrade(trade)) {
			return fallback;
		}
		const entry = toNumber(trade.entry_price);
		const size = toNumber(trade.size);
		const asset = typeof trade.asset === 'string' ? trade.asset : '';
		const currentPriceRaw = prices?.[asset];
		const currentPrice = toNumber(currentPriceRaw);
		if (entry === null || size === null || currentPrice === null) {
			return fallback;
		}
		const direction = String(trade.direction ?? 'long').toLowerCase();
		const move = direction === 'short'
			? entry - currentPrice
			: currentPrice - entry;
		return size * move;
	}

	function tradeDisplayPnl(trade: ForvenTrade): number | null {
		return String(trade.status ?? '').toUpperCase() === 'OPEN'
			? computeLivePnlUsd(trade)
			: toNumber(trade.pnl_usd);
	}

	$: closedPnl = recentTrades.reduce((acc, trade) => {
		if (String(trade?.status ?? '').toUpperCase() === 'OPEN') return acc;
		return acc + (toNumber(trade?.pnl_usd) ?? 0);
	}, 0);
	$: openPnl = openTrades.reduce((acc, trade) => acc + (computeLivePnlUsd(trade) ?? 0), 0);
	$: tradeDerivedSessionPnl = closedPnl + openPnl;
	$: sessionPnl = (() => {
		if (executionMode === 'paper' && !simulationActive) {
			const start = dailyRiskStartEquity ?? accountValue;
			const current = dailyRiskCurrentEquity ?? accountValue;
			if (start > 0) return current - start;
			return 0;
		}
		return tradeDerivedSessionPnl;
	})();
	$: recentRows = recentTrades.slice(0, 8);

	function tradeElapsed(trade: ForvenTrade): string {
		const opened = trade.opened_at;
		if (!opened) return '--';
		const ms = Date.now() - new Date(opened).getTime();
		if (Number.isNaN(ms) || ms < 0) return '--';
		const mins = Math.floor(ms / 60000);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ${mins % 60}m`;
		const days = Math.floor(hrs / 24);
		return `${days}d ${hrs % 24}h`;
	}

	function tradeCurrentPrice(trade: ForvenTrade): string {
		const asset = typeof trade.asset === 'string' ? trade.asset : '';
		const cp = toNumber(prices?.[asset]);
		return cp !== null ? formatPrice(cp) : '--';
	}

	let expanded = true;
</script>

{#if loading && !dashboard}
	<div class="animate-pulse border border-[#222] bg-[#050505] p-3 text-xs text-gray-500">
		Loading live trading data...
	</div>
{:else if dashboardError && !dashboard}
	<div class="border border-red-800/50 bg-red-950/20 p-3 text-xs text-red-300">
		{dashboardError}
	</div>
{:else if dashboard}
	<!-- KPI Strip -->
	<div class="flex flex-wrap items-center gap-x-4 gap-y-1 border border-[#222] bg-[#050505] px-3 py-1.5 font-mono text-xs" data-testid="live-stats-strip">
		<span><span class="mr-1 text-[10px] uppercase text-gray-500">Equity</span><span class="font-bold text-gray-200">${formatUsd(accountValue)}</span>{#if accountNetworkLabel}<span class="text-[10px] text-teal-400"> {accountNetworkLabel}</span>{/if}</span>
		<span><span class="mr-1 text-[10px] uppercase text-gray-500">Avail</span><span class="text-gray-300">${formatUsd(availableToTrade)}</span></span>
		<span><span class="mr-1 text-[10px] uppercase text-gray-500">Margin</span><span class="text-gray-300">${formatUsd(marginUsed)}</span></span>
		<span><span class="mr-1 text-[10px] uppercase text-gray-500">Session</span><span class="font-bold {sessionPnl >= 0 ? 'text-green-400' : 'text-red-400'}">{sessionPnl >= 0 ? '+' : '-'}${Math.abs(sessionPnl).toFixed(2)}</span></span>
		<span><span class="mr-1 text-[10px] uppercase text-gray-500">Open</span><span class="text-gray-200">{openTrades.length}</span>{#if activeStratCount > 0}<span class="text-purple-400"> · {activeStratCount} strat</span>{/if}</span>
		<span class="flex min-w-0 flex-wrap items-center gap-1">
			{#each priceEntries as [coin, price]}
				<span class="border border-[#333] bg-[#111] px-1 text-[10px]"><span class="font-bold text-white">{coin}</span> <span class="text-gray-300">{formatPrice(price)}</span></span>
			{:else}
				<span class="text-[10px] text-gray-500">Waiting…</span>
			{/each}
			{#if hiddenPriceCount > 0}<span class="text-[10px] text-gray-600">+{hiddenPriceCount}</span>{/if}
		</span>
		{#if activeStratNames.length > 0}
			<span class="flex min-w-0 flex-wrap items-center gap-1">
				<span class="text-[10px] uppercase text-gray-500">Active</span>
				{#each activeStratNames.slice(0, 6) as stratName}
					<span class="inline-flex items-center gap-1 border border-[#333] bg-[#111] px-1 text-[10px]">
						<span class="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-green-500"></span>
						<span class="truncate text-gray-300">{stratName}</span>
					</span>
				{/each}
				{#if activeStratNames.length > 6}
					<span class="text-[10px] text-gray-600">+{activeStratNames.length - 6}</span>
				{/if}
			</span>
		{/if}
	</div>

	{#if dashboard?.recovery?.active || dashboard?.recovery?.requires_operator}
		<div class="border border-amber-800/60 bg-amber-950/20 px-3 py-2 text-[10px] text-amber-200">
			<div class="font-bold uppercase tracking-wider text-amber-300">Recovery Blocking Entries</div>
			<div class="mt-1 text-amber-100/90">{dashboard?.recovery?.summary || 'Startup exchange recovery is active.'}</div>
		</div>
	{/if}

	<!-- Open Trades (always visible) -->
	{#if openTrades.length > 0}
		<div class="border border-[#222] bg-[#050505]">
			<div class="px-3 py-2 border-b border-[#222] flex items-center gap-2">
				<h3 class="font-bold text-[10px] text-gray-400 uppercase tracking-wider">Open Trades</h3>
				<span class="border border-yellow-700 bg-yellow-900 px-1.5 py-0.5 text-[9px] text-yellow-300">{openTrades.length}</span>
			</div>
			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead>
						<tr class="border-b border-[#222] text-gray-500 uppercase tracking-wider">
							<th class="px-3 py-1.5 font-medium">Asset</th>
							<th class="px-3 py-1.5 font-medium">Dir</th>
							<th class="px-3 py-1.5 font-medium">Strategy</th>
							<th class="px-3 py-1.5 font-medium">Entry</th>
							<th class="px-3 py-1.5 font-medium">Current</th>
							<th class="px-3 py-1.5 font-medium">PnL</th>
							<th class="px-3 py-1.5 font-medium">Duration</th>
						</tr>
					</thead>
					<tbody class="divide-y divide-[#222]">
						{#each openTrades as trade}
							{@const direction = tradeDirection(trade)}
							{@const pnl = computeLivePnlUsd(trade)}
							{@const badges = tradeBadges(trade)}
							<tr class="hover:bg-[#111] transition-colors">
								<td class="px-3 py-1.5 font-bold text-gray-200">{String(trade.asset ?? '--')}</td>
								<td class="px-3 py-1.5">
									<span class="border px-1 py-0.5 text-[10px] font-bold uppercase tracking-wider
										{direction === 'long' ? 'text-green-500 border-green-500/30' : 'text-red-500 border-red-500/30'}"
									>
										{direction}
									</span>
								</td>
								<td class="px-3 py-1.5 text-gray-400">
									<div>{tradeStrategyLabel(trade)}</div>
									{#if badges.length > 0}
										<div class="mt-1 flex flex-wrap gap-1">
											{#each badges as badge}
												<span class={`border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider ${badge.className}`}>
													{badge.label}
												</span>
											{/each}
										</div>
									{/if}
								</td>
								<td class="px-3 py-1.5 text-gray-300">{formatPrice(trade.entry_price)}</td>
								<td class="px-3 py-1.5 text-gray-300">{tradeCurrentPrice(trade)}</td>
								<td class="px-3 py-1.5 font-bold {(pnl ?? 0) >= 0 ? 'text-green-500' : 'text-red-500'}">
									{pnl != null ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : '--'}
								</td>
								<td class="px-3 py-1.5 text-gray-400">{tradeElapsed(trade)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}

	<!-- Expand toggle for equity curve + trades -->
	<button
		class="w-full flex items-center justify-between px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500 hover:text-gray-300 transition-colors"
		on:click={() => (expanded = !expanded)}
	>
		<span class="flex items-center gap-2">
			<span class="flex items-center gap-1">
				{#if dashboard}
					{@const executionMode = String(dashboard.execution_mode ?? 'paper').toLowerCase()}
					{@const tradingAllowed = Boolean(dashboard.trading_allowed)}
					<span class="border border-[#333] px-1.5 py-0.5 text-[10px] {executionMode === 'live' ? 'text-green-500' : 'text-yellow-500'}">{executionMode}</span>
					<span class="border border-[#333] px-1.5 py-0.5 text-[10px] {tradingAllowed ? 'text-green-500' : 'text-red-500'}">{tradingAllowed ? 'Trading' : 'Halted'}</span>
				{/if}
			</span>
			<span>Live Trading Details</span>
		</span>
		<svg class="w-3 h-3 transition-transform {expanded ? 'rotate-180' : ''}" viewBox="0 0 20 20" fill="currentColor">
			<path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd" />
		</svg>
	</button>

	{#if expanded}

		<!-- Recent Executions -->
		{#if recentTrades.length > 0}
			<div class="border border-[#222] bg-[#050505]">
				<div class="px-3 py-2 border-b border-[#222]">
					<h3 class="font-bold text-[10px] text-gray-400 uppercase tracking-wider">Recent Executions</h3>
				</div>
				<div class="overflow-x-auto">
					<table class="w-full text-left text-xs">
						<thead>
							<tr class="border-b border-[#222] text-gray-500 uppercase tracking-wider">
								<th class="px-3 py-1.5 font-medium">Asset</th>
								<th class="px-3 py-1.5 font-medium">Dir</th>
								<th class="px-3 py-1.5 font-medium">Strategy</th>
								<th class="px-3 py-1.5 font-medium">Entry</th>
								<th class="px-3 py-1.5 font-medium">PnL</th>
								<th class="px-3 py-1.5 font-medium">Status</th>
							</tr>
						</thead>
						<tbody class="divide-y divide-[#222]">
							{#each recentRows as trade}
								{@const direction = tradeDirection(trade)}
								{@const status = tradeStatus(trade)}
								{@const pnl = tradeDisplayPnl(trade)}
								<tr class="hover:bg-[#111] transition-colors">
									<td class="px-3 py-1.5 font-bold text-gray-200">{String(trade.asset ?? '--')}</td>
									<td class="px-3 py-1.5">
										<span class="border px-1 py-0.5 text-[10px] font-bold uppercase tracking-wider
											{direction === 'long' ? 'text-green-500 border-green-500/30' : 'text-red-500 border-red-500/30'}"
										>
											{direction}
										</span>
									</td>
									<td class="px-3 py-1.5 text-gray-400">{tradeStrategyLabel(trade)}</td>
									<td class="px-3 py-1.5 text-gray-300">{formatPrice(trade.entry_price)}</td>
									<td class="px-3 py-1.5 font-bold {(pnl ?? 0) >= 0 ? 'text-green-500' : 'text-red-500'}">
										{pnl != null ? `$${pnl.toFixed(2)}` : '--'}
									</td>
									<td class="px-3 py-1.5">
										<span class="border px-1 py-0.5 text-[10px] font-bold uppercase tracking-wider
											{status === 'OPEN' ? 'text-yellow-500 border-yellow-500/30' : 'text-gray-500 border-gray-600'}"
										>
											{status}
										</span>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		{/if}
	{/if}
{/if}
