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
		researching: ['developing', 'rejected', 'archived'],
		developing: ['backtesting', 'researching', 'rejected', 'archived'],
		backtesting: ['paper_trading', 'developing', 'rejected', 'archived'],
		paper_trading: ['deployed', 'backtesting', 'archived'],
		deployed: ['paper_trading', 'developing', 'archived'],
		archived: ['researching'],
		rejected: ['researching', 'archived'],
	};

	const STAGE_LABELS: Record<string, string> = {
		researching: 'Researching',
		developing: 'Developing',
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
	class="fixed inset-0 bg-sc-bg/80 z-[1000]"
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

<aside class="fixed top-0 right-0 h-full w-full max-w-[520px] bg-sc-panel border-l border-sc-line z-[1001] overflow-y-auto p-5 space-y-4">
	<div class="flex items-start justify-between gap-3">
		<div>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Strategy container</div>
			<div class="text-2xl font-bold uppercase tracking-widest text-sc-ink">{displayId}</div>
			<div class="text-sm text-sc-ink2 mt-1">{strategyName}</div>
			<div class="text-[11px] text-sc-ink3 mt-1">
				{STAGE_ICONS[stage] || '📦'} {STAGE_LABELS[stage] || stage}
			</div>
		</div>
		<div class="flex items-center gap-2">
			<StrategyExportMenu strategyId={strategyId} displayId={displayId} name={strategyName} compact />
			<button
				type="button"
				class="terminal-button px-2 py-1 text-[12px]"
				on:click={closeDrawer}
			>
				Close
			</button>
		</div>
	</div>

	{#if showTransitions}
		<div class="border border-sc-line bg-sc-panel2 rounded p-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Manual Transition</div>
			<div class="mt-2 flex flex-wrap gap-2">
				{#each (VALID_TRANSITIONS[stage] || []) as target}
					<button
						type="button"
						class="rounded-md text-[12px] border border-sc-line2 text-sc-ink2 px-2 py-1 hover:border-sc-line2 hover:text-sc-ink transition-colors"
						on:click={() => handleTransition(target)}
					>
						{STAGE_LABELS[target] || target}
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<div class="border border-sc-line bg-sc-panel2 rounded p-3">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Metrics</div>
		<div class="mt-2 grid grid-cols-2 gap-y-1 text-xs">
			<div class="text-sc-ink3">Sharpe</div>
			<div class="text-right text-sc-ink">{formatMetric(metrics.sharpe)}</div>
			<div class="text-sc-ink3">Win Rate</div>
			<div class="text-right text-sc-ink">{formatPercent(metrics.winRate)}</div>
			<div class="text-sc-ink3">Profit Factor</div>
			<div class="text-right text-sc-ink">{formatMetric(metrics.profitFactor)}</div>
			<div class="text-sc-ink3">Market Pot</div>
			<div class="text-right text-sc-ink">{marketPot || '--'}</div>
		</div>
	</div>

	<div class="border border-sc-line bg-sc-panel2 rounded p-3">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Task Containers</div>
		{#if drawerLoading}
			<div class="mt-2 text-xs text-sc-ink3 animate-pulse">Loading task links...</div>
		{:else if selectedTasks.length === 0}
			<div class="mt-2 text-xs text-sc-ink3">No linked tasks.</div>
		{:else}
			<div class="mt-2 space-y-2">
				{#each selectedTasks as task}
					<button
						type="button"
						class={`rounded-md w-full text-left border p-2 transition-colors ${
							selectedTaskDetail && getTaskDisplayId(selectedTaskDetail) === getTaskDisplayId(task)
								? 'border-sc-line2 bg-sc-panel2'
								: 'border-sc-line hover:border-sc-line2 hover:bg-sc-panel2'
						}`}
						on:click={() => openTaskContainerDetail(task)}
					>
						<div class="flex items-center justify-between gap-2">
							<span class="text-xs font-bold text-yellow-300">{getTaskDisplayId(task)}</span>
							<span class="text-[10px] text-sc-ink3">{task.status}</span>
						</div>
						<div class="text-[11px] text-sc-ink2 mt-1">{task.title}</div>
						<div class="text-[10px] text-sc-ink3 mt-1">{task.agent_id}</div>
					</button>
				{/each}
			</div>
		{/if}
	</div>

	{#if selectedTaskDetail}
		<div class="border border-sc-line bg-sc-panel2 rounded p-3 space-y-3">
			<div class="flex items-center justify-between gap-2">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Task Container Detail</div>
				<div class="text-[10px] text-sc-ink2 font-bold">{getTaskDisplayId(selectedTaskDetail)}</div>
			</div>

			<div class="grid grid-cols-2 gap-y-1 text-xs">
				<div class="text-sc-ink3">Status</div>
				<div class="text-right text-sc-ink">{String(selectedTaskDetail.status || '--')}</div>
				<div class="text-sc-ink3">Agent</div>
				<div class="text-right text-sc-ink">{String(selectedTaskDetail.agent_id || '--')}</div>
				<div class="text-sc-ink3">Strategy</div>
				<div class="text-right text-sc-ink">{String(selectedTaskDetail.strategy_id || '--')}</div>
				<div class="text-sc-ink3">Created</div>
				<div class="text-right text-sc-ink">{formatTimestamp(selectedTaskDetail.created_at)}</div>
				<div class="text-sc-ink3">Started</div>
				<div class="text-right text-sc-ink">{formatTimestamp(selectedTaskDetail.started_at)}</div>
				<div class="text-sc-ink3">Completed</div>
				<div class="text-right text-sc-ink">{formatTimestamp(selectedTaskDetail.completed_at)}</div>
			</div>

			{#if selectedTaskDetail.title}
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Title</div>
					<div class="text-xs text-sc-ink mt-1">{String(selectedTaskDetail.title)}</div>
				</div>
			{/if}

			{#if selectedTaskDetail.description}
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Description</div>
					<div class="text-xs text-sc-ink2 mt-1 whitespace-pre-wrap">{String(selectedTaskDetail.description)}</div>
				</div>
			{/if}

			{#if taskDetailLoading}
				<div class="text-xs text-sc-ink3 animate-pulse">Loading full task details...</div>
			{:else}
				{#if taskDetailError}
					<div class="text-xs text-red-400">{taskDetailError}</div>
				{/if}

				<div>
					<div class="flex items-center justify-between">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Audit Log</div>
						<div class="text-[10px] text-sc-ink3">{selectedTaskAuditLog.length}</div>
					</div>
					{#if selectedTaskAuditLog.length === 0}
						<div class="mt-1 text-xs text-sc-ink3">No task audit events.</div>
					{:else}
						<div class="mt-2 space-y-1.5 max-h-[170px] overflow-y-auto">
							{#each selectedTaskAuditLog as auditItem}
								<div class="border border-sc-line2 rounded p-2">
									<div class="text-[10px] text-sc-ink3">{String(auditItem.event || auditItem.action || '--')}</div>
									<div class="text-[10px] text-sc-ink3 mt-0.5">{formatTimestamp(auditItem.timestamp || auditItem.created_at)}</div>
									{#if auditItem.reason}
										<div class="text-[10px] text-sc-ink2 mt-1">{String(auditItem.reason)}</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<div>
					<div class="flex items-center justify-between">
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Tool Calls</div>
						<div class="text-[10px] text-sc-ink3">{selectedTaskToolCalls.length}</div>
					</div>
					{#if selectedTaskToolCalls.length === 0}
						<div class="mt-1 text-xs text-sc-ink3">No tool calls recorded.</div>
					{:else}
						<div class="mt-2 space-y-1.5 max-h-[170px] overflow-y-auto">
							{#each selectedTaskToolCalls as toolCall}
								<div class="border border-sc-line2 rounded p-2">
									<div class="text-xs text-sc-ink">{String(toolCall.tool_name || toolCall.tool || '--')}</div>
									<div class="text-[10px] text-sc-ink3 mt-0.5">{formatTimestamp(toolCall.started_at || toolCall.created_at)}</div>
									<div class="text-[10px] text-sc-ink3 mt-0.5">Duration: {String(toolCall.duration_ms ?? '--')} ms</div>
									{#if toolCall.error}
										<div class="text-[10px] text-red-400 mt-1">{String(toolCall.error)}</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Raw Task Payload</div>
					<pre class="mt-1 max-h-[200px] overflow-auto bg-sc-bg/40 border border-sc-line2 rounded p-2 text-[10px] text-sc-ink2 whitespace-pre-wrap break-words">{formatTaskValue(selectedTaskDetail)}</pre>
				</div>
			{/if}
		</div>
	{/if}

	<div class="border border-sc-line bg-sc-panel2 rounded p-3">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Audit Timeline</div>
		{#if drawerLoading}
			<div class="mt-2 text-xs text-sc-ink3 animate-pulse">Loading audit trail...</div>
		{:else if selectedAudit.merged.length === 0}
			<div class="mt-2 text-xs text-sc-ink3">No audit events yet.</div>
		{:else}
			<div class="mt-2 space-y-2 max-h-[320px] overflow-y-auto">
				{#each selectedAudit.merged as item}
					<div class="border border-sc-line2 rounded p-2">
						<div class="text-[10px] text-sc-ink3">
							{String(item.timestamp || item.created_at || '--')}
						</div>
						<div class="text-xs text-sc-ink mt-0.5">
							{String(item.event || 'transition')}:
							{String(item.from || item.from_state || '--')} → {String(item.to || item.to_state || '--')}
						</div>
						{#if item.reason}
							<div class="text-[10px] text-sc-ink3 mt-1">{String(item.reason)}</div>
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
