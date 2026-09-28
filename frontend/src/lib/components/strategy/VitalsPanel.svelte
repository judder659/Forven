<script lang="ts">
	// The preview's numbers split the way Run Backtest splits the window: the
	// in-sample part that shaped the rule and the out-of-sample part it is scored
	// on, plus the deflated Sharpe for the variants tried and the traps found.
	import type { PreviewVitals, SampleStats } from '$lib/api';

	export let vitals: PreviewVitals | null = null;

	const pct = (value: number, digits = 1, signed = true) =>
		`${signed && value > 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
	const day = (iso: string | null) => (iso ? iso.slice(0, 10) : '—');

	const COLUMNS: Array<{ label: string; title: string; cell: (s: SampleStats) => string; tone?: (s: SampleStats) => string }> = [
		{ label: 'Trades', title: 'Closed trades', cell: (s) => String(s.trades) },
		{ label: 'Return', title: 'Net return, compounded on closed trades', cell: (s) => pct(s.net_return),
			tone: (s) => (s.net_return > 0 ? 'text-emerald-400' : s.net_return < 0 ? 'text-red-400' : 'text-[#aaa]') },
		{ label: 'Win', title: 'Winning trades', cell: (s) => (s.trades ? pct(s.win_rate, 0, false) : '—') },
		{ label: 'PF', title: 'Profit factor: gross wins ÷ gross losses', cell: (s) => (!s.trades ? '—' : s.profit_factor_is_infinite ? '∞' : (s.profit_factor ?? 0).toFixed(2)) },
		{ label: 'Max DD', title: 'Deepest fall from a closed-trade equity peak', cell: (s) => pct(-Math.abs(s.max_drawdown), 1, false) },
		{ label: 'Avg', title: 'Average trade', cell: (s) => (s.trades ? pct(s.avg_trade, 2) : '—') },
		{ label: 'Exposure', title: 'Share of bars in a trade', cell: (s) => pct(s.exposure, 0, false) },
	];

	$: rows = vitals
		? [
			{ label: 'In-sample', stats: vitals.in_sample, note: 'Shapes the rule. Not scored.', scored: false },
			{ label: 'Out-of-sample', stats: vitals.out_of_sample, note: 'What Run Backtest scores.', scored: true },
		]
		: [];
	$: dsr = vitals?.deflated_sharpe ?? null;
	$: dsrTone = !dsr ? 'text-[#666]' : dsr.probability >= 0.95 ? 'text-emerald-400' : dsr.probability >= 0.5 ? 'text-amber-400' : 'text-red-400';
</script>

{#if vitals}
	<div class="border border-[#222] bg-[#050505]" data-testid="vitals">
		<table class="w-full text-[11px]">
			<thead>
				<tr class="text-[9px] uppercase tracking-wider text-[#555]">
					<th class="px-3 py-1.5 text-left font-normal">Sample</th>
					{#each COLUMNS as col}<th class="px-2 py-1.5 text-right font-normal" title={col.title}>{col.label}</th>{/each}
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.label)}
					<tr class="border-t border-[#141414] {row.scored ? 'bg-white/[0.03]' : ''}">
						<td class="px-3 py-1.5" title={row.note}>
							<div class={row.scored ? 'text-white' : 'text-[#aaa]'}>{row.label}</div>
							<div class="text-[9px] text-[#555]">{day(row.stats.start)} → {day(row.stats.end)}</div>
						</td>
						{#each COLUMNS as col}
							<td class="px-2 py-1.5 text-right font-mono {col.tone ? col.tone(row.stats) : 'text-[#ccc]'}">{col.cell(row.stats)}</td>
						{/each}
					</tr>
				{/each}
			</tbody>
		</table>
		<div class="flex flex-wrap items-baseline gap-x-2 border-t border-[#141414] px-3 py-1.5 text-[11px]" data-testid="deflated-sharpe">
			<span class="text-[9px] uppercase tracking-wider text-[#555]">Deflated Sharpe</span>
			{#if dsr}
				<span class="font-mono {dsrTone}">{(dsr.probability * 100).toFixed(0)}%</span>
				<span class="text-[#777]">chance the out-of-sample edge is real, allowing for the {dsr.trials} result{dsr.trials === 1 ? '' : 's'} you have looked at</span>
			{:else}
				<span class="text-[#666]">needs at least 5 out-of-sample trades</span>
			{/if}
		</div>
		{#if vitals.traps.length}
			<div class="space-y-0.5 border-t border-[#141414] px-3 py-1.5" data-testid="traps">
				{#each vitals.traps as trap (trap.code)}
					<div class="text-[11px] {trap.level === 'warn' ? 'text-amber-400' : 'text-[#888]'}">{trap.level === 'warn' ? '⚠' : 'ℹ'} {trap.text}</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
