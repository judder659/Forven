<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		getBrainDecision,
		type BrainDecisionDetail,
		type BrainDecisionLinkedTask
	} from '$lib/api/brain';

	export let decisionId: number;
	export let onClose: () => void;

	let detail: BrainDecisionDetail | null = null;
	let loading = true;
	let error = '';
	let decisionExpanded = false;
	let copied = false;
	let copyTimer: ReturnType<typeof setTimeout> | null = null;

	function deepLink(id: number): string {
		// The drawer's open state round-trips through ?tab=decisions&d_id=<id>,
		// so a shareable link just pins those params onto the current URL.
		const url = new URL(window.location.href);
		url.searchParams.set('tab', 'decisions');
		url.searchParams.set('d_id', String(id));
		return url.toString();
	}

	async function copyLink() {
		const link = deepLink(decisionId);
		try {
			await navigator.clipboard.writeText(link);
		} catch {
			// Fallback for environments without async clipboard access.
			const ta = document.createElement('textarea');
			ta.value = link;
			ta.style.position = 'fixed';
			ta.style.opacity = '0';
			document.body.appendChild(ta);
			ta.select();
			try {
				document.execCommand('copy');
			} catch {
				/* best-effort */
			}
			document.body.removeChild(ta);
		}
		copied = true;
		if (copyTimer) clearTimeout(copyTimer);
		copyTimer = setTimeout(() => {
			copied = false;
		}, 2000);
	}

	async function load(id: number) {
		loading = true;
		error = '';
		try {
			detail = await getBrainDecision(id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			detail = null;
		} finally {
			loading = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function taskLink(task: BrainDecisionLinkedTask): string {
		// The /tasks/[id] route resolves by display_id (LOWER(display_id)); a numeric id 404s.
		return `/tasks/${task.display_id ?? task.id}`;
	}

	function strategyLink(task: BrainDecisionLinkedTask): string | null {
		return task.strategy_id ? `/lab/strategy/${task.strategy_id}` : null;
	}

	function handleEsc(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	$: void load(decisionId);

	onMount(() => {
		document.addEventListener('keydown', handleEsc);
	});

	onDestroy(() => {
		document.removeEventListener('keydown', handleEsc);
		if (copyTimer) clearTimeout(copyTimer);
	});
</script>

<button type="button" class="overlay" on:click={onClose} aria-label="Close drawer"></button>

<div class="drawer" role="dialog" aria-modal="true" aria-label="Decision detail" tabindex="-1">
	<header>
		<div class="title">
			<span class="kicker">Decision</span>
			<h2>#{decisionId}</h2>
		</div>
		<div class="header-actions">
			<button
				type="button"
				class="copy-link"
				on:click={copyLink}
				title="Copy a shareable link to this decision"
			>
				{copied ? 'Copied' : 'Copy link'}
			</button>
			<button type="button" class="close" on:click={onClose} aria-label="Close">×</button>
		</div>
	</header>

	<div class="body">
		{#if loading}
			<div class="loading">Loading…</div>
		{:else if error}
			<div class="error-banner">
				<strong>Failed to load decision:</strong>
				{error}
			</div>
		{:else if detail}
			<dl>
				<dt>Cycle</dt>
				<dd>{detail.cycle_id ?? '—'}</dd>
				<dt>Created</dt>
				<dd>{formatTimestamp(detail.created_at)}</dd>
				<dt>Outcome</dt>
				<dd>{detail.outcome_observed ?? 'pending'}{detail.outcome_at ? ` @ ${formatTimestamp(detail.outcome_at)}` : ''}</dd>
				<dt>Prompt hash</dt>
				<dd class="mono">{detail.prompt_hash ?? '—'}</dd>
			</dl>

			<section>
				<h3>Situation</h3>
				<pre class="block">{detail.situation_summary ?? ''}</pre>
			</section>

			<section>
				<h3>
					<button
						type="button"
						class="collapse"
						on:click={() => (decisionExpanded = !decisionExpanded)}
					>
						Decision JSON {decisionExpanded ? '▾' : '▸'}
					</button>
				</h3>
				{#if decisionExpanded}
					<pre class="block">{JSON.stringify(detail.decision, null, 2)}</pre>
				{/if}
			</section>

			{#if detail.action_taken}
				<section>
					<h3>Action taken</h3>
					<pre class="block">{detail.action_taken}</pre>
				</section>
			{/if}

			<section>
				<h3>Linked tasks ({detail.linked_tasks.length})</h3>
				{#if detail.linked_tasks.length === 0}
					<p class="empty">No agent_tasks rows linked to this decision.</p>
				{:else}
					<ul class="tasks">
						{#each detail.linked_tasks as task (task.id)}
							<li>
								<div class="task-head">
									<a href={taskLink(task)}>#{task.id}</a>
									<span class="chip">{task.type ?? '—'}</span>
									<span class="chip">{task.status ?? '—'}</span>
									{#if task.strategy_id}
										<a class="strategy" href={strategyLink(task)}>
											{task.strategy_id}
										</a>
									{/if}
								</div>
								<div class="task-meta">
									<span>{task.title ?? ''}</span>
									{#if task.cost_usd != null}
										<span>${task.cost_usd.toFixed(4)}</span>
									{/if}
									{#if task.provider}
										<span>{task.provider}:{task.model_id ?? '—'}</span>
									{/if}
									<span>{formatTimestamp(task.created_at)}</span>
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/if}
	</div>
</div>

<style>
	.overlay {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.8);
		border: none;
		padding: 0;
		cursor: pointer;
		z-index: 1000;
	}

	.drawer {
		position: fixed;
		top: 0;
		right: 0;
		width: min(640px, 100%);
		height: 100vh;
		background: #0a0a0a;
		border-left: 1px solid #222;
		display: flex;
		flex-direction: column;
		z-index: 1001;
		box-shadow: none;
	}

	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1rem 1.25rem;
		border-bottom: 1px solid #222;
	}

	.kicker {
		display: block;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: #888;
	}

	header h2 {
		margin: 0;
		font-size: 1.25rem;
		font-weight: 600;
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.copy-link {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #888;
		font-size: 0.8125rem;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
		border-radius: 0;
		white-space: nowrap;
	}

	.copy-link:hover {
		background: #222;
		color: #fff;
	}

	.close {
		background: transparent;
		border: none;
		color: #aaa;
		font-size: 1.5rem;
		line-height: 1;
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		border-radius: 0;
	}

	.close:hover {
		background: #1a1a1a;
		color: #fff;
	}

	.body {
		flex: 1;
		overflow-y: auto;
		padding: 1rem 1.25rem;
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
	}

	.loading,
	.empty {
		color: #888;
		font-size: 0.875rem;
	}

	.error-banner {
		background: #2a1010;
		border: 1px solid #5a2020;
		color: #f8c0c0;
		padding: 0.625rem 0.875rem;
		border-radius: 0;
		font-size: 0.875rem;
	}

	dl {
		display: grid;
		grid-template-columns: 120px 1fr;
		gap: 0.4rem 1rem;
		margin: 0;
		font-size: 0.875rem;
	}

	dt {
		color: #888;
		text-transform: uppercase;
		font-size: 0.75rem;
		letter-spacing: 0.04em;
	}

	dd {
		margin: 0;
		color: #ddd;
	}

	dd.mono {
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		font-size: 0.8125rem;
		word-break: break-all;
	}

	section h3 {
		margin: 0 0 0.5rem;
		font-size: 0.9375rem;
		color: #ddd;
	}

	.collapse {
		background: transparent;
		border: none;
		color: #ddd;
		cursor: pointer;
		padding: 0;
		font-size: inherit;
		font-weight: inherit;
	}

	.collapse:hover {
		color: #fff;
	}

	.block {
		background: #050505;
		border: 1px solid #1a1a1a;
		border-radius: 0;
		padding: 0.625rem;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		font-size: 0.8125rem;
		color: #ccc;
		white-space: pre-wrap;
		word-wrap: break-word;
		max-height: 360px;
		overflow-y: auto;
	}

	.tasks {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.tasks li {
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.5rem 0.75rem;
	}

	.task-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		margin-bottom: 0.375rem;
	}

	.task-head a {
		color: #888;
		text-decoration: none;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
	}

	.task-head a:hover {
		text-decoration: underline;
	}

	.task-head .strategy {
		margin-left: auto;
		color: #fde68a;
	}

	.chip {
		padding: 0.125rem 0.5rem;
		border-radius: 0;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		background: #1a1a1a;
		color: #aaa;
		border: 1px solid #222;
	}

	.task-meta {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
		font-size: 0.75rem;
		color: #888;
	}
</style>
