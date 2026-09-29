<script lang="ts">
	import { onMount } from 'svelte';
	import {
		SETTINGS_MANIFEST,
		SETTINGS_SUBSECTIONS,
		type SettingsEntry,
	} from '$lib/settings/manifest';
	import SettingsSubsection from '$lib/components/settings/primitives/SettingsSubsection.svelte';
	import SettingsFieldRow from '$lib/components/settings/primitives/SettingsFieldRow.svelte';
	import SettingsAdvancedHeader from '$lib/components/settings/primitives/SettingsAdvancedHeader.svelte';
	import NotificationChannelsCard from '$lib/components/settings/NotificationChannelsCard.svelte';
	import { originalValues, pendingValues } from '$lib/settings/dirty';

	export let settings: Record<string, unknown>;
	// currentValues is exposed so the parent (Task 20 shell) can read it for the save bar.
	// It is derived reactively from originalValues + pendingValues for this area.
	export let currentValues: Record<string, unknown> = {};

	const AREA = 'notifications' as const;

	const subs = SETTINGS_SUBSECTIONS.filter((s) => s.area === AREA);
	const areaEntries = SETTINGS_MANIFEST.filter((e) => e.area === AREA);
	const entriesBySub: Record<string, SettingsEntry[]> = Object.fromEntries(
		subs.map((s) => [s.id, areaEntries.filter((e) => e.subsection === s.id)]),
	);

	function readByPath(obj: unknown, path: string): unknown {
		return path
			.split('.')
			.reduce<any>((cursor, key) => (cursor == null ? undefined : cursor[key]), obj);
	}

	function initialValue(entry: SettingsEntry): unknown {
		// Settings blob is FLAT — backendSection is only a routing label, not a storage key.
		// Read directly against the settings object using backendPath.
		const v = readByPath(settings, entry.backendPath);
		return v === undefined ? entry.default : v;
	}

	// Seed original/current on mount so parent and dirty tracking have a baseline.
	onMount(() => {
		const origSeed: Record<string, unknown> = {};
		for (const e of areaEntries) origSeed[e.id] = initialValue(e);
		originalValues.update((o) => ({ ...o, ...origSeed }));
	});

	// Reactive derivation: currentValues = originals + pending (pending wins).
	// Keep bound to the parent by reassigning the object (triggers reactivity).
	$: {
		const originals: Record<string, unknown> = {};
		for (const e of areaEntries) originals[e.id] = initialValue(e);
		const pend = $pendingValues;
		const areaPending: Record<string, unknown> = {};
		for (const e of areaEntries) {
			if (e.id in pend) areaPending[e.id] = pend[e.id];
		}
		currentValues = { ...currentValues, ...originals, ...areaPending };
	}

	function displayValue(entry: SettingsEntry): unknown {
		const pend = $pendingValues;
		if (entry.id in pend) return pend[entry.id];
		return initialValue(entry);
	}

	// Secrets never round-trip from the backend, so the input is always empty
	// on reload. Map each secret field to its sibling `_configured` flag so
	// the row can render a "Saved" badge.
	const CONFIGURED_FLAG_BY_BACKEND_PATH: Record<string, string> = {
		discord_bot_token: 'discord_bot_token_configured',
		discord_webhook_url: 'discord_webhook_configured',
	};

	function isConfigured(entry: SettingsEntry): boolean {
		if (entry.type !== 'secret') return false;
		const flag = CONFIGURED_FLAG_BY_BACKEND_PATH[entry.backendPath];
		if (!flag) return false;
		return Boolean((settings as Record<string, unknown>)[flag]);
	}
</script>

<div class="space-y-6">
	<!-- Per-event pop-up / Discord switches and the sidebar badges (saved as they
	     are flipped); the Discord transport and master switch follow below. -->
	<NotificationChannelsCard {settings} />

	{#each subs as sub (sub.id)}
		{@const entries = entriesBySub[sub.id] ?? []}
		{@const usedBy = [...new Set(entries.flatMap((e) => e.usedBy))]}
		<SettingsSubsection
			label={sub.label}
			description={sub.description ?? ''}
			deepLinkTo={sub.deepLinkTo}
			{usedBy}
		>
			{#if sub.advanced}<SettingsAdvancedHeader />{/if}
			{#each entries as entry (entry.id)}
				<SettingsFieldRow
					id={entry.id}
					label={entry.label}
					description={entry.description}
					unit={entry.unit}
					defaultValue={entry.default}
					value={displayValue(entry)}
					type={entry.type}
					options={entry.options ?? []}
					configured={isConfigured(entry)}
				/>
			{/each}
		</SettingsSubsection>
	{/each}
</div>
