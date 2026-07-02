<script lang="ts">
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { onDestroy } from 'svelte';
	import {
		getTaskAudit,
		type TaskContainer,
		type TaskAuditEvent,
		type TaskTranscriptMessage,
	} from '$lib/api';
	import type { PageData } from './$types';

	export let data: PageData;

	let task: TaskContainer | null = data.task;
	let auditLog: TaskAuditEvent[] = data.auditLog;
	let toolCalls: Array<Record<string, unknown>> = data.toolCalls;
	let transcript: TaskTranscriptMessage[] = data.transcript;
	let loading = false;
	let error: string | null = data.loadError;

	const TABS = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'transcript', label: 'Transcript' },
		{ id: 'audit', label: 'Audit Log' },
		{ id: 'tools', label: 'Tool Calls' },
		{ id: 'data', label: 'Task Data' },
		{ id: 'raw', label: 'Raw JSON' },
	] as const;
	let activeTab: string = 'overview';

	$: taskId = $page.params.id ?? '';
	$: returnTo = $page.url.searchParams.get('returnTo') || '/agents?tab=tasks';

	// Re-sync local state when the load function reruns (e.g. navigating between
	// task ids, where SvelteKit reuses this component instance).
	let lastData: PageData | null = null;
	$: if (data !== lastData) {
		lastData = data;
		task = data.task;
		auditLog = data.auditLog;
		toolCalls = data.toolCalls;
		transcript = data.transcript;
		error = data.loadError;
		loading = false;
	}

	function taskStatus(t: TaskContainer): string {
		return String(t?.status || 'pending').toLowerCase();
	}

	function statusClass(status: string): string {
		switch (status) {
			case 'running':
				return 'text-emerald-400 border-emerald-900 bg-emerald-500/10';
			case 'done':
			case 'reviewed':
				return 'text-[#888] border-[#333]';
			case 'failed':
				return 'text-red-400 border-red-900 bg-red-500/10';
			case 'blocked':
				return 'text-yellow-400 border-yellow-900 bg-yellow-500/10';
			case 'rejected':
				return 'text-red-400 border-red-900 bg-red-500/10';
			default:
				return 'text-[#888] border-[#333]';
		}
	}

	function fmtDate(value: unknown): string {
		if (!value) return '--';
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return '--';
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
	}

	function fmtMs(value: unknown): string {
		const n = Number(value);
		if (!Number.isFinite(n)) return '--';
		if (n >= 1000) return `${(n / 1000).toFixed(1)}s`;
		return `${n.toFixed(0)}ms`;
	}

	function formatValue(value: unknown): string {
		if (value === null || value === undefined) return '--';
		if (typeof value === 'string') {
			const trimmed = value.trim();
			if (!trimmed) return '--';
			if (
				(trimmed.startsWith('{') && trimmed.endsWith('}')) ||
				(trimmed.startsWith('[') && trimmed.endsWith(']'))
			) {
				try {
					return JSON.stringify(JSON.parse(trimmed), null, 2);
				} catch {
					return trimmed;
				}
			}
			return trimmed;
		}
		try {
			return JSON.stringify(value, null, 2) || '--';
		} catch {
			return String(value);
		}
	}

	function strategyLabel(t: TaskContainer): string {
		const displayId = String(t.strategy_display_id || '').trim();
		if (displayId) return displayId;
		const strategyId = String(t.strategy_id || '').trim();
		return strategyId || '--';
	}

	// Bumped on a timer for running tasks so the live elapsed clock ticks.
	let nowTick = Date.now();

	function elapsed(t: TaskContainer, now: number = Date.now()): string {
		const start = t.started_at ? new Date(String(t.started_at)).getTime() : 0;
		const end = t.completed_at
			? new Date(String(t.completed_at)).getTime()
			: t.started_at
				? now
				: 0;
		if (!start || !end) return '--';
		const ms = end - start;
		if (ms < 1000) return `${ms}ms`;
		if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
		if (ms < 3600000) return `${(ms / 60000).toFixed(1)}m`;
		return `${(ms / 3600000).toFixed(1)}h`;
	}

	async function loadTask(silent = false) {
		// Silent refreshes (auto-poll) update data in place without flashing the
		// full-page loading/error state, so live tasks refresh unobtrusively.
		if (!silent) {
			loading = true;
			error = null;
		}
		try {
			const details = await getTaskAudit(taskId);
			task = details.task;
			auditLog = Array.isArray(details.audit_log) ? details.audit_log : [];
			toolCalls = Array.isArray(details.tool_calls) ? details.tool_calls : [];
			transcript = Array.isArray(details.transcript) ? details.transcript : [];
		} catch (e) {
			if (!silent) {
				error = e instanceof Error ? e.message : 'Failed to load task details';
			}
		} finally {
			if (!silent) loading = false;
		}
	}

	// Auto-refresh + live elapsed clock while a task is running.
	const REFRESH_INTERVAL_MS = 7000;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;
	let tickTimer: ReturnType<typeof setInterval> | null = null;

	function stopLiveTimers() {
		if (refreshTimer) {
			clearInterval(refreshTimer);
			refreshTimer = null;
		}
		if (tickTimer) {
			clearInterval(tickTimer);
			tickTimer = null;
		}
	}

	function syncLiveTimers(isRunning: boolean) {
		if (isRunning) {
			if (!refreshTimer) {
				refreshTimer = setInterval(() => {
					if (!loading) loadTask(true);
				}, REFRESH_INTERVAL_MS);
			}
			if (!tickTimer) {
				tickTimer = setInterval(() => {
					nowTick = Date.now();
				}, 1000);
			}
		} else {
			stopLiveTimers();
		}
	}

	$: syncLiveTimers(!!task && taskStatus(task) === 'running');

	function goBack() {
		goto(returnTo);
	}

	function fmtUsd(value: unknown): string {
		const n = Number(value);
		if (!Number.isFinite(n) || n <= 0) return '--';
		if (n < 0.01) return `$${n.toFixed(6)}`;
		if (n < 1) return `$${n.toFixed(4)}`;
		return `$${n.toFixed(2)}`;
	}

	function fmtTokens(value: unknown): string {
		const n = Number(value);
		if (!Number.isFinite(n) || n <= 0) return '--';
		return n.toLocaleString();
	}

	function modelDisplay(t: TaskContainer): string {
		const provider = String(t?.model || t?.provider || '').trim();
		const modelId = String(t?.model_id || '').trim();
		if (provider && modelId) return `${provider} \u00b7 ${modelId}`;
		return provider || modelId || '--';
	}

	function truncationCount(t: TaskContainer): number {
		const v = t?.truncation_count ?? t?.tool_truncation_count;
		const n = Number(v);
		return Number.isFinite(n) && n > 0 ? n : 0;
	}

	function goToStrategy(t: TaskContainer) {
		const sid = String(t.strategy_display_id || t.strategy_id || '').trim();
		if (sid) goto(`/lab/strategy/${encodeURIComponent(sid)}`);
	}

	const AUDIT_KNOWN_KEYS = ['event', 'action', 'from', 'to', 'reason', 'timestamp', 'created_at'];

	function auditExtras(event: Record<string, unknown>): [string, unknown][] {
		return Object.entries(event).filter(
			([k, v]) =>
				!AUDIT_KNOWN_KEYS.includes(k) &&
				v !== null &&
				v !== undefined &&
				String(v).trim() !== '',
		);
	}

	// Known data fields to display in the overview (excludes internal/structural keys)
	const CORE_FIELDS = [
		'id',
		'display_id',
		'title',
		'status',
		'agent_id',
		'strategy_id',
		'strategy_display_id',
		'strategy_stage',
		'strategy_name',
		'priority',
		'created_at',
		'started_at',
		'completed_at',
		'retry_at',
		'audit_log',
	];

	$: extraFields = task
		? Object.keys(task).filter(
				(key) =>
					!CORE_FIELDS.includes(key) &&
					task![key] !== null &&
					task![key] !== undefined &&
					String(task![key]).trim() !== '',
			)
		: [];

	onDestroy(() => {
		stopLiveTimers();
	});
