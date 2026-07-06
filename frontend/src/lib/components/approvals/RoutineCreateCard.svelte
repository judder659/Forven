<script lang="ts">
	/**
	 * Renderer for `routine_create` (tools_brain.propose_routine):
	 *   { name, prompt, cron_expr, tools_context, channel, rationale,
	 *     proposed_by }
	 * The cron expression is translated to local-time plain English with the
	 * same util the Routines page uses.
	 */
	import type { ApprovalRecord } from '$lib/api/forven';
	import { describeCronLocal } from '$lib/utils/schedule';

	export let approval: ApprovalRecord;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: cronExpr = str(payload.cron_expr);
	$: schedule = cronExpr ? describeCronLocal(cronExpr) : '';

	function str(value: unknown): string {
		return typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value);
	}
</script>

<div class="card">
	<header class="card-header">
		<div>
			<div class="card-eyebrow">Proposed routine</div>
			<div class="card-title">{str(payload.name) || '(unnamed routine)'}</div>
		</div>
		{#if str(payload.proposed_by)}<span class="chip">by {str(payload.proposed_by)}</span>{/if}
	</header>

	<div class="facts">
		<div class="fact">
			<div class="fact-label">Schedule</div>
			<div class="fact-value">{schedule || '—'}{#if cronExpr}<span class="cron"> ({cronExpr})</span>{/if}</div>
		</div>
		<div class="fact">
			<div class="fact-label">Delivery channel</div>
			<div class="fact-value">{str(payload.channel) || 'default'}</div>
		</div>
	</div>

	{#if str(payload.rationale)}
		<section class="section">
			<h4>Why the Brain wants this</h4>
			<p class="text">{str(payload.rationale)}</p>
		</section>
	{/if}

	{#if str(payload.prompt)}
		<details class="prompt">
			<summary>Routine prompt</summary>
			<pre>{str(payload.prompt)}</pre>
		</details>
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
		gap: 0.6rem;
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
		font-size: 0.95rem;
		font-weight: 600;
		color: #fff;
		margin-top: 0.125rem;
	}

	.chip {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.12em;
		padding: 0.2rem 0.5rem;
		border: 1px solid #2a2a2a;
		color: #888;
		white-space: nowrap;
	}

	.facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.5rem;
	}

	.fact {
		padding: 0.45rem 0.55rem;
		border: 1px solid #1f1f1f;
		background: rgba(0, 0, 0, 0.3);
	}

	.fact-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.14em;
		color: #666;
	}

	.fact-value {
		margin-top: 0.15rem;
		font-size: 0.8rem;
		color: #fff;
	}

	.cron {
		color: #666;
		font-family: ui-monospace, monospace;
		font-size: 0.7rem;
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	.section h4 {
		margin: 0;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.text {
		margin: 0;
		font-size: 0.8rem;
		color: #ccc;
		white-space: pre-wrap;
	}

	.prompt summary,
	.raw summary {
		font-size: 0.6875rem;
		color: #666;
		cursor: pointer;
		text-transform: uppercase;
		letter-spacing: 0.12em;
	}

	.prompt pre,
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
