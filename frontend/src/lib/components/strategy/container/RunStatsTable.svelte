<script lang="ts">
	import type { Slice, TradeStats } from '$lib/utils/strategyContainer/metrics';
	import { tradesPerMonth } from '$lib/utils/strategyContainer/metrics';
	import { fmtFraction, fmtNum, fmtUsd, isNum, signClass } from '$lib/utils/strategyContainer/format';

	/** In-sample against out-of-sample for one run; trade-level figures come from the stored OOS trades. */
	export let inSample: Slice | null = null;
	export let outOfSample: Slice | null = null;
	export let stats: TradeStats | null = null;

	type Cell = { text: string; cls?: string };
	type Row = { label: string; is: Cell; oos: Cell } | { group: string };
	const none: Cell = { text: '—', cls: 'text-sc-ink4' };
	const notStored: Cell = { text: 'not stored', cls: 'text-sc-ink4' };
	const frac = (value: number | null | undefined, digits = 1, signed = true): Cell =>
		isNum(value) ? { text: fmtFraction(value, digits, signed), cls: signed ? signClass(value) : '' } : none;
	const num = (value: number | null | undefined, digits = 2): Cell => (isNum(value) ? { text: fmtNum(value, digits) } : none);
	const pf = (value: number | null | undefined): Cell => (value === Number.POSITIVE_INFINITY ? { text: '∞' } : num(value));
	const calmar = (slice: Slice | null) => (slice && isNum(slice.cagr) && isNum(slice.maxDrawdown) && slice.maxDrawdown > 0 ? slice.cagr / slice.maxDrawdown : null);

	$: rows = [
		{ group: 'Returns' },
		{ label: 'Total return', is: frac(inSample?.totalReturn), oos: frac(outOfSample?.totalReturn) },
		{ label: 'CAGR', is: frac(inSample?.cagr), oos: frac(outOfSample?.cagr) },
		{ label: 'Average month', is: frac(inSample?.monthlyReturn, 2), oos: frac(outOfSample?.monthlyReturn, 2) },
		{ label: 'Average trade', is: frac(inSample?.avgTrade, 2), oos: frac(outOfSample?.avgTrade, 2) },
		{ group: 'Risk' },
		{ label: 'Sharpe', is: num(inSample?.sharpe), oos: num(outOfSample?.sharpe) },
		{ label: 'Sortino', is: num(inSample?.sortino), oos: num(outOfSample?.sortino) },
		{ label: 'Max drawdown', is: frac(inSample?.maxDrawdown, 1, false), oos: frac(outOfSample?.maxDrawdown, 1, false) },
		{ label: 'Calmar (CAGR ÷ max DD)', is: num(calmar(inSample)), oos: num(calmar(outOfSample)) },
		{ group: 'Trades' },
		{ label: 'Trades', is: isNum(inSample?.trades) ? { text: String(Math.round(inSample?.trades ?? 0)) } : none, oos: isNum(outOfSample?.trades) ? { text: String(Math.round(outOfSample?.trades ?? 0)) } : none },
		{ label: 'Trades a month', is: num(tradesPerMonth(inSample), 1), oos: num(tradesPerMonth(outOfSample), 1) },
		{ label: 'Win rate', is: frac(inSample?.winRate, 1, false), oos: frac(outOfSample?.winRate, 1, false) },
		{ label: 'Profit factor', is: pf(inSample?.profitFactor), oos: pf(outOfSample?.profitFactor) },
		{ label: 'Payoff (avg win ÷ avg loss)', is: notStored, oos: num(stats?.payoff) },
		{ label: 'Expectancy per trade', is: notStored, oos: isNum(stats?.expectancy) ? { text: fmtUsd(stats?.expectancy), cls: signClass(stats?.expectancy) } : none },
		{ label: 'Largest win / loss', is: notStored, oos: stats ? { text: `${fmtUsd(stats.largestWin, 0)} / ${fmtUsd(stats.largestLoss, 0)}` } : none },
		{ label: 'Longest losing streak', is: notStored, oos: stats ? { text: `${stats.longestLossStreak} trades` } : none },
		{ label: 'Average hold', is: isNum(inSample?.avgBars) ? { text: `${fmtNum(inSample?.avgBars, 1)} bars` } : none, oos: isNum(outOfSample?.avgBars) ? { text: `${fmtNum(outOfSample?.avgBars, 1)} bars` } : none },
	] as Row[];
</script>

<div class="overflow-x-auto" data-testid="run-stats">
	<table class="w-full border-collapse text-[12px] font-plex-mono tabular-nums">
		<thead>
			<tr class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3">
				<th class="px-2.5 py-1.5 text-left font-normal">Metric</th>
				<th class="px-2.5 py-1.5 text-right font-normal">In-sample</th>
				<th class="px-2.5 py-1.5 text-right font-normal">Out-of-sample</th>
			</tr>
		</thead>
		<tbody>
			{#each rows as row, index (index)}
				{#if 'group' in row}
					<tr><td colspan="3" class="border-t border-sc-line px-2.5 pb-1 pt-3 text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{row.group}</td></tr>
				{:else}
					<tr class="border-t border-sc-line">
						<td class="px-2.5 py-1.5 text-left font-plex text-[13px] text-sc-ink2">{row.label}</td>
						<td class={`px-2.5 py-1.5 text-right ${row.is.cls ?? 'text-sc-ink'}`}>{row.is.text}</td>
						<td class={`px-2.5 py-1.5 text-right ${row.oos.cls ?? 'text-sc-ink'}`}>{row.oos.text}</td>
					</tr>
				{/if}
			{/each}
		</tbody>
	</table>
	<p class="m-0 mt-2 text-[12px] text-sc-ink3">Trade-level rows use this run's stored trades, which are out-of-sample only; the engine does not keep in-sample trades.</p>
</div>