</script>

<div class="h-full flex flex-col overflow-hidden">
	<!-- Header -->
	<header
		class="flex-shrink-0 border-b border-[#222] px-6 py-3 flex items-center justify-between"
	>
		<div class="flex items-center gap-3">
			<button
				type="button"
				on:click={goBack}
				class="text-gray-400 hover:text-white transition-colors text-xs border border-[#333] px-2 py-1 hover:border-white"
			>
				&larr; Back
			</button>
			{#if task}
				<h1 class="text-lg font-bold text-white tracking-tight font-mono">
					{task.display_id || `T${String(task.id ?? '').padStart(4, '0')}`}
				</h1>
				<span
					class={`text-[10px] px-2 py-0.5 border rounded uppercase ${statusClass(taskStatus(task))}`}
				>
					{taskStatus(task)}
				</span>
			{:else}
				<h1 class="text-lg font-bold text-white tracking-tight">Task Detail</h1>
			{/if}
		</div>
		<button
			type="button"
			on:click={() => loadTask()}
			class="text-xs border border-[#333] px-2 py-1 text-gray-400 hover:text-white hover:border-white transition-colors"
		>
			Refresh
		</button>
	</header>

	{#if loading}
		<div class="flex-1 flex items-center justify-center">
			<div class="text-gray-500 text-sm">Loading task details...</div>
		</div>
	{:else if error}
		<div class="flex-1 flex items-center justify-center">
			<div class="max-w-md text-center">
				<div
					class="bg-red-900/20 border border-red-800 text-red-300 text-sm px-4 py-3 rounded"
				>
					{error}
				</div>
				<button
					type="button"
					on:click={goBack}
					class="mt-4 text-xs text-gray-400 hover:text-white"
				>
					&larr; Return to Task List
				</button>
			</div>
		</div>
	{:else if task}
		<!-- Tab Bar -->
		<div class="flex-shrink-0 border-b border-[#222] flex items-center px-6">
			{#each TABS as tab}
				<button
					type="button"
					class="px-4 py-2.5 text-xs font-medium transition-colors border-b-2 {activeTab ===
					tab.id
						? 'text-white border-cyan-400'
						: 'text-gray-500 border-transparent hover:text-gray-300'}"
					on:click={() => (activeTab = tab.id)}
				>
					{tab.label}{tab.id === 'audit' ? ` (${auditLog.length})` : ''}{tab.id === 'tools' ? ` (${toolCalls.length})` : ''}
				</button>
			{/each}
		</div>

		<!-- Tab Content -->
		<div class="flex-1 overflow-auto p-6">
			{#if activeTab === 'overview'}
				<!-- Cost / Model / Truncation badges -->
				<div class="flex flex-wrap items-center gap-2 mb-4">
					<span
						class="inline-flex items-center gap-1.5 text-[11px] border border-amber-800 bg-amber-950/20 text-amber-200 px-2 py-1 rounded"
						title="Estimated cost in USD"
					>
						<span class="text-amber-400/80 uppercase tracking-wider text-[9px]">Cost</span>
						<span class="font-mono">{fmtUsd(task.cost_usd)}</span>
					</span>
					<span
						class="inline-flex items-center gap-1.5 text-[11px] border border-[#333] bg-[#0c0c0c] text-[#aaa] px-2 py-1"
						title="Total tokens (input + output)"
					>
						<span class="text-[#666] uppercase tracking-wider text-[9px]">Tokens</span>
						<span class="font-mono">{fmtTokens(task.total_tokens)}</span>
					</span>
					<span
						class="inline-flex items-center gap-1.5 text-[11px] border border-[#333] bg-[#0c0c0c] text-[#aaa] px-2 py-1"
						title="Provider and model used"
					>
						<span class="text-gray-500 uppercase tracking-wider text-[9px]">Model</span>
						<span class="font-mono">{modelDisplay(task)}</span>
					</span>
					{#if truncationCount(task) > 0}
						<a
							href={`/diagnostics?task=${encodeURIComponent(task.display_id || String(task.id ?? ''))}`}
							class="inline-flex items-center gap-1.5 text-[11px] border border-orange-800 bg-orange-950/20 text-orange-200 px-2 py-1 rounded hover:bg-orange-950/40 transition-colors"
							title="Tool outputs truncated by output cap. Open Diagnostics for detail."
						>
							<span class="text-orange-400/80 uppercase tracking-wider text-[9px]">Truncated</span>
							<span class="font-mono">{truncationCount(task)}</span>
						</a>
					{/if}
				</div>
				<!-- Identity + Status Cards -->
				<div class="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
					<!-- Identity -->
					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-3"
						>
							Identity
						</div>
						<div class="space-y-2 text-xs">
							<div>
								<span class="text-gray-500">Display ID:</span>
								<span class="text-[#aaa] font-mono ml-1"
									>{task.display_id || '--'}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Internal ID:</span>
								<span class="text-gray-300 font-mono ml-1"
									>{task.id ?? '--'}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Title:</span>
								<span class="text-gray-200 ml-1"
									>{task.title || 'Untitled'}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Agent:</span>
								<span class="text-gray-300 ml-1"
									>{task.agent_id || '--'}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Priority:</span>
								<span class="text-gray-300 font-mono ml-1"
									>{Number(task.priority ?? 0)}</span
								>
							</div>
						</div>
					</div>

					<!-- Strategy -->
					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-3"
						>
							Strategy
						</div>
						<div class="space-y-2 text-xs">
							<div>
								<span class="text-gray-500">Strategy ID:</span>
								{#if task.strategy_id || task.strategy_display_id}
									<button
										type="button"
										class="text-[#aaa] hover:text-white font-mono ml-1"
										on:click={() => task && goToStrategy(task)}
									>
										{strategyLabel(task)}
									</button>
								{:else}
									<span class="text-gray-600 ml-1">--</span>
								{/if}
							</div>
							<div>
								<span class="text-gray-500">Strategy Name:</span>
								<span class="text-gray-300 ml-1"
									>{task.strategy_name || '--'}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Stage:</span>
								<span class="text-gray-300 ml-1"
									>{task.strategy_stage || '--'}</span
								>
							</div>
						</div>
					</div>

					<!-- Timing -->
					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-3"
						>
							Timing
						</div>
						<div class="space-y-2 text-xs">
							<div>
								<span class="text-gray-500">Created:</span>
								<span class="text-gray-300 ml-1"
									>{fmtDate(task.created_at)}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Started:</span>
								<span class="text-gray-300 ml-1"
									>{fmtDate(task.started_at)}</span
								>
							</div>
							<div>
								<span class="text-gray-500">Completed:</span>
								<span class="text-gray-300 ml-1"
									>{fmtDate(task.completed_at)}</span
								>
							</div>
							{#if task.retry_at}
								<div>
									<span class="text-gray-500">Retry At:</span>
									<span class="text-yellow-300 ml-1"
										>{fmtDate(task.retry_at)}</span
									>
								</div>
							{/if}
							<div>
								<span class="text-gray-500">Elapsed:</span>
								<span class="text-gray-300 font-mono ml-1"
									>{elapsed(task, nowTick)}</span
								>
							</div>
						</div>
					</div>
				</div>

				<!-- Error Banner -->
				{#if task.error}
					<div
						class="mb-6 border border-red-800 rounded p-4 bg-red-950/20"
					>
						<div
							class="text-[10px] uppercase tracking-wider text-red-400 mb-2"
						>
							Error
						</div>
						<pre
							class="text-sm text-red-300 whitespace-pre-wrap break-words">{formatValue(task.error)}</pre>
					</div>
				{/if}

				<!-- Lifecycle Timeline -->
				<div class="border border-[#222] rounded p-4 bg-[#0c0c0c] mb-6">
					<div
						class="text-[10px] uppercase tracking-wider text-gray-500 mb-3"
					>
						Lifecycle Timeline
					</div>
					<div class="relative pl-4 border-l border-[#333] space-y-4">
						{#if task.created_at}
							<div class="relative">
								<div
									class="absolute -left-[21px] top-1 w-2.5 h-2.5 rounded-full bg-gray-600 border-2 border-[#0c0c0c]"
								></div>
								<div class="text-xs text-gray-300">
									Container Created
								</div>
								<div class="text-[11px] text-gray-500">
									{fmtDate(task.created_at)} &middot; Assigned
									to {task.agent_id || '--'}
								</div>
							</div>
						{/if}
						{#if task.started_at}
							<div class="relative">
								<div
									class="absolute -left-[21px] top-1 w-2.5 h-2.5 rounded-full bg-white border-2 border-[#0c0c0c]"
								></div>
								<div class="text-xs text-[#aaa]">
									Execution Started
								</div>
								<div class="text-[11px] text-gray-500">
									{fmtDate(task.started_at)}
								</div>
							</div>
						{/if}
						{#if task.retry_at}
							<div class="relative">
								<div
									class="absolute -left-[21px] top-1 w-2.5 h-2.5 rounded-full bg-yellow-500 border-2 border-[#0c0c0c]"
								></div>
								<div class="text-xs text-yellow-300">
									Retry Scheduled
								</div>
								<div class="text-[11px] text-gray-500">
									{fmtDate(task.retry_at)}
								</div>
							</div>
						{/if}
						{#if task.completed_at}
							<div class="relative">
								<div
									class="absolute -left-[21px] top-1 w-2.5 h-2.5 rounded-full {taskStatus(task) === 'failed' ? 'bg-red-500' : 'bg-green-500'} border-2 border-[#0c0c0c]"
								></div>
								<div
									class="text-xs {taskStatus(task) === 'failed'
										? 'text-red-300'
										: 'text-green-300'}"
								>
									Execution Completed
								</div>
								<div class="text-[11px] text-gray-500">
									{fmtDate(task.completed_at)} &middot; Status: {taskStatus(task)}
								</div>
							</div>
						{/if}
					</div>
				</div>

				<!-- Quick Data Preview -->
				{#if extraFields.length > 0}
					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-3"
						>
							Additional Fields
						</div>
						<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
							{#each extraFields as field}
								<div
									class="border border-[#1b1b1b] rounded p-2 bg-black/30"
								>
									<div
										class="text-[10px] text-gray-500 uppercase tracking-wider"
									>
										{field.replace(/_/g, ' ')}
									</div>
									{#if typeof task[field] === 'object'}
										<pre
											class="mt-1 max-h-[100px] overflow-auto text-[11px] text-gray-300 whitespace-pre-wrap break-words">{formatValue(task[field])}</pre>
									{:else}
										<div class="mt-1 text-xs text-gray-300">
											{formatValue(task[field])}
										</div>
									{/if}
								</div>
							{/each}
						</div>
					</div>
				{/if}
			{:else if activeTab === 'transcript'}
				{#if transcript.length === 0}
					<div class="text-sm text-gray-600">
						No transcript recorded for this task. Per-round transcripts are
						captured for runs executed after the agent-overhaul upgrade.
					</div>
				{:else}
					<div class="space-y-2">
						{#each transcript as msg (msg.id)}
							{#if msg.role === 'user'}
								<div class="border border-[#222] rounded p-3 bg-[#0c0c0c]">
									<div class="flex items-center justify-between gap-4">
										<div class="text-[10px] uppercase tracking-wider text-[#888]">
											Prompt
										</div>
										<div class="text-[11px] text-gray-600 whitespace-nowrap">
											{fmtDate(msg.created_at)}
										</div>
									</div>
									<pre
										class="mt-1 max-h-[280px] overflow-auto text-xs text-gray-300 whitespace-pre-wrap break-words">{msg.content}</pre>
								</div>
							{:else if msg.role === 'assistant'}
								<div class="border border-[#222] rounded p-3 bg-[#0c0c0c]">
									<div class="flex items-center justify-between gap-4">
										<div class="text-[10px] uppercase tracking-wider text-[#888]">
											Assistant
											{#if msg.tool_round != null}
												<span class="text-gray-600 normal-case tracking-normal ml-1"
													>round {msg.tool_round + 1}</span
												>
											{/if}
										</div>
										<div class="text-[11px] text-gray-600 whitespace-nowrap">
											{#if msg.input_tokens || msg.output_tokens}
												<span class="font-mono"
													>{fmtTokens(msg.input_tokens)} in / {fmtTokens(
														msg.output_tokens,
													)} out</span
												>
												<span class="mx-1 text-gray-800">·</span>
											{/if}
											{#if msg.provider}
												<span class="font-mono">{msg.provider}{msg.model_id ? `/${msg.model_id}` : ''}</span>
												<span class="mx-1 text-gray-800">·</span>
											{/if}
											{fmtDate(msg.created_at)}
										</div>
									</div>
									{#if msg.reasoning}
										<details class="mt-2" open>
											<summary
												class="cursor-pointer text-[10px] uppercase tracking-wider text-amber-500/90 select-none"
											>
												Reasoning
											</summary>
											<pre
												class="mt-1 max-h-[280px] overflow-auto bg-black/40 border border-amber-900/30 rounded p-2 text-[11px] text-amber-100/70 whitespace-pre-wrap break-words">{msg.reasoning}</pre>
										</details>
									{/if}
									{#if msg.content}
										<pre
											class="mt-2 max-h-[320px] overflow-auto text-xs text-gray-200 whitespace-pre-wrap break-words">{msg.content}</pre>
									{/if}
								</div>
							{:else if msg.role === 'tool'}
								<div class="border border-[#222] rounded p-3 bg-[#0c0c0c] ml-5">
									<div class="flex items-center justify-between gap-4">
										<div class="text-sm">
											<span class="text-[10px] uppercase tracking-wider text-purple-400"
												>Tool</span
											>
											<span class="font-mono text-gray-200 ml-2"
												>{msg.tool_name || 'tool'}</span
											>
										</div>
										<div class="text-[11px] text-gray-600 whitespace-nowrap">
											{fmtDate(msg.created_at)}
										</div>
									</div>
									{#if msg.tool_args}
										<details class="mt-2">
											<summary
												class="cursor-pointer text-[10px] uppercase tracking-wider text-gray-600 select-none"
											>
												Arguments
											</summary>
											<pre
												class="mt-1 max-h-[200px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-2 text-[11px] text-gray-300 whitespace-pre-wrap break-words">{formatValue(msg.tool_args)}</pre>
										</details>
									{/if}
									{#if msg.tool_result}
										<details class="mt-2">
											<summary
												class="cursor-pointer text-[10px] uppercase tracking-wider text-gray-600 select-none"
											>
												Result
											</summary>
											<pre
												class="mt-1 max-h-[280px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-2 text-[11px] text-gray-300 whitespace-pre-wrap break-words">{formatValue(msg.tool_result)}</pre>
										</details>
									{/if}
								</div>
							{:else}
								<div
									class="px-3 py-1.5 ml-5 text-[11px] text-gray-500 italic border-l-2 border-[#333]"
								>
									{msg.content || msg.role}
								</div>
							{/if}
						{/each}
					</div>
				{/if}
			{:else if activeTab === 'audit'}
				{#if auditLog.length === 0}
					<div class="text-sm text-gray-600">
						No audit events recorded for this task.
					</div>
				{:else}
					<div class="space-y-2">
						{#each auditLog as event, i}
							<div
								class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
							>
								<div
									class="flex items-start justify-between gap-4"
								>
									<div class="min-w-0">
										<div class="text-sm text-gray-200">
											<span class="font-mono text-[#aaa]"
												>#{i + 1}</span
											>
											{String(
												event.event ||
													event.action ||
													'event',
											)}
										</div>
										{#if event.from || event.to}
											<div
												class="mt-1 text-xs font-mono text-gray-400"
											>
												{String(event.from || '--')} &rarr;
												{String(event.to || '--')}
											</div>
										{/if}
										{#if event.reason}
											<div
												class="mt-1 text-xs text-gray-500"
											>
												{String(event.reason)}
											</div>
										{/if}
									</div>
									<div
										class="text-[11px] text-gray-600 whitespace-nowrap"
									>
										{fmtDate(
											event.timestamp ||
												event.created_at,
										)}
									</div>
								</div>
								<!-- Show any extra fields on the audit event -->
								{#if auditExtras(event).length > 0}
									<div
										class="mt-2 grid grid-cols-2 gap-2 text-[10px]"
									>
										{#each auditExtras(event) as [key, val]}
											<div
												class="border border-[#1b1b1b] rounded px-2 py-1 bg-black/30"
											>
												<span
													class="text-gray-600 uppercase"
													>{key.replace(
														/_/g,
														' ',
													)}</span
												>
												<span class="text-gray-400 ml-1"
													>{typeof val === 'object'
														? JSON.stringify(val)
														: String(val)}</span
												>
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			{:else if activeTab === 'tools'}
				{#if toolCalls.length === 0}
					<div class="text-sm text-gray-600">
						No tool calls recorded for this task.
					</div>
				{:else}
					<div class="space-y-2">
						{#each toolCalls as call, i}
							<div
								class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
							>
								<div
									class="flex items-start justify-between gap-4"
								>
									<div class="min-w-0">
										<div class="text-sm text-gray-200">
											<span class="font-mono text-[#aaa]"
												>#{i + 1}</span
											>
											<span class="font-mono ml-1"
												>{String(
													call.tool_name ||
														call.tool ||
														'tool',
												)}</span
											>
										</div>
										<div
											class="mt-1 text-xs text-gray-500"
										>
											Duration: <span
												class="text-gray-300 font-mono"
												>{fmtMs(
													call.duration_ms,
												)}</span
											>
										</div>
									</div>
									<div
										class="text-[11px] text-gray-600 whitespace-nowrap"
									>
										{fmtDate(
											call.started_at ||
												call.created_at,
										)}
									</div>
								</div>

								{#if call.error}
									<div
										class="mt-2 bg-red-950/20 border border-red-900 rounded p-2"
									>
										<div
											class="text-[10px] text-red-400 uppercase tracking-wider"
										>
											Error
										</div>
										<pre
											class="mt-1 text-xs text-red-300 whitespace-pre-wrap break-words">{formatValue(call.error)}</pre>
									</div>
								{/if}

								{#if call.input_json}
									<div class="mt-2">
										<div
											class="text-[10px] text-gray-600 uppercase tracking-wider"
										>
											Input
										</div>
										<pre
											class="mt-1 max-h-[200px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-2 text-[11px] text-gray-300 whitespace-pre-wrap break-words">{formatValue(call.input_json)}</pre>
									</div>
								{/if}

								{#if call.output_summary}
									<div class="mt-2">
										<div
											class="text-[10px] text-gray-600 uppercase tracking-wider"
										>
											Output
										</div>
										<pre
											class="mt-1 max-h-[200px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-2 text-[11px] text-gray-300 whitespace-pre-wrap break-words">{formatValue(call.output_summary)}</pre>
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			{:else if activeTab === 'data'}
				<div class="space-y-4">
					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
						>
							Description
						</div>
						<pre
							class="max-h-[300px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(task.description)}</pre>
					</div>

					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
						>
							Input Data
						</div>
						<pre
							class="max-h-[400px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(task.input_data)}</pre>
					</div>

					<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
						>
							Output Data
						</div>
						<pre
							class="max-h-[400px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(task.output_data)}</pre>
					</div>

					<div
						class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4"
					>
						<div
							class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
						>
							<div
								class="text-[10px] text-gray-500 uppercase tracking-wider"
							>
								Assigned By
							</div>
							<div class="mt-1 text-xs text-gray-300">
								{formatValue(task.assigned_by)}
							</div>
						</div>
						<div
							class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
						>
							<div
								class="text-[10px] text-gray-500 uppercase tracking-wider"
							>
								Type
							</div>
							<div class="mt-1 text-xs text-gray-300">
								{formatValue(task.type)}
							</div>
						</div>
						<div
							class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
						>
							<div
								class="text-[10px] text-gray-500 uppercase tracking-wider"
							>
								Decision
							</div>
							<div class="mt-1 text-xs text-gray-300">
								{formatValue(task.decision)}
							</div>
						</div>
						<div
							class="border border-[#222] rounded p-3 bg-[#0c0c0c]"
						>
							<div
								class="text-[10px] text-gray-500 uppercase tracking-wider"
							>
								Feedback
							</div>
							<div class="mt-1 text-xs text-gray-300">
								{formatValue(task.feedback)}
							</div>
						</div>
					</div>

					{#if task.error}
						<div
							class="border border-red-800 rounded p-4 bg-red-950/20"
						>
							<div
								class="text-[10px] uppercase tracking-wider text-red-400 mb-2"
							>
								Error
							</div>
							<pre
								class="max-h-[300px] overflow-auto text-xs text-red-300 whitespace-pre-wrap break-words">{formatValue(task.error)}</pre>
						</div>
					{/if}
				</div>
			{:else if activeTab === 'raw'}
				<div class="border border-[#222] rounded p-4 bg-[#0c0c0c]">
					<div
						class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
					>
						Full Task Container
					</div>
					<pre
						class="max-h-[600px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(task)}</pre>
				</div>

				{#if auditLog.length > 0}
					<div
						class="border border-[#222] rounded p-4 bg-[#0c0c0c] mt-4"
					>
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
						>
							Audit Log (Raw)
						</div>
						<pre
							class="max-h-[400px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(auditLog)}</pre>
					</div>
				{/if}

				{#if toolCalls.length > 0}
					<div
						class="border border-[#222] rounded p-4 bg-[#0c0c0c] mt-4"
					>
						<div
							class="text-[10px] uppercase tracking-wider text-gray-500 mb-2"
						>
							Tool Calls (Raw)
						</div>
						<pre
							class="max-h-[400px] overflow-auto bg-black/40 border border-[#1b1b1b] rounded p-3 text-xs text-gray-300 whitespace-pre-wrap break-words">{formatValue(toolCalls)}</pre>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>
