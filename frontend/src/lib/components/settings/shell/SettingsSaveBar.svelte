<script lang="ts">
	import {
		dirtyFields,
		originalValues,
		clearDirty,
		groupDirtyByBackendSection,
		listEmptyNumericDirtyFields,
	} from '$lib/settings/dirty';
	import { updateSettingsSection } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';

	export let currentValues: Record<string, unknown>;

	let saving = false;
	let error: string | null = null;

	$: count = $dirtyFields.size;
	$: hidden = count === 0;

	async function saveAll() {
		saving = true;
		error = null;
		const savingIds = new Set($dirtyFields);
		if (savingIds.size === 0) {
			saving = false;
			return;
		}
		const snapshot: Record<string, unknown> = {};
		for (const id of savingIds) snapshot[id] = currentValues[id];
		// Refuse to submit empty numeric fields — a persisted null silently
		// poisons backend configs. Mirror of the backend's 400 on null leaves.
		const emptyNumeric = listEmptyNumericDirtyFields(snapshot);
		if (emptyNumeric.length > 0) {
			saving = false;
			error = `${emptyNumeric.map((f) => `'${f.label}'`).join(', ')} ${
				emptyNumeric.length === 1 ? 'is' : 'are'
			} empty — enter a number or revert`;
			return;
		}
		const grouped = groupDirtyByBackendSection(snapshot);
		if (Object.keys(grouped).length === 0) {
			saving = false;
			error = 'No saveable fields';
			return;
		}
		const sections = Object.entries(grouped);
		const results = await Promise.allSettled(
			sections.map(([section, payload]) => updateSettingsSection(section, payload)),
		);
		const failed = results
			.map((r, i) => ({ result: r, section: sections[i][0] }))
			.filter((f) => f.result.status === 'rejected');
		saving = false;
		if (failed.length === 0) {
			originalValues.update((o) => ({ ...o, ...snapshot }));
			dirtyFields.update((set) => {
				const next = new Set(set);
				for (const id of savingIds) next.delete(id);
				return next;
			});
			addToast('Settings saved', 'success');
		} else {
			// Surface the ACTUAL backend refusal, not just a count — the inline
			// text is easy to miss and "can't save" is undebuggable without it.
			const detail = failed
				.map((f) => {
					const reason = (f.result as PromiseRejectedResult).reason;
					const message = reason instanceof Error ? reason.message : String(reason);
					return `${f.section}: ${message}`;
				})
				.join(' · ');
			error = detail;
			addToast(`Save failed — ${detail}`, 'error', undefined, 12_000);
		}
	}

	function revertAll() {
		clearDirty();
	}
</script>

{#if !hidden}
	<div
		class="fixed bottom-0 inset-x-0 z-40 bg-[#050505] border-t border-[#222] py-3 px-6 flex items-center justify-between"
	>
		<span class="text-xs uppercase tracking-wider text-yellow-400"
			>{count} unsaved change{count === 1 ? '' : 's'}</span
		>
		<div class="flex items-center gap-2">
			{#if error}<span class="text-xs text-red-400">{error}</span>{/if}
			<button
				type="button"
				on:click={revertAll}
				disabled={saving}
				class="terminal-button text-xs"
			>
				Revert all
			</button>
			<button
				type="button"
				on:click={saveAll}
				disabled={saving}
				class="terminal-button-primary text-xs"
			>
				{saving ? 'Saving…' : 'Save all'}
			</button>
		</div>
	</div>
{/if}
