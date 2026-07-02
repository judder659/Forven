<script lang="ts">
	import { createEventDispatcher, onDestroy, onMount } from 'svelte';
	import {
		getGauntletStatus,
		type GauntletStatus,
		type GauntletTestEntry,
		type GauntletTestKey,
	} from '$lib/api/lifecycle';
	import { getApprovals } from '$lib/api/forven';

	export let strategyId: string;
	export let stage: string | null | undefined = null;
	export let pollIntervalMs = 10_000;
	export let selectedTestKey: GauntletTestKey | null = null;
	export let testOverrides: Partial<Record<GauntletTestKey, GauntletTestEntry>> = {};

	const dispatch = createEventDispatcher<{
		refresh: { status: GauntletStatus };
		promote: { status: GauntletStatus };
		selectTest: { key: GauntletTestKey };
	}>();

	const TEST_LABELS: Record<GauntletTestKey, string> = {
		walk_forward: 'Walk-Forward',
		monte_carlo: 'Monte Carlo',
		parameter_jitter: 'Param Jitter',
		cost_stress: 'Cost Stress',
		regime_split: 'Regime Split',
	};

	const TEST_ORDER: GauntletTestKey[] = [
		'walk_forward',
		'monte_carlo',
		'parameter_jitter',
		'cost_stress',
		'regime_split',
	];
	const EMPTY_TESTS: Record<GauntletTestKey, GauntletTestEntry | null> = {
		walk_forward: null,
		monte_carlo: null,
		parameter_jitter: null,
		cost_stress: null,
		regime_split: null,
	};

	let status: GauntletStatus | null = null;
	let loading = true;
	let error: string | null = null;
	let pendingApprovalId: number | null = null;
	let pollHandle: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;

	$: isGauntlet = (stage ?? '').toLowerCase() === 'gauntlet';
	$: displayTests = status ? ({ ...status.tests, ...testOverrides } as Record<GauntletTestKey, GauntletTestEntry | null>) : EMPTY_TESTS;
	$: displayTestsCompleted = status ? TEST_ORDER.filter((key) => isCompleted(displayTests[key])).length : 0;
	$: displayTestsPassed = status ? TEST_ORDER.filter((key) => isPassed(displayTests[key])).length : 0;
	$: displayMissingRequired = status
		? status.required_tests.filter((key) => !isPassed(displayTests[key]))
		: [];
	// ready_for_paper only means required-test verdicts passed + stage; the real
	// transition gate also enforces the robustness floor (and drawdown/return/etc.).
	// Surface the most common additional failure — composite below floor — so the
	// banner isn't falsely green.
	$: meetsRobustnessFloor =
		!status || status.composite_robustness_score == null || status.min_robustness_score == null
			? true
			: Number(status.composite_robustness_score) >= Number(status.min_robustness_score);
	$: hasInFlight = !!status && TEST_ORDER.some((k) => {
		const s = (displayTests[k]?.status ?? '').toLowerCase();
		return s === 'submitted' || s === 'running' || s === 'queued' || s === 'pending';
	});
	$: staleTests = status ? TEST_ORDER.filter((k) => displayTests[k]?.stale === true) : [];

	function cancelPoll() {
		if (pollHandle) {
			clearTimeout(pollHandle);
			pollHandle = null;
		}
	}

	function schedulePoll() {
		cancelPoll();
		if (destroyed || !hasInFlight || pollIntervalMs <= 0) return;
		pollHandle = setTimeout(() => {
			void load({ silent: true });
		}, pollIntervalMs);
	}

	async function loadPendingApproval() {
		try {
			const rows = await getApprovals({
				status: 'pending',
				approval_type: 'strategy_promotion_approval',
				target_id: strategyId,
				limit: 1,
			});
			const first = Array.isArray(rows) ? rows[0] : null;
			const id = first && typeof first.id === 'number' ? first.id : null;
			pendingApprovalId = id;
		} catch {
			pendingApprovalId = null;
		}
	}

	async function load(opts: { silent?: boolean } = {}) {
		if (!opts.silent) loading = true;
		error = null;
		try {
			const next = await getGauntletStatus(strategyId);
			status = next;
			dispatch('refresh', { status: next });
			void loadPendingApproval();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load gauntlet status';
		} finally {
			loading = false;
			schedulePoll();
		}
	}

	export async function refresh() {
		await load();
	}

	onMount(() => {
		void load();
	});

	onDestroy(() => {
		destroyed = true;
		cancelPoll();
	});

	let lastStrategyId = strategyId;
	$: if (strategyId !== lastStrategyId) {
		lastStrategyId = strategyId;
		status = null;
		pendingApprovalId = null;
		void load();
	}

	function formatScore(n: number | null | undefined): string {
		if (n == null || Number.isNaN(Number(n))) return '--';
		const v = Number(n);
		return v >= 100 ? '100' : v.toFixed(1);
	}

	function scoreTone(score: number | null | undefined, min: number | null | undefined): string {
		if (score == null) return 'border-[#2a2a2a] bg-[#0d0d0d] text-gray-400';
		const v = Number(score);
		const floor = min == null ? 50 : Number(min);
		if (v >= floor + 20) return 'border-emerald-800/40 bg-emerald-950/20 text-emerald-200';
		if (v >= floor) return 'border-yellow-800/40 bg-yellow-950/20 text-yellow-200';
		return 'border-red-900/40 bg-red-950/20 text-red-200';
	}

	function pillTone(entry: GauntletTestEntry | null | undefined): string {
		const s = (entry?.status ?? 'not_started').toLowerCase();
		const v = (entry?.verdict ?? '').toUpperCase();
		if ((s === 'succeeded' || s === 'passed') && (!v || v === 'PASS')) {
			return 'border-emerald-800/40 bg-emerald-950/30 text-emerald-200';
		}
		if ((s === 'succeeded' || s === 'passed' || s === 'failed_gate') && v === 'FAIL') {
			return 'border-red-900/40 bg-red-950/30 text-red-200';
		}
		if (s === 'failed' || s === 'error' || s === 'failed_gate' || s === 'blocked_runtime' || s === 'blocked_data' || s === 'blocked_operator') {
			return 'border-red-900/40 bg-red-950/30 text-red-200';
		}
		if (s === 'running' || s === 'submitted' || s === 'queued' || s === 'pending') {
			return 'border-cyan-800/40 bg-cyan-950/30 text-cyan-200 animate-pulse';
		}
		return 'border-[#2a2a2a] bg-[#0b0b0b] text-gray-500';
	}

	function pillLabel(entry: GauntletTestEntry | null | undefined): string {
		const s = (entry?.status ?? 'not_started').toLowerCase();
		const v = (entry?.verdict ?? '').toUpperCase();
		if ((s === 'succeeded' || s === 'passed') && v) return v;
		if (s === 'succeeded' || s === 'passed') return 'PASS';
		if (s === 'running' || s === 'submitted') return 'RUN';
		if (s === 'queued' || s === 'pending') return 'QUEUED';
		if (s === 'failed_gate') return 'FAIL';
		if (s === 'blocked_runtime' || s === 'blocked_data' || s === 'blocked_operator') return 'BLOCK';
		if (s === 'failed' || s === 'error') return 'ERR';
		if (s === 'skipped') return 'SKIP';
		if (s === 'cancelled') return 'CXL';
		if (s === 'not_started') return 'OFF';
		return s.toUpperCase().slice(0, 6);
	}

	function isCompleted(entry: GauntletTestEntry | null | undefined): boolean {
		const s = (entry?.status ?? '').toLowerCase();
		// All TERMINAL statuses count as completed (mirrors the backend's
		// STEP_TERMINAL_STATUSES) so blocked/skipped/cancelled steps aren't undercounted.
		return (
			s === 'succeeded' || s === 'passed' || s === 'failed_gate' || s === 'failed' || s === 'error' ||
			s === 'blocked_runtime' || s === 'blocked_data' || s === 'blocked_operator' ||
			s === 'skipped' || s === 'cancelled'
		);
	}

	function isPassed(entry: GauntletTestEntry | null | undefined): boolean {
		const s = (entry?.status ?? '').toLowerCase();
		const v = (entry?.verdict ?? '').toUpperCase();
		return (s === 'succeeded' || s === 'passed') && (!v || v === 'PASS');
	}

	function tileTone(key: GauntletTestKey): string {
		if (selectedTestKey === key) {
			return 'border-cyan-700/60 bg-cyan-950/20 text-cyan-100 ring-1 ring-cyan-500/30';
		}
		return 'border-[#1d1d1d] bg-black/40 text-gray-400 hover:border-[#333] hover:text-gray-200';
	}
