<script lang="ts" context="module">
	import type { SampleStats } from '$lib/api';
	import type { RuleSpec } from './templates';

	/** One distinct version of the rules the preview has run this session. */
	export interface Variant {
		key: string;
		n: number;
		spec: RuleSpec;
		at: number;
		ins: SampleStats | null;
		oos: SampleStats | null;
		dsr: number | null;
	}
</script>

<script lang="ts">
	// Every version of the rules previewed this session, with its numbers: the
	// count the deflated Sharpe charges for, and a way back to any of them.
	import { createEventDispatcher } from 'svelte';
	import { diffSpecs } from '$lib/utils/specDiff';

	export let variants: Variant[] = [];
	/** Every result looked at this session, the count the deflated Sharpe charges for. */
	export let resultsSeen = 0;
	export let currentKey = '';
	export let current: RuleSpec | null = null;

	const dispatch = createEventDispatcher<{ restore: string }>();
	let open = '';

	const pct = (value: number | undefined) => (value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}%`);
	const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
	$: newestFirst = variants.slice().reverse();
</script>

<div class="space-y-2">
	<p class="text-[11px] text-[#666]">
		{variants.length} version{variants.length === 1 ? '' : 's'} of the rules previewed this session. The deflated Sharpe counts all {resultsSeen} result{resultsSeen === 1 ? '' : 's'} you have looked at, including each market and every stress-test, heatmap and market-grid cell: the more results seen on the same data, the likelier the best looks good by luck.
	</p>
	<div class="max-h-[320px] divide-y divide-[#111] overflow-y-auto border border-[#161616]">
		{#each newestFirst as variant (variant.key)}
			{@const isCurrent = variant.key === currentKey}
			<div class="px-2 py-1.5 {isCurrent ? 'bg-white/[0.03]' : ''}">
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
					<span class="font-mono text-[#666]">v{variant.n}</span>
					<span class="font-mono text-[10px] text-[#555]">{clock(variant.at)}</span>
					<span class="text-[#888]">OOS <span class="font-mono {(variant.oos?.net_return ?? 0) > 0 ? 'text-emerald-400' : (variant.oos?.net_return ?? 0) < 0 ? 'text-red-400' : 'text-[#aaa]'}">{pct(variant.oos?.net_return)}</span>
						<span class="text-[#555]">({variant.oos?.trades ?? 0} trades)</span></span>
					<span class="text-[#666]">IS <span class="font-mono text-[#999]">{pct(variant.ins?.net_return)}</span></span>
					{#if variant.dsr != null}<span class="text-[#666]">DSR <span class="font-mono text-[#999]">{(variant.dsr * 100).toFixed(0)}%</span></span>{/if}
					<span class="ml-auto flex items-center gap-2">
						{#if isCurrent}
							<span class="text-[10px] uppercase tracking-wider text-white">current</span>
						{:else}
							<button type="button" on:click={() => (open = open === variant.key ? '' : variant.key)}
								class="text-[10px] uppercase tracking-wider text-[#777] hover:text-white">{open === variant.key ? 'Hide' : 'Diff'}</button>
							<button type="button" on:click={() => dispatch('restore', variant.key)}
								class="text-[10px] uppercase tracking-wider text-[#aaa] hover:text-white">Restore</button>
						{/if}
					</span>
				</div>
				{#if open === variant.key && !isCurrent}
					{@const changes = diffSpecs(variant.spec, current)}
					<div class="mt-1.5 space-y-0.5 border-l border-[#333] pl-2 text-[11px]">
						<div class="text-[9px] uppercase tracking-wider text-[#555]">From v{variant.n} to the current rules</div>
						{#each changes as change}
							<div>
								<span class="{change.kind === 'added' ? 'text-emerald-400' : change.kind === 'removed' ? 'text-red-400' : 'text-amber-300'}">{change.kind}</span>
								<span class="text-[#888]">{change.area}</span>
								<span class="text-white">{change.label}</span>
								{#if change.before}<span class="font-mono text-[#777] line-through decoration-[#555]">{change.before}</span>{/if}
								{#if change.after}<span class="font-mono text-[#ccc]">{change.after}</span>{/if}
							</div>
						{/each}
						{#if !changes.length}<div class="text-[#555]">Same rules; only the market or execution settings differ.</div>{/if}
					</div>
				{/if}
			</div>
		{/each}
		{#if !variants.length}
			<div class="px-2 py-6 text-center text-[12px] text-[#555]">Each version of the rules you preview is listed here.</div>
		{/if}
	</div>
</div>
