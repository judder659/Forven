<script lang="ts">
	/**
	 * Renderer for `regime_champion_promotion` (lab_matrix_engine):
	 *   { program_id, model_version_id, score_version,
	 *     container_payloads: [...],           // heavy — raw toggle only
	 *     champion_changes: [{ regime, old_champion_strategy_id,
	 *                          new_champion_strategy_id, new_champion_score }] }
	 */
	import type { ApprovalRecord } from '$lib/api/forven';

	export let approval: ApprovalRecord;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: changes = Array.isArray(payload.champion_changes)
		? (payload.champion_changes as Array<Record<string, unknown>>)
		: [];

	function str(value: unknown): string {
		return typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value);
	}

	function num(value: unknown): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed.toFixed(3) : '—';
	}
</script>

<div class="card">
	<div class="row">
		{#if str(payload.program_id)}<span class="chip">program {str(payload.program_id)}</span>{/if}
		{#if str(payload.model_version_id)}<span class="chip">model {str(payload.model_version_id)}</span>{/if}
		{#if str(payload.score_version)}<span class="chip chip--muted">score {str(payload.score_version)}</span>{/if}
	</div>

	{#if changes.length > 0}
		<table class="changes">
			<thead>
				<tr><th>Regime</th><th>Current champion</th><th></th><th>Proposed champion</th><th>Score</th></tr>
			</thead>
			<tbody>
				{#each changes as change}
					<tr>
						<td class="regime">{str(change.regime) || '—'}</td>
						<td class="old">{str(change.old_champion_strategy_id) || '(none)'}</td>
						<td class="arrow">→</td>
						<td class="new">
							{#if str(change.new_champion_strategy_id)}
								<a href={'/lab/strategy/' + encodeURIComponent(str(change.new_champion_strategy_id))}>{str(change.new_champion_strategy_id)}</a>
							{:else}
								—
							{/if}
						</td>
						<td class="score">{num(change.new_champion_score)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="fine-print">No champion changes listed in this proposal.</p>
	{/if}

	<details class="raw">
		<summary>Raw payload (incl. container payloads)</summary>
		<pre>{JSON.stringify(payload, null, 2)}</pre>
	</details>
</div>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		padding: 0.75rem;
		border: 1px solid #1f1f1f;
		background: #050505;
	}

	.row {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
	}

	.chip {
		font-size: 0.625rem;
		letter-spacing: 0.08em;
		padding: 0.2rem 0.5rem;
		border: 1px solid #2a2a2a;
		color: #ccc;
		font-family: ui-monospace, monospace;
	}

	.chip--muted {
		color: #666;
	}

	.changes {
		width: 100%;
		font-size: 0.75rem;
		border-collapse: collapse;
	}

	.changes th {
		text-align: left;
		font-weight: 600;
		color: #888;
		padding: 0.3rem 0.5rem;
		border-bottom: 1px solid #1f1f1f;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.14em;
	}

	.changes td {
		padding: 0.35rem 0.5rem;
		border-bottom: 1px solid #141414;
		font-family: ui-monospace, monospace;
	}

	.regime {
		color: #fff;
		font-weight: 600;
	}

	.old {
		color: #888;
	}

	.arrow {
		color: #666;
	}

	.new a {
		color: #6ee7b7;
	}

	.new a:hover {
		text-decoration: underline;
	}

	.score {
		color: #ccc;
	}

	.fine-print {
		margin: 0;
		font-size: 0.6875rem;
		color: #666;
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
