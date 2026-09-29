<script lang="ts">
	import type { PipelineExplainStrategy } from '$lib/api/lifecycle';
	import type { ForwardRecord } from '$lib/utils/forge/forward';
	import { paperProgress, statusLine, statusMeta, TONE_DOT, TONE_TEXT } from '$lib/utils/forge/status';

	export let entry: PipelineExplainStrategy | null = null;
	/** True while the gate explainer has not answered yet. */
	export let loading = false;
	/** Live strategies read their state from the live fleet. */
	export let forward: ForwardRecord | null = null;
	export let stage = '';

	$: meta = statusMeta(entry?.status ?? (loading ? null : 'unknown'));
	$: line = statusLine(entry);
	$: progress = paperProgress(entry);
	$: liveNote = stage === 'live_graduated' && forward?.book === 'live' && entry?.status === 'live' ? forward.stateLabel : '';
</script>

{#if !entry && loading}
	<div class="grid gap-1" aria-busy="true">
		<span class="h-3 w-24 animate-pulse rounded bg-sc-raise/70"></span>
		<span class="h-2.5 w-36 animate-pulse rounded bg-sc-raise/50"></span>
	</div>
{:else}
	<div class="grid min-w-0 gap-0.5" title={meta.help}>
		<span class={`inline-flex items-center gap-1.5 text-[12px] font-medium ${TONE_TEXT[meta.tone]}`}>
			<span class={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[meta.tone]}`} aria-hidden="true"></span>
			{meta.label}{#if liveNote && forward}<span class={`font-normal ${forward.tone === 'wait' || forward.tone === 'ok' ? 'text-sc-ink2' : TONE_TEXT[forward.tone]}`}>· {liveNote}</span>{/if}
		</span>
		{#if line.text && !liveNote}
			<span class="truncate text-[11px] text-sc-ink2" title={line.full}>{line.text}</span>
		{/if}
		{#if progress.length > 0}
			<div class="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
				{#each progress as item (item.key)}
					<span class="inline-flex items-center gap-1.5 text-[10.5px] text-sc-ink3" title={`${item.label}: ${item.current} of ${item.threshold} ${item.unit} needed for the paper→live gate`}>
						{item.label}
						<span class="relative h-[3px] w-10 overflow-hidden rounded-full bg-sc-line2">
							<i class={`absolute inset-y-0 left-0 ${item.met ? 'bg-[#3cc48f]' : 'bg-sc-ink2'}`} style={`width:${(item.fraction * 100).toFixed(1)}%`}></i>
						</span>
						<span class={`font-plex-mono ${item.met ? 'text-[#3cc48f]' : 'text-sc-ink2'}`}>{item.current}/{item.threshold}</span>
					</span>
				{/each}
			</div>
		{/if}
	</div>
{/if}
