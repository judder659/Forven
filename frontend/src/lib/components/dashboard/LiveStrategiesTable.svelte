<script lang="ts">
	/**
	 * One row per live strategy: is it holding, watching, blocked or not being
	 * scanned, what it has made net of costs, and why its entries were refused.
	 */
	import type { ForvenRiskStatus } from '$lib/api';
	import type { LiveFleetStrategy, LiveStrategyState } from '$lib/api/dashboard';
	import { finite, formatAge, formatPct, formatUsd, pnlTone } from '$lib/utils/liveDashboard';

	type Sizing = NonNullable<NonNullable<ForvenRiskStatus['portfolio_budget_live']>['strategy_sizing']>[number];

	export let strategies: LiveFleetStrategy[] = [];
	export let sizing: Sizing[] = [];
	export let liveBotsArmed = 0;
	export let now = Date.now();

	const STATE_ORDER: Record<LiveStrategyState, number> = { stale: 0, blocked: 1, in_position: 2, watching: 3 };
	const STATE_CHIP: Record<LiveStrategyState, { label: string; tone: string; hint: string }> = {
		in_position: {
			label: 'In position',
			tone: 'border-emerald-900 bg-emerald-500/10 text-emerald-400',
			hint: 'Holding a live position.',
		},
		watching: {
			label: 'Watching',
			tone: 'border-[#333] text-gray-400',
			hint: 'Flat and being scanned; waiting for an entry signal.',
		},
		blocked: {
			label: 'Blocked',
			tone: 'border-amber-800 bg-amber-500/10 text-amber-300',
			hint: 'Its latest entry signal was refused by the live path.',
		},
		stale: {
			label: 'Not scanned',
			tone: 'border-red-800 bg-red-500/10 text-red-300',
			hint: 'The scanner has not evaluated it recently.',
		},
	};

	$: sizingById = new Map(sizing.map((entry) => [entry.strategy_id, entry]));
	$: rows = [...strategies].sort(
		(a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.strategy_id.localeCompare(b.strategy_id),
	);

	function scanSummary(scan: LiveFleetStrategy['last_scan']): string {
		if (!scan) return 'never scanned';
		if (scan.signal_type === 'evaluate') return 'no signal';
		if (scan.executed) return `${scan.signal_type} filled`;
		const reason = scan.reason ?? '';
		if (!reason || reason === 'evaluation_only') return `${scan.signal_type} signal`;
		if (reason === 'no_actionable_position_or_order') return `${scan.signal_type} · nothing to do`;
		return `${scan.signal_type} blocked`;
	}

	function market(strategy: LiveFleetStrategy): string {
		return [strategy.symbol, strategy.timeframe].filter(Boolean).join(' · ') || '—';
	}
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="live-strategies">
	<div class="flex items-center justify-between border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400">
			Live strategies <span class="text-gray-600">({strategies.length})</span>
		</h2>
		{#if liveBotsArmed > 0}
			<a href="/bot-factory" class="text-[10px] uppercase tracking-wider text-gray-500 hover:text-white">
				+ {liveBotsArmed} live {liveBotsArmed === 1 ? 'bot' : 'bots'} →
			</a>
		{/if}
	</div>
	{#if rows.length === 0}
		<div class="px-3 py-4 text-xs text-gray-500">No strategies are at the live stage.</div>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full text-left text-xs tabular-nums">
				<thead>
					<tr class="border-b border-[#222] text-[10px] uppercase tracking-wider text-gray-500">
						<th class="px-3 py-1.5 font-medium">Strategy</th>
						<th class="px-3 py-1.5 font-medium">State</th>
						<th class="px-3 py-1.5 text-right font-medium" title="What the live sizing will put on per trade">Size</th>
						<th class="px-3 py-1.5 text-right font-medium" title="Closed live trades · wins/losses · failed orders">Trades</th>
						<th class="px-3 py-1.5 text-right font-medium" title="Realized, net of fees and funding">Net P&amp;L</th>
						<th class="px-3 py-1.5 font-medium">Last trade</th>
						<th class="px-3 py-1.5 font-medium">Last scan</th>
						<th class="px-3 py-1.5 font-medium">Blocked entries</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as strategy (strategy.strategy_id)}
						{@const chip = STATE_CHIP[strategy.state]}
						{@const size = sizingById.get(strategy.strategy_id)}
						{@const blocked = strategy.blocked_entries}
						{@const trades = strategy.trades}
						<tr class="border-t border-[#1a1a1a] hover:bg-[#0d0d0d]">
							<td class="max-w-[260px] px-3 py-1.5">
								<a href={`/lab/strategy/${encodeURIComponent(strategy.strategy_id)}`} class="font-bold text-gray-100 hover:text-white">
									{strategy.strategy_id}
								</a>
								<span class="ml-1 text-gray-500">{market(strategy)}</span>
								<div class="truncate text-[10px] text-gray-600" title={strategy.display_name || strategy.name}>
									{strategy.display_name || strategy.name}
								</div>
							</td>
							<td class="px-3 py-1.5">
								<span class="whitespace-nowrap border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider {chip.tone}" title={chip.hint}>
									{chip.label}
								</span>
							</td>
							<td class="px-3 py-1.5 text-right font-mono text-gray-300" title={size ? `${size.binding === 'manual' ? 'Manual cap' : 'Equal share of the account'}` : ''}>
								{formatUsd(finite(size?.effective_usd))}
							</td>
							<td class="px-3 py-1.5 text-right font-mono whitespace-nowrap text-gray-300">
								{trades.closed}
								<span class="text-gray-500">· {trades.wins}/{trades.losses}</span>
								{#if trades.failed > 0}
									<span class="text-red-400" title="Orders that failed at the exchange">· {trades.failed} failed</span>
								{/if}
							</td>
							<td class="px-3 py-1.5 text-right font-mono font-bold {pnlTone(trades.closed ? trades.net_pnl_usd : null)}">
								{trades.closed ? formatUsd(trades.net_pnl_usd, true) : '—'}
								{#if trades.win_rate !== null}
									<div class="text-[10px] font-normal text-gray-500">win {formatPct(trades.win_rate, 0)}</div>
								{/if}
							</td>
							<td class="px-3 py-1.5 whitespace-nowrap text-gray-400">
								{trades.last_trade_at ? `${formatAge(trades.last_trade_at, now)} ago` : 'never'}
							</td>
							<td class="px-3 py-1.5 whitespace-nowrap text-gray-400">
								{#if strategy.last_scan}
									{formatAge(strategy.last_scan.at, now)} ago
									<div class="text-[10px] text-gray-500">{scanSummary(strategy.last_scan)}</div>
								{:else}
									never
								{/if}
							</td>
							<td class="px-3 py-1.5 text-gray-400">
								{#if blocked.count === 0}
									<span class="text-gray-600">none in {blocked.window_days}d</span>
								{:else}
									<span class={strategy.state === 'blocked' ? 'text-amber-300' : 'text-gray-400'} title={blocked.top_reason ?? ''}>
										{blocked.count}× in {blocked.window_days}d · last {formatAge(blocked.last_at, now)} ago
									</span>
								{/if}
							</td>
						</tr>
						{#if strategy.state === 'blocked' && blocked.last_reason}
							<tr>
								<td colspan="8" class="px-3 pb-2 pt-0 text-[11px] text-amber-300/90">
									↳ {blocked.last_reason}
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
