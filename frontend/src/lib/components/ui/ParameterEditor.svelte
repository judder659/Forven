<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import {
		cloneParameterRecord,
		detectParameterValueKind,
		parseParameterValue,
		serializeParameterValue,
		type ParameterValueKind,
	} from '$lib/utils/parameterEditor';

	export let params: Record<string, unknown> = {};
	export let saving = false;
	/**
	 * Bindable: true when any field currently holds invalid input. The editor keeps the
	 * last VALID value in `params` while an invalid field shows its error, so consumers
	 * must check this before persisting/running to avoid silently using stale values.
	 */
	export let hasErrors = false;

	const dispatch = createEventDispatcher<{
		paramsChange: Record<string, unknown>;
	}>();

	let jsonBuffers: Record<string, string> = {};
	let fieldErrors: Record<string, string> = {};
	$: hasErrors = Object.values(fieldErrors).some(Boolean);

	function prettyLabel(key: string): string {
		return key
			.replace(/[_-]+/g, ' ')
			.replace(/\s+/g, ' ')
			.trim()
			.replace(/\b\w/g, (match) => match.toUpperCase());
	}

	function parameterDescription(key: string, kind: ParameterValueKind): string {
		const normalized = key.trim().toLowerCase();
		if (normalized === 'fast') return 'Short lookback used for the fast leg of the signal.';
		if (normalized === 'slow') return 'Longer lookback used as the slower confirmation leg.';
		if (normalized === 'signal') return 'Smoothing period that confirms crosses and reduces noise.';
		if (normalized.includes('overbought')) return 'Upper threshold that marks stretched momentum conditions.';
		if (normalized.includes('oversold')) return 'Lower threshold that marks exhausted downside momentum.';
		if (normalized.includes('stop_loss') || normalized === 'sl') return 'Loss cap applied when a trade moves against the thesis.';
		if (normalized.includes('take_profit') || normalized === 'tp') return 'Profit target applied when the trade reaches its objective.';
		if (normalized.includes('risk')) return 'Risk or position-sizing control used by the strategy.';
		if (normalized.includes('threshold')) return 'Decision boundary that determines when the rule fires.';
		if (normalized.includes('lookback') || normalized.includes('window') || normalized.includes('period') || normalized.includes('length')) {
			return 'History depth used before the strategy emits a signal.';
		}
		if (normalized.includes('entry')) {
			return kind === 'json'
				? 'Structured entry logic sent directly to the backtest engine.'
				: 'Entry-side setting used when opening new trades.';
		}
		if (normalized.includes('exit')) {
			return kind === 'json'
				? 'Structured exit logic sent directly to the backtest engine.'
				: 'Exit-side setting used when closing active trades.';
		}
		if (normalized.includes('indicator')) {
			return kind === 'json'
				? 'Indicator definitions and their tuning values for this draft.'
				: 'Indicator-level setting used inside the strategy.';
		}
		if (normalized.includes('note')) return 'Free-form context saved alongside the working draft.';
		if (kind === 'boolean') return 'Toggle this rule on or off for the next run.';
		if (kind === 'json') return 'Structured configuration block that will be passed through as JSON.';
		if (kind === 'number') return 'Numeric tuning value used directly by the strategy logic.';
		return 'Free-form value passed through with the current draft.';
	}

	function rowsForJsonBuffer(value: string): number {
		const lines = value.split(/\r?\n/).length;
		return Math.min(Math.max(lines + 1, 6), 14);
	}

	function kindSortOrder(kind: ParameterValueKind): number {
		if (kind === 'number') return 0;
		if (kind === 'string') return 1;
		if (kind === 'boolean') return 2;
		return 3;
	}

	function syncJsonBuffers(): void {
		const nextBuffers: Record<string, string> = {};
		for (const [key, value] of Object.entries(params)) {
			if (detectParameterValueKind(value) === 'json') {
				nextBuffers[key] = jsonBuffers[key] ?? serializeParameterValue(value);
			}
		}
		jsonBuffers = nextBuffers;
	}

	function updatePrimitive(key: string, rawValue: string, kind: ParameterValueKind): void {
		const parsed = parseParameterValue(rawValue, kind);
		fieldErrors = {
			...fieldErrors,
			[key]: parsed.error ?? '',
		};
		if (!parsed.error) {
			params = {
				...params,
				[key]: parsed.value,
			};
			dispatch('paramsChange', params);
		}
	}

	function updateBoolean(key: string, checked: boolean): void {
		params = {
			...params,
			[key]: checked,
		};
		dispatch('paramsChange', params);
	}

	function updateJsonBuffer(key: string, rawValue: string): void {
		jsonBuffers = {
			...jsonBuffers,
			[key]: rawValue,
		};
		const parsed = parseParameterValue(rawValue, 'json');
		fieldErrors = {
			...fieldErrors,
			[key]: parsed.error ?? '',
		};
		if (!parsed.error) {
			params = {
				...params,
				[key]: cloneParameterRecord(parsed.value as Record<string, unknown>),
			};
			dispatch('paramsChange', params);
		}
	}

	$: paramEntries = Object.entries(params).sort(([leftKey, leftValue], [rightKey, rightValue]) => {
		const kindDiff = kindSortOrder(detectParameterValueKind(leftValue)) - kindSortOrder(detectParameterValueKind(rightValue));
		if (kindDiff !== 0) return kindDiff;
		return leftKey.localeCompare(rightKey);
	});
	$: syncJsonBuffers();
