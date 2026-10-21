<script lang="ts">
	// "Only what I changed": every field that differs from its default, grouped
	// by area, each with its own reset. Custom blocks (status cards, dials) are
	// left out; only plain manifest fields count as changes.
	import { SETTINGS_AREAS, SETTINGS_MANIFEST } from '$lib/settings/manifest';
	import { originalValues, pendingValues } from '$lib/settings/dirty';
	import { differsFromDefault, entryValue, isEntryVisible, seedOriginals } from '$lib/settings/values';
	import { onMount } from 'svelte';
	import SettingsAreaView from './SettingsAreaView.svelte';

	export let settings: Record<string, unknown>;
	export let hiddenAreas: string[] = [];
	export let onOpenArea: (id: string) => void = () => {};

	onMount(() => seedOriginals(settings));

	$: values = { ...$originalValues, ...$pendingValues };

	$: areas = SETTINGS_AREAS.filter((a) => a.id !== 'home' && !hiddenAreas.includes(a.id))
		.map((area) => ({
			area,
			count: SETTINGS_MANIFEST.filter(
				(e) =>
					e.area === area.id &&
					!e.hidden &&
					isEntryVisible(e, settings, values) &&
					(differsFromDefault(e, e.id in values ? values[e.id] : entryValue(settings, e)) ||
						differsFromDefault(e, e.id in $originalValues ? $originalValues[e.id] : entryValue(settings, e))),
			).length,
		}))
		.filter((a) => a.count > 0);
</script>

<div class="space-y-8" data-testid="changed-settings">
	<div>
		<h2 class="text-[17px] font-semibold text-sc-ink">Only what I changed</h2>
		<p class="text-xs text-sc-ink3">Every setting that differs from its default. Reset one to put it back; nothing changes until you save.</p>
	</div>
	{#if areas.length === 0}
		<p class="rounded-md border border-sc-line bg-sc-panel p-4 text-xs text-sc-ink2" data-testid="changed-settings-empty">
			Everything is at its default.
		</p>
	{:else}
		{#each areas as { area, count } (area.id)}
			<section class="space-y-3" data-testid="changed-area-{area.id}">
				<div class="flex items-baseline justify-between gap-2">
					<h3 class="text-[14px] font-semibold text-sc-ink">{area.label} <span class="font-plex-mono text-[11px] font-normal text-sc-ink3">{count}</span></h3>
					<button
						type="button"
						class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 hover:text-sc-ink"
						on:click={() => onOpenArea(area.id)}
					>Open {area.label} →</button>
				</div>
				<SettingsAreaView area={area.id} {settings} onlyChanged />
			</section>
		{/each}
	{/if}
</div>
