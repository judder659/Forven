<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getPortfolioAllocation,
		getPortfolioBasket,
		getPortfolioLayerEnabled,
		refreshPortfolioAllocation,
		resetPortfolioBasket,
		tickPortfolioBasket,
		type BasketSummary,
		type PortfolioAllocationResponse,
	} from '$lib/api/portfolio';
	import type { EquityPoint } from '$lib/api';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	let basket: BasketSummary | null = null;
	let allocation: PortfolioAllocationResponse | null = null;
	let loading = true;
	let error = '';
	let actionMessage = '';
	let tickBusy = false;
	let resetBusy = false;
	let refreshBusy = false;
	let confirmingReset = false;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	// PORT-GATE-1: while the master switch is off, every other portfolio route
	// 404s — show the enable pointer instead of an error wall.
	let layerEnabled: boolean | null = null;

	async function load() {
		try {
			layerEnabled = await getPortfolioLayerEnabled();
			if (!layerEnabled) {
				loading = false;
				return;
			}
			const [b, a] = await Promise.all([getPortfolioBasket(), getPortfolioAllocation()]);
			basket = b;
			allocation = a;
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		// The basket ticks hourly and the allocator refreshes hourly — a slow
		// poll keeps the page current without hammering the backend.
		refreshTimer = setInterval(load, 60_000);
	});
	onDestroy(() => {
		if (refreshTimer) clearInterval(refreshTimer);
	});

	async function forceTick() {
		tickBusy = true;
		actionMessage = '';
		try {
			const res = await tickPortfolioBasket();
			actionMessage = res.ok ? 'Tick executed.' : 'Tick skipped — see backend log.';
			await load();
		} catch (e) {
			actionMessage = `Tick failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			tickBusy = false;
		}
	}

	async function doReset() {
		if (!confirmingReset) {
			confirmingReset = true;
			return;
		}
		confirmingReset = false;
		resetBusy = true;
		actionMessage = '';
		try {
			await resetPortfolioBasket();
			actionMessage = 'Paper book reset — it re-initializes on the next tick.';
			await load();
		} catch (e) {
			actionMessage = `Reset failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			resetBusy = false;
		}
	}

	async function refreshAllocation() {
		refreshBusy = true;
		actionMessage = '';
		try {
			await refreshPortfolioAllocation();
			await load();
			actionMessage = 'Allocation recomputed.';
		} catch (e) {
			actionMessage = `Refresh failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			refreshBusy = false;
		}
	}

	// --- basket derivations -------------------------------------------------
	$: legs = basket?.legs ?? [];
	$: longLegs = legs.filter((l) => l.weight > 0).sort((a, b) => b.weight - a.weight);
	$: shortLegs = legs.filter((l) => l.weight < 0).sort((a, b) => a.weight - b.weight);
	$: equityCurve = (basket?.equity_curve ?? []).map(
		(p): EquityPoint => ({ timestamp: p.t, equity: p.equity })
	);
	$: decomposition = basket?.pnl_decomposition ?? { price: 0, funding: 0, cost: 0 };
	$: fundingShare =
		Math.abs(decomposition.funding) + Math.abs(decomposition.price) > 0
			? Math.abs(decomposition.funding) /
				(Math.abs(decomposition.funding) + Math.abs(decomposition.price))
			: null;
	$: tickAge = basket?.tick_age_hours ?? null;
	// The hourly job missing two beats means the loop is dead — say so loudly.
	$: tickStale = basket?.enabled && tickAge !== null && tickAge > 2.5;
	$: universe = basket?.universe ?? null;
	$: universeThin =
		universe && basket?.config ? universe.eligible < basket.config.n_legs * 2 : false;
	$: recentTicks = (basket?.recent_ticks ?? []).slice(0, 12);
	$: nextRebalanceIn = (() => {
		const iso = basket?.next_rebalance_at;
		if (!iso) return null;
		const ms = new Date(iso).getTime() - Date.now();
		if (Number.isNaN(ms)) return null;
		if (ms <= 0) return 'due now';
		const h = Math.floor(ms / 3_600_000);
		const m = Math.round((ms % 3_600_000) / 60_000);
		return h > 0 ? `in ${h}h ${m}m` : `in ${m}m`;
	})();

	// Funding rates are stored PER-HOUR; annualize for human eyes.
	const annualizeHourly = (rate: number | null | undefined) =>
		rate === null || rate === undefined ? null : rate * 24 * 365;

	// --- allocator derivations ----------------------------------------------
	$: snapshot = allocation?.snapshot ?? null;
	$: strategies = Object.entries(snapshot?.strategies ?? {}).sort(
		(a, b) => (b[1].risk_multiplier ?? 0) - (a[1].risk_multiplier ?? 0)
	);
	$: measuredCount = snapshot?.book?.measured_strategies ?? 0;
	$: virtualBook = snapshot?.book?.virtual ?? null;

	const fmtPct = (v: number | null | undefined, digits = 2) =>
		v === null || v === undefined || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(digits)}%`;
	const fmtNum = (v: number | null | undefined, digits = 2) =>
		v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits);
	const fmtWhen = (iso: string | null | undefined) => {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	};
</script>

<svelte:head>
	<title>Portfolio — Forven</title>
</svelte:head>

<div class="space-y-4 p-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-wider text-white">Portfolio</h1>
			<p class="text-[11px] text-[#666]">
				The book above the strategies: measured-risk allocation and basket products. Paper
				sandboxes are never scaled — everything here proves itself virtually before touching
				live sizing.
			</p>
		</div>
	</div>

	{#if error}
		<ErrorBanner message={error} />
	{/if}
	{#if actionMessage}
		<div class="border border-[#333] bg-[#0a0a0a] px-3 py-2 text-xs text-[#aaa]">{actionMessage}</div>
	{/if}

	{#if loading}
		<LoadingState message="Loading portfolio…" />
	{:else if layerEnabled === false}
		<div class="border border-[#222] bg-[#050505] p-6 text-center space-y-2">
			<div class="text-sm font-bold uppercase tracking-wider text-[#888]">Portfolio layer is disabled</div>
			<p class="text-xs text-[#666]">
				Enable it under
				<a href="/settings#system/risk.portfolio_layer_enabled" class="underline text-[#888] hover:text-white">Settings → System → Experimental features</a>
				to activate the allocator, the basket, and this page.
			</p>
		</div>
	{:else}
		<!-- ─────────────────────────── funding-carry basket ─────────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-4">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-bold uppercase tracking-wider text-white">
						Funding-Carry Basket
						<span class="ml-2 text-[10px] font-normal normal-case text-[#666]">forward paper book</span>
					</h2>
					<p class="text-[11px] text-[#666]">
						Short the highest-funding perps, long the lowest — dollar-neutral. Paper only: no
						orders are placed. Rebalances on its own cadence; marks and accrues funding hourly.
					</p>
				</div>
				<div class="flex items-center gap-2">
					<span
						class={`text-xs px-2 py-1 border ${basket?.enabled ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}
					>
						{basket?.enabled ? 'Ticking' : 'Disabled'}
					</span>
					<a
						href="/settings#portfolio/risk.basket_funding_carry_enabled"
						class="text-[10px] uppercase tracking-wider text-[#666] hover:text-[#888] transition-colors"
						>Settings</a
					>
				</div>
			</div>

			{#if !basket?.exists}
				<div class="text-xs text-[#666]">
					No paper book yet — it initializes on the first tick. {#if !basket?.enabled}Enable the
						basket in Settings, or force a tick below.{/if}
				</div>
			{:else}
				{#if tickStale}
					<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">
						Last tick was {fmtNum(tickAge, 1)}h ago — the hourly job should have fired. Check the
						scheduler (forven-basket-funding-carry) or force a tick below.
					</div>
				{/if}
				{#if universeThin && universe && basket.config}
					<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-500">
						Universe thin: only {universe.eligible}/{universe.total} symbols eligible at the last
						tick — the basket wants {basket.config.n_legs * 2} legs. Stale closes or missing
						funding series shrink eligibility; the keepalive catches up within a collector cycle.
					</div>
				{/if}

				<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2 text-center">
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Equity</div>
						<div class="text-base font-bold text-white">{fmtNum(basket.equity, 5)}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Total return</div>
						<div
							class={`text-base font-bold ${(basket.total_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
						>
							{fmtNum(basket.total_return_pct, 3)}%
						</div>
					</div>
					<div
						class="border border-[#222] bg-[#0a0a0a] p-2"
						title="Sum of −weight × current funding across the legs, annualized — what the book earns if rates and prices hold. The reason this basket exists."
					>
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Expected carry /yr</div>
						<div
							class={`text-base font-bold ${(basket.expected_carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
						>
							{fmtPct(basket.expected_carry_annualized, 1)}
						</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Positions</div>
						<div class="text-base font-bold text-white">{basket.positions?.count ?? 0}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Rebalances</div>
						<div class="text-base font-bold text-white">{basket.rebalances ?? 0}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Next rebalance</div>
						<div class="text-[11px] font-bold text-[#aaa] pt-1">
							{nextRebalanceIn ?? '—'}
						</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Last tick</div>
						<div class={`text-[11px] font-bold pt-1 ${tickStale ? 'text-red-400' : 'text-[#aaa]'}`}>
							{tickAge !== null ? `${fmtNum(tickAge, 1)}h ago` : fmtWhen(basket.last_tick_at)}
						</div>
					</div>
				</div>

				{#if basket.config}
					<div class="text-[10px] text-[#555]">
						{basket.config.rebalance_hours}h cadence · {basket.config.n_legs} legs/side ·
						{basket.config.gross_leverage}× gross · {fmtNum(basket.config.fee_bps + basket.config.slippage_bps, 1)}bps
						cost per traded weight
						{#if universe}
							· universe {universe.eligible}/{universe.total} eligible
						{/if}
						· <a href="/settings#portfolio/risk.basket_rebalance_hours" class="text-[#666] hover:text-[#888] underline">edit</a>
					</div>
				{/if}

				{#if equityCurve.length > 2}
					<EquityChart data={equityCurve} height={220} showDrawdown={false} />
				{:else}
					<div class="text-[11px] text-[#555]">
						Equity curve appears after a few ticks ({equityCurve.length} point{equityCurve.length === 1 ? '' : 's'} so far).
					</div>
				{/if}

				<!-- PnL decomposition: the "is it still carry?" panel -->
				<div class="space-y-1">
					<div class="flex items-center justify-between">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">PnL decomposition</div>
						{#if fundingShare !== null}
							<div class="text-[10px] text-[#666]">
								funding share of gross PnL:
								<span class={fundingShare >= 0.5 ? 'text-emerald-400' : 'text-yellow-400'}
									>{fmtPct(fundingShare, 0)}</span
								>
								{#if fundingShare < 0.5}<span class="text-yellow-500"> — drifting toward beta</span>{/if}
							</div>
						{/if}
					</div>
					<div class="grid grid-cols-3 gap-2 text-center text-[11px]">
						<div class="border border-[#222] bg-[#0a0a0a] p-2">
							<div class="text-[#666]">Funding</div>
							<div class={decomposition.funding >= 0 ? 'text-emerald-400' : 'text-red-400'}>
								{fmtNum(decomposition.funding, 6)}
							</div>
						</div>
						<div class="border border-[#222] bg-[#0a0a0a] p-2">
							<div class="text-[#666]">Price</div>
							<div class={decomposition.price >= 0 ? 'text-emerald-400' : 'text-red-400'}>
								{fmtNum(decomposition.price, 6)}
							</div>
						</div>
						<div class="border border-[#222] bg-[#0a0a0a] p-2">
							<div class="text-[#666]">Costs</div>
							<div class="text-red-300">−{fmtNum(decomposition.cost, 6)}</div>
						</div>
					</div>
				</div>

				<!-- current legs: weight, the funding each is positioned against, and
				     its expected carry contribution -->
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
					<div>
						<div class="flex items-baseline justify-between mb-1">
							<span class="text-[10px] uppercase tracking-wider text-emerald-500">Long (lowest funding)</span>
							<span class="text-[9px] uppercase tracking-wider text-[#555]">weight · funding /yr · carry /yr</span>
						</div>
						{#each longLegs as leg (leg.symbol)}
							<div
								class="flex items-center justify-between border border-[#1c2b1c] bg-[#050805] px-3 py-1.5 text-[11px] mb-1"
							>
								<span class="font-bold text-[#aaa]">{leg.symbol}</span>
								<span class="flex items-center gap-3">
									<span class="text-emerald-400">{fmtPct(leg.weight, 1)}</span>
									<span class="text-[#777] w-16 text-right" title="Current funding rate, annualized. A long leg WANTS this negative — shorts pay it the rate.">
										{fmtPct(annualizeHourly(leg.funding_rate_hourly), 1)}
									</span>
									<span
										class={`w-14 text-right ${(leg.carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
										title="This leg's expected carry contribution, annualized"
									>
										{fmtPct(leg.carry_annualized, 1)}
									</span>
								</span>
							</div>
						{:else}
							<div class="text-[11px] text-[#555]">none</div>
						{/each}
					</div>
					<div>
						<div class="flex items-baseline justify-between mb-1">
							<span class="text-[10px] uppercase tracking-wider text-red-500">Short (highest funding)</span>
							<span class="text-[9px] uppercase tracking-wider text-[#555]">weight · funding /yr · carry /yr</span>
						</div>
						{#each shortLegs as leg (leg.symbol)}
							<div
								class="flex items-center justify-between border border-[#2b1c1c] bg-[#080505] px-3 py-1.5 text-[11px] mb-1"
							>
								<span class="font-bold text-[#aaa]">{leg.symbol}</span>
								<span class="flex items-center gap-3">
									<span class="text-red-400">{fmtPct(leg.weight, 1)}</span>
									<span class="text-[#777] w-16 text-right" title="Current funding rate, annualized. A short leg WANTS this positive — it collects the rate from longs.">
										{fmtPct(annualizeHourly(leg.funding_rate_hourly), 1)}
									</span>
									<span
										class={`w-14 text-right ${(leg.carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
										title="This leg's expected carry contribution, annualized"
									>
										{fmtPct(leg.carry_annualized, 1)}
									</span>
								</span>
							</div>
						{:else}
							<div class="text-[11px] text-[#555]">none</div>
						{/each}
					</div>
				</div>

				<!-- recent ticks: what actually happened, tick by tick -->
				{#if recentTicks.length > 0}
					<div class="space-y-1">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Recent ticks</div>
						<div class="overflow-x-auto">
							<table class="w-full text-[11px]">
								<thead>
									<tr class="text-left text-[10px] uppercase tracking-wider text-[#666] border-b border-[#222]">
										<th class="py-1 pr-2">When</th>
										<th class="py-1 pr-2 text-right">Equity</th>
										<th class="py-1 pr-2 text-right">Funding PnL</th>
										<th class="py-1 pr-2 text-right">Price PnL</th>
										<th class="py-1 pr-2 text-right">Cost</th>
										<th class="py-1 text-right">Event</th>
									</tr>
								</thead>
								<tbody>
									{#each recentTicks as tick (tick.t)}
										<tr class="border-b border-[#151515] text-[#999]">
											<td class="py-1 pr-2">{fmtWhen(tick.t)}</td>
											<td class="py-1 pr-2 text-right text-[#bbb]">{fmtNum(tick.equity, 5)}</td>
											<td class={`py-1 pr-2 text-right ${tick.funding_pnl > 0 ? 'text-emerald-400' : tick.funding_pnl < 0 ? 'text-red-400' : 'text-[#555]'}`}>
												{fmtPct(tick.funding_pnl, 4)}
											</td>
											<td class={`py-1 pr-2 text-right ${tick.price_pnl > 0 ? 'text-emerald-400' : tick.price_pnl < 0 ? 'text-red-400' : 'text-[#555]'}`}>
												{fmtPct(tick.price_pnl, 4)}
											</td>
											<td class={`py-1 pr-2 text-right ${tick.cost > 0 ? 'text-red-300' : 'text-[#555]'}`}>
												{tick.cost > 0 ? `−${fmtPct(tick.cost, 4)}` : '—'}
											</td>
											<td class="py-1 text-right">
												{#if tick.rebalanced}
													<span class="text-[10px] px-1.5 py-0.5 border border-[#333] text-[#aaa]">REBALANCE</span>
												{:else}
													<span class="text-[#555]">mark</span>
												{/if}
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{/if}
			{/if}

			<div class="flex items-center gap-2 pt-1">
				<button
					class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
					on:click={forceTick}
					disabled={tickBusy}
				>
					{tickBusy ? 'Ticking…' : 'Tick now'}
				</button>
				<button
					class={`border px-3 py-1.5 text-xs disabled:opacity-50 ${confirmingReset ? 'border-red-700 bg-red-950 text-red-300' : 'border-[#333] bg-[#111] text-[#aaa] hover:bg-[#1a1a1a]'}`}
					on:click={doReset}
					disabled={resetBusy}
				>
					{resetBusy ? 'Resetting…' : confirmingReset ? 'Click again to confirm reset' : 'Reset paper book'}
				</button>
				{#if confirmingReset}
					<button class="text-[11px] text-[#666] hover:text-[#888]" on:click={() => (confirmingReset = false)}>cancel</button>
				{/if}
			</div>
		</div>

		<!-- ─────────────────────────── allocator ─────────────────────────── -->
		<div class="border border-[#222] bg-[#050505] p-4 space-y-4">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-bold uppercase tracking-wider text-white">
						Measured-Risk Allocator
					</h2>
					<p class="text-[11px] text-[#666]">
						Per-strategy risk multipliers from realized volatility and correlations (1.0 = the flat
						legacy allocation). Publishes weights and proves the combined book virtually; live
						sizing only applies when both allocator flags are enabled.
					</p>
				</div>
				<div class="flex items-center gap-2">
					<span
						class={`text-xs px-2 py-1 border ${allocation?.enabled ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}
					>
						{allocation?.enabled ? 'Refreshing hourly' : 'Disabled'}
					</span>
					<span
						class={`text-xs px-2 py-1 border ${allocation?.live_sizing_enabled ? 'text-red-400 border-red-800' : 'text-[#666] border-[#333]'}`}
					>
						{allocation?.live_sizing_enabled ? 'SIZING LIVE' : 'Not sizing live'}
					</span>
					<a
						href="/settings#portfolio/risk.portfolio_allocator_enabled"
						class="text-[10px] uppercase tracking-wider text-[#666] hover:text-[#888] transition-colors"
						>Settings</a
					>
				</div>
			</div>

			{#if !snapshot}
				<div class="text-xs text-[#666]">
					No allocation snapshot yet — enable the allocator (or recompute below).
				</div>
			{:else}
				<div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-center text-[11px]">
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[#666] uppercase text-[10px] tracking-wider">Cohort</div>
						<div class="text-base font-bold text-white">{snapshot.cohort_size}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[#666] uppercase text-[10px] tracking-wider">Measured</div>
						<div class={`text-base font-bold ${measuredCount > 0 ? 'text-white' : 'text-yellow-400'}`}>
							{measuredCount}
						</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[#666] uppercase text-[10px] tracking-wider">Book vol (est, ann.)</div>
						<div class="text-base font-bold text-white">
							{fmtPct(snapshot.book?.scaled_annualized_vol ?? snapshot.book?.estimated_annualized_vol, 1)}
						</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[#666] uppercase text-[10px] tracking-wider">Computed</div>
						<div class="text-[11px] font-bold text-[#aaa] pt-1">{fmtWhen(snapshot.computed_at)}</div>
					</div>
				</div>

				{#if measuredCount === 0}
					<div class="border border-yellow-900 bg-[#0a0a05] px-3 py-2 text-[11px] text-yellow-500">
						Whole cohort unmeasured — strategies need ~10 distinct trading days of kernel parity
						closes before they earn a measured multiplier. Everything sizes at the neutral 1.0
						until then.
					</div>
				{/if}

				{#if virtualBook?.weighted}
					<div class="space-y-1">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">
							Virtual book — weighted vs flat baseline
							<span class="normal-case text-[#555]">(retrospective, in-sample evidence)</span>
						</div>
						<div class="grid grid-cols-2 gap-2 text-[11px]">
							{#each [{ label: 'Weighted', stats: virtualBook.weighted }, { label: 'Flat baseline', stats: virtualBook.flat_baseline }] as entry}
								{#if entry.stats}
									<div class="border border-[#222] bg-[#0a0a0a] p-2 space-y-0.5">
										<div class="font-bold text-[#aaa]">{entry.label}</div>
										<div class="flex justify-between"><span class="text-[#666]">Return</span><span class={entry.stats.total_return >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtPct(entry.stats.total_return)}</span></div>
										<div class="flex justify-between"><span class="text-[#666]">Sharpe</span><span class="text-[#aaa]">{fmtNum(entry.stats.sharpe)}</span></div>
										<div class="flex justify-between"><span class="text-[#666]">Max DD</span><span class="text-red-300">{fmtPct(entry.stats.max_drawdown)}</span></div>
									</div>
								{/if}
							{/each}
						</div>
					</div>
				{/if}

				{#if strategies.length > 0}
					<div class="overflow-x-auto">
						<table class="w-full text-[11px]">
							<thead>
								<tr class="text-left text-[10px] uppercase tracking-wider text-[#666] border-b border-[#222]">
									<th class="py-1.5 pr-2">Strategy</th>
									<th class="py-1.5 pr-2">Asset</th>
									<th class="py-1.5 pr-2">Stage</th>
									<th class="py-1.5 pr-2">Lean</th>
									<th class="py-1.5 pr-2 text-right">Trade days</th>
									<th class="py-1.5 pr-2 text-right">Ann. vol</th>
									<th class="py-1.5 text-right">Risk multiplier</th>
								</tr>
							</thead>
							<tbody>
								{#each strategies as [sid, s]}
									<tr class="border-b border-[#151515] text-[#999]">
										<td class="py-1.5 pr-2 font-bold text-[#bbb]">{sid}</td>
										<td class="py-1.5 pr-2">{s.asset}</td>
										<td class="py-1.5 pr-2">{s.stage}</td>
										<td class="py-1.5 pr-2">
											<span class={s.direction_lean === 'short' ? 'text-red-400' : 'text-emerald-400'}>{s.direction_lean}</span>
										</td>
										<td class="py-1.5 pr-2 text-right">{s.observed_days}</td>
										<td class="py-1.5 pr-2 text-right">{fmtPct(s.annualized_vol, 1)}</td>
										<td class="py-1.5 text-right">
											{#if s.measured}
												<span class={s.risk_multiplier > 1 ? 'text-emerald-400' : s.risk_multiplier < 1 ? 'text-yellow-400' : 'text-[#aaa]'}>
													×{fmtNum(s.risk_multiplier)}
												</span>
											{:else}
												<span class="text-[#555]" title="Not enough realized parity history — sizes at the legacy flat allocation">×1.00 (unmeasured)</span>
											{/if}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			{/if}

			<div class="flex items-center gap-2 pt-1">
				<button
					class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
					on:click={refreshAllocation}
					disabled={refreshBusy}
				>
					{refreshBusy ? 'Recomputing…' : 'Recompute now'}
				</button>
			</div>
		</div>
	{/if}
</div>
