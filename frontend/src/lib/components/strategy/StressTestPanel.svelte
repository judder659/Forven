<script lang="ts">
	// Stress test: every knob and indicator setting nudged -25%, -10%, +10% and
	// +25%, each re-run as the backtest would run it. A rule whose out-of-sample
	// result survives small nudges is less likely to be a fitted accident.
	import { createEventDispatcher } from 'svelte';
	import type { SensitivityResult } from '$lib/api';
	import { formatValue } from '$lib/utils/ruleLabels';

	export let result: SensitivityResult | null = null;
	export let loading = false;
	export let error = '';
	/** The strategy or its settings changed since this result was computed. */
	export let stale = false;
	export let canRun = true;

	const dispatch = createEventDispatcher<{ run: void }>();
	const STEPS = [-0.25, -0.1, 0.1, 0.25];
	const pct = (value: number) => `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;

	function tone(value: number, base: number): string {
		if (value <= 0) return 'bg-red-500/15 text-red-300';
		if (base > 0 && value < 0.5 * base) return 'bg-amber-500/10 text-amber-300';
		return 'bg-emerald-500/10 text-emerald-300';
	}

	$: base = result?.base ?? null;
	$: verdict = result?.verdict ?? null;
	$: badge = verdict?.status === 'stable' ? 'border-emerald-700 text-emerald-400'
		: verdict?.status === 'fragile' ? 'border-amber-700 text-amber-400' : 'border-red-800 text-red-400';
</script>

<div class="space-y-3">
	<div class="flex flex-wrap items-center gap-3">
		<button type="button" on:click={() => dispatch('run')} disabled={loading || !canRun}
			class="terminal-button-primary text-[12px] disabled:opacity-40">{loading ? 'Stress testing…' : result ? 'Run again' : 'Run stress test'}</button>
		<p class="min-w-0 flex-1 text-[11px] text-sc-ink3">
			Nudges every knob and indicator setting by −25%, −10%, +10% and +25% and re-runs each. Cells show the out-of-sample return.
		</p>
	</div>

	{#if error}
		<div class="border border-red-900 bg-red-500/5 px-3 py-1.5 text-[11px] text-red-400" role="alert">{error}</div>
	{/if}
	{#if stale && result}
		<div class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[11px] text-sc-ink2" role="status">The strategy changed since this stress test. Run it again to check the current version.</div>
	{/if}
	{#each result?.warnings ?? [] as warning}
		<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{warning}</div>
	{/each}

	{#if result && base && result.knobs.length}
		{#if verdict}
			<div class="flex items-baseline gap-2" data-testid="stress-verdict">
				<span class="border px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {badge}">{verdict.status}</span>
				<span class="text-[12px] text-sc-ink">{verdict.text}</span>
			</div>
		{/if}
		<div class="overflow-x-auto border border-sc-line">
			<table class="w-full text-[11px]">
				<thead class="bg-sc-panel font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
					<tr>
						<th class="px-2 py-1 text-left font-normal">Setting</th>
						{#each STEPS.slice(0, 2) as step}<th class="px-2 py-1 text-right font-normal">{pct(step).replace('.0', '')}</th>{/each}
						<th class="px-2 py-1 text-right font-normal text-sc-ink2">As is</th>
						{#each STEPS.slice(2) as step}<th class="px-2 py-1 text-right font-normal">{pct(step).replace('.0', '')}</th>{/each}
					</tr>
				</thead>
				<tbody>
					{#each result.knobs as knob (knob.label)}
						{@const cells = STEPS.map((step) => knob.variants.find((v) => Math.abs(v.step - step) < 1e-9))}
						<tr class="border-t border-sc-line">
							<td class="px-2 py-1 text-sc-ink">{knob.label} <span class="font-mono text-[10px] text-sc-ink3">{formatValue(knob.value)}</span></td>
							{#each cells.slice(0, 2) as cell}
								<td class="px-1 py-0.5 text-right">
									{#if cell}<span class="block px-1 py-0.5 font-mono {tone(cell.oos_return, base.oos_return)}" title={`${knob.label} = ${formatValue(cell.value)} · ${cell.oos_trades} out-of-sample trades`}>{pct(cell.oos_return)}</span>{:else}<span class="text-sc-ink4">—</span>{/if}
								</td>
							{/each}
							<td class="px-1 py-0.5 text-right"><span class="block px-1 py-0.5 font-mono text-sc-ink" title={`${base.oos_trades} out-of-sample trades`}>{pct(base.oos_return)}</span></td>
							{#each cells.slice(2) as cell}
								<td class="px-1 py-0.5 text-right">
									{#if cell}<span class="block px-1 py-0.5 font-mono {tone(cell.oos_return, base.oos_return)}" title={`${knob.label} = ${formatValue(cell.value)} · ${cell.oos_trades} out-of-sample trades`}>{pct(cell.oos_return)}</span>{:else}<span class="text-sc-ink4">—</span>{/if}
								</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<p class="text-[10px] text-sc-ink3">Green keeps at least half the out-of-sample result, amber keeps less, red loses money. Hover a cell for the value tried.</p>
	{:else if !result && !loading}
		<div class="border border-dashed border-sc-line px-3 py-6 text-center text-[12px] text-sc-ink3">
			A strategy that only works at one exact setting is usually fitted to noise. Run the stress test to see how its knobs hold up.
		</div>
	{/if}
</div>
