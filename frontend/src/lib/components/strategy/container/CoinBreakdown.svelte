<script lang="ts">
	// One row per coin of a multi-coin paper container (backend forven.multi_coin):
	// the same rule and params on every coin, each coin sized on 1/N of the paper book.
	import type { CoinForward, MultiCoinInfo } from '$lib/api/multiCoin';
	import { fmtDateUtc, fmtUsd, signClass } from '$lib/utils/strategyContainer/format';
	import { ago } from '$lib/utils/forge/time';

	export let info: MultiCoinInfo;
	/** Rows to show; defaults to the container payload's own breakdown. */
	export let rows: CoinForward[] | null = null;
	/** Paper summary rows carry an unrealized figure; the container payload does not. */
	export let showUnrealized = false;
	export let compact = false;

	$: coinRows = (rows ?? info.by_coin).length
		? (rows ?? info.by_coin)
		: info.coins.map((coin) => ({
				coin,
				home: coin === info.home,
				in_container: true,
				closed_count: 0,
				wins: 0,
				win_rate_pct: null,
				realized_pnl_usd: 0,
				open_count: 0,
				open_sides: [],
				unrealized_pnl_usd: 0,
				last_closed_at: null,
			}));
	$: traded = coinRows.filter((row) => row.closed_count > 0 || row.open_count > 0).length;
	$: sharePct = Math.round(info.weight * 1000) / 10;

	function nowLabel(row: CoinForward): string {
		if (row.open_count === 0) return '—';
		const sides = row.open_sides.length ? row.open_sides.join(' + ') : 'open';
		return row.open_count > 1 ? `${row.open_count} ${sides}` : sides;
	}
</script>

<div class="grid gap-2" data-testid="coin-breakdown">
	<p class="m-0 text-[11px] leading-relaxed text-sc-ink3">
		Paper-trades the same rule on {info.coins.length} coins under one strategy, each on {sharePct}% of the paper book.
		{#if info.frozen_at}The list was frozen {fmtDateUtc(info.frozen_at)} by the breadth test and does not change.{/if}
		{#if !compact}{traded} of {info.coins.length} have traded so far.{/if}
	</p>
	{#if !info.trading}
		<p class="m-0 text-[11px] text-[#e7b24a]">Multi-coin paper is switched off in Settings, so only {info.home} trades now. The list is kept.</p>
	{/if}
	<div class="overflow-x-auto">
		<table class="w-full text-[12px]">
			<thead>
				<tr class="text-left font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">
					<th class="py-1 pr-2 font-medium">Coin</th>
					<th class="py-1 pr-2 font-medium">Now</th>
					<th class="py-1 pr-2 text-right font-medium">Closed</th>
					<th class="py-1 pr-2 text-right font-medium">Win</th>
					<th class="py-1 pr-2 text-right font-medium">Realized</th>
					{#if showUnrealized}<th class="py-1 pr-2 text-right font-medium">Open P&amp;L</th>{/if}
					{#if !compact}<th class="py-1 text-right font-medium">Last close</th>{/if}
				</tr>
			</thead>
			<tbody>
				{#each coinRows as row (row.coin)}
					<tr class="border-t border-sc-line" data-coin={row.coin}>
						<td class="py-1 pr-2 font-plex-mono text-sc-ink">
							{row.coin}
							{#if row.home}<span class="ml-1 font-plex-cond text-[10px] uppercase tracking-[0.06em] text-sc-ink4" title="The strategy's own coin">home</span>{/if}
							{#if !row.in_container}<span class="ml-1 font-plex-cond text-[10px] uppercase tracking-[0.06em] text-sc-ink4" title="Traded, but not on the frozen list">off list</span>{/if}
						</td>
						<td class={`py-1 pr-2 ${row.open_count ? 'text-sc-ink' : 'text-sc-ink4'}`}>{nowLabel(row)}</td>
						<td class="py-1 pr-2 text-right font-plex-mono text-sc-ink2">{row.closed_count}</td>
						<td class="py-1 pr-2 text-right font-plex-mono text-sc-ink2">{row.win_rate_pct === null ? '—' : `${Math.round(row.win_rate_pct)}%`}</td>
						<td class={`py-1 pr-2 text-right font-plex-mono ${row.closed_count ? signClass(row.realized_pnl_usd) || 'text-sc-ink2' : 'text-sc-ink4'}`}>{row.closed_count ? fmtUsd(row.realized_pnl_usd, 0) : '—'}</td>
						{#if showUnrealized}
							<td class={`py-1 pr-2 text-right font-plex-mono ${row.open_count ? signClass(row.unrealized_pnl_usd) || 'text-sc-ink2' : 'text-sc-ink4'}`}>{row.open_count ? fmtUsd(row.unrealized_pnl_usd, 0) : '—'}</td>
						{/if}
						{#if !compact}
							<td class="py-1 text-right text-[11px] text-sc-ink3" title={row.last_closed_at ?? ''}>{row.last_closed_at ? ago(row.last_closed_at) : '—'}</td>
						{/if}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</div>
