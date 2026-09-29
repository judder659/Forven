<script lang="ts">
	/** Forward results against the out-of-sample backtest, with a sample-size-aware verdict. */
	import type { DeskMode } from '$lib/api/desk';
	import { dur, fmtPct, fmtUsd, num, timeframeMs } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';

	export let mode: DeskMode;
	export let row: DeskRow;

	$: exp = row.expectation;
	$: stats = row.stats;
	$: bt = exp?.backtest ?? null;
	$: liveRate = exp && exp.months ? stats.n / exp.months : null;
	$: holdBars = stats.avgHoldHours !== null ? (stats.avgHoldHours * 3_600_000) / timeframeMs(row.timeframe) : null;
	$: pin = exp?.odds !== null && exp?.odds !== undefined ? Math.max(0, Math.min(100, exp.odds * 100)) : null;
	$: noun = mode === 'live' ? 'Live' : 'Paper';

	const PILL: Record<string, string> = {
		ok: 'border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]',
		caution: 'border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]',
		fail: 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#e5574f]',
		idle: 'border-sc-line2 text-sc-ink2',
	};
</script>

{#if !exp || !bt}
	<div class="grid gap-1 py-6 text-center text-[12.5px] text-sc-ink3">
		<b class="font-medium text-sc-ink2">No backtest to compare against</b>
		This strategy has no stored out-of-sample result.
	</div>
{:else}
	<div class="grid gap-3" data-testid="desk-expectation">
		<span class={`w-fit rounded-full border px-2.5 py-0.5 text-[12px] font-semibold ${PILL[exp.pill.tone]}`}>{exp.pill.text}</span>
		<p class="text-[13px] leading-relaxed text-sc-ink">{exp.verdict}</p>
		{#if pin !== null}
			<div class="grid gap-1">
				<div class="relative h-2 rounded-full bg-[linear-gradient(90deg,rgba(229,87,79,0.55)_0%,rgba(231,178,74,0.45)_10%,#2a2f38_25%,#2a2f38_100%)]">
					<i class="absolute -top-1 h-4 w-0.5 bg-sc-ink" style={`left:${pin}%`}></i>
				</div>
				<div class="flex justify-between font-plex-mono text-[10.5px] text-sc-ink3"><span>0%</span><span>10%</span><span>50%</span><span>100%</span></div>
				<span class="text-[12px] text-sc-ink3">{exp.oddsText}</span>
			</div>
		{/if}
		<table class="w-full border-collapse text-[12px]">
			<thead>
				<tr class="border-b border-sc-line font-plex-cond text-[11px] uppercase tracking-[0.06em] text-sc-ink3">
					<th class="py-1.5 text-left font-medium"></th><th class="py-1.5 text-right font-medium">Backtest OOS</th><th class="py-1.5 text-right font-medium">{noun}</th>
				</tr>
			</thead>
			<tbody class="font-plex-mono tabular-nums">
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Trades</td><td class="text-right">{bt.total_trades} in {num(bt.backtest_months)?.toFixed(1)} mo</td><td class="text-right">{stats.n} in {exp.months !== null ? exp.months.toFixed(1) : '—'} mo</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Per month</td><td class="text-right">{exp.rate.toFixed(1)}</td><td class="text-right">{liveRate !== null ? liveRate.toFixed(1) : '—'}</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Win rate</td><td class="text-right">{fmtPct((num(bt.win_rate) ?? 0) * 100, 1, false)}</td><td class="text-right">{stats.n ? fmtPct((stats.wins / stats.n) * 100, 1, false) : '—'}</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Profit factor</td><td class="text-right">{num(bt.profit_factor)?.toFixed(2) ?? '—'}</td><td class="text-right">{stats.profitFactor !== null ? stats.profitFactor.toFixed(2) : '—'}</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Avg trade, % of capital</td><td class="text-right">{fmtPct((num(bt.avg_trade_pct) ?? 0) * 100, 2)}</td><td class="text-right">{fmtPct(stats.avgPctOfCapital, 2)}</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Avg hold</td><td class="text-right">{num(bt.avg_bars_held) ?? '—'} bars</td><td class="text-right">{holdBars !== null ? `${holdBars.toFixed(1)} bars` : '—'}</td></tr>
				<tr class="border-b border-sc-line"><td class="py-1.5 font-sans text-sc-ink2">Max drawdown</td><td class="text-right">{fmtPct((num(bt.max_drawdown_pct) ?? 0) * 100, 1, false)}</td><td class="text-right">{fmtPct(row.perf.maxDrawdownPct, 1, false)}</td></tr>
				<tr><td class="py-1.5 font-sans text-sc-ink2">Return</td><td class="text-right">{fmtPct((num(bt.total_return_pct) ?? 0) * 100, 1)}</td><td class="text-right" title={`${fmtUsd(row.perf.total, { signed: true })} since inception, open P&L included`}>{fmtPct(row.perf.returnPct, 1)}</td></tr>
			</tbody>
		</table>
		<p class="text-[12px] text-sc-ink3">
			{mode === 'live' ? 'Live capital per trade is the strategy’s slice of the account' : 'Paper trades size off the strategy’s own book'}, so "% of capital" compares like with like. Win rate is the steadiest comparison at small samples; profit factor swings on a single trade.{stats.failed ? ` ${stats.failed} failed entr${stats.failed === 1 ? 'y is' : 'ies are'} left out.` : ''}
			{#if stats.avgHoldHours !== null}{` Average hold ${dur(stats.avgHoldHours * 3_600_000)}.`}{/if}
			{' The backtest return covers its whole window; the forward return covers the time since inception.'}
		</p>
		<p class="font-plex-mono text-[10.5px] text-sc-ink4">Backtest window {bt.start_date?.slice(0, 10) ?? '—'} → {bt.end_date?.slice(0, 10) ?? '—'}</p>
	</div>
{/if}
