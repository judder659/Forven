<script lang="ts">
	import {
		getContainerAudit,
		getContainerTasks,
		getTaskAudit,
		transitionStage,
		type TaskContainer,
	} from '$lib/api';
	import StrategyExportMenu from '$lib/components/strategy/StrategyExportMenu.svelte';
	import { createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher();

	export let strategyId: string;
	export let displayId: string = strategyId;
	export let strategyName: string = '';
	export let stage: string = 'researching';
	export let showTransitions: boolean = false;
	export let metrics: {
		sharpe?: number | null;
		winRate?: number | null;
		profitFactor?: number | null;
	} = {};
	export let marketPot: string | null = null;

	interface DrawerAuditPayload {
		events: Array<Record<string, unknown>>;
		summary: Array<Record<string, unknown>>;
		merged: Array<Record<string, unknown>>;
	}

	let drawerLoading = false;
	let drawerError = '';
	let selectedAudit: DrawerAuditPayload = { events: [], summary: [], merged: [] };
	let selectedTasks: TaskContainer[] = [];
	let selectedTaskDetail: TaskContainer | null = null;
	let selectedTaskAuditLog: Array<Record<string, unknown>> = [];
	let selectedTaskToolCalls: Array<Record<string, unknown>> = [];
	let taskDetailLoading = false;
	let taskDetailError = '';

	const VALID_TRANSITIONS: Record<string, string[]> = {
		researching: ['developing', 'research_only', 'rejected', 'archived'],
		developing: ['backtesting', 'research_only', 'researching', 'rejected', 'archived'],
		research_only: ['researching', 'rejected', 'archived'],
		backtesting: ['paper_trading', 'research_only', 'developing', 'rejected', 'archived'],
		paper_trading: ['deployed', 'backtesting', 'archived'],
		deployed: ['paper_trading', 'developing', 'archived'],
		archived: ['researching', 'research_only'],
		rejected: ['researching', 'research_only', 'archived'],
	};

	const STAGE_LABELS: Record<string, string> = {
		researching: 'Researching',
		developing: 'Developing',
		research_only: 'Research Only',
		backtesting: 'Gauntlet',
		paper_trading: 'Paper Trading',
		deployed: 'Deployed',
		rejected: 'Rejected',
		archived: 'Graveyard',
	};

	const STAGE_ICONS: Record<string, string> = {
		researching: '🔬',
		developing: '🛠️',
		backtesting: '🧪',
		paper_trading: '🛡️',
		deployed: '🚀',
		rejected: '⛔',
		archived: '🪦',
	};

	function formatMetric(value: number | null | undefined): string {
		if (value === null || value === undefined || Number.isNaN(value)) return '--';
		return value.toFixed(2);
	}

	function formatPercent(value: number | null | undefined): string {
		if (value === null || value === undefined || Number.isNaN(value)) return '--';
		return `${(value * 100).toFixed(1)}%`;
	}

	function formatTimestamp(value: unknown): string {
		if (!value) return '--';
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return String(value);
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function getTaskDisplayId(task: TaskContainer): string {
		const raw = String(task.display_id || '').trim();
		if (raw) return raw;
		const fallbackId = String(task.id || '').trim();
		return fallbackId ? `T${fallbackId.padStart(4, '0')}` : '--';
	}

	function formatTaskValue(value: unknown): string {
		if (value === null || value === undefined) return '--';
		if (typeof value === 'string') {
			const trimmed = value.trim();
			if (!trimmed) return '--';
			if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
				try {
					return JSON.stringify(JSON.parse(trimmed), null, 2);
				} catch {
					return value;
				}
			}
			return value;
		}
		try {
			return JSON.stringify(value, null, 2) || '--';
		} catch {
			return String(value);
		}
	}

	async function loadDetails() {
		drawerError = '';
		drawerLoading = true;
		selectedAudit = { events: [], summary: [], merged: [] };
		selectedTasks = [];
		selectedTaskDetail = null;

		try {
			const [audit, tasks] = await Promise.all([
				getContainerAudit(strategyId),
				getContainerTasks(strategyId)
			]);
			selectedAudit = {
				events: Array.isArray(audit.events) ? audit.events : [],
				summary: Array.isArray(audit.summary) ? audit.summary : [],
				merged: Array.isArray(audit.merged) ? audit.merged : [],
			};
			selectedTasks = tasks;
		} catch (err) {
			drawerError = err instanceof Error ? err.message : 'Failed to load container details';
		} finally {
			drawerLoading = false;
		}
	}

	async function openTaskContainerDetail(task: TaskContainer) {
		selectedTaskDetail = task;
		selectedTaskAuditLog = [];
		selectedTaskToolCalls = [];
		taskDetailError = '';
		taskDetailLoading = true;

		const displayId = String(task.display_id || '').trim();
		if (!displayId) {
			taskDetailLoading = false;
			taskDetailError = 'Task container does not have a display ID.';
			return;
		}

		try {
			const payload = await getTaskAudit(displayId);
			selectedTaskDetail = payload.task;
			selectedTaskAuditLog = Array.isArray(payload.audit_log) ? payload.audit_log : [];
			selectedTaskToolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls : [];
		} catch (err) {
			taskDetailError = err instanceof Error ? err.message : 'Failed to load task container details';
		} finally {
			taskDetailLoading = false;
		}
	}

	async function handleTransition(targetStage: string) {
		try {
			await transitionStage(strategyId, targetStage, `Manual transition from detail drawer`, 'manual');
			dispatch('transition', { strategyId, targetStage });
			void loadDetails();
		} catch (err) {
			drawerError = err instanceof Error ? err.message : `Transition to ${targetStage} failed`;
		}
	}

	function closeDrawer() {
		dispatch('close');
	}

	$: if (strategyId) {
		void loadDetails();
	}
</script>

<div
	class="fixed inset-0 bg-black/80 z-[1000]"
	role="button"
	tabindex="0"
	aria-label="Close detail drawer"
	on:click={closeDrawer}
	on:keydown={(event) => {
		if (event.key === 'Escape' || event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			closeDrawer();
		}
	}}
></div>

<aside class="fixed top-0 right-0 h-full w-full max-w-[520px] bg-[#050505] border-l border-[#222] z-[1001] overflow-y-auto p-5 space-y-4">
	<div class="flex items-start justify-between gap-3">
		<div>
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Strategy container</div>
			<div class="text-2xl font-bold uppercase tracking-widest text-white">{displayId}</div>
			<div class="text-sm text-gray-300 mt-1">{strategyName}</div>
			<div class="text-[11px] text-gray-500 mt-1">
				{STAGE_ICONS[stage] || '📦'} {STAGE_LABELS[stage] || stage}
			</div>
		</div>
		<div class="flex items-center gap-2">
			<StrategyExportMenu strategyId={strategyId} displayId={displayId} name={strategyName} compact />
			<button
				type="button"
				class="terminal-button px-2 py-1 text-[10px]"
				on:click={closeDrawer}
			>
				Close
			</button>
		</div>
	</div>

	{#if showTransitions}
		<div class="border border-[#262626] bg-[#111] rounded p-3">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Manual Transition</div>
			<div class="mt-2 flex flex-wrap gap-2">
				{#each (VALID_TRANSITIONS[stage] || []) as target}
					<button
						type="button"
						class="text-[10px] uppercase tracking-wide border border-[#333] text-[#888] px-2 py-1 hover:border-[#555] hover:text-white transition-colors"
						on:click={() => handleTransition(target)}
					>
						{STAGE_LABELS[target] || target}
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<div class="border border-[#262626] bg-[#111] rounded p-3">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">Metrics</div>
		<div class="mt-2 grid grid-cols-2 gap-y-1 text-xs">
			<div class="text-gray-500">Sharpe</div>
			<div class="text-right text-gray-200">{formatMetric(metrics.sharpe)}</div>
			<div class="text-gray-500">Win Rate</div>
			<div class="text-right text-gray-200">{formatPercent(metrics.winRate)}</div>
			<div class="text-gray-500">Profit Factor</div>
			<div class="text-right text-gray-200">{formatMetric(metrics.profitFactor)}</div>
			<div class="text-gray-500">Market Pot</div>
			<div class="text-right text-gray-200">{marketPot || '--'}</div>
		</div>
	</div>

	<div class="border border-[#262626] bg-[#111] rounded p-3">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">Task Containers</div>
		{#if drawerLoading}
			<div class="mt-2 text-xs text-gray-500 animate-pulse">Loading task links...</div>
		{:else if selectedTasks.length === 0}
			<div class="mt-2 text-xs text-gray-500">No linked tasks.</div>
		{:else}
			<div class="mt-2 space-y-2">
				{#each selectedTasks as task}
					<button
						type="button"
						class={`w-full text-left border p-2 transition-colors ${
							selectedTaskDetail && getTaskDisplayId(selectedTaskDetail) === getTaskDisplayId(task)
								? 'border-[#555] bg-[#111]'
								: 'border-[#222] hover:border-[#555] hover:bg-[#111]'
						}`}
						on:click={() => openTaskContainerDetail(task)}
					>
						<div class="flex items-center justify-between gap-2">
							<span class="text-xs font-bold text-yellow-300">{getTaskDisplayId(task)}</span>
							<span class="text-[10px] text-gray-500">{task.status}</span>
						</div>
						<div class="text-[11px] text-gray-300 mt-1">{task.title}</div>
						<div class="text-[10px] text-gray-500 mt-1">{task.agent_id}</div>
					</button>
				{/each}
			</div>
		{/if}
	</div>

	{#if selectedTaskDetail}
		<div class="border border-[#262626] bg-[#111] rounded p-3 space-y-3">
			<div class="flex items-center justify-between gap-2">
				<div class="text-[10px] uppercase tracking-wider text-gray-500">Task Container Detail</div>
				<div class="text-[10px] text-[#aaa] font-bold">{getTaskDisplayId(selectedTaskDetail)}</div>
			</div>

			<div class="grid grid-cols-2 gap-y-1 text-xs">
				<div class="text-gray-500">Status</div>
				<div class="text-right text-gray-200">{String(selectedTaskDetail.status || '--')}</div>
				<div class="text-gray-500">Agent</div>
				<div class="text-right text-gray-200">{String(selectedTaskDetail.agent_id || '--')}</div>
				<div class="text-gray-500">Strategy</div>
				<div class="text-right text-gray-200">{String(selectedTaskDetail.strategy_id || '--')}</div>
				<div class="text-gray-500">Created</div>
				<div class="text-right text-gray-200">{formatTimestamp(selectedTaskDetail.created_at)}</div>
				<div class="text-gray-500">Started</div>
				<div class="text-right text-gray-200">{formatTimestamp(selectedTaskDetail.started_at)}</div>
				<div class="text-gray-500">Completed</div>
				<div class="text-right text-gray-200">{formatTimestamp(selectedTaskDetail.completed_at)}</div>
			</div>

			{#if selectedTaskDetail.title}
				<div>
					<div class="text-[10px] uppercase tracking-wider text-gray-500">Title</div>
					<div class="text-xs text-gray-200 mt-1">{String(selectedTaskDetail.title)}</div>
				</div>
			{/if}

			{#if selectedTaskDetail.description}
				<div>
					<div class="text-[10px] uppercase tracking-wider text-gray-500">Description</div>
					<div class="text-xs text-gray-300 mt-1 whitespace-pre-wrap">{String(selectedTaskDetail.description)}</div>
				</div>
			{/if}

			{#if taskDetailLoading}
				<div class="text-xs text-gray-500 animate-pulse">Loading full task details...</div>
			{:else}
				{#if taskDetailError}
					<div class="text-xs text-red-400">{taskDetailError}</div>
				{/if}

				<div>
					<div class="flex items-center justify-between">
						<div class="text-[10px] uppercase tracking-wider text-gray-500">Audit Log</div>
						<div class="text-[10px] text-gray-600">{selectedTaskAuditLog.length}</div>
					</div>
					{#if selectedTaskAuditLog.length === 0}
						<div class="mt-1 text-xs text-gray-500">No task audit events.</div>
					{:else}
						<div class="mt-2 space-y-1.5 max-h-[170px] overflow-y-auto">
							{#each selectedTaskAuditLog as auditItem}
								<div class="border border-[#2b2b2b] rounded p-2">
									<div class="text-[10px] text-gray-500">{String(auditItem.event || auditItem.action || '--')}</div>
									<div class="text-[10px] text-gray-600 mt-0.5">{formatTimestamp(auditItem.timestamp || auditItem.created_at)}</div>
									{#if auditItem.reason}
										<div class="text-[10px] text-gray-400 mt-1">{String(auditItem.reason)}</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<div>
					<div class="flex items-center justify-between">
						<div class="text-[10px] uppercase tracking-wider text-gray-500">Tool Calls</div>
						<div class="text-[10px] text-gray-600">{selectedTaskToolCalls.length}</div>
					</div>
					{#if selectedTaskToolCalls.length === 0}
						<div class="mt-1 text-xs text-gray-500">No tool calls recorded.</div>
					{:else}
						<div class="mt-2 space-y-1.5 max-h-[170px] overflow-y-auto">
							{#each selectedTaskToolCalls as toolCall}
								<div class="border border-[#2b2b2b] rounded p-2">
									<div class="text-xs text-gray-200">{String(toolCall.tool_name || toolCall.tool || '--')}</div>
									<div class="text-[10px] text-gray-600 mt-0.5">{formatTimestamp(toolCall.started_at || toolCall.created_at)}</div>
									<div class="text-[10px] text-gray-500 mt-0.5">Duration: {String(toolCall.duration_ms ?? '--')} ms</div>
									{#if toolCall.error}
										<div class="text-[10px] text-red-400 mt-1">{String(toolCall.error)}</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<div>
					<div class="text-[10px] uppercase tracking-wider text-gray-500">Raw Task Payload</div>
					<pre class="mt-1 max-h-[200px] overflow-auto bg-black/40 border border-[#2b2b2b] rounded p-2 text-[10px] text-gray-300 whitespace-pre-wrap break-words">{formatTaskValue(selectedTaskDetail)}</pre>
				</div>
			{/if}
		</div>
	{/if}

	<div class="border border-[#262626] bg-[#111] rounded p-3">
		<div class="text-[10px] uppercase tracking-wider text-gray-500">Audit Timeline</div>
		{#if drawerLoading}
			<div class="mt-2 text-xs text-gray-500 animate-pulse">Loading audit trail...</div>
		{:else if selectedAudit.merged.length === 0}
			<div class="mt-2 text-xs text-gray-500">No audit events yet.</div>
		{:else}
			<div class="mt-2 space-y-2 max-h-[320px] overflow-y-auto">
				{#each selectedAudit.merged as item}
					<div class="border border-[#2b2b2b] rounded p-2">
						<div class="text-[10px] text-gray-500">
							{String(item.timestamp || item.created_at || '--')}
						</div>
						<div class="text-xs text-gray-200 mt-0.5">
							{String(item.event || 'transition')}:
							{String(item.from || item.from_state || '--')} → {String(item.to || item.to_state || '--')}
						</div>
						{#if item.reason}
							<div class="text-[10px] text-gray-500 mt-1">{String(item.reason)}</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
		{#if drawerError}
			<div class="mt-2 text-xs text-red-400">{drawerError}</div>
		{/if}
	</div>
</aside>
