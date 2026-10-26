<script lang="ts">
	import { UPDATE_STEPS, type UpdateProgress } from '$lib/stores/updateStatus';

	export let progress: UpdateProgress;

	// Takes `p` as an argument so the template re-runs it when progress changes.
	function state(p: UpdateProgress, index: number): 'done' | 'active' | 'failed' | 'todo' {
		if (p.done || index < p.step) return 'done';
		if (index === p.step) return p.failed ? 'failed' : 'active';
		return 'todo';
	}
</script>

<ol class="space-y-0" aria-label="Update progress">
	{#each UPDATE_STEPS as label, i (label)}
		{@const s = state(progress, i)}
		<li class="relative flex items-center gap-3 py-1.5" aria-current={s === 'active' ? 'step' : undefined}>
			{#if i < UPDATE_STEPS.length - 1}
				<span
					class="absolute left-[9px] top-[26px] h-[calc(100%-16px)] w-px {s === 'done' ? 'bg-emerald-500/50' : 'bg-sc-line2'}"
					aria-hidden="true"
				></span>
			{/if}
			<span class="relative z-10 flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-full border {s === 'done'
					? 'border-emerald-500 bg-emerald-500 text-[#06130c]'
					: s === 'failed'
						? 'border-red-500 bg-red-500/15 text-red-400'
						: s === 'active'
							? 'border-sc-ink bg-sc-panel2'
							: 'border-sc-line2 bg-sc-panel'}"
			>
				{#if s === 'done'}
					<svg class="h-3 w-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
						<path fill-rule="evenodd" d="M16.7 5.3a1 1 0 0 1 0 1.4l-8 8a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.4L8 12.58l7.3-7.3a1 1 0 0 1 1.4 0Z" clip-rule="evenodd" />
					</svg>
				{:else if s === 'failed'}
					<svg class="h-3 w-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
						<path d="M6.3 5.3a1 1 0 0 1 1.4 0L10 7.6l2.3-2.3a1 1 0 1 1 1.4 1.4L11.4 9l2.3 2.3a1 1 0 0 1-1.4 1.4L10 10.4l-2.3 2.3a1 1 0 0 1-1.4-1.4L8.6 9 6.3 6.7a1 1 0 0 1 0-1.4Z" />
					</svg>
				{:else if s === 'active'}
					<span class="h-2.5 w-2.5 animate-spin rounded-full border-[1.5px] border-sc-ink border-t-transparent" aria-hidden="true"></span>
				{/if}
			</span>
			<span
				class="text-[13px] {s === 'todo' ? 'text-sc-ink3' : s === 'failed' ? 'text-red-300' : 'text-sc-ink'} {s === 'active' ? 'font-medium' : ''}"
			>
				{label}
			</span>
			<span class="sr-only">({s === 'todo' ? 'not started' : s})</span>
		</li>
	{/each}
</ol>
