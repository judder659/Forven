<script lang="ts">
	/**
	 * Shared sticky "unsaved changes" bar for the Agents config tabs.
	 *
	 * Renders nothing until `dirty` is true; then a single sticky bar with one
	 * Save and one Discard action. Standardizes the make-many-changes-then-save
	 * pattern across tabs (Routing & Fallbacks, Models, …).
	 */
	export let dirty = false;
	export let saving = false;
	export let message = 'You have unsaved changes.';
	export let saveLabel = 'Save changes';
	export let onSave: () => void;
	export let onDiscard: () => void;
</script>

{#if dirty}
	<div
		class="rounded-md sticky bottom-0 z-10 flex items-center justify-between gap-3 border border-sc-line2 bg-sc-panel px-4 py-3"
	>
		<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">{message}</span>
		<div class="flex gap-2">
			<button
				type="button"
				on:click={onDiscard}
				disabled={saving}
				class="terminal-button text-[12px]"
			>
				Discard
			</button>
			<button
				type="button"
				on:click={onSave}
				disabled={saving}
				class="terminal-button-primary text-[12px]"
			>
				{saving ? 'Saving…' : saveLabel}
			</button>
		</div>
	</div>
{/if}
