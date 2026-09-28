<script lang="ts">
	// A rule's state on one bar: each condition as the sentence it tests, with the
	// values it saw and whether it held.
	import type { RuleConditionState, RuleGroupState } from '$lib/api';
	import { formatValue, OPERATOR_LABELS, operandDisplay } from '$lib/utils/ruleLabels';
	import RuleTraceView from './RuleTraceView.svelte';

	export let rule: RuleGroupState;
	export let labels: Record<string, string> = {};
	export let knobs: Record<string, number> = {};
	export let nested = false;

	function isGroup(item: RuleConditionState | RuleGroupState): item is RuleGroupState {
		return Array.isArray((item as RuleGroupState).items);
	}
	/** A number or knob shows its value in its label; a series shows what it read. */
	function readsSeries(operand: unknown): boolean {
		if (typeof operand === 'string') return true;
		return !!operand && typeof operand === 'object' && ('series' in operand || 'indicator' in operand);
	}
</script>

<div class={nested ? 'border-l border-[#262626] pl-2' : ''}>
	{#if nested || rule.items.length > 1}
		<div class="mb-0.5 text-[10px] uppercase tracking-wider text-[#555]">
			{rule.logic === 'and' ? 'all of' : 'any of'} · <span class={rule.result ? 'text-emerald-500' : 'text-[#666]'}>{rule.result ? 'held' : 'did not hold'}</span>
		</div>
	{/if}
	<ul class="space-y-1">
		{#each rule.items as item}
			<li>
				{#if isGroup(item)}
					<RuleTraceView rule={item} {labels} {knobs} nested />
				{:else}
					<div class="flex items-baseline gap-2 text-[12px] leading-5">
						<span class="w-3 shrink-0 {item.result ? 'text-emerald-400' : 'text-[#555]'}" aria-label={item.result ? 'held' : 'did not hold'}>{item.result ? '✓' : '✗'}</span>
						<span class={item.result ? 'text-[#ddd]' : 'text-[#777]'}>
							{operandDisplay(item.left, labels, knobs)}
							{#if readsSeries(item.left)}<span class="font-mono text-[11px] text-white">{formatValue(item.left_value)}</span>{/if}
							<span class="italic text-[#888]">{OPERATOR_LABELS[item.op] ?? item.op}</span>
							{operandDisplay(item.right, labels, knobs)}
							{#if readsSeries(item.right)}<span class="font-mono text-[11px] text-white">{formatValue(item.right_value)}</span>{/if}
						</span>
					</div>
					{#if item.op.startsWith('crosses')}
						<div class="ml-5 text-[10px] text-[#555]">the bar before: {formatValue(item.left_prev)} vs {formatValue(item.right_prev)}</div>
					{/if}
				{/if}
			</li>
		{/each}
	</ul>
</div>
