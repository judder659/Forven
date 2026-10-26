<script lang="ts">
	import { updateStatus, updateSnoozed, updateBusy, updatePanelOpen } from '$lib/stores/updateStatus';

	$: status = $updateStatus;
	$: visible = $updateBusy || Boolean(status?.supported && status?.update_available && !$updateSnoozed);
	$: urgent = Boolean(status?.urgent);
	$: label = (() => {
		if ($updateBusy) return 'Updating…';
		if (status?.target_version) return `v${status.target_version} available`;
		const n = status?.behind ?? 0;
		return `${n} new change${n === 1 ? '' : 's'}`;
	})();
</script>

{#if visible}
	<button
		type="button"
		class="group inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 normal-case tracking-normal text-[11px] font-medium transition-colors {urgent
			? 'border-amber-500/60 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20'
			: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20'}"
		on:click={() => updatePanelOpen.set(true)}
		aria-label="{label}. Open what's new"
		data-testid="update-pill"
	>
		{#if $updateBusy}
			<span class="h-2.5 w-2.5 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" aria-hidden="true"></span>
		{:else}
			<svg class="h-3 w-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
				<path d="M10 3a1 1 0 0 1 1 1v7.59l2.3-2.3a1 1 0 1 1 1.4 1.42l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.42L9 11.6V4a1 1 0 0 1 1-1Zm-6 13a1 1 0 0 1 1-1h10a1 1 0 1 1 0 2H5a1 1 0 0 1-1-1Z" />
			</svg>
		{/if}
		{label}
		{#if urgent && !$updateBusy}
			<span class="rounded bg-amber-500/20 px-1 text-[10px] font-semibold uppercase tracking-[0.06em]">Urgent</span>
		{/if}
	</button>
{/if}
