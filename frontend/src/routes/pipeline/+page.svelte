<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		getJobs,
		getDashboardOverview,
		getForvenSchedulerJobs,
		type Job,
		type DashboardOverview,
		type ForvenSchedulerJob,
		type Scan,
	} from '$lib/api';
	import { activeProcesses, type TrackedProcess } from '$lib/stores/processTracker';
	import { createRealtimeRefresh } from '$lib/utils/realtime';
	import { formatIntervalMs } from '$lib/utils/schedule';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { fetchApi } from '$lib/api/core';
	import PipelineExplainBoard from '$lib/components/lifecycle/PipelineExplainBoard.svelte';

	type PipelineTab = 'strategies' | 'pipeline' | 'code-review';

	let recentJobs: Job[] = [];
	let runningJobs: Job[] = [];
	let schedulerJobs: ForvenSchedulerJob[] = [];
	let overview: DashboardOverview | null = null;
	let loading = true;
	let error: string | null = null;

	let activeTab: PipelineTab = ($page.url.searchParams.get('tab') as PipelineTab) || 'strategies';
	let codeReviewLog: Array<{ message: string; created_at: string; detail: Record<string, unknown> }> = [];
	let codeReviewLoading = false;
	let codeReviewError: string | null = null;

	function selectTab(tab: PipelineTab) {
		activeTab = tab;
		const url = new URL($page.url);
		url.searchParams.set('tab', tab);
		goto(url.pathname + url.search, { replaceState: true, keepFocus: true, noScroll: true });
		if (tab === 'code-review') loadCodeReviewLog();
	}

	async function loadCodeReviewLog() {
		codeReviewLoading = true;
		try {
			codeReviewLog = await fetchApi('/pipeline/code-review-log?days=30&limit=100');
			codeReviewError = null;
		} catch (e) {
			codeReviewLog = [];
			codeReviewError = e instanceof Error ? e.message : 'Failed to load code review log';
		} finally {
			codeReviewLoading = false;
		}
	}

	async function refresh() {
		try {
			const [succeeded, failed, running, queued, scheduler, dash] = await Promise.allSettled([
				getJobs('succeeded', 10),
				getJobs('failed', 10),
				getJobs('running', 20),
				getJobs('queued', 20),
				getForvenSchedulerJobs(),
				getDashboardOverview(),
			]);

			if (succeeded.status === 'fulfilled' && failed.status === 'fulfilled') {
				const merged = [...succeeded.value, ...failed.value];
				merged.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
				recentJobs = merged.slice(0, 20);
			}

			// Merge running + queued backend jobs (exclude any already tracked by processTracker)
			{
				const trackedIds = new Set($activeProcesses.map(p => p.id));
				const backendActive: Job[] = [];
				if (running.status === 'fulfilled') backendActive.push(...running.value);
				if (queued.status === 'fulfilled') backendActive.push(...queued.value);
				runningJobs = backendActive.filter(j => !trackedIds.has(j.id));
			}
			if (scheduler.status === 'fulfilled') {
				schedulerJobs = scheduler.value
					.sort((a, b) => {
						if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
						if (!a.next_run_at) return 1;
						if (!b.next_run_at) return -1;
						return new Date(a.next_run_at).getTime() - new Date(b.next_run_at).getTime();
					});
			}
			if (dash.status === 'fulfilled') {
				overview = dash.value;
			}
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load pipeline data';
		} finally {
			loading = false;
		}
	}

	const realtimeController = createRealtimeRefresh(refresh, {
		fallbackMs: 15_000,
		pollWhenWsOfflineOnly: true,
	});

	onMount(() => {
		refresh();
		realtimeController.start();
		if (activeTab === 'code-review') loadCodeReviewLog();
	});

	onDestroy(() => {
		realtimeController.stop();
	});

	function timeAgo(dateStr: string | null | undefined): string {
		if (!dateStr) return '-';
		const ms = Date.now() - new Date(dateStr).getTime();
		if (ms < 0) return 'now';
		const seconds = Math.floor(ms / 1000);
		if (seconds < 60) return `${seconds}s ago`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m ago`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `${hours}h ago`;
		const days = Math.floor(hours / 24);
		return `${days}d ago`;
	}

	function elapsed(addedAt: number): string {
		const ms = Date.now() - addedAt;
		const seconds = Math.floor(ms / 1000);
		if (seconds < 60) return `${seconds}s`;
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m`;
		const hours = Math.floor(minutes / 60);
		return `${hours}h ${minutes % 60}m`;
	}

	function progressPercent(proc: TrackedProcess): number | null {
		if (proc.type === 'job') {
			const job = proc.data as Job;
			if (!job.progress) return null;
			const match = String(job.progress).match(/(\d+)/);
			return match ? Math.min(100, parseInt(match[1])) : null;
		}
		if (proc.type === 'scan') {
			const scan = proc.data as Scan;
			if (scan.progress_json?.pct_complete != null) return Math.round(scan.progress_json.pct_complete);
			if (scan.total_combinations > 0 && scan.completed_count >= 0) {
				return Math.round((scan.completed_count / scan.total_combinations) * 100);
			}
			return null;
		}
		return null;
	}

	function typeBadgeClass(type: string): string {
		switch (type) {
			case 'job': return 'bg-sc-panel2 text-sc-ink border-sc-line2';
			case 'scan': return 'bg-sc-panel2 text-sc-ink2 border-sc-line2';
			case 'tournament': return 'bg-sc-panel2 text-sc-ink2 border-sc-line2';
			default: return 'bg-sc-panel2 text-sc-ink2 border-sc-line2';
		}
	}

	function statusColor(status: string): string {
		switch (status) {
			case 'running': case 'processing': return 'text-emerald-400';
			case 'queued': case 'pending': return 'text-yellow-400';
			case 'succeeded': case 'completed': return 'text-emerald-400';
			case 'failed': return 'text-red-400';
			case 'cancelled': return 'text-sc-ink3';
			default: return 'text-sc-ink2';
		}
	}

	function asScan(data: unknown): Scan | null {
		if (data && typeof data === 'object' && 'progress_json' in data) return data as Scan;
		return null;
	}

	function jobHref(job: Job): string {
		return job.strategy_id ? `/lab/strategy/${encodeURIComponent(job.strategy_id)}` : '/lab';
	}

	$: totalActive = $activeProcesses.length + runningJobs.length;

	function schedulerStatusColor(status: string | null | undefined): string {
		if (!status) return 'text-sc-ink3';
		const s = status.toLowerCase();
		if (s === 'ok' || s === 'success' || s === 'succeeded') return 'text-emerald-400';
		if (s === 'failed' || s === 'error') return 'text-red-400';
		if (s === 'running') return 'text-yellow-400';
		return 'text-sc-ink2';
	}
