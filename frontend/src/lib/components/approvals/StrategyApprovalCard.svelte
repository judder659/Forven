<script lang="ts">
	/**
	 * Renderer for `strategy_dethrone_recommendation` and
	 * `strategy_promotion_approval` — the two approval types that dominate the
	 * queue. Everything renders defensively: approvals created before the
	 * 2026-07-06 payload enrichment carry no evidence/strategy_snapshot.
	 *
	 * Payload shape (brain._queue_dethrone_approval / _queue_promotion_approval):
	 *   { strategy_id, current_stage, recommended_action,
	 *     recommended_target_stage, trigger_actor, trigger_reason,
	 *     evidence?: {...}, strategy_snapshot?: StrategySnapshot }
	 * plus the policy variants: repeated-failure (gate, reason_code,
	 * failure_count, threshold) and superior_challenger (challenger_id,
	 * challenger_sharpe, incumbent_sharpe).
	 */
	import {
		getApprovalContext,
		type ApprovalRecord,
		type ApprovalStrategyContext,
		type StrategySnapshot,
	} from '$lib/api/forven';

	export let approval: ApprovalRecord;

	const STAGE_RANK: Record<string, number> = {
		quick_screen: 0,
		research_only: 0,
		gauntlet: 1,
		paper: 2,
		live_graduated: 3,
	};
	const TERMINAL_STAGES = new Set(['archived', 'rejected', 'backtest_failed']);

	let context: ApprovalStrategyContext | null = null;
	let contextLoading = false;
	let contextError = '';
	let historyOpen = false;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: strategyId = str(payload.strategy_id) || str(approval.target_id);
	$: fromStage = str(payload.current_stage);
	$: toStage = str(payload.recommended_target_stage) || str(approval.requested_status);
	$: triggerActor = str(payload.trigger_actor) || str(approval.actor);
	$: triggerReason = str(payload.trigger_reason);
	$: snapshot = (payload.strategy_snapshot ?? null) as StrategySnapshot | null;
	$: evidence = isRecord(payload.evidence) ? (payload.evidence as Record<string, unknown>) : null;
	$: isChallenger = str(payload.trigger) === 'superior_challenger';
	$: isRepeatedFailure = payload.failure_count !== undefined && payload.gate !== undefined;
	$: isDecay = str(evidence?.trigger) === 'decay_tracker';
	$: strategyLabel = snapshot?.display_name || snapshot?.name || strategyId || '(unknown strategy)';
	$: genericEvidence = evidence && !isDecay
		? Object.entries(evidence).filter(([key]) => key !== 'trigger')
		: [];

	function str(value: unknown): string {
		return typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value);
	}

	function isRecord(value: unknown): value is Record<string, unknown> {
		return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
	}

	function num(value: unknown, digits = 2): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—';
	}

	function pct(value: unknown): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(1)}%` : '—';
	}

	function intOrDash(value: unknown): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? String(Math.round(parsed)) : '—';
	}

	function fmtDate(value: unknown): string {
		if (!value) return '—';
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return '—';
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function stageTone(stage: string, isTarget: boolean): string {
		const key = stage.toLowerCase();
		if (TERMINAL_STAGES.has(key)) return 'chip chip--terminal';
		if (!isTarget) return 'chip chip--current';
		const fromRank = STAGE_RANK[fromStage.toLowerCase()];
		const toRank = STAGE_RANK[key];
		if (fromRank !== undefined && toRank !== undefined && toRank < fromRank) return 'chip chip--down';
		return 'chip chip--up';
	}

	async function toggleHistory() {
		historyOpen = !historyOpen;
		if (!historyOpen || context || contextLoading) return;
		contextLoading = true;
		contextError = '';
		try {
			const response = await getApprovalContext(approval.id);
			context = response.strategy_context ?? null;
			if (!context) contextError = 'No strategy context available.';
		} catch (err) {
			contextError = err instanceof Error ? err.message : 'Failed to load strategy context.';
		} finally {
			contextLoading = false;
		}
	}
</script>

<div class="card">
	<header class="card-header">
		<div class="min-w-0">
			<div class="card-eyebrow">{approval.approval_type === 'strategy_promotion_approval' ? 'Promotion request' : 'Dethrone recommendation'}</div>
			{#if strategyId}
				<a class="card-title" href={'/lab/strategy/' + encodeURIComponent(strategyId)}>{strategyLabel}</a>
			{:else}
				<span class="card-title">{strategyLabel}</span>
			{/if}
			{#if snapshot?.symbol || snapshot?.timeframe}
				<span class="market">{snapshot?.symbol ?? ''}{snapshot?.timeframe ? ` · ${snapshot.timeframe}` : ''}</span>
			{/if}
		</div>
		<div class="stage-flow">
			{#if fromStage}<span class={stageTone(fromStage, false)}>{fromStage}</span>{/if}
			<span class="arrow">→</span>
			{#if toStage}<span class={stageTone(toStage, true)}>{toStage}</span>{:else}<span class="chip chip--current">?</span>{/if}
		</div>
	</header>

	<div class="trigger-row">
		<span class="trigger-actor" title="Who attempted this transition">{triggerActor || 'unknown actor'}</span>
		<span class="trigger-reason">{triggerReason || approval.reason || 'No reason recorded.'}</span>
	</div>

	{#if isDecay && evidence}
		<section class="section">
			<h4>Decay evidence</h4>
			<div class="metric-grid">
				<div class="metric"><div class="metric-label">Baseline Sharpe</div><div class="metric-value">{num(evidence.baseline_sharpe)}</div></div>
				<div class="metric"><div class="metric-label">Live Sharpe ({intOrDash(evidence.window_hours)}h)</div><div class="metric-value metric-value--bad">{num(evidence.live_sharpe_72h)}</div></div>
				<div class="metric"><div class="metric-label">Degradation</div><div class="metric-value metric-value--bad">{pct(evidence.degradation)}</div></div>
				<div class="metric"><div class="metric-label">Trades in window</div><div class="metric-value">{intOrDash(evidence.trade_count_72h)}</div></div>
			</div>
		</section>
	{:else if isChallenger}
		<section class="section">
			<h4>Challenger comparison (backtest Sharpe)</h4>
			<div class="metric-grid metric-grid--two">
				<div class="metric metric--good">
					<div class="metric-label">Challenger {str(payload.challenger_id)}</div>
					<div class="metric-value">{num(payload.challenger_sharpe)}</div>
				</div>
				<div class="metric">
					<div class="metric-label">Incumbent {strategyId}</div>
					<div class="metric-value">{num(payload.incumbent_sharpe)}</div>
				</div>
			</div>
		</section>
	{:else if isRepeatedFailure}
		<section class="section">
			<h4>Repeated gate failures</h4>
			<div class="metric-grid">
				<div class="metric"><div class="metric-label">Gate</div><div class="metric-value metric-value--mono">{str(payload.gate) || '—'}</div></div>
				<div class="metric"><div class="metric-label">Reason</div><div class="metric-value metric-value--mono">{str(payload.reason_code) || '—'}</div></div>
				<div class="metric"><div class="metric-label">Failures</div><div class="metric-value metric-value--bad">{intOrDash(payload.failure_count)} / {intOrDash(payload.threshold)}</div></div>
			</div>
			{#if str(payload.reason_text)}<p class="fine-print">{str(payload.reason_text)}</p>{/if}
		</section>
	{:else if genericEvidence.length > 0}
		<section class="section">
			<h4>Evidence</h4>
			<div class="metric-grid">
				{#each genericEvidence as [key, value]}
					<div class="metric"><div class="metric-label">{key.replaceAll('_', ' ')}</div><div class="metric-value">{typeof value === 'number' ? num(value) : str(value) || '—'}</div></div>
				{/each}
			</div>
		</section>
	{/if}

	{#if snapshot}
		<section class="section">
			<h4>Strategy at recommendation time</h4>
			<div class="metric-grid">
				<div class="metric"><div class="metric-label">Backtest Sharpe</div><div class="metric-value">{num(snapshot.backtest?.sharpe)}</div></div>
				<div class="metric"><div class="metric-label">Return</div><div class="metric-value">{num(snapshot.backtest?.total_return)}</div></div>
				<div class="metric"><div class="metric-label">Max DD</div><div class="metric-value">{num(snapshot.backtest?.max_drawdown)}</div></div>
				<div class="metric"><div class="metric-label">BT trades</div><div class="metric-value">{intOrDash(snapshot.backtest?.trades)}</div></div>
			</div>
			<div class="metric-grid">
				<div class="metric"><div class="metric-label">Fwd trades ({intOrDash(snapshot.forward?.window_days)}d)</div><div class="metric-value">{intOrDash(snapshot.forward?.closed_trades)}</div></div>
				<div class="metric"><div class="metric-label">Fwd wins</div><div class="metric-value">{intOrDash(snapshot.forward?.wins)}</div></div>
				<div class="metric"><div class="metric-label">Fwd PnL % sum</div><div class="metric-value {Number(snapshot.forward?.realized_pnl_pct_sum) < 0 ? 'metric-value--bad' : ''}">{num(snapshot.forward?.realized_pnl_pct_sum)}</div></div>
				<div class="metric"><div class="metric-label">In stage since</div><div class="metric-value metric-value--small">{fmtDate(snapshot.stage_changed_at)}</div></div>
			</div>
		</section>
	{:else}
		<p class="fine-print">No snapshot captured with this approval (created before payload enrichment) — open the strategy page for current numbers.</p>
	{/if}

	<div class="footer-row">
		<button type="button" class="history-toggle" on:click={() => void toggleHistory()}>
			{historyOpen ? 'Hide history' : 'Decision history'}
		</button>
		<details class="raw">
			<summary>Raw payload</summary>
			<pre>{JSON.stringify(payload, null, 2)}</pre>
		</details>
	</div>

	{#if historyOpen}
		<section class="section history">
			{#if contextLoading}
				<div class="fine-print">Loading decision history…</div>
			{:else if contextError}
				<div class="fine-print">{contextError}</div>
			{:else if context}
				<div class="metric-grid">
					<div class="metric"><div class="metric-label">Denied before</div><div class="metric-value">{intOrDash(context.dethrone_history?.denied)}</div></div>
					<div class="metric"><div class="metric-label">Approved before</div><div class="metric-value">{intOrDash(context.dethrone_history?.approved)}</div></div>
					<div class="metric"><div class="metric-label">Last denied</div><div class="metric-value metric-value--small">{fmtDate(context.dethrone_history?.last_denied_at)}</div></div>
					<div class="metric">
						<div class="metric-label">Deny cooldown</div>
						<div class="metric-value metric-value--small {context.cooldown?.active ? 'metric-value--warn' : ''}">
							{context.cooldown?.active ? `until ${fmtDate(context.cooldown?.until)} (deny #${context.cooldown?.deny_count ?? 0})` : 'inactive'}
						</div>
					</div>
				</div>
			{/if}
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
		flex-wrap: wrap;
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
		display: inline-block;
	}

	a.card-title:hover {
		text-decoration: underline;
		color: #7dd3fc;
	}

	.market {
		margin-left: 0.5rem;
		font-size: 0.7rem;
		color: #888;
	}

	.stage-flow {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}

	.arrow {
		color: #666;
		font-size: 0.8rem;
	}

	.chip {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.14em;
		padding: 0.2rem 0.55rem;
		border: 1px solid #333;
		color: #ccc;
	}

	.chip--current {
		border-color: #333;
		color: #ccc;
	}

	.chip--up {
		border-color: rgba(52, 211, 153, 0.4);
		color: #6ee7b7;
		background: rgba(52, 211, 153, 0.06);
	}

	.chip--down {
		border-color: rgba(250, 204, 21, 0.4);
		color: #fde68a;
		background: rgba(250, 204, 21, 0.06);
	}

	.chip--terminal {
		border-color: rgba(248, 113, 113, 0.4);
		color: #fca5a5;
		background: rgba(248, 113, 113, 0.06);
	}

	.trigger-row {
		display: flex;
		gap: 0.5rem;
		align-items: baseline;
		font-size: 0.8rem;
		flex-wrap: wrap;
	}

	.trigger-actor {
		font-family: ui-monospace, monospace;
		font-size: 0.7rem;
		padding: 0.1rem 0.4rem;
		border: 1px solid #2a2a2a;
		color: #888;
		white-space: nowrap;
	}

	.trigger-reason {
		color: #ccc;
		min-width: 0;
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

	.metric-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
		gap: 0.5rem;
	}

	.metric-grid--two {
		grid-template-columns: 1fr 1fr;
	}

	.metric {
		padding: 0.45rem 0.55rem;
		border: 1px solid #1f1f1f;
		background: rgba(0, 0, 0, 0.3);
	}

	.metric--good {
		border-color: rgba(52, 211, 153, 0.25);
		background: rgba(52, 211, 153, 0.04);
	}

	.metric-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.14em;
		color: #666;
	}

	.metric-value {
		margin-top: 0.15rem;
		font-size: 0.9rem;
		font-weight: 600;
		color: #fff;
	}

	.metric-value--bad {
		color: #fca5a5;
	}

	.metric-value--warn {
		color: #fde68a;
	}

	.metric-value--mono {
		font-family: ui-monospace, monospace;
		font-size: 0.75rem;
	}

	.metric-value--small {
		font-size: 0.7rem;
		font-weight: 400;
		color: #ccc;
	}

	.fine-print {
		margin: 0;
		font-size: 0.6875rem;
		color: #666;
		line-height: 1.4;
	}

	.footer-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
	}

	.history-toggle {
		font-size: 0.6875rem;
		text-transform: uppercase;
		letter-spacing: 0.12em;
		color: #888;
		background: transparent;
		border: 1px solid #2a2a2a;
		padding: 0.25rem 0.6rem;
		cursor: pointer;
	}

	.history-toggle:hover {
		color: #fff;
		border-color: #555;
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

	.history {
		border-top: 1px solid #1a1a1a;
		padding-top: 0.6rem;
	}
</style>
