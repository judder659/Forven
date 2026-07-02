<script lang="ts">
	export let message = 'Loading...';
	export let compact = true;
</script>

<div class={`text-xs uppercase tracking-wider text-[#555] ${compact ? '' : 'py-6'}`} role="status" aria-live="polite">
	<span class="inline-flex items-center gap-2">
		<span class="inline-block w-1.5 h-1.5 rounded-full bg-[#555] animate-pulse"></span>
		{message}
	</span>
</div>
