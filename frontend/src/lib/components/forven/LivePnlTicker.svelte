<script lang="ts">
	/**
	 * Global header P&L ticker — unrealized $ P&L across every OPEN trade,
	 * marked to the live WS tick price so it moves in realtime on every page.
	 * Split into LIVE (real exposure) and PAPER chips, each linking to its page.
	 *
	 * Open trades come from the system heartbeat (forvenOpenTrades, WS-debounced
	 * refresh); the marks come from the tick-level forvenLivePrices feed, so the
	 * number updates on every price tick — a position bleeding out is visible
	 * from anywhere in the app without opening a trades page.
	 */
	import { forvenOpenTrades } from '$lib/stores/forven';
	import { forvenLivePrices } from '$lib/stores/forvenWebSocket';
	import { liveUnrealizedUsd } from '$lib/utils/livePnl';
	import type { ForvenTrade } from '$lib/api';

	interface PnlBucket {
		pnl: number;
		positions: number;
		priced: number; // positions with a live mark available right now
	}

	function bucketPnl(trades: ForvenTrade[], prices: Record<string, number>, live: boolean): PnlBucket {
		const bucket: PnlBucket = { pnl: 0, positions: 0, priced: 0 };
		for (const trade of trades) {
			const isLive = String(trade.execution_type ?? '').toLowerCase() === 'live';
			if (isLive !== live) continue;
			bucket.positions += 1;
			const usd = liveUnrealizedUsd(trade, prices);
			if (usd === null) continue;
			bucket.priced += 1;
			bucket.pnl += usd;
		}
		return bucket;
	}

	$: liveBucket = bucketPnl($forvenOpenTrades, $forvenLivePrices, true);
	$: paperBucket = bucketPnl($forvenOpenTrades, $forvenLivePrices, false);

	function fmtUsd(value: number): string {
		const abs = Math.abs(value).toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
		return `${value < 0 ? '-' : '+'}$${abs}`;
	}

	function chipLabel(bucket: PnlBucket): string {
		if (bucket.positions === 0) return '—';
		if (bucket.priced === 0) return '…';
		return fmtUsd(bucket.pnl);
	}

	function chipClass(bucket: PnlBucket): string {
		if (bucket.positions === 0 || bucket.priced === 0) return 'border-[#333] text-[#666]';
		if (bucket.pnl > 0) return 'border-emerald-900 text-emerald-400';
		if (bucket.pnl < 0) return 'border-red-900 text-red-400';
		return 'border-[#333] text-[#888]';
	}

	function chipTitle(bucket: PnlBucket, label: string): string {
		if (bucket.positions === 0) return `No open ${label} positions`;
		const scope = `${bucket.positions} open ${label} position${bucket.positions === 1 ? '' : 's'}`;
		if (bucket.priced === 0) return `${scope} — waiting for live prices`;
		const partial = bucket.priced < bucket.positions ? ` (${bucket.priced}/${bucket.positions} priced)` : '';
		return `Unrealized P&L across ${scope}${partial}, marked to the live tick price`;
	}
</script>

<a
	href="/live-trades"
	data-sveltekit-preload-data="hover"
	class={`px-2 py-1 border whitespace-nowrap font-bold transition-colors hover:bg-[#111] ${chipClass(liveBucket)}`}
	title={chipTitle(liveBucket, 'LIVE')}
>
	LIVE {chipLabel(liveBucket)}
</a>
<a
	href="/paper-trades"
	data-sveltekit-preload-data="hover"
	class={`px-2 py-1 border whitespace-nowrap transition-colors hover:bg-[#111] ${chipClass(paperBucket)}`}
	title={chipTitle(paperBucket, 'paper')}
>
	SIM {chipLabel(paperBucket)}
</a>
