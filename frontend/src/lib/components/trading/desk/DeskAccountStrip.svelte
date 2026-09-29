<script lang="ts">
	/**
	 * Live: the real Hyperliquid account (equity, today, open P&L, realized, drawdown,
	 * risk in use, margin). Paper: the simulated books, one per strategy; paper
	 * dollars are never added to live dollars and are never called equity.
	 */
	import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import type { DeskMode } from '$lib/api/desk';
	import type { PaperTradingSession } from '$lib/api/paper';
	import { ago, fmtPct, fmtUsd, num, toneClass } from '$lib/utils/tradingDesk/format';

	export let mode: DeskMode;
	export let dashboard: ForvenDashboardResponse | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let fleet: LiveFleet | null = null;
	export let sessions: PaperTradingSession[] = [];
	export let openPnl = 0;
	export let openLegs = 0;
	export let openLong = 0;
	export let openShort = 0;
	export let now = Date.now();

	const label = 'font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3';
	const tile = 'grid min-w-0 content-start gap-1 border-b border-r border-sc-line px-3.5 py-2.5';
	const big = 'whitespace-nowrap text-[20px] font-medium tracking-tight';

	$: realized = fleet?.realized ?? null;
	$: account = dashboard?.account ?? null;
	$: equity = num(account?.accountValue);
	$: dayStart = num(dashboard?.daily_risk?.start_equity);
	$: today = equity !== null && dayStart ? equity - dayStart : null;
	$: todayPct = today !== null && dayStart ? (today / dayStart) * 100 : null;
	$: dayLimitPct = (num(risk?.limits?.daily_loss_limit) ?? 0.05) * 100;
	$: lossUsed = todayPct !== null && todayPct < 0 ? Math.abs(todayPct) : 0;
	$: ddPct = (num(dashboard?.risk?.drawdown_pct) ?? 0) * 100;
	$: ddLimitPct = (num(risk?.limits?.max_drawdown) ?? 0.3) * 100;
	$: budget = risk?.portfolio_budget_live ?? null;
	$: riskUsed = num(budget?.total_open_risk_usd) ?? 0;
	$: riskLimit = num(budget?.total_open_risk_limit_usd);
	$: books = budget?.per_book ?? {};

	$: bookCount = sessions.length;
	$: bookTotal = sessions.reduce((sum, session) => sum + (num(session.capital) ?? 0), 0);
	$: bookStart = sessions.reduce((sum, session) => sum + (num(session.initial_capital) ?? 10_000), 0);
	$: bookChange = bookTotal - bookStart;
	$: paperRisk = risk?.portfolio_paper ?? null;
	$: paperGroups = Object.values(paperRisk?.groups ?? {});
	$: paperGrossLong = paperGroups.reduce((sum, group) => sum + (num(group.gross_long) ?? 0), 0);
	$: paperGrossShort = paperGroups.reduce((sum, group) => sum + (num(group.gross_short) ?? 0), 0);
	$: sortedBooks = [...sessions].sort((a, b) => (num(a.capital) ?? 0) - (num(b.capital) ?? 0));

	function meterTone(fraction: number): string {
		return fraction >= 0.8 ? 'bg-[#e5574f]' : fraction >= 0.5 ? 'bg-[#e7b24a]' : 'bg-sc-ink2';
	}
</script>

