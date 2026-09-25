<script lang="ts">
	/** Latest live trade activity, including orders the exchange rejected. */
	import type { LiveFill } from '$lib/api/dashboard';
	import { formatAge, formatUsd, pnlTone } from '$lib/utils/liveDashboard';

	export let fills: LiveFill[] = [];
	export let now = Date.now();

	const STATUS_TONE: Record<string, string> = {
		OPEN: 'border-emerald-900 text-emerald-400',
		CLOSED: 'border-[#333] text-gray-400',
		FAILED: 'border-red-900 text-red-400',
	};

	function reasonLabel(fill: LiveFill): string {
		if (fill.status === 'FAILED') return fill.failure_reason || 'order failed';
		if (fill.status === 'OPEN') return 'opened';
		return (fill.close_reason || 'closed').replace(/_/g, ' ');
	}
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="recent-fills">
	<div class="flex items-center justify-between border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400">Recent live trades</h2>
		<a href="/all-trades" class="text-[10px] uppercase tracking-wider text-gray-500 hover:text-white">All trades →</a>
	</div>
	{#if fills.length === 0}
		<div class="px-3 py-4 text-xs text-gray-500">No live trades yet.</div>
	{:else}
		<table class="w-full text-left text-xs tabular-nums">
			<tbody class="divide-y divide-[#1a1a1a]">
				{#each fills as fill (fill.id)}
					<tr class="hover:bg-[#0d0d0d]">
						<td class="w-12 px-3 py-1.5 text-gray-500">{formatAge(fill.closed_at ?? fill.opened_at, now)}</td>
						<td class="px-2 py-1.5 whitespace-nowrap">
							<span class="font-bold text-gray-200">{fill.asset}</span>
							<span class="text-[10px] uppercase {fill.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}">{fill.direction}</span>
							<span class="ml-1 text-gray-500">{fill.strategy_id.startsWith('bot:') ? 'bot' : fill.strategy_id}</span>
						</td>
						<td class="px-2 py-1.5">
							<span class="border px-1 text-[10px] font-bold uppercase {STATUS_TONE[fill.status] ?? STATUS_TONE.CLOSED}">{fill.status}</span>
						</td>
						<td class="max-w-[180px] truncate px-2 py-1.5 text-gray-500" title={reasonLabel(fill)}>{reasonLabel(fill)}</td>
						<td class="px-3 py-1.5 text-right font-mono font-bold {pnlTone(fill.net_pnl_usd)}">
							{fill.net_pnl_usd === null ? '' : formatUsd(fill.net_pnl_usd, true)}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
