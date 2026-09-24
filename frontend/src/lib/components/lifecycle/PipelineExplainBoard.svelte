<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		explainPipeline,
		type PipelineExplainResponse,
		type PipelineExplainStrategy,
	} from '$lib/api/lifecycle';
	import { createRealtimeRefresh } from '$lib/utils/realtime';

	let data: PipelineExplainResponse | null = null;
	let loading = true;
	let error: string | null = null;
	let expanded: Record<string, boolean> = {};

	const STAGE_ORDER = ['quick_screen', 'gauntlet', 'paper', 'live_graduated'] as const;
	const STAGE_TITLES: Record<string, string> = {
		quick_screen: 'Quick Screen',
		gauntlet: 'Gauntlet',
		paper: 'Paper',
		live_graduated: 'Live',
	};

	const STATUS_META: Record<string, { label: string; cls: string; pulse?: boolean }> = {
		ready: { label: 'READY', cls: 'text-emerald-400 border-emerald-900 bg-emerald-500/10' },
		in_flight: { label: 'RUNNING', cls: 'text-yellow-400 border-yellow-900 bg-yellow-500/10', pulse: true },
		waiting_evidence: { label: 'WAITING ON EVIDENCE', cls: 'text-amber-300 border-amber-900 bg-amber-500/10' },
		blocked_merit: { label: 'FAILED GATE', cls: 'text-red-400 border-red-900 bg-red-500/10' },
		slot_contention: { label: 'SLOT CONTENTION', cls: 'text-orange-400 border-orange-900 bg-orange-500/10' },
		awaiting_operator: { label: 'NEEDS YOU', cls: 'text-purple-400 border-purple-900 bg-purple-500/10' },
		live: { label: 'LIVE', cls: 'text-emerald-400 border-emerald-900 bg-emerald-500/10' },
		unknown: { label: 'UNKNOWN', cls: 'text-[#888] border-[#333] bg-[#111]' },
	};

	function statusMeta(status: string) {
		return STATUS_META[status] ?? STATUS_META.unknown;
	}

	function fmtDays(days: number | null | undefined): string {
		if (days == null) return '?';
		if (days < 1) return '<1d';
		return `${Math.round(days)}d`;
	}

	function toggle(id: string) {
		expanded = { ...expanded, [id]: !expanded[id] };
	}

	async function load() {
		try {
			data = await explainPipeline();
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load pipeline explanation';
		} finally {
			loading = false;
		}
	}

	const realtime = createRealtimeRefresh(load, {
		fallbackMs: 30_000,
		pollWhenWsOfflineOnly: true,
	});

	onMount(() => {
		void load();
		realtime.start();
	});

	onDestroy(() => {
		realtime.stop();
	});

	$: statusCounts = data?.counts?.by_status ?? {};
	$: grouped = (data?.strategies ?? []).reduce<Record<string, PipelineExplainStrategy[]>>((acc, s) => {
		(acc[s.stage] ??= []).push(s);
		return acc;
	}, {});
</script>

