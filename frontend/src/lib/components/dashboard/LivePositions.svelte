<script lang="ts">
	/**
	 * Open real-money positions only (execution_type === 'live'), marked to the
	 * live tick price. Stop, risk and wallet come from the live budget snapshot.
	 */
	import type { ForvenRiskStatus, ForvenTrade } from '$lib/api';
	import { livePrice, liveUnrealizedUsd } from '$lib/utils/livePnl';
	import { finite, formatAge, formatPrice, formatUsd, pnlTone } from '$lib/utils/liveDashboard';

	type BudgetPosition = NonNullable<NonNullable<ForvenRiskStatus['portfolio_budget_live']>['positions']>[number];

	export let trades: ForvenTrade[] = [];
	export let prices: Record<string, number> = {};
	export let budgetPositions: BudgetPosition[] = [];
	export let now = Date.now();

	$: budgetById = new Map(budgetPositions.map((position) => [String(position.trade_id ?? ''), position]));
	$: rows = trades.map((trade) => {
		const budget = budgetById.get(String(trade.id ?? ''));
		const entry = finite(trade.fill_entry_price) ?? finite(trade.entry_price);
		const mark = livePrice(trade.asset, prices);
		const size = finite(trade.size);
		const stop = finite(budget?.stop_price);
		const direction = String(trade.direction ?? 'long').toLowerCase();
		const stopDistance = stop !== null && mark ? ((direction === 'short' ? stop - mark : mark - stop) / mark) : null;
		const strategyId = String(trade.strategy_id ?? trade.strategy ?? '');
		return {
			id: String(trade.id ?? ''),
			asset: String(trade.asset ?? '—'),
			direction,
			strategyId,
			isBot: strategyId.startsWith('bot:'),
			entry,
			mark,
			pnl: liveUnrealizedUsd(trade, prices),
			notional: size !== null && mark !== null ? Math.abs(size * mark) : finite(budget?.notional_usd),
			leverage: finite(trade.leverage),
			stop,
			stopDistance,
			risk: finite(budget?.risk_usd),
			book: budget?.book ?? null,
			openedAt: trade.opened_at ?? null,
		};
	});
	$: totalPnl = rows.reduce((sum, row) => sum + (row.pnl ?? 0), 0);
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="live-positions">
	<div class="flex items-center justify-between border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400">
			Live positions <span class="text-gray-600">({rows.length})</span>
		</h2>
		<div class="flex items-center gap-3 text-[10px] uppercase tracking-wider">
			{#if rows.length > 0}
				<span class="text-gray-500">Unrealized <span class="font-mono {pnlTone(totalPnl)}">{formatUsd(totalPnl, true)}</span></span>
			{/if}
			<a href="/live-trades" class="text-gray-500 hover:text-white">Live trades →</a>
		</div>
	</div>
	{#if rows.length === 0}
		<div class="px-3 py-4 text-xs text-gray-500">No open live positions.</div>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full text-left text-xs tabular-nums">
				<thead>
					<tr class="border-b border-[#222] text-[10px] uppercase tracking-wider text-gray-500">
						<th class="px-3 py-1.5 font-medium">Position</th>
						<th class="px-3 py-1.5 font-medium">Strategy</th>
						<th class="px-3 py-1.5 text-right font-medium">Entry → mark</th>
						<th class="px-3 py-1.5 text-right font-medium">Unrealized</th>
						<th class="px-3 py-1.5 text-right font-medium">Stop</th>
						<th class="px-3 py-1.5 text-right font-medium">Notional</th>
						<th class="px-3 py-1.5 font-medium">Wallet</th>
						<th class="px-3 py-1.5 text-right font-medium">Age</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-[#1a1a1a]">
					{#each rows as row (row.id)}
						<tr class="hover:bg-[#0d0d0d]">
							<td class="px-3 py-1.5 whitespace-nowrap">
								<span class="font-bold text-gray-100">{row.asset}</span>
								<span class="ml-1 border px-1 text-[10px] font-bold uppercase {row.direction === 'short' ? 'border-red-900 text-red-400' : 'border-emerald-900 text-emerald-400'}">
									{row.direction}
								</span>
							</td>
							<td class="px-3 py-1.5 whitespace-nowrap">
								{#if row.isBot}
									<a href="/bot-factory" class="text-gray-300 hover:text-white">Bot</a>
								{:else}
									<a href={`/live-trades?select=${encodeURIComponent(row.strategyId)}`} class="text-gray-300 hover:text-white">{row.strategyId || '—'}</a>
								{/if}
							</td>
							<td class="px-3 py-1.5 text-right font-mono whitespace-nowrap text-gray-300">
								{formatPrice(row.entry)} → {formatPrice(row.mark)}
							</td>
							<td class="px-3 py-1.5 text-right font-mono font-bold {pnlTone(row.pnl)}">{formatUsd(row.pnl, true)}</td>
							<td class="px-3 py-1.5 text-right font-mono whitespace-nowrap">
								{#if row.stop === null}
									<span class="font-bold text-red-400">none</span>
								{:else}
									<span class="text-gray-300">{formatPrice(row.stop)}</span>
									{#if row.stopDistance !== null}
										<span class="text-gray-500"> ({(row.stopDistance * 100).toFixed(1)}%)</span>
									{/if}
								{/if}
							</td>
							<td class="px-3 py-1.5 text-right font-mono whitespace-nowrap text-gray-300">
								{formatUsd(row.notional)}{#if row.leverage}<span class="ml-1 text-gray-500">· {row.leverage}x</span>{/if}
							</td>
							<td class="px-3 py-1.5 text-gray-400">{row.book ?? '—'}</td>
							<td class="px-3 py-1.5 text-right text-gray-400">{formatAge(row.openedAt, now) || '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
