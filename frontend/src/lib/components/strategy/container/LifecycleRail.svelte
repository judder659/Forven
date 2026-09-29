<script lang="ts">
	import type { RailStage } from '$lib/utils/strategyContainer/lifecycle';

	/** Quick screen → gauntlet → paper → live, with the next gate on the right. */
	export let stages: RailStage[] = [];
	export let gateLabel = 'Next gate';
	export let gateHeadline = '';
	export let gateDetail = '';
	export let terminalNote = '';
</script>

<div class="grid overflow-hidden rounded-md border border-sc-line bg-sc-panel md:grid-cols-[repeat(4,minmax(0,1fr))_minmax(240px,1.3fr)]" data-testid="lifecycle-rail">
	{#each stages as stage (stage.key)}
		<div class="relative min-w-0 border-b border-sc-line py-2.5 pl-9 pr-3 md:border-b-0 md:border-r" data-testid={`rail-stage-${stage.key}`} data-state={stage.state}>
			<span
				class={`absolute left-3.5 top-[15px] h-2.5 w-2.5 rounded-full border-2 ${
					stage.state === 'done'
						? 'border-[#3cc48f] bg-[#3cc48f]'
						: stage.state === 'now'
							? 'border-sc-ink bg-sc-ink shadow-[0_0_0_3px_rgba(238,241,245,0.12)]'
							: stage.state === 'stopped'
								? 'border-[#e5574f] bg-[#e5574f]'
								: 'border-sc-ink4 bg-sc-panel'
				}`}
			></span>
			<div class={`text-[13px] font-semibold ${stage.state === 'next' ? 'text-sc-ink3' : 'text-sc-ink'}`}>{stage.label}</div>
			<div class={`text-[11px] ${stage.state === 'stopped' ? 'text-[#f2956f]' : stage.state === 'now' ? 'text-sc-ink2' : 'text-sc-ink3'}`}>{stage.meta}</div>
			{#if stage.progress !== null}
				<div class="mt-1.5 h-[3px] overflow-hidden rounded-full bg-sc-line2"><i class="block h-full bg-sc-ink2" style={`width:${(stage.progress * 100).toFixed(1)}%`}></i></div>
			{/if}
		</div>
	{/each}
	<div class="grid content-start gap-0.5 bg-sc-panel2 px-3.5 py-2.5" data-testid="rail-gate">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{gateLabel}</div>
		{#if terminalNote}
			<div class="text-[12px] text-[#f2956f]">{terminalNote}</div>
		{/if}
		{#if gateHeadline}
			<div class="text-[12px] text-sc-ink"><b class="font-semibold">{gateHeadline}</b>{#if gateDetail}{' '}<span class="text-sc-ink2">{gateDetail}</span>{/if}</div>
		{/if}
		<slot />
	</div>
</div>
