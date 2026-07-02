<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export let message = '';
	export let tone: 'error' | 'warning' | 'info' = 'error';
	export let dismissible = false;

	const dispatch = createEventDispatcher<{ dismiss: void }>();

	const toneClasses: Record<typeof tone, string> = {
		error: 'text-red-400 border-red-900 bg-red-500/5',
		warning: 'text-yellow-400 border-yellow-900 bg-yellow-500/5',
		info: 'text-[#888] border-[#333] bg-[#111]',
	};

	$: classes = toneClasses[tone] ?? toneClasses.error;
</script>

{#if message}
	<div class={`text-xs border px-3 py-2 ${classes}`} role="alert">
		<div class="flex items-center gap-2">
			<span class="flex-1">{message}</span>
			{#if dismissible}
				<button
					type="button"
					class="text-[10px] uppercase tracking-wider opacity-80 hover:opacity-100"
					on:click={() => dispatch('dismiss')}
				>
					Dismiss
				</button>
			{/if}
		</div>
	</div>
{/if}
