<script lang="ts">
	import type { RailStage } from '$lib/utils/strategyContainer/lifecycle';

	/** Quick screen → gauntlet → paper → live, with the next gate on the right. */
	export let stages: RailStage[] = [];
	export let gateLabel = 'Next gate';
	export let gateHeadline = '';
	export let gateDetail = '';
	export let terminalNote = '';
</script>

<div class="grid border border-[#1d1d1d] bg-[#090909] md:grid-cols-[repeat(4,minmax(0,1fr))_minmax(240px,1.3fr)]" data-testid="lifecycle-rail">
	{#each stages as stage (stage.key)}
		<div class="relative min-w-0 border-b border-[#1d1d1d] py-2.5 pl-9 pr-3 md:border-b-0 md:border-r" data-testid={`rail-stage-${stage.key}`} data-state={stage.state}>
			<span
				class={`absolute left-3.5 top-[15px] h-2.5 w-2.5 rounded-full border-2 ${
					stage.state === 'done'
						? 'border-[#3cc48f] bg-[#3cc48f]'
						: stage.state === 'now'
							? 'border-white bg-white shadow-[0_0_0_3px_rgba(255,255,255,0.12)]'
							: 'border-[#444] bg-[#090909]'
				}`}
			></span>
			<div class={`text-[13px] font-semibold ${stage.state === 'next' ? 'text-[#888]' : 'text-white'}`}>{stage.label}</div>
			<div class={`text-[11px] ${stage.state === 'now' ? 'text-[#aab1bc]' : 'text-[#666]'}`}>{stage.meta}</div>
			{#if stage.progress !== null}
				<div class="mt-1.5 h-[3px] bg-[#2a2f38]"><i class="block h-full bg-[#aab1bc]" style={`width:${(stage.progress * 100).toFixed(1)}%`}></i></div>
			{/if}
		</div>
	{/each}
	<div class="grid content-start gap-0.5 bg-[#0d0d0d] px-3.5 py-2.5" data-testid="rail-gate">
		<div class="text-[10px] uppercase tracking-[0.2em] text-[#555]">{gateLabel}</div>
		{#if terminalNote}
			<div class="text-[12px] text-[#f2956f]">{terminalNote}</div>
		{/if}
		{#if gateHeadline}
			<div class="text-[12px] text-white"><b class="font-semibold">{gateHeadline}</b>{#if gateDetail}<span class="text-[#aab1bc]"> {gateDetail}</span>{/if}</div>
		{/if}
		<slot />
	</div>
</div>
