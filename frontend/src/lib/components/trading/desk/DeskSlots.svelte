<script lang="ts">
	/**
	 * The live desk's slot strip: how many live positions the account can carry at
	 * once, how many are taken, and what each coin adds up to on the exchange.
	 * One slot is one full-size position; a probation trade takes its share of one.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getLiveNetting, type NettingCoin, type NettingStatus, type NettingTrade } from '$lib/api/liveNetting';
	import { fmtPx, fmtQty, fmtUsd } from '$lib/utils/tradingDesk/format';

	/** Drawn as separate boxes up to this many slots; a plain meter above it. */
	const MAX_BOXES = 40;

	let status: NettingStatus | null = null;
	let failed = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	const label = 'font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3';

	async function load() {
		try {
			status = await getLiveNetting();
			failed = false;
		} catch {
			failed = true;
		}
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), 20000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	type Segment = { trade: NettingTrade; coin: string; share: number };

	function share(trade: NettingTrade): number {
		const value = Number(trade.slot_share);
		return Number.isFinite(value) && value > 0 ? Math.min(value, 1) : 1;
	}

	function trim(value: number): string {
		return Number(value.toFixed(2)).toString();
	}

	function coinState(coin: NettingCoin): { text: string; tone: string } {
		if (coin.mismatch?.confirmed) return { text: 'entries paused', tone: 'border-red-900 text-red-400' };
		if (coin.mismatch) return { text: 'checking', tone: 'border-yellow-900 text-yellow-500' };
		if (coin.disaster_stop?.error) return { text: 'disaster stop not set', tone: 'border-yellow-900 text-yellow-500' };
		return { text: '', tone: 'border-sc-line text-sc-ink2' };
	}

	function netText(coin: NettingCoin): string {
		const net = coin.book_units;
		if (Math.abs(net) < 1e-12) return 'net flat';
		return `net ${net > 0 ? 'long' : 'short'} ${fmtQty(Math.abs(net))}`;
	}

	$: slots = status?.slots ?? null;
	$: total = slots?.slots ?? 0;
	$: used = slots?.in_use ?? 0;
	$: free = Math.max(total - used, 0);
	$: coins = (status?.coins ?? []).filter((coin) => coin.trades.length || coin.mismatch);
	$: segments = coins.flatMap((coin) => coin.trades.map((trade): Segment => ({ trade, coin: coin.coin, share: share(trade) })));
	$: full = total > 0 && free < 0.25;
	$: paused = coins.filter((coin) => coin.mismatch?.confirmed);
</script>

