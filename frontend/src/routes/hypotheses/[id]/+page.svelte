<script lang="ts">
	import { page } from '$app/stores';
	import { onDestroy, onMount } from 'svelte';

	import {
		ApiError,
		archiveHypothesis,
		forceRevisitHypothesis,
		generateHypothesisStrategies,
		getHypothesisDetail,
		reopenHypothesis,
		restoreHypothesis,
		retriggerHypothesisResearch,
		trashHypothesis,
		triggerVerdict,
		updateHypothesis,
		type HypothesisDetailResponse,
		type HypothesisDetailStrategy,
		type HypothesisQuality,
	} from '$lib/api';
	import AgentActivityPanel from '$lib/components/hypotheses/AgentActivityPanel.svelte';
	import DataGapLeaderboard from '$lib/components/hypotheses/DataGapLeaderboard.svelte';
	import HypothesisArtifacts from '$lib/components/hypotheses/HypothesisArtifacts.svelte';
	import SourceTags from '$lib/components/hypotheses/SourceTags.svelte';
	import VerdictMemoCard from '$lib/components/hypotheses/VerdictMemoCard.svelte';
	import {
		crucibleStatusClasses,
		crucibleStatusLabel,
		forgeStageLabel,
		forgeStatusClasses,
		forgeStatusLabel,
		managerStateLabel,
		originClasses,
		originLabel,
		protectionBadge,
	} from '$lib/crucible';
	import { buildStrategyHref } from '$lib/utils/strategyLinks';

	let detail: HypothesisDetailResponse | null = null;
	let loading = true;
	let error: string | null = null;
	let latestRequestId = 0;
	let mutationPending = false;
	let banner: { tone: 'success' | 'error'; message: string } | null = null;
	let bannerTimer: ReturnType<typeof setTimeout> | null = null;

	// In-app confirmation prompt (replaces native window.confirm / unguarded destructive actions)
	let pendingConfirm:
		| {
				message: string;
				confirmLabel: string;
				tone: 'danger' | 'warn';
				onConfirm: () => void | Promise<void>;
		  }
		| null = null;

	let includeContent = false;

	function setBanner(next: { tone: 'success' | 'error'; message: string } | null): void {
		if (bannerTimer) {
			clearTimeout(bannerTimer);
			bannerTimer = null;
		}
		banner = next;
		if (next && next.tone === 'success') {
			bannerTimer = setTimeout(() => {
				banner = null;
				bannerTimer = null;
			}, 6000);
		}
	}

	function dismissBanner(): void {
		setBanner(null);
	}

	function requestConfirm(prompt: NonNullable<typeof pendingConfirm>): void {
		pendingConfirm = prompt;
	}

	function cancelConfirm(): void {
		pendingConfirm = null;
	}

	async function acceptConfirm(): Promise<void> {
		const prompt = pendingConfirm;
		pendingConfirm = null;
		if (prompt) {
			await prompt.onConfirm();
		}
	}

	async function loadDetail(hypothesisId: string): Promise<void> {
		const requestId = ++latestRequestId;
		loading = true;
		error = null;
		try {
			const response = await getHypothesisDetail(hypothesisId, { includeContent });
			if (requestId !== latestRequestId) {
				return;
			}
			detail = response;
		} catch (err) {
			if (requestId !== latestRequestId) {
				return;
			}
			error = err instanceof Error ? err.message : 'Failed to load hypothesis.';
			detail = null;
		} finally {
			if (requestId === latestRequestId) {
				loading = false;
			}
		}
	}

	function confirmTrash(): void {
		requestConfirm({
			message: 'Move this crucible to trash? You can restore it from the Trash view afterwards.',
			confirmLabel: 'Move to trash',
			tone: 'danger',
			onConfirm: () => mutateLifecycle('trash'),
		});
	}

	async function mutateLifecycle(action: 'archive' | 'trash' | 'restore'): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			const response =
				action === 'archive'
					? await archiveHypothesis(hypothesis.id)
					: action === 'trash'
						? await trashHypothesis(hypothesis.id)
						: await restoreHypothesis(hypothesis.id);

			if (detail) {
				detail = {
					...detail,
					hypothesis: {
						...detail.hypothesis,
						...response.hypothesis,
					},
				};
			}
			setBanner({
				tone: 'success',
				message:
					action === 'archive'
						? 'Crucible archived.'
						: action === 'trash'
							? 'Crucible moved to trash.'
							: 'Crucible restored.',
			});
		} catch (err) {
			setBanner({
				tone: 'error',
				message: err instanceof Error ? err.message : 'Lifecycle action failed.',
			});
		} finally {
			mutationPending = false;
		}
	}

	function strategyHref(strategy: HypothesisDetailStrategy, hypothesisId: string): string {
		return buildStrategyHref(strategy.id, { returnTo: `/hypotheses/${hypothesisId}` });
	}

	async function doReopen(rationale: string): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			await reopenHypothesis(hypothesis.id, rationale || undefined);
			await loadDetail(hypothesis.id);
			setBanner({ tone: 'success', message: 'Crucible reopened.' });
		} catch (err) {
			setBanner({
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to reopen hypothesis.',
			});
		} finally {
			mutationPending = false;
		}
	}

	async function runVerdict(): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			const res = await triggerVerdict(hypothesis.id);
			if (res.ok) {
				setBanner({ tone: 'success', message: 'Verdict requested — refreshing memo.' });
				await loadDetail(hypothesis.id);
			} else {
				setBanner({
					tone: 'error',
					message: res.error_code
						? `Verdict failed: ${res.error_code}`
						: 'Verdict could not be produced.',
				});
			}
		} catch (err) {
			setBanner({
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to request verdict.',
			});
		} finally {
			mutationPending = false;
		}
	}

	async function runRevisit(): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			await forceRevisitHypothesis(hypothesis.id);
			await loadDetail(hypothesis.id);
			setBanner({ tone: 'success', message: 'Crucible revisited — now active.' });
		} catch (err) {
			setBanner({
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to revisit hypothesis.',
			});
		} finally {
			mutationPending = false;
		}
	}

	$: hypothesis = detail?.hypothesis ?? null;
	$: linkedStrategies = detail?.strategies ?? [];
	$: artifacts = detail?.artifacts ?? [];
	$: dataGaps = detail?.data_gaps ?? [];
	$: researchTask = detail?.research_task ?? null;
	$: agentActivity = detail?.agent_activity ?? [];
	$: hypothesisRouteId = hypothesis?.display_id || hypothesis?.id || '';
	$: currentReturnTo = `${$page.url.pathname}${$page.url.search}`;
	$: quality = hypothesis?.quality as HypothesisQuality | undefined;
	$: detailProtBadge = protectionBadge(hypothesis?.protection_status);
	$: verdictSignals = hypothesis?.verdict_signals ?? hypothesis?.verdict_memo?.signals ?? null;

	// Inline-edit state
	let editing = false;
	let editTitle = '';
	let editThesis = '';
	let editMechanism = '';
	let editNotes = '';
	let editWhyNow = '';
	let editAssets = '';
	let editTimeframes = '';

	function startEdit(): void {
		if (!hypothesis) return;
		editTitle = hypothesis.title;
		editThesis = hypothesis.market_thesis;
		editMechanism = hypothesis.mechanism;
		editNotes = hypothesis.operator_notes || '';
		editWhyNow = hypothesis.why_now || '';
		editAssets = (hypothesis.target_assets || []).join(', ');
		editTimeframes = (hypothesis.target_timeframes || []).join(', ');
		editing = true;
	}

	function cancelEdit(): void {
		editing = false;
		setBanner(null);
	}

	function parseList(value: string): string[] {
		return value
			.split(',')
			.map((entry) => entry.trim())
			.filter((entry) => entry.length > 0);
	}

	async function saveEdit(): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			await updateHypothesis(hypothesis.id, {
				title: editTitle.trim(),
				market_thesis: editThesis.trim(),
				mechanism: editMechanism.trim(),
				operator_notes: editNotes.trim(),
				why_now: editWhyNow.trim(),
				target_assets: parseList(editAssets),
				target_timeframes: parseList(editTimeframes),
			});
			setBanner({ tone: 'success', message: 'Crucible updated.' });
			editing = false;
			await loadDetail(hypothesis.id);
		} catch (err) {
			setBanner({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to update.' });
		} finally {
			mutationPending = false;
		}
	}

	async function runResearch(): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			const res = await retriggerHypothesisResearch(hypothesis.id);
			setBanner({
				tone: 'success',
				message: res.already_running ? 'Research already queued.' : 'Research task queued.',
			});
			await loadDetail(hypothesis.id);
		} catch (err) {
			setBanner({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to queue research.' });
		} finally {
			mutationPending = false;
		}
	}

	function extractErrorCode(err: unknown): { code: string; message: string } | null {
		if (!(err instanceof ApiError)) return null;
		const payload = err.payload as { detail?: unknown } | null | undefined;
		const detail = payload?.detail;
		if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
			const obj = detail as { error_code?: unknown; message?: unknown };
			if (typeof obj.error_code === 'string') {
				return {
					code: obj.error_code,
					message: typeof obj.message === 'string' ? obj.message : err.message,
				};
			}
		}
		return null;
	}

	async function runGenerateStrategies(force = false): Promise<void> {
		if (!hypothesis) return;
		mutationPending = true;
		setBanner(null);
		try {
			const res = await generateHypothesisStrategies(hypothesis.id, { force });
			setBanner({
				tone: 'success',
				message: res.already_running
					? 'Candidate strategy task already queued.'
					: 'Candidate strategy task queued.',
			});
			await loadDetail(hypothesis.id);
		} catch (err) {
			const coded = extractErrorCode(err);
			if (coded?.code === 'source_content_missing' && !force) {
				mutationPending = false;
				requestConfirm({
					message: `${coded.message} Proceed anyway?`,
					confirmLabel: 'Proceed anyway',
					tone: 'warn',
					onConfirm: () => runGenerateStrategies(true),
				});
				return;
			}
			setBanner({
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to queue strategy generation.',
			});
		} finally {
			mutationPending = false;
		}
	}

	async function toggleCachedContent(): Promise<void> {
		if (!hypothesis) return;
		includeContent = !includeContent;
		await loadDetail(hypothesis.id);
	}

	function formatPct(value: number | null | undefined): string {
		if (value === null || value === undefined) return '—';
		return `${value.toFixed(2)}%`;
	}

	function formatSharpe(value: number | null | undefined): string {
		if (value === null || value === undefined) return '—';
		return value.toFixed(2);
	}

	// Self-scheduling poll with exponential backoff (10s → 60s cap). Pauses when the
	// browser tab is hidden so a long-running research task stops hammering the detail
	// endpoint while the operator is looking elsewhere.
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let pollHypothesisId = '';
	let pollDelay = 10000;
	const POLL_MIN_DELAY = 10000;
	const POLL_MAX_DELAY = 60000;
	let tabHidden = false;

	function scheduleNextPoll(): void {
		if (pollTimer) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
		if (!pollHypothesisId || tabHidden) return;
		pollTimer = setTimeout(async () => {
			pollTimer = null;
			if (!pollHypothesisId || tabHidden) return;
			await loadDetail(pollHypothesisId);
			pollDelay = Math.min(pollDelay * 2, POLL_MAX_DELAY);
			scheduleNextPoll();
		}, pollDelay);
	}

	function startResearchPoll(hypothesisId: string): void {
		pollHypothesisId = hypothesisId;
		pollDelay = POLL_MIN_DELAY;
		scheduleNextPoll();
	}

	function stopResearchPoll(): void {
		pollHypothesisId = '';
		if (pollTimer) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
	}

	function handleVisibilityChange(): void {
		tabHidden = typeof document !== 'undefined' && document.hidden;
		if (tabHidden) {
			if (pollTimer) {
				clearTimeout(pollTimer);
				pollTimer = null;
			}
		} else if (pollHypothesisId) {
			// Tab back in focus: refresh once immediately and reset backoff.
			void loadDetail(pollHypothesisId);
			pollDelay = POLL_MIN_DELAY;
			scheduleNextPoll();
		}
	}

	$: {
		const hid = hypothesis?.id ?? '';
		if (researchTask && hid) {
			if (pollHypothesisId !== hid) startResearchPoll(hid);
		} else {
			stopResearchPoll();
		}
	}

	onDestroy(() => {
		stopResearchPoll();
		if (bannerTimer) {
			clearTimeout(bannerTimer);
			bannerTimer = null;
		}
	});

	onMount(() => {
		let activeHypothesisId = '';
		if (typeof document !== 'undefined') {
			tabHidden = document.hidden;
			document.addEventListener('visibilitychange', handleVisibilityChange);
		}
		const unsubscribe = page.subscribe(($page) => {
			const nextHypothesisId = String($page.params.id ?? '').trim();
			if (nextHypothesisId === activeHypothesisId) {
				return;
			}
			activeHypothesisId = nextHypothesisId;
			if (nextHypothesisId) {
				void loadDetail(nextHypothesisId);
				return;
			}
			latestRequestId += 1;
			detail = null;
			loading = false;
			error = 'Missing hypothesis id.';
		});
		return () => {
			unsubscribe();
			if (typeof document !== 'undefined') {
				document.removeEventListener('visibilitychange', handleVisibilityChange);
			}
		};
	});
