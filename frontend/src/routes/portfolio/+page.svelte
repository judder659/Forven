<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getPortfolioAllocation,
		getPortfolioBasket,
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

	async function load() {
		try {
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
	$: weights = Object.entries(basket?.positions?.weights ?? {});
	$: longLegs = weights.filter(([, w]) => w > 0).sort((x, y) => y[1] - x[1]);
	$: shortLegs = weights.filter(([, w]) => w < 0).sort((x, y) => x[1] - y[1]);
	$: equityCurve = (basket?.equity_curve ?? []).map(
		(p): EquityPoint => ({ timestamp: p.t, equity: p.equity })
	);
	$: decomposition = basket?.pnl_decomposition ?? { price: 0, funding: 0, cost: 0 };
	$: fundingShare =
		Math.abs(decomposition.funding) + Math.abs(decomposition.price) > 0
			? Math.abs(decomposition.funding) /
				(Math.abs(decomposition.funding) + Math.abs(decomposition.price))
			: null;

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
						href="/settings#trading/risk.basket_funding_carry_enabled"
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
				<div class="grid grid-cols-2 md:grid-cols-5 gap-2 text-center">
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
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Positions</div>
						<div class="text-base font-bold text-white">{basket.positions?.count ?? 0}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Rebalances</div>
						<div class="text-base font-bold text-white">{basket.rebalances ?? 0}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Last tick</div>
						<div class="text-[11px] font-bold text-[#aaa] pt-1">{fmtWhen(basket.last_tick_at)}</div>
					</div>
				</div>

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

				<!-- current legs -->
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
					<div>
						<div class="text-[10px] uppercase tracking-wider text-emerald-500 mb-1">
							Long (lowest funding)
						</div>
						{#each longLegs as [sym, w]}
							<div
								class="flex items-center justify-between border border-[#1c2b1c] bg-[#050805] px-3 py-1.5 text-[11px] mb-1"
							>
								<span class="font-bold text-[#aaa]">{sym}</span>
								<span class="text-emerald-400">{fmtPct(w, 1)}</span>
							</div>
						{:else}
							<div class="text-[11px] text-[#555]">none</div>
						{/each}
					</div>
					<div>
						<div class="text-[10px] uppercase tracking-wider text-red-500 mb-1">
							Short (highest funding)
						</div>
						{#each shortLegs as [sym, w]}
							<div
								class="flex items-center justify-between border border-[#2b1c1c] bg-[#080505] px-3 py-1.5 text-[11px] mb-1"
							>
								<span class="font-bold text-[#aaa]">{sym}</span>
								<span class="text-red-400">{fmtPct(w, 1)}</span>
							</div>
						{:else}
							<div class="text-[11px] text-[#555]">none</div>
						{/each}
					</div>
				</div>
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
						href="/settings#trading/risk.portfolio_allocator_enabled"
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