{#snippet realizedBlock()}
	<div class="grid grid-cols-3 justify-start gap-x-3 text-[12px]">
		{#each ['7d', '30d', 'all'] as key}
			<span class="text-[11px] text-sc-ink3">{key === 'all' ? 'All time' : key}</span>
		{/each}
		{#each ['7d', '30d', 'all'] as key}
			{@const win = realized?.[key as '7d' | '30d' | 'all']}
			<span class={`whitespace-nowrap font-plex-mono text-[13px] font-medium ${toneClass(win?.net_pnl_usd)}`}>{fmtUsd(win?.net_pnl_usd, { signed: true })}</span>
		{/each}
		{#each ['7d', '30d', 'all'] as key}
			<span class="text-[11px] text-sc-ink3">{realized?.[key as '7d' | '30d' | 'all']?.closed ?? 0} trades</span>
		{/each}
	</div>
{/snippet}

<section class="grid grid-cols-2 overflow-hidden rounded-md border border-sc-line bg-sc-panel md:grid-cols-4 2xl:grid-cols-7 [&>*:last-child]:border-r-0" aria-label={mode === 'live' ? 'Account' : 'Paper books'} data-testid="desk-account">
	{#if mode === 'live'}
		<div class={tile}>
			<span class={label}>Equity</span>
			<span class={big}>{fmtUsd(equity)}</span>
			<span class="text-[12px] text-sc-ink3">Long wallet <b class="font-medium text-sc-ink2">{fmtUsd(books.long?.equity_usd)}</b> · short <b class="font-medium text-sc-ink2">{fmtUsd(books.short?.equity_usd)}</b></span>
			<span class="text-[12px] text-sc-ink3">{account?.synced_at ? `Synced ${ago(account.synced_at, now)}` : 'Balance not synced yet'}</span>
		</div>
		<div class={tile}>
			<span class={label}>Today</span>
			<span class={`${big} ${toneClass(today)}`}>{fmtUsd(today, { signed: true })}<small class="ml-1 text-[12px] font-normal text-sc-ink3">{fmtPct(todayPct)}</small></span>
			<span class="text-[12px] text-sc-ink3">Daily loss limit {fmtPct(dayLimitPct, 0, false)} · used <b class="font-medium text-sc-ink2">{fmtPct(lossUsed, 2, false)}</b></span>
			<span class="relative mt-1 h-[5px] overflow-hidden rounded bg-sc-line2"><i class={`absolute inset-y-0 left-0 min-w-[2px] rounded ${meterTone(lossUsed / dayLimitPct)}`} style={`width:${Math.min(100, (lossUsed / dayLimitPct) * 100)}%`}></i></span>
		</div>
		<div class={tile}>
			<span class={label}>Open P&amp;L</span>
			<span class={`${big} ${toneClass(openPnl)}`}>{fmtUsd(openPnl, { signed: true })}</span>
			<span class="text-[12px] text-sc-ink3">{openLegs ? `${openLegs} open position${openLegs === 1 ? '' : 's'} · ${openLong} long, ${openShort} short` : 'No open live positions'}</span>
		</div>
		<div class={tile}>
			<span class={label}>Realized, net of costs</span>
			{@render realizedBlock()}
		</div>
		<div class={tile}>
			<span class={label}>Drawdown</span>
			<span class={big}>{fmtPct(ddPct, 1, false)}</span>
			<span class="text-[12px] text-sc-ink3">From peak <b class="font-medium text-sc-ink2">{fmtUsd(dashboard?.risk?.high_water_mark)}</b> · limit {fmtPct(ddLimitPct, 0, false)}</span>
			<span class="relative mt-1 h-[5px] overflow-hidden rounded bg-sc-line2"><i class={`absolute inset-y-0 left-0 min-w-[2px] rounded ${meterTone(ddPct / ddLimitPct)}`} style={`width:${Math.min(100, (ddPct / ddLimitPct) * 100)}%`}></i></span>
		</div>
		<div class={tile}>
			<span class={label}>Risk in use</span>
			<span class={big}>{fmtUsd(riskUsed)}<small class="ml-1 text-[12px] font-normal text-sc-ink3">of {fmtUsd(riskLimit)}</small></span>
			<span class="text-[12px] text-sc-ink3">Loss if every stop hits · budget {fmtPct(num(budget?.limits_pct?.live_max_total_open_risk_pct) ?? 5, 0, false)} of equity</span>
			<span class="relative mt-1 h-[5px] overflow-hidden rounded bg-sc-line2"><i class={`absolute inset-y-0 left-0 min-w-[2px] rounded ${meterTone(riskLimit ? riskUsed / riskLimit : 0)}`} style={`width:${riskLimit ? Math.min(100, (riskUsed / riskLimit) * 100) : 0}%`}></i></span>
		</div>
		<div class={tile}>
			<span class={label}>Margin</span>
			<span class={big}>{fmtUsd(account?.totalMarginUsed)}<small class="ml-1 text-[12px] font-normal text-sc-ink3">used</small></span>
			<span class="text-[12px] text-sc-ink3">Withdrawable <b class="font-medium text-sc-ink2">{fmtUsd(account?.withdrawable)}</b></span>
			<span class="text-[12px] text-sc-ink3">Wallet cap {fmtPct(num(budget?.limits_pct?.live_max_book_margin_pct) ?? 80, 0, false)} of each wallet</span>
		</div>
	{:else}
		<div class={tile}>
			<span class={label}>Paper books</span>
			<span class={big}>{fmtUsd(bookTotal, { digits: 0 })}</span>
			<span class="text-[12px] text-sc-ink3">{bookCount} books started at {fmtUsd(bookStart, { digits: 0 })} · <b class={`font-medium ${toneClass(bookChange)}`}>{fmtUsd(bookChange, { signed: true, digits: 0 })}</b></span>
		</div>
		<div class={tile}>
			<span class={label}>Open P&amp;L</span>
			<span class={`${big} ${toneClass(openPnl)}`}>{fmtUsd(openPnl, { signed: true })}</span>
			<span class="text-[12px] text-sc-ink3">{openLegs ? `${openLegs} open position${openLegs === 1 ? '' : 's'}` : 'No open paper positions'}</span>
		</div>
		<div class={tile}>
			<span class={label}>Realized, net of costs</span>
			{@render realizedBlock()}
		</div>
		<div class={tile}>
			<span class={label}>Positions</span>
			<span class={big}>{openLegs}</span>
			<span class="text-[12px] text-sc-ink3">{openLong} long · {openShort} short</span>
		</div>
		<div class={tile}>
			<span class={label}>Win rate, 30 days</span>
			<span class={big}>{realized?.['30d']?.win_rate !== null && realized?.['30d']?.win_rate !== undefined ? fmtPct(realized['30d'].win_rate * 100, 0, false) : '—'}</span>
			<span class="text-[12px] text-sc-ink3">{realized?.['30d']?.wins ?? 0} wins · {realized?.['30d']?.losses ?? 0} losses · profit factor {realized?.['30d']?.profit_factor !== null && realized?.['30d']?.profit_factor !== undefined ? realized['30d'].profit_factor.toFixed(2) : '—'}</span>
		</div>
		<div class={tile}>
			<span class={label}>Risk at stops</span>
			<span class={big}>{fmtPct((num(paperRisk?.total_net_risk) ?? 0) * 100, 1, false)}<small class="ml-1 text-[12px] font-normal text-sc-ink3">net</small></span>
			<span class="text-[12px] text-sc-ink3">Long {fmtPct(paperGrossLong * 100, 1, false)} · short {fmtPct(paperGrossShort * 100, 1, false)} of each book, summed</span>
		</div>
		<div class={tile}>
			<span class={label}>Books</span>
			<span class="text-[12px] text-sc-ink3">Best <b class={`font-plex-mono font-medium ${toneClass((num(sortedBooks[sortedBooks.length - 1]?.capital) ?? 0) - (num(sortedBooks[sortedBooks.length - 1]?.initial_capital) ?? 10_000))}`}>{sortedBooks.length ? `${sortedBooks[sortedBooks.length - 1].strategy_id ?? sortedBooks[sortedBooks.length - 1].strategy_name} ${fmtUsd(sortedBooks[sortedBooks.length - 1].capital, { digits: 0 })}` : '—'}</b></span>
			<span class="text-[12px] text-sc-ink3">Worst <b class={`font-plex-mono font-medium ${toneClass((num(sortedBooks[0]?.capital) ?? 0) - (num(sortedBooks[0]?.initial_capital) ?? 10_000))}`}>{sortedBooks.length ? `${sortedBooks[0].strategy_id ?? sortedBooks[0].strategy_name} ${fmtUsd(sortedBooks[0].capital, { digits: 0 })}` : '—'}</b></span>
		</div>
	{/if}
</section>
