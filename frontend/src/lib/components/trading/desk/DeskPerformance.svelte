<script lang="ts">
	/** Per-strategy results, equity (live), realized per day and how trades closed. */
	import type { ForvenEquityHistory } from '$lib/api';
	import type { DeskFill, DeskMode } from '$lib/api/desk';
	import { describeClose } from '$lib/utils/tradingDesk/describe';
	import { dur, fmtDay, fmtPct, fmtUsd, num, toneClass } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';
	import DeskDailyBars from './DeskDailyBars.svelte';
	import DeskEquityChart from './DeskEquityChart.svelte';

	export let mode: DeskMode;
	export let rows: DeskRow[] = [];
	export let fills: DeskFill[] = [];
	export let equity: ForvenEquityHistory | null = null;
	export let selectedSid: string | null = null;
	export let scopeLabel = '';
	export let now = Date.now();

	$: closeMix = (() => {
		const counts = new Map<string, { count: number; net: number; tone: string }>();
		for (const fill of fills) {
			if (String(fill.status).toUpperCase() !== 'CLOSED') continue;
			const why = describeClose({ status: 'CLOSED', close_reason: fill.close_reason, exit_price: fill.exit_price, stop_price: fill.stop_price });
			const entry = counts.get(why.text) ?? { count: 0, net: 0, tone: why.tone };
			entry.count += 1;
			entry.net += num(fill.net_pnl_usd) ?? 0;
			counts.set(why.text, entry);
		}
		return [...counts.entries()].sort((a, b) => b[1].count - a[1].count);
	})();
	$: closedTotal = closeMix.reduce((sum, [, entry]) => sum + entry.count, 0);

	const th = 'sticky top-0 whitespace-nowrap bg-sc-panel px-2.5 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3';
	const td = 'whitespace-nowrap border-b border-sc-line px-2.5 py-1.5';
</script>

<div class="grid xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]" data-testid="desk-performance">
	<div class="grid min-w-0 content-start gap-2 border-sc-line p-3 xl:border-r">
		<h3 class="text-[13px] font-semibold text-sc-ink">By strategy</h3>
		<div class="overflow-x-auto">
			<table class="w-full border-collapse text-[12px]">
				<thead>
					<tr>
						<th class={`${th} text-left`}>Strategy</th><th class={`${th} text-left`}>Market</th><th class={`${th} text-right`}>Since</th>
						<th class={`${th} text-right`}>Trades</th><th class={`${th} text-right`}>Win rate</th><th class={`${th} text-right`}>Backtest win</th>
						<th class={`${th} text-right`}>Net P&amp;L</th><th class={`${th} text-right`}>Profit factor</th><th class={`${th} text-right`}>Avg, % of capital</th>
						<th class={`${th} text-right`}>Costs</th><th class={`${th} text-right`}>Best</th><th class={`${th} text-right`}>Worst</th><th class={`${th} text-right`}>Avg hold</th>
					</tr>
				</thead>
				<tbody class="font-plex-mono tabular-nums">
					{#each rows as row (row.session.id)}
						{@const stats = row.stats}
						{@const bt = row.fleet?.backtest_oos}
						<tr class={`hover:bg-sc-hover ${row.sid === selectedSid ? 'bg-[#0f1319]' : ''}`}>
							<td class={`${td} text-left font-sans`}>{row.sid}</td>
							<td class={`${td} text-left font-sans`}>{row.asset} {row.timeframe}</td>
							<td class={`${td} text-right`}>{fmtDay(row.fleet?.live_since ?? row.session.started_at)}</td>
							<td class={`${td} text-right`}>{stats.n}{#if stats.failed}<span class="text-sc-ink3"> +{stats.failed} failed</span>{/if}</td>
							<td class={`${td} text-right`}>{stats.n ? fmtPct((stats.wins / stats.n) * 100, 0, false) : '—'}</td>
							<td class={`${td} text-right text-sc-ink3`}>{bt?.win_rate !== null && bt?.win_rate !== undefined ? fmtPct(bt.win_rate * 100, 0, false) : '—'}</td>
							<td class={`${td} text-right ${toneClass(stats.net)}`}>{fmtUsd(stats.net, { signed: true })}</td>
							<td class={`${td} text-right`}>{stats.profitFactor !== null ? stats.profitFactor.toFixed(2) : '—'}</td>
							<td class={`${td} text-right ${toneClass(stats.avgPctOfCapital)}`}>{fmtPct(stats.avgPctOfCapital, 2)}</td>
							<td class={`${td} text-right`}>{fmtUsd(stats.costs)}</td>
							<td class={`${td} text-right text-[#5ccac4]`}>{fmtUsd(stats.best, { signed: true })}</td>
							<td class={`${td} text-right text-[#f2956f]`}>{fmtUsd(stats.worst, { signed: true })}</td>
							<td class={`${td} text-right`}>{stats.avgHoldHours !== null ? dur(stats.avgHoldHours * 3_600_000) : '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if closedTotal}
			<h3 class="mt-2 text-[13px] font-semibold text-sc-ink">How trades closed</h3>
			<div class="grid gap-1.5">
				{#each closeMix as [text, entry]}
					<div class="grid grid-cols-[minmax(0,10em)_minmax(0,1fr)_auto] items-center gap-2.5 text-[12px]">
						<span class={entry.tone === 'stop' ? 'text-[#f2956f]' : 'text-sc-ink2'}>{text}</span>
						<span class="relative h-2 overflow-hidden rounded bg-sc-line2"><i class="absolute inset-y-0 left-0 rounded bg-sc-ink2" style={`width:${(entry.count / closedTotal) * 100}%`}></i></span>
						<span class="whitespace-nowrap font-plex-mono text-sc-ink2">{entry.count} · <span class={toneClass(entry.net)}>{fmtUsd(entry.net, { signed: true })}</span></span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
	<div class="grid min-w-0 content-start gap-2 p-3">
		{#if mode === 'live'}
			<h3 class="text-[13px] font-semibold text-sc-ink">Account equity, closed trades</h3>
			<DeskEquityChart history={equity} />
		{/if}
		<h3 class="text-[13px] font-semibold text-sc-ink">Realized per day, last 30 days{scopeLabel ? ` · ${scopeLabel}` : ''}</h3>
		<DeskDailyBars {fills} {now} />
	</div>
</div>