</script>

{#if paramEntries.length === 0}
	<div class="border border-dashed border-[#333] px-4 py-5 text-center">
		<div class="text-xs font-bold uppercase tracking-widest text-white">No editable parameters yet</div>
		<div class="mt-1 text-[11px] text-[#666]">
			This strategy does not currently expose a default parameter set for the draft editor.
		</div>
	</div>
{:else}
	<div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
		{#each paramEntries as [key, value]}
			{@const kind = detectParameterValueKind(value)}
			<div
				class={`border bg-[#050505] p-2 ${
					kind === 'json'
						? 'sm:col-span-2 xl:col-span-3 border-[#333]'
						: 'border-[#222]'
				}`}
			>
				<div class="flex items-center justify-between gap-2">
					<div class="text-[10px] uppercase tracking-wider text-[#888]">{prettyLabel(key)}</div>
					<span class="border border-[#333] px-1.5 py-0.5 font-mono text-[9px] text-[#555]">{key}</span>
				</div>
				<div class="mt-1.5">
					{#if kind === 'boolean'}
						<label class={`flex cursor-pointer items-center justify-between gap-3 border border-[#333] bg-black px-2.5 py-1.5 text-xs ${saving ? 'cursor-not-allowed opacity-60' : ''}`}>
							<span class="text-[#888]">{value ? 'Enabled' : 'Disabled'}</span>
							<!--
								The checkbox is the real focusable control (peer). It is visually
								collapsed but remains keyboard-operable and focus-visible — the
								styled track below reacts to peer-focus-visible with a ring so
								tab focus is clearly indicated without changing the visual switch.
							-->
							<input
								type="checkbox"
								class="peer h-0 w-0 opacity-0"
								role="switch"
								aria-label={`${prettyLabel(key)} ${value ? 'enabled' : 'disabled'}`}
								aria-checked={Boolean(value)}
								checked={Boolean(value)}
								on:change={(event) => updateBoolean(key, (event.currentTarget as HTMLInputElement).checked)}
								disabled={saving}
							/>
							<span class={`relative inline-flex h-5 w-9 items-center border transition-colors peer-focus-visible:ring-1 peer-focus-visible:ring-white peer-focus-visible:ring-offset-1 peer-focus-visible:ring-offset-black ${value ? 'border-white bg-white/20' : 'border-[#333] bg-black'}`}>
								<span class={`inline-block h-3.5 w-3.5 ${value ? 'bg-white' : 'bg-[#555]'} transition ${value ? 'translate-x-4' : 'translate-x-0.5'}`}></span>
							</span>
						</label>
					{:else if kind === 'json'}
						<div class={`border ${fieldErrors[key] ? 'border-red-900' : 'border-[#333]'} bg-black p-2 transition-colors focus-within:border-white`}>
							<textarea
								rows={rowsForJsonBuffer(jsonBuffers[key] ?? serializeParameterValue(value))}
								class="w-full resize-y bg-transparent font-mono text-xs leading-5 text-white outline-none placeholder:text-[#444] disabled:opacity-40"
								value={jsonBuffers[key] ?? serializeParameterValue(value)}
								on:input={(event) => updateJsonBuffer(key, (event.currentTarget as HTMLTextAreaElement).value)}
								disabled={saving}
							></textarea>
						</div>
					{:else}
						<div class={`border ${fieldErrors[key] ? 'border-red-900' : 'border-[#333]'} bg-black px-2.5 py-1.5 transition-colors focus-within:border-white`}>
							<input
								type={kind === 'number' ? 'number' : 'text'}
								step={kind === 'number' ? 'any' : undefined}
								class="w-full bg-transparent text-sm text-white outline-none placeholder:text-[#444] disabled:opacity-40"
								value={serializeParameterValue(value)}
								on:input={(event) => updatePrimitive(key, (event.currentTarget as HTMLInputElement).value, kind)}
								disabled={saving}
							/>
						</div>
					{/if}
				</div>
				{#if fieldErrors[key]}
					<div class="mt-1.5 border border-red-900 bg-red-500/5 px-2 py-1 text-[11px] text-red-400">
						{fieldErrors[key]}
					</div>
				{/if}
			</div>
		{/each}
	</div>
{/if}
