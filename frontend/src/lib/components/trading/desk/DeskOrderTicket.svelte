<script lang="ts" context="module">
	import type { OpenManualPaperPositionOptions } from '$lib/api/paper';
	import type { TicketResult } from '$lib/utils/tradingDesk/ticket';

	export interface TicketReview {
		options: OpenManualPaperPositionOptions;
		result: TicketResult;
		direction: 'long' | 'short';
		stop: number | null;
		takeProfit: number | null;
	}
</script>

<script lang="ts">
	/**
	 * Manual order ticket. Live: a stop is required, risk is a percent of account
	 * equity, and the order is checked against the per-trade cap, the open-risk
	 * budget, the wallet it routes to, the single-order cap and the ceiling. Paper:
	 * risk is a percent of this strategy's own book and fills at the live mid.
	 */
	import { createEventDispatcher } from 'svelte';
	import type { DeskMode } from '$lib/api/desk';
	import { fmtPct, fmtPx, fmtQty, fmtUsd, num, pxDigits } from '$lib/utils/tradingDesk/format';
	import { defaultRiskPct, ticketCalc, type SizeMode, type TicketInput } from '$lib/utils/tradingDesk/ticket';

	export let mode: DeskMode;
	export let strategyId: string;
	export let asset: string;
	export let tradeMode: string | null | undefined = 'both';
	export let price: number | null = null;
	export let equity: number | null = null;
	export let takerBps = 4.5;
	export let validatedLeverage = 1;
	export let riskPerTrade: number | null = null;
	export let sliceUsd: number | null = null;
	export let atr: number | null = null;
	export let atrMultiplier = 2;
	export let ceilingUsd: number | null = null;
	export let walletFree: { long: number | null; short: number | null } = { long: null, short: null };
	export let limits: TicketInput['limits'] = undefined;
	export let gatesBlocking: string[] = [];
	export let busy = false;

	const dispatch = createEventDispatcher<{ review: TicketReview }>();

	let direction: 'long' | 'short' = tradeMode === 'short_only' ? 'short' : 'long';
	let sizeMode: SizeMode = 'risk';
	let riskPct: string | number | null = '';
	let usd: string | number | null = 100;
	let units: string | number | null = '';
	let leverage: string | number | null = Math.max(1, Math.ceil(validatedLeverage || 1));
	let stop: string | number | null = '';
	let takeProfit: string | number | null = '';
	let seededFor = '';
	let riskSeededFor = '';
	let riskTouched = false;

	// Re-seed when the strategy or direction changes (and once the price arrives).
	$: seedKey = `${strategyId}|${direction}|${price !== null}|${atr !== null}`;
	$: if (seedKey !== seededFor && price !== null) {
		const strategyChanged = !seededFor.startsWith(`${strategyId}|`);
		seededFor = seedKey;
		if (strategyChanged) {
			riskTouched = false;
			leverage = mode === 'live' ? Math.max(1, Math.ceil(validatedLeverage || 1)) : Math.max(1, validatedLeverage || 1);
			takeProfit = '';
		}
		stop = suggestedStop() ?? '';
	}
	// The default risk mirrors the strategy's own sizing, so wait for its inputs
	// (live: the capital slice and account equity) and never override an edit.
	$: riskKey = `${strategyId}|${riskPerTrade}|${sliceUsd}|${equity !== null}`;
	$: if (riskKey !== riskSeededFor && !riskTouched && (mode === 'paper' || (sliceUsd !== null && equity !== null))) {
		riskSeededFor = riskKey;
		riskPct = defaultRiskPct(mode, riskPerTrade, sliceUsd, equity);
	}
	$: if (tradeMode === 'short_only' && direction === 'long') direction = 'short';
	$: if (tradeMode === 'long_only' && direction === 'short') direction = 'long';

	function suggestedStop(): number | null {
		if (atr === null || price === null) return null;
		const raw = direction === 'long' ? price - atrMultiplier * atr : price + atrMultiplier * atr;
		const digits = pxDigits(price);
		return raw > 0 ? Number(raw.toFixed(digits)) : null;
	}

	$: input = {
		mode,
		direction,
		sizeMode,
		riskPct,
		usd,
		units,
		leverage,
		stop,
		takeProfit,
		price,
		equity,
		takerBps,
		strategyId,
		ceilingUsd,
		wallet: mode === 'live' ? { name: direction, freeMargin: walletFree[direction] } : null,
		limits: mode === 'live' ? limits : undefined,
		gatesBlocking: mode === 'live' ? gatesBlocking : [],
	} satisfies TicketInput;
	$: result = ticketCalc(input);
	$: canReview = !busy && !result.errors.length && !result.blocked && result.size !== null;
	$: flagged = result.checks.filter((check) => check.tone !== 'ok');
	$: passing = result.checks.filter((check) => check.tone === 'ok');

	function review() {
		if (!canReview || result.size === null) return;
		const stopValue = num(stop);
		const tpValue = num(takeProfit);
		const options: OpenManualPaperPositionOptions = {
			direction,
			leverage: result.leverage,
			stopLossPrice: stopValue && stopValue > 0 ? stopValue : null,
			takeProfitPrice: tpValue && tpValue > 0 ? tpValue : null,
		};
		// Risk sizing is re-run by the backend at its fresh mark; fixed sizes go as units.
		if (sizeMode === 'risk') options.riskPct = num(riskPct) ?? undefined;
		else options.size = Number(result.size.toFixed(6));
		dispatch('review', { options, result, direction, stop: options.stopLossPrice ?? null, takeProfit: options.takeProfitPrice ?? null });
	}

	const field = 'grid grid-cols-[92px_minmax(0,1fr)] items-center gap-2';
	const inputClass = 'w-full min-w-0 rounded border border-sc-line2 bg-sc-bg px-2 py-1 font-plex-mono text-[12.5px] text-sc-ink focus:border-sc-ink4 focus:outline-none';
	const hint = 'col-start-2 -mt-1 text-[11.5px] text-sc-ink3';
	const seg = (on: boolean) => `px-2.5 py-0.5 text-[12px] ${on ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink2'}`;
