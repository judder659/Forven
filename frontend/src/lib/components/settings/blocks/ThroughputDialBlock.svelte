<script lang="ts">
	// Throughput preset dial for AI & agents.
	import { SETTINGS_MANIFEST, type SettingsEntry } from '$lib/settings/manifest';
	import { markField, pendingValues } from '$lib/settings/dirty';
	import { entryValue } from '$lib/settings/values';

	export let settings: Record<string, unknown>;

	function initialValue(entry: SettingsEntry): unknown {
		return entryValue(settings, entry);
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
		'research.strategy_creation_daily_budget',
	];
	// The bundle spans two areas (AI & agents and System > Performance tuning),
	// so it reads the whole manifest; the save bar takes edits from any area.
	const throughputEntries = SETTINGS_MANIFEST.filter((e) =>
		THROUGHPUT_PRESET_ENTRY_IDS.includes(e.id),
	);
	// Outcome-phrased blurbs: the creation budget is the dominant AI-call-volume
	// driver, so each preset is described by what it spends, not what it sets.
	const THROUGHPUT_PRESET_META: Array<{ name: string; label: string; blurb: string }> = [
		{
			name: 'trickle',
			label: 'Trickle',
			blurb: '4 strategy-creation tasks/day, hours-scale agent cadence, single-file workers — for hard-capped free models.',
		},
		{
			name: 'conserve',
			label: 'Conserve',
			blurb: '12 strategy-creation tasks/day, slowed cadence, small batches — for free or rate-limited routes; the fix if you are seeing provider 429s.',
		},
		{
			name: 'balanced',
			label: 'Balanced',
			blurb: '40 strategy-creation tasks/day — the shipped defaults for a normal paid provider key.',
		},
		{
			name: 'max',
			label: 'Max',
			blurb: '120 strategy-creation tasks/day, minutes-scale cadence, all workers up — requires a high-limit paid or local model; a free route will rate-limit instantly.',
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

<div
	data-testid="throughput-preset-dial"
	class="rounded-md border border-sc-line bg-sc-panel2 p-3 mb-3 space-y-2"
>
	<div class="flex items-center justify-between gap-3">
		<label for="throughput-preset-select" class="text-sm text-sc-ink2"
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
	<p class="text-xs text-sc-ink3">
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
	<p class="text-[10px] text-sc-ink3">
		Also sets three workers under System, Performance tuning. Does not change headless agent
		concurrency (env FORVEN_HEADLESS_AGENT_CONCURRENCY, applies on restart).
	</p>
</div>
