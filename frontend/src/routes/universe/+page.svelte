<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getPortfolioLayerEnabled,
		getUniverseBooks,
		getUniverseResearch,
		resetUniverseBook,
		tickUniverseBooks,
		type UniverseBook,
		type UniverseCurvePoint,
		type UniverseResearchReport,
		type UniverseStats,
	} from '$lib/api/universe';
	import EquityChart from '$lib/components/EquityChart.svelte';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	let layerEnabled: boolean | null = null;
	let booksEnabled = false;
	let books: UniverseBook[] = [];
	let selected = '';
	let reports: Record<string, UniverseResearchReport> = {};
	let reportLoading = false;
	let loading = true;
	let busy = false;
	let error = '';
	let actionMessage = '';

	$: book = books.find((b) => b.book === selected) ?? null;
	$: report = selected ? reports[selected] ?? null : null;
	$: if (selected && layerEnabled && !reports[selected] && !reportLoading) void loadReport(selected);

	onMount(async () => {
		layerEnabled = await getPortfolioLayerEnabled();
		if (layerEnabled) await loadBooks();
		loading = false;
	});

	async function loadBooks() {
		try {
			const res = await getUniverseBooks();
			books = res.books;
			booksEnabled = res.enabled;
			if (!selected && books.length) selected = books[0].book;
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function loadReport(name: string, refresh = false) {
		reportLoading = true;
		try {
			const res = await getUniverseResearch(name, refresh);
			reports = { ...reports, [name]: res.report };
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			reportLoading = false;
		}
	}

	async function tickNow() {
		busy = true;
		try {
			const res = await tickUniverseBooks();
			const report = res.report as { enabled?: boolean; reason?: string; books?: Array<{ book: string; reason?: string; days?: number; started?: boolean }> };
			if (!report.enabled) actionMessage = 'Forward books are off.';
			else if (report.reason) actionMessage = report.reason;
			else
				actionMessage = (report.books ?? [])
					.map((b) => `${b.book}: ${b.started ? 'started' : b.days ? `${b.days} day(s) booked` : b.reason ?? 'no change'}`)
					.join(' · ');
			await loadBooks();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}

	async function resetBook(name: string) {
		if (!confirm(`Reset ${name}? Its forward record is discarded and it restarts on the next tick.`)) return;
		busy = true;
		try {
			await resetUniverseBook(name);
			actionMessage = `${name} reset.`;
			await loadBooks();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}

	function curve(points: UniverseCurvePoint[] | undefined) {
		return (points ?? []).map((p) => ({ timestamp: `${p.day}T00:00:00Z`, equity: p.equity }));
	}

	function pct(value: number | null | undefined, digits = 1): string {
		if (value == null || !Number.isFinite(value)) return '—';
		return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
	}

	function num(value: number | null | undefined, digits = 2): string {
		if (value == null || !Number.isFinite(value)) return '—';
		return value.toFixed(digits);
	}

	function tone(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value) || value === 0) return 'text-sc-ink';
		return value > 0 ? 'text-emerald-400' : 'text-red-400';
	}

	function headline(stats: UniverseStats | undefined) {
		if (!stats) return [];
		return [
			{ label: 'Return', value: pct(stats.return_pct), cls: tone(stats.return_pct) },
			{ label: 'CAGR', value: pct(stats.cagr_pct), cls: tone(stats.cagr_pct) },
			{ label: 'Sharpe', value: num(stats.sharpe), cls: tone(stats.sharpe) },
			{ label: 'Max drawdown', value: pct(stats.max_drawdown_pct), cls: 'text-sc-ink' },
			{ label: 'Realised vol', value: pct(stats.ann_vol_pct), cls: 'text-sc-ink' },
			{ label: 'Avg gross exposure', value: num(stats.avg_gross_exposure), cls: 'text-sc-ink' },
		];
	}
</script>

<div class="space-y-4 p-4">
	<div class="flex flex-wrap items-start justify-between gap-3">
		<div>
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Universe books</h1>
			<p class="text-[11px] text-sc-ink3">
				One pre-registered rule held across 15 coins at once, rebalanced at each daily close. Paper only.
			</p>
		</div>
		{#if layerEnabled}
			<div class="flex items-center gap-2 shrink-0">
				<span
					class={`text-xs px-2 py-1 border ${booksEnabled ? 'text-emerald-400 border-emerald-800' : 'text-sc-ink3 border-sc-line2'}`}
				>
					Forward books {booksEnabled ? 'on' : 'off'}
				</span>
				<button
					class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
					disabled={busy || !booksEnabled}
					on:click={tickNow}
				>
					Tick now
				</button>
			</div>
		{/if}
	</div>

	{#if error}
		<ErrorBanner message={error} />
	{/if}
	{#if actionMessage}
		<div class="rounded-md border border-sc-line2 bg-sc-panel px-3 py-2 text-xs text-sc-ink2">{actionMessage}</div>
	{/if}

	{#if loading}
		<LoadingState message="Loading universe books…" />
	{:else if !layerEnabled}
		<div class="rounded-md border border-sc-line bg-sc-panel p-6 text-center text-xs text-sc-ink3">
			The portfolio layer is off. Turn it on under Settings → System → Experimental features.
		</div>
	{:else}
		<div class="flex flex-wrap gap-2">
			{#each books as b (b.book)}
				<button
					class={`rounded-md border px-3 py-1.5 text-[12px] ${selected === b.book ? 'border-sc-ink3 bg-sc-raise text-sc-ink' : 'border-sc-line2 bg-sc-panel text-sc-ink2 hover:bg-sc-raise'}`}
					on:click={() => (selected = b.book)}
				>
					{b.label}
				</button>
			{/each}
		</div>

		{#if book}
			<!-- Forward paper book: the clean test. -->
			<section class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div>
						<h2 class="text-[14px] font-semibold text-sc-ink">Forward paper book</h2>
						<p class="text-[11px] text-sc-ink3">
							The clean test: it starts when switched on, takes positions at the next daily close and is never backfilled.
						</p>
					</div>
					{#if book.exists}
						<button
							class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
							disabled={busy}
							on:click={() => resetBook(book.book)}
						>
							Reset
						</button>
					{/if}
				</div>

				{#if !book.exists}
					<p class="text-[12px] text-sc-ink3">
						{booksEnabled
							? 'Not started yet — it starts on the next hourly tick.'
							: 'Not started. Turn on “Universe paper books” under Settings → Portfolio → Universe books.'}
					</p>
				{:else}
					<div class="grid grid-cols-2 md:grid-cols-5 gap-2 text-center">
						<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
							<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Started</div>
							<div class="text-[13px] font-semibold text-sc-ink">{book.started_at?.slice(0, 10) ?? '—'}</div>
						</div>
						<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
							<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Days booked</div>
							<div class="text-[13px] font-semibold text-sc-ink">{book.stats?.days ?? 0}</div>
						</div>
						<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
							<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Return</div>
							<div class={`text-[13px] font-semibold ${tone(book.stats?.return_pct)}`}>{pct(book.stats?.return_pct, 2)}</div>
						</div>
						<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
							<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Max drawdown</div>
							<div class="text-[13px] font-semibold text-sc-ink">{pct(book.stats?.max_drawdown_pct, 2)}</div>
						</div>
						<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
							<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Gross exposure</div>
							<div class="text-[13px] font-semibold text-sc-ink">{num(book.gross_exposure)}</div>
						</div>
					</div>
					<p class="text-[11px] text-sc-ink3">Last close booked: {book.last_day ?? '—'}</p>

					{#if (book.equity_curve ?? []).length > 1}
						<EquityChart data={curve(book.equity_curve)} height={220} showDrawdown={false} />
					{/if}

					{#if (book.positions ?? []).length}
						<table class="w-full text-[12px]">
							<thead>
								<tr class="text-left text-[10px] uppercase tracking-wider text-sc-ink3">
									<th class="py-1">Coin</th>
									<th class="py-1 text-right">Weight</th>
									<th class="py-1 text-right">PnL since start</th>
								</tr>
							</thead>
							<tbody>
								{#each book.positions ?? [] as p (p.symbol)}
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink">{p.symbol}</td>
										<td class={`py-1 text-right font-mono ${tone(p.weight)}`}>{(p.weight * 100).toFixed(2)}%</td>
										<td class={`py-1 text-right font-mono ${tone(p.pnl)}`}>{pct(p.pnl * 100, 2)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					{:else}
						<p class="text-[12px] text-sc-ink3">Flat — no positions held.</p>
					{/if}
				{/if}
			</section>

			<!-- Sealed research report. -->
			<section class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div>
						<h2 class="text-[14px] font-semibold text-sc-ink">Research (sealed history)</h2>
						<p class="text-[11px] text-sc-ink3">
							{#if report?.cutoff}
								2021-01-01 to the research cutoff {report.cutoff.slice(0, 10)}. Rule fixed in {report.spec_doc}
								before any run.
							{:else}
								Pre-registered rule; see docs/universe-trend-blend-spec.md.
							{/if}
						</p>
					</div>
					<button
						class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
						disabled={reportLoading}
						on:click={() => loadReport(book.book, true)}
					>
						Recompute
					</button>
				</div>

				{#if reportLoading && !report}
					<LoadingState message="Running the sealed research report…" />
				{:else if report?.error}
					<p class="text-[12px] text-red-400">{report.error}</p>
				{:else if report?.summary}
					<div class="grid grid-cols-2 md:grid-cols-6 gap-2 text-center">
						{#each headline(report.summary) as item (item.label)}
							<div class="rounded-md border border-sc-line bg-sc-panel2 p-2">
								<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">{item.label}</div>
								<div class={`text-[13px] font-semibold ${item.cls}`}>{item.value}</div>
							</div>
						{/each}
					</div>

					{#if (report.equity_curve ?? []).length > 1}
						<EquityChart data={curve(report.equity_curve)} height={240} />
					{/if}

					<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
						<div class="space-y-2">
							<h3 class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Evidence checks</h3>
							<table class="w-full text-[12px]">
								<tbody>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Coins with a positive contribution</td>
										<td class="py-1 text-right font-mono text-sc-ink">
											{report.share_coins_positive == null ? '—' : `${Math.round(report.share_coins_positive * 100)}%`}
										</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Worst Sharpe with one coin left out</td>
										<td class="py-1 text-right font-mono text-sc-ink">{num(report.leave_one_out_min_sharpe)}</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Sharpe at 2× costs</td>
										<td class="py-1 text-right font-mono text-sc-ink">{num(report.costs_2x?.sharpe)}</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">
											Placebo (timing shuffled, {report.placebo?.draws ?? 0} draws): median / 95th pct Sharpe
										</td>
										<td class="py-1 text-right font-mono text-sc-ink">
											{num(report.placebo?.median_sharpe)} / {num(report.placebo?.p95_sharpe)}
										</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Share of placebos the rule beats</td>
										<td class="py-1 text-right font-mono text-sc-ink">
											{report.placebo?.share_beaten_by_actual == null
												? '—'
												: `${Math.round(report.placebo.share_beaten_by_actual * 100)}%`}
										</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Alpha vs equal-weight buy-and-hold (t-stat, beta)</td>
										<td class="py-1 text-right font-mono text-sc-ink">
											{pct(report.alpha_vs_equal_weight?.alpha_pct_per_year)}/yr
											(t {num(report.alpha_vs_equal_weight?.alpha_t_stat)}, β {num(report.alpha_vs_equal_weight?.beta)})
										</td>
									</tr>
									<tr class="border-t border-sc-line">
										<td class="py-1 text-sc-ink2">Cost / funding drag per year</td>
										<td class="py-1 text-right font-mono text-sc-ink">
											{pct(report.summary.cost_drag_pct_per_year, 2)} / {pct(report.summary.funding_drag_pct_per_year, 2)}
										</td>
									</tr>
								</tbody>
							</table>
						</div>

						<div class="space-y-2">
							<h3 class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">By year</h3>
							<table class="w-full text-[12px]">
								<thead>
									<tr class="text-left text-[10px] uppercase tracking-wider text-sc-ink3">
										<th class="py-1">Year</th>
										<th class="py-1 text-right">Return</th>
										<th class="py-1 text-right">Sharpe</th>
										<th class="py-1 text-right">Max DD</th>
									</tr>
								</thead>
								<tbody>
									{#each report.years ?? [] as y (y.year)}
										<tr class="border-t border-sc-line">
											<td class="py-1 text-sc-ink">{y.year}{y.days < 360 ? ` (${y.days}d)` : ''}</td>
											<td class={`py-1 text-right font-mono ${tone(y.return_pct)}`}>{pct(y.return_pct)}</td>
											<td class="py-1 text-right font-mono text-sc-ink">{num(y.sharpe)}</td>
											<td class="py-1 text-right font-mono text-sc-ink">{pct(y.max_drawdown_pct)}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>

					<div class="space-y-2">
						<h3 class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Contribution by coin</h3>
						<div class="grid grid-cols-2 md:grid-cols-5 gap-1 text-[12px]">
							{#each report.coin_contributions ?? [] as c (c.symbol)}
								<div class="flex justify-between rounded border border-sc-line px-2 py-1">
									<span class="text-sc-ink2">{c.symbol.replace('-USDT', '')}</span>
									<span class={`font-mono ${tone(c.contribution_pct)}`}>{pct(c.contribution_pct)}</span>
								</div>
							{/each}
						</div>
					</div>

					{#if report.post_cutoff}
						<div class="rounded-md border border-yellow-900 bg-yellow-500/5 p-3 space-y-2">
							<div class="flex flex-wrap items-baseline justify-between gap-2">
								<h3 class="text-[13px] font-semibold text-yellow-500">
									After the cutoff — {report.post_cutoff.label}
								</h3>
								<span class="text-[11px] text-sc-ink3">
									{report.post_cutoff.summary.start} to {report.post_cutoff.summary.end}
								</span>
							</div>
							<p class="text-[11px] text-sc-ink3">
								A September review already saw this rule family's results for this period, so it is not a clean test. The
								forward paper book above is.
							</p>
							<div class="flex flex-wrap gap-4 text-[12px]">
								<span class="text-sc-ink2">Return <span class={`font-mono ${tone(report.post_cutoff.summary.return_pct)}`}>{pct(report.post_cutoff.summary.return_pct)}</span></span>
								<span class="text-sc-ink2">Sharpe <span class="font-mono text-sc-ink">{num(report.post_cutoff.summary.sharpe)}</span></span>
								<span class="text-sc-ink2">Max DD <span class="font-mono text-sc-ink">{pct(report.post_cutoff.summary.max_drawdown_pct)}</span></span>
								<span class="text-sc-ink2">
									Equal-weight buy-and-hold
									<span class="font-mono text-sc-ink">{pct(report.post_cutoff.alpha_vs_equal_weight?.benchmark_return_pct)}</span>
								</span>
							</div>
						</div>
					{/if}
				{/if}
			</section>
		{/if}
	{/if}
</div>
