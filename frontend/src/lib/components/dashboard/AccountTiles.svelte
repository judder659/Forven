<script lang="ts">
	/**
	 * The live account at a glance: real equity, today's move against the daily
	 * loss limit, drawdown against the max-drawdown limit, open risk to stops, and
	 * realized P&L net of fees and funding.
	 */
	import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import { finite, formatPct, formatUsd, meterTone, pnlTone } from '$lib/utils/liveDashboard';

	export let dashboard: ForvenDashboardResponse | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let fleet: LiveFleet | null = null;

	$: account = dashboard?.account ?? {};
	$: equity = finite(account.accountValue);
	$: realBalance = equity !== null && equity > 0 && String(account.source ?? '').toLowerCase() !== 'paper';
	$: budget = risk?.portfolio_budget_live ?? null;
	$: books = Object.entries(budget?.per_book ?? {})
		.map(([label, book]) => ({ label, equity: finite(book.equity_usd) }))
		.filter((book) => book.equity !== null);

	$: dayStart = finite(dashboard?.daily_risk?.start_equity);
	$: dayNow = finite(dashboard?.daily_risk?.current_equity);
	$: dayPnl = dayStart !== null && dayNow !== null ? dayNow - dayStart : null;
	$: dayPct = dayPnl !== null && dayStart ? dayPnl / dayStart : null;
	$: dailyLimit = finite(risk?.limits?.daily_loss_limit);
	$: dayLossUsed = dayPct !== null && dailyLimit ? Math.max(0, -dayPct) / dailyLimit : null;

	$: hwm = finite(risk?.high_water_mark) ?? finite(dashboard?.risk?.high_water_mark);
	$: drawdown = finite(dashboard?.risk?.drawdown_pct)
		?? (hwm && equity !== null ? Math.max(0, (hwm - equity) / hwm) : null);
	$: maxDrawdown = finite(risk?.limits?.max_drawdown);
	$: drawdownUsed = drawdown !== null && maxDrawdown ? drawdown / maxDrawdown : null;

	$: openRisk = finite(budget?.total_open_risk_usd);
	$: openRiskLimit = finite(budget?.total_open_risk_limit_usd);
	$: openRiskUsed = finite(budget?.total_open_risk_used_frac);

	$: realized = fleet?.realized ?? null;

	function width(fraction: number | null): string {
		if (fraction === null) return '0%';
		return `${Math.min(100, Math.max(0, fraction * 100)).toFixed(1)}%`;
	}
</script>

<div class="grid grid-cols-2 gap-2 lg:grid-cols-5" data-testid="account-tiles">
	<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Live equity</div>
		<div class="mt-1 font-mono text-lg font-bold text-sc-ink">
			{realBalance ? formatUsd(equity) : 'Unavailable'}
		</div>
		<div class="mt-1 text-[10px] text-sc-ink3">
			{#if books.length > 0}
				{books.map((book) => `${book.label} ${formatUsd(book.equity)}`).join(' · ')}
			{:else if realBalance}
				free {formatUsd(finite(account.withdrawable))} · margin {formatUsd(finite(account.totalMarginUsed))}
			{:else}
				No exchange balance reported
			{/if}
		</div>
	</div>

	<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Today (UTC)</div>
		<div class="mt-1 font-mono text-lg font-bold {pnlTone(dayPnl)}">
			{formatUsd(dayPnl, true)}
			<span class="text-xs font-normal">{dayPct !== null ? `(${dayPct >= 0 ? '+' : '−'}${formatPct(Math.abs(dayPct), 2)})` : ''}</span>
		</div>
		<div class="mt-1 h-1 bg-sc-raise" title="Share of the daily loss limit used">
			<div class="h-1 {meterTone(dayLossUsed)}" style="width: {width(dayLossUsed)}"></div>
		</div>
		<div class="mt-1 text-[10px] text-sc-ink3">
			{dailyLimit ? `daily loss limit ${formatPct(dailyLimit, 0)}` : 'daily loss limit —'}
		</div>
	</div>

	<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Drawdown</div>
		<div class="mt-1 font-mono text-lg font-bold text-sc-ink">{formatPct(drawdown, 1)}</div>
		<div class="mt-1 h-1 bg-sc-raise" title="Share of the max-drawdown limit used">
			<div class="h-1 {meterTone(drawdownUsed)}" style="width: {width(drawdownUsed)}"></div>
		</div>
		<div class="mt-1 text-[10px] text-sc-ink3">
			peak {formatUsd(hwm)} · limit {formatPct(maxDrawdown, 0)}
		</div>
	</div>

	<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Open risk to stops</div>
		<div class="mt-1 font-mono text-lg font-bold text-sc-ink">{formatUsd(openRisk)}</div>
		<div class="mt-1 h-1 bg-sc-raise" title="Share of the portfolio risk budget used">
			<div class="h-1 {meterTone(openRiskUsed)}" style="width: {width(openRiskUsed)}"></div>
		</div>
		<div class="mt-1 text-[10px] text-sc-ink3">budget {formatUsd(openRiskLimit)}</div>
	</div>

	<div class="rounded-md col-span-2 border border-sc-line bg-sc-panel px-3 py-2 lg:col-span-1" data-testid="realized-tile">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3" title="Closed live trades, net of recorded fees and funding">
			Realized net · 30d
		</div>
		<div class="mt-1 font-mono text-lg font-bold {pnlTone(realized?.['30d'].net_pnl_usd)}">
			{realized ? formatUsd(realized['30d'].net_pnl_usd, true) : '—'}
		</div>
		{#if realized}
			<div class="mt-1 text-[10px] text-sc-ink3">
				{realized['30d'].closed} trades · win {formatPct(realized['30d'].win_rate, 0)} · PF {realized['30d'].profit_factor?.toFixed(2) ?? '—'}
			</div>
			<div class="text-[10px] text-sc-ink3">
				7d <span class={pnlTone(realized['7d'].net_pnl_usd)}>{formatUsd(realized['7d'].net_pnl_usd, true)}</span>
				· all <span class={pnlTone(realized.all.net_pnl_usd)}>{formatUsd(realized.all.net_pnl_usd, true)}</span>
			</div>
		{/if}
	</div>
</div>