</script>

<div class="p-6 space-y-6 font-mono text-sm">
	<!-- Header -->
	<div class="flex justify-between items-center">
		<div class="flex items-center gap-4">
			<div>
				<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Pipeline</h1>
				<p class="text-xs text-sc-ink3 mt-1">Strategy funnel, background processes, scheduler jobs, and autopilot status.</p>
			</div>
			<div class="rounded-md flex bg-sc-panel2 border border-sc-line p-0.5 ml-4">
				<button class="px-3 py-1 text-[12px] {activeTab === 'strategies' ? 'bg-sc-line2 text-sc-ink' : 'text-sc-ink2 hover:text-sc-ink'}" on:click={() => selectTab('strategies')} data-testid="pipeline-tab-strategies">Strategies</button>
				<button class="px-3 py-1 text-[12px] {activeTab === 'pipeline' ? 'bg-sc-line2 text-sc-ink' : 'text-sc-ink2 hover:text-sc-ink'}" on:click={() => selectTab('pipeline')}>Processes</button>
				<button class="px-3 py-1 text-[12px] {activeTab === 'code-review' ? 'bg-sc-line2 text-sc-ink' : 'text-sc-ink2 hover:text-sc-ink'}" on:click={() => selectTab('code-review')}>Code Review</button>
			</div>
		</div>
		{#if loading}
			<span class="text-xs text-sc-ink3 animate-pulse">Loading...</span>
		{/if}
	</div>

	{#if activeTab === 'code-review'}
		<!-- Code Review Log -->
		<div class="space-y-4">
			<div class="flex justify-between items-center">
				<p class="text-xs text-sc-ink2">Agent code suggestions logged for manual review. Implement from your IDE.</p>
				<button type="button" class="rounded-md text-[12px] border border-sc-line2 px-3 py-1.5 text-sc-ink2 hover:text-sc-ink hover:border-sc-line2 transition-colors" on:click={loadCodeReviewLog}>Refresh</button>
			</div>

			{#if codeReviewError}
				<div class="border border-red-900 bg-red-500/5 px-4 py-3 text-xs text-red-400">{codeReviewError}</div>
			{:else if codeReviewLoading}
				<div class="text-sc-ink3 text-xs animate-pulse">Loading code review log...</div>
			{:else if codeReviewLog.length === 0}
				<div class="rounded-md border border-sc-line bg-sc-panel p-8 text-center">
					<div class="text-sc-ink3 text-sm">No code suggestions yet.</div>
					<div class="text-sc-ink3 text-xs mt-1">Agents will log suggestions here when they identify code improvements.</div>
				</div>
			{:else}
				<div class="space-y-3">
					{#each codeReviewLog as entry}
						<article class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-2">
							<div class="flex justify-between items-start gap-3">
								<div class="flex-1 min-w-0">
									<div class="text-sm font-semibold text-sc-ink">{entry.message}</div>
									{#if entry.detail?.agent_id}
										<div class="text-[10px] text-sc-ink3 mt-1">Agent: {entry.detail.agent_id}{entry.detail.strategy_id ? ` | Strategy: ${entry.detail.strategy_id}` : ''}</div>
									{/if}
								</div>
								<div class="text-[10px] text-sc-ink3 whitespace-nowrap">{new Date(entry.created_at).toLocaleString()}</div>
							</div>
							{#if entry.detail?.description}
								<pre class="rounded-md text-[11px] text-sc-ink2 bg-sc-bg border border-sc-line p-3 max-h-48 overflow-auto whitespace-pre-wrap">{entry.detail.description}</pre>
							{/if}
						</article>
					{/each}
				</div>
			{/if}
		</div>
	{:else if activeTab === 'strategies'}
		<!-- Strategy funnel: why each strategy is where it is, what unblocks it -->
		<PipelineExplainBoard />
	{:else}

	{#if error}
		<div class="border border-red-900 bg-red-500/5 px-4 py-3 text-xs text-red-400">{error}</div>
	{/if}

	<!-- Active Processes — full width -->
	<section class="rounded-md border border-sc-line bg-sc-panel overflow-hidden">
		<div class="px-4 py-3 border-b border-sc-line flex justify-between items-center">
			<h2 class="text-[14px] font-semibold text-sc-ink3">Active Processes</h2>
			<span class="text-[11px] text-sc-ink3">{totalActive} running</span>
		</div>
		{#if totalActive === 0}
			<div class="px-4 py-8 text-center text-sc-ink3 text-xs">
				No active processes. Gauntlet runs, scans, and tournaments will appear here when running.
			</div>
		{:else}
			<div class="divide-y divide-sc-line">
				<!-- Frontend-tracked processes (backtests, scans, tournaments started from UI) -->
				{#each $activeProcesses as proc (proc.id)}
					{@const pct = progressPercent(proc)}
					<a href={proc.href} class="block px-4 py-3 hover:bg-sc-panel2 transition-colors">
						<div class="flex items-center gap-3">
							<span class="px-1.5 py-0.5 border font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {typeBadgeClass(proc.type)}">
								{proc.type}
							</span>
							<span class="text-sc-ink truncate flex-1">{proc.label}</span>
							<span class="text-[11px] {statusColor(proc.status)} font-bold uppercase animate-pulse">
								{proc.status}
							</span>
							<span class="text-[11px] text-sc-ink3 tabular-nums w-16 text-right">{elapsed(proc.addedAt)}</span>
						</div>
						{#if pct !== null}
							<div class="mt-2 h-1 bg-sc-raise overflow-hidden">
								<div
									class="h-full bg-emerald-500 transition-all duration-500"
									style="width: {pct}%"
								></div>
							</div>
							<div class="mt-1 text-[10px] text-sc-ink3 text-right">{pct}%</div>
						{/if}
						{#if proc.type === 'scan'}
							{@const scanData = asScan(proc.data)}
							{#if scanData?.progress_json?.best_sharpe}
								<div class="mt-1 text-[10px] text-sc-ink3">
									Best Sharpe: <span class="text-sc-ink">{scanData.progress_json.best_sharpe.toFixed(2)}</span>
									{#if scanData.progress_json?.completed_count != null}
										&middot; {scanData.progress_json.completed_count}/{scanData.total_combinations} combos
									{/if}
								</div>
							{/if}
						{/if}
					</a>
				{/each}
				<!-- Backend running/queued jobs not already in processTracker -->
				{#each runningJobs as job (job.id)}
					<a href={jobHref(job)} class="block px-4 py-3 hover:bg-sc-panel2 transition-colors">
						<div class="flex items-center gap-3">
							<span class="px-1.5 py-0.5 border font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {typeBadgeClass('job')}">
								{job.type || 'job'}
							</span>
							<span class="text-sc-ink truncate flex-1">
								{job.strategy_id || job.symbol || job.id}
							</span>
							<span class="text-[11px] {statusColor(job.status)} font-bold uppercase {job.status === 'running' ? 'animate-pulse' : ''}">
								{job.status}
							</span>
							<span class="text-[11px] text-sc-ink3 tabular-nums w-16 text-right">{timeAgo(job.created_at)}</span>
						</div>
						{#if job.progress}
							<div class="mt-1 text-[10px] text-sc-ink3 pl-5">{job.progress}</div>
						{/if}
					</a>
				{/each}
			</div>
		{/if}
	</section>

	<!-- Grid: Autopilot + Recent + Scheduler -->
	<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
		<!-- Autopilot Status -->
		<section class="rounded-md border border-sc-line bg-sc-panel overflow-hidden">
			<div class="px-4 py-3 border-b border-sc-line">
				<h2 class="text-[14px] font-semibold text-sc-ink3">Autopilot</h2>
			</div>
			{#if overview?.autopilot}
				{@const ap = overview.autopilot}
				<div class="p-4">
					<div class="flex items-center gap-2 mb-4">
						<span class="w-2 h-2 rounded-full {ap.running ? 'bg-emerald-400' : 'bg-sc-line2'}"></span>
						<span class="text-xs font-bold uppercase {ap.running ? 'text-emerald-400' : 'text-sc-ink3'}">
							{ap.running ? (ap.paused ? 'Paused' : 'Running') : 'Stopped'}
						</span>
						{#if ap.disabled_reason}
							<span class="text-[10px] text-red-400 ml-2">({ap.disabled_reason})</span>
						{/if}
					</div>
					<div class="grid grid-cols-2 gap-3">
						<div class="border border-sc-line p-3">
							<div class="text-[10px] uppercase text-sc-ink3 mb-1">Workers</div>
							<div class="text-xl font-bold text-sc-ink">{ap.active_workers}<span class="text-sc-ink3 text-sm">/{ap.worker_concurrency}</span></div>
							{#if ap.worker_concurrency > 0}
								<div class="mt-2 h-1 bg-sc-raise overflow-hidden">
									<div class="h-full bg-emerald-500" style="width: {(ap.active_workers / ap.worker_concurrency) * 100}%"></div>
								</div>
							{/if}
						</div>
						<div class="border border-sc-line p-3">
							<div class="text-[10px] uppercase text-sc-ink3 mb-1">Queued Jobs</div>
							<div class="text-xl font-bold {ap.queued_jobs > 0 ? 'text-yellow-400' : 'text-sc-ink'}">{ap.queued_jobs}</div>
						</div>
						<div class="border border-sc-line p-3">
							<div class="text-[10px] uppercase text-sc-ink3 mb-1">Dead Letters</div>
							<div class="text-xl font-bold {ap.dead_letter_jobs > 0 ? 'text-red-400' : 'text-sc-ink'}">{ap.dead_letter_jobs}</div>
						</div>
						<div class="border border-sc-line p-3">
							<div class="text-[10px] uppercase text-sc-ink3 mb-1">Health</div>
							<div class="text-xl font-bold {ap.health_ok ? 'text-emerald-400' : ap.health_ok === false ? 'text-red-400' : 'text-sc-ink3'}">
								{ap.health_ok ? 'OK' : ap.health_ok === false ? 'FAIL' : '--'}
							</div>
						</div>
					</div>
					{#if ap.last_tick_error}
						<div class="mt-3 text-[10px] text-red-400 border border-red-900 bg-red-500/5 px-2 py-1.5 truncate" title={ap.last_tick_error}>
							{ap.last_tick_error}
						</div>
					{/if}
				</div>
			{:else}
				<div class="px-4 py-8 text-center text-sc-ink3 text-xs">
					{loading ? 'Loading...' : 'Autopilot data unavailable.'}
				</div>
			{/if}
		</section>

		<!-- Recent Completions -->
		<section class="rounded-md border border-sc-line bg-sc-panel overflow-hidden">
			<div class="px-4 py-3 border-b border-sc-line">
				<h2 class="text-[14px] font-semibold text-sc-ink3">Recent Completions</h2>
			</div>
			{#if recentJobs.length === 0}
				<div class="px-4 py-8 text-center text-sc-ink3 text-xs">
					{loading ? 'Loading...' : 'No recent job completions.'}
				</div>
			{:else}
				<div class="divide-y divide-sc-line max-h-[400px] overflow-y-auto">
					{#each recentJobs as job (job.id)}
						<div class="px-4 py-2.5 hover:bg-sc-panel2 transition-colors">
							<div class="flex items-center gap-2">
								{#if job.status === 'succeeded'}
									<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 flex-shrink-0"></span>
								{:else}
									<span class="w-1.5 h-1.5 rounded-full bg-red-400 flex-shrink-0"></span>
								{/if}
								<span class="text-[10px] font-bold uppercase text-sc-ink3 w-16">{job.type || 'job'}</span>
								<span class="text-xs text-sc-ink2 truncate flex-1">
									{job.strategy_id || job.symbol || job.id}
								</span>
								<span class="text-[10px] text-sc-ink3 tabular-nums flex-shrink-0">{timeAgo(job.updated_at)}</span>
							</div>
							{#if job.status === 'failed' && job.error}
								<div class="mt-1 text-[10px] text-red-400/70 truncate pl-5" title={job.error}>{job.error}</div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<!-- Scheduler — spans full width -->
		<section class="rounded-md border border-sc-line bg-sc-panel overflow-hidden lg:col-span-2">
			<div class="px-4 py-3 border-b border-sc-line flex justify-between items-center">
				<h2 class="text-[14px] font-semibold text-sc-ink3">Scheduler</h2>
				<span class="text-[11px] text-sc-ink3">{schedulerJobs.length} jobs</span>
			</div>
			<div class="px-4 py-2 border-b border-sc-line text-[10px] text-sc-ink3">
				Engine cron jobs (backtests, maintenance). For LLM agent routines see <a href="/routines" class="text-sc-ink2 hover:text-sc-ink underline">Routines</a>.
			</div>
			{#if schedulerJobs.length === 0}
				<div class="px-4 py-8 text-center text-sc-ink3 text-xs">
					{loading ? 'Loading...' : 'No scheduler jobs found.'}
				</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-left border-collapse">
						<thead>
							<tr class="text-[10px] text-sc-ink3 uppercase border-b border-sc-line">
								<th class="px-4 py-2 font-medium"></th>
								<th class="px-4 py-2 font-medium">Name</th>
								<th class="px-4 py-2 font-medium">Schedule</th>
								<th class="px-4 py-2 font-medium text-right">Next Run</th>
								<th class="px-4 py-2 font-medium text-right">Last Run</th>
								<th class="px-4 py-2 font-medium text-right">Last Status</th>
							</tr>
						</thead>
						<tbody class="text-xs">
							{#each schedulerJobs as job}
								<tr class="border-b border-sc-line hover:bg-sc-panel2 transition-colors {!job.enabled ? 'opacity-40' : ''}">
									<td class="px-4 py-2">
										<span class="w-2 h-2 rounded-full inline-block {job.enabled ? 'bg-emerald-400' : 'bg-sc-line2'}"></span>
									</td>
									<td class="px-4 py-2 text-sc-ink2 whitespace-nowrap">{job.name || '-'}</td>
									<td class="px-4 py-2 text-sc-ink3 font-mono text-[11px]">{job.schedule_type === 'interval' ? formatIntervalMs(job.schedule_expr) : job.schedule_expr || '-'}</td>
									<td class="px-4 py-2 text-right text-sc-ink2 tabular-nums">{timeAgo(job.next_run_at)}</td>
									<td class="px-4 py-2 text-right text-sc-ink2 tabular-nums">{timeAgo(job.last_run_at)}</td>
									<td class="px-4 py-2 text-right font-bold uppercase {schedulerStatusColor(job.last_status)}">{job.last_status || '-'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</section>
	</div>
	{/if}
</div>
