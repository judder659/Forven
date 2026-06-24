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
	import ResearchSettingsPanel from '$lib/components/settings/ResearchSettingsPanel.svelte';
	import {
		getSystemMode,
		setSystemMode,
		updateSettingsSection,
		type PausedManualCounts,
		type ResearchSettings,
		type SystemMode,
	} from '$lib/api';
	import { originalValues, pendingValues, dirtyFields, markField } from '$lib/settings/dirty';

	export let settings: Record<string, unknown>;
	// currentValues is exposed so the parent (Task 20 shell) can read it for the save bar.
	// It is derived reactively from originalValues + pendingValues for this area.
	export let currentValues: Record<string, unknown> = {};

	// Local draft for the ResearchSettingsPanel. The panel handles its own save flow
	// (emits 'save' / onsave) and does NOT participate in the pendingValues dirty graph.
	function defaultResearchSettings(): any {
		return {
			external_benchmarking_enabled: true,
			lane_weights: {
				exploration: 0.5,
				exploitation: 0.3,
				benchmarking: 0.2,
			},
			spawn_limits: {
				per_run: 2,
				rolling_window: 6,
				window_days: 7,
			},
			memory_modes: {
				exploration: { constraint_memory: true, inspiration_memory: 'optional' },
				exploitation: { constraint_memory: true, inspiration_memory: 'bounded' },
				benchmarking: { constraint_memory: true, inspiration_memory: 'none' },
			},
			allowed_external_source_types: [],
			research_sources: {
				reddit: {
					enabled: true,
					subs: ['algotrading', 'quant', 'options', 'thetagang', 'systematictrading'],
					client_id: null,
					client_secret: null,
					rate_limit_per_min: 30,
				},
			},
		};
	}

	function mergeResearchDraft(raw: unknown): any {
		const base = defaultResearchSettings();
		if (!raw || typeof raw !== 'object') return base;
		const src = raw as Record<string, unknown>;
		return {
			...base,
			...src,
			lane_weights: { ...base.lane_weights, ...(src.lane_weights as object ?? {}) },
			spawn_limits: { ...base.spawn_limits, ...(src.spawn_limits as object ?? {}) },
			memory_modes: { ...base.memory_modes, ...(src.memory_modes as object ?? {}) },
			research_sources: {
				...base.research_sources,
				...(src.research_sources as object ?? {}),
				reddit: {
					...base.research_sources.reddit,
					...(((src.research_sources as Record<string, unknown> | undefined)?.reddit as object) ?? {}),
				},
			},
			allowed_external_source_types: Array.isArray(src.allowed_external_source_types)
				? src.allowed_external_source_types
				: base.allowed_external_source_types,
		};
	}

	export let researchSettingsDraft: any = mergeResearchDraft(settings?.research_settings);

	let researchSaving = false;
	let researchBanner: { tone: 'success' | 'error'; message: string } | null = null;

	async function handleResearchSave(event: CustomEvent<ResearchSettings>): Promise<void> {
		researchSaving = true;
		researchBanner = null;
		try {
			await updateSettingsSection('research', { research_settings: event.detail });
			researchSettingsDraft = mergeResearchDraft(event.detail);
			researchBanner = { tone: 'success', message: 'Research settings saved.' };
			setTimeout(() => (researchBanner = null), 2500);
		} catch (err) {
			researchBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to save research settings.',
			};
		} finally {
			researchSaving = false;
		}
	}

	let systemMode: SystemMode = 'manual';
	let systemModeLoading = true;
	let systemModeSaving: SystemMode | null = null;
	let systemModeBanner: { tone: 'success' | 'error'; message: string } | null = null;
	let pausedManualCounts: PausedManualCounts = emptyPausedManualCounts();

	const SYSTEM_MODE_OPTIONS: {
		value: SystemMode;
		label: string;
		short: string;
		tagline: string;
		description: string;
	}[] = [
		{
			value: 'manual',
			label: 'Manual',
			short: 'Strict freeze',
			tagline: 'Only direct operator actions run',
			description:
				'All autonomous background work freezes. Scheduled jobs stop, queued autonomous tasks pause, and only direct operator actions can run until you leave manual mode.',
		},
		{
			value: 'semi_auto',
			label: 'Semi',
			short: 'User-initiated',
			tagline: 'You create crucibles; the system evaluates them',
			description:
				'The scanner and agents will NOT spawn new crucibles on their own. Crucibles you enter manually are fully processed by the research, Gauntlet, robustness, and lifecycle machinery. Live trading stays active.',
		},
		{
			value: 'auto',
			label: 'Auto',
			short: 'Fully autonomous',
			tagline: 'Original autonomous pipeline',
			description:
				'The scanner and agents autonomously generate, evaluate, and promote hypotheses. Live trading is active. Use only when the pipeline is healthy.',
		},
	];

	function emptyPausedManualCounts(): PausedManualCounts {
		return { agent_tasks: 0, brain_tasks: 0, total: 0 };
	}

	function normalizePausedManualCounts(value: unknown): PausedManualCounts {
		if (!value || typeof value !== 'object') return emptyPausedManualCounts();
		const raw = value as Record<string, unknown>;
		const agentTasks = Number(raw.agent_tasks ?? 0);
		const brainTasks = Number(raw.brain_tasks ?? 0);
		const total = Number(raw.total ?? agentTasks + brainTasks);
		return {
			agent_tasks: Number.isFinite(agentTasks) ? agentTasks : 0,
			brain_tasks: Number.isFinite(brainTasks) ? brainTasks : 0,
			total: Number.isFinite(total) ? total : 0,
		};
	}

	function pausedManualSummary(counts: PausedManualCounts): string {
		if (counts.total <= 0) return 'No queued autonomous work is currently paused.';
		if (counts.total === 1) return '1 queued autonomous task is currently paused.';
		return `${counts.total} queued autonomous tasks are currently paused.`;
	}

	async function loadSystemMode() {
		systemModeLoading = true;
		try {
			const result = await getSystemMode();
			systemMode = (result.system_mode as SystemMode) ?? 'manual';
			pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
		} catch (err) {
			systemModeBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to load system mode.',
			};
		} finally {
			systemModeLoading = false;
		}
	}

	async function handleSystemModeChange(next: SystemMode) {
		if (next === systemMode || systemModeSaving) return;
		systemModeSaving = next;
		systemModeBanner = null;
		try {
			const result = await setSystemMode(next);
			systemMode = (result.system_mode as SystemMode) ?? next;
			pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
			systemModeBanner = {
				tone: 'success',
				message: next === 'manual' ? `System mode set to manual. ${pausedManualSummary(pausedManualCounts)}` : `System mode set to ${next}.`,
			};
			setTimeout(() => (systemModeBanner = null), 2500);
		} catch (err) {
			systemModeBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to change system mode.',
			};
		} finally {
			systemModeSaving = null;
		}
	}

	const AREA = 'lab' as const;

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
		void loadSystemMode();
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

	// --- Stance preset: populate knobs on select + value-based "custom" flip -------
	// Picking a named preset fills every pipeline knob with that preset's RESOLVED
	// values (so the form updates live), and editing any knob away from the selected
	// preset's bundle flips the selector to "custom". Both use the backend-provided
	// `pipeline_presets` bundles (same display units as the main settings blob), so the
	// selector and the fields can't drift. The backend also re-applies a named preset
	// authoritatively (policy._normalize_pipeline_config), so a named stance wins over
	// any stored knobs while "custom" lets per-knob edits win.
	const PRESET_ID = 'pipeline.pipeline_preset';
	const presetEntry = areaEntries.find((e) => e.id === PRESET_ID);
	const PIPELINE_KNOB_ENTRIES = areaEntries.filter(
		(e) => e.backendSection === 'pipeline' && e.id !== PRESET_ID,
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
				(k) => dirty.has(k.id) && !sameValue(displayValue(k), readByPath(bundle, k.backendPath)),
			);
			if (editedAway) markField(PRESET_ID, 'custom');
		}
	}
