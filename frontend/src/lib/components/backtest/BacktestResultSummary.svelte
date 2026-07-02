<script lang="ts">
	import EquityChart from '$lib/components/EquityChart.svelte';
	import type { BacktestResult, Trade } from '$lib/api';

	export let result: BacktestResult;

	type AnyMetrics = Record<string, unknown>;

	function num(v: unknown): number | null {
		const n = typeof v === 'string' ? parseFloat(v) : (v as number);
		return typeof n === 'number' && Number.isFinite(n) ? n : null;
	}

	// Tolerant accessor: the result-detail endpoint returns metrics under either
	// raw engine keys (total_return_pct, sharpe, max_drawdown_pct, win_rate as a
	// 0–1 fraction) or normalised keys (total_return, sharpe_ratio, …). Try each.
	function pick(m: AnyMetrics, ...keys: string[]): number | null {
		for (const k of keys) {
			const v = num(m?.[k]);
			if (v !== null) return v;
		}
		return null;
	}

	$: m = (result?.metrics ?? {}) as AnyMetrics;

	// Returns/drawdown are stored as fractions on the raw path → render as %.
	$: totalReturnPct = pick(m, 'total_return_pct', 'total_return');
	$: maxDrawdownPct = (() => {
		const v = pick(m, 'max_drawdown_pct', 'max_drawdown');
		return v === null ? null : Math.abs(v);
	})();
	$: winRate = (() => {
		const v = pick(m, 'win_rate');
		if (v === null) return null;
		return v <= 1 ? v * 100 : v; // 0–1 fraction → %
	})();
	$: sharpe = pick(m, 'sharpe', 'sharpe_ratio');
	$: sortino = pick(m, 'sortino', 'sortino_ratio');
	$: calmar = pick(m, 'calmar', 'calmar_ratio');
	$: profitFactor = pick(m, 'profit_factor');
	$: pfInfinite = m?.profit_factor_is_infinite === true;
	$: expectancy = pick(m, 'expectancy', 'avg_trade_pct');
	// annualized_return_pct is already percent points; cagr (if used) is a fraction.
	$: annualized = (() => {
		const a = num(m?.annualized_return_pct);
		if (a !== null) return a;
		const c = num(m?.cagr);
		return c === null ? null : (Math.abs(c) <= 1.5 ? c * 100 : c);
	})();
	$: totalTrades = pick(m, 'total_trades') ?? (result?.trades?.length ?? 0);

	function asPct(v: number | null, dp = 2): string {
		if (v === null) return '–';
		// fractions (|v|<=1.5) are scaled; already-% values pass through
		const scaled = Math.abs(v) <= 1.5 ? v * 100 : v;
		return `${scaled >= 0 ? '' : ''}${scaled.toFixed(dp)}%`;
	}
	function pctDirect(v: number | null, dp = 2): string {
		return v === null ? '–' : `${v.toFixed(dp)}%`;
	}
	function asNum(v: number | null, dp = 2): string {
		return v === null ? '–' : v.toFixed(dp);
	}

	$: tiles = [
		{ label: 'Total Return', value: pctDirect(totalReturnPct === null ? null : totalReturnPct * 100), tone: (totalReturnPct ?? 0) >= 0 ? 'pos' : 'neg', title: 'Out-of-sample total return.' },
		{ label: 'Annualized', value: pctDirect(annualized), tone: (annualized ?? 0) >= 0 ? 'pos' : 'neg', title: 'Annualized (CAGR) return.' },
		{ label: 'Sharpe', value: asNum(sharpe), tone: (sharpe ?? 0) >= 1 ? 'pos' : (sharpe ?? 0) < 0 ? 'neg' : 'neutral', title: 'Risk-adjusted return (annualized).' },
		{ label: 'Sortino', value: asNum(sortino), tone: 'neutral', title: 'Downside risk-adjusted return.' },
		{ label: 'Calmar', value: asNum(calmar), tone: 'neutral', title: 'Return / max drawdown.' },
		{ label: 'Max DD', value: pctDirect(maxDrawdownPct === null ? null : maxDrawdownPct * 100), tone: 'neg', title: 'Maximum peak-to-trough drawdown.' },
		{ label: 'Win Rate', value: pctDirect(winRate), tone: (winRate ?? 0) >= 50 ? 'pos' : 'neutral', title: 'Share of winning trades.' },
		{ label: 'Profit Factor', value: pfInfinite ? '∞' : asNum(profitFactor), tone: pfInfinite || (profitFactor ?? 0) >= 1 ? 'pos' : 'neg', title: 'Gross profit / gross loss.' },
		{ label: 'Expectancy', value: asPct(expectancy), tone: (expectancy ?? 0) >= 0 ? 'pos' : 'neg', title: 'Average return per trade.' },
		{ label: 'Trades', value: String(Math.round(totalTrades)), tone: 'neutral', title: 'Out-of-sample trade count.' },
	];

	// --- Trade list + summary footer -------------------------------------------
	$: trades = (result?.trades ?? []) as Trade[];

	function row(t: Trade): Record<string, unknown> {
		return t as unknown as Record<string, unknown>;
	}
	function tradePnlPct(t: Trade): number | null {
		// getResult() shape carries return_pct ALREADY in percent points
		// (_normalize_trade_rows multiplies by 100); the live-engine shape carries
		// pnl_pct as a raw fraction. Prefer return_pct as-is — re-scaling it would
		// turn a +1.32% trade into +132%.
		const rp = num(t.return_pct);
		if (rp !== null) return rp;
		const pp = num(row(t).pnl_pct);
		return pp === null ? null : pp * 100;
	}
	function sizeFrac(t: Trade): number | null {
		return num(row(t).size_fraction);
	}
	function exitReason(t: Trade): string {
		return String(row(t).exit_reason ?? '').replace(/_/g, ' ');
	}

	$: summary = (() => {
		const pnls = trades.map(tradePnlPct).filter((v): v is number => v !== null);
		if (pnls.length === 0) return null;
		const wins = pnls.filter((v) => v > 0);
		const losses = pnls.filter((v) => v < 0);
		const avgWin = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : 0;
		const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / losses.length : 0;
		const payoff = avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : null;
		// longest streaks
		let curW = 0, curL = 0, maxW = 0, maxL = 0;
		for (const v of pnls) {
			if (v > 0) { curW++; curL = 0; maxW = Math.max(maxW, curW); }
			else if (v < 0) { curL++; curW = 0; maxL = Math.max(maxL, curL); }
		}
		return {
			count: pnls.length,
			wins: wins.length,
			losses: losses.length,
			avgWin,
			avgLoss,
			payoff,
			best: Math.max(...pnls),
			worst: Math.min(...pnls),
			maxWinStreak: maxW,
			maxLossStreak: maxL,
		};
	})();

	$: hasEquity = Array.isArray(result?.equity_curve) && result.equity_curve.length > 1;
	$: visibleTrades = trades.slice(0, 50);

	function fmtTime(ts: unknown): string {
		const s = String(ts ?? '');
		return s.length > 16 ? s.slice(0, 16).replace('T', ' ') : s;
	}
	function fmtPrice(v: unknown): string {
		const n = num(v);
		if (n === null) return '–';
		const dp = Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : 6;
		return n.toFixed(dp);
	}
