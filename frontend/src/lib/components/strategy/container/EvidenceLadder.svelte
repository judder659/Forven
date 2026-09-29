<script lang="ts">
	import type { LadderColumn } from '$lib/utils/strategyContainer/ladder';
	import { fmtFraction, fmtNum, fmtUsd, isNum, signClass } from '$lib/utils/strategyContainer/format';

	export let columns: LadderColumn[] = [];
	export let runLabel = '';

	$: maxSharpe = Math.max(0.01, ...columns.map((col) => (isNum(col.sharpe) ? col.sharpe : 0)));

	type Row = { label: string; cell: (col: LadderColumn) => { text: string; cls?: string } };
	const blank = (col: LadderColumn) => ({ text: col.note && col.key !== 'paper' && col.key !== 'live' ? col.note : '—', cls: 'text-[#444]' });
	const rows: Row[] = [
		{ label: 'Trades', cell: (col) => (isNum(col.trades) ? { text: String(Math.round(col.trades)) } : blank(col)) },
		{
			label: 'Return',
			cell: (col) => (isNum(col.totalReturn) ? { text: fmtFraction(col.totalReturn), cls: signClass(col.totalReturn) } : isNum(col.pnlUsd) ? { text: fmtUsd(col.pnlUsd), cls: signClass(col.pnlUsd) } : { text: '—', cls: 'text-[#444]' }),
		},
		{ label: 'CAGR', cell: (col) => (isNum(col.cagr) ? { text: fmtFraction(col.cagr), cls: signClass(col.cagr) } : { text: '—', cls: 'text-[#444]' }) },
		{ label: 'Max drawdown', cell: (col) => (isNum(col.maxDrawdown) ? { text: fmtFraction(col.maxDrawdown, 1, false) } : { text: '—', cls: 'text-[#444]' }) },
		{ label: 'Win rate', cell: (col) => (isNum(col.winRate) ? { text: fmtFraction(col.winRate, 1, false) } : { text: '—', cls: 'text-[#444]' }) },
		{
			label: 'Profit factor',
			cell: (col) => (col.profitFactor === Number.POSITIVE_INFINITY ? { text: '∞' } : isNum(col.profitFactor) ? { text: fmtNum(col.profitFactor) } : { text: '—', cls: 'text-[#444]' }),
		},
		{ label: 'Buy & hold Sharpe, same days', cell: (col) => (isNum(col.benchSharpe) ? { text: fmtNum(col.benchSharpe) } : { text: '—', cls: 'text-[#444]' }) },
	];
</script>

<article class="grid content-start gap-3 border border-[#1d1d1d] bg-[#090909] p-4" id="evidence-ladder" data-testid="evidence-ladder">
	<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
		<h2 class="m-0 text-[13px] font-semibold text-white">Evidence ladder</h2>
		<span class="text-[11px] text-[#666]">The same metrics on each body of evidence, weakest to strongest. An edge that is real survives the move right.</span>
	</div>
	<div class="overflow-x-auto">
		<table class="w-full min-w-[760px] border-collapse text-[12px]">
			<thead>
				<tr>
					<th class="sticky left-0 bg-[#090909] px-2.5 py-2 text-left align-bottom text-[10px] font-normal uppercase tracking-[0.2em] text-[#555]">Metric</th>
					{#each columns as col (col.key)}
						<th class="px-2.5 py-2 text-right align-bottom font-normal" data-testid={`ladder-col-${col.key}`}>
							<span class="block text-[12px] font-semibold text-white">{col.name}</span>
							<span class="block whitespace-nowrap text-[11px] text-[#666]">{col.when}</span>
							<span class="mt-1 inline-block rounded-full border border-[#2a2f38] px-1.5 text-[10px] uppercase tracking-wide text-[#666]">{col.tag}</span>
						</th>
					{/each}
				</tr>
			</thead>
			<tbody class="tabular-nums">
				{#each rows as row, index (row.label)}
					{#if index === 3}
						<tr class="bg-[#0f1216]">
							<td class="sticky left-0 border-t border-[#1d1d1d] bg-[#0f1216] px-2.5 py-1.5 text-left font-medium text-white">Sharpe</td>
							{#each columns as col (col.key)}
								<td class="border-t border-[#1d1d1d] px-2.5 py-1.5 text-right" data-testid={`ladder-sharpe-${col.key}`}>
									{#if isNum(col.sharpe)}
										<span class="inline-grid grid-cols-[46px_auto] items-center justify-end gap-2">
											<i class="block h-1.5 justify-self-end bg-[#aab1bc]" style={`width:${Math.max(2, (Math.max(col.sharpe, 0) / maxSharpe) * 46)}px`}></i>
											<span class="text-white">{fmtNum(col.sharpe)}</span>
										</span>
									{:else}
										<span class="text-[#444]">—</span>
									{/if}
								</td>
							{/each}
						</tr>
					{/if}
					<tr>
						<td class="sticky left-0 border-t border-[#1d1d1d] bg-[#090909] px-2.5 py-1.5 text-left text-[#aab1bc]">{row.label}</td>
						{#each columns as col (col.key)}
							{@const cell = row.cell(col)}
							<td class={`border-t border-[#1d1d1d] px-2.5 py-1.5 text-right ${cell.cls ?? 'text-[#ddd]'}`}>{cell.text}</td>
						{/each}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<p class="m-0 max-w-[120ch] text-[11px] text-[#666]">
		In-sample and out-of-sample come from {runLabel || 'the selected run'} (the pinned run, else the newest; pick another on Performance). Walk-forward folds re-fit on a rolling window and overlap the out-of-sample period. Held-back data was sealed from research and scored once. Paper and live are closed trades by book; live has no fixed equity base, so its return shows in dollars.
	</p>
</article>
