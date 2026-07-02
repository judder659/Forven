<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		assignErrorToAgent,
		dismissForvenAgentTask,
		getPipelineMotionLog,
		getTaskAudit,
		getTaskContainers,
		type PipelineMotionLogEntry,
		type TaskContainer,
	} from '$lib/api';
	import { createRealtimeRefresh } from '$lib/utils/realtime';

	type StatusBucket = 'all' | 'pending' | 'running' | 'paused_manual' | 'done' | 'failed' | 'blocked' | 'rejected';

	const BUCKETS: Array<{ id: StatusBucket; label: string }> = [
		{ id: 'all', label: 'All' },
		{ id: 'pending', label: 'Pending' },
		{ id: 'running', label: 'Running' },
		{ id: 'paused_manual', label: 'Paused' },
		{ id: 'done', label: 'Done' },
		{ id: 'failed', label: 'Failed' },
		{ id: 'blocked', label: 'Blocked' },
		{ id: 'rejected', label: 'Rejected' },
	];

	// The route this tab lives on — used for detail-page back links.
	const RETURN_TO = '/agents?tab=tasks';

	let loading = false;
	let error: string | null = null;
	let search = '';
	let bucket: StatusBucket = 'all';
	// Drill-through filter seeded from /insights (e.g. ?model=gpt-4o). The model_id
	// column rides along on every task-container row via SELECT t.* even though it
	// isn't in the typed TaskContainer interface, so we can match it exactly.
	let modelFilter = '';
	let tasks: TaskContainer[] = [];
	let selectedTask: TaskContainer | null = null;
	let detailLoading = false;
	let detailError: string | null = null;
	let detailAuditLog: Array<Record<string, unknown>> = [];
	let detailToolCalls: Array<Record<string, unknown>> = [];

	type TaskLogKind = 'lifecycle' | 'audit' | 'tool';
	type TaskLogEntry = {
		kind: TaskLogKind;
		timestamp: string | null;
		sortKey: number;
		title: string;
		summary: string;
		detail: string;
		hasError?: boolean;
	};

	type MotionScopeFilter = 'all' | 'pipeline' | 'live_trading';
	type MotionTypeFilter = 'all' | 'promotion' | 'demotion';

	// Pseudo-agent key for MCP / unassigned tasks (empty agent_id) so they can be
	// toggled in the Agent Filters list like any real agent.
	const MCP_FILTER_KEY = '__mcp__';
	let agentFilter: Record<string, boolean> = {};
	let availableAgents: string[] = [];
	let hasUnassignedTasks = false;
	let actionPending: Record<number, boolean> = {};
	let actionError: string | null = null;
	let motionLog: PipelineMotionLogEntry[] = [];
	let motionError: string | null = null;
	let motionLoading = false;
	let motionSearch = '';
	let motionScopeFilter: MotionScopeFilter = 'all';
	let motionTypeFilter: MotionTypeFilter = 'all';
	let expandedMotionRows: Record<string, boolean> = {};

	type MainTab = 'tasks' | 'motion';
	let activeTab: MainTab = 'tasks';

	function taskStatus(task: TaskContainer): string {
		return String(task?.status || 'pending').toLowerCase();
	}

	function taskStatusLabel(status: string): string {
		if (status === 'paused_manual') return 'Paused by manual mode';
		return status || 'pending';
	}

	function ensureAgentFilter(nextTasks: TaskContainer[]) {
		const nextAgents = Array.from(
			new Set(
				nextTasks
					.map((task) => String(task.agent_id || '').trim())
					.filter((agent) => agent.length > 0)
			)
		).sort((a, b) => a.localeCompare(b));
		// Any task without an agent_id is an MCP / unassigned task; surface it as a
		// single pseudo-filter so those rows aren't permanently un-filterable.
		hasUnassignedTasks = nextTasks.some((task) => String(task.agent_id || '').trim().length === 0);
		const filterKeys = hasUnassignedTasks ? [...nextAgents, MCP_FILTER_KEY] : nextAgents;
		const nextFilter: Record<string, boolean> = {};
		for (const key of filterKeys) {
			nextFilter[key] = key in agentFilter ? Boolean(agentFilter[key]) : true;
		}
		agentFilter = nextFilter;
		availableAgents = nextAgents;
	}

	function setAllAgentFilters(value: boolean) {
		const next: Record<string, boolean> = {};
		for (const key of Object.keys(agentFilter)) next[key] = value;
		agentFilter = next;
	}

	let motionLoaded = false;

	// Pre-apply filters from drill-through links (e.g. /agents?tab=tasks&model=...,
	// &role=..., or &status=failed from the roster error strip). model_id is an
	// exact column match; role isn't carried on task rows, so we seed the free-text
	// search with it as an honest text pre-filter.
	function applyUrlFilters() {
		const params = $page.url.searchParams;
		const model = (params.get('model') || '').trim();
		const role = (params.get('role') || '').trim();
		const status = (params.get('status') || '').trim().toLowerCase();
		if (model) modelFilter = model;
		if (role && !search.trim()) search = role;
		if (BUCKETS.some((item) => item.id === status)) bucket = status as StatusBucket;
	}

	function selectBucket(next: StatusBucket) {
		bucket = next;
		// Drop a seeded ?status= once the operator picks a different bucket so a
		// remount of this tab doesn't snap the view back to the drill-through filter.
		const url = new URL($page.url);
		if (url.searchParams.has('status') && url.searchParams.get('status') !== next) {
			url.searchParams.delete('status');
			void goto(`${url.pathname}${url.search}`, { replaceState: true, keepFocus: true, noScroll: true });
		}
	}

	function clearModelFilter() {
		modelFilter = '';
		// Also strip the URL param so remounting the tab doesn't resurrect the chip.
		const url = new URL($page.url);
		if (url.searchParams.has('model')) {
			url.searchParams.delete('model');
			void goto(`${url.pathname}${url.search}`, { replaceState: true, keepFocus: true, noScroll: true });
		}
	}

	async function loadTasks() {
		loading = true;
		error = null;
		try {
			const rows = await getTaskContainers({ limit: 1000 });
			tasks = Array.isArray(rows) ? rows : [];
			ensureAgentFilter(tasks);
			if (selectedTask?.display_id) {
				const match = tasks.find((item) => item.display_id === selectedTask?.display_id) ?? null;
				selectedTask = match;
				if (!match) {
					detailAuditLog = [];
					detailToolCalls = [];
					detailError = 'Selected task no longer exists.';
				}
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load task manager';
		} finally {
			loading = false;
		}
	}

	async function loadMotion() {
		motionLoading = true;
		motionError = null;
		try {
			const rows = await getPipelineMotionLog(300);
			motionLog = Array.isArray(rows) ? rows : [];
			motionLoaded = true;
		} catch (e) {
			motionError = e instanceof Error ? e.message : 'Failed to load pipeline motion log';
		} finally {
			motionLoading = false;
		}
	}

	// Refresh only the data backing the tab the operator is currently viewing so we
	// don't fetch up to 1300 records every cycle when the other tab is hidden.
	async function refresh() {
		if (activeTab === 'motion') {
			await loadMotion();
		} else {
			await loadTasks();
		}
	}

	function selectTab(tab: MainTab) {
		activeTab = tab;
		// Lazily load the motion log the first time the tab is opened.
		if (tab === 'motion' && !motionLoaded && !motionLoading) {
			void loadMotion();
		}
	}

	async function dismissTask(task: TaskContainer) {
		if (typeof task.id !== 'number') return;
		actionError = null;
		actionPending = { ...actionPending, [task.id]: true };
		try {
			await dismissForvenAgentTask(task.id, 'tasks');
			await loadTasks();
		} catch (e) {
			actionError = e instanceof Error ? e.message : 'Failed to dismiss task';
		} finally {
			const { [task.id]: _removed, ...rest } = actionPending;
			actionPending = rest;
		}
	}

	async function reassignTask(task: TaskContainer) {
		if (typeof task.id !== 'number') return;
		const current = String(task.agent_id || '').trim();
		const agentId = (window.prompt('Reassign this task to which agent?', current) || '').trim();
		if (!agentId) return;
		actionError = null;
		actionPending = { ...actionPending, [task.id]: true };
		try {
			await assignErrorToAgent(task.id, agentId, 'Reassigned from Task Manager');
			await loadTasks();
		} catch (e) {
			actionError = e instanceof Error ? e.message : 'Failed to reassign task';
		} finally {
			const { [task.id]: _removed, ...rest } = actionPending;
			actionPending = rest;
		}
	}

	const realtimeController = createRealtimeRefresh(refresh, {
		fallbackMs: 15_000,
		pollWhenWsOfflineOnly: true,
	});

	function statusClass(status: string): string {
		switch (status) {
			case 'running':
				return 'text-white border-[#555] bg-[#111]';
			case 'paused_manual':
				return 'text-yellow-400 border-yellow-900 bg-yellow-500/10';
			case 'done':
			case 'reviewed':
				return 'text-emerald-400 border-emerald-900 bg-emerald-500/10';
			case 'failed':
				return 'text-red-400 border-red-900 bg-red-500/10';
			case 'blocked':
				return 'text-yellow-400 border-yellow-900 bg-yellow-500/10';
			case 'rejected':
				return 'text-[#888] border-[#333] bg-[#111]';
			default:
				return 'text-[#888] border-[#333] bg-[#111]';
		}
	}

	function fmtDate(value: unknown): string {
		if (!value) return '--';
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return '--';
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function parseDateSortKey(value: unknown): number {
		if (!value) return 0;
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return 0;
		return date.getTime();
	}

	function fmtMs(value: unknown): string {
		const n = Number(value);
		if (!Number.isFinite(n)) return '--';
		return `${n.toFixed(0)}ms`;
	}

	function compact(value: unknown, maxLen = 88): string {
		const text = String(value ?? '').trim();
		if (!text) return '--';
		if (text.length <= maxLen) return text;
		return `${text.slice(0, maxLen)}...`;
	}

	function formatValue(value: unknown): string {
		if (value === null || value === undefined) return '--';
		if (typeof value === 'string') {
			const trimmed = value.trim();
			if (!trimmed) return '--';
			if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
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

	function inlineValue(value: unknown, maxLen = 220): string {
		const rendered = formatValue(value).replace(/\s+/g, ' ').trim();
		if (!rendered || rendered === '--') return '';
		return rendered.length <= maxLen ? rendered : `${rendered.slice(0, maxLen)}...`;
	}

	function strategyLabel(task: TaskContainer): string {
		const strategyDisplayId = String(task.strategy_display_id || '').trim();
		if (strategyDisplayId) return strategyDisplayId;
		const strategyId = String(task.strategy_id || '').trim();
		return strategyId || '--';
	}

	function isMcpTask(task: TaskContainer): boolean {
		const assignedBy = String(task.assigned_by || '').trim().toLowerCase();
		const agentId = String(task.agent_id || '').trim();
		return assignedBy === 'manual' && agentId === '';
	}

	function taskAgentLabel(task: TaskContainer): string {
		const agentId = String(task.agent_id || '').trim();
		if (agentId) return agentId;
		if (isMcpTask(task)) return 'mcp';
		return '--';
	}

	function taskTitleLabel(task: TaskContainer): string {
		const raw = String(task.title || '').trim();
		if (raw) return raw;
		if (isMcpTask(task)) {
			const type = String(task.type || '').trim() || 'task';
			const strat = strategyLabel(task);
			return `MCP ${type}${strat !== '--' ? `: ${strat}` : ''}`;
		}
		return '';
	}

	function bucketCount(target: StatusBucket): number {
		if (target === 'all') return tasks.length;
		return tasks.filter((task) => taskStatus(task) === target).length;
	}

	function motionRowKey(entry: PipelineMotionLogEntry): string {
		return [
			String(entry.event_id ?? ''),
			String(entry.timestamp ?? ''),
			String(entry.strategy_id ?? ''),
			String(entry.from_state ?? ''),
			String(entry.to_state ?? ''),
		].join(':');
	}

	function isMotionExpanded(entry: PipelineMotionLogEntry): boolean {
		return Boolean(expandedMotionRows[motionRowKey(entry)]);
	}

	function toggleMotionExpanded(entry: PipelineMotionLogEntry) {
		const key = motionRowKey(entry);
		expandedMotionRows = {
			...expandedMotionRows,
			[key]: !expandedMotionRows[key],
		};
	}

	function motionTypeClass(type: string): string {
		switch (String(type || '').toLowerCase()) {
			case 'promotion':
				return 'text-emerald-400 border-emerald-900 bg-emerald-500/10';
			case 'demotion':
				return 'text-red-400 border-red-900 bg-red-500/10';
			default:
				return 'text-[#888] border-[#333] bg-[#111]';
		}
	}

	function motionScopeClass(scope: string): string {
		switch (String(scope || '').toLowerCase()) {
			case 'live_trading':
				return 'text-yellow-400 border-yellow-900 bg-yellow-500/10';
			case 'pipeline':
				return 'text-[#ccc] border-[#333] bg-[#111]';
			default:
				return 'text-[#888] border-[#333] bg-[#111]';
		}
	}

	function motionPipelines(entry: PipelineMotionLogEntry): string[] {
		const raw = Array.isArray(entry.pipelines) ? entry.pipelines : [];
		const normalized = raw
			.map((item) => String(item || '').trim().toLowerCase())
			.filter((item) => item.length > 0);
		return normalized.length > 0 ? normalized : ['pipeline'];
	}

	function motionScopeCount(scope: MotionScopeFilter): number {
		if (scope === 'all') return motionLog.length;
		return motionLog.filter((entry) => motionPipelines(entry).includes(scope)).length;
	}

	function motionStrategyLabel(entry: PipelineMotionLogEntry): string {
		const displayId = String(entry.strategy_display_id || '').trim();
		if (displayId) return displayId;
		const strategyId = String(entry.strategy_id || '').trim();
		return strategyId || '--';
	}

	function motionMetricPreview(entry: PipelineMotionLogEntry): string {
		const metrics = entry.decision_metrics;
		if (!metrics || typeof metrics !== 'object') return '';
		const orderedKeys = [
			'baseline_sharpe',
			'live_sharpe_72h',
			'degradation',
			'total_trades',
			'trade_count_72h',
			'sharpe',
			'sharpe_ratio',
			'profit_factor',
			'max_drawdown_pct',
			'fitness',
		];
		const parts: string[] = [];
		for (const key of orderedKeys) {
			if (!(key in metrics)) continue;
			const value = (metrics as Record<string, unknown>)[key];
			if (value === null || value === undefined) continue;
			const label = key.replaceAll('_', ' ');
			parts.push(`${label}: ${String(value)}`);
			if (parts.length >= 6) break;
		}
		return parts.join(' | ');
	}

	function buildTaskLog(
		task: TaskContainer,
		auditLog: Array<Record<string, unknown>>,
		toolCalls: Array<Record<string, unknown>>,
	): TaskLogEntry[] {
		const entries: TaskLogEntry[] = [];
		const displayId = String(task.display_id || '').trim() || `T${String(task.id ?? '').padStart(4, '0')}`;
		const status = taskStatus(task);

		const push = (entry: Omit<TaskLogEntry, 'sortKey'>) => {
			entries.push({
				...entry,
				sortKey: parseDateSortKey(entry.timestamp),
			});
		};

		push({
			kind: 'lifecycle',
			timestamp: task.created_at ? String(task.created_at) : null,
			title: 'Container Created',
			summary: `${displayId} assigned to ${String(task.agent_id || '--')}`,
			detail: String(task.title || 'Untitled task'),
		});

		if (task.started_at) {
			push({
				kind: 'lifecycle',
				timestamp: String(task.started_at),
				title: 'Execution Started',
				summary: `Status moved to running`,
				detail: String(task.agent_id || '--'),
			});
		}

		if (task.retry_at) {
			push({
				kind: 'lifecycle',
				timestamp: String(task.retry_at),
				title: 'Retry Scheduled',
				summary: `Queued for retry`,
				detail: String(task.error || ''),
				hasError: true,
			});
		}

		if (task.completed_at) {
			push({
				kind: 'lifecycle',
				timestamp: String(task.completed_at),
				title: 'Execution Completed',
				summary: `Finished with status: ${taskStatusLabel(status)}`,
				detail: String(task.error || ''),
				hasError: status === 'failed',
			});
		}

		for (const auditItem of auditLog) {
			const eventName = String(auditItem.event || auditItem.action || 'audit').trim() || 'audit';
			const from = String(auditItem.from || '').trim();
			const to = String(auditItem.to || '').trim();
			const reason = String(auditItem.reason || '').trim();
			const transition = from || to ? `${from || '--'} → ${to || '--'}` : '';
			push({
				kind: 'audit',
				timestamp: auditItem.timestamp ? String(auditItem.timestamp) : (auditItem.created_at ? String(auditItem.created_at) : null),
				title: `Audit: ${eventName}`,
				summary: transition || eventName,
				detail: reason,
			});
		}

		for (const call of toolCalls) {
			const tool = String(call.tool_name || call.tool || 'tool').trim() || 'tool';
			const duration = fmtMs(call.duration_ms);
			const errorText = String(call.error || '').trim();
			const detailParts = [
				inlineValue(call.input_json) ? `input: ${inlineValue(call.input_json)}` : '',
				inlineValue(call.output_summary) ? `output: ${inlineValue(call.output_summary)}` : '',
				errorText ? `error: ${errorText}` : '',
			].filter((part) => part.length > 0);
			push({
				kind: 'tool',
				timestamp: call.started_at ? String(call.started_at) : (call.created_at ? String(call.created_at) : null),
				title: `Tool: ${tool}`,
				summary: `Duration ${duration}`,
				detail: detailParts.join(' | '),
				hasError: Boolean(errorText),
			});
		}

		return entries.sort((a, b) => a.sortKey - b.sortKey);
	}

	$: filteredTasks = tasks.filter((task) => {
		const status = taskStatus(task);
		if (bucket !== 'all' && status !== bucket) return false;

		if (modelFilter) {
			const taskModel = String(task.model_id || '').trim().toLowerCase();
			if (taskModel !== modelFilter.toLowerCase()) return false;
		}

		const agent = String(task.agent_id || '').trim();
		if (agent) {
			if (agent in agentFilter && !agentFilter[agent]) return false;
		} else if (MCP_FILTER_KEY in agentFilter && !agentFilter[MCP_FILTER_KEY]) {
			return false;
		}

		if (search.trim()) {
			const q = search.trim().toLowerCase();
			const haystack = [
				String(task.display_id || ''),
				String(task.title || ''),
				String(task.agent_id || ''),
				String(task.strategy_id || ''),
				String(task.strategy_display_id || ''),
				String(task.strategy_name || ''),
				String(task.model_id || ''),
				String(task.provider || ''),
				status,
			].join(' ').toLowerCase();
			if (!haystack.includes(q)) return false;
		}
		return true;
	});

	$: filteredMotionLog = motionLog.filter((entry) => {
		const type = String(entry.motion_type || '').trim().toLowerCase();
		if (motionTypeFilter !== 'all' && type !== motionTypeFilter) return false;

		const scopes = motionPipelines(entry);
		if (motionScopeFilter !== 'all' && !scopes.includes(motionScopeFilter)) return false;

		if (motionSearch.trim()) {
			const q = motionSearch.trim().toLowerCase();
			const haystack = [
				String(entry.strategy_id || ''),
				String(entry.strategy_display_id || ''),
				String(entry.strategy_name || ''),
				String(entry.from_state || ''),
				String(entry.to_state || ''),
				String(entry.motion_type || ''),
				String(entry.actor || ''),
				String(entry.layman_reason || ''),
				String(entry.reason || ''),
				String(entry.decision_mode || ''),
				String(entry.decision_summary || ''),
			].join(' ').toLowerCase();
			if (!haystack.includes(q)) return false;
		}

		return true;
	});

	$: inspectorLog = selectedTask ? buildTaskLog(selectedTask, detailAuditLog, detailToolCalls) : [];

	async function inspectTask(task: TaskContainer) {
		selectedTask = task;
		detailError = null;
		detailAuditLog = [];
		detailToolCalls = [];

		if (!task.display_id) {
			detailError = 'Task does not have a container display ID.';
			return;
		}

		detailLoading = true;
		try {
			const details = await getTaskAudit(task.display_id);
			selectedTask = details.task;
			detailAuditLog = Array.isArray(details.audit_log) ? details.audit_log : [];
			detailToolCalls = Array.isArray(details.tool_calls) ? details.tool_calls : [];
		} catch (e) {
			detailError = e instanceof Error ? e.message : 'Failed to load task details';
		} finally {
			detailLoading = false;
		}
	}

	onMount(async () => {
		applyUrlFilters();
		await loadTasks();
		realtimeController.start();
	});

	onDestroy(() => {
		realtimeController.stop();
	});
</script>

<div class="h-full flex overflow-hidden">
	<aside class="w-64 flex-shrink-0 border-r border-[#222] bg-[#050505] p-3 overflow-y-auto">
		<div class="text-[10px] uppercase tracking-wider text-[#666] mb-2">Status Buckets</div>
		{#each BUCKETS as item}
			<button
				type="button"
				class="w-full text-left px-2 py-1.5 text-xs transition-colors {bucket === item.id ? 'bg-[#111] text-white' : 'text-[#888] hover:bg-[#111]'}"
				on:click={() => selectBucket(item.id)}
			>
				{item.label} ({bucketCount(item.id)})
			</button>
		{/each}

		<div class="flex items-center justify-between mt-4 mb-2">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Agent Filters</div>
			{#if availableAgents.length > 0 || hasUnassignedTasks}
				<div class="flex items-center gap-1.5">
					<button type="button" class="text-[10px] text-[#666] hover:text-white" on:click={() => setAllAgentFilters(true)}>All</button>
					<span class="text-[10px] text-[#333]">/</span>
					<button type="button" class="text-[10px] text-[#666] hover:text-white" on:click={() => setAllAgentFilters(false)}>None</button>
				</div>
			{/if}
		</div>
		{#if availableAgents.length === 0 && !hasUnassignedTasks}
			<div class="text-xs text-[#555]">No agents found</div>
		{:else}
			<div class="space-y-1 text-xs">
				{#each availableAgents as agent}
					<label class="flex items-center gap-2 text-[#888]">
						<input type="checkbox" bind:checked={agentFilter[agent]} class="accent-white w-3 h-3" />
						<span class="truncate">{agent}</span>
					</label>
				{/each}
				{#if hasUnassignedTasks}
					<label class="flex items-center gap-2 text-[#888]">
						<input type="checkbox" bind:checked={agentFilter[MCP_FILTER_KEY]} class="accent-white w-3 h-3" />
						<span class="truncate">mcp / unassigned</span>
					</label>
				{/if}
			</div>
		{/if}
	</aside>

	<section class="flex-1 min-w-0 overflow-hidden flex flex-col">
		<div class="flex-shrink-0 border-b border-[#222] flex items-center">
			<button
				type="button"
				class="px-4 py-2.5 text-xs font-medium transition-colors border-b-2 {activeTab === 'tasks' ? 'text-white border-white' : 'text-[#888] border-transparent hover:text-white'}"
				on:click={() => selectTab('tasks')}
			>
				Task Log
				<span class="ml-1 text-[10px] text-[#666]">({filteredTasks.length})</span>
			</button>
			<button
				type="button"
				class="px-4 py-2.5 text-xs font-medium transition-colors border-b-2 {activeTab === 'motion' ? 'text-white border-white' : 'text-[#888] border-transparent hover:text-white'}"
				on:click={() => selectTab('motion')}
			>
				Pipeline Motion Log
				<span class="ml-1 text-[10px] text-[#666]">({motionLog.length})</span>
			</button>
		</div>

		{#if activeTab === 'motion'}
			<div class="flex-1 flex flex-col overflow-hidden">
				<div class="flex-shrink-0 p-3 border-b border-[#222]">
					<div class="flex items-center justify-between gap-2">
						<div class="text-xs text-[#888]">
							{filteredMotionLog.length} visible of {motionLog.length} promotion/demotion decisions
						</div>
						<button
							type="button"
							on:click={refresh}
							class="text-[11px] border border-[#333] px-2 py-1 text-[#888] hover:text-white hover:border-white transition-colors"
						>
							Refresh
						</button>
					</div>

					<div class="mt-3 flex flex-wrap gap-1.5 items-center">
						<button
							type="button"
							on:click={() => (motionScopeFilter = 'all')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionScopeFilter === 'all' ? 'text-white border-white bg-white/10' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							All ({motionScopeCount('all')})
						</button>
						<button
							type="button"
							on:click={() => (motionScopeFilter = 'pipeline')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionScopeFilter === 'pipeline' ? 'text-white border-[#555] bg-[#111]' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							Pipeline ({motionScopeCount('pipeline')})
						</button>
						<button
							type="button"
							on:click={() => (motionScopeFilter = 'live_trading')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionScopeFilter === 'live_trading' ? 'text-yellow-400 border-yellow-700 bg-yellow-500/10' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							Live Trading ({motionScopeCount('live_trading')})
						</button>
						<button
							type="button"
							on:click={() => (motionTypeFilter = 'all')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionTypeFilter === 'all' ? 'text-white border-white bg-white/10' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							All Types
						</button>
						<button
							type="button"
							on:click={() => (motionTypeFilter = 'promotion')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionTypeFilter === 'promotion' ? 'text-emerald-400 border-emerald-700 bg-emerald-500/10' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							Promotions
						</button>
						<button
							type="button"
							on:click={() => (motionTypeFilter = 'demotion')}
							class={`text-[10px] px-2 py-1 border uppercase transition-colors ${motionTypeFilter === 'demotion' ? 'text-red-400 border-red-700 bg-red-500/10' : 'text-[#888] border-[#333] hover:border-[#555]'}`}
						>
							Demotions
						</button>
						<input
							type="text"
							bind:value={motionSearch}
							placeholder="Search motion decisions..."
							class="bg-black border border-[#333] px-2 py-1 text-[11px] min-w-[220px] flex-1 focus:outline-none focus:border-white"
						/>
					</div>
				</div>

				<div class="flex-1 overflow-auto p-3">
					{#if motionError}
						<div class="border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{motionError}</div>
					{:else if motionLoading}
						<div class="text-xs text-[#666]">Loading motion decisions...</div>
					{:else if motionLog.length === 0}
						<div class="text-xs text-[#555]">No promotion/demotion decisions have been recorded yet.</div>
					{:else if filteredMotionLog.length === 0}
						<div class="text-xs text-[#555]">No motion decisions match the current filters ({motionLog.length} total hidden by filters).</div>
					{:else}
						<div class="space-y-2">
							{#each filteredMotionLog as entry}
								<div class="border border-[#1a1a1a] p-2 bg-[#050505] text-[11px]">
									<div class="flex items-start justify-between gap-2">
										<div class="min-w-0">
											<div class="text-white font-mono truncate">{motionStrategyLabel(entry)}</div>
											<div class="text-[#666] truncate">{entry.strategy_name || '--'}</div>
										</div>
										<div class="flex items-center gap-1 flex-wrap justify-end">
											<span class={`px-1.5 py-0.5 border uppercase text-[10px] ${motionTypeClass(String(entry.motion_type || ''))}`}>
												{String(entry.motion_type || 'transition')}
											</span>
											{#each motionPipelines(entry) as scope}
												<span class={`px-1.5 py-0.5 border uppercase text-[10px] ${motionScopeClass(scope)}`}>
													{scope.replace('_', ' ')}
												</span>
											{/each}
										</div>
									</div>

									<div class="mt-1 text-[#ccc] font-mono">
										{String(entry.from_state || '--')} → {String(entry.to_state || '--')}
									</div>
									<div class="mt-1 text-[#555]">
										{fmtDate(entry.timestamp)} | actor {entry.actor || '--'} | decision {entry.decision_mode || 'transition'}
									</div>
									{#if entry.layman_reason}
										<div class="mt-1 text-yellow-400">{compact(entry.layman_reason, 260)}</div>
									{/if}
									{#if entry.reason}
										<div class="mt-1 text-[#666]">raw: {compact(entry.reason, 220)}</div>
									{/if}
									{#if motionMetricPreview(entry)}
										<div class="mt-1 text-[#888]">{motionMetricPreview(entry)}</div>
									{/if}

									<div class="mt-1 flex items-center justify-between">
										<div class="text-[10px] text-[#555]">
											{Array.isArray(entry.related_activity) ? entry.related_activity.length : 0} related activity records
										</div>
										<button
											type="button"
											class="text-[10px] text-[#888] hover:text-white"
											on:click={() => toggleMotionExpanded(entry)}
										>
											{isMotionExpanded(entry) ? 'Hide Details' : 'Show Details'}
										</button>
									</div>

									{#if isMotionExpanded(entry)}
										<div class="mt-2 space-y-2">
											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[10px] uppercase tracking-wider text-[#666]">Plain-English Why</div>
												<div class="text-yellow-400 mt-1">{entry.layman_reason || '--'}</div>
											</div>

											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[10px] uppercase tracking-wider text-[#666]">Decision Summary</div>
												<div class="text-[#888] mt-1">{entry.decision_summary || '--'}</div>
											</div>

											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[10px] uppercase tracking-wider text-[#666]">Strategy Snapshot</div>
												<pre class="mt-1 max-h-[120px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(entry.strategy_snapshot)}</pre>
											</div>

											{#if Array.isArray(entry.related_activity) && entry.related_activity.length > 0}
												<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
													<div class="text-[10px] uppercase tracking-wider text-[#666]">Related Activity</div>
													<div class="mt-1 space-y-1.5 max-h-[140px] overflow-auto">
														{#each entry.related_activity as related}
															<div class="border border-[#1a1a1a] px-2 py-1 bg-[#050505]">
																<div class="text-[#ccc]">{String(related.message || '--')}</div>
																<div class="text-[#555]">{fmtDate(related.timestamp)}</div>
																<div class="text-[#666]">{String(related.source || '--')} / {String(related.level || '--')}</div>
																{#if related.data}
																	<pre class="mt-1 max-h-[90px] overflow-auto bg-black border border-[#1a1a1a] p-1.5 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(related.data)}</pre>
																{/if}
															</div>
														{/each}
													</div>
												</div>
											{/if}

											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[10px] uppercase tracking-wider text-[#666]">Full Motion Record</div>
												<pre class="mt-1 max-h-[160px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(entry)}</pre>
											</div>
										</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>
			</div>
		{:else}
			<div class="flex-shrink-0 p-3 border-b border-[#222] flex items-center gap-2">
				<input
					type="text"
					bind:value={search}
					placeholder="Search task containers..."
					class="bg-black border border-[#333] px-3 py-1.5 text-xs w-80 focus:outline-none focus:border-white"
				/>
				{#if modelFilter}
					<button
						type="button"
						class="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider px-2 py-1 border text-[#ccc] border-[#333] bg-[#111] hover:border-[#555]"
						title="Clear model filter"
						on:click={clearModelFilter}
					>
						model: {modelFilter}
						<span class="text-[#888]">x</span>
					</button>
				{/if}
				<span class="text-[10px] text-[#666]">{filteredTasks.length} items</span>
				<div class="flex-1"></div>
				<button
					type="button"
					on:click={refresh}
					class="text-[11px] border border-[#333] px-2 py-1 text-[#888] hover:text-white hover:border-white transition-colors"
				>
					Refresh
				</button>
			</div>

			{#if error}
				<div class="mx-3 mt-3 border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{error}</div>
			{/if}
			{#if actionError}
				<div class="mx-3 mt-3 border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{actionError}</div>
			{/if}

			<div class="flex-1 flex min-h-0">
				<div class="flex-1 min-w-0 overflow-auto border-r border-[#222]">
					<table class="w-full text-xs">
						<thead class="sticky top-0 bg-[#0d0d0d] z-10">
							<tr class="text-[#666] border-b border-[#222]">
								<th class="py-2 px-2 text-left">T-ID</th>
								<th class="py-2 px-2 text-left">Title</th>
								<th class="py-2 px-2 text-left">Agent</th>
								<th class="py-2 px-2 text-left">Strategy</th>
								<th class="py-2 px-2 text-left">Status</th>
								<th class="py-2 px-2 text-right">Priority</th>
								<th class="py-2 px-2 text-left">Created</th>
								<th class="py-2 px-2 text-left">Completed</th>
								<th class="py-2 px-2 text-right">Audit</th>
								<th class="py-2 px-2 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#if loading}
								<tr><td colspan="10" class="py-8 text-center text-[#555]">Loading...</td></tr>
							{:else if filteredTasks.length === 0}
								<tr><td colspan="10" class="py-8 text-center text-[#555]">No task containers match this view.</td></tr>
							{:else}
								{#each filteredTasks as task}
									<tr class="border-t border-[#181818] hover:bg-[#111] transition-colors">
										<td class="py-2 px-2 font-mono">
											{#if task.display_id}
												<button type="button" class="text-white hover:underline" on:click={() => goto(`/tasks/${encodeURIComponent(String(task.display_id))}?returnTo=${encodeURIComponent(RETURN_TO)}`)}>
													{task.display_id}
												</button>
											{:else}
												<span class="text-[#555]">--</span>
											{/if}
										</td>
										<td class="py-2 px-2 text-[#ccc] max-w-[340px] truncate" title={taskTitleLabel(task) || String(task.title || '')}>{compact(taskTitleLabel(task), 76)}</td>
										<td class="py-2 px-2 {isMcpTask(task) ? 'text-[#888]' : 'text-[#ccc]'}">{taskAgentLabel(task)}</td>
										<td class="py-2 px-2 text-[#ccc] font-mono">{strategyLabel(task)}</td>
										<td class="py-2 px-2">
											<span class={`text-[10px] px-1.5 py-0.5 border uppercase ${statusClass(taskStatus(task))}`}>{taskStatusLabel(taskStatus(task))}</span>
										</td>
										<td class="py-2 px-2 text-right text-[#ccc] font-mono">{Number(task.priority ?? 0)}</td>
										<td class="py-2 px-2 text-[#666]">{fmtDate(task.created_at)}</td>
										<td class="py-2 px-2 text-[#666]">{fmtDate(task.completed_at)}</td>
										<td class="py-2 px-2 text-right text-[#888] font-mono">{Array.isArray(task.audit_log) ? task.audit_log.length : 0}</td>
										<td class="py-2 px-2 text-right whitespace-nowrap">
											<div class="inline-flex items-center gap-2 justify-end">
												<button type="button" class="text-white hover:underline" on:click={() => void inspectTask(task)}>Inspect</button>
												<button
													type="button"
													class="text-[#888] hover:text-white disabled:opacity-40"
													disabled={Boolean(actionPending[task.id])}
													on:click={() => void reassignTask(task)}
												>Reassign</button>
												<button
													type="button"
													class="text-red-400 hover:text-red-300 disabled:opacity-40"
													disabled={Boolean(actionPending[task.id])}
													on:click={() => void dismissTask(task)}
												>Dismiss</button>
											</div>
										</td>
									</tr>
								{/each}
							{/if}
						</tbody>
					</table>
				</div>

				<aside class="w-[400px] max-w-[45%] min-w-[320px] overflow-auto bg-[#050505]">
				<div class="px-3 py-2 border-b border-[#222] text-xs uppercase tracking-wider text-[#666]">Task Inspector</div>
				{#if !selectedTask}
					<div class="p-4 text-xs text-[#555]">Select a task container to inspect audit and tool-call history.</div>
				{:else}
					<div class="p-3 space-y-3">
						<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Container</div>
							<div class="mt-1 text-sm text-white font-mono">{selectedTask.display_id || '--'}</div>
							<div class="mt-2 text-xs text-[#ccc]">{taskTitleLabel(selectedTask) || selectedTask.title || 'Untitled Task'}</div>
							<div class="mt-2 grid grid-cols-2 gap-2 text-[11px]">
								<div><span class="text-[#666]">Agent:</span> <span class="{isMcpTask(selectedTask) ? 'text-[#888]' : 'text-[#ccc]'}">{taskAgentLabel(selectedTask)}</span></div>
								<div><span class="text-[#666]">Status:</span> <span class="text-[#ccc]">{taskStatusLabel(taskStatus(selectedTask))}</span></div>
								<div><span class="text-[#666]">Strategy:</span> <span class="text-[#ccc] font-mono">{strategyLabel(selectedTask)}</span></div>
								<div><span class="text-[#666]">Priority:</span> <span class="text-[#ccc]">{Number(selectedTask.priority ?? 0)}</span></div>
							</div>
							<div class="mt-3 flex items-center gap-2">
								<button
									type="button"
									class="text-[11px] border border-[#333] px-2 py-1 text-[#888] hover:text-white hover:border-white transition-colors disabled:opacity-40"
									disabled={Boolean(actionPending[selectedTask.id])}
									on:click={() => selectedTask && void reassignTask(selectedTask)}
								>Reassign</button>
								<button
									type="button"
									class="text-[11px] border border-red-900 px-2 py-1 text-red-400 hover:text-red-300 hover:border-red-700 transition-colors disabled:opacity-40"
									disabled={Boolean(actionPending[selectedTask.id])}
									on:click={() => selectedTask && void dismissTask(selectedTask)}
								>Dismiss</button>
							</div>
						</div>

							{#if detailLoading}
								<div class="text-xs text-[#666]">Loading details...</div>
							{:else if detailError}
								<div class="border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{detailError}</div>
							{:else}
								<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
									<div class="flex items-center justify-between">
										<div class="text-[10px] uppercase tracking-wider text-[#666]">Container Log</div>
										<div class="text-[10px] text-[#555]">{inspectorLog.length} events</div>
									</div>
									{#if inspectorLog.length === 0}
										<div class="mt-2 text-xs text-[#555]">No lifecycle events recorded.</div>
									{:else}
										<div class="mt-2 space-y-1.5 max-h-[260px] overflow-auto">
											{#each inspectorLog as entry}
												<div class="text-[11px] border border-[#1a1a1a] px-2 py-1 bg-[#050505]">
													<div class="flex items-center justify-between gap-2">
														<div class={entry.hasError ? 'text-red-400' : 'text-[#ccc]'}>{entry.title}</div>
														<div class="text-[#555]">{fmtDate(entry.timestamp)}</div>
													</div>
													<div class="text-[#666] mt-0.5">{entry.summary}</div>
													{#if entry.detail}
														<div class={entry.hasError ? 'text-red-400 mt-0.5' : 'text-[#666] mt-0.5'}>{entry.detail}</div>
													{/if}
												</div>
											{/each}
										</div>
									{/if}
								</div>

								<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Task Data</div>
									<div class="mt-2 space-y-2">
										<div>
											<div class="text-[10px] text-[#666] uppercase tracking-wider">Description</div>
											<pre class="mt-1 max-h-[110px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(selectedTask.description)}</pre>
										</div>
										<div>
											<div class="text-[10px] text-[#666] uppercase tracking-wider">Input Data</div>
											<pre class="mt-1 max-h-[120px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(selectedTask.input_data)}</pre>
										</div>
										<div>
											<div class="text-[10px] text-[#666] uppercase tracking-wider">Output Data</div>
											<pre class="mt-1 max-h-[140px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(selectedTask.output_data)}</pre>
										</div>
										<div class="grid grid-cols-2 gap-2 text-[10px]">
											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[#666] uppercase tracking-wider">Assigned By</div>
												<div class="text-[#ccc] mt-1">{formatValue(selectedTask.assigned_by)}</div>
											</div>
											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[#666] uppercase tracking-wider">Type</div>
												<div class="text-[#ccc] mt-1">{formatValue(selectedTask.type)}</div>
											</div>
											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[#666] uppercase tracking-wider">Decision</div>
												<div class="text-[#ccc] mt-1">{formatValue(selectedTask.decision)}</div>
											</div>
											<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
												<div class="text-[#666] uppercase tracking-wider">Feedback</div>
												<div class="text-[#ccc] mt-1">{formatValue(selectedTask.feedback)}</div>
											</div>
										</div>
										{#if selectedTask.error}
											<div>
												<div class="text-[10px] text-red-400 uppercase tracking-wider">Error</div>
												<pre class="mt-1 max-h-[90px] overflow-auto bg-red-500/5 border border-red-900 p-2 text-[10px] text-red-400 whitespace-pre-wrap break-words">{formatValue(selectedTask.error)}</pre>
											</div>
										{/if}
									</div>
								</div>

								<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
									<div class="flex items-center justify-between">
										<div class="text-[10px] uppercase tracking-wider text-[#666]">Audit Log</div>
										<div class="text-[10px] text-[#555]">{detailAuditLog.length} events</div>
									</div>
									{#if detailAuditLog.length === 0}
										<div class="mt-2 text-xs text-[#555]">No audit events recorded.</div>
									{:else}
										<div class="mt-2 space-y-1.5 max-h-[220px] overflow-auto">
											{#each detailAuditLog as event}
												<div class="text-[11px] border border-[#1a1a1a] px-2 py-1 bg-[#050505]">
													<div class="text-[#888]">{String(event.event || event.action || 'event')}</div>
													<div class="text-[#555]">{fmtDate(event.timestamp || event.created_at)}</div>
													{#if event.reason}
														<div class="text-[#666] mt-0.5">{compact(event.reason, 120)}</div>
													{/if}
												</div>
											{/each}
										</div>
									{/if}
								</div>

								<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
									<div class="flex items-center justify-between">
										<div class="text-[10px] uppercase tracking-wider text-[#666]">Tool Calls</div>
										<div class="text-[10px] text-[#555]">{detailToolCalls.length} calls</div>
									</div>
									{#if detailToolCalls.length === 0}
										<div class="mt-2 text-xs text-[#555]">No tool calls recorded.</div>
									{:else}
										<div class="mt-2 space-y-1.5 max-h-[260px] overflow-auto">
											{#each detailToolCalls as call}
												<div class="text-[11px] border border-[#1a1a1a] px-2 py-1 bg-[#050505]">
													<div class="text-[#ccc]">{String(call.tool_name || call.tool || 'tool')}</div>
													<div class="text-[#555]">{fmtDate(call.started_at || call.created_at)}</div>
													<div class="text-[#666]">Duration: {fmtMs(call.duration_ms)}</div>
													{#if call.error}
														<div class="text-red-400 mt-0.5">{compact(call.error, 120)}</div>
													{/if}
												</div>
											{/each}
										</div>
									{/if}
								</div>

								<div class="border border-[#1a1a1a] p-2 bg-[#050505]">
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Full Container Record</div>
									<pre class="mt-2 max-h-[220px] overflow-auto bg-black border border-[#1a1a1a] p-2 text-[10px] text-[#888] whitespace-pre-wrap break-words">{formatValue(selectedTask)}</pre>
								</div>
							{/if}
					</div>
				{/if}
			</aside>
			</div>
		{/if}
	</section>
</div>
