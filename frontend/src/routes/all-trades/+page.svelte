<script lang="ts">
	import { getForvenAllTrades, getForvenTradesStats, markForvenTradeFailed } from '$lib/api';
	import type { ForvenTrade, ForvenTradesPage, ForvenTradesStats, ForvenTradesQuery } from '$lib/api';
	import { forvenLivePrices } from '$lib/stores/forvenWebSocket';

	export let data: { initialPage: ForvenTradesPage | null; initialStats: ForvenTradesStats | null };

	const STATUSES = ['ALL', 'OPEN', 'CLOSED', 'FAILED'] as const;
	type StatusFilter = (typeof STATUSES)[number];

	// Server-sortable columns (label is the header text; key is the API sort param).
	const SORT_LABELS: Record<string, string> = {
		opened_at: 'Opened',
		strategy: 'Strategy',
		asset: 'Asset',
		status: 'Status',
		size: 'Notional',
		pnl_usd: '$ P&L',
		duration: 'Held'
	};

	interface Col {
		key: string;
		label: string;
		sortKey?: string;
		align?: 'left' | 'right' | 'center';
	}
	const COLUMNS: Col[] = [
		{ key: 'expand', label: '' },
		{ key: 'opened_at', label: 'Opened', sortKey: 'opened_at' },
		{ key: 'strategy', label: 'Strategy', sortKey: 'strategy' },
		{ key: 'asset', label: 'Asset', sortKey: 'asset' },
		{ key: 'side', label: 'Side' },
		{ key: 'type', label: 'Type' },
		{ key: 'status', label: 'Status', sortKey: 'status' },
		{ key: 'entry', label: 'Entry', align: 'right' },
		{ key: 'exit', label: 'Exit', align: 'right' },
		{ key: 'notional', label: 'Notional', sortKey: 'size', align: 'right' },
		{ key: 'pnl_usd', label: '$ P&L', sortKey: 'pnl_usd', align: 'right' },
		{ key: 'pnl_pct', label: 'P&L %', align: 'right' },
		{ key: 'duration', label: 'Held', sortKey: 'duration', align: 'right' },
		{ key: 'actions', label: '', align: 'right' }
	];

	let trades: ForvenTrade[] = data.initialPage?.trades ?? [];
	let total: number = data.initialPage?.total ?? trades.length;
	let stats: ForvenTradesStats | null = data.initialStats ?? null;

	// Filters
	let statusFilter: StatusFilter = 'ALL';
	let assetFilter = '';
	let strategyFilter = '';
	let directionFilter = '';
	let execTypeFilter = '';
	let fromDate = '';
	let toDate = '';
	let search = '';

	// Sort + paging
	let sort = data.initialPage?.sort ?? 'opened_at';
	let sortDir: 'asc' | 'desc' = (data.initialPage?.sort_dir as 'asc' | 'desc') ?? 'desc';
	let pageSize = 100;
	let offset = 0;

	let loading = false;
	let error = '';
	let notice = '';
	let busyTradeId = '';
	let expandedId = '';
	let searchTimer: ReturnType<typeof setTimeout> | undefined;

	function buildQuery(): ForvenTradesQuery {
		const q: ForvenTradesQuery = {
			limit: pageSize,
			offset,
			sort,
			sort_dir: sortDir
		};
		if (statusFilter !== 'ALL') q.status = statusFilter;
		if (assetFilter.trim()) q.asset = assetFilter.trim().toUpperCase();
		if (strategyFilter.trim()) q.strategy = strategyFilter.trim();
		if (directionFilter) q.direction = directionFilter;
		if (execTypeFilter) q.execution_type = execTypeFilter;
		if (fromDate) q.opened_from = fromDate;
		if (toDate) q.opened_to = `${toDate}T23:59:59.999`;
		if (search.trim()) q.search = search.trim();
		return q;
	}

	async function loadTrades(reset = false): Promise<void> {
		if (reset) offset = 0;
		loading = true;
		error = '';
		try {
			const page = await getForvenAllTrades(buildQuery());
			trades = page.trades;
			total = page.total;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load trades';
		} finally {
			loading = false;
		}
	}

	async function loadStats(): Promise<void> {
		try {
			const { limit, offset: _o, sort: _s, sort_dir: _sd, ...filters } = buildQuery();
			stats = await getForvenTradesStats(filters);
		} catch {
			/* stat bar is best-effort; the table is the source of truth */
		}
	}

	/** Filter change → reset to page 1 and refresh BOTH the table and the stat bar. */
	async function applyFilters(): Promise<void> {
		notice = '';
		await Promise.all([loadTrades(true), loadStats()]);
	}

	function setStatus(s: StatusFilter): void {
		if (statusFilter === s) return;
		statusFilter = s;
		void applyFilters();
	}

	function onSearchInput(): void {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => void applyFilters(), 300);
	}

	function clearFilters(): void {
		statusFilter = 'ALL';
		assetFilter = '';
		strategyFilter = '';
		directionFilter = '';
		execTypeFilter = '';
		fromDate = '';
		toDate = '';
		search = '';
		void applyFilters();
	}

	function toggleSort(col: Col): void {
		if (!col.sortKey) return;
		if (sort === col.sortKey) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sort = col.sortKey;
			sortDir = 'desc';
		}
		void loadTrades(true);
	}

	function toggleExpand(id: string | undefined): void {
		const tid = (id ?? '').trim();
		if (!tid) return;
		expandedId = expandedId === tid ? '' : tid;
	}

	async function nextPage(): Promise<void> {
		if (offset + pageSize >= total) return;
		offset += pageSize;
		await loadTrades();
	}
	async function prevPage(): Promise<void> {
		if (offset === 0) return;
		offset = Math.max(0, offset - pageSize);
		await loadTrades();
	}
	function changePageSize(e: Event): void {
		pageSize = Number((e.target as HTMLSelectElement).value) || 100;
		void loadTrades(true);
	}

	async function handleMarkFailed(trade: ForvenTrade): Promise<void> {
		const tradeId = (trade.id ?? '').trim();
		if (!tradeId) return;
		const confirmed = window.confirm(
			`Mark trade ${tradeId} as FAILED and release its risk slot?\n\n` +
				'Use this only for a phantom open that never filled — it does NOT send any ' +
				'exchange order. For a real position, use Force Close on the Live Trading page.'
		);
		if (!confirmed) return;
		busyTradeId = tradeId;
		error = '';
		notice = '';
		try {
			await markForvenTradeFailed(tradeId);
			notice = `Trade ${tradeId} marked FAILED and its position released.`;
			await Promise.all([loadTrades(), loadStats()]);
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to mark ${tradeId} failed`;
		} finally {
			busyTradeId = '';
		}
	}

	// ---- derivations & formatting -------------------------------------------

	function toNumber(value: unknown): number | null {
		if (value === null || value === undefined || value === '') return null;
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	}

	function signalData(trade: ForvenTrade): Record<string, unknown> {
		const sd = trade.signal_data;
		if (sd && typeof sd === 'object') return sd as Record<string, unknown>;
		if (typeof sd === 'string' && sd.trim()) {
			try {
				return JSON.parse(sd) as Record<string, unknown>;
			} catch {
				return {};
			}
		}
		return {};
	}

	function notional(t: ForvenTrade): number | null {
		const size = toNumber(t.size);
		const price =
			toNumber(t.fill_entry_price) ?? toNumber(t.entry_price) ?? toNumber(t.signal_entry_price);
		if (size === null || price === null) return null;
		return Math.abs(size * price);
	}

	function realizedUsd(t: ForvenTrade): number | null {
		return toNumber(t.pnl_usd) ?? toNumber(t.pnl);
	}

	function isOpen(t: ForvenTrade): boolean {
		return String(t.status ?? '').toUpperCase() === 'OPEN';
	}

	/** Current live mark for an asset from the WS price feed (keyed by plain asset,
	 * e.g. BTC). Falls back to a separator/quote-insensitive match. */
	function livePrice(asset: string | undefined, prices: Record<string, number>): number | null {
		const key = String(asset ?? '').trim().toUpperCase();
		if (!key) return null;
		const direct = Number(prices[key]);
		if (Number.isFinite(direct) && direct > 0) return direct;
		const norm = (s: string) => s.toUpperCase().replace(/[-_/]/g, '').replace(/(USDT|USD|PERP)$/, '');
		const want = norm(key);
		for (const [k, v] of Object.entries(prices)) {
			if (norm(k) !== want) continue;
			const n = Number(v);
			if (Number.isFinite(n) && n > 0) return n;
		}
		return null;
	}

	/** Live unrealized $ P&L for an OPEN trade (price move × size, direction-signed).
	 * Null when no live price is available. */
	function liveUnrealizedUsd(t: ForvenTrade, prices: Record<string, number>): number | null {
		const entry = toNumber(t.fill_entry_price) ?? toNumber(t.entry_price);
		const size = toNumber(t.size);
		const px = livePrice(t.asset, prices);
		if (entry === null || size === null || px === null) return null;
		const sign = String(t.direction ?? '').toLowerCase() === 'short' ? -1 : 1;
		return (px - entry) * size * sign;
	}

	/** Dollar P&L shown in the table: realized for closed, LIVE unrealized for open. */
	function effectiveUsd(t: ForvenTrade, prices: Record<string, number>): number | null {
		return isOpen(t) ? liveUnrealizedUsd(t, prices) : realizedUsd(t);
	}

	/** Return on entry notional, derived from the effective dollar P&L —
	 * convention-independent (avoids the stored pnl_pct equity-fraction-vs-margin
	 * unit blend), and live for open trades. */
	function effectivePct(t: ForvenTrade, prices: Record<string, number>): number | null {
		const pnl = effectiveUsd(t, prices);
		const n = notional(t);
		if (pnl === null || n === null || n === 0) return null;
		return (pnl / n) * 100;
	}

	/** Strategy link target: hop into that strategy's trade on the Trades page root
	 * (selecting its session), NOT the strategy-lab container. */
	function tradeHref(t: ForvenTrade): string {
		const sid = strategyId(t);
		const live = String(t.execution_type ?? '').toLowerCase() === 'live';
		return `/trading?select=${encodeURIComponent(sid)}${live ? '&view=live' : ''}`;
	}

	function rMultiple(t: ForvenTrade): number | null {
		const sd = signalData(t);
		const stop = toNumber(sd.stop_loss_price ?? sd.stop_loss);
		const entry = toNumber(t.fill_entry_price) ?? toNumber(t.entry_price);
		const size = toNumber(t.size);
		const pnl = realizedUsd(t);
		if (stop === null || entry === null || size === null || pnl === null) return null;
		const risk = Math.abs(entry - stop) * Math.abs(size);
		if (risk === 0) return null;
		return pnl / risk;
	}

	function durationMs(t: ForvenTrade): number | null {
		const o = t.opened_at ? new Date(t.opened_at).getTime() : NaN;
		if (Number.isNaN(o)) return null;
		const isOpen = String(t.status ?? '').toUpperCase() === 'OPEN';
		const end = t.closed_at
			? new Date(t.closed_at).getTime()
			: isOpen
				? Date.now()
				: NaN;
		if (Number.isNaN(end)) return null;
		return Math.max(0, end - o);
	}

	/** The STRATEGY number (S####) — not the per-execution id (E####, which is the
	 * row's `id`/`display_id`). This is what links to the strategy's detail page. */
	function strategyId(t: ForvenTrade): string {
		return String(t.strategy_id ?? t.strategy ?? '').trim();
	}

	function fmtPrice(value: number | null): string {
		if (value === null) return '—';
		if (Math.abs(value) >= 1) return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
		return value.toLocaleString(undefined, { maximumFractionDigits: 8 });
	}
	function fmtUsd(value: number | null, signed = true): string {
		if (value === null) return '—';
		const abs = Math.abs(value).toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2
		});
		if (!signed) return `$${abs}`;
		return `${value >= 0 ? '+' : '-'}$${abs}`;
	}
	function fmtUsdCompact(value: number | null): string {
		if (value === null) return '—';
		const abs = Math.abs(value);
		if (abs >= 1000) return `$${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k`;
		return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
	}
	function fmtPct(value: number | null, digits = 2): string {
		if (value === null) return '—';
		return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`;
	}
	function fmtRatio(value: number | null): string {
		if (value === null) return '—';
		if (!Number.isFinite(value)) return '∞';
		return value.toFixed(2);
	}
	function fmtDuration(ms: number | null): string {
		if (ms === null) return '—';
		const m = Math.floor(ms / 60000);
		if (m < 60) return `${m}m`;
		const h = Math.floor(m / 60);
		const rem = m % 60;
		if (h < 24) return `${h}h${rem ? `${rem}m` : ''}`;
		const d = Math.floor(h / 24);
		const hr = h % 24;
		return `${d}d${hr ? `${hr}h` : ''}`;
	}
	function fmtTs(value: string | null | undefined): string {
		if (!value) return '—';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '—';
		return `${date.toLocaleDateString(undefined, { month: '2-digit', day: '2-digit' })} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function pnlClass(value: number | null): string {
		if (value === null || value === 0) return 'text-[#777]';
		return value > 0 ? 'text-emerald-400' : 'text-red-400';
	}
	function statusClass(status: unknown): string {
		const t = String(status ?? '').toUpperCase();
		if (t === 'OPEN') return 'text-white';
		if (t === 'FAILED') return 'text-red-400';
		if (t === 'CLOSED') return 'text-[#888]';
		return 'text-[#555]';
	}

	$: showingFrom = total === 0 ? 0 : offset + 1;
	$: showingTo = Math.min(offset + trades.length, total);
	$: winRatePct = stats && stats.win_rate !== null ? stats.win_rate * 100 : null;
	// Assets seen on the current page — a convenience datalist for the asset filter.
	$: assetOptions = Array.from(
		new Set(trades.map((t) => String(t.asset ?? '').toUpperCase()).filter(Boolean))
	).sort();
</script>

<div class="flex flex-col h-full">
	<div class="panel-header">
		<span>All Trades</span>
		<button class="terminal-button text-xs" on:click={() => applyFilters()} disabled={loading}>
			{loading ? 'Loading…' : 'Refresh'}
		</button>
	</div>

	<!-- Stat bar -->
	<div class="flex flex-wrap items-stretch gap-x-5 gap-y-1 px-4 py-2 border-b border-[#222] text-xs">
		{#if stats}
			<div class="stat">
				<span class="stat-label">Trades</span>
				<span class="stat-value">{stats.total}</span>
				<span class="stat-sub">{stats.closed_count} closed · {stats.open_count} open</span>
			</div>
			<div class="stat">
				<span class="stat-label">Win rate</span>
				<span class="stat-value">{winRatePct === null ? '—' : `${winRatePct.toFixed(1)}%`}</span>
				<span class="stat-sub">{stats.wins}W / {stats.losses}L</span>
			</div>
			<div class="stat">
				<span class="stat-label">Profit factor</span>
				<span class="stat-value">{fmtRatio(stats.profit_factor)}</span>
			</div>
			<div class="stat">
				<span class="stat-label">Net P&L</span>
				<span class="stat-value {pnlClass(stats.net_pnl)}">{fmtUsd(stats.net_pnl)}</span>
				<span class="stat-sub">realized</span>
			</div>
			<div class="stat">
				<span class="stat-label">Avg win / loss</span>
				<span class="stat-value">
					<span class="text-emerald-400">{fmtUsd(stats.avg_win)}</span>
					<span class="text-[#555]"> / </span>
					<span class="text-red-400">{fmtUsd(stats.avg_loss)}</span>
				</span>
				<span class="stat-sub">expectancy {fmtUsd(stats.expectancy)}</span>
			</div>
			<div class="stat">
				<span class="stat-label">Best / worst</span>
				<span class="stat-value">
					<span class="text-emerald-400">{fmtUsd(stats.best)}</span>
					<span class="text-[#555]"> / </span>
					<span class="text-red-400">{fmtUsd(stats.worst)}</span>
				</span>
			</div>
			<div class="stat">
				<span class="stat-label">Open exposure</span>
				<span class="stat-value">{fmtUsd(stats.open_exposure, false)}</span>
				<span class="stat-sub">{stats.open_count} position(s)</span>
			</div>
		{:else}
			<span class="text-[#555]">No stats</span>
		{/if}
	</div>

	<!-- Filters -->
	<div class="flex flex-col gap-2 px-4 py-2 border-b border-[#222] text-xs">
		<div class="flex items-center gap-2">
			{#each STATUSES as s}
				<button
					class="px-3 py-1 border uppercase tracking-wide {statusFilter === s
						? 'border-white text-white'
						: 'border-[#333] text-[#666] hover:text-[#888]'}"
					on:click={() => setStatus(s)}
				>
					{s}
				</button>
			{/each}
			<span class="ml-auto text-[#666]">{showingFrom}–{showingTo} of {total}</span>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			<input
				class="filter-input w-24"
				placeholder="Asset"
				list="asset-options"
				bind:value={assetFilter}
				on:change={() => applyFilters()}
			/>
			<datalist id="asset-options">
				{#each assetOptions as a}<option value={a}></option>{/each}
			</datalist>
			<input
				class="filter-input w-32"
				placeholder="Strategy"
				bind:value={strategyFilter}
				on:change={() => applyFilters()}
			/>
			<select class="filter-input" bind:value={directionFilter} on:change={() => applyFilters()}>
				<option value="">Any side</option>
				<option value="long">Long</option>
				<option value="short">Short</option>
			</select>
			<select class="filter-input" bind:value={execTypeFilter} on:change={() => applyFilters()}>
				<option value="">Any type</option>
				<option value="paper">Paper</option>
				<option value="live">Live</option>
			</select>
			<label class="flex items-center gap-1 text-[#666]">
				from <input class="filter-input" type="date" bind:value={fromDate} on:change={() => applyFilters()} />
			</label>
			<label class="flex items-center gap-1 text-[#666]">
				to <input class="filter-input" type="date" bind:value={toDate} on:change={() => applyFilters()} />
			</label>
			<input
				class="filter-input w-40"
				placeholder="Search id / strategy…"
				bind:value={search}
				on:input={onSearchInput}
			/>
			<button class="terminal-button text-xs" on:click={clearFilters}>Clear</button>
		</div>
	</div>

	{#if error}
		<div class="px-4 py-2 text-xs text-red-400 border-b border-red-900 bg-red-500/5">{error}</div>
	{/if}
	{#if notice}
		<div class="px-4 py-2 text-xs text-emerald-400 border-b border-emerald-900 bg-emerald-500/5">{notice}</div>
	{/if}

	<!-- Blotter -->
	<div class="flex-1 overflow-auto">
		<table class="w-full text-[11px]">
			<thead class="text-[#666] border-b border-[#222] bg-[#0a0a0a] sticky top-0 z-10">
				<tr>
					{#each COLUMNS as col}
						<th
							class="px-2 py-2 {col.align === 'right' ? 'text-right' : 'text-left'} {col.sortKey
								? 'cursor-pointer select-none hover:text-white'
								: ''}"
							on:click={() => toggleSort(col)}
						>
							{col.label}{#if col.sortKey && sort === col.sortKey}<span class="text-white"
									>{sortDir === 'asc' ? ' ▲' : ' ▼'}</span
								>{/if}
						</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#if trades.length > 0}
					{#each trades as trade (trade.id)}
						{@const sd = signalData(trade)}
						{@const usd = effectiveUsd(trade, $forvenLivePrices)}
						{@const pct = effectivePct(trade, $forvenLivePrices)}
						{@const rowOpen = isOpen(trade)}
						<tr class="border-b border-[#111] hover:bg-[#111]">
							<td class="px-2 py-1.5 text-center">
								<button
									class="text-[#555] hover:text-white"
									on:click={() => toggleExpand(trade.id)}
									aria-label="Toggle detail"
								>
									{expandedId === trade.id ? '▾' : '▸'}
								</button>
							</td>
							<td class="px-2 py-1.5 text-[#888] whitespace-nowrap">{fmtTs(trade.opened_at)}</td>
							<td class="px-2 py-1.5 font-mono">
								{#if strategyId(trade)}
									<a
										class="text-white hover:underline"
										href={tradeHref(trade)}
										title="Hop into {strategyId(trade)}'s trade on the Trades page"
									>{strategyId(trade)}</a>
								{:else}
									<span class="text-[#666]">—</span>
								{/if}
							</td>
							<td class="px-2 py-1.5 text-white font-bold">{String(trade.asset ?? '—').toUpperCase()}</td>
							<td class="px-2 py-1.5 font-bold {String(trade.direction ?? '').toLowerCase() === 'short' ? 'text-red-400' : 'text-emerald-400'}">
								{String(trade.direction ?? '—').toUpperCase()}
							</td>
							<td class="px-2 py-1.5">
								<span
									class="px-1.5 py-0.5 border text-[10px] uppercase {String(trade.execution_type ?? '').toLowerCase() === 'live'
										? 'border-red-900 text-red-400'
										: 'border-[#333] text-[#888]'}"
								>
									{String(trade.execution_type ?? '—')}
								</span>
							</td>
							<td class="px-2 py-1.5 font-bold {statusClass(trade.status)}">{String(trade.status ?? '—').toUpperCase()}</td>
							<td class="px-2 py-1.5 text-right text-[#888]">{fmtPrice(toNumber(trade.fill_entry_price) ?? toNumber(trade.entry_price))}</td>
							<td class="px-2 py-1.5 text-right text-[#888]">{fmtPrice(toNumber(trade.fill_exit_price) ?? toNumber(trade.exit_price))}</td>
							<td class="px-2 py-1.5 text-right text-[#888]">{fmtUsd(notional(trade), false)}</td>
							<td class="px-2 py-1.5 text-right font-bold {pnlClass(usd)}">
								{fmtUsd(usd)}{#if rowOpen && usd !== null}<span class="text-[#555] text-[9px] ml-0.5">●</span>{/if}
							</td>
							<td class="px-2 py-1.5 text-right font-bold {pnlClass(pct)}">{fmtPct(pct)}</td>
							<td class="px-2 py-1.5 text-right text-[#888] whitespace-nowrap">{fmtDuration(durationMs(trade))}</td>
							<td class="px-2 py-1.5 text-right">
								{#if rowOpen}
									<button
										class="terminal-button-danger text-xs py-0.5"
										on:click={() => handleMarkFailed(trade)}
										disabled={busyTradeId === trade.id}
									>
										{busyTradeId === trade.id ? '…' : 'Mark Failed'}
									</button>
								{:else}
									<span class="text-[#444]">—</span>
								{/if}
							</td>
						</tr>
						{#if expandedId === trade.id}
							<tr class="bg-[#0a0a0a] border-b border-[#111]">
								<td colspan={COLUMNS.length} class="px-6 py-3">
									<div class="grid grid-cols-2 md:grid-cols-4 gap-x-8 gap-y-1.5 text-[11px]">
										<div class="detail"><span class="detail-k">Trade ID</span><span class="detail-v font-mono">{trade.id ?? '—'}</span></div>
										<div class="detail"><span class="detail-k">Strategy</span><span class="detail-v font-mono">{trade.strategy_id ?? trade.strategy ?? '—'}{trade.strategy_name ? ` · ${trade.strategy_name}` : ''}</span></div>
										<div class="detail"><span class="detail-k">Source</span><span class="detail-v">{trade.source ?? '—'}{trade.book ? ` · book ${trade.book}` : ''}</span></div>
										<div class="detail"><span class="detail-k">Timeframe</span><span class="detail-v">{trade.timeframe ?? '—'}</span></div>

										<div class="detail"><span class="detail-k">Size</span><span class="detail-v">{toNumber(trade.size) === null ? '—' : trade.size} units</span></div>
										<div class="detail"><span class="detail-k">Leverage</span><span class="detail-v">{toNumber(trade.leverage) === null ? '—' : `${trade.leverage}×`}</span></div>
										<div class="detail"><span class="detail-k">Notional</span><span class="detail-v">{fmtUsd(notional(trade), false)}</span></div>
										<div class="detail"><span class="detail-k">R-multiple</span><span class="detail-v {pnlClass(rMultiple(trade))}">{fmtRatio(rMultiple(trade))}{rMultiple(trade) === null ? '' : 'R'}</span></div>

										<div class="detail"><span class="detail-k">Stop loss</span><span class="detail-v">{fmtPrice(toNumber(sd.stop_loss_price ?? sd.stop_loss))}</span></div>
										<div class="detail"><span class="detail-k">Take profit</span><span class="detail-v">{fmtPrice(toNumber(sd.take_profit_price ?? sd.take_profit))}</span></div>
										<div class="detail"><span class="detail-k">Close reason</span><span class="detail-v">{String(sd.close_reason ?? '—')}</span></div>
										<div class="detail"><span class="detail-k">Close source</span><span class="detail-v">{String(sd.close_price_source ?? '—')}</span></div>

										<div class="detail"><span class="detail-k">Entry signal / fill</span><span class="detail-v">{fmtPrice(toNumber(trade.signal_entry_price))} / {fmtPrice(toNumber(trade.fill_entry_price))}</span></div>
										<div class="detail"><span class="detail-k">Exit signal / fill</span><span class="detail-v">{fmtPrice(toNumber(trade.signal_exit_price))} / {fmtPrice(toNumber(trade.fill_exit_price))}</span></div>
										<div class="detail"><span class="detail-k">Slippage in / out</span><span class="detail-v">{toNumber(trade.entry_slippage_bps) ?? '—'} / {toNumber(trade.exit_slippage_bps) ?? '—'} bps</span></div>
										<div class="detail"><span class="detail-k">Fees</span><span class="detail-v">{toNumber(trade.fees_pct) === null ? '—' : `${(Number(trade.fees_pct) * 100).toFixed(3)}%`}</span></div>

										<div class="detail"><span class="detail-k">Opened</span><span class="detail-v">{fmtTs(trade.opened_at)}</span></div>
										<div class="detail"><span class="detail-k">Closed</span><span class="detail-v">{fmtTs(trade.closed_at)}</span></div>
										<div class="detail"><span class="detail-k">Held</span><span class="detail-v">{fmtDuration(durationMs(trade))}</span></div>
										<div class="detail"><span class="detail-k">Net P&L %</span><span class="detail-v {pnlClass(toNumber(trade.net_pnl_pct))}">{toNumber(trade.net_pnl_pct) === null ? '—' : `${(Number(trade.net_pnl_pct) * 100).toFixed(3)}%`}</span></div>
									</div>
								</td>
							</tr>
						{/if}
					{/each}
				{:else}
					<tr>
						<td colspan={COLUMNS.length} class="py-8 text-center text-[#555] text-xs">
							{loading ? 'Loading…' : 'No trades match these filters'}
						</td>
					</tr>
				{/if}
			</tbody>
		</table>
	</div>

	<!-- Pagination -->
	<div class="flex items-center justify-between gap-2 px-4 py-2 border-t border-[#222] text-xs">
		<div class="flex items-center gap-2">
			<button class="terminal-button text-xs" on:click={prevPage} disabled={offset === 0 || loading}>Prev</button>
			<button
				class="terminal-button text-xs"
				on:click={nextPage}
				disabled={offset + pageSize >= total || loading}
			>
				Next
			</button>
		</div>
		<span class="text-[#666]">{showingFrom}–{showingTo} of {total}</span>
		<label class="flex items-center gap-1 text-[#666]">
			rows
			<select class="filter-input" value={pageSize} on:change={changePageSize}>
				<option value={50}>50</option>
				<option value={100}>100</option>
				<option value={200}>200</option>
			</select>
		</label>
	</div>
</div>

<style>
	.stat {
		display: flex;
		flex-direction: column;
		justify-content: center;
		line-height: 1.2;
	}
	.stat-label {
		color: #6b7280;
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.stat-value {
		color: #e5e7eb;
		font-weight: 700;
	}
	.stat-sub {
		color: #4b5563;
		font-size: 10px;
	}
	.filter-input {
		background: #0a0a0a;
		border: 1px solid #333;
		color: #d1d5db;
		padding: 0.2rem 0.4rem;
		font-size: 11px;
	}
	.filter-input:focus {
		outline: none;
		border-color: #555;
	}
	.detail {
		display: flex;
		flex-direction: column;
	}
	.detail-k {
		color: #6b7280;
		font-size: 10px;
		text-transform: uppercase;
	}
	.detail-v {
		color: #d1d5db;
	}
</style>
