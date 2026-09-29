<script lang="ts">
	/**
	 * Why a paper or live strategy's new entries are refused, and the fixes that
	 * apply: accept a backtest of the current settings as the live baseline, or
	 * restore the validated settings. Paper points to the gauntlet. Renders nothing
	 * while the strategy can trade (unless showOk).
	 */
	import { createEventDispatcher } from 'svelte';
	import {
		acceptExecutionBacktest,
		getExecutionCheck,
		restoreValidatedParams,
		type ExecutionCheck,
	} from '$lib/api/executionCheck';
	import { explainCheck, formatSetting, isLiveStage } from '$lib/utils/executionCheck';

	export let strategyId: string;
	/** Refetch whenever this changes (e.g. the stored params after a save). */
	export let refreshKey: unknown = null;
	/** Where to run a backtest or the gauntlet, when that is the next step. */
	export let forgeHref: string | null = null;
	export let showOk = false;

	const dispatch = createEventDispatcher<{ changed: ExecutionCheck }>();

	let check: ExecutionCheck | null = null;
	let loadError = '';
	let actionError = '';
	let busy = false;
	let step: 'idle' | 'restore' | 'accept' = 'idle';
	let reason = '';
	let selectedResultId = '';
	let sequence = 0;
	let loadedFor = '';

	$: void load(strategyId, refreshKey);

	async function load(id: string, _key: unknown): Promise<void> {
		if (!id) return;
		if (id !== loadedFor) {
			// A reused panel (the desk switches strategies) must not carry one strategy's
			// half-finished confirm, reason or chosen backtest over to the next.
			loadedFor = id;
			check = null;
			step = 'idle';
			reason = '';
			selectedResultId = '';
			actionError = '';
		}
		const mine = ++sequence;
		try {
			const next = await getExecutionCheck(id);
			if (mine !== sequence) return;
			check = next;
			loadError = '';
		} catch (error) {
			if (mine !== sequence) return;
			loadError = error instanceof Error ? error.message : 'The execution check could not be loaded.';
		}
	}

	function begin(next: 'restore' | 'accept'): void {
		step = next;
		actionError = '';
		reason = next === 'restore' ? 'Undo a settings change' : 'Accept the new settings for live trading';
		// Nothing preselected: the operator picks which backtest becomes the evidence.
		selectedResultId = '';
	}

	async function run(action: () => Promise<ExecutionCheck>): Promise<void> {
		busy = true;
		actionError = '';
		try {
			const next = await action();
			sequence += 1;
			check = next;
			step = 'idle';
			dispatch('changed', next);
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'That did not go through.';
		} finally {
			busy = false;
		}
	}

	const pct = (value: number | null, digits = 1) => (value === null || value === undefined ? '—' : `${(value * 100).toFixed(digits)}%`);
	const day = (value: string | null) => (value ? String(value).slice(0, 10) : '—');

	$: live = isLiveStage(check?.stage);
	$: blocked = Boolean(check?.executing && check.executable === false);
	$: explanation = check && blocked ? explainCheck(check) : null;
	$: needsForge = Boolean(check && blocked && !check.actions.accept_backtest && (check.actions.gauntlet || (live && check.kind !== 'unavailable')));
	$: validatedLeverage = check?.accepted?.leverage ?? null;
</script>