</script>

<div class="space-y-6">
	<!-- Metrics grid -->
	<div class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
		{#each tiles as tile}
			<div class="border border-[#1a1a1a] bg-[#050505] px-3 py-2.5" title={tile.title}>
				<div class="text-[9px] uppercase tracking-wider text-[#666]">{tile.label}</div>
				<div class="mt-1 font-mono text-sm tabular-nums {tile.tone === 'pos' ? 'text-emerald-400' : tile.tone === 'neg' ? 'text-red-400' : 'text-white'}">
					{tile.value}
				</div>
			</div>
		{/each}
	</div>

	<!-- Equity / drawdown / benchmark -->
	{#if hasEquity}
		<div class="border border-[#1a1a1a] bg-[#050505] p-4">
			<div class="mb-2 flex items-center justify-between">
				<div class="text-[10px] uppercase tracking-widest text-[#666]">Equity & Drawdown</div>
				<div class="flex items-center gap-3 text-[10px] text-[#666]">
					<span class="flex items-center gap-1"><span class="inline-block h-2 w-3 bg-cyan-400/80"></span> Strategy</span>
					{#if result.benchmark_curve && result.benchmark_curve.length > 1}
						<span class="flex items-center gap-1"><span class="inline-block h-2 w-3 bg-gray-500/80"></span> Buy &amp; Hold</span>
					{/if}
				</div>
			</div>
			<EquityChart
				data={result.equity_curve ?? []}
				benchmarkData={result.benchmark_curve ?? null}
				showDrawdown={true}
				height={280}
			/>
		</div>
	{/if}

	<!-- Trade table + summary -->
	{#if trades.length > 0}
		<div class="border border-[#1a1a1a] bg-[#050505] p-4">
			<div class="mb-3 flex items-center justify-between">
				<div class="text-[10px] uppercase tracking-widest text-[#666]">
					Trades <span class="ml-1 normal-case tracking-normal text-[#555]">(out-of-sample · showing {visibleTrades.length} of {trades.length})</span>
				</div>
			</div>

			{#if summary}
				<div class="mb-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4 lg:grid-cols-7">
					<div><span class="text-[#666]">W/L:</span> <span class="font-mono text-white">{summary.wins}/{summary.losses}</span></div>
					<div><span class="text-[#666]">Avg Win:</span> <span class="font-mono text-emerald-400">{summary.avgWin.toFixed(2)}%</span></div>
					<div><span class="text-[#666]">Avg Loss:</span> <span class="font-mono text-red-400">{summary.avgLoss.toFixed(2)}%</span></div>
					<div><span class="text-[#666]">Payoff:</span> <span class="font-mono text-white">{summary.payoff !== null ? summary.payoff.toFixed(2) : '–'}</span></div>
					<div><span class="text-[#666]">Best:</span> <span class="font-mono text-emerald-400">{summary.best.toFixed(2)}%</span></div>
					<div><span class="text-[#666]">Worst:</span> <span class="font-mono text-red-400">{summary.worst.toFixed(2)}%</span></div>
					<div><span class="text-[#666]">Streak W/L:</span> <span class="font-mono text-white">{summary.maxWinStreak}/{summary.maxLossStreak}</span></div>
				</div>
			{/if}

			<div class="max-h-72 overflow-auto border border-[#1a1a1a]">
				<table class="w-full text-[11px]">
					<thead class="sticky top-0 bg-[#050505] text-[#666]">
						<tr class="border-b border-[#1a1a1a]">
							<th class="px-2 py-1.5 text-left font-medium">Dir</th>
							<th class="px-2 py-1.5 text-left font-medium">Entry</th>
							<th class="px-2 py-1.5 text-right font-medium">Entry px</th>
							<th class="px-2 py-1.5 text-right font-medium">Exit px</th>
							<th class="px-2 py-1.5 text-right font-medium">PnL %</th>
							<th class="px-2 py-1.5 text-right font-medium">Size</th>
							<th class="px-2 py-1.5 text-left font-medium">Exit</th>
							<th class="px-2 py-1.5 text-right font-medium">Bars</th>
						</tr>
					</thead>
					<tbody class="font-mono text-[#888]">
						{#each visibleTrades as t}
							{@const pnl = tradePnlPct(t)}
							<tr class="border-b border-[#111]">
								<td class="px-2 py-1 {(t.direction ?? 'long') === 'short' ? 'text-red-400' : 'text-emerald-400'}">{(t.direction ?? 'long')}</td>
								<td class="px-2 py-1 text-[#666]">{fmtTime(t.entry_time)}</td>
								<td class="px-2 py-1 text-right">{fmtPrice(t.entry_price)}</td>
								<td class="px-2 py-1 text-right">{fmtPrice(t.exit_price)}</td>
								<td class="px-2 py-1 text-right {(pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}">{pnl === null ? '–' : pnl.toFixed(2)}</td>
								<td class="px-2 py-1 text-right text-[#666]">{sizeFrac(t) !== null ? `${(sizeFrac(t)! * 100).toFixed(0)}%` : '–'}</td>
								<td class="px-2 py-1 text-left text-[#555]">{exitReason(t) || '–'}</td>
								<td class="px-2 py-1 text-right text-[#666]">{num(row(t).bars_held) ?? '–'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}
</div>
