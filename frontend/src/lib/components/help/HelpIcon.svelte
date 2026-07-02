<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export let helpId: string;
	export let size: 'sm' | 'md' | 'lg' = 'sm';

	const dispatch = createEventDispatcher<{ click: string }>();

	const sizeClasses = {
		sm: 'w-4 h-4 text-xs',
		md: 'w-5 h-5 text-sm',
		lg: 'w-6 h-6 text-base'
	};

	function handleClick(e: MouseEvent) {
		e.preventDefault();
		e.stopPropagation();
		dispatch('click', helpId);
	}
</script>

<button
	type="button"
	class="inline-flex items-center justify-center border border-[#333] bg-[#111] hover:border-[#555] hover:bg-[#222] text-[#888] hover:text-white transition-colors cursor-pointer {sizeClasses[size]}"
	on:click={handleClick}
	title="Click for more information"
>
	<span class="font-bold">?</span>
</button>

<style>
	button {
		line-height: 1;
		flex-shrink: 0;
	}
</style>
