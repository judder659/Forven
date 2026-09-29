<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import type { PipelineExplainStrategy } from '$lib/api/lifecycle';
	import type { ManagerRow } from '$lib/utils/strategy';
	import { stageClass } from '$lib/utils/strategy';
	import type { ForwardRecord } from '$lib/utils/forge/forward';
	import type { Cause } from '$lib/utils/forge/causes';
	import { CAUSES } from '$lib/utils/forge/causes';
	import {
		BLOCKER_KIND_LABEL,
		approvalTypeLabel,
		blockerTone,
		cleanReason,
		stageLabel,
		statusMeta,
		TONE_DOT,
		TONE_PILL,
		TONE_TEXT,
	} from '$lib/utils/forge/status';
	import { ago, daysLabel, shortDateTime } from '$lib/utils/forge/time';
	import { fmtNum, fmtPct, fmtUsd, signClass } from '$lib/utils/strategyContainer/format';
	import RowMenu from './RowMenu.svelte';

	export let row: ManagerRow;
	export let title: string;
	export let bucket: 'active' | 'trash' = 'active';
	export let entry: PipelineExplainStrategy | null = null;
	export let explainLoading = false;
	export let forward: ForwardRecord | null = null;
	export let cause: Cause | null = null;
	export let archivedFrom: string | null = null;
	export let now = Date.now();

	const dispatch = createEventDispatcher<{ close: void; open: void; move: string; graveyard: void; delete: void; recover: void; export: 'download' | 'clipboard' }>();

	let panel: HTMLElement;

	$: meta = statusMeta(entry?.status);
	$: steps = (entry?.readiness_steps ?? []).filter((step) => step.status !== 'skipped');
	$: tests = Object.entries(entry?.evidence?.validation_tests ?? {});
	$: gauntlet = entry?.gauntlet ?? null;

	const STEP_MARK: Record<string, { text: string; cls: string }> = {
		passed: { text: '✓', cls: 'bg-[#3cc48f]/15 text-[#3cc48f]' },
		failed: { text: '×', cls: 'bg-[#e5574f]/15 text-[#f2956f]' },
		warning: { text: '!', cls: 'bg-[#e7b24a]/15 text-[#e7b24a]' },
	};

	function stepExtra(extra: unknown): { current: number; threshold: number; unit: string; fraction: number; met: boolean } | null {
		if (!extra || typeof extra !== 'object') return null;
		const rec = extra as Record<string, unknown>;
		const current = Number(rec.current);
		const threshold = Number(rec.threshold);
		if (!Number.isFinite(current) || !Number.isFinite(threshold) || threshold <= 0) return null;
		const direction = String(rec.direction ?? 'gte');
		// A ceiling (drawdown under its limit) is not progress toward anything; no bar.
		if (direction === 'lt' || direction === 'lte') return null;
		const met = direction === 'gt' ? current > threshold : current >= threshold;
		return { current, threshold, unit: String(rec.unit ?? ''), fraction: Math.max(0, Math.min(1, current / threshold)), met };
	}

	function testLabel(key: string): string {
		return key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
	}

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape') dispatch('close');
	}

	onMount(() => {
		panel?.focus();
	});

	$: metrics = [
		{ label: 'Sharpe', value: fmtNum(row.sharpe_ratio, 2), tone: '' },
		{ label: 'OOS Sharpe', value: fmtNum(row.out_of_sample_sharpe, 2), tone: '' },
		{ label: 'CAGR', value: fmtPct(row.annualized_return, 1), tone: signClass(row.annualized_return) },
		{ label: 'OOS CAGR', value: fmtPct(row.out_of_sample_cagr, 1), tone: signClass(row.out_of_sample_cagr) },
		{ label: 'Max DD', value: row.max_drawdown === null ? '—' : `${row.max_drawdown.toFixed(1)}%`, tone: '' },
		{ label: 'Win rate', value: fmtPct(row.win_rate, 1, false), tone: '' },
		{ label: 'Profit factor', value: row.profit_factor_is_infinite ? '∞' : fmtNum(row.profit_factor, 2), tone: '' },
		{ label: 'Trades', value: row.total_trades === null ? '—' : String(Math.round(row.total_trades)), tone: '' },
		{ label: 'Robustness', value: row.robustness_score === null ? '—' : `${Math.round(row.robustness_score)}`, tone: '' },
		{ label: 'Deflated Sharpe', value: fmtNum(row.deflated_sharpe, 2), tone: '' },
	];
</script>

<svelte:window on:keydown={onKey} />

