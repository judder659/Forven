<script lang="ts">
	/** One strategy's whole result since inception: the headline, the breakdown and its own curve. */
	import type { DeskMode } from '$lib/api/desk';
	import { dur, fmtDay, fmtPct, fmtUsd, num, toneClass } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';
	import DeskEquityChart from './DeskEquityChart.svelte';

	export let mode: DeskMode;
	export let row: DeskRow;

	$: perf = row.perf;
	$: stats = row.stats;
	$: bt = row.fleet?.backtest_oos ?? null;
	$: btWin = num(bt?.win_rate);
	$: btPf = num(bt?.profit_factor);
	$: btDd = num(bt?.max_drawdown_pct);
	$: history = { base: perf.base, curve: perf.curve };
	$: items = [
		perf.balance !== null
			? { label: 'Book balance', value: fmtUsd(perf.balance), note: `started at ${fmtUsd(perf.capital, { digits: 0 })}`, tone: '' }
			: { label: 'Capital slice', value: perf.capital !== null ? fmtUsd(perf.capital) : '—', note: 'today, sizes each trade', tone: '' },
		{ label: 'Closed trades', value: fmtUsd(perf.realized, { signed: true }), note: 'net of fees and funding', tone: toneClass(perf.realized) },
		{ label: 'Open P&L', value: row.legs.length ? fmtUsd(perf.open, { signed: true }) : 'Flat', note: row.legs.length ? 'at the live mark' : '', tone: row.legs.length ? toneClass(perf.open) : '' },
		...(Math.abs(perf.openCosts) >= 0.005
			? [{ label: 'Open-leg costs', value: fmtUsd(perf.openCosts, { signed: true }), note: 'fees and funding already charged', tone: toneClass(perf.openCosts) }]
			: []),
		{
			label: 'Trades',
			value: String(perf.trades),
			note: perf.trades ? `${perf.wins} won · ${perf.losses} lost${stats.failed ? ` · ${stats.failed} failed` : ''}` : stats.failed ? `${stats.failed} failed` : 'none closed yet',
			tone: '',
		},
		{ label: 'Win rate', value: perf.winRate !== null ? fmtPct(perf.winRate * 100, 0, false) : '—', note: btWin !== null ? `backtest ${fmtPct(btWin * 100, 0, false)}` : '', tone: '' },
		{ label: 'Profit factor', value: stats.profitFactor !== null ? stats.profitFactor.toFixed(2) : '—', note: btPf !== null ? `backtest ${btPf.toFixed(2)}` : '', tone: '' },
		{ label: 'Avg trade', value: fmtPct(stats.avgPctOfCapital, 2), note: 'of the capital it used', tone: toneClass(stats.avgPctOfCapital) },
		{
			label: 'Max drawdown',
			value: perf.maxDrawdown !== null ? fmtUsd(perf.maxDrawdown) : '—',
			note: [perf.maxDrawdownPct !== null ? `${fmtPct(perf.maxDrawdownPct, 1, false)} of ${mode === 'paper' ? 'the peak book' : 'the average slice'}` : '', btDd !== null ? `backtest ${fmtPct(btDd * 100, 1, false)}` : ''].filter(Boolean).join(' · '),
			tone: '',
		},
		{ label: 'Best · worst', value: stats.n ? `${fmtUsd(stats.best, { signed: true })} · ${fmtUsd(stats.worst, { signed: true })}` : '—', note: '', tone: '' },
		{ label: 'Costs paid', value: fmtUsd(stats.costs), note: 'on closed trades', tone: '' },
		{ label: 'Avg hold', value: stats.avgHoldHours !== null ? dur(stats.avgHoldHours * 3_600_000) : '—', note: '', tone: '' },
	];
</script>

<!-- The panel is full width under the chart below 2xl and a narrow right column at 2xl. -->
<div class="grid gap-3 lg:grid-cols-2 lg:gap-5 2xl:grid-cols-1 2xl:gap-3" data-testid="desk-strategy-performance">
	<div class="grid min-w-0 content-start gap-3">
		<div class="grid gap-0.5">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
				{perf.since !== null ? `Since ${fmtDay(perf.since)}` : 'Since inception'} · {mode === 'paper' ? 'paper' : 'live'}
			</span>
			<div class="flex flex-wrap items-baseline gap-x-2">
				<b class={`font-plex-mono text-[22px] font-semibold tabular-nums ${toneClass(perf.total)}`} data-testid="desk-performance-total">{fmtUsd(perf.total, { signed: true })}</b>
				{#if perf.returnPct !== null}<span class={`font-plex-mono text-[13px] tabular-nums ${toneClass(perf.total)}`}>{fmtPct(perf.returnPct, 2)}</span>{/if}
			</div>
			<span class="text-[12px] text-sc-ink3">
				{perf.returnPct !== null ? `Everything since inception, closed and open, ${perf.returnBasis}.` : 'Everything since inception, closed and open. No capital slice is known yet, so there is no return figure.'}
			</span>
		</div>

		<dl class="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3 2xl:grid-cols-2">
			{#each items as item (item.label)}
				<div class="grid min-w-0 content-start">
					<dt class="text-[11.5px] text-sc-ink3">{item.label}</dt>
					<dd class={`font-plex-mono text-[13px] tabular-nums ${item.tone || 'text-sc-ink'}`}>{item.value}</dd>
					{#if item.note}<dd class="truncate text-[11px] text-sc-ink4" title={item.note}>{item.note}</dd>{/if}
				</div>
			{/each}
		</dl>
	</div>

	<div class="grid min-w-0 content-start gap-1">
		<h4 class="text-[12.5px] font-semibold text-sc-ink">{mode === 'paper' ? 'Book balance since inception' : 'P&L since going live'}</h4>
		<DeskEquityChart
			{history}
			label={`${row.sid} ${mode === 'paper' ? 'book balance' : 'cumulative live P&L'} after each closed trade, ending at now`}
			emptyText="The curve starts at inception; no history is recorded yet."
			startTitle={mode === 'paper' ? 'Start' : '$0'}
		/>
		<p class="text-[11.5px] text-sc-ink3">One point per closed trade; the last point is now, open P&L included.</p>
	</div>
</div>
