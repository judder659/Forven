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
		class="sticky bottom-0 z-10 flex items-center justify-between gap-3 border border-[#333] bg-[#050505] px-4 py-3"
	>
		<span class="text-xs uppercase tracking-wider text-[#aaa]">{message}</span>
		<div class="flex gap-2">
			<button
				type="button"
				on:click={onDiscard}
				disabled={saving}
				class="terminal-button text-xs"
			>
				Discard
			</button>
			<button
				type="button"
				on:click={onSave}
				disabled={saving}
				class="terminal-button-primary text-xs"
			>
				{saving ? 'Saving…' : saveLabel}
			</button>
		</div>
	</div>
{/if}
