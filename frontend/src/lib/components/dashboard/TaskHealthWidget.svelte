<script lang="ts">
	/**
	 * Dashboard widget: task / queue health.
	 *
	 * Pulls GET /api/health (runtime summary) and shows agent+brain queue
	 * depth, running count, and stale pending/running counts. Stale > 0 is
	 * highlighted as a warning — stale tasks are how the pipeline silently
	 * stalls.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getTaskHealth, type TaskHealth } from '$lib/api/dashboard';

	const REFRESH_MS = 60_000;

	let health: TaskHealth | null = null;
	let loading = true;
	let error = '';
	let timer: ReturnType<typeof setInterval> | null = null;

	$: queues = health?.queues ?? null;
	$: queued = (queues?.agent_pending ?? 0) + (queues?.brain_pending ?? 0);
	$: running = (queues?.agent_running ?? 0) + (queues?.brain_running ?? 0);
	$: stalePending = (queues?.agent_stale_pending ?? 0) + (queues?.brain_stale_pending ?? 0);
	$: staleRunning = (queues?.agent_stale_running ?? 0) + (queues?.brain_stale_running ?? 0);
	$: degraded = (health?.status ?? '') !== 'ok';

	async function load(): Promise<void> {
		try {
			health = await getTaskHealth();
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load task health.';
		} finally {
			loading = false;
		}
	}

	function tick() {
		if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
		void load();
	}

	onMount(() => {
		void load();
		timer = setInterval(tick, REFRESH_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<section class="widget" data-testid="task-health-widget">
	<header class="widget-head">
		<div class="title-block">
			<div class="eyebrow">Task Health</div>
			<div class="subtitle">Agent + brain queues</div>
		</div>
		{#if health}
			<span
				class="status-pill"
				class:status-pill--warn={degraded}
				data-testid="task-health-status"
			>
				{health.status}
			</span>
		{/if}
	</header>

	{#if error}
		<div class="error" data-testid="task-health-error">{error}</div>
	{:else if loading && !health}
		<div class="empty">Loading…</div>
	{:else if queues}
		<div class="stats">
			<div class="stat" title={`agent ${queues.agent_pending} / brain ${queues.brain_pending}`}>
				<span class="stat-label">Queued</span>
				<span class="stat-value" data-testid="task-health-queued">{queued}</span>
			</div>
			<div class="stat" title={`agent ${queues.agent_running} / brain ${queues.brain_running}`}>
				<span class="stat-label">Running</span>
				<span class="stat-value" data-testid="task-health-running">{running}</span>
			</div>
			<div
				class="stat"
				title={`agent ${queues.agent_stale_pending} / brain ${queues.brain_stale_pending}`}
			>
				<span class="stat-label">Stale pend</span>
				<span
					class="stat-value"
					class:stat-value--warn={stalePending > 0}
					data-testid="task-health-stale-pending"
				>
					{stalePending}
				</span>
			</div>
			<div
				class="stat"
				title={`agent ${queues.agent_stale_running} / brain ${queues.brain_stale_running}`}
			>
				<span class="stat-label">Stale run</span>
				<span
					class="stat-value"
					class:stat-value--warn={staleRunning > 0}
					data-testid="task-health-stale-running"
				>
					{staleRunning}
				</span>
			</div>
		</div>
		{#if health && health.issues.length > 0}
			<div
				class="issues"
				title={health.issues.join('\n')}
				data-testid="task-health-issues"
			>
				{health.issues[0]}{health.issues.length > 1 ? ` (+${health.issues.length - 1})` : ''}
			</div>
		{/if}
	{/if}
</section>

<style>
	.widget {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.6rem 0.75rem;
		border: 1px solid #222;
		background: #050505;
		min-width: 220px;
	}

	.widget-head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 0.5rem;
	}

	.title-block {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.eyebrow {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.subtitle {
		font-size: 0.6875rem;
		color: #666;
	}

	.status-pill {
		font-size: 0.625rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		padding: 0.2rem 0.5rem;
		border: 1px solid #064e3b;
		background: rgba(52, 211, 153, 0.08);
		color: #34d399;
	}

	.status-pill--warn {
		border-color: #713f12;
		background: rgba(250, 204, 21, 0.1);
		color: #facc15;
	}

	.stats {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 0.4rem;
	}

	.stat {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		padding: 0.3rem 0.4rem;
		background: #050505;
		border: 1px solid #1a1a1a;
	}

	.stat-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: #666;
		white-space: nowrap;
	}

	.stat-value {
		font-family: ui-monospace, monospace;
		font-size: 0.875rem;
		font-weight: 700;
		color: #fff;
		font-variant-numeric: tabular-nums;
	}

	.stat-value--warn {
		color: #facc15;
	}

	.issues {
		font-size: 0.625rem;
		color: #facc15;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.empty {
		font-size: 0.75rem;
		color: #666;
		padding: 0.5rem;
		text-align: center;
	}

	.error {
		font-size: 0.6875rem;
		color: #f87171;
		padding: 0.4rem 0.5rem;
		border: 1px solid #7f1d1d;
		background: rgba(239, 68, 68, 0.05);
	}
</style>