</script>

<div class="grid gap-2.5 rounded-md border border-sc-line p-3" data-testid="desk-ticket">
	<div class="flex items-center justify-between gap-2">
		<h3 class="text-[13px] font-semibold text-sc-ink">Manual order</h3>
		{#if mode === 'live'}
			<span class="rounded-full border border-[#8fb0ff]/45 px-2 text-[11.5px] text-[#8fb0ff]">Live · real money</span>
		{:else}
			<span class="rounded-full border border-sc-line2 px-2 text-[11.5px] text-sc-ink2">Paper · simulated</span>
		{/if}
	</div>
	<div class="grid grid-cols-2 overflow-hidden rounded-md border border-sc-line2" role="group" aria-label="Direction">
		<button
			type="button"
			class={`py-1.5 text-[13px] font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${direction === 'long' ? 'bg-[#139a9f]/15 text-[#5ccac4]' : 'text-sc-ink3'}`}
			aria-pressed={direction === 'long'}
			disabled={tradeMode === 'short_only'}
			title={tradeMode === 'short_only' ? `${strategyId} trades short only` : undefined}
			on:click={() => (direction = 'long')}
		>Long</button>
		<button
			type="button"
			class={`py-1.5 text-[13px] font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${direction === 'short' ? 'bg-[#e0663f]/15 text-[#f2956f]' : 'text-sc-ink3'}`}
			aria-pressed={direction === 'short'}
			disabled={tradeMode === 'long_only'}
			title={tradeMode === 'long_only' ? `${strategyId} trades long only` : undefined}
			on:click={() => (direction = 'short')}
		>Short</button>
	</div>
	<div class={field}>
		<span class="text-[12px] text-sc-ink2">Size by</span>
		<div class="inline-flex w-fit overflow-hidden rounded-full border border-sc-line2" role="group" aria-label="Size by">
			<button type="button" class={seg(sizeMode === 'risk')} aria-pressed={sizeMode === 'risk'} on:click={() => (sizeMode = 'risk')}>Risk</button>
			<button type="button" class={seg(sizeMode === 'usd')} aria-pressed={sizeMode === 'usd'} on:click={() => (sizeMode = 'usd')}>USD</button>
			<button type="button" class={seg(sizeMode === 'units')} aria-pressed={sizeMode === 'units'} on:click={() => (sizeMode = 'units')}>{asset}</button>
		</div>
	</div>
	{#if sizeMode === 'risk'}
		<div class={field}>
			<label class="text-[12px] text-sc-ink2" for="desk-ticket-risk">Risk</label>
			<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-risk" class={inputClass} type="number" min="0" step="0.05" bind:value={riskPct} on:input={() => (riskTouched = true)} /><span class="whitespace-nowrap text-[12px] text-sc-ink3">% of {mode === 'live' ? 'equity' : 'this book'}</span></div>
			<span class={hint}>
				{fmtUsd(((equity ?? 0) * (num(riskPct) ?? 0)) / 100)} {result.riskUsd !== null ? 'lost if the stop fills' : 'deployed per 1× leverage without a stop'}.
				{riskPerTrade ? `${strategyId}'s own orders risk ${fmtPct(riskPerTrade * 100, 0, false)} of ${mode === 'live' ? `its ${fmtUsd(sliceUsd)} slice` : 'its book'}.` : `Forven's default sizing risks 1% of ${mode === 'live' ? `a strategy's ${fmtUsd(sliceUsd)} slice` : 'the book'}.`}
			</span>
		</div>
	{:else if sizeMode === 'usd'}
		<div class={field}>
			<label class="text-[12px] text-sc-ink2" for="desk-ticket-usd">Order value</label>
			<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-usd" class={inputClass} type="number" min="0" step="10" bind:value={usd} /><span class="text-[12px] text-sc-ink3">USD</span></div>
		</div>
	{:else}
		<div class={field}>
			<label class="text-[12px] text-sc-ink2" for="desk-ticket-units">Size</label>
			<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-units" class={inputClass} type="number" min="0" step="any" bind:value={units} /><span class="text-[12px] text-sc-ink3">{asset}</span></div>
		</div>
	{/if}
	<div class={field}>
		<label class="text-[12px] text-sc-ink2" for="desk-ticket-stop">Stop{#if mode === 'live'} <span class="text-[#f2956f]">*</span>{/if}</label>
		<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-stop" class={inputClass} type="number" step="any" placeholder={mode === 'live' ? 'required' : 'optional'} bind:value={stop} /><span class="whitespace-nowrap text-[12px] text-sc-ink3">{result.stopPct !== null ? fmtPct(result.stopPct, 2, false) : ''}</span></div>
		{#if atr !== null && price !== null}
			<span class={hint}>Suggested {atrMultiplier}× ATR(14) = {fmtPx(atr * atrMultiplier)} away. <button type="button" class="underline underline-offset-2 hover:text-sc-ink" on:click={() => (stop = suggestedStop() ?? '')}>Use it</button></span>
		{/if}
	</div>
	<div class={field}>
		<label class="text-[12px] text-sc-ink2" for="desk-ticket-tp">Target</label>
		<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-tp" class={inputClass} type="number" step="any" placeholder="optional" bind:value={takeProfit} /><span class="whitespace-nowrap text-[12px] text-sc-ink3">{result.rr !== null ? `${result.rr.toFixed(2)}R` : ''}</span></div>
	</div>
	<div class={field}>
		<label class="text-[12px] text-sc-ink2" for="desk-ticket-lev">Leverage</label>
		<div class="flex min-w-0 items-center gap-1.5"><input id="desk-ticket-lev" class={inputClass} type="number" min="1" step={mode === 'live' ? '1' : '0.5'} bind:value={leverage} /><span class="whitespace-nowrap text-[12px] text-sc-ink3">×{mode === 'live' ? ' whole numbers' : ''}</span></div>
		{#if mode === 'live' && validatedLeverage % 1}
			<span class={hint}>{strategyId} was validated at {validatedLeverage}×; Hyperliquid only sets whole-number leverage.</span>
		{/if}
	</div>

	{#if result.errors.length}
		<ul class="grid gap-1">
			{#each result.errors as error}
				<li class="grid grid-cols-[16px_minmax(0,1fr)] gap-1.5 text-[12px] text-[#f0b3ae]"><span class="font-plex-mono font-semibold text-[#e5574f]">!</span><span>{error}</span></li>
			{/each}
		</ul>
	{:else}
		<div class="grid gap-1 rounded border border-sc-line bg-sc-bg px-2.5 py-2">
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-[12px]"><span class="text-sc-ink3">Size</span><span class="font-plex-mono text-sc-ink">{fmtQty(result.size)} {asset}</span></div>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-[12px]"><span class="text-sc-ink3">Order value</span><span class="font-plex-mono text-sc-ink">{fmtUsd(result.notional)}</span></div>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-[12px]"><span class="text-sc-ink3">Margin at {result.leverage}×</span><span class="font-plex-mono text-sc-ink">{fmtUsd(result.margin)}</span></div>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-[12px]"><span class="text-sc-ink3">Loss if the stop fills</span><span class="font-plex-mono text-[#f2956f]">{result.riskUsd !== null ? `${fmtUsd(-result.riskUsd)} · ${fmtPct(result.riskPctOfEquity, 2, false)} of ${mode === 'live' ? 'equity' : 'the book'}` : 'No stop'}</span></div>
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 text-[12px]"><span class="text-sc-ink3">Est. fee each way ({takerBps} bps taker)</span><span class="font-plex-mono text-sc-ink">{fmtUsd(result.feeUsd)}</span></div>
		</div>
		{#if flagged.length}
			<ul class="grid gap-1">
				{#each flagged as check}
					<li class={`grid grid-cols-[16px_minmax(0,1fr)] gap-1.5 text-[12px] ${check.tone === 'fail' ? 'text-[#f0b3ae]' : 'text-sc-ink2'}`}>
						<span class={`font-plex-mono font-semibold ${check.tone === 'fail' ? 'text-[#e5574f]' : 'text-[#e7b24a]'}`}>{check.tone === 'fail' ? '!' : '–'}</span><span>{check.text}</span>
					</li>
				{/each}
			</ul>
		{/if}
		{#if passing.length}
			<details class="text-[12px] text-sc-ink2">
				<summary class="w-fit cursor-pointer"><span class="font-plex-mono font-semibold text-[#3cc48f]">✓</span> {passing.length} check{passing.length === 1 ? '' : 's'} pass</summary>
				<ul class="mt-1.5 grid gap-1">
					{#each passing as check}
						<li class="grid grid-cols-[16px_minmax(0,1fr)] gap-1.5"><span class="font-plex-mono font-semibold text-[#3cc48f]">✓</span><span>{check.text}</span></li>
					{/each}
				</ul>
			</details>
		{/if}
	{/if}
	<button
		type="button"
		class={`w-full rounded-md border px-3 py-2 text-[13px] font-semibold disabled:cursor-not-allowed disabled:opacity-45 ${direction === 'long' ? 'border-[#139a9f]/60 bg-[#139a9f]/15 text-[#5ccac4]' : 'border-[#e0663f]/60 bg-[#e0663f]/15 text-[#f2956f]'}`}
		disabled={!canReview}
		on:click={review}
		data-testid="desk-ticket-review"
	>Review {direction} {result.size !== null ? fmtQty(result.size) : ''} {asset}</button>
	<p class="text-[11.5px] text-sc-ink3">
		{mode === 'live'
			? "Fills at Hyperliquid's price. The stop rests on the exchange as a reduce-only order; spread and depth are checked again at submit."
			: 'Fills at the current mid in the simulated book. No real order is sent.'}
	</p>
</div>
