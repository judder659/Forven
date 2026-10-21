<script lang="ts">
	import { onMount } from 'svelte';
	import { dirtyFields, markField } from '$lib/settings/dirty';
	import { formatEffective } from '$lib/settings/effective';
	import { differsFromDefault } from '$lib/settings/values';
	import { describeRange, numberBounds, outOfRange } from '$lib/settings/bounds';

	export let id: string;
	export let label: string;
	export let description: string;
	export let unit: string | undefined = undefined;
	// Opt-in live helper: 'years' shows "≈ N.Ny" next to a day-valued number input.
	export let valueHint: 'years' | undefined = undefined;
	export let defaultValue: unknown;
	export let value: unknown;
	export let type: 'number' | 'text' | 'toggle' | 'select' | 'secret' | 'csv' = 'text';
	export let options: Array<{ value: string; label: string }> = [];
	// For `secret` fields: backend never returns the raw value, so the input
	// always reads empty after reload. `configured` is a sibling boolean from
	// the settings blob (e.g. discord_bot_token_configured) used to render a
	// "Saved" badge so users can see a credential is persisted.
	export let configured: boolean = false;
	// RISK-BOUND-1: when the engine replaces the saved value (e.g. a cap locked
	// on mainnet), the value actually in force and why.
	export let inForce: { effective: unknown; reason: string } | null = null;
	// Changes what live orders do with real money.
	export let realMoney = false;

	// The description, default and range sit behind the (?) beside the name:
	// shown on hover or focus, and pinned open by a click.
	let helpOpen = false;
	let helpEl: HTMLElement | null = null;

	function closeHelpOutside(e: MouseEvent): void {
		if (helpOpen && helpEl && !helpEl.contains(e.target as Node)) helpOpen = false;
	}

	$: dirty = $dirtyFields.has(id);
	$: bounds = type === 'number' ? numberBounds(id) : undefined;
	$: rangeText = describeRange(bounds);
	$: invalid = outOfRange(value, bounds);
	$: showSavedBadge = type === 'secret' && configured && !dirty;
	$: selectedValues = arrayValue(value);
	$: changed = differsFromDefault({ id, type, default: defaultValue } as never, value);
	$: defaultText = describeValue(defaultValue);

	function describeValue(v: unknown): string {
		if (v === null || v === undefined || v === '') return 'empty';
		if (typeof v === 'boolean') return v ? 'On' : 'Off';
		if (Array.isArray(v)) return v.length ? v.join(', ') : 'none';
		const option = options.find((o) => o.value === v);
		if (option) return option.label;
		return `${v}${unit ? ` ${unit}` : ''}`;
	}

	function resetToDefault(): void {
		value = defaultValue;
		markField(id, defaultValue);
	}
	$: yearHint =
		valueHint === 'years' && type === 'number' && value != null && Number(value) > 0
			? `≈ ${(Number(value) / 365).toFixed(Number(value) / 365 >= 1 ? 1 : 2)}y`
			: '';

	onMount(() => {
		if (
			type === 'select' &&
			options.length > 0 &&
			!options.some((o) => o.value === value)
		) {
			console.warn(
				`SettingsFieldRow: select '${id}' mounted with unmatched value ${JSON.stringify(
					value,
				)} — pick one of ${options.map((o) => o.value).join(', ')}`,
			);
		}
	});

	function handleNumberOrTextInput(e: Event) {
		const el = e.target as HTMLInputElement;
		const raw = el.value;
		if (type === 'number') {
			if (raw === '') {
				value = null;
				markField(id, null);
				return;
			}
			const coerced = Number(raw);
			if (Number.isNaN(coerced)) {
				value = null;
				markField(id, null);
				return;
			}
			value = coerced;
			markField(id, coerced);
			return;
		}
		value = raw;
		markField(id, raw);
	}

	function handleSelect(e: Event) {
		const el = e.target as HTMLSelectElement;
		value = el.value;
		markField(id, el.value);
	}

	function handleToggle(e: Event) {
		const checked = (e.target as HTMLInputElement).checked;
		value = checked;
		markField(id, checked);
	}

	function handleCsvInput(e: Event) {
		const raw = (e.target as HTMLInputElement).value;
		const parts = raw
			.split(',')
			.map((s) => s.trim())
			.filter((s) => s.length > 0);
		value = parts;
		markField(id, parts);
	}

	function handleCsvCheckbox(e: Event, optionValue: string) {
		const checked = (e.target as HTMLInputElement).checked;
		const selected = new Set(arrayValue(value));
		if (checked) selected.add(optionValue);
		else selected.delete(optionValue);
		const ordered = options
			.map((option) => option.value)
			.filter((candidate) => selected.has(candidate));
		value = ordered;
		markField(id, ordered);
	}

	function arrayValue(v: unknown): string[] {
		if (Array.isArray(v)) return v.map((item) => String(item));
		if (typeof v === 'string') {
			return v
				.split(',')
				.map((s) => s.trim())
				.filter((s) => s.length > 0);
		}
		return [];
	}

	function csvDisplay(v: unknown): string {
		if (Array.isArray(v)) return v.join(', ');
		if (typeof v === 'string') return v;
		return '';
	}
