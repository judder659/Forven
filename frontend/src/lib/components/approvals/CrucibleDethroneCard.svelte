<script lang="ts">
	/**
	 * Renderer for `crucible_dethrone` (crucibles.request_dethrone_approval):
	 *   { current_protection_status, current_crucible_status,
	 *     initial_viability_evidence_id, current_verdict_memo,
	 *     new_evidence: {...}, recommended_action, intent_history?: [...] }
	 */
	import type { ApprovalRecord } from '$lib/api/forven';

	export let approval: ApprovalRecord;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: memo = str(payload.current_verdict_memo);
	$: newEvidence = isRecord(payload.new_evidence) ? (payload.new_evidence as Record<string, unknown>) : null;
	$: intentHistory = Array.isArray(payload.intent_history) ? (payload.intent_history as Array<Record<string, unknown>>) : [];

	function str(value: unknown): string {
		return typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value);
	}

	function isRecord(value: unknown): value is Record<string, unknown> {
		return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
	}

	function pretty(value: unknown): string {
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}
</script>

<div class="card">
	<div class="row">
		<span class="chip" title="Crucible id">{approval.target_id || '(unknown crucible)'}</span>
		{#if str(payload.current_crucible_status)}<span class="chip chip--status">{str(payload.current_crucible_status)}</span>{/if}
		{#if str(payload.current_protection_status)}<span class="chip chip--protect">{str(payload.current_protection_status)}</span>{/if}
		{#if str(payload.recommended_action)}<span class="chip chip--action">{str(payload.recommended_action)}</span>{/if}
	</div>

	{#if memo}
		<section class="section">
			<h4>Current verdict memo</h4>
			<p class="memo">{memo}</p>
		</section>
	{/if}

	{#if newEvidence && Object.keys(newEvidence).length > 0}
		<section class="section">
			<h4>New evidence (vs initial viability {str(payload.initial_viability_evidence_id) || '—'})</h4>
			<pre class="evidence">{pretty(newEvidence)}</pre>
		</section>
	{/if}

	{#if intentHistory.length > 0}
		<section class="section">
			<h4>Prior dethrone intents ({intentHistory.length})</h4>
			<ul class="intent-list">
				{#each intentHistory as intent}
					<li>{str(intent.recommended_action) || 'intent'} → {str(intent.requested_status) || '?'}</li>
				{/each}
			</ul>
		</section>
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

	.row {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
	}

	.chip {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.12em;
		padding: 0.2rem 0.5rem;
		border: 1px solid #2a2a2a;
		color: #ccc;
		font-family: ui-monospace, monospace;
	}

	.chip--status {
		color: #6ee7b7;
		border-color: rgba(52, 211, 153, 0.35);
	}

	.chip--protect {
		color: #fde68a;
		border-color: rgba(250, 204, 21, 0.35);
	}

	.chip--action {
		color: #fca5a5;
		border-color: rgba(248, 113, 113, 0.35);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.section h4 {
		margin: 0;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.memo {
		margin: 0;
		font-size: 0.8rem;
		color: #ccc;
		white-space: pre-wrap;
	}

	.evidence {
		margin: 0;
		max-height: 160px;
		overflow: auto;
		border: 1px solid #1f1f1f;
		background: #000;
		padding: 0.5rem;
		font-size: 0.6875rem;
		color: #a7f3d0;
		white-space: pre-wrap;
		word-break: break-word;
	}

	.intent-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.75rem;
		color: #888;
		font-family: ui-monospace, monospace;
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
