<script lang="ts">
	export let message = 'Loading...';
	export let compact = true;
</script>

<div class={`font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 ${compact ? '' : 'py-6'}`} role="status" aria-live="polite">
	<span class="inline-flex items-center gap-2">
		<span class="inline-block w-1.5 h-1.5 rounded-full bg-sc-line2 animate-pulse"></span>
		{message}
	</span>
</div>