</script>

<svelte:window on:click={closeHelpOutside} on:keydown={(e) => { if (e.key === 'Escape') helpOpen = false; }} />

<div class="flex flex-col gap-1 py-3 border-b border-sc-line">
	<div class="flex items-center justify-between gap-3">
		<span class="group/help relative flex min-w-0 items-center gap-1.5" bind:this={helpEl}>
		<label for={id} title="Setting ID: {id}" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 flex flex-wrap items-center gap-2">
			{label}
			{#if realMoney}
				<span
					class="rounded border border-red-800/70 bg-red-950/30 px-1 text-[10px] text-red-300"
					data-testid="real-money-{id}"
				>Real money</span>
			{/if}
			{#if dirty}
				<span
					data-testid="dirty-dot-{id}"
					class="w-1.5 h-1.5 rounded-full bg-yellow-400"
					aria-label="unsaved"
				></span>
			{/if}
		</label>
		<button
			type="button"
			class="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-sc-line2 text-[10px] leading-none text-sc-ink3 hover:border-sc-ink3 hover:text-sc-ink focus:border-sc-ink3 focus:text-sc-ink focus:outline-none"
			aria-label="What is {label}?"
			aria-expanded={helpOpen}
			aria-controls="{id}-help"
			data-testid="help-{id}"
			on:click={() => (helpOpen = !helpOpen)}
		>?</button>
		<div
			id="{id}-help"
			role="tooltip"
			class="absolute left-0 top-full z-30 mt-1 w-80 max-w-[80vw] flex-col gap-1.5 rounded-md border border-sc-line2 bg-sc-panel p-3 shadow-lg group-hover/help:flex group-focus-within/help:flex {helpOpen ? 'flex' : 'hidden'}"
			data-testid="help-text-{id}"
		>
			<p class="m-0 text-xs normal-case tracking-normal text-sc-ink2">{description}</p>
			<p class="m-0 flex flex-wrap items-center gap-x-2 text-[10px] text-sc-ink3">
				<span>Default: {defaultText}</span>
				{#if rangeText}<span>· Range: <span data-testid="range-{id}">{rangeText}{unit ? ` ${unit}` : ''}</span></span>{/if}
			</p>
		</div>
		</span>
		<div class="flex items-center gap-2">
			{#if inForce}
				<span class="text-[10px] uppercase tracking-[0.08em] text-sc-ink3">Saved</span>
			{/if}
			{#if type === 'toggle'}
				<input {id} type="checkbox" checked={!!value} on:change={handleToggle} />
			{:else if type === 'select'}
				<select
					{id}
					value={value as string}
					on:change={handleSelect}
					class="bg-sc-panel2 border border-sc-line2 text-sc-ink px-2 py-1 rounded text-sm"
				>
					{#each options as opt}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			{:else if type === 'secret'}
				{#if showSavedBadge}
					<span
						data-testid="saved-badge-{id}"
						class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-emerald-400 border border-emerald-700/50 bg-emerald-900/20 rounded px-1.5 py-0.5"
						aria-label="credential saved"
					>✓ Saved</span>
				{/if}
				<input
					{id}
					type="password"
					value={value as string}
					placeholder={showSavedBadge ? '•••••••• (saved, type to replace)' : ''}
					on:input={handleNumberOrTextInput}
					class="bg-sc-panel2 border border-sc-line2 text-sc-ink px-2 py-1 rounded text-sm w-48 placeholder:text-sc-ink4"
				/>
			{:else if type === 'number'}
				<input
					{id}
					type="number"
					value={value as number}
					min={bounds?.min}
					max={bounds?.max}
					step={bounds?.step ?? 'any'}
					aria-invalid={invalid || undefined}
					aria-describedby={invalid ? `${id}-range` : undefined}
					on:input={handleNumberOrTextInput}
					class="bg-sc-panel2 border text-sc-ink px-2 py-1 rounded text-sm w-32 {invalid ? 'border-[#e7b24a]' : 'border-sc-line2'}"
				/>
			{:else if type === 'csv'}
				{#if options.length > 0}
					<div class="flex max-w-[42rem] flex-wrap justify-end gap-2">
						{#each options as opt}
							<label
								for={`${id}-${opt.value}`}
								class="inline-flex items-center gap-1 rounded border border-sc-line2 bg-sc-panel2 px-2 py-1 text-xs text-sc-ink"
							>
								<input
									id={`${id}-${opt.value}`}
									type="checkbox"
									checked={selectedValues.includes(opt.value)}
									on:change={(event) => handleCsvCheckbox(event, opt.value)}
								/>
								<span>{opt.label}</span>
							</label>
						{/each}
					</div>
				{:else}
					<input
						{id}
						type="text"
						value={csvDisplay(value)}
						on:input={handleCsvInput}
						class="terminal-input w-96"
					/>
				{/if}
			{:else}
				<input
					{id}
					type="text"
					value={value as string}
					on:input={handleNumberOrTextInput}
					class="terminal-input w-48"
				/>
			{/if}
			{#if unit}<span class="text-xs text-sc-ink3">{unit}</span>{/if}
			{#if yearHint}<span class="text-xs text-sc-ink3" data-testid="value-hint-{id}">{yearHint}</span>{/if}
		</div>
	</div>
	{#if inForce}
		<div
			class="flex flex-wrap items-baseline justify-between gap-2 rounded border border-[#e7b24a]/50 bg-[#e7b24a]/10 px-3 py-2"
			data-testid="in-force-{id}"
		>
			<span class="text-xs text-sc-ink2">The engine uses <span class="text-sm font-semibold text-sc-ink">{formatEffective(inForce.effective)}{unit ? ` ${unit}` : ''}</span></span>
			<span class="text-[11px] text-[#e7b24a]">{inForce.reason}. Your saved value is not used.</span>
		</div>
	{/if}
	{#if invalid}
		<p id="{id}-range" class="text-xs text-[#e7b24a]" data-testid="out-of-range-{id}">
			Must be {rangeText}{unit ? ` ${unit}` : ''}.{bounds?.enforced ? ' The save will not keep this value.' : ''}
		</p>
	{/if}
	{#if changed}
		<p class="m-0 flex flex-wrap items-center gap-x-2 text-[10px] text-sc-ink3">
			<span class="text-sc-ink2" data-testid="changed-{id}">Changed from {defaultText}</span>
			<button
				type="button"
				class="underline hover:text-sc-ink"
				on:click={resetToDefault}
				data-testid="reset-{id}"
			>Use default</button>
		</p>
	{/if}
</div>