<div class="space-y-4" data-testid="pipeline-explain-board">
	{#if error}
		<div class="border border-red-900 bg-red-500/5 px-4 py-3 text-xs text-red-400" data-testid="explain-error">{error}</div>
	{:else if loading && !data}
		<div class="text-[#666] text-xs animate-pulse" data-testid="explain-loading">Loading strategy pipeline...</div>
	{:else if data}
		<!-- Summary strip -->
		<div class="flex flex-wrap items-center gap-2 text-[10px]" data-testid="explain-summary">
			{#each Object.entries(statusCounts) as [status, count]}
				{@const meta = statusMeta(status)}
				<span class="px-2 py-1 border font-bold uppercase tracking-wider {meta.cls}">
					{meta.label}: {count}
				</span>
			{/each}
			<span class="ml-auto text-[#555]">
				preset: <span class="text-[#888] uppercase">{data.pipeline_preset}</span>
				{#if data.truncated}<span class="text-yellow-400 ml-2">list truncated</span>{/if}
			</span>
		</div>

		<!--
			explain_pipeline() drops any strategy whose row build raised and reports
			it in data.errors rather than blanking the fleet. Without this the card
			just vanishes and every count above silently under-reports — a board
			that quietly shows you less than the truth is worse than one that admits
			it is incomplete.
		-->
		{#if data.errors?.length}
			<div
				class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400"
				data-testid="explain-errors"
			>
				<span class="font-bold uppercase tracking-wider">Incomplete view</span> —
				{data.errors.length}
				{data.errors.length === 1 ? 'strategy' : 'strategies'} could not be explained and
				{data.errors.length === 1 ? 'is' : 'are'} missing from the board and from the counts above:
				<span class="text-red-300">{data.errors.map((e) => e.id ?? '?').join(', ')}</span>
			</div>
		{/if}

		<!-- Stage columns -->
		<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
			{#each STAGE_ORDER as stage}
				{@const items = grouped[stage] ?? []}
				<section class="border border-[#222] bg-[#050505] overflow-hidden" data-testid="explain-column-{stage}">
					<div class="px-3 py-2.5 border-b border-[#222] flex justify-between items-center">
						<h3 class="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#666]">{STAGE_TITLES[stage]}</h3>
						<span class="text-[11px] text-[#555] tabular-nums">{items.length}</span>
					</div>
					{#if items.length === 0}
						<div class="px-3 py-6 text-center text-[#555] text-[11px]">No strategies</div>
					{:else}
						<div class="divide-y divide-[#1a1a1a]">
							{#each items as s (s.id)}
								{@const meta = statusMeta(s.status)}
								<article class="px-3 py-2.5" data-testid="explain-card-{s.id}">
									<div class="flex items-start justify-between gap-2">
										<div class="min-w-0">
											<a href="/lab/strategy/{encodeURIComponent(s.id)}" class="text-xs text-white hover:underline truncate block" title={s.name}>
												{s.display_id || s.id}<span class="text-[#666]"> · {s.name}</span>
											</a>
											<div class="text-[10px] text-[#666] mt-0.5">
												{s.symbol || '—'}{s.timeframe ? ` ${s.timeframe}` : ''}
												<span class="text-[#555]"> · in stage {fmtDays(s.days_in_stage)}</span>
												{#if s.rejections_in_stage > 0}
													<span class="text-[#555]"> · {s.rejections_in_stage} rejection{s.rejections_in_stage === 1 ? '' : 's'}</span>
												{/if}
											</div>
										</div>
										<span
											class="px-1.5 py-0.5 border text-[9px] font-bold uppercase tracking-wider whitespace-nowrap {meta.cls} {meta.pulse ? 'animate-pulse' : ''}"
											data-testid="explain-status-{s.id}"
										>{meta.label}</span>
									</div>

									{#if s.blockers.length > 0}
										<div class="mt-1.5 text-[10px] text-[#888] leading-snug" data-testid="explain-blocker-{s.id}" title={s.blockers[0].reason}>
											{s.blockers[0].reason}
										</div>
									{/if}

									{#if s.next_action}
										<div class="mt-1 text-[10px] text-sky-300" data-testid="explain-action-{s.id}">
											→ {s.next_action.label}
										</div>
									{/if}

									<button
										type="button"
										class="mt-1.5 text-[9px] uppercase tracking-wider text-[#555] hover:text-[#888]"
										on:click={() => toggle(s.id)}
										data-testid="explain-toggle-{s.id}"
									>
										{expanded[s.id] ? '▾ less' : '▸ details'}
									</button>

									{#if expanded[s.id]}
										<div class="mt-2 space-y-2 border-t border-[#1a1a1a] pt-2" data-testid="explain-detail-{s.id}">
											{#if s.next_transition}
												<div class="text-[10px]">
													<span class="text-[#555] uppercase">Next:</span>
													<span class="text-[#888]"> {s.next_transition.label}</span>
													{#if s.next_transition.trigger}
														<div class="text-[#666] mt-0.5">{s.next_transition.trigger}</div>
													{/if}
												</div>
											{/if}

											{#if s.blockers.length > 1}
												<div class="text-[10px] space-y-1">
													<div class="text-[#555] uppercase">All blockers</div>
													{#each s.blockers as b}
														<div class="text-[#888]">
															<span class="text-[#555]">[{b.kind}]</span> {b.reason}
															{#if b.extra && b.extra.current != null && b.extra.threshold != null}
																<span class="text-[#666]"> ({b.extra.current}/{b.extra.threshold} {b.extra.unit || ''})</span>
															{/if}
														</div>
													{/each}
												</div>
											{/if}

											{#if s.evidence.validation_tests && Object.keys(s.evidence.validation_tests).length > 0}
												<div class="text-[10px]">
													<div class="text-[#555] uppercase mb-1">Validation evidence</div>
													<div class="flex flex-wrap gap-1">
														{#each Object.entries(s.evidence.validation_tests) as [key, t]}
															<span class="px-1.5 py-0.5 border border-[#333] text-[9px] {t.verdict === 'PASS' ? 'text-emerald-400' : t.verdict === 'FAIL' ? 'text-red-400' : 'text-[#666]'}">
																{key}
																{#if t.age_days != null}<span class="text-[#555]"> {fmtDays(t.age_days)}</span>{/if}
																{#if t.stale}<span class="text-yellow-400"> STALE</span>{/if}
															</span>
														{/each}
													</div>
												</div>
											{/if}

											{#if s.evidence.last_backtest_age_days != null || s.evidence.last_optimization_age_days != null}
												<div class="text-[10px] text-[#666]">
													{#if s.evidence.last_backtest_age_days != null}Last backtest {fmtDays(s.evidence.last_backtest_age_days)} ago.{/if}
													{#if s.evidence.last_optimization_age_days != null} Last optimization {fmtDays(s.evidence.last_optimization_age_days)} ago.{/if}
												</div>
											{/if}

											{#if s.evidence.paper}
												<div class="text-[10px] text-[#888] space-y-0.5">
													{#if s.evidence.paper.paper_duration}
														<div>Paper days: <span class="tabular-nums">{s.evidence.paper.paper_duration.current}/{s.evidence.paper.paper_duration.threshold}</span></div>
													{/if}
													{#if s.evidence.paper.paper_trades}
														<div>Closed trades: <span class="tabular-nums">{s.evidence.paper.paper_trades.current}/{s.evidence.paper.paper_trades.threshold}</span></div>
													{/if}
													{#if s.evidence.paper.last_trade_age_days != null}
														<div>Last paper trade {fmtDays(s.evidence.paper.last_trade_age_days)} ago</div>
													{/if}
												</div>
											{/if}

											{#if s.pending_approval}
												<a
													href="/approval?approval_id={s.pending_approval.id}"
													class="block text-[10px] text-purple-300 hover:underline"
													data-testid="explain-approval-{s.id}"
												>
													Pending approval #{s.pending_approval.id} ({s.pending_approval.approval_type || 'approval'}) — review
												</a>
											{/if}

											{#if s.last_rejection}
												<div class="text-[10px] text-[#666]" title={s.last_rejection.reason_text || ''}>
													Last rejection at the {s.last_rejection.gate} gate {fmtDays(s.last_rejection.age_days)} ago
													<span class="text-[#555]">({s.last_rejection.reason_code})</span>
												</div>
											{/if}
										</div>
									{/if}
								</article>
							{/each}
						</div>
					{/if}
				</section>
			{/each}
		</div>
	{/if}
</div>
