<script lang="ts">
	/** Live: wallets, the risk budget, regime stances and coin/side overlaps. Paper: the books and regimes. */
	import type { ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import type { DeskMode } from '$lib/api/desk';
	import { humanRegime } from '$lib/utils/tradingDesk/describe';
	import { cap1, fmtPct, fmtUsd, num, toneClass } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';

	export let mode: DeskMode;
	export let fleet: LiveFleet | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let rows: DeskRow[] = [];

	$: budget = risk?.portfolio_budget_live ?? null;
	$: books = budget?.per_book ?? {};
	$: wallets = fleet?.capacity?.wallets ?? [];
	$: limitsPct = budget?.limits_pct ?? {};
	$: equity = num(budget?.equity_usd);
	$: riskUsed = num(budget?.total_open_risk_usd) ?? 0;
	$: riskLimit = num(budget?.total_open_risk_limit_usd);
	$: gate = risk?.regime_gate ?? null;
	$: liquidity = risk?.liquidity_guard_live?.limits ?? {};
	$: overlaps = (() => {
		const groups = new Map<string, { coin: string; sides: string[]; ids: Set<string> }>();
		for (const conflict of fleet?.capacity?.conflicts ?? []) {
			const key = `${conflict.coin}:${conflict.sides.join('/')}`;
			const group = groups.get(key) ?? { coin: conflict.coin, sides: conflict.sides, ids: new Set<string>() };
			conflict.strategy_ids.forEach((id) => group.ids.add(id));
			groups.set(key, group);
		}
		return [...groups.values()];
	})();
	$: sortedBooks = [...rows].sort((a, b) => (num(a.session.capital) ?? 0) - (num(b.session.capital) ?? 0));

	const heading = 'text-[13px] font-semibold text-sc-ink';
</script>

<div class="grid lg:grid-cols-3" data-testid="desk-risk">
	{#if mode === 'live'}
		<section class="grid min-w-0 content-start gap-2.5 border-b border-sc-line p-3 lg:border-b-0 lg:border-r">
			<h3 class={heading}>Wallets</h3>
			{#each wallets as wallet}
				{@const used = num(books[wallet.wallet]?.margin_usd) ?? 0}
				{@const capacity = num(wallet.capacity_usd) ?? 0}
				<div class="grid gap-1">
					<div class="flex justify-between gap-2 text-[12px]"><span class="text-sc-ink2">{cap1(wallet.wallet)} wallet · {wallet.sides.join(', ')}</span><b class="font-plex-mono font-medium text-sc-ink">{fmtUsd(wallet.equity_usd)}</b></div>
					<div class="relative h-2.5 overflow-hidden rounded-sm bg-sc-line2" title={`Margin in use against the ${fleet?.capacity?.margin_cap_pct ?? 80}% cap`}>
						<i class="absolute inset-y-0 left-0 bg-sc-ink2" style={`width:${capacity ? Math.min(100, (used / capacity) * 100) : 0}%`}></i>
						{#if capacity && wallet.worst_case_margin_usd !== null}<span class="absolute -bottom-0.5 -top-0.5 w-0.5 bg-[#e7b24a]" style={`left:${Math.min(99, ((wallet.worst_case_margin_usd ?? 0) / capacity) * 100)}%`} title={`If every ${wallet.wallet} strategy opened at once`}></span>{/if}
					</div>
					<div class="flex justify-between gap-2 text-[11.5px] text-sc-ink3"><span>{fmtUsd(used)} margin used of {fmtUsd(capacity)}</span><span>all at once {fmtUsd(wallet.worst_case_margin_usd)}</span></div>
				</div>
			{:else}
				<p class="text-[12px] text-sc-ink3">Wallet figures are not available.</p>
			{/each}
			<p class="text-[12px] text-sc-ink3">The amber tick is the margin needed if every strategy on that side opened at once.{wallets.every((wallet) => !wallet.over_capacity) && wallets.length ? ' Every wallet fits it today.' : ''}</p>
		</section>
		<section class="grid min-w-0 content-start gap-2.5 border-b border-sc-line p-3 lg:border-b-0 lg:border-r">
			<h3 class={heading}>Risk budget</h3>
			<div class="grid gap-1">
				<div class="flex justify-between gap-2 text-[12px]"><span class="text-sc-ink2">Open risk, if every stop hits</span><b class="font-plex-mono font-medium text-sc-ink">{fmtUsd(riskUsed)} of {fmtUsd(riskLimit)}</b></div>
				<div class="relative h-2.5 overflow-hidden rounded-sm bg-sc-line2"><i class="absolute inset-y-0 left-0 bg-sc-ink2" style={`width:${riskLimit ? Math.min(100, (riskUsed / riskLimit) * 100) : 0}%`}></i></div>
			</div>
			<ul class="grid gap-1.5 text-[12px] text-sc-ink2">
				<li>Per trade, hard cap: <b class="font-medium text-sc-ink">{fmtPct(num(limitsPct.live_hard_max_per_trade_risk_pct) ?? 2, 0, false)}</b> of equity{equity !== null ? ` (${fmtUsd((equity * (num(limitsPct.live_hard_max_per_trade_risk_pct) ?? 2)) / 100)})` : ''}</li>
				<li>One asset: <b class="font-medium text-sc-ink">{fmtPct(num(limitsPct.live_max_asset_exposure_pct) ?? 150, 0, false)}</b> of equity · one group: <b class="font-medium text-sc-ink">{fmtPct(num(limitsPct.live_max_group_exposure_pct) ?? 200, 0, false)}</b></li>
				<li>Positions without a stop: <b class="font-medium text-sc-ink">{budget?.stops_missing ?? 0}</b></li>
				<li>Capital slice per strategy: <b class="font-medium text-sc-ink">{fmtUsd(budget?.capital_slice?.slice_usd)}</b> ({budget?.capital_slice?.cohort_size ?? rows.length} live)</li>
				{#if liquidity.live_min_daily_volume_usd}
					<li>Liquidity guard: daily volume over <b class="font-medium text-sc-ink">{fmtUsd(liquidity.live_min_daily_volume_usd, { digits: 0 })}</b>, spread under <b class="font-medium text-sc-ink">{liquidity.live_max_spread_bps ?? '—'} bps</b>, at most <b class="font-medium text-sc-ink">{fmtPct(liquidity.live_max_book_participation_pct, 0, false)}</b> of the book</li>
				{/if}
			</ul>
		</section>
	{:else}
		<section class="grid min-w-0 content-start gap-2 border-b border-sc-line p-3 lg:col-span-2 lg:border-b-0 lg:border-r">
			<h3 class={heading}>Paper books</h3>
			<p class="text-[12px] text-sc-ink3">Each paper strategy trades its own simulated book. Books are independent: no wallet netting and no shared margin.</p>
			<div class="overflow-x-auto">
				<table class="w-full border-collapse text-[12px]">
					<thead>
						<tr class="font-plex-cond text-[11px] uppercase tracking-[0.06em] text-sc-ink3">
							<th class="px-2 py-1.5 text-left font-medium">Strategy</th><th class="px-2 py-1.5 text-right font-medium">Book</th><th class="px-2 py-1.5 text-right font-medium">Since start</th><th class="px-2 py-1.5 text-right font-medium">Open legs</th><th class="px-2 py-1.5 text-right font-medium">Open P&amp;L</th><th class="px-2 py-1.5 text-right font-medium">Risk at stops</th>
						</tr>
					</thead>
					<tbody class="font-plex-mono tabular-nums">
						{#each sortedBooks as row (row.session.id)}
							{@const start = num(row.session.initial_capital) ?? 10_000}
							{@const capital = num(row.session.capital) ?? start}
							{@const atRisk = row.legMath.reduce((sum, math) => sum + (math.risk ?? 0), 0)}
							<tr class="border-t border-sc-line">
								<td class="px-2 py-1.5 text-left font-sans">{row.sid}</td>
								<td class="px-2 py-1.5 text-right">{fmtUsd(capital, { digits: 0 })}</td>
								<td class={`px-2 py-1.5 text-right ${toneClass(capital - start)}`}>{fmtPct(((capital - start) / start) * 100, 1)}</td>
								<td class="px-2 py-1.5 text-right">{row.legs.length || '—'}</td>
								<td class={`px-2 py-1.5 text-right ${toneClass(row.openPnl)}`}>{row.legs.length ? fmtUsd(row.openPnl, { signed: true }) : '—'}</td>
								<td class="px-2 py-1.5 text-right">{atRisk ? `${fmtUsd(atRisk)} · ${fmtPct((atRisk / capital) * 100, 1, false)}` : '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>
	{/if}
	<section class="grid min-w-0 content-start gap-2.5 p-3">
		<h3 class={heading}>Regime{mode === 'live' ? ' and overlap' : ''}</h3>
		{#each gate?.stances ?? [] as stance}
			{@const confidence = num(stance.confidence) ?? 0}
			<div class="grid grid-cols-[40px_minmax(0,1fr)_44px] items-center gap-2 text-[12px]">
				<b class="font-plex-mono font-medium text-sc-ink">{stance.asset}</b>
				<div>
					<div class="text-sc-ink2">{cap1(humanRegime(stance.regime))}</div>
					<div class="relative h-1.5 overflow-hidden rounded bg-sc-line2" title={`Confidence ${fmtPct(confidence * 100, 0, false)}; the gate acts at ${fmtPct((gate?.min_confidence ?? 0.6) * 100, 0, false)}`}>
						<i class="absolute inset-y-0 left-0 bg-sc-ink2" style={`width:${confidence * 100}%`}></i>
						<b class="absolute -bottom-0.5 -top-0.5 w-0.5 bg-sc-ink" style={`left:${(gate?.min_confidence ?? 0.6) * 100}%`}></b>
					</div>
				</div>
				<span class="text-right font-plex-mono text-sc-ink3">{fmtPct(confidence * 100, 0, false)}</span>
			</div>
		{:else}
			<p class="text-[12px] text-sc-ink3">No regime stances yet.</p>
		{/each}
		{#if gate}
			<p class="text-[12px] text-sc-ink3">Gate mode: {gate.mode}. The white tick is the {fmtPct((gate.min_confidence ?? 0.6) * 100, 0, false)} confidence the gate needs before it acts.</p>
		{/if}
		{#if mode === 'live' && overlaps.length}
			<ul class="grid gap-1.5 text-[12px] text-sc-ink2">
				{#each overlaps as overlap}
					<li><b class="font-medium text-sc-ink">{overlap.coin} {overlap.sides.join('/')}</b>: {[...overlap.ids].join(', ')} refuse each other's entries (one live strategy per coin and side).</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
