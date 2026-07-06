<script lang="ts">
	/**
	 * Renderer for `task_approval` / `code_change` payloads
	 * (brain.py _queue task approvals):
	 *   { task_id, task_display_id, agent_id, task_type, strategy_id,
	 *     assigned_by?, priority? }
	 * The linked_task enrichment (title/status) rides on the approval row.
	 */
	import type { ApprovalRecord } from '$lib/api/forven';

	export let approval: ApprovalRecord;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: task = approval.linked_task ?? null;
	$: taskDisplayId = str(payload.task_display_id) || task?.display_id || (payload.task_id ? `Task #${payload.task_id}` : '');
	$: agentId = str(payload.agent_id) || str(task?.agent_id);
	$: taskType = str(payload.task_type) || str(task?.type);
	$: strategyId = str(payload.strategy_id) || str(task?.strategy_id);
	$: assignedBy = str(payload.assigned_by);

	function str(value: unknown): string {
		return typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value);
	}
</script>

<div class="card">
	<div class="row">
		{#if taskDisplayId}
			<a class="task-link" href={'/tasks/' + encodeURIComponent(taskDisplayId) + '?returnTo=' + encodeURIComponent('/approval')}>{taskDisplayId}</a>
		{:else}
			<span class="task-link task-link--dead">No task reference</span>
		{/if}
		{#if taskType}<span class="tag">{taskType}</span>{/if}
		{#if agentId}<span class="tag" title="Assigned agent">{agentId}</span>{/if}
		{#if assignedBy}<span class="tag tag--muted" title="Assigned by">by {assignedBy}</span>{/if}
		{#if strategyId}
			<a class="tag tag--strategy" href={'/lab/strategy/' + encodeURIComponent(strategyId)}>{strategyId}</a>
		{/if}
	</div>
	{#if task?.title || task?.description}
		<p class="task-title">{task?.title || task?.description}</p>
	{/if}
	<details class="raw">
		<summary>Raw payload</summary>
		<pre>{JSON.stringify(payload, null, 2)}</pre>
	</details>
</div>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.75rem;
		border: 1px solid #1f1f1f;
		background: #050505;
	}

	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.task-link {
		font-family: ui-monospace, monospace;
		font-size: 0.85rem;
		font-weight: 600;
		color: #fff;
	}

	.task-link:hover {
		text-decoration: underline;
		color: #7dd3fc;
	}

	.task-link--dead {
		color: #666;
	}

	.tag {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.12em;
		padding: 0.15rem 0.45rem;
		border: 1px solid #2a2a2a;
		color: #aaa;
	}

	.tag--muted {
		color: #666;
	}

	.tag--strategy {
		font-family: ui-monospace, monospace;
		text-transform: none;
		letter-spacing: 0;
		color: #7dd3fc;
		border-color: rgba(125, 211, 252, 0.3);
	}

	.tag--strategy:hover {
		text-decoration: underline;
	}

	.task-title {
		margin: 0;
		font-size: 0.8rem;
		color: #ccc;
	}

	.raw summary {
		font-size: 0.6875rem;
		color: #666;
		cursor: pointer;
		text-transform: uppercase;
		letter-spacing: 0.12em;
	}

	.raw pre {
		margin-top: 0.4rem;
		max-height: 200px;
		overflow: auto;
		border: 1px solid #1f1f1f;
		background: #000;
		padding: 0.5rem;
		font-size: 0.6875rem;
		color: #888;
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