</script>

<svelte:head>
	<title>{hypothesis?.title ? `${hypothesis.title} | Crucibles | Forven` : 'Crucible Detail | Forven'}</title>
</svelte:head>

<div class="min-h-full bg-[#050505] px-4 py-5 text-white md:px-6">
	<div class="border border-[#222] bg-[#050505] px-4 py-3">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<a href="/hypotheses" class="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-[#666] transition hover:text-white">
				<span aria-hidden="true">←</span>
				Back To Crucibles
			</a>
			{#if hypothesis}
				<div class="flex flex-wrap items-center gap-2">
					<span class="border border-[#333] bg-black px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#888]">
						{managerStateLabel(hypothesis.manager_state)}
					</span>
					{#if hypothesis.manager_state === 'active'}
						<button
							type="button"
							data-detail-action="research"
							on:click={runResearch}
							disabled={mutationPending}
							class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
							title="Queue a fresh strategy-developer research task"
						>
							{quality === 'researching' ? 'Re-queue research' : 'Re-research'}
						</button>
						<button
							type="button"
							data-detail-action="generate-strategies"
							on:click={() => runGenerateStrategies()}
							disabled={mutationPending}
							class="terminal-button-primary px-3 py-2 text-[11px] disabled:opacity-40"
							title="Send 1-3 candidate strategies to the Forge to prove/disprove this crucible"
						>
							Generate Candidate Strategies
						</button>
						{#if hypothesis.status !== 'disproven'}
							<button
								type="button"
								data-detail-action="verdict"
								on:click={runVerdict}
								disabled={mutationPending}
								class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
								title="Ask the agent to write a verdict memo for this crucible"
							>
								Request Verdict
							</button>
						{/if}
						<button
							type="button"
							data-detail-action="edit"
							on:click={startEdit}
							disabled={mutationPending || editing}
							class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
						>
							Edit
						</button>
						<button
							type="button"
							data-detail-action="archive"
							on:click={() => mutateLifecycle('archive')}
							disabled={mutationPending}
							class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
						>
							Archive
						</button>
						<button
							type="button"
							data-detail-action="trash"
							on:click={confirmTrash}
							disabled={mutationPending}
							class="border border-yellow-900 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-yellow-400 transition-colors hover:border-yellow-400 hover:text-yellow-300 disabled:opacity-40"
						>
							Delete
						</button>
					{:else if hypothesis.manager_state === 'archived'}
						<button
							type="button"
							data-detail-action="research"
							disabled
							class="cursor-not-allowed border border-[#222] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#444]"
							title="Restore this crucible to re-research or generate strategies"
						>
							Re-research
						</button>
						<button
							type="button"
							data-detail-action="restore"
							on:click={() => mutateLifecycle('restore')}
							disabled={mutationPending}
							class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
						>
							Restore
						</button>
						<button
							type="button"
							data-detail-action="trash"
							on:click={confirmTrash}
							disabled={mutationPending}
							class="border border-yellow-900 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-yellow-400 transition-colors hover:border-yellow-400 hover:text-yellow-300 disabled:opacity-40"
						>
							Delete
						</button>
					{:else if hypothesis.manager_state === 'graduated'}
						<button
							type="button"
							data-detail-action="research"
							disabled
							class="cursor-not-allowed border border-[#222] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#444]"
							title="Revisit this crucible to re-research or generate strategies"
						>
							Re-research
						</button>
						<button
							type="button"
							data-detail-action="revisit"
							on:click={runRevisit}
							disabled={mutationPending}
							class="border border-emerald-900 bg-emerald-500/10 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-emerald-400 transition-colors hover:bg-emerald-500/20 disabled:opacity-40"
							title="Move back to active pool to beat the canonical"
						>
							Revisit
						</button>
					{:else}
						<button
							type="button"
							data-detail-action="research"
							disabled
							class="cursor-not-allowed border border-[#222] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#444]"
							title="Restore this crucible to re-research or generate strategies"
						>
							Re-research
						</button>
						<button
							type="button"
							data-detail-action="restore"
							on:click={() => mutateLifecycle('restore')}
							disabled={mutationPending}
							class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
						>
							Restore
						</button>
					{/if}
				</div>
			{/if}
		</div>
	</div>

	{#if banner}
		<div class={`mt-4 flex items-start justify-between gap-3 border px-4 py-3 text-sm ${banner.tone === 'success' ? 'border-emerald-900 bg-emerald-500/10 text-emerald-400' : 'border-red-900 bg-red-500/5 text-red-400'}`}>
			<span>{banner.message}</span>
			<button
				type="button"
				on:click={dismissBanner}
				aria-label="Dismiss notification"
				class="shrink-0 text-base leading-none text-current opacity-60 transition hover:opacity-100"
			>
				×
			</button>
		</div>
	{/if}

	{#if pendingConfirm}
		<div class={`mt-4 flex flex-wrap items-center justify-between gap-3 border px-4 py-3 text-sm ${pendingConfirm.tone === 'danger' ? 'border-red-900 bg-red-500/5 text-red-400' : 'border-yellow-900 bg-yellow-500/5 text-yellow-400'}`}>
			<span>{pendingConfirm.message}</span>
			<div class="flex shrink-0 gap-2">
				<button
					type="button"
					data-detail-action="confirm-accept"
					on:click={acceptConfirm}
					disabled={mutationPending}
					class={`border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider transition-colors disabled:opacity-40 ${pendingConfirm.tone === 'danger' ? 'border-red-900 text-red-400 hover:bg-red-500/10' : 'border-yellow-900 text-yellow-400 hover:bg-yellow-500/10'}`}
				>
					{pendingConfirm.confirmLabel}
				</button>
				<button
					type="button"
					data-detail-action="confirm-cancel"
					on:click={cancelConfirm}
					disabled={mutationPending}
					class="border border-[#333] px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
				>
					Cancel
				</button>
			</div>
		</div>
	{/if}

	{#if error}
		<div class="mt-4 border border-red-900 bg-red-500/5 px-4 py-3 text-sm text-red-400">{error}</div>
	{:else if loading}
		<div class="mt-4 border border-[#222] bg-black p-6">
			<div class="h-4 w-32 animate-pulse bg-[#1a1a1a]"></div>
			<div class="mt-4 h-8 w-2/3 animate-pulse bg-[#1a1a1a]"></div>
			<div class="mt-4 h-3 w-full animate-pulse bg-[#1a1a1a]"></div>
			<div class="mt-2 h-3 w-5/6 animate-pulse bg-[#1a1a1a]"></div>
		</div>
	{:else if hypothesis}
		<section class="mt-4 border border-[#222] bg-[#050505] p-6">
			<div class="flex flex-wrap items-center gap-2">
				<span class={`inline-flex items-center border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] ${crucibleStatusClasses(hypothesis.crucible_status)}`}>
					{crucibleStatusLabel(hypothesis.crucible_status)}
				</span>
				{#if detailProtBadge}
					<span class={`inline-flex items-center border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${detailProtBadge.classes}`}>
						{detailProtBadge.label}
					</span>
				{/if}
				<span class={`inline-flex items-center border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] ${originClasses(hypothesis.origin)}`}>
					{originLabel(hypothesis.origin)}
				</span>
				{#if hypothesis.display_id}
					<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-wider text-[#888]">{hypothesis.display_id}</span>
				{/if}
				<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#666]">{hypothesis.source_type}</span>
				<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#888]">{managerStateLabel(hypothesis.manager_state)}</span>
				{#if hypothesis.source_tags?.length}
					<SourceTags tags={hypothesis.source_tags} size="md" />
				{/if}
			</div>

			{#if editing}
				<label class="mt-4 block text-[10px] uppercase tracking-[0.18em] text-[#666]">
					Title
					<input
						bind:value={editTitle}
						class="mt-2 w-full border border-[#333] bg-black px-3 py-2 text-xl font-semibold text-white outline-none focus:border-white"
					/>
				</label>
				<label class="mt-3 block text-[10px] uppercase tracking-[0.18em] text-[#666]">
					Market thesis
					<textarea
						bind:value={editThesis}
						rows="3"
						class="mt-2 w-full border border-[#333] bg-black px-3 py-2 text-sm leading-6 text-white outline-none focus:border-white"
					></textarea>
				</label>
				<label class="mt-3 block text-[10px] uppercase tracking-[0.18em] text-[#666]">
					Why now
					<textarea
						bind:value={editWhyNow}
						rows="2"
						placeholder="What makes this timely right now?"
						class="mt-2 w-full border border-[#333] bg-black px-3 py-2 text-sm leading-6 text-white outline-none placeholder:text-[#555] focus:border-white"
					></textarea>
				</label>
				<div class="mt-3 grid gap-3 md:grid-cols-2">
					<label class="block text-[10px] uppercase tracking-[0.18em] text-[#666]">
						Target assets (comma separated)
						<input
							bind:value={editAssets}
							placeholder="BTC, ETH, SOL"
							class="mt-2 w-full border border-[#333] bg-black px-3 py-2 text-sm text-white outline-none placeholder:text-[#555] focus:border-white"
						/>
					</label>
					<label class="block text-[10px] uppercase tracking-[0.18em] text-[#666]">
						Target timeframes (comma separated)
						<input
							bind:value={editTimeframes}
							placeholder="1h, 4h, 1d"
							class="mt-2 w-full border border-[#333] bg-black px-3 py-2 text-sm text-white outline-none placeholder:text-[#555] focus:border-white"
						/>
					</label>
				</div>
				<div class="mt-3 flex gap-2">
					<button
						type="button"
						on:click={saveEdit}
						disabled={mutationPending}
						class="border border-green-600/60 bg-green-950/40 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-green-100 transition hover:bg-green-900/60 disabled:opacity-40"
					>
						Save
					</button>
					<button
						type="button"
						on:click={cancelEdit}
						disabled={mutationPending}
						class="border border-[#333] px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#888] transition-colors hover:border-white hover:text-white"
					>
						Cancel
					</button>
				</div>
			{:else}
				<h1 class="mt-4 text-2xl font-bold tracking-tight text-white">{hypothesis.title}</h1>
				<p class="mt-4 max-w-4xl text-sm leading-7 text-[#888]">{hypothesis.market_thesis}</p>
			{/if}

			{#if researchTask}
				<div class="mt-4 flex flex-wrap items-center gap-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400">
					<span class="inline-flex h-2 w-2 animate-pulse rounded-full bg-yellow-400"></span>
					<span class="font-semibold uppercase tracking-wider">
						{researchTask.status === 'running' ? 'Researching now' : 'Research queued'}
					</span>
					<span class="text-yellow-400/70">
						{researchTask.display_id || `Task ${researchTask.task_id}`} · {researchTask.type}
					</span>
					{#if researchTask.origin_mode === 'operator_url_paste'}
						<span class="text-yellow-400/60">(from your URL paste — agent will enrich fields and spawn strategies)</span>
					{/if}
				</div>
			{/if}

			<div class="mt-5 grid gap-4 lg:grid-cols-3">
				<div class="border border-[#222] bg-black p-4">
					<div class="text-[10px] uppercase tracking-[0.18em] text-[#666]">Mechanism</div>
					{#if editing}
						<textarea
							bind:value={editMechanism}
							rows="3"
							class="mt-2 w-full border border-[#333] bg-[#050505] px-2 py-1.5 text-sm leading-6 text-white outline-none focus:border-white"
						></textarea>
					{:else}
						<p class="mt-2 text-sm leading-6 text-[#888]">{hypothesis.mechanism}</p>
					{/if}
				</div>
				<div class="border border-[#222] bg-black p-4">
					<div class="text-[10px] uppercase tracking-[0.18em] text-[#666]">Why Now</div>
					<p class="mt-2 text-sm leading-6 text-[#888]">{hypothesis.why_now || 'No timing note attached yet.'}</p>
				</div>
				<div class="border border-[#222] bg-black p-4">
					<div class="text-[10px] uppercase tracking-[0.18em] text-[#666]">Origin</div>
					<p class="mt-2 text-sm leading-6 text-[#888]">{hypothesis.origin_agent_id || 'unknown agent'}</p>
					<p class="mt-1 text-xs text-[#666]">{hypothesis.origin_model || 'unknown model'}</p>
				</div>
			</div>

			<div class="mt-4">
				<VerdictMemoCard
					status={hypothesis.status}
					memo={hypothesis.verdict_memo}
					memoAt={hypothesis.verdict_memo_at}
					memoBy={hypothesis.verdict_memo_by}
					signals={verdictSignals}
					canReopen={hypothesis.status === 'disproven' && !mutationPending}
					onReopen={doReopen}
				/>
			</div>

			<div class="mt-4 border border-[#222] bg-black p-4">
				<div class="text-[10px] uppercase tracking-[0.18em] text-[#666]">Operator notes</div>
				{#if editing}
					<textarea
						bind:value={editNotes}
						rows="3"
						placeholder="Private notes — not shown to the agent, for your reference."
						class="mt-2 w-full border border-[#333] bg-[#050505] px-2 py-1.5 text-sm leading-6 text-white outline-none placeholder:text-[#555] focus:border-white"
					></textarea>
				{:else if hypothesis.operator_notes}
					<p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-[#888]">{hypothesis.operator_notes}</p>
				{:else}
					<p class="mt-2 text-sm leading-6 text-[#666] italic">No notes yet. Click Edit to add one.</p>
				{/if}
			</div>

			{#if !editing}
				<div class="mt-5 flex flex-wrap gap-2">
					{#each hypothesis.target_assets as asset}
						<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#888]">{asset}</span>
					{/each}
					{#each hypothesis.target_timeframes as timeframe}
						<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#888]">{timeframe}</span>
					{/each}
				</div>
			{/if}
		</section>

		<div class="mt-5 grid gap-5 xl:grid-cols-[minmax(0,1.5fr)_minmax(320px,1fr)]">
			<div class="space-y-5">
				<div class="border border-[#222] bg-black">
					<div class="border-b border-[#222] bg-[#050505] px-5 py-4">
						<h2 class="text-sm font-semibold uppercase tracking-[0.22em] text-[#888]">Forge — Proof Attempts</h2>
						<p class="mt-1 text-xs text-[#666]">Each candidate is sent to the Forge to prove or disprove this crucible; results roll back up here as the verdict.</p>
						<div class="mt-2 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.2em] text-[#555]">
							<span class="text-[#666]">Idea</span><span>→</span><span class="text-[#666]">Forge</span><span>→</span><span class="text-[#666]">Verdict</span>
						</div>
					</div>
					<div class="divide-y divide-[#1a1a1a]">
						{#if linkedStrategies.length === 0}
							<div class="px-5 py-10 text-sm text-[#666]">No linked strategies yet.</div>
						{:else}
							{#each linkedStrategies as strategy}
								<a href={strategyHref(strategy, hypothesisRouteId)} class="block px-5 py-4 transition hover:bg-[#111]">
									<div class="flex flex-wrap items-center gap-2">
										<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#888]">{forgeStageLabel(strategy.stage)}</span>
										{#if strategy.gauntlet_status && forgeStatusLabel(strategy.gauntlet_status)}
											<span class={`border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] ${forgeStatusClasses(strategy.gauntlet_status)}`} data-forge-status={strategy.gauntlet_status}>
												{forgeStatusLabel(strategy.gauntlet_status)}
											</span>
										{/if}
										{#if strategy.canonical}
											<span
												data-canonical-badge
												class="border border-emerald-900 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-emerald-400"
												title="Canonical: cleanup-protected per (asset, timeframe) cell"
											>
												Canonical
											</span>
										{/if}
										{#if strategy.parent_strategy_id}
											<span
												class="border border-[#333] px-2.5 py-1 text-[10px] uppercase tracking-wider text-[#888]"
												title={`Parent: ${strategy.parent_strategy_id}`}
											>
												⮑ iter
											</span>
										{/if}
										{#if strategy.symbol}
											<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#666]">{strategy.symbol}</span>
										{/if}
										{#if strategy.timeframe}
											<span class="border border-[#333] bg-black px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-[#666]">{strategy.timeframe}</span>
										{/if}
									</div>
									<div class="mt-3 flex items-center justify-between gap-4">
										<div class="min-w-0 flex-1">
											<div class="text-base font-semibold text-white">{strategy.name}</div>
											<div class="mt-1 text-sm text-[#666]">{strategy.owner || 'Unassigned owner'}</div>
										</div>
										{#if strategy.latest_result}
											<div class="flex flex-shrink-0 gap-3 text-right text-[11px] uppercase tracking-[0.14em]">
												<div>
													<div class="text-[#666]">Sharpe</div>
													<div class="mt-1 text-sm font-semibold text-white">{formatSharpe(strategy.latest_result.sharpe)}</div>
												</div>
												<div>
													<div class="text-[#666]">Return</div>
													<div class="mt-1 text-sm font-semibold text-white">{formatPct(strategy.latest_result.total_return_pct)}</div>
												</div>
												<div>
													<div class="text-[#666]">Trades</div>
													<div class="mt-1 text-sm font-semibold text-white">{strategy.latest_result.total_trades ?? '—'}</div>
												</div>
											</div>
										{:else}
											<div class="text-xs uppercase tracking-[0.18em] text-[#555]">No backtest yet</div>
										{/if}
									</div>
								</a>
							{/each}
						{/if}
					</div>
				</div>

				<HypothesisArtifacts {artifacts} includeContent={includeContent} onToggleContent={toggleCachedContent} />
			</div>

			<div class="space-y-5">
				<AgentActivityPanel activity={agentActivity} returnTo={currentReturnTo} />
				<DataGapLeaderboard items={dataGaps} title="Crucible Data Gaps" subtitle="These are the missing inputs blocking better validation or execution." />
			</div>
		</div>
	{/if}
</div>
