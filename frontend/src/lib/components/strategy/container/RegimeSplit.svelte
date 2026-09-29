<script lang="ts">
	import { REGIME_LABELS } from '$lib/utils/strategyContainer/metrics';
	import { fmtPct, isNum, toNumber } from '$lib/utils/strategyContainer/format';
	import SignedBars from './SignedBars.svelte';

	/** Average return per trade by the regime at entry, from the regime-split payload. */
	export let payload: Record<string, unknown> | null = null;

	type Bag = Record<string, unknown>;
	$: regimes = (Array.isArray(payload?.regimes) ? (payload?.regimes as Bag[]) : []).filter((row) => row && typeof row === 'object');
	$: dropped = new Set((Array.isArray(payload?.dropped_low_trade_regimes) ? (payload?.dropped_low_trade_regimes as unknown[]) : []).map(String));
	$: minTrades = toNumber(payload?.regime_min_trades);
	$: rows = regimes.map((row) => {
		const name = String(row.name ?? '');
		const trades = toNumber(row.trade_count);
		const win = toNumber(row.win_rate);
		return {
			label: REGIME_LABELS[name] ?? name,
			sub: `${trades ?? '—'} trades · win ${fmtPct(win, 0, false)}${dropped.has(name) ? ' · too few, not scored' : ''}`,
			value: toNumber(row.avg_return_pct),
			title: `${REGIME_LABELS[name] ?? name}: total ${fmtPct(toNumber(row.total_return_pct))}, best ${fmtPct(toNumber(row.best_return_pct))}, worst ${fmtPct(toNumber(row.worst_return_pct))}`,
		};
	});
	$: share = toNumber(payload?.profitable_regime_share);
	$: threshold = toNumber(payload?.verdict_threshold);
	$: weakest = String(payload?.weakest_regime ?? '');
</script>

{#if rows.length}
	<div class="grid gap-3" data-testid="regime-split">
		<SignedBars {rows} format={(value) => `${fmtPct(value, 2)}/trade`} />
		<p class="m-0 text-[12px] leading-relaxed text-sc-ink3">
			{#if isNum(share)}Profitable in {fmtPct(share * 100, 0, false)} of scored regimes{isNum(threshold) ? ` (gate ≥ ${fmtPct(threshold * 100, 0, false)})` : ''}.{/if}
			{#if weakest} Weakest: {(REGIME_LABELS[weakest] ?? weakest).toLowerCase()}.{/if}
			{#if dropped.size} Regimes with fewer than {minTrades ?? 5} trades are shown but not scored.{/if}
		</p>
	</div>
{:else}
	<div class="text-[12px] text-sc-ink3">Regime split has not run.</div>
{/if}