</script>

<div
	data-testid="gauntlet-status-card"
	class="rounded border border-[#1d1d1d] bg-[linear-gradient(180deg,#0b0b0b_0%,#070707_100%)] p-3 space-y-3"
>
	<div class="flex items-center justify-between gap-3">
		<div class="flex items-center gap-2">
			<span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-gray-400">Gauntlet Status</span>
			{#if hasInFlight}
				<span class="rounded-full border border-cyan-800/40 bg-cyan-950/30 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-cyan-200 animate-pulse">Live</span>
			{/if}
		</div>
		<button
			type="button"
			data-testid="gauntlet-status-refresh"
			on:click={() => void load()}
			disabled={loading}
			class="rounded border border-[#2a2a2a] bg-black px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] text-gray-400 hover:text-gray-200 disabled:opacity-50"
		>
			{loading ? 'Loading...' : 'Refresh'}
		</button>
	</div>

	{#if error}
		<div class="rounded border border-red-900/40 bg-red-950/20 px-2.5 py-2 text-[11px] text-red-300">
			{error}
		</div>
	{:else if !status && loading}
		<div class="py-2 text-center text-[11px] text-gray-500">Loading gauntlet status...</div>
	{:else if status && !status.ok}
		<div class="rounded border border-yellow-900/40 bg-yellow-950/20 px-2.5 py-2 text-[11px] text-yellow-300">
			{status.error ?? 'Gauntlet status unavailable'}
		</div>
	{:else if status}
		<div class="flex flex-wrap items-center gap-3">
			<div
				data-testid="gauntlet-composite-score"
				class={`rounded border px-3 py-2 ${scoreTone(status.composite_robustness_score, status.min_robustness_score)}`}
				title={status.min_robustness_score != null ? `Gauntlet threshold: ${status.min_robustness_score}` : ''}
			>
				<div class="text-[9px] font-semibold uppercase tracking-[0.18em] opacity-80">Composite</div>
				<div class="text-lg font-bold leading-tight">{formatScore(status.composite_robustness_score)}</div>
				<div class="text-[9px] uppercase tracking-[0.12em] opacity-70">
					/ 100{status.min_robustness_score != null ? ` · floor ${status.min_robustness_score}` : ''}
				</div>
			</div>
			<div class="flex flex-col gap-0.5 text-[11px]">
				<div class="text-gray-400">
					<span class="text-gray-200 font-medium">{displayTestsPassed}</span> / {status.tests_total} passed
					<span class="text-gray-600">·</span>
					<span class="text-gray-200 font-medium">{displayTestsCompleted}</span> / {status.tests_total} completed
				</div>
				{#if status.required_tests.length}
					<div class="text-[10px] text-gray-500">
						Required:
						{#each status.required_tests as rt, i}
							{@const reqMissing = displayMissingRequired.includes(rt)}
							<span
								class={reqMissing ? 'text-red-300' : 'text-emerald-300'}
								aria-label={`${TEST_LABELS[rt] ?? rt} ${reqMissing ? 'pending' : 'passed'}`}
							>
								<span aria-hidden="true">{reqMissing ? '✗' : '✓'}</span>
								{TEST_LABELS[rt] ?? rt}{i < status.required_tests.length - 1 ? ',' : ''}
							</span>
							{' '}
						{/each}
					</div>
				{/if}
			</div>
		</div>

		<div class="grid grid-cols-5 gap-1.5">
			{#each TEST_ORDER as key}
				{@const entry = displayTests[key] ?? null}
				<button
					type="button"
					data-testid={`gauntlet-test-${key}`}
					aria-pressed={selectedTestKey === key}
					on:click={() => dispatch('selectTest', { key })}
					class={`rounded border px-2 py-1.5 text-left transition focus:outline-none focus:ring-1 focus:ring-cyan-500/50 ${tileTone(key)}`}
					title={entry?.stale
						? `${TEST_LABELS[key]}: strategy params changed AFTER this test ran — its verdict no longer describes the current strategy. Re-run to revalidate.`
						: entry?.error
							? `${TEST_LABELS[key]}: ${entry.error}`
							: TEST_LABELS[key]}
				>
					<div class="truncate text-[10px] uppercase tracking-[0.12em] text-current opacity-70">{TEST_LABELS[key]}</div>
					<div class="mt-0.5 flex items-center gap-1">
						<span
							data-testid={`gauntlet-test-verdict-${key}`}
							class={`inline-block rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em] ${pillTone(entry)}`}
						>
							{pillLabel(entry)}
						</span>
						{#if entry?.stale}
							<span
								data-testid={`gauntlet-test-stale-${key}`}
								class="inline-block rounded-full border border-amber-700/60 bg-amber-950/40 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em] text-amber-200"
							>
								Stale
							</span>
						{/if}
					</div>
				</button>
			{/each}
		</div>

		{#if staleTests.length > 0}
			<div data-testid="gauntlet-stale-warning" class="rounded border border-amber-900/40 bg-amber-950/15 px-2.5 py-2 text-[11px] text-amber-200">
				Params changed since {staleTests.map((k) => TEST_LABELS[k] ?? k).join(', ')} ran — those verdicts describe the OLD configuration. Re-run to revalidate.
			</div>
		{/if}

		{#if displayMissingRequired.length > 0 && isGauntlet}
			<div class="rounded border border-yellow-900/40 bg-yellow-950/15 px-2.5 py-2 text-[11px] text-yellow-200">
				Missing required: {displayMissingRequired.map((k) => TEST_LABELS[k] ?? k).join(', ')}
			</div>
		{/if}

		{#if pendingApprovalId != null}
			<div
				data-testid="gauntlet-approval-pending"
				class="flex items-center justify-between gap-2 rounded border border-violet-800/40 bg-violet-950/20 px-2.5 py-2 text-[11px] text-violet-200"
			>
				<span>Operator approval pending (#{pendingApprovalId}) — promotion is queued for review.</span>
				<a
					href={`/approval?approval_id=${pendingApprovalId}`}
					class="rounded border border-violet-700/50 bg-violet-950/40 px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] text-violet-100 hover:bg-violet-900/50"
				>
					Review
				</a>
			</div>
		{:else if status.ready_for_paper}
			<div
				data-testid="gauntlet-ready-for-paper"
				class={`flex items-center justify-between gap-2 rounded border px-2.5 py-2 text-[11px] ${meetsRobustnessFloor ? 'border-emerald-800/40 bg-emerald-950/20 text-emerald-200' : 'border-amber-800/40 bg-amber-950/20 text-amber-200'}`}
			>
				<span>
					{#if meetsRobustnessFloor}
						Required robustness tests passed — run the final promotion check.
					{:else}
						Required tests passed, but composite {formatScore(status.composite_robustness_score)} is below the {status.min_robustness_score} floor — the promotion check will likely reject.
					{/if}
				</span>
				<button
					type="button"
					on:click={() => status && dispatch('promote', { status })}
					class={`rounded border px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] ${meetsRobustnessFloor ? 'border-emerald-700/50 bg-emerald-950/40 text-emerald-100 hover:bg-emerald-900/50' : 'border-amber-700/50 bg-amber-950/40 text-amber-100 hover:bg-amber-900/50'}`}
				>
					Run check
				</button>
			</div>
		{/if}
	{/if}
</div>
