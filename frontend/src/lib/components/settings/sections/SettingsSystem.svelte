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
	import UpdatePanel from '$lib/components/settings/UpdatePanel.svelte';
	import { markField, originalValues, pendingValues } from '$lib/settings/dirty';

	export let settings: Record<string, unknown>;
	// currentValues is exposed so the parent (Task 20 shell) can read it for the save bar.
	// It is derived reactively from originalValues + pendingValues for this area.
	export let currentValues: Record<string, unknown> = {};
	export let variant: 'default' | 'wizard' = 'default';
	export let visibleSubsections: string[] | null = null;

	const AREA = 'system' as const;

	const allSubs = SETTINGS_SUBSECTIONS.filter((s) => s.area === AREA);
	$: subs = variant === 'wizard' && visibleSubsections
		? allSubs.filter((s) => visibleSubsections!.includes(s.id))
		: allSubs;
	const areaEntries = SETTINGS_MANIFEST.filter((e) => e.area === AREA);
	$: entriesBySub = Object.fromEntries(
		subs.map((s) => [s.id, areaEntries.filter((e) => e.subsection === s.id)]),
	) as Record<string, SettingsEntry[]>;

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
	// The field rows MUST render from this object (not from a helper function
	// that reads $pendingValues internally — invisible to the compiler, so
	// programmatic fills like the preset dial would never re-render the rows).
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

	// --- Throughput preset dial: fills real knob values; active name is DERIVED ---
	// Unlike the Lab stance preset, NOTHING named is persisted. Picking a preset
	// fills the real knob fields (saved through the normal save bar), and the
	// active name is re-derived by comparing current values against the
	// backend-provided bundles (settings.throughput_presets, same maps the backend
	// uses for `throughput_preset_effective`) — so hand-editing back onto a bundle
	// reads as that preset again, and no stored name can ever lie about the
	// effective values.
	const THROUGHPUT_PRESET_ENTRY_IDS = [
		'bot-operations.ideation_interval_minutes',
		'bot-operations.coding_interval_minutes',
		'bot-operations.testing_interval_minutes',
		'bot-operations.graduation_interval_minutes',
		'bot-operations.agent_task_claim_limit',
		'bot-operations.backtest_subprocess_budget',
		'bot-operations.gauntlet_drain_workers',
		'research.crucible_daily_develop_budget',
	];
	const throughputEntries = areaEntries.filter((e) =>
		THROUGHPUT_PRESET_ENTRY_IDS.includes(e.id),
	);
	// Outcome-phrased blurbs: the develop budget is the dominant AI-call-volume
	// driver, so each preset is described by what it spends, not what it sets.
	const THROUGHPUT_PRESET_META: Array<{ name: string; label: string; blurb: string }> = [
		{
			name: 'trickle',
			label: 'Trickle',
			blurb: '≈20 develops/day, hours-scale agent cadence, single-file workers — for hard-capped free models.',
		},
		{
			name: 'conserve',
			label: 'Conserve',
			blurb: '≈60 develops/day, slowed cadence, small batches — for free or rate-limited routes; the fix if you are seeing provider 429s.',
		},
		{
			name: 'balanced',
			label: 'Balanced',
			blurb: '≈150 develops/day — the shipped defaults for a normal paid provider key.',
		},
		{
			name: 'max',
			label: 'Max',
			blurb: '≈500 develops/day, minutes-scale cadence, all workers up — requires a high-limit paid or local model; a free route will rate-limit instantly.',
		},
	];

	// Bundle keys are the flat backend key names — the tail of each backendPath.
	function throughputKeyOf(entry: SettingsEntry): string {
		const parts = entry.backendPath.split('.');
		return parts[parts.length - 1];
	}

	$: throughputBundles = ((settings?.throughput_presets as Record<string, Record<string, number>>) ??
		{}) as Record<string, Record<string, number>>;

	function deriveThroughputPreset(
		pend: Record<string, unknown>,
		bundles: Record<string, Record<string, number>>,
	): string {
		for (const meta of THROUGHPUT_PRESET_META) {
			const bundle = bundles[meta.name];
			if (!bundle) continue;
			const matches =
				throughputEntries.length > 0 &&
				throughputEntries.every((e) => {
					const current = e.id in pend ? pend[e.id] : initialValue(e);
					return Number(current) === Number(bundle[throughputKeyOf(e)]);
				});
			if (matches) return meta.name;
		}
		return 'custom';
	}
	// Reference $pendingValues DIRECTLY so Svelte tracks the dependency (a function
	// that reads the store internally is invisible to the compiler and goes stale).
	$: activeThroughputPreset = deriveThroughputPreset($pendingValues, throughputBundles);
	$: activeThroughputMeta = THROUGHPUT_PRESET_META.find(
		(m) => m.name === activeThroughputPreset,
	);

	// Fills every bundle knob synchronously (form updates live, before any save).
	function applyThroughputPreset(name: string): void {
		const bundle = throughputBundles[name];
		if (!bundle) return;
		for (const e of throughputEntries) {
			const v = bundle[throughputKeyOf(e)];
			if (v !== undefined) markField(e.id, v);
		}
	}

	$: schedulerControlOff = settings?.throughput_auto_scheduler_control === false;
</script>

<div class="space-y-6">
	{#if variant !== 'wizard'}
		<UpdatePanel />
	{/if}
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
			{#if sub.id === 'system-throughput'}
				<div
					data-testid="throughput-preset-dial"
					class="border border-[#222] bg-[#0d0d0d] p-3 mb-3 space-y-2"
				>
					<div class="flex items-center justify-between gap-3">
						<label for="throughput-preset-select" class="text-sm text-[#888]"
							>Throughput preset</label
						>
						<select
							id="throughput-preset-select"
							class="terminal-select"
							value={activeThroughputPreset}
							on:change={(e) => applyThroughputPreset((e.target as HTMLSelectElement).value)}
						>
							{#each THROUGHPUT_PRESET_META as m (m.name)}
								<option value={m.name}>{m.label}</option>
							{/each}
							<option value="custom" disabled>Custom</option>
						</select>
					</div>
					<p class="text-xs text-[#666]">
						{activeThroughputMeta
							? activeThroughputMeta.blurb
							: 'Custom — hand-tuned values. Picking a preset fills every throughput knob and the resource-tuning workers; nothing is saved until you hit Save.'}
					</p>
					{#if schedulerControlOff}
						<p class="text-xs text-yellow-400">
							Auto scheduler control is off — interval values are stored but not applied to
							running jobs.
						</p>
					{/if}
					<p class="text-[10px] text-[#555]">
						Also sets the resource-tuning workers below. Does not change headless agent
						concurrency (env FORVEN_HEADLESS_AGENT_CONCURRENCY, applies on restart).
					</p>
				</div>
			{/if}
			{#each entries as entry (entry.id)}
				<SettingsFieldRow
					id={entry.id}
					label={entry.label}
					description={entry.description}
					unit={entry.unit}
					defaultValue={entry.default}
					value={currentValues[entry.id]}
					type={entry.type}
					options={entry.options ?? []}
				/>
			{/each}
		</SettingsSubsection>
	{/each}
</div>