{#if loadError && !check}
	<p class="text-[12px] text-sc-ink3" data-testid="execution-check-error">Execution check unavailable: {loadError}</p>
{:else if check && blocked && explanation}
	<section class="grid gap-2.5 rounded-md border border-[#e7b24a]/40 bg-[#e7b24a]/[0.06] p-3" aria-label="Execution check" data-testid="execution-check">
		<div class="grid gap-0.5">
			<span class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.08em] text-[#e7b24a]">Execution check</span>
			<h3 class="text-[13.5px] font-semibold text-sc-ink">{explanation.title}</h3>
			<p class="text-[12.5px] leading-relaxed text-sc-ink2">{explanation.body}</p>
		</div>

		{#if check.changes.length}
			<div class="overflow-x-auto">
				<table class="w-full border-collapse text-[12px]" data-testid="execution-check-changes">
					<thead>
						<tr class="font-plex-cond text-[11px] uppercase tracking-[0.06em] text-sc-ink3">
							<th class="py-1 pr-3 text-left font-medium">Setting</th>
							<th class="py-1 pr-3 text-right font-medium">Validated</th>
							<th class="py-1 text-right font-medium">Now</th>
						</tr>
					</thead>
					<tbody class="font-plex-mono tabular-nums">
						{#each check.changes as change (change.key)}
							<tr class="border-t border-sc-line">
								<td class="py-1 pr-3 text-left font-sans text-sc-ink2">{change.key}</td>
								<td class="py-1 pr-3 text-right text-sc-ink3">{formatSetting(change.validated)}</td>
								<td class="py-1 text-right text-sc-ink">{formatSetting(change.current)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if explanation.fix}<p class="text-[12px] text-sc-ink2">{explanation.fix}</p>{/if}

		{#if step === 'idle'}
			<div class="flex flex-wrap gap-2">
				{#if check.actions.accept_backtest}
					<button type="button" class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12.5px] font-medium text-sc-ink hover:border-sc-ink4" on:click={() => begin('accept')} data-testid="execution-check-accept">Accept a backtest as the live baseline</button>
				{/if}
				{#if check.actions.restore}
					<button type="button" class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12.5px] font-medium text-sc-ink hover:border-sc-ink4" on:click={() => begin('restore')} data-testid="execution-check-restore">Restore validated settings</button>
				{/if}
				{#if needsForge && forgeHref}
					<a class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12.5px] text-sc-ink2 no-underline hover:text-sc-ink" href={forgeHref}>{check.actions.gauntlet ? 'Open the Forge to move it back to Gauntlet' : 'Open the Forge to run a backtest'}</a>
				{/if}
			</div>
		{:else if step === 'restore'}
			<div class="grid gap-2 rounded-md border border-sc-line bg-sc-panel p-2.5" data-testid="execution-check-restore-confirm">
				<p class="text-[12.5px] text-sc-ink2">
					Put back the exact settings it was validated with{check.changes.length ? `, undoing ${check.changes.length} change${check.changes.length === 1 ? '' : 's'}` : ''}. New entries resume at the validated settings{validatedLeverage !== null ? `, including ${validatedLeverage}× leverage` : ''}.
				</p>
				<label class="grid gap-1 text-[12px] text-sc-ink3">Reason (recorded)
					<input class="rounded border border-sc-line2 bg-sc-panel2 px-2 py-1 text-[12.5px] text-sc-ink focus:border-sc-ink3 focus:outline-none" bind:value={reason} maxlength="500" />
				</label>
				<div class="flex gap-2">
					<button type="button" class="rounded-md border border-sc-ink4 bg-sc-raise px-3 py-1.5 text-[12.5px] font-medium text-sc-ink disabled:opacity-50" disabled={busy || !reason.trim()} on:click={() => run(() => restoreValidatedParams(strategyId, reason.trim()))}>{busy ? 'Restoring…' : 'Restore'}</button>
					<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12.5px] text-sc-ink2" disabled={busy} on:click={() => (step = 'idle')}>Cancel</button>
				</div>
			</div>
		{:else}
			<div class="grid gap-2 rounded-md border border-sc-line bg-sc-panel p-2.5" data-testid="execution-check-accept-confirm">
				<p class="text-[12.5px] text-sc-ink2">
					Pick the backtest to accept. It becomes the evidence this live book trades on: the scanner then allows entries at exactly these settings. This records your acceptance with the reason below; it does not re-run the research gates.
				</p>
				<div class="grid gap-1" role="radiogroup" aria-label="Backtests of the current settings">
					{#each check.candidates as candidate (candidate.result_id)}
						<label class={`grid cursor-pointer grid-cols-[16px_minmax(0,1fr)] items-start gap-2 rounded border px-2 py-1.5 text-[12px] ${selectedResultId === candidate.result_id ? 'border-sc-ink4 bg-sc-panel2' : 'border-sc-line'}`}>
							<input type="radio" class="mt-0.5" name={`candidate-${strategyId}`} value={candidate.result_id} bind:group={selectedResultId} />
							<span class="grid gap-0.5">
								<span class="font-plex-mono text-sc-ink">{candidate.result_id}</span>
								<span class="text-sc-ink3">Run {day(candidate.created_at)} · data {day(candidate.start_date)} → {day(candidate.end_date)}</span>
								<span class="font-plex-mono tabular-nums text-sc-ink2">{candidate.trades} trades · return {pct(candidate.total_return_pct)} · max DD {pct(candidate.max_drawdown_pct)} · win {pct(candidate.win_rate, 0)}{candidate.leverage !== null ? ` · ${candidate.leverage}×` : ''}</span>
							</span>
						</label>
					{/each}
				</div>
				<label class="grid gap-1 text-[12px] text-sc-ink3">Reason (recorded)
					<input class="rounded border border-sc-line2 bg-sc-panel2 px-2 py-1 text-[12.5px] text-sc-ink focus:border-sc-ink3 focus:outline-none" bind:value={reason} maxlength="500" />
				</label>
				<div class="flex gap-2">
					<button type="button" class="rounded-md border border-sc-ink4 bg-sc-raise px-3 py-1.5 text-[12.5px] font-medium text-sc-ink disabled:opacity-50" disabled={busy || !reason.trim() || !selectedResultId} on:click={() => run(() => acceptExecutionBacktest(strategyId, selectedResultId, reason.trim()))}>{busy ? 'Accepting…' : 'Accept as live baseline'}</button>
					<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12.5px] text-sc-ink2" disabled={busy} on:click={() => (step = 'idle')}>Cancel</button>
				</div>
			</div>
		{/if}

		{#if actionError}<p class="text-[12px] text-[#f3a39d]" role="alert">{actionError}</p>{/if}
		{#if check.reason}<p class="font-plex-mono text-[11px] text-sc-ink4" title="The check's own words">{check.reason}</p>{/if}
	</section>
{:else if check && showOk && check.executing && check.executable}
	<p class="text-[12px] text-sc-ink3" data-testid="execution-check-ok">Execution check: trading at its validated settings{check.accepted?.result_id ? ` (evidence ${check.accepted.result_id})` : ''}.</p>
{/if}
