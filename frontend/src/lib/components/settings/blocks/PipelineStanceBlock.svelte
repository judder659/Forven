<script lang="ts">
	// Stance preset picker for the Strategy pipeline area.
	import { SETTINGS_MANIFEST, type SettingsEntry } from '$lib/settings/manifest';
	import { dirtyFields, markField, pendingValues } from '$lib/settings/dirty';
	import { entryValue, readByPath } from '$lib/settings/values';

	export let settings: Record<string, unknown>;

	function initialValue(entry: SettingsEntry): unknown {
		return entryValue(settings, entry);
	}

	function displayValue(entry: SettingsEntry): unknown {
		const pend = $pendingValues;
		if (entry.id in pend) return pend[entry.id];
		return initialValue(entry);
	}

	// --- Stance preset: populate knobs on select + value-based "custom" flip -------
	// Picking a named preset fills every pipeline knob with that preset's RESOLVED
	// values (so the form updates live), and editing any knob away from the selected
	// preset's bundle flips the selector to "custom". Both use the backend-provided
	// `pipeline_presets` bundles (same display units as the main settings blob), so the
	// selector and the fields can't drift. The backend also re-applies a named preset
	// authoritatively (policy._normalize_pipeline_config), so a named stance wins over
	// any stored knobs while "custom" lets per-knob edits win.
	const PRESET_ID = 'pipeline.pipeline_preset';
	const presetEntry = SETTINGS_MANIFEST.find((e) => e.id === PRESET_ID);
	// The door into paper, the paper pool, the live-gate evidence rule and the
	// quick-screen rule, its widest window and its OOS floor are not part of any
	// stance bundle (2026-10 gate plan): picking a stance leaves them alone.
	const STANCE_INDEPENDENT_PREFIXES = [
		'paper_door.',
		'paper_pool.',
		'paper_trading.live_',
		'quick_screen.rule',
		'quick_screen.max_window_days',
		'quick_screen.min_oos_sharpe',
	];
	const PIPELINE_KNOB_ENTRIES = SETTINGS_MANIFEST.filter(
		(e) =>
			e.area === 'lab' &&
			e.backendSection === 'pipeline' &&
			e.id !== PRESET_ID &&
			!STANCE_INDEPENDENT_PREFIXES.some((prefix) => e.backendPath.startsWith(prefix)),
	);
	$: presetBundles = ((settings?.pipeline_presets as Record<string, any>) ?? {}) as Record<
		string,
		any
	>;

	function currentPreset(): string {
		return presetEntry ? String(displayValue(presetEntry) ?? 'default') : 'default';
	}
	function sameValue(a: unknown, b: unknown): boolean {
		if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
		if (Array.isArray(a) || Array.isArray(b)) return JSON.stringify(a) === JSON.stringify(b);
		return a === b;
	}

	// Picking a named stance fills every pipeline knob from its backend-resolved bundle
	// synchronously (so the form updates live, before any save). Called directly by the
	// stance <select>'s on:change below — no reactive timing involved.
	function applyPreset(value: string): void {
		markField(PRESET_ID, value);
		const bundle = presetBundles[value];
		if (value === 'custom' || !bundle) return;
		for (const knob of PIPELINE_KNOB_ENTRIES) {
			const v = readByPath(bundle, knob.backendPath);
			if (v !== undefined) markField(knob.id, v);
		}
	}
	// Reference $pendingValues DIRECTLY so Svelte tracks the dependency (a function that
	// reads the store internally is invisible to the compiler and would go stale).
	$: presetSelectValue =
		PRESET_ID in $pendingValues
			? String($pendingValues[PRESET_ID])
			: presetEntry
				? String(initialValue(presetEntry) ?? 'default')
				: 'default';

	$: {
		const dirty = $dirtyFields;
		const preset = currentPreset();
		const bundle = presetBundles[preset];
		if (preset !== 'custom' && bundle) {
			const editedAway = PIPELINE_KNOB_ENTRIES.some(
				(k) =>
					dirty.has(k.id) &&
					readByPath(bundle, k.backendPath) !== undefined &&
					!sameValue(displayValue(k), readByPath(bundle, k.backendPath)),
			);
			if (editedAway) markField(PRESET_ID, 'custom');
		}
	}
</script>

{#if presetEntry}
	<div class="flex items-center justify-between gap-3 py-3">
		<label for="pipeline-stance-select" class="text-sm text-sc-ink2">{presetEntry.label}</label>
		<select
			id="pipeline-stance-select"
			value={presetSelectValue}
			on:change={(e) => applyPreset((e.target as HTMLSelectElement).value)}
			class="terminal-select"
		>
			{#each presetEntry.options ?? [] as opt}
				<option value={opt.value}>{opt.label}</option>
			{/each}
		</select>
	</div>
	<p class="text-xs text-sc-ink3 pb-3">{presetEntry.description}</p>
{/if}
