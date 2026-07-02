<script lang="ts">
	/**
	 * Specialized renderer for `approval_type === 'skill_update_proposal'`.
	 *
	 * The approval payload shape is set by the `propose_skill_update` Brain
	 * tool in `forven/agents/tools_brain.py`:
	 *   {
	 *     skill_name,
	 *     description?,
	 *     what_works_additions?: string[],
	 *     what_doesnt_work_additions?: string[],
	 *     metadata_updates?: { ... }   // confidence/sample_size stripped server-side
	 *   }
	 *
	 * The current skill state is fetched on mount so operators can see the
	 * before/after side-by-side without leaving the queue.
	 */
	import { onMount } from 'svelte';
	import { getSkill, type SkillDetail } from '$lib/api/skills';

	export let payload: Record<string, unknown> | null;

	let current: SkillDetail | null = null;
	let loading = false;
	let loadError = '';

	$: skillName =
		payload && typeof payload['skill_name'] === 'string'
			? (payload['skill_name'] as string)
			: null;

	$: descriptionUpdate =
		payload && typeof payload['description'] === 'string'
			? (payload['description'] as string)
			: null;

	$: workAdds = Array.isArray(payload?.['what_works_additions'])
		? (payload!['what_works_additions'] as unknown[]).map(String)
		: [];

	$: failAdds = Array.isArray(payload?.['what_doesnt_work_additions'])
		? (payload!['what_doesnt_work_additions'] as unknown[]).map(String)
		: [];

	$: metaUpdates =
		payload && typeof payload['metadata_updates'] === 'object' && payload['metadata_updates']
			? (payload['metadata_updates'] as Record<string, unknown>)
			: {};

	$: hasChanges =
		descriptionUpdate !== null ||
		workAdds.length > 0 ||
		failAdds.length > 0 ||
		Object.keys(metaUpdates).length > 0;

	async function loadCurrent(name: string): Promise<void> {
		loading = true;
		loadError = '';
		try {
			current = await getSkill(name);
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load current skill state.';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		if (skillName) void loadCurrent(skillName);
	});
</script>

<div class="card">
	<header class="card-header">
		<div>
			<div class="card-eyebrow">Skill Update Proposal</div>
			<div class="card-title">{skillName ?? '(unknown skill)'}</div>
		</div>
		{#if current}
			<div class="version-pill">v{current.version} → v{current.version + 1}</div>
		{:else if loading}
			<div class="version-pill version-pill--loading">loading…</div>
		{/if}
	</header>

	{#if loadError}
		<div class="warn">Could not load current skill state: {loadError}</div>
	{/if}

	{#if !hasChanges}
		<div class="warn">Proposal has no diff. The Brain may have submitted an empty payload.</div>
	{/if}

	{#if descriptionUpdate !== null}
		<section class="section">
			<h4>Description</h4>
			<div class="diff-grid">
				<div class="diff-cell diff-old">
					<div class="diff-label">Current</div>
					<p>{current?.description ?? (loading ? '…' : '—')}</p>
				</div>
				<div class="diff-cell diff-new">
					<div class="diff-label">Proposed</div>
					<p>{descriptionUpdate}</p>
				</div>
			</div>
		</section>
	{/if}

	{#if workAdds.length > 0}
		<section class="section">
			<h4>+ What works (additions)</h4>
			<ul class="add-list add-list--positive">
				{#each workAdds as item}
					<li>{item}</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if failAdds.length > 0}
		<section class="section">
			<h4>+ What doesn't (additions)</h4>
			<ul class="add-list add-list--negative">
				{#each failAdds as item}
					<li>{item}</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if Object.keys(metaUpdates).length > 0}
		<section class="section">
			<h4>Metadata updates</h4>
			<table class="meta-table">
				<thead>
					<tr><th>Key</th><th>Current</th><th>Proposed</th></tr>
				</thead>
				<tbody>
					{#each Object.entries(metaUpdates) as [key, value]}
						<tr>
							<td class="meta-key">{key}</td>
							<td class="meta-old">{current?.metadata?.[key] ?? '—'}</td>
							<td class="meta-new">{String(value)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
			<p class="hint">
				Confidence and sample_size are managed by the outcome-closure pipeline and
				are stripped from proposals automatically — they cannot be updated through
				this approval.
			</p>
		</section>
	{/if}
</div>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		padding: 0.75rem;
		border: 1px solid #1f1f1f;
		background: #050505;
	}

	.card-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 0.75rem;
	}

	.card-eyebrow {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
	}

	.card-title {
		font-family: ui-monospace, monospace;
		font-size: 0.95rem;
		font-weight: 600;
		color: #fff;
		margin-top: 0.125rem;
	}

	.version-pill {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		padding: 0.2rem 0.55rem;
		border: 1px solid #333;
		background: transparent;
		color: #888;
	}

	.version-pill--loading {
		color: #888;
		border-color: #2a2a2a;
		background: transparent;
	}

	.warn {
		font-size: 0.75rem;
		color: #fca5a5;
		border: 1px solid rgba(248, 113, 113, 0.3);
		background: rgba(248, 113, 113, 0.08);
		padding: 0.4rem 0.6rem;
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}

	.section h4 {
		margin: 0;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.diff-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.5rem;
	}

	.diff-cell {
		padding: 0.5rem;
		border: 1px solid #1f1f1f;
		background: #050505;
		font-size: 0.8rem;
		color: #ccc;
	}

	.diff-cell p {
		margin: 0;
		white-space: pre-wrap;
	}

	.diff-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #666;
		margin-bottom: 0.25rem;
	}

	.diff-old {
		border-color: rgba(248, 113, 113, 0.18);
	}

	.diff-new {
		border-color: rgba(52, 211, 153, 0.25);
		background: rgba(52, 211, 153, 0.04);
	}

	.add-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.add-list li {
		padding: 0.4rem 0.55rem;
		font-size: 0.8rem;
		border: 1px solid transparent;
	}

	.add-list--positive li {
		background: rgba(52, 211, 153, 0.06);
		border-color: rgba(52, 211, 153, 0.25);
		color: #a7f3d0;
	}

	.add-list--negative li {
		background: rgba(248, 113, 113, 0.06);
		border-color: rgba(248, 113, 113, 0.25);
		color: #fecaca;
	}

	.meta-table {
		width: 100%;
		font-size: 0.75rem;
		border-collapse: collapse;
	}

	.meta-table th {
		text-align: left;
		font-weight: 600;
		color: #888;
		padding: 0.3rem 0.5rem;
		border-bottom: 1px solid #1f1f1f;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.14em;
	}

	.meta-table td {
		padding: 0.3rem 0.5rem;
		border-bottom: 1px solid #141414;
		color: #ccc;
	}

	.meta-key {
		font-family: ui-monospace, monospace;
		color: #fff;
	}

	.meta-old {
		color: #888;
	}

	.meta-new {
		color: #34d399;
	}

	.hint {
		font-size: 0.6875rem;
		color: #666;
		margin: 0.25rem 0 0;
		line-height: 1.4;
	}
</style>
