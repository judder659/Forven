<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import {
		getPromotionReadiness,
		getPaperLiveReadiness,
		runTimeframeSweep,
		type PromotionReadiness,
		type ReadinessStep,
	} from '$lib/api/lifecycle';
	import { addToast } from '$lib/stores/processTracker';
	import type { QuickScreenEvidenceRow } from '$lib/utils/quickScreenReadiness';

	export let strategyId: string;
	export let stage: string = 'gauntlet';
	export let quickScreenRows: QuickScreenEvidenceRow[] = [];

	const dispatch = createEventDispatcher();

	let readiness: PromotionReadiness | null = null;
	let loading = true;
	let error: string | null = null;
	let actionRunning: string | null = null;

	$: isPaperStage = stage === 'paper' || stage === 'paper_trading';
	$: isGauntletStage = stage === 'gauntlet';
	$: isQuickScreen = stage === 'quick_screen';
	$: isLiveGraduated = stage === 'live_graduated';
	$: hasChecklist = isQuickScreen || isGauntletStage || isPaperStage;

	const stageTitle: Record<string, string> = {
		quick_screen: 'Quick Screen -> Gauntlet',
		gauntlet: 'Gauntlet -> Paper Trading',
		paper: 'Paper Trading -> Live',
		paper_trading: 'Paper Trading -> Live',
		live_graduated: 'Live Graduated',
	};

	const liveGraduatedInfo = [
		{ name: 'Allocation Schedule', desc: 'Weeks 1-2: 25%, Weeks 3-4: 50%, Week 5+: 100%' },
		{ name: 'Kill Switch', desc: '30% drawdown triggers automatic position closure' },
		{ name: 'Weekly Review', desc: 'Bottom 20% retired, top performers logged' },
	];

	async function loadReadiness() {
		if (!hasChecklist) {
			loading = false;
			return;
		}
		if (isQuickScreen) {
			readiness = null;
			error = null;
			loading = false;
			return;
		}
		loading = true;
		error = null;
		try {
			const paper = stage === 'paper' || stage === 'paper_trading';
			readiness = paper
				? await getPaperLiveReadiness(strategyId)
				: await getPromotionReadiness(strategyId);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load readiness';
		} finally {
			loading = false;
		}
	}

	onMount(loadReadiness);

	let readinessKey = `${strategyId}::${stage}`;
	let prevReadinessKey = readinessKey;
	$: readinessKey = `${strategyId}::${stage}`;
	$: if (readinessKey !== prevReadinessKey) {
		prevReadinessKey = readinessKey;
		readiness = null;
		loadReadiness();
	}

	const stepLabels: Record<string, string> = {
		multi_tf_sweep: 'Multi-Timeframe Gauntlet Sweep',
		validation_artifacts: 'Validation Suite (WFA, MC, Jitter, Cost, Regime)',
		paper_duration: 'Paper Trading Duration',
		paper_trades: 'Paper Trade Count',
		paper_return: 'Paper Return',
		paper_drawdown: 'Paper Drawdown',
		optimization: 'Parameter Optimization',
		params_applied: 'Optimized Params Applied',
		confirmation_backtest: 'Confirmation Gauntlet',
	};

	const compactStepLabels: Record<string, string> = {
		multi_tf_sweep: 'Multi-Timeframe Sweep',
		validation_artifacts: 'Validation Suite',
		paper_duration: 'Paper Trading Duration',
		paper_trades: 'Paper Trade Count',
		paper_return: 'Paper Return',
		paper_drawdown: 'Paper Drawdown',
		optimization: 'Parameter Optimization',
		params_applied: 'Optimized Params Applied',
		confirmation_backtest: 'Confirmation Gauntlet',
	};

	const actionLabels: Record<string, string> = {
		run_timeframe_sweep: 'Run TF Sweep',
		run_optimization: 'Run Optimization',
		apply_best_params: 'Apply Best Params',
		run_confirmation_backtest: 'Run the Gauntlet',
		run_validation_suite: 'Run Validation',
		re_run_validation_suite: 'Re-run Validation',
	};

	// Numeric progress payload the paper checks attach (current/threshold/direction).
	// Older backends return no extra — the row degrades to detail text only.
	type ProgressExtra = { current: number; threshold: number; direction: string; unit?: string };

	function progressExtra(step: ReadinessStep): ProgressExtra | null {
		const extra = step.extra;
		if (!extra || typeof extra !== 'object' || Array.isArray(extra)) return null;
		const record = extra as Record<string, unknown>;
		const current = Number(record.current);
		const threshold = Number(record.threshold);
		const direction = String(record.direction ?? '').trim();
		if (!Number.isFinite(current) || !Number.isFinite(threshold) || !direction) return null;
		return {
			current,
			threshold,
			direction,
			unit: typeof record.unit === 'string' ? record.unit : undefined,
		};
	}

	function progressPct(info: ProgressExtra): number {
		if (info.threshold <= 0) return info.current > 0 ? 100 : 0;
		return Math.max(0, Math.min(100, (info.current / info.threshold) * 100));
	}

	function progressTone(info: ProgressExtra, status: string): string {
		if (info.direction === 'lt' || info.direction === 'lte') {
			// The bar shows usage of a LIMIT (e.g. drawdown): filling up is bad.
			const usage = progressPct(info);
			if (usage >= 100) return 'bg-red-500';
			if (usage >= 75) return 'bg-yellow-400';
			return 'bg-emerald-500';
		}
		return status === 'passed' ? 'bg-emerald-500' : 'bg-white';
	}

	function progressValueLabel(info: ProgressExtra): string {
		const unit = info.unit === '%' ? '%' : info.unit ? ` ${info.unit}` : '';
		return `${info.current}${unit}`;
	}

	function progressTargetLabel(info: ProgressExtra): string {
		const unit = info.unit === '%' ? '%' : info.unit ? ` ${info.unit}` : '';
		const word = info.direction === 'lt' || info.direction === 'lte' ? 'limit' : 'target';
		return `${word} ${info.threshold}${unit}`;
	}

	function statusLabel(status: string): string {
		switch (status) {
			case 'passed':
				return 'Passed';
			case 'warning':
				return 'Warning';
			case 'skipped':
				return 'Skipped';
			default:
				return 'Blocked';
		}
	}

	function statusTone(status: string): string {
		switch (status) {
			case 'passed':
				return 'border-emerald-800/40 bg-emerald-950/15';
			case 'warning':
				return 'border-yellow-800/40 bg-yellow-950/15';
			case 'skipped':
				return 'border-[#222] bg-[#111]';
			default:
				return 'border-red-800/40 bg-red-950/15';
		}
	}

	function statusBadgeClass(status: string): string {
		switch (status) {
			case 'passed':
				return 'border border-emerald-700/50 bg-emerald-950/40 text-emerald-300';
			case 'warning':
				return 'border border-yellow-700/50 bg-yellow-950/40 text-yellow-200';
			case 'skipped':
				return 'border border-[#333] bg-[#111] text-[#888]';
			default:
				return 'border border-red-700/50 bg-red-950/40 text-red-200';
		}
	}

	async function handleAction(step: ReadinessStep) {
		if (!step.actionable || actionRunning) return;
		actionRunning = step.actionable;

		try {
			switch (step.actionable) {
				case 'run_timeframe_sweep': {
					const result = await runTimeframeSweep(strategyId);
					if (result.ok) {
						const failedCount = result.errors?.length ?? 0;
						addToast(
							`TF sweep: ${result.submitted?.length ?? 0} submitted, ${result.skipped?.length ?? 0} already done${failedCount ? `, ${failedCount} failed` : ''}`,
							failedCount ? 'error' : 'success'
						);
					}
					break;
				}
				case 'run_optimization':
				case 'run_confirmation_backtest':
				case 'run_validation_suite':
				case 're_run_validation_suite':
				case 'apply_best_params':
					dispatch('action', { action: step.actionable, strategyId });
					break;
			}
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Action failed', 'error');
		} finally {
			actionRunning = null;
			await loadReadiness();
		}
	}
