<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		armBasketLive,
		disarmBasketLive,
		getBasketLive,
		getPortfolioAllocation,
		getPortfolioBasket,
		getPortfolioLayerEnabled,
		refreshPortfolioAllocation,
		resetPortfolioBasket,
		tickPortfolioBasket,
		type BasketLiveStatus,
		type BasketSummary,
		type PortfolioAllocationResponse,
	} from '$lib/api/portfolio';
	import type { EquityPoint } from '$lib/api';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	let basket: BasketSummary | null = null;
	let allocation: PortfolioAllocationResponse | null = null;
	let live: BasketLiveStatus | null = null;
	let armWallet = '';
	let armCapital = 500;
	let armPhrase = '';
	let armBusy = false;
	let disarmBusy = false;
	let confirmingFlatten = false;

	async function doArm() {
		armBusy = true;
		actionMessage = '';
		try {
			await armBasketLive(armPhrase, armCapital, armWallet);
			actionMessage = 'Live execution ARMED — the next tick reconciles the wallet.';
			armPhrase = '';
			await load();
		} catch (e) {
			actionMessage = `Arming refused: ${e instanceof Error ? e.message : e}`;
		} finally {
			armBusy = false;
		}
	}

	async function doDisarm(flatten: boolean) {
		if (flatten && !confirmingFlatten) {
			confirmingFlatten = true;
			return;
		}
		confirmingFlatten = false;
		disarmBusy = true;
		actionMessage = '';
		try {
			await disarmBasketLive(flatten);
			actionMessage = flatten
				? 'Disarmed and flattened — every wallet position was closed reduce-only.'
				: 'Disarmed — positions left in the wallet; use Disarm + flatten to close them.';
			await load();
		} catch (e) {
			actionMessage = `Disarm failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			disarmBusy = false;
		}
	}
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
			const [b, a, lv] = await Promise.all([
				getPortfolioBasket(),
				getPortfolioAllocation(),
				getBasketLive().catch(() => null),
			]);
			basket = b;
			allocation = a;
			live = lv;
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
	$: forwardBook = snapshot?.book?.forward ?? null;
	$: forwardCurve = ((forwardBook?.curve ?? []) as Array<{ t: string; equity: number }>).map(
		(pnt): EquityPoint => ({ timestamp: pnt.t, equity: pnt.equity })
	);

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

	// --- reference capital: render fractions as money ------------------------
	// The engine works in fractions of 1.0 (so results scale to any capital),
	// but fractions read as noise. Everything renders in dollars at a
	// reference capital the operator picks; the underlying math is untouched.
	let referenceCapital = 10_000;
	onMount(() => {
		const saved = localStorage.getItem('portfolio.referenceCapital');
		if (saved && Number(saved) > 0) referenceCapital = Number(saved);
	});
	$: if (typeof localStorage !== 'undefined' && referenceCapital > 0) {
		localStorage.setItem('portfolio.referenceCapital', String(referenceCapital));
	}
	const fmtUsd = (fraction: number | null | undefined, digits = 2) => {
		if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return '—';
		const usd = fraction * referenceCapital;
		const abs = Math.abs(usd);
		const shown = abs >= 1000 ? usd.toLocaleString(undefined, { maximumFractionDigits: 0 }) : usd.toFixed(digits);
		return `${usd < 0 ? '−' : ''}$${shown.replace('-', '')}`;
	};

	// Plain-language "what happened" over the last ~24h of ticks.
	$: last24 = (basket?.recent_ticks ?? []).filter(
		(t) => Date.now() - new Date(t.t).getTime() < 24 * 3_600_000
	);
	$: day = last24.reduce(
		(acc, t) => ({
			funding: acc.funding + t.funding_pnl,
			price: acc.price + t.price_pnl,
			cost: acc.cost + t.cost,
			rebalances: acc.rebalances + (t.rebalanced ? 1 : 0),
		}),
		{ funding: 0, price: 0, cost: 0, rebalances: 0 }
	);
</script>

<svelte:head>
	<title>Portfolio — Forven</title>
</svelte:head>

<div class="space-y-4 p-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-wider text-white">Portfolio</h1>
			<p class="text-[11px] text-[#666]">
				The book above the strategies: measured-risk allocation across your strategy team, and
				basket products run directly at the portfolio level. Everything proves itself on paper
				before it can touch real sizing.
			</p>
		</div>
		<label class="flex items-center gap-2 text-[11px] text-[#666] shrink-0">
			Show amounts as if running
			<span class="flex items-center border border-[#333] bg-[#0a0a0a] px-2 py-1">
				$<input
					type="number"
					bind:value={referenceCapital}
					min="100"
					step="1000"
					class="w-24 bg-transparent text-right text-white outline-none"
				/>
			</span>
		</label>
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

				<!-- plain-language day summary: the sentence a person actually wants -->
				{#if last24.length > 0}
					<div class="border border-[#1a2438] bg-[#050a12] px-3 py-2 text-[11px] text-[#9ab]">
						<span class="font-bold text-white">Last 24h:</span>
						collected <span class="text-emerald-400">{fmtUsd(day.funding)}</span> in funding,
						<span class={day.price >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtUsd(day.price)}</span> from price moves,
						<span class="text-red-300">{day.cost > 0 ? `−${fmtUsd(day.cost)}` : '$0.00'}</span> in costs
						{#if day.rebalances > 0}· rebalanced {day.rebalances}×{/if}
						<span class="text-[#556]"> (at ${referenceCapital.toLocaleString()} reference)</span>
					</div>
				{/if}

				<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2 text-center">
					<div class="border border-[#222] bg-[#0a0a0a] p-2" title="The paper account's value at your reference capital. It started at exactly the reference amount.">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Book value</div>
						<div class="text-base font-bold text-white">{fmtUsd(basket.equity ?? 1)}</div>
					</div>
					<div class="border border-[#222] bg-[#0a0a0a] p-2">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Total P&L</div>
						<div
							class={`text-base font-bold ${(basket.total_return_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
						>
							{fmtUsd((basket.equity ?? 1) - 1)}
							<span class="block text-[10px] font-normal text-[#666]">{fmtNum(basket.total_return_pct, 3)}%</span>
						</div>
					</div>
					<div
						class="border border-[#222] bg-[#0a0a0a] p-2"
						title="What the book earns per year if funding rates and prices hold — the fees it collects for taking the unpopular side. The reason this basket exists."
					>
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Expected income /yr</div>
						<div
							class={`text-base font-bold ${(basket.expected_carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
						>
							{fmtUsd(basket.expected_carry_annualized)}
							<span class="block text-[10px] font-normal text-[#666]">{fmtPct(basket.expected_carry_annualized, 1)}</span>
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
						<div class="border border-[#222] bg-[#0a0a0a] p-2" title="Fees collected from (or paid to) the other side of the market — the income this basket exists to harvest. Should dominate.">
							<div class="text-[#666]">Fees collected (funding)</div>
							<div class={decomposition.funding >= 0 ? 'text-emerald-400' : 'text-red-400'}>
								{fmtUsd(decomposition.funding)}
							</div>
						</div>
						<div class="border border-[#222] bg-[#0a0a0a] p-2" title="Profit or loss from prices moving. The long and short sides mostly cancel — this should stay small relative to funding.">
							<div class="text-[#666]">Price moves</div>
							<div class={decomposition.price >= 0 ? 'text-emerald-400' : 'text-red-400'}>
								{fmtUsd(decomposition.price)}
							</div>
						</div>
						<div class="border border-[#222] bg-[#0a0a0a] p-2" title="Simulated trading fees + slippage paid at each rebalance.">
							<div class="text-[#666]">Trading costs</div>
							<div class="text-red-300">−{fmtUsd(decomposition.cost)}</div>
						</div>
					</div>
				</div>

				<!-- current legs: weight, the funding each is positioned against, and
				     its expected carry contribution -->
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
					<div>
						<div class="flex items-baseline justify-between mb-1">
							<span class="text-[10px] uppercase tracking-wider text-emerald-500">Long (lowest funding)</span>
							<span class="text-[9px] uppercase tracking-wider text-[#555]">weight · funding /yr · earns /yr</span>
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
										class={`w-20 text-right ${(leg.carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
										title="What this leg earns per year at current rates, at your reference capital"
									>
										{leg.carry_annualized !== null ? fmtUsd(leg.carry_annualized) : '—'}
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
							<span class="text-[9px] uppercase tracking-wider text-[#555]">weight · funding /yr · earns /yr</span>
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
										class={`w-20 text-right ${(leg.carry_annualized ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
										title="What this leg earns per year at current rates, at your reference capital"
									>
										{leg.carry_annualized !== null ? fmtUsd(leg.carry_annualized) : '—'}
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
											<td class="py-1 pr-2 text-right text-[#bbb]">{fmtUsd(tick.equity)}</td>
											<td class={`py-1 pr-2 text-right ${tick.funding_pnl > 0 ? 'text-emerald-400' : tick.funding_pnl < 0 ? 'text-red-400' : 'text-[#555]'}`}>
												{tick.funding_pnl !== 0 ? fmtUsd(tick.funding_pnl) : '—'}
											</td>
											<td class={`py-1 pr-2 text-right ${tick.price_pnl > 0 ? 'text-emerald-400' : tick.price_pnl < 0 ? 'text-red-400' : 'text-[#555]'}`}>
												{tick.price_pnl !== 0 ? fmtUsd(tick.price_pnl) : '—'}
											</td>
											<td class={`py-1 pr-2 text-right ${tick.cost > 0 ? 'text-red-300' : 'text-[#555]'}`}>
												{tick.cost > 0 ? `−${fmtUsd(tick.cost)}` : '—'}
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

			<!-- ─────────────── live execution (real money, behind arming) ─────────────── -->
			<div class={`border p-3 space-y-3 ${live?.armed ? 'border-red-800 bg-[#0a0505]' : 'border-[#222] bg-[#070707]'}`}>
				<div class="flex items-center justify-between">
					<div>
						<h3 class="text-xs font-bold uppercase tracking-wider text-white">
							Live execution
							{#if live?.armed}
								<span class="ml-2 text-[10px] px-2 py-0.5 border border-red-700 text-red-400">ARMED — ${Number(live.capital_usd ?? 0).toLocaleString()} in wallet “{live.wallet_label}”</span>
							{:else}
								<span class="ml-2 text-[10px] px-2 py-0.5 border border-[#333] text-[#666]">Not armed — paper only</span>
							{/if}
						</h3>
						<p class="text-[11px] text-[#666]">
							When armed, each tick mirrors the paper book into a dedicated wallet with real
							orders: increases go through the liquidity guard, reductions are reduce-only,
							every order is ceiling-checked and logged below. Losses are confined to that
							wallet's capital.
						</p>
					</div>
				</div>

				{#if live?.armed}
					{#if live.last_reconcile}
						<div class="text-[11px] text-[#888]">
							Last reconcile {fmtWhen(live.last_reconcile.t)}:
							<span class="text-emerald-400">{live.last_reconcile.orders_ok} orders ok</span>
							{#if live.last_reconcile.orders_failed > 0}
								· <span class="text-red-400">{live.last_reconcile.orders_failed} failed</span>
							{/if}
							{#if live.last_reconcile.unlistable > 0}
								· <span class="text-yellow-500">{live.last_reconcile.unlistable} leg(s) not listed on the venue</span>
							{/if}
						</div>
					{/if}
					<div class="flex items-center gap-2">
						<button
							class="border border-[#333] bg-[#111] px-3 py-1.5 text-xs text-[#aaa] hover:bg-[#1a1a1a] disabled:opacity-50"
							on:click={() => doDisarm(false)}
							disabled={disarmBusy}
						>
							Disarm (leave positions)
						</button>
						<button
							class={`border px-3 py-1.5 text-xs disabled:opacity-50 ${confirmingFlatten ? 'border-red-700 bg-red-950 text-red-300' : 'border-red-900 bg-[#150808] text-red-400 hover:bg-[#1f0a0a]'}`}
							on:click={() => doDisarm(true)}
							disabled={disarmBusy}
						>
							{confirmingFlatten ? 'Click again: close ALL wallet positions' : 'Disarm + flatten'}
						</button>
						{#if confirmingFlatten}
							<button class="text-[11px] text-[#666] hover:text-[#888]" on:click={() => (confirmingFlatten = false)}>cancel</button>
						{/if}
					</div>
				{:else}
					<div class="grid grid-cols-1 md:grid-cols-4 gap-2 items-end text-[11px]">
						<label class="space-y-1">
							<span class="text-[#666] uppercase text-[10px] tracking-wider">Dedicated wallet label</span>
							<input
								type="text"
								bind:value={armWallet}
								placeholder="e.g. basket"
								class="w-full border border-[#333] bg-[#0a0a0a] px-2 py-1.5 text-white outline-none"
							/>
						</label>
						<label class="space-y-1">
							<span class="text-[#666] uppercase text-[10px] tracking-wider">Capital (USD)</span>
							<input
								type="number"
								bind:value={armCapital}
								min="50"
								step="50"
								class="w-full border border-[#333] bg-[#0a0a0a] px-2 py-1.5 text-white outline-none"
							/>
						</label>
						<label class="space-y-1">
							<span class="text-[#666] uppercase text-[10px] tracking-wider">Type GO LIVE to confirm</span>
							<input
								type="text"
								bind:value={armPhrase}
								placeholder="GO LIVE"
								class="w-full border border-[#333] bg-[#0a0a0a] px-2 py-1.5 text-white outline-none"
							/>
						</label>
						<button
							class="border border-red-900 bg-[#150808] px-3 py-1.5 text-xs text-red-400 hover:bg-[#1f0a0a] disabled:opacity-40"
							on:click={doArm}
							disabled={armBusy || armPhrase.trim().toUpperCase() !== 'GO LIVE' || !armWallet.trim() || armCapital <= 0}
						>
							{armBusy ? 'Arming…' : 'ARM LIVE EXECUTION'}
						</button>
					</div>
					<p class="text-[10px] text-[#555]">
						Requires a registered named wallet with no other trades in it (Settings →
						HyperLiquid → Wallets). Recommendation: don't arm until the paper book has weeks of
						evidence and its funding share stays dominant.
					</p>
				{/if}

				{#if (live?.ledger ?? []).length > 0}
					<details class="text-[11px]">
						<summary class="cursor-pointer text-[#666] hover:text-[#888] uppercase text-[10px] tracking-wider">Execution ledger ({(live?.ledger ?? []).length} recent)</summary>
						<div class="mt-1 space-y-0.5 max-h-48 overflow-y-auto">
							{#each live?.ledger ?? [] as entry}
								<div class="text-[#777] font-mono text-[10px] border-b border-[#141414] py-0.5">
									{JSON.stringify(entry)}
								</div>
							{/each}
						</div>
					</details>
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

				{#if forwardBook && (forwardBook.active_days ?? 0) > 0}
					<div class="space-y-1">
						<div class="text-[10px] uppercase tracking-wider text-emerald-600">
							Walk-forward book — the honest track record
							<span class="normal-case text-[#557]">(each day weighted by multipliers published BEFORE it — out-of-sample since {forwardBook.since})</span>
						</div>
						<div class="grid grid-cols-3 gap-2 text-[11px] text-center">
							<div class="border border-[#1c2b1c] bg-[#050805] p-2">
								<div class="text-[#666]">Return</div>
								<div class={(forwardBook.total_return ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
									{fmtUsd(forwardBook.total_return)} <span class="text-[#666]">({fmtPct(forwardBook.total_return)})</span>
								</div>
							</div>
							<div class="border border-[#1c2b1c] bg-[#050805] p-2">
								<div class="text-[#666]">Sharpe</div>
								<div class="text-[#aaa]">{fmtNum(forwardBook.sharpe)}</div>
							</div>
							<div class="border border-[#1c2b1c] bg-[#050805] p-2">
								<div class="text-[#666]">Max drawdown</div>
								<div class="text-red-300">{fmtPct(forwardBook.max_drawdown)}</div>
							</div>
						</div>
						{#if forwardCurve.length > 2}
							<EquityChart data={forwardCurve} height={160} showDrawdown={false} />
						{/if}
					</div>
				{:else if forwardBook?.note}
					<div class="text-[11px] text-[#556]">{forwardBook.note}</div>
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
