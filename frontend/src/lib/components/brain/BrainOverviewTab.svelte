<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getBrainOverview,
		type BrainActivityRow,
		type BrainAttentionItem,
		type BrainOverview,
		type BrainOverviewTask,
		type BrainRepeatedFailure
	} from '$lib/api/brain';

	let overview: BrainOverview | null = null;
	let loading = true;
	let error = '';
	let refreshing = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function load(silent = false): Promise<void> {
		if (silent) refreshing = true;
		else loading = true;
		try {
			overview = await getBrainOverview();
			error = '';
		} catch (err) {
			// A failed background poll should not blow away the rendered view;
			// only surface load errors for explicit (non-silent) loads.
			if (!silent) {
				error = err instanceof Error ? err.message : 'Failed to load Brain overview.';
			}
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '-';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function memoryLines(body: string): string[] {
		return body
			.split('\n')
			.map((line) => line.trim())
			.filter(Boolean)
			.slice(0, 10);
	}

	function taskHref(task: BrainOverviewTask): string {
		// The /tasks/[id] route resolves by display_id (LOWER(display_id)); a numeric id 404s.
		return `/tasks/${task.display_id ?? task.id}`;
	}

	function strategyHref(task: BrainOverviewTask): string | null {
		return task.strategy_id ? `/lab/strategy/${task.strategy_id}` : null;
	}

	function taskLabel(task: BrainOverviewTask): string {
		return task.display_id || `T${task.id}`;
	}

	function attentionClass(item: BrainAttentionItem): string {
		return `attention ${item.severity || 'info'}`;
	}

	function statusClass(status: string | null | undefined): string {
		const normalized = (status || 'pending').toLowerCase();
		if (normalized === 'failed') return 'status failed';
		if (normalized === 'blocked' || normalized === 'paused_manual') return 'status blocked';
		if (normalized === 'running') return 'status running';
		if (normalized === 'done' || normalized === 'reviewed') return 'status done';
		return 'status pending';
	}

	function shortMessage(row: BrainActivityRow): string {
		const max = 180;
		return row.message.length > max ? `${row.message.slice(0, max)}...` : row.message;
	}

	function failureLabel(row: BrainRepeatedFailure): string {
		return row.type || 'unknown';
	}

	onMount(() => {
		void load(false);
		// Keep attention signals / active-task counts live while the tab is
		// visible (mirrors the visibility-gated poll in BrainMemoryTab).
		pollTimer = setInterval(() => {
			if (document.visibilityState === 'visible') {
				void load(true);
			}
		}, 30000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
	});
</script>

<div class="overview-tab">
	{#if loading}
		<div class="empty">Loading Brain overview...</div>
	{:else if error}
		<div class="error-banner">
			<strong>Failed to load overview:</strong>
			{error}
			<button type="button" on:click={() => load(false)}>Retry</button>
		</div>
	{:else if overview}
		<section class="hero-panel">
			<div>
				<p class="kicker">Current Brain State</p>
				<h2>Autonomy state, actions, blockers, and memory.</h2>
				<p class="meta">
					Memory updated by {overview.memory.updated_by ?? '-'} at
					{formatTimestamp(overview.memory.updated_at)}
				</p>
			</div>
			<button type="button" on:click={() => load(true)} disabled={refreshing}>
				{refreshing ? 'Refreshing...' : 'Refresh'}
			</button>
		</section>

		<section class="stats-grid" aria-label="Brain overview stats">
			<a class="stat" href="/brain?tab=memory">
				<span>Memory</span>
				<strong>{overview.memory.char_count}/{overview.memory.cap}</strong>
			</a>
			<a class="stat" href="/agents?tab=tasks">
				<span>Active Work</span>
				<strong>{overview.stats.active_tasks}</strong>
			</a>
			<a class="stat" href="/approval">
				<span>Approvals</span>
				<strong>{overview.stats.pending_approvals}</strong>
			</a>
			<a class="stat" href="/brain?tab=decisions">
				<span>Decisions</span>
				<strong>{overview.stats.decisions}</strong>
			</a>
		</section>

		<div class="main-grid">
			<section class="panel attention-panel">
				<header>
					<h3>Needs Attention</h3>
					<span>{overview.attention.length}</span>
				</header>
				{#if overview.attention.length === 0}
					<p class="empty-inline">No attention signals right now.</p>
				{:else}
					<ul class="attention-list">
						{#each overview.attention as item}
							<li class={attentionClass(item)}>
								<div>
									<strong>{item.title}</strong>
									<p>{item.detail}</p>
								</div>
								<span>{item.kind}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="panel memory-panel">
				<header>
					<h3>Memory Snapshot</h3>
					<a href="/brain?tab=memory">Edit</a>
				</header>
				{#if overview.memory.body}
					<ul class="memory-lines">
						{#each memoryLines(overview.memory.body) as line}
							<li>{line}</li>
						{/each}
					</ul>
				{:else}
					<p class="empty-inline">Brain memory is empty.</p>
				{/if}
			</section>
		</div>

		<section class="panel">
			<header>
				<h3>Active Brain-Assigned Work</h3>
				<a href="/agents?tab=tasks">Open Tasks</a>
			</header>
			{#if overview.active_tasks.length === 0}
				<p class="empty-inline">No pending, running, blocked, or failed Brain-assigned tasks.</p>
			{:else}
				<ul class="task-list">
					{#each overview.active_tasks as task (task.id)}
						<li>
							<div class="task-head">
								<a href={taskHref(task)}>{taskLabel(task)}</a>
								<span class={statusClass(task.status)}>{task.status ?? 'pending'}</span>
								<span class="type">{task.type ?? '-'}</span>
								<span class="when">{formatTimestamp(task.created_at)}</span>
							</div>
							<div class="task-title">{task.title ?? 'Untitled task'}</div>
							<div class="task-meta">
								<span>{task.agent_id ?? '-'}</span>
								{#if task.strategy_id}
									<a href={strategyHref(task)}>{task.strategy_id}</a>
								{/if}
								{#if task.error}
									<span class="task-error">{task.error}</span>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<div class="main-grid">
			<section class="panel">
				<header>
					<h3>Recent Brain Activity</h3>
					<span>{overview.activity.length}</span>
				</header>
				{#if overview.activity.length === 0}
					<p class="empty-inline">No Brain activity rows found.</p>
				{:else}
					<ul class="activity-list">
						{#each overview.activity.slice(0, 12) as row (row.id)}
							<li>
								<div class="activity-head">
									<span class="level">{row.level}</span>
									<span>{row.source ?? '-'}</span>
									<span class="when">{formatTimestamp(row.created_at)}</span>
								</div>
								<p>{shortMessage(row)}</p>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="panel">
				<header>
					<h3>Repeated Failures</h3>
					<span>{overview.repeated_failures.length}</span>
				</header>
				{#if overview.repeated_failures.length === 0}
					<p class="empty-inline">No task types with 3 or more Brain-assigned failures.</p>
				{:else}
					<ul class="failure-list">
						{#each overview.repeated_failures as failure}
							<li>
								<span>{failureLabel(failure)}</span>
								<strong>{failure.count}</strong>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.overview-tab {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.hero-panel {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 1rem;
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
	}

	.kicker {
		margin: 0 0 0.25rem;
		color: #888;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}

	.hero-panel h2 {
		margin: 0;
		font-size: 1.125rem;
		font-weight: 600;
		color: #fff;
	}

	.meta {
		margin: 0.35rem 0 0;
		color: #888;
		font-size: 0.8125rem;
	}

	button,
	.panel a,
	.stat {
		color: #888;
		text-decoration: none;
	}

	button {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ddd;
		padding: 0.5rem 0.875rem;
		border-radius: 0;
		cursor: pointer;
		font-size: 0.875rem;
	}

	button:hover:not(:disabled) {
		background: #222;
		color: #fff;
	}

	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.stats-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 0.75rem;
	}

	.stat {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.875rem;
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
	}

	.stat:hover {
		border-color: #555;
		background: #111;
	}

	.stat span {
		color: #888;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.stat strong {
		color: #fff;
		font-size: 1.35rem;
		font-weight: 600;
	}

	.main-grid {
		display: grid;
		grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
		gap: 1rem;
	}

	.panel {
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.875rem;
	}

	.panel header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.75rem;
		margin-bottom: 0.75rem;
	}

	.panel h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: #ddd;
	}

	.panel header span,
	.panel header a {
		color: #888;
		font-size: 0.75rem;
	}

	.panel header a:hover {
		color: #888;
		text-decoration: underline;
	}

	.empty,
	.empty-inline {
		color: #888;
		font-size: 0.875rem;
	}

	.empty {
		padding: 1rem;
		text-align: center;
		border: 1px dashed #222;
		border-radius: 0;
	}

	.error-banner {
		background: #2a1010;
		border: 1px solid #5a2020;
		color: #f8c0c0;
		padding: 0.625rem 0.875rem;
		border-radius: 0;
		font-size: 0.875rem;
	}

	.error-banner button {
		margin-left: 0.5rem;
		color: #f8c0c0;
	}

	.attention-list,
	.memory-lines,
	.task-list,
	.activity-list,
	.failure-list {
		list-style: none;
		padding: 0;
		margin: 0;
	}

	.attention-list,
	.task-list,
	.activity-list,
	.failure-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.attention {
		display: flex;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.625rem 0.75rem;
		border-radius: 0;
		border: 1px solid #222;
		background: #050505;
	}

	.attention.critical {
		border-color: #7f1d1d;
		background: #241010;
	}

	.attention.warning {
		border-color: #60430f;
		background: #211b0d;
	}

	.attention.info {
		border-color: #1a1a1a;
		background: #050505;
	}

	.attention strong {
		display: block;
		color: #f5f5f5;
		font-size: 0.875rem;
	}

	.attention p {
		margin: 0.25rem 0 0;
		color: #aaa;
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.attention > span {
		color: #777;
		font-size: 0.75rem;
		text-transform: uppercase;
		white-space: nowrap;
	}

	.memory-lines {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.memory-lines li {
		color: #ccc;
		font-size: 0.875rem;
		line-height: 1.45;
		word-break: break-word;
	}

	.task-list li,
	.activity-list li {
		padding: 0.625rem 0.75rem;
		background: #050505;
		border: 1px solid #1a1a1a;
		border-radius: 0;
	}

	.task-head,
	.activity-head,
	.task-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		font-size: 0.75rem;
		color: #888;
	}

	.task-head a {
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		color: #888;
	}

	.status,
	.type,
	.level {
		padding: 0.0625rem 0.4rem;
		border-radius: 0;
		background: #1f1f1f;
		color: #aaa;
		text-transform: uppercase;
		font-size: 0.6875rem;
		font-weight: 600;
	}

	.status.failed {
		background: #5a1a1a;
		color: #fca5a5;
	}

	.status.blocked {
		background: #4a3814;
		color: #fde68a;
	}

	.status.running {
		background: #0f3d28;
		color: #34d399;
	}

	.status.done {
		background: #14532d;
		color: #86efac;
	}

	.when {
		margin-left: auto;
		color: #666;
	}

	.task-title {
		margin-top: 0.35rem;
		color: #e5e5e5;
		font-size: 0.875rem;
		line-height: 1.4;
	}

	.task-meta {
		margin-top: 0.35rem;
	}

	.task-error {
		color: #fca5a5;
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.activity-list p {
		margin: 0.35rem 0 0;
		color: #ccc;
		font-size: 0.875rem;
		line-height: 1.45;
	}

	.failure-list li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.625rem 0.75rem;
		background: #050505;
		border: 1px solid #1a1a1a;
		border-radius: 0;
	}

	.failure-list span {
		color: #ddd;
		font-size: 0.875rem;
	}

	.failure-list strong {
		color: #fca5a5;
		font-size: 1rem;
	}

	@media (max-width: 860px) {
		.hero-panel {
			align-items: flex-start;
			flex-direction: column;
		}

		.main-grid {
			grid-template-columns: 1fr;
		}

		.when {
			margin-left: 0;
		}
	}
</style>
