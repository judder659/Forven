<script lang="ts">
	/**
	 * Live probation on the strategy page. In paper: the readiness checks and a
	 * Start button. In probation: the loud striped banner with size, ladder
	 * progress, the live record against its limits, and every manual control
	 * (change size, resume ladder, graduate, pause, demote).
	 */
	import { createEventDispatcher } from 'svelte';
	import {
		GO_LIVE_CONFIRM_PHRASE,
		PROBATION_CONFIRM_PHRASE,
		demoteProbation,
		getProbationStatus,
		graduateProbation,
		pauseProbation,
		probationRefusal,
		setProbationSize,
		startProbation,
		type ProbationCheck,
		type ProbationStatus,
	} from '$lib/api/probation';
	import ProbationBadge from './ProbationBadge.svelte';
	import TypedConfirmDialog from './TypedConfirmDialog.svelte';

	export let strategyId: string;
	/** Changes when the page reloads the strategy (stage moves, trades close). */
	export let refreshKey = '';

	const dispatch = createEventDispatcher<{ changed: void }>();

	let status: ProbationStatus | null = null;
	let loadError: string | null = null;
	let loadedKey = '';
	let busy = false;
	let actionError: string | null = null;
	let dialog: null | 'start' | 'graduate' | 'demote' = null;
	let showChecks = false;
	let sizeInput: number | null = null;
	let editingSize = false;

	$: key = `${strategyId}|${refreshKey}`;
	$: if (strategyId && key !== loadedKey) {
		loadedKey = key;
		void load();
	}

	async function load() {
		const target = strategyId;
		try {
			const next = await getProbationStatus(target);
			if (target !== strategyId) return;
			status = next;
			loadError = null;
		} catch (error) {
			if (target !== strategyId) return;
			loadError = error instanceof Error ? error.message : 'Could not read probation status';
		}
	}

	async function run(action: () => Promise<ProbationStatus>, after?: () => void) {
		busy = true;
		actionError = null;
		try {
			status = await action();
			after?.();
			dispatch('changed');
		} catch (error) {
			const refusal = probationRefusal(error);
			actionError = refusal
				? [refusal.message, ...refusal.checks.filter((c) => c.ok !== true).map((c) => `${c.label}: ${c.detail}`)].join(' · ')
				: error instanceof Error ? error.message : 'Request failed';
		} finally {
			busy = false;
		}
	}

	const closeDialog = () => { dialog = null; actionError = null; };

	$: record = status?.record ?? null;
	$: inProbation = Boolean(status?.in_probation && record);
	$: entry = status?.entry ?? null;
	$: isPaper = status?.stage === 'paper' || status?.stage === 'paper_trading';
	$: live = status?.live ?? null;
	$: ladder = status?.ladder;
	$: graduateAt = ladder?.graduate_trades ?? 10;
	$: progressPct = live ? Math.min(100, (live.live_trades / Math.max(1, graduateAt)) * 100) : 0;
	$: failing = (entry?.failing ?? []) as ProbationCheck[];
	$: hardFailing = failing.filter((c) => c.bypassable === false);
	$: gradFailing = (status?.graduation?.failing ?? []) as ProbationCheck[];
	$: lastEvent = record?.history?.length ? record.history[record.history.length - 1] : null;

	function pct(value: number | null | undefined, digits = 2, signed = false): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '—';
		return `${signed && value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
	}

	function when(value: string | null | undefined): string {
		if (!value) return '—';
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
	}
</script>

{#if loadError}
	<div class="mt-2 rounded border border-sc-line bg-sc-panel px-3 py-2 text-[11px] text-sc-ink3" data-testid="probation-panel-error">Probation status unavailable: {loadError}</div>
{:else if status && inProbation && record}
	<section
		class="mt-2 grid gap-2 rounded-lg border-2 border-amber-400/70 bg-[repeating-linear-gradient(135deg,rgba(251,191,36,0.10)_0_14px,rgba(251,191,36,0.03)_14px_28px)] p-3"
		data-testid="probation-panel"
	>
		<div class="flex flex-wrap items-center gap-2">
			<ProbationBadge state={record.state} sizePct={record.size_pct} />
			<span class="text-[13px] font-semibold text-amber-100">
				{#if record.state === 'demoting'}
					Being demoted to paper: no new entries; the open trade runs to its exit
				{:else if record.state === 'paused'}
					Live on probation, paused: no new entries
				{:else}
					Trading live with real money at {Math.round(record.size_pct)}% of normal size
				{/if}
			</span>
			{#if record.size_locked}
				<span class="rounded border border-sc-line px-1.5 py-0.5 text-[10px] text-sc-ink2" title="Set by hand; the ladder does not move it">size set by hand</span>
			{/if}
		</div>

		{#if live}
			<div class="grid gap-1">
				<div class="flex flex-wrap items-baseline justify-between gap-2 text-[12px] text-sc-ink2">
					<span><span class="font-plex-mono text-sc-ink">{live.live_trades}</span> of {graduateAt} live trades to full size</span>
					{#if status.next_step}
						<span>Next: {Math.round(status.next_step.size_pct)}% at {status.next_step.at_trades} trades</span>
					{/if}
				</div>
				<div class="h-1.5 overflow-hidden rounded bg-sc-line" aria-hidden="true">
					<div class="h-full bg-amber-400" style="width: {progressPct}%"></div>
				</div>
			</div>
			<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px] sm:grid-cols-4">
				<div><span class="text-sc-ink3">Live result</span><div class="font-plex-mono {live.live_return_pct < 0 ? 'text-red-300' : 'text-sc-ink'}">{pct(live.live_return_pct, 2, true)}</div></div>
				<div><span class="text-sc-ink3">Drawdown / limit</span><div class="font-plex-mono text-sc-ink">{pct(live.live_drawdown_pct)} / {pct(live.drawdown_limit_pct)}</div></div>
				<div><span class="text-sc-ink3">Losing streak / limit</span><div class="font-plex-mono text-sc-ink">{live.losing_streak} / {live.losing_streak_limit ?? '—'}</div></div>
				<div><span class="text-sc-ink3">Open positions</span><div class="font-plex-mono text-sc-ink">{status.open_positions ?? 0}</div></div>
			</div>
			{#if live.triggers.length}
				<div class="rounded border border-red-800 bg-red-900/25 px-2.5 py-1.5 text-[12px] text-red-200" data-testid="probation-triggers">
					<span class="font-semibold">Outside what the backtest predicted:</span>
					{#each live.triggers as trigger (trigger.key)}<div>• {trigger.label}: {trigger.detail}</div>{/each}
					{#if !status.automation.auto_demote}<div class="mt-1 text-red-300">Automatic demotion is off: demote by hand if you agree.</div>{/if}
				</div>
			{/if}
		{/if}

		{#if status.skips.length}
			<details class="text-[11px] text-sc-ink3">
				<summary class="cursor-pointer">{status.skips.length} skipped signal{status.skips.length === 1 ? '' : 's'} (order too small to raise to the exchange minimum within the risk cap)</summary>
				{#each status.skips.slice().reverse().slice(0, 5) as skip}
					<div class="font-plex-mono">{when(skip.at)} · {skip.asset ?? ''} · {skip.reason ?? ''}</div>
				{/each}
			</details>
		{/if}

		{#if lastEvent}
			<div class="text-[11px] text-sc-ink3">Entered {when(record.entered_at)} by {record.entered_by ?? '—'}{record.bypassed.length ? ` (bypassed: ${record.bypassed.join(', ')})` : ''} · last: {lastEvent.event.replace(/_/g, ' ')} {when(lastEvent.at)}</div>
		{/if}

		{#if record.state !== 'demoting'}
			<div class="flex flex-wrap items-center gap-2">
				{#if editingSize}
					<input type="number" min="1" max="99" step="1" bind:value={sizeInput} class="terminal-input w-20" data-testid="probation-size-input" />
					<span class="text-[12px] text-sc-ink3">%</span>
					<button type="button" class="rounded border border-amber-500/60 bg-amber-500/20 px-2.5 py-1 text-[12px] text-amber-100 disabled:opacity-40" disabled={busy || !sizeInput || sizeInput < 1 || sizeInput >= 100}
						on:click={() => run(() => setProbationSize(strategyId, Number(sizeInput)), () => (editingSize = false))}>Set size</button>
					<button type="button" class="rounded border border-sc-line px-2.5 py-1 text-[12px] text-sc-ink2" on:click={() => (editingSize = false)}>Cancel</button>
				{:else}
					<button type="button" class="rounded border border-sc-line px-2.5 py-1 text-[12px] text-sc-ink2 hover:bg-sc-raise" disabled={busy}
						on:click={() => { sizeInput = Math.round(record?.size_pct ?? 25); editingSize = true; }}>Change size</button>
					{#if record.size_locked}
						<button type="button" class="rounded border border-sc-line px-2.5 py-1 text-[12px] text-sc-ink2 hover:bg-sc-raise" disabled={busy}
							on:click={() => run(() => setProbationSize(strategyId, null))}>Resume ladder</button>
					{/if}
				{/if}
				<button type="button" class="rounded border border-sc-line px-2.5 py-1 text-[12px] text-sc-ink2 hover:bg-sc-raise" disabled={busy}
					on:click={() => run(() => pauseProbation(strategyId, record?.state !== 'paused'))}>{record.state === 'paused' ? 'Resume entries' : 'Pause entries'}</button>
				<button type="button" class="rounded border border-emerald-600/60 bg-emerald-600/15 px-2.5 py-1 text-[12px] text-emerald-100 hover:bg-emerald-600/25" disabled={busy}
					title={gradFailing.length ? gradFailing.map((c) => `${c.label}: ${c.detail}`).join('\n') : 'All graduation checks pass'}
					on:click={() => { actionError = null; dialog = 'graduate'; }}>Graduate to full size{gradFailing.length ? ' (override)' : ''}</button>
				<button type="button" class="rounded border border-red-700 bg-red-900/30 px-2.5 py-1 text-[12px] text-red-100 hover:bg-red-900/50" disabled={busy}
					on:click={() => { actionError = null; dialog = 'demote'; }}>Demote to paper</button>
			</div>
		{/if}
		{#if actionError && !dialog}
			<div class="rounded border border-red-800 bg-red-900/20 px-2.5 py-1.5 text-[12px] text-red-300">{actionError}</div>
		{/if}
	</section>
{:else if status && isPaper && entry}
	<section class="mt-2 grid gap-2 rounded-lg border border-sc-line bg-sc-panel px-3 py-2" data-testid="probation-entry">
		<div class="flex flex-wrap items-center gap-2 text-[12px]">
			<span class="font-semibold text-sc-ink">Live probation</span>
			{#if entry.eligible}
				<span class="rounded border border-emerald-600/60 bg-emerald-600/15 px-1.5 py-0.5 text-[10px] text-emerald-200">READY</span>
				<span class="text-sc-ink2">Its paper trades match its backtest ({entry.confirmation.matched} confirmed).</span>
			{:else}
				<span class="text-sc-ink3">{failing.length} check{failing.length === 1 ? '' : 's'} not met yet · {entry.confirmation.matched} paper trade{entry.confirmation.matched === 1 ? '' : 's'} confirmed</span>
			{/if}
			<button type="button" class="text-[11px] text-sc-ink3 underline hover:text-sc-ink" on:click={() => (showChecks = !showChecks)}>{showChecks ? 'Hide checks' : 'Show checks'}</button>
			<span class="flex-1"></span>
			<button
				type="button"
				class="rounded border px-2.5 py-1 text-[12px] font-semibold disabled:opacity-40 {entry.eligible ? 'border-amber-500/60 bg-amber-500/20 text-amber-100 hover:bg-amber-500/30' : 'border-sc-line text-sc-ink2 hover:bg-sc-raise'}"
				disabled={busy || hardFailing.length > 0}
				title={hardFailing.length ? hardFailing.map((c) => `${c.label}: ${c.detail}`).join('\n') : ''}
				data-testid="probation-start"
				on:click={() => { actionError = null; dialog = 'start'; }}
			>{entry.eligible ? 'Start probation' : 'Start probation (override)'}</button>
		</div>
		{#if showChecks}
			<div class="grid gap-0.5 text-[11px]">
				{#each entry.checks as check (check.key)}
					<div class="flex gap-2">
						<span class={check.ok === true ? 'text-emerald-300' : check.bypassable === false ? 'text-red-300' : 'text-amber-300'}>{check.ok === true ? '✓' : '✗'}</span>
						<span class="text-sc-ink2">{check.label}</span>
						<span class="text-sc-ink3">{check.detail}{check.ok !== true && check.bypassable === false ? ' (cannot be overridden)' : ''}</span>
					</div>
				{/each}
				{#each entry.notes as note}<div class="text-sc-ink3">{note}</div>{/each}
			</div>
		{/if}
	</section>
{/if}

{#if dialog === 'start' && entry}
	<TypedConfirmDialog
		title="Start live probation?"
		phrase={PROBATION_CONFIRM_PHRASE}
		confirmLabel="Start probation"
		message={`This strategy will trade live with real money at ${ladder?.start_size_pct ?? 25}% of normal size, stepping up as live trades confirm the backtest. It is demoted to paper if live results fall outside what the backtest predicted.`}
		bypassing={failing}
		notes={entry.notes}
		askReason={failing.length > 0}
		{busy}
		errorMessage={actionError}
		on:cancel={closeDialog}
		on:confirm={(event) => run(
			() => startProbation(strategyId, { confirm: event.detail.phrase, bypass: failing.length > 0, reason: event.detail.reason }),
			() => (dialog = null),
		)}
	/>
{:else if dialog === 'graduate'}
	<TypedConfirmDialog
		title="Graduate to full size?"
		phrase={GO_LIVE_CONFIRM_PHRASE}
		confirmLabel="Graduate"
		danger
		message="Probation ends and this strategy trades live at 100% of normal size."
		bypassing={gradFailing}
		{busy}
		errorMessage={actionError}
		on:cancel={closeDialog}
		on:confirm={(event) => run(
			() => graduateProbation(strategyId, { confirm: event.detail.phrase, bypass: gradFailing.length > 0 }),
			() => (dialog = null),
		)}
	/>
{:else if dialog === 'demote'}
	<TypedConfirmDialog
		title="Demote to paper?"
		phrase="DEMOTE"
		confirmLabel="Demote"
		message="No new live entries from now. An open live trade runs to its stop or exit, then the strategy moves back to paper."
		askReason
		{busy}
		errorMessage={actionError}
		on:cancel={closeDialog}
		on:confirm={(event) => run(() => demoteProbation(strategyId, event.detail.reason), () => (dialog = null))}
	/>
{/if}
