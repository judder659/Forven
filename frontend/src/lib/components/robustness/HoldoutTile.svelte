<script lang="ts">
	import { onMount } from 'svelte';

	import { getHoldoutSummary, submitHoldout, type HoldoutSummary } from '$lib/api/backtesting';

	export let strategyId: string;

	let summary: HoldoutSummary | null = null;
	let error = '';
	let submitting = false;

	const LABELS: Record<HoldoutSummary['state'], string> = {
		pass: 'PASSED',
		fail: 'FAILED',
		running: 'RUNNING',
		missing: 'NOT RUN YET',
		exempt: 'EXEMPT',
		budget_exhausted: 'WAITING FOR NEXT QUARTER',
		errored: 'ERRORED',
		off: 'OFF',
	};

	async function load(): Promise<void> {
		try {
			summary = await getHoldoutSummary(strategyId);
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function runTest(): Promise<void> {
		submitting = true;
		try {
			await submitHoldout(strategyId);
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			submitting = false;
		}
	}

	function badge(state: string): string {
		if (state === 'pass') return 'border-emerald-900 bg-emerald-500/10 text-emerald-400';
		if (state === 'fail') return 'border-red-900 bg-red-500/10 text-red-400';
		return 'border-[#333] text-[#888]';
	}

	function day(value: string | null | undefined): string {
		return value ? value.slice(0, 10) : '—';
	}

	function fractionPct(value: number | undefined): string {
		const pct = Number(value ?? 0) * 100;
		return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
	}

	onMount(load);

	$: result = summary?.latest?.result;
	$: oos = result?.out_of_sample;
	$: hurdle = result?.baseline_hurdle;
</script>

{#if summary?.enabled}
	<div class="mb-3 terminal-card px-4 py-3" data-testid="holdout-tile">
		<div class="flex flex-wrap items-center gap-2">
			<span class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Held-back test</span>
			<span class={`border px-1.5 py-0.5 text-[10px] font-bold ${badge(summary.state)}`}>{LABELS[summary.state] ?? summary.state}</span>
			<span class="text-[10px] text-[#555]">
				research data ends {day(summary.cutoff)} · one shot per config ·
				{summary.paper_mode === 'enforce' ? 'a pass is required for paper' : `paper gate: ${summary.paper_mode}`}
			</span>
		</div>

		{#if result && oos}
			<div class="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
				<div>
					<div class="text-[10px] text-[#666]">Held back</div>
					<div class="mt-1 font-mono text-sm text-[#888]">{day(result.held_back?.start)} → {day(result.held_back?.end)}</div>
				</div>
				<div>
					<div class="text-[10px] text-[#666]">Net return</div>
					<div class="mt-1 font-mono text-sm {Number(oos.total_return_pct ?? 0) > 0 ? 'text-emerald-400' : 'text-red-400'}">
						{fractionPct(oos.total_return_pct)}
					</div>
				</div>
				<div>
					<div class="text-[10px] text-[#666]">Trades</div>
					<div class="mt-1 font-mono text-sm text-[#888]">{oos.total_trades ?? oos.trades ?? 0}</div>
				</div>
				<div>
					<div class="text-[10px] text-[#666]">Alpha / yr vs baselines</div>
					<div class="mt-1 font-mono text-sm text-[#888]">
						{hurdle?.alpha_pct != null ? `${hurdle.alpha_pct >= 0 ? '+' : ''}${Number(hurdle.alpha_pct).toFixed(1)}%` : '—'}
					</div>
				</div>
			</div>
			{#if result.family}
				<div class="mt-2 text-[10px] text-[#555]">
					'{result.family}' family shot {result.family_shot ?? '?'}{summary.max_family_shots ? ` of ${summary.max_family_shots}` : ''} this quarter
				</div>
			{/if}
			{#if result.verdict_reasons?.length}
				<ul class="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-red-400" data-testid="holdout-reasons">
					{#each result.verdict_reasons as reason}<li>{reason}</li>{/each}
				</ul>
			{/if}
		{:else if summary.state === 'exempt'}
			<p class="mt-1 text-[11px] text-[#888]">{summary.reason ?? 'Created before the holdout'}; judged on forward results instead.</p>
		{:else if summary.state === 'budget_exhausted'}
			<p class="mt-1 text-[11px] text-[#888]">
				The '{summary.family}' family has used its {summary.limit} held-back tests this quarter; it waits for the next roll.
			</p>
		{:else if summary.state === 'errored'}
			<p class="mt-1 text-[11px] text-red-400">The evaluation errored {summary.attempts} times.</p>
		{:else if summary.state === 'missing'}
			<p class="mt-1 text-[11px] text-[#888]">
				Runs automatically once every other paper check passes.
				<button
					class="ml-1 border border-[#333] px-2 py-0.5 text-[10px] text-[#aaa] hover:bg-[#111] disabled:opacity-50"
					title="Spends this candidate's one shot and one of its family's held-back tests this quarter"
					disabled={submitting}
					on:click={runTest}
					data-testid="holdout-run"
				>
					Run the one-shot test now
				</button>
			</p>
		{/if}

		{#if error}
			<p class="mt-1 text-[11px] text-red-400">{error}</p>
		{/if}
	</div>
{/if}