</script>

<div class="space-y-2 border border-[#1a1a1a] bg-[#0a0a0a] p-2.5">
	<div class="flex items-center justify-between">
		<h3 class="text-[10px] font-semibold uppercase tracking-wide text-[#888]">
			{stageTitle[stage] || 'Promotion Requirements'}
		</h3>
		{#if hasChecklist && !isQuickScreen}
			<button
				on:click={loadReadiness}
				disabled={loading}
				class="text-[10px] text-[#666] transition-colors hover:text-white"
			>
				{loading ? 'Loading...' : 'Refresh'}
			</button>
		{/if}
	</div>

	{#if isQuickScreen}
		<div class="space-y-1.5">
			{#each quickScreenRows as step}
				<div
					data-testid={`readiness-row-qs-${step.key}`}
					class={`border px-2.5 py-2 ${statusTone(step.status)}`}
				>
					<div class="flex items-start justify-between gap-3">
						<div class="min-w-0 flex-1 space-y-1">
							<div class="text-[11px] font-medium uppercase tracking-[0.12em] text-white">
								{step.label}
							</div>
							<div
								data-testid={`readiness-detail-qs-${step.key}`}
								class="text-[11px] leading-relaxed text-[#888]"
							>
								{step.detail}
							</div>
						</div>
						<span
							data-testid={`readiness-status-qs-${step.key}`}
							class={`shrink-0 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${statusBadgeClass(step.status)}`}
						>
							{statusLabel(step.status)}
						</span>
					</div>
				</div>
			{/each}
		</div>
	{:else if isLiveGraduated}
		<div class="space-y-1">
			{#each liveGraduatedInfo as info}
				<div class="flex items-center gap-2 border border-[#1a1a1a] bg-black/30 px-2 py-1.5">
					<span class="text-xs text-[#666]">-</span>
					<div class="min-w-0 flex-1">
						<div class="text-xs font-medium text-white">{info.name}</div>
						<div class="text-[11px] text-[#666]">{info.desc}</div>
					</div>
				</div>
			{/each}
		</div>
	{:else if error}
		<div class="border border-red-900 bg-red-500/5 px-2 py-1.5 text-xs text-red-400">
			{error}
		</div>
	{:else if readiness}
		<div
			class="flex items-center gap-2 px-2 py-1.5 text-xs {readiness.ready ? 'border border-emerald-800/30 bg-emerald-950/20' : 'border border-red-800/30 bg-red-950/20'}"
		>
			<span class="font-bold {readiness.ready ? 'text-emerald-400' : 'text-red-400'}">
				{readiness.ready ? '+' : 'x'}
			</span>
			<span class="font-medium {readiness.ready ? 'text-emerald-300' : 'text-red-300'}">
				{readiness.ready
					? isPaperStage ? 'Ready for live graduation' : 'Ready for promotion'
					: isPaperStage ? 'Not ready for live - complete steps below' : 'Not ready - complete steps below'}
			</span>
		</div>

		<div class="space-y-1.5">
			{#each readiness.steps as step}
				{@const progress = progressExtra(step)}
				<div
					data-testid={`readiness-row-${step.name}`}
					class={`border px-2.5 py-2 ${statusTone(step.status)}`}
				>
					<div class="flex items-start justify-between gap-3">
						<div class="min-w-0 flex-1 space-y-1">
							<div class="text-[11px] font-medium uppercase tracking-[0.12em] text-white">
								{compactStepLabels[step.name] || stepLabels[step.name] || step.name}
							</div>
							<div
								data-testid={`readiness-detail-${step.name}`}
								class="text-[11px] leading-relaxed text-[#888]"
							>
								{step.detail}
							</div>
							{#if progress}
								<div data-testid={`readiness-progress-${step.name}`}>
									<div class="h-1.5 w-full bg-[#222]">
										<div
											class={`h-1.5 transition-all ${progressTone(progress, step.status)}`}
											style={`width: ${progressPct(progress)}%`}
										></div>
									</div>
									<div class="mt-0.5 flex justify-between text-[10px] text-[#666]">
										<span>{progressValueLabel(progress)}</span>
										<span>{progressTargetLabel(progress)}</span>
									</div>
								</div>
							{/if}
						</div>

						<div class="flex shrink-0 items-center gap-2">
							<span
								data-testid={`readiness-status-${step.name}`}
								class={`px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${statusBadgeClass(step.status)}`}
							>
								{statusLabel(step.status)}
							</span>

							{#if step.actionable && step.status !== 'passed'}
								<button
									data-testid={`readiness-action-${step.name}`}
									on:click={() => handleAction(step)}
									disabled={Boolean(actionRunning)}
									class="shrink-0 border border-[#333] bg-[#111] px-2 py-0.5 text-[10px] font-medium text-white transition-colors hover:border-white disabled:cursor-not-allowed disabled:opacity-50"
								>
									{actionRunning === step.actionable
										? 'Running...'
										: actionLabels[step.actionable] || step.actionable}
								</button>
							{/if}
						</div>
					</div>
				</div>
			{/each}
		</div>
	{:else if loading}
		<div class="py-2 text-center text-xs text-[#666]">Loading readiness checks...</div>
	{/if}
</div>
