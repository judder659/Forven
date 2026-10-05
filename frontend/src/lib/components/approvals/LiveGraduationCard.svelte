<script lang="ts">
	/**
	 * Renderer for `strategy_live_graduation_recommendation` (forven/live_graduation.py):
	 *   { recommendation: { strategy_id, display_id, name, symbol, timeframe,
	 *       evidence: { soak_days, closed_paper_trades, paper_pnl_sum, ... },
	 *       risk_multiplier, proposed_arm_usd },
	 *     executes_nothing: true, next_step }
	 *
	 * Approving records intent only. Real capital still goes through the
	 * strategy's own typed GO LIVE step, so the card says so and links there.
	 */
	import type { ApprovalRecord } from '$lib/api/forven';

	export let approval: ApprovalRecord;

	$: payload = (approval.payload ?? {}) as Record<string, unknown>;
	$: rec = (payload.recommendation ?? {}) as Record<string, unknown>;
	$: evidence = (rec.evidence ?? {}) as Record<string, unknown>;
	$: strategyId = String(rec.strategy_id || approval.target_id || '');
	$: label = String(rec.display_id || strategyId || 'strategy');

	function money(value: unknown): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? `$${parsed.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—';
	}

	function num(value: unknown, digits = 1): string {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—';
	}
</script>

<div class="rounded-md border border-sc-line bg-sc-bg/30 p-3 space-y-3 text-xs" data-testid="live-graduation-card">
	<div class="rounded border border-yellow-700/60 bg-yellow-900/15 px-3 py-2 text-yellow-200">
		Approving only records that you agree. Nothing trades yet. To put money behind {label}, open the strategy and use Go live with the proposed cap.
	</div>
	<div class="grid gap-2 sm:grid-cols-3">
		<div><div class="text-sc-ink3 uppercase tracking-wider">Strategy</div><div class="text-sc-ink font-mono">{label}</div><div class="text-sc-ink3">{rec.symbol || '—'} · {rec.timeframe || '—'}</div></div>
		<div><div class="text-sc-ink3 uppercase tracking-wider">Proposed cap</div><div class="text-sc-ink font-semibold">{money(rec.proposed_arm_usd)}</div><div class="text-sc-ink3">allocator × {num(rec.risk_multiplier, 2)}</div></div>
		<div><div class="text-sc-ink3 uppercase tracking-wider">Paper record</div><div class="text-sc-ink">{num(evidence.soak_days)} days · {evidence.closed_paper_trades ?? '—'} trades</div><div class="text-sc-ink3">PnL {money(evidence.paper_pnl_sum)}</div></div>
	</div>
	{#if strategyId}
		<a class="inline-block terminal-button text-[12px] px-3 py-1.5" href={`/lab/strategy/${encodeURIComponent(strategyId)}`}>Open strategy to go live</a>
	{/if}
</div>
