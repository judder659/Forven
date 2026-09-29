<script lang="ts">
	/** Exchange-style fills: an open row and a close row per trade, with slippage and the reason. */
	import type { DeskFill, DeskMode } from '$lib/api/desk';
	import { executionSummary, fillRows } from '$lib/utils/tradingDesk/fills';
	import { fmtBps, fmtDateTime, fmtPx, fmtQty, fmtUsd, toneClass } from '$lib/utils/tradingDesk/format';

	export let mode: DeskMode;
	export let fills: DeskFill[] = [];
	export let selectedSid: string | null = null;
	export let limit = 250;

	$: rows = fillRows(fills);
	$: shown = rows.slice(0, limit);
	$: summary = executionSummary(fills);
	$: anyInferred = shown.some((row) => row.why.inferred);

	const nameOf = (sid: string) => (sid.startsWith('bot:') ? 'Bot' : sid);
	const word = (bps: number) => (Math.abs(bps) < 0.05 ? 'at the signal' : bps > 0 ? 'worse than the signal' : 'better than the signal');
	const th = 'sticky top-0 z-[1] whitespace-nowrap bg-sc-panel px-2.5 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3';
	const td = 'whitespace-nowrap border-b border-sc-line px-2.5 py-1.5';
</script>

{#if rows.length === 0}
	<div class="grid gap-1 px-4 py-7 text-center text-[12.5px] text-sc-ink3"><b class="font-medium text-sc-ink2">No {mode} fills yet</b>Fills appear here the moment an order lands.</div>
{:else}
	<div class="max-h-[460px] overflow-auto">
		<table class="w-full border-collapse text-[12px]" data-testid="desk-fills">
			<thead>
				<tr>
					<th class={`${th} text-left`}>Time</th>
					<th class={`${th} text-left`}>Strategy</th>
					<th class={`${th} text-left`}>Market</th>
					<th class={`${th} text-left`}>Fill</th>
					<th class={`${th} text-right`}>Price</th>
					<th class={`${th} text-right`}>Size</th>
					<th class={`${th} text-right`}>Value</th>
					<th class={`${th} text-right`} title="Positive = filled worse than the signal price">Slippage, bps</th>
					<th class={`${th} text-right`}>Net P&amp;L</th>
					<th class={`${th} text-right`} title="Fees and funding on the closed trade">Costs</th>
					<th class={`${th} text-left`}>Why</th>
				</tr>
			</thead>
			<tbody class="font-plex-mono tabular-nums">
				{#each shown as row (row.key)}
					<tr class={`hover:bg-sc-hover ${row.fill.strategy_id === selectedSid ? 'bg-[#0f1319]' : ''}`}>
						<td class={`${td} text-left`}>{fmtDateTime(row.time)}</td>
						<td class={`${td} text-left font-sans`}>{nameOf(row.fill.strategy_id)}</td>
						<td class={`${td} text-left font-sans`}>{row.fill.asset ?? '—'}</td>
						{#if row.kind === 'failed'}
							<td class={`${td} text-left font-sans text-[#e5574f]`}>Failed {row.fill.direction}</td>
							<td class={`${td} text-right`}>{fmtPx(row.price)}</td>
							<td class={`${td} text-right`}>{fmtQty(row.fill.size)}</td>
							<td class={`${td} text-right`}>—</td><td class={`${td} text-right`}>—</td><td class={`${td} text-right`}>—</td><td class={`${td} text-right`}>—</td>
							<td class={`${td} text-left font-sans text-sc-ink2`}>{row.why.text}</td>
						{:else}
							<td class={`${td} text-left font-sans ${row.isBuy ? 'text-[#5ccac4]' : 'text-[#f2956f]'}`}>{row.kind === 'open' ? 'Open' : 'Close'} {row.fill.direction}</td>
							<td class={`${td} text-right`}>{fmtPx(row.price)}</td>
							<td class={`${td} text-right`}>{fmtQty(row.fill.size)}</td>
							<td class={`${td} text-right`}>{fmtUsd(row.value)}</td>
							<td class={`${td} text-right ${row.slippageBps === null ? '' : row.slippageBps > 0 ? 'text-[#f2956f]' : row.slippageBps < 0 ? 'text-[#5ccac4]' : ''}`}>{fmtBps(row.slippageBps)}</td>
							<td class={`${td} text-right ${row.kind === 'close' ? toneClass(row.netPnl) : ''}`}>{row.kind === 'close' ? fmtUsd(row.netPnl, { signed: true }) : ''}</td>
							<td class={`${td} text-right text-sc-ink3`}>{row.kind === 'close' && row.fill.costs_usd !== null ? fmtUsd(row.fill.costs_usd, { digits: 3 }) : ''}</td>
							<td class={`${td} text-left font-sans ${row.why.tone === 'stop' ? 'text-[#f2956f]' : 'text-sc-ink2'}`} title={row.why.inferred ?? row.fill.close_reason ?? ''}>{row.why.text}{#if row.why.inferred}<sup class="text-sc-ink3"> *</sup>{/if}</td>
						{/if}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	<p class="px-3 py-2 text-[12px] text-sc-ink3">
		{#if summary.entryCount || summary.exitCount}
			Strategy execution: the median entry filled {summary.entryMedian !== null ? `${fmtBps(summary.entryMedian)} bps (${word(summary.entryMedian)}) across ${summary.entryCount} fills` : '—'}, the median exit {summary.exitMedian !== null ? `${fmtBps(summary.exitMedian)} bps (${word(summary.exitMedian)}) across ${summary.exitCount}` : '—'}.{summary.worstEntry !== null ? ` Worst entry ${fmtBps(summary.worstEntry)} bps.` : ''} The backtests assume 2 bps against you each way.
		{/if}
		{#if anyInferred} * Stop fill inferred: the close was recorded by reconcile and its exit matches the resting stop.{/if}
		{#if rows.length > shown.length} Showing the newest {shown.length} of {rows.length} fills.{/if}
	</p>
{/if}