{#if status}
	<section class="grid gap-2 rounded-md border border-sc-line bg-sc-panel px-3.5 py-2.5" aria-label="Live slots" data-testid="desk-slots">
		{#if !status.active}
			<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
				<span class={label}>Slots</span>
				<span class="text-sc-ink2">
					{#if status.books_on}
						Shared coins are off while direction books are on: one live strategy per coin.
					{:else if status.enabled}
						Shared coins are not running in this mode.
					{:else}
						Shared coins are off: one live strategy can hold a coin at a time and other entries on it are refused.
					{/if}
				</span>
				<a href="/risk" class="text-[11px] text-sc-ink3 underline hover:text-sc-ink">Open Risk</a>
			</div>
		{:else}
			<div class="flex flex-wrap items-baseline gap-x-5 gap-y-1">
				<span class={label}>Slots</span>
				{#if slots}
					<span class="whitespace-nowrap text-[20px] font-medium tracking-tight text-sc-ink" data-testid="desk-slots-count">
						{trim(free)}<small class="ml-1 text-[12px] font-normal text-sc-ink3">free of {total}</small>
					</span>
					<span class="text-[12px] text-sc-ink3">In use <b class="font-medium text-sc-ink2">{trim(used)}</b></span>
					<span class="text-[12px] text-sc-ink3">One slot <b class="font-medium text-sc-ink2">{fmtUsd(slots.slot_usd)}</b></span>
					<span class="text-[12px] text-sc-ink3">
						{#if slots.override}
							Set by you · Forven would use <b class="font-medium text-sc-ink2">{slots.slots_calculated ?? '—'}</b>
						{:else}
							History asks for <b class="font-medium text-sc-ink2">{slots.slots_wanted ?? '—'}</b> · balance can carry
							<b class={`font-medium ${slots.capital_bound ? 'text-yellow-500' : 'text-sc-ink2'}`}>{slots.slots_capital_cap ?? '—'}</b>
						{/if}
					</span>
					{#if full}<span class="text-[12px] text-yellow-500">Full: a new full-size entry waits for a slot.</span>{/if}
				{:else}
					<span class="text-[12px] text-sc-ink3">Slots appear once the account balance has synced.</span>
				{/if}
				<a href="/risk" class="ml-auto text-[11px] text-sc-ink3 underline hover:text-sc-ink">Settings</a>
			</div>

			{#if slots && total > 0}
				<div class="relative h-6 overflow-hidden rounded border border-sc-line2 bg-sc-bg" role="img" aria-label={`${trim(used)} of ${total} slots in use`} data-testid="desk-slots-bar">
					<div class="absolute inset-0 flex">
						{#each segments as seg (seg.trade.trade_id)}
							<span
								class={`flex h-full min-w-[3px] items-center justify-center overflow-hidden border-r border-sc-bg px-1 text-[10.5px] font-medium ${seg.trade.side > 0 ? 'bg-emerald-500/30 text-emerald-200' : 'bg-red-500/30 text-red-200'}`}
								style={`flex: 0 0 ${Math.min((seg.share / total) * 100, 100)}%`}
								title={`${seg.trade.strategy_id} · ${seg.coin} ${seg.trade.side > 0 ? 'long' : 'short'} ${fmtQty(seg.trade.size)} · ${seg.share >= 1 ? 'one slot' : `${trim(seg.share * 100)}% of a slot (probation)`}`}
							>
								<span class="truncate">{seg.coin} {seg.trade.side > 0 ? 'L' : 'S'}</span>
							</span>
						{/each}
					</div>
					{#if total <= MAX_BOXES}
						<div class="pointer-events-none absolute inset-0 grid" style={`grid-template-columns: repeat(${total}, minmax(0, 1fr))`}>
							{#each Array(total) as _, index (index)}
								<span class={index < total - 1 ? 'border-r border-sc-line2' : ''}></span>
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			{#if coins.length}
				<div class="flex flex-wrap gap-1.5 text-[11.5px]">
					{#each coins as coin (coin.coin)}
						{@const state = coinState(coin)}
						<span class={`flex items-center gap-1.5 rounded border px-2 py-0.5 ${state.tone}`} data-testid="desk-slots-coin">
							<b class="font-semibold text-sc-ink">{coin.coin}</b>
							<span>{netText(coin)}</span>
							<span class="text-sc-ink3">
								{coin.trades.filter((t) => t.side > 0).length} long, {coin.trades.filter((t) => t.side < 0).length} short
							</span>
							{#if coin.disaster_stop && !coin.disaster_stop.error}
								<span class="text-sc-ink3">disaster stop {fmtPx(coin.disaster_stop.price)}</span>
							{/if}
							{#if state.text}<span class="font-medium">{state.text}</span>{/if}
						</span>
					{/each}
				</div>
			{:else if slots}
				<span class="text-[12px] text-sc-ink3">No live position open. Any number of live strategies can trade the same coin; each takes a slot while it is in a trade.</span>
			{/if}
			{#if paused.length}
				<span class="text-[12px] text-red-400">
					{paused.map((coin) => coin.coin).join(', ')}: Forven's trades and the exchange position differ, so new entries there are paused.
					<a href="/risk" class="underline">Resolve on the Risk page</a>.
				</span>
			{/if}
		{/if}
	</section>
{:else if failed}
	<section class="rounded-md border border-sc-line bg-sc-panel px-3.5 py-2 text-[12px] text-sc-ink3" data-testid="desk-slots">
		Slots could not be read. Retrying.
	</section>
{/if}