</script>

<div class="space-y-6">
	<section
		data-testid="system-mode-card"
		class="border border-[#222] bg-[#0d0d0d] rounded p-4 space-y-3"
	>
		<header class="flex flex-wrap items-start justify-between gap-2">
			<div>
				<h2 class="text-sm font-bold uppercase tracking-wider text-white">
					System mode
				</h2>
				<p class="text-xs text-gray-400 mt-0.5">
					Controls how much of the pipeline runs on its own. Changes apply immediately.
				</p>
			</div>
			{#if systemModeLoading}
				<span class="text-[10px] uppercase tracking-wider text-gray-500">Loading…</span>
			{:else}
				<div class="text-right">
					<div class="text-[10px] uppercase tracking-wider text-gray-500">
						Current: <span class="text-white">{systemMode}</span>
					</div>
					{#if systemMode === 'manual'}
						<div class="mt-1 text-[10px] text-amber-300">{pausedManualSummary(pausedManualCounts)}</div>
					{/if}
				</div>
			{/if}
		</header>
		{#if systemModeBanner}
			<div
				class={`border px-3 py-2 text-xs ${
					systemModeBanner.tone === 'success'
						? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
						: 'border-rose-500/30 bg-rose-500/10 text-rose-200'
				}`}
			>
				{systemModeBanner.message}
			</div>
		{/if}
		<div class="grid grid-cols-1 md:grid-cols-3 gap-2">
			{#each SYSTEM_MODE_OPTIONS as option (option.value)}
				{@const active = systemMode === option.value}
				{@const saving = systemModeSaving === option.value}
				<button
					type="button"
					class={`text-left border rounded px-3 py-3 transition-colors ${
						active
							? option.value === 'auto'
								? 'border-red-600 bg-red-950/30 text-red-100'
								: option.value === 'semi_auto'
									? 'border-sky-600 bg-sky-950/30 text-sky-100'
									: 'border-amber-600 bg-amber-950/30 text-amber-100'
							: 'border-[#2a2a2a] bg-[#0a0a0a] text-gray-300 hover:border-[#444] hover:bg-[#111]'
					} ${saving ? 'opacity-60 cursor-wait' : ''}`}
					on:click={() => handleSystemModeChange(option.value)}
					disabled={systemModeLoading || saving}
					aria-pressed={active}
				>
					<div class="flex items-center justify-between mb-1">
						<span class="font-bold uppercase tracking-wider text-[11px]">{option.label}</span>
						{#if active}
							<span class="text-[10px] uppercase tracking-wider">Active</span>
						{:else if saving}
							<span class="text-[10px] uppercase tracking-wider">Saving…</span>
						{/if}
					</div>
					<div class="text-[11px] uppercase tracking-wider text-gray-400 mb-1">
						{option.short}
					</div>
					<p class="text-xs leading-relaxed text-gray-300">{option.description}</p>
				</button>
			{/each}
		</div>
	</section>
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
			{#if sub.id === 'lab-research'}
				{#if researchBanner}
					<div
						class={`mb-3 border px-3 py-2 text-xs ${
							researchBanner.tone === 'success'
								? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
								: 'border-rose-500/30 bg-rose-500/10 text-rose-200'
						}`}
					>
						{researchBanner.message}
					</div>
				{/if}
				<ResearchSettingsPanel
					bind:draft={researchSettingsDraft}
					saving={researchSaving}
					on:save={handleResearchSave}
				/>
			{:else if sub.id === 'lab-pipeline-preset' && presetEntry}
				<div class="flex items-center justify-between gap-3 py-3">
					<label for="pipeline-stance-select" class="text-sm text-gray-200">{presetEntry.label}</label>
					<select
						id="pipeline-stance-select"
						value={presetSelectValue}
						on:change={(e) => applyPreset((e.target as HTMLSelectElement).value)}
						class="bg-gray-900 border border-gray-700 text-white px-2 py-1 rounded text-sm"
					>
						{#each presetEntry.options ?? [] as opt}
							<option value={opt.value}>{opt.label}</option>
						{/each}
					</select>
				</div>
				<p class="text-xs text-gray-400 pb-3">{presetEntry.description}</p>
			{:else}
				{#each entries as entry (entry.id)}
					<SettingsFieldRow
						id={entry.id}
						label={entry.label}
						description={entry.description}
						unit={entry.unit}
						valueHint={entry.valueHint}
						defaultValue={entry.default}
						value={currentValues[entry.id]}
						type={entry.type}
						options={entry.options ?? []}
					/>
				{/each}
			{/if}
		</SettingsSubsection>
	{/each}
</div>
