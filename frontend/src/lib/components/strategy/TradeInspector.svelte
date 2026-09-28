<script lang="ts">
	// Why one preview trade opened and closed: its fills and outcome, the entry
	// rule on the bar that fired it, and the exit rule or the price event that
	// ended it.
	import { createEventDispatcher } from 'svelte';
	import type { PreviewTrade } from '$lib/api';
	import { formatValue } from '$lib/utils/ruleLabels';
	import RuleTraceView from './RuleTraceView.svelte';

	export let trade: PreviewTrade | null = null;
	export let total = 0;
	export let labels: Record<string, string> = {};
	export let knobs: Record<string, number> = {};

	const dispatch = createEventDispatcher<{ step: number; focus: void }>();

	const stamp = (iso: string | null) => (iso ? iso.slice(0, 16).replace('T', ' ') : '—');
	const pct = (value: number, digits = 2) => `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;

	const EXIT_REASONS: Record<string, string> = {
		signal: 'Exit rule',
		stop_loss: 'Stop loss',
		take_profit: 'Take profit',
		trailing_stop: 'Trailing stop',
		time_stop: 'Time stop',
		liquidation: 'Liquidation',
		window_end: 'End of test window',
	};

	function exitStory(t: PreviewTrade): string {
		switch (t.exit_reason) {
			case 'stop_loss':
				return 'Price reached the stop loss during the exit bar and the trade closed at the stop.';
			case 'take_profit':
				return 'Price reached the take-profit target during the exit bar.';
			case 'trailing_stop':
				return 'Price reversed to the trailing stop, which had followed the move.';
			case 'time_stop':
				return `The time stop closed the trade after ${t.bars_held} bars.`;
			case 'liquidation':
				return 'Losses reached the liquidation level for this leverage.';
			case 'window_end':
				return t.sample === 'in'
					? 'Still open where the in-sample part ends, so it was closed there. The out-of-sample part starts flat.'
					: 'Still open when the data ended, so it was closed at the last bar to be scored.';
			default:
				return 'The exit rule held on the close before the exit, and the trade closed at the next open.';
		}
	}
</script>

{#if !trade}
	<div class="px-1 py-6 text-center text-[12px] text-[#555]">
		Click a trade on the chart or in the Trades list to see why it opened and closed.
	</div>
{:else}
	<div class="space-y-3">
		<div class="flex flex-wrap items-center gap-2">
			<span class="text-[12px] font-bold text-white">Trade #{trade.n}</span>
			<span class="text-[11px] text-[#555]">of {total}</span>
			<span class="border px-1.5 py-0.5 text-[9px] uppercase tracking-wider {trade.direction === 'long' ? 'border-emerald-800 text-emerald-400' : 'border-orange-800 text-orange-400'}">{trade.direction}</span>
			<span class="border border-[#333] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[#888]" title={trade.sample === 'out' ? 'Scored by Run Backtest' : 'Used to shape the rule; not scored'}>{trade.sample === 'out' ? 'out-of-sample' : 'in-sample'}</span>
			<span class="ml-auto flex items-center gap-1">
				<button type="button" on:click={() => dispatch('step', -1)} disabled={trade.n <= 1} aria-label="previous trade"
					class="border border-[#2a2a2a] px-2 py-0.5 text-[11px] text-[#aaa] hover:border-white hover:text-white disabled:opacity-30">←</button>
				<button type="button" on:click={() => dispatch('step', 1)} disabled={trade.n >= total} aria-label="next trade"
					class="border border-[#2a2a2a] px-2 py-0.5 text-[11px] text-[#aaa] hover:border-white hover:text-white disabled:opacity-30">→</button>
				<button type="button" on:click={() => dispatch('focus')}
					class="border border-[#2a2a2a] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[#aaa] hover:border-white hover:text-white">Show on chart</button>
			</span>
		</div>

		<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px] sm:grid-cols-4" data-testid="trade-facts">
			<div><div class="text-[9px] uppercase tracking-wider text-[#555]">Entry</div><div class="font-mono text-[#ddd]">{stamp(trade.entry_time)}</div><div class="font-mono text-[11px] text-[#888]">@ {formatValue(trade.entry_price)}</div></div>
			<div><div class="text-[9px] uppercase tracking-wider text-[#555]">Exit · {EXIT_REASONS[trade.exit_reason] ?? trade.exit_reason.replaceAll('_', ' ')}</div><div class="font-mono text-[#ddd]">{stamp(trade.exit_time)}</div><div class="font-mono text-[11px] text-[#888]">@ {formatValue(trade.exit_price)}</div></div>
			<div><div class="text-[9px] uppercase tracking-wider text-[#555]">Result</div><div class="font-mono text-[15px] {trade.pnl_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}">{pct(trade.pnl_pct)}</div><div class="text-[11px] text-[#888]">{trade.bars_held} bars held</div></div>
			<div><div class="text-[9px] uppercase tracking-wider text-[#555]">Costs</div><div class="font-mono text-[11px] text-[#aaa]">fees {pct(-trade.cost_pct)}</div><div class="font-mono text-[11px] text-[#aaa]">funding {pct(-trade.funding_pct, 3)}</div><div class="font-mono text-[11px] text-[#666]">size {(trade.size_fraction * 100).toFixed(0)}% of equity</div></div>
		</div>

		<div class="border-t border-[#161616] pt-2">
			<div class="mb-1 text-[10px] uppercase tracking-wider text-[#666]">Why it entered{#if trade.entry_signal_time}<span class="normal-case tracking-normal text-[#555]"> · rule held on the {stamp(trade.entry_signal_time)} close, filled at the next open</span>{/if}</div>
			{#if trade.entry_rule}
				<RuleTraceView rule={trade.entry_rule} {labels} {knobs} />
			{:else}
				<div class="text-[11px] text-[#555]">The entry rule state is not available for this trade.</div>
			{/if}
		</div>

		<div class="border-t border-[#161616] pt-2">
			<div class="mb-1 text-[10px] uppercase tracking-wider text-[#666]">Why it exited{#if trade.exit_signal_time}<span class="normal-case tracking-normal text-[#555]"> · rule held on the {stamp(trade.exit_signal_time)} close</span>{/if}</div>
			<p class="text-[12px] text-[#aaa]">{exitStory(trade)}</p>
			{#if trade.exit_rule}
				<div class="mt-1"><RuleTraceView rule={trade.exit_rule} {labels} {knobs} /></div>
			{/if}
		</div>
	</div>
{/if}