<div
	bind:this={panel}
	tabindex="-1"
	role="dialog"
	aria-label={`Details for ${title}`}
	class="fixed bottom-0 right-0 top-0 z-40 flex w-[min(460px,100vw)] flex-col border-l border-sc-line2 bg-sc-panel shadow-[-24px_0_48px_rgba(0,0,0,0.45)] outline-none"
	data-testid="forge-peek"
>
	<header class="grid gap-2 border-b border-sc-line px-4 pb-3 pt-3.5">
		<div class="flex items-center gap-2">
			<span class={`rounded border px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] ${stageClass(row.stage)}`}>{stageLabel(row.stage)}</span>
			{#if entry?.days_in_stage !== undefined && entry?.days_in_stage !== null}
				<span class="text-[11px] text-sc-ink3">{daysLabel(entry.days_in_stage)} in stage</span>
			{:else if bucket === 'trash'}
				<span class="text-[11px] text-sc-ink3">archived {ago(row.stage_changed_at || row.deleted_at || row.created_at, now)}</span>
			{/if}
			<button type="button" class="ml-auto grid h-7 w-7 place-items-center rounded text-[16px] text-sc-ink3 hover:bg-sc-raise hover:text-sc-ink" aria-label="Close details" on:click={() => dispatch('close')}>×</button>
		</div>
		<h2 class="m-0 text-[16px] font-semibold leading-snug text-sc-ink" title={row.name}>{title}</h2>
		<div class="flex flex-wrap items-center gap-x-2 gap-y-1 font-plex-mono text-[11px] text-sc-ink3">
			<span>{row.id}</span><span aria-hidden="true">·</span><span>{row.symbol}</span><span aria-hidden="true">·</span><span>{row.timeframe}</span>
			{#if row.hypothesis_id}<span aria-hidden="true">·</span><span>Idea {row.hypothesis_display_id || row.hypothesis_id}</span>{/if}
		</div>
		<div class="flex items-center gap-2">
			<button type="button" class="rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black hover:bg-white" on:click={() => dispatch('open')}>Open strategy page →</button>
			<RowMenu {bucket} stage={row.stage} label="More actions" on:open={() => dispatch('open')} on:move={(e) => dispatch('move', e.detail)} on:graveyard={() => dispatch('graveyard')} on:delete={() => dispatch('delete')} on:recover={() => dispatch('recover')} on:export={(e) => dispatch('export', e.detail)} />
		</div>
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if bucket === 'trash'}
			<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
				<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Why it was archived</h3>
				{#if cause}
					<div class="flex flex-wrap items-center gap-2">
						<span class={`rounded-full border px-2 py-px text-[12px] font-medium ${CAUSES[cause.key].merit ? TONE_PILL.fail : TONE_PILL.caution}`}>{cause.label}</span>
						{#if cause.detail}<span class="text-[12px] text-sc-ink2">{cause.detail}</span>{/if}
					</div>
					<p class="m-0 text-[12px] leading-relaxed text-sc-ink2">{CAUSES[cause.key].help}</p>
					{#if cause.clause}
						<p class="m-0 rounded border border-sc-line bg-sc-bg px-2.5 py-1.5 font-plex-mono text-[11px] leading-relaxed text-sc-ink2">{cause.clause}</p>
					{/if}
				{/if}
				<p class="m-0 text-[11px] text-sc-ink3">
					{#if archivedFrom}Left the pipeline at {stageLabel(archivedFrom).toLowerCase()}{' · '}{/if}archived {shortDateTime(row.stage_changed_at || row.deleted_at || row.created_at)} · created {shortDateTime(row.created_at)}
				</p>
				{#if row.notes}
					<details class="group text-[11px]">
						<summary class="cursor-pointer list-none text-sc-ink3 hover:text-sc-ink2"><span class="group-open:hidden">Show</span><span class="hidden group-open:inline">Hide</span> the full archival record</summary>
						<p class="m-0 mt-1.5 whitespace-pre-wrap rounded border border-sc-line bg-sc-bg px-2.5 py-1.5 font-plex-mono text-[10.5px] leading-relaxed text-sc-ink3">{row.notes}</p>
					</details>
				{/if}
			</section>
		{:else}
			<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
				<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Where it stands</h3>
				{#if !entry && explainLoading}
					<div class="grid gap-1.5" aria-busy="true">
						<span class="h-4 w-40 animate-pulse rounded bg-sc-raise/70"></span>
						<span class="h-3 w-64 animate-pulse rounded bg-sc-raise/50"></span>
						<span class="text-[11px] text-sc-ink3">Dry-running the gates…</span>
					</div>
				{:else if !entry}
					<p class="m-0 text-[12px] text-sc-ink3">The pipeline explainer has not reported on this strategy.</p>
				{:else}
					<span class={`inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] font-semibold ${TONE_PILL[meta.tone]}`}>
						<span class={`h-1.5 w-1.5 rounded-full ${TONE_DOT[meta.tone]}`} aria-hidden="true"></span>{meta.label}
					</span>
					<p class="m-0 text-[12px] leading-relaxed text-sc-ink2">{meta.help}</p>
					{#if entry.pending_approval}
						<a
							href={`/approval?approval_id=${encodeURIComponent(String(entry.pending_approval.id))}`}
							class="flex items-center justify-between gap-3 rounded-md border border-[#e7b24a]/40 bg-[#e7b24a]/10 px-3 py-2 text-[12px] text-[#e7b24a] hover:bg-[#e7b24a]/15"
						>
							<span>Approval #{entry.pending_approval.id} — {approvalTypeLabel(entry.pending_approval.approval_type)}{#if entry.pending_approval.requested_status} (→ {stageLabel(entry.pending_approval.requested_status).toLowerCase()}){/if}</span>
							<span class="whitespace-nowrap font-medium">Review →</span>
						</a>
					{/if}
					{#if entry.next_action}
						<p class="m-0 text-[12px] text-sc-ink"><span class="text-sc-ink3">Next step:</span> {cleanReason(entry.next_action.label, { firstClause: false })}</p>
					{/if}
					{#if entry.next_transition}
						<p class="m-0 text-[11px] leading-relaxed text-sc-ink3">
							<span class="text-sc-ink2">{entry.next_transition.label}</span>{#if entry.next_transition.trigger} — {entry.next_transition.trigger}{/if}
						</p>
					{/if}
				{/if}
			</section>

			{#if entry && entry.blockers.length > 0}
				<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
					<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">What's in the way</h3>
					<ul class="m-0 grid list-none gap-1.5 p-0">
						{#each entry.blockers as blocker, i (i)}
							<li class="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2">
								<span class={`mt-px rounded border px-1.5 py-px font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] ${TONE_PILL[blockerTone(blocker)]}`}>{BLOCKER_KIND_LABEL[blocker.kind] ?? blocker.kind}</span>
								<div class="min-w-0 text-[12px] leading-snug">
									<span class="text-sc-ink">{cleanReason(blocker.reason, { firstClause: false })}</span>
									{#if blocker.action}<span class="block text-[11px] text-sc-ink3">{cleanReason(blocker.action.label, { firstClause: false })}</span>{/if}
								</div>
							</li>
						{/each}
					</ul>
				</section>
			{/if}

			{#if steps.length > 0}
				<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
					<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Paper → live checklist</h3>
					<ul class="m-0 grid list-none gap-1.5 p-0">
						{#each steps as step (step.name)}
							{@const extra = stepExtra(step.extra)}
							{@const mark = STEP_MARK[step.status] ?? { text: '·', cls: 'bg-sc-raise text-sc-ink3' }}
							<li class="grid grid-cols-[18px_minmax(0,1fr)] items-start gap-2">
								<span class={`mt-px grid h-[18px] w-[18px] place-items-center rounded-full text-[11px] font-semibold ${mark.cls}`} aria-hidden="true">{mark.text}</span>
								<div class="min-w-0">
									<span class="block text-[12px] text-sc-ink2">{step.detail}</span>
									{#if extra}
										<span class="mt-1 block h-[3px] w-full overflow-hidden rounded-full bg-sc-line2">
											<i class={`block h-full ${extra.met ? 'bg-[#3cc48f]' : 'bg-sc-ink2'}`} style={`width:${(extra.fraction * 100).toFixed(1)}%`}></i>
										</span>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				</section>
			{/if}

			{#if forward}
				<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
					<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{forward.book === 'live' ? 'Live results (wallet $)' : 'Paper results (paper book)'}</h3>
					<div class="grid grid-cols-4 gap-2">
						<div><span class="block text-[10.5px] text-sc-ink3">Realized</span><b class={`font-plex-mono text-[13px] font-medium ${signClass(forward.pnlUsd)}`}>{fmtUsd(forward.pnlUsd, forward.book === 'live' ? 2 : 0)}</b></div>
						<div><span class="block text-[10.5px] text-sc-ink3">Closed</span><b class="font-plex-mono text-[13px] font-medium text-sc-ink">{forward.closed}</b></div>
						<div><span class="block text-[10.5px] text-sc-ink3">Win rate</span><b class="font-plex-mono text-[13px] font-medium text-sc-ink">{forward.winRate === null ? '—' : `${Math.round(forward.winRate)}%`}</b></div>
						<div><span class="block text-[10.5px] text-sc-ink3">Now</span><span class={`text-[12px] ${TONE_TEXT[forward.tone]}`}>{forward.stateLabel}</span></div>
					</div>
					{#if forward.blockedEntries > 0}
						<p class="m-0 text-[11px] text-[#e7b24a]">{forward.blockedEntries} live entr{forward.blockedEntries === 1 ? 'y' : 'ies'} refused{#if forward.blockedReason} — {cleanReason(forward.blockedReason)}{/if}</p>
					{/if}
				</section>
			{/if}

			{#if entry && (tests.length > 0 || gauntlet || entry.evidence?.last_backtest_at || entry.last_rejection)}
				<section class="grid gap-1.5 border-b border-sc-line px-4 py-3">
					<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Evidence</h3>
					<dl class="m-0 grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 text-[12px]">
						{#if entry.evidence?.last_backtest_at}
							<dt class="text-sc-ink3">Last backtest</dt><dd class="m-0 text-right text-sc-ink2">{ago(entry.evidence.last_backtest_at, now)}</dd>
						{/if}
						{#if entry.evidence?.last_optimization_at}
							<dt class="text-sc-ink3">Last optimization</dt><dd class="m-0 text-right text-sc-ink2">{ago(entry.evidence.last_optimization_at, now)}</dd>
						{/if}
						{#if gauntlet && gauntlet.tests_total}
							<dt class="text-sc-ink3">Gauntlet tests</dt>
							<dd class="m-0 text-right font-plex-mono text-sc-ink2">{gauntlet.tests_passed ?? 0}/{gauntlet.tests_total} passed</dd>
						{/if}
						{#if gauntlet && gauntlet.composite_robustness_score !== null}
							<dt class="text-sc-ink3">Robustness score</dt>
							<dd class="m-0 text-right font-plex-mono text-sc-ink2">{Math.round(gauntlet.composite_robustness_score)}{#if gauntlet.min_robustness_score !== null}<span class="text-sc-ink3"> / min {Math.round(gauntlet.min_robustness_score)}</span>{/if}</dd>
						{/if}
						{#each tests as [key, test] (key)}
							<dt class="text-sc-ink3">{testLabel(key)}</dt>
							<dd class="m-0 text-right text-sc-ink2">
								<span class={String(test.verdict ?? test.status ?? '').toLowerCase().includes('pass') ? 'text-[#3cc48f]' : String(test.verdict ?? test.status ?? '').toLowerCase().includes('fail') ? 'text-[#f2956f]' : ''}>{test.verdict ?? test.status ?? '—'}</span>
								{#if test.stale || test.stale_engine}<span class="ml-1 rounded border border-[#e7b24a]/40 px-1 text-[10px] text-[#e7b24a]">stale</span>{/if}
								{#if test.age_days !== null && test.age_days !== undefined}<span class="text-sc-ink3"> · {daysLabel(test.age_days)} old</span>{/if}
							</dd>
						{/each}
					</dl>
					{#if entry.last_rejection}
						<p class="m-0 mt-1 text-[11px] leading-relaxed text-sc-ink3">
							Last rejected by the {String(entry.last_rejection.gate ?? '').replace(/_/g, ' ')} gate {ago(entry.last_rejection.at, now)}{#if entry.rejections_in_stage > 1}{' — '}{entry.rejections_in_stage.toLocaleString('en-US')} gate checks have said no during this stage{/if}.
						</p>
					{/if}
				</section>
			{/if}
		{/if}

		<section class="grid gap-2 px-4 py-3">
			<h3 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Backtest (full window)</h3>
			{#if row.has_backtest_results || row.sharpe_ratio !== null}
				<div class="grid grid-cols-2 gap-x-4 gap-y-1.5">
					{#each metrics as metric (metric.label)}
						<div class="flex items-baseline justify-between gap-2 border-b border-sc-line/60 pb-1 text-[12px]">
							<span class="text-sc-ink3">{metric.label}</span>
							<b class={`font-plex-mono font-medium ${metric.tone || 'text-sc-ink'}`}>{metric.value}</b>
						</div>
					{/each}
				</div>
				{#if !row.sharpe_is_reliable}
					<p class="m-0 text-[11px] text-sc-ink3">Fewer than 20 trades — Sharpe is noisy.</p>
				{/if}
			{:else}
				<p class="m-0 text-[12px] text-sc-ink3">No backtest on record yet.</p>
			{/if}
		</section>
	</div>
</div>
