<script lang="ts">
	import {
		DATE_RANGE_PRESETS,
		estimateBarCount,
		formatBarEstimate,
		formatDateWindowSummary,
		inferDateRangePreset,
		resolveDateRangePreset,
		type DateRangePresetId,
	} from '$lib/utils/dateRange';

	export let idPrefix = 'date-range';
	export let startDate = '';
	export let endDate = '';
	export let timeframe = '1h';
	export let title = 'Date range';
	export let description = 'Use a preset to move quickly, or switch to custom dates.';
	export let minDate = '';
	export let maxDate = '';
	export let accent: 'cyan' | 'blue' | 'violet' | 'amber' | 'rose' = 'cyan';

	// Today in UTC (YYYY-MM-DD). When no explicit maxDate is provided we cap the
	// date inputs at today so the End date can't be pushed into the future. This
	// guard only affects the <input max> ceiling — preset resolution still uses
	// the raw `maxDate` prop, so existing preset behavior is unchanged.
	const todayUtc = new Date().toISOString().slice(0, 10);
	$: maxCeiling = maxDate || todayUtc;

	// Terminal design language: accents collapse to the white/neutral scale.
	const accentStyles = {
		cyan: {
			active: 'border-sc-ink bg-sc-ink text-black',
			idle: 'border-sc-line2 bg-transparent text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink',
			meta: 'text-sc-ink2',
		},
		blue: {
			active: 'border-sc-ink bg-sc-ink text-black',
			idle: 'border-sc-line2 bg-transparent text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink',
			meta: 'text-sc-ink2',
		},
		violet: {
			active: 'border-sc-ink bg-sc-ink text-black',
			idle: 'border-sc-line2 bg-transparent text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink',
			meta: 'text-sc-ink2',
		},
		amber: {
			active: 'border-sc-ink bg-sc-ink text-black',
			idle: 'border-sc-line2 bg-transparent text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink',
			meta: 'text-sc-ink2',
		},
		rose: {
			active: 'border-sc-ink bg-sc-ink text-black',
			idle: 'border-sc-line2 bg-transparent text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink',
			meta: 'text-sc-ink2',
		},
	};

	function applyPreset(presetId: Exclude<DateRangePresetId, 'custom'>): void {
		const resolved = resolveDateRangePreset(presetId, {
			minDate: minDate || undefined,
			maxDate: maxDate || undefined,
		});
		startDate = resolved.startDate;
		endDate = resolved.endDate;
	}

	$: activePreset = inferDateRangePreset(startDate, endDate, {
		minDate: minDate || undefined,
		maxDate: maxDate || undefined,
	});
	$: barEstimate = estimateBarCount(startDate, endDate, timeframe);
	$: barEstimateLabel = formatBarEstimate(barEstimate);
	$: windowSummary = formatDateWindowSummary(startDate, endDate);
	$: styles = accentStyles[accent];
</script>

<div class="rounded-md border border-sc-line bg-sc-panel p-3">
	<div class="flex flex-wrap items-start justify-between gap-3">
		<div>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{title}</div>
			<div class="mt-1 text-[11px] text-sc-ink3">{description}</div>
		</div>
		<div class="flex flex-wrap items-center gap-2 text-[11px]">
			<span class={`border border-sc-line2 px-2 py-0.5 ${styles.meta}`}>
				{windowSummary}
			</span>
			<span class="border border-sc-line2 px-2 py-0.5 text-sc-ink3">
				{barEstimateLabel}
			</span>
		</div>
	</div>

	<div class="mt-3 flex flex-wrap gap-2">
		{#each DATE_RANGE_PRESETS as preset}
			<button
				type="button"
				class={`rounded-md border px-2.5 py-1 text-[11px] transition-colors ${activePreset === preset.id ? styles.active : styles.idle}`}
				on:click={() => applyPreset(preset.id)}
			>
				{preset.label}
			</button>
		{/each}
		{#if minDate}
			<button
				type="button"
				class={`rounded-md border px-2.5 py-1 text-[11px] transition-colors ${activePreset === 'max' ? styles.active : styles.idle}`}
				on:click={() => applyPreset('max')}
			>
				Max
			</button>
		{/if}
		<span class={`border px-2.5 py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] ${activePreset === 'custom' ? styles.active : 'border-sc-line2 text-sc-ink3'}`}>
			Custom
		</span>
	</div>

	<div class="mt-3 grid gap-3 md:grid-cols-2">
		<label class="block" for={`${idPrefix}-start`}>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Start</div>
			<input
				id={`${idPrefix}-start`}
				type="date"
				bind:value={startDate}
				min={minDate || undefined}
				max={endDate || maxCeiling}
				class="terminal-input mt-1.5"
			/>
		</label>
		<label class="block" for={`${idPrefix}-end`}>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">End</div>
			<input
				id={`${idPrefix}-end`}
				type="date"
				bind:value={endDate}
				min={startDate || minDate || undefined}
				max={maxCeiling}
				class="terminal-input mt-1.5"
			/>
		</label>
	</div>
</div>
