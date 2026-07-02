<script lang="ts">
	import { onMount, createEventDispatcher } from 'svelte';
	import { getCoverage, backfillGaps, type DataCoverage, type DataCoverageEntry } from '$lib/api/data';

	const dispatch = createEventDispatcher<{ view: { symbol: string; timeframe: string } }>();

	let coverage: DataCoverage = {};
	let loading = true;
	let error: string | null = null;
	let backfilling: string | null = null; // "symbol|tf" currently in flight
	let backfillMsg: string | null = null;
	let stream: 'ohlcv' | 'oi' | 'basis' = 'ohlcv';

	// Backend coverage keys are "{stream}/{tf}" (e.g. "ohlcv/1h", "oi/15m"); funding
	// is a single non-timeframed series and is shown in Source health instead.
	const TF_ORDER = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '8h', '12h', '1d', '3d', '1w'];
	const HIDDEN_TFS = new Set(['6h']); // timeframes we don't trade — hidden from the matrix
	const STREAM_LABEL: Record<string, string> = { ohlcv: 'Price (OHLCV)', oi: 'Open interest', basis: 'Basis (premium)' };

	$: symbols = Object.keys(coverage).sort();

	$: availableStreams = (() => {
		const present = new Set<string>();
		for (const sym of Object.keys(coverage)) {
			for (const key of Object.keys(coverage[sym] || {})) {
				const slash = key.indexOf('/');
				if (slash > 0) present.add(key.slice(0, slash));
			}
		}
		return ['ohlcv', 'oi', 'basis'].filter((s) => present.has(s));
	})();

	$: timeframes = (() => {
		const prefix = `${stream}/`;
		const present = new Set<string>();
		for (const sym of Object.keys(coverage)) {
			for (const key of Object.keys(coverage[sym] || {})) {
				if (key.startsWith(prefix)) present.add(key.slice(prefix.length));
			}
		}
		const ordered = TF_ORDER.filter((tf) => present.has(tf));
		const extras = [...present].filter((tf) => !TF_ORDER.includes(tf)).sort();
		return [...ordered, ...extras].filter((tf) => !HIDDEN_TFS.has(tf));
	})();

	function entryFor(symbol: string, tf: string): DataCoverageEntry | undefined {
		return coverage[symbol]?.[`${stream}/${tf}`];
	}

	function tfHours(tf: string): number {
		const m = /^(\d+)(m|h|d|w)$/.exec(tf);
		if (!m) return 1;
		const n = Number(m[1]);
		return n * (m[2] === 'm' ? 1 / 60 : m[2] === 'h' ? 1 : m[2] === 'd' ? 24 : 24 * 7);
	}

	// Hours since the last stored bar. Prefer the precise ISO `to_ts`; fall back to the
	// date-only `to` (midnight UTC) for older payloads that don't carry it.
	function hoursBehind(entry: DataCoverageEntry | undefined): number | null {
		const iso = entry?.to_ts ?? (entry?.to ? `${entry.to}T00:00:00Z` : undefined);
		if (!iso) return null;
		const t = Date.parse(iso);
		if (Number.isNaN(t)) return null;
		return Math.max(0, (Date.now() - t) / 3_600_000);
	}

	type Fresh = 'current' | 'behind' | 'stale' | 'missing';
	function freshness(tf: string, entry: DataCoverageEntry | undefined): Fresh {
		if (!entry || !entry.rows) return 'missing';
		const h = hoursBehind(entry);
		if (h == null) return 'behind';
		// Threshold scales with the bar interval. The latest CLOSED bar sits up to one
		// interval behind "now", so allow ~2 bars before flagging behind, ~8 before stale.
		const iv = tfHours(tf);
		if (h <= iv * 2) return 'current';
		if (h <= iv * 8) return 'behind';
		return 'stale';
	}

	function ageLabel(entry: DataCoverageEntry | undefined): string {
		const h = hoursBehind(entry);
		if (h == null) return 'unknown';
		if (h < 1) return `${Math.round(h * 60)}m ago`;
		if (h < 48) return `${h.toFixed(1)}h ago`;
		return `${(h / 24).toFixed(1)}d ago`;
	}

	const FRESH_CLASS: Record<Fresh, string> = {
		current: 'border border-emerald-900 bg-emerald-500/10 text-emerald-400',
		behind: 'border border-yellow-900 bg-yellow-500/10 text-yellow-400',
		stale: 'border border-red-900 bg-red-500/10 text-red-400',
		missing: 'border border-[#1a1a1a] bg-[#0a0a0a] text-[#555]'
	};

	function cellLabel(entry: DataCoverageEntry | undefined): string {
		if (!entry?.rows) return '·';
		return entry.rows > 999 ? `${Math.round(entry.rows / 1000)}k` : String(entry.rows);
	}

	function cellTitle(symbol: string, tf: string, entry: DataCoverageEntry | undefined): string {
		if (!entry || !entry.rows) return `${symbol} ${tf} (${STREAM_LABEL[stream]}): not collected`;
		const action = stream === 'ohlcv' ? '\nclick to fill gaps & update to the latest bar' : '';
		return `${symbol} ${tf} · ${STREAM_LABEL[stream]}\n${entry.rows.toLocaleString()} bars · ${entry.from} → ${entry.to}\nlatest bar: ${ageLabel(entry)} · ${freshness(tf, entry)}${action}`;
	}

	// Transient backend blips (watchdog restarts, startup races) should not
	// leave the panel dead until a full page reload — retry briefly first.
	const RETRY_DELAYS_MS = [1000, 3000, 8000];
	let retryTimer: ReturnType<typeof setTimeout> | null = null;

	async function load(attempt = 0) {
		if (retryTimer) {
			clearTimeout(retryTimer);
			retryTimer = null;
		}
		loading = true;
		if (attempt === 0) error = null;
		try {
			coverage = await getCoverage();
			error = null;
			loading = false;
		} catch (e) {
			if (attempt < RETRY_DELAYS_MS.length) {
				retryTimer = setTimeout(() => void load(attempt + 1), RETRY_DELAYS_MS[attempt]);
				return; // stay in the loading state while a retry is pending
			}
			error = e instanceof Error ? e.message : 'Failed to load coverage';
			loading = false;
		}
	}

	async function doBackfill(symbol: string, tf: string) {
		if (stream !== 'ohlcv') return; // gap backfill applies to OHLCV only
		const key = `${symbol}|${tf}`;
		if (backfilling) return;
		const entry = entryFor(symbol, tf);
		if (!entry?.rows) {
			backfillMsg = `${symbol} ${tf}: no series to backfill — download it first from the Datasets tab.`;
			return;
		}
		backfilling = key;
		backfillMsg = `${symbol} ${tf}: backfilling…`;
		try {
			const r = await backfillGaps(symbol, tf);
			const parts: string[] = [];
			if (r.gaps_filled > 0) parts.push(`filled ${r.gaps_filled}/${r.gaps_found} gap${r.gaps_found === 1 ? '' : 's'}`);
			if (r.bars_added > 0) parts.push(`+${r.bars_added.toLocaleString()} bars`);
			if (r.extended_to_now) parts.push('brought current');
			if (r.no_recent_data) {
				backfillMsg = `${symbol} ${tf}: ${[...parts, 'no newer data available — the symbol may be delisted (e.g. MATIC → POL)'].join(', ')}`;
			} else {
				backfillMsg = parts.length
					? `${symbol} ${tf}: ${parts.join(', ')} ✓`
					: `${symbol} ${tf}: already complete & current ✓`;
			}
			await load();
		} catch (e) {
			backfillMsg = `${symbol} ${tf}: backfill failed — ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			backfilling = null;
		}
	}

	// Open the data viewer for a symbol — prefer the first timeframe with OHLCV rows
	// (the viewer opens on the OHLCV tab), then the current stream, then the first tf,
	// so the default view lands on real data regardless of the matrix's stream toggle.
	function viewSymbol(symbol: string) {
		const hasRows = (key: string) => (coverage[symbol]?.[key]?.rows ?? 0) > 0;
		const tf =
			timeframes.find((t) => hasRows(`ohlcv/${t}`)) ??
			timeframes.find((t) => hasRows(`${stream}/${t}`)) ??
			timeframes[0] ??
			'1h';
		dispatch('view', { symbol, timeframe: tf });
	}

	onMount(() => {
		void load();
		return () => {
			if (retryTimer) clearTimeout(retryTimer);
		};
	});
</script>

<div class="terminal-card">
	<div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#1a1a1a] px-4 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Coverage matrix</h2>
		<div class="flex flex-wrap items-center gap-3 text-[11px] text-[#666]">
			{#if availableStreams.length > 1}
				<div class="flex border border-[#333]">
					{#each availableStreams as s}
						<button
							class="px-2 py-0.5 text-[10px] uppercase tracking-wider transition-colors {stream === s ? 'bg-[#222] text-white' : 'text-[#666] hover:text-white'}"
							on:click={() => (stream = s as 'ohlcv' | 'oi' | 'basis')}
						>{STREAM_LABEL[s]}</button>
					{/each}
				</div>
			{/if}
			<span class="flex items-center gap-1"><span class="inline-block h-3 w-3 border border-emerald-900 bg-emerald-500/10"></span> current</span>
			<span class="flex items-center gap-1"><span class="inline-block h-3 w-3 border border-yellow-900 bg-yellow-500/10"></span> behind</span>
			<span class="flex items-center gap-1"><span class="inline-block h-3 w-3 border border-red-900 bg-red-500/10"></span> stale</span>
			<span class="flex items-center gap-1"><span class="inline-block h-3 w-3 border border-[#1a1a1a] bg-[#0a0a0a]"></span> not collected</span>
			<button class="terminal-button ml-1 px-2 py-1 text-[10px]" on:click={() => void load()} disabled={loading}>
				{loading ? '…' : 'Refresh'}
			</button>
		</div>
	</div>
	<p class="px-4 py-3 text-[11px] text-[#555]">
		Each cell shows <span class="text-[#888]">bars stored</span> for that symbol &amp; timeframe; the
		<span class="text-[#888]">colour</span> is how current the latest bar is.
		{#if stream === 'ohlcv'}Click any cell to fill its gaps and update it to the latest bar (red → green).{:else}Switch to Price to backfill gaps.{/if}
		Click a <span class="text-[#888]">symbol name</span> to open its data viewer.
	</p>

	{#if error}
		<div class="flex items-center justify-between gap-3 rounded bg-red-900/40 p-2 text-xs text-red-200">
			<span>{error}</span>
			<button
				class="shrink-0 rounded border border-red-700/70 px-2 py-0.5 text-red-100 hover:bg-red-900/60"
				on:click={() => void load()}
			>Retry</button>
		</div>
	{:else if loading && symbols.length === 0}
		<div class="text-xs text-gray-500">Loading coverage…</div>
	{:else if symbols.length === 0}
		<div class="text-xs text-gray-500">No datasets yet — download some from the Datasets tab.</div>
	{:else if timeframes.length === 0}
		<div class="text-xs text-gray-500">No {STREAM_LABEL[stream]} series stored yet.</div>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full border-separate border-spacing-1 text-xs">
				<thead>
					<tr>
						<th class="w-px whitespace-nowrap px-2 py-1 text-left font-medium text-gray-400">symbol</th>
						{#each timeframes as tf}
							<th class="px-2 py-1 text-center font-medium text-gray-400">{tf}</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each symbols as symbol}
						<tr>
							<td class="w-px whitespace-nowrap px-2 py-1 pr-4 font-mono">
							<button
								class="text-[#aaa] hover:text-white hover:underline"
								on:click={() => viewSymbol(symbol)}
								title="View {symbol} data"
							>{symbol}</button>
						</td>
							{#each timeframes as tf}
								{@const entry = coverage[symbol]?.[`${stream}/${tf}`]}
								<td class="p-0 text-center">
									{#if stream === 'ohlcv'}
										<button
											class="h-7 w-full rounded {FRESH_CLASS[freshness(tf, entry)]} hover:ring-1 hover:ring-cyan-400/60 disabled:opacity-50"
											title={cellTitle(symbol, tf, entry)}
											disabled={backfilling === `${symbol}|${tf}`}
											on:click={() => doBackfill(symbol, tf)}
										>
											{#if backfilling === `${symbol}|${tf}`}…{:else}{cellLabel(entry)}{/if}
										</button>
									{:else}
										<div class="flex h-7 w-full items-center justify-center rounded {FRESH_CLASS[freshness(tf, entry)]}" title={cellTitle(symbol, tf, entry)}>
											{cellLabel(entry)}
										</div>
									{/if}
								</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	{#if backfillMsg}
		<div class="mt-2 rounded border border-[#222] bg-[#111] p-2 text-xs text-gray-200">{backfillMsg}</div>
	{/if}
</div>
