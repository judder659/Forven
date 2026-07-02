<script lang="ts">
	export let rows = 4;
	export let rowClass = 'h-3';
</script>

<div class="space-y-2">
	{#each Array.from({ length: rows }) as _, idx}
		<div
			class="bg-[#151515] animate-pulse {rowClass} {idx % 3 === 0 ? 'w-full' : idx % 3 === 1 ? 'w-5/6' : 'w-3/4'}"
		></div>
	{/each}
</div>
