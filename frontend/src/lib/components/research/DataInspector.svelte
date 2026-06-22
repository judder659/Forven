<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import {
		fetchData, uploadCSV, previewCSV,
		shouldUseBackgroundIngestion,
		getDataSources,
		getStreamHealth, triggerCollect,
		triggerBackfill, getBackfillStatus,
	} from '$lib/api';
	import type { Dataset, DataQualityExtended, DataSource, CSVPreview, StreamsResponse, BackfillStatus } from '$lib/api';
	import SymbolSearch from '$lib/components/research/SymbolSearch.svelte';
	import { ORDERED_TIMEFRAME_VALUES } from '$lib/config/timeframes';
	import {
		dataFetchState,
		startDataFetchTask,
		updateDataFetchProgress,
		completeDataFetchSuccess,
		completeDataFetchError,
		setDataFetchAbortController,
		saveDataFetchFormConfig,
		getDataFetchFormConfig,
	} from '$lib/stores/dataFetch';

	export let mode: 'details' | 'fetch' = 'details';
	export let selectedDataset: Dataset | null = null;
	export let quality: DataQualityExtended | null = null;
	export let qualityLoading = false;

	// Stream health state (F1, F3, F4, F5)
	let streams: StreamsResponse | null = null;
	let streamsLoading = false;
	let collectingStream: string | null = null;
	let collectCooldowns: Record<string, number> = {};
	// Per-stream result of the last manual Collect, so a click shows what happened
	// instead of silently doing nothing.
	let collectResult: Record<string, { ok: boolean; msg: string } | undefined> = {};

	async function loadStreams(symbol: string) {
		streamsLoading = true;
		streams = null;
		try {
			streams = await getStreamHealth(symbol);
		} catch {
			streams = null;
		} finally {
			streamsLoading = false;
		}
	}

	async function collectNow(symbol: string, stream: string) {
		if (collectingStream || collectCooldowns[stream]) return;
		collectingStream = stream;
		collectResult = { ...collectResult, [stream]: undefined };
		try {
			const res = await triggerCollect(symbol, stream);
			const n = Number(res?.rows_added ?? 0);
			collectResult = {
				...collectResult,
				[stream]: {
					ok: true,
					msg: n > 0 ? `+${n.toLocaleString()} new row${n === 1 ? '' : 's'}` : 'up to date — no new rows'
				}
			};
			collectCooldowns = { ...collectCooldowns, [stream]: 1 };
			setTimeout(() => {
				collectCooldowns = { ...collectCooldowns, [stream]: 0 };
			}, 60_000);
			await loadStreams(symbol);
		} catch (e) {
			// Surface the outcome (incl. the expected "Debounced — try again in Ns")
			// instead of swallowing it, so a click never looks like it did nothing.
			collectResult = {
				...collectResult,
				[stream]: { ok: false, msg: e instanceof Error ? e.message : 'Collection failed' }
			};
		} finally {
			collectingStream = null;
		}
	}

	$: if (mode === 'details' && selectedDataset) {
		loadStreams(selectedDataset.symbol);
	}

	// Backfill state
	let backfillStatus: BackfillStatus | null = null;
	let backfillLoading = false;
	let backfillPollTimer: ReturnType<typeof setInterval> | null = null;

	async function loadBackfillStatus() {
		try {
			backfillStatus = await getBackfillStatus();
			if (backfillStatus?.running) {
				if (!backfillPollTimer) {
					backfillPollTimer = setInterval(() => void loadBackfillStatus(), 3000);
				}
			} else {
				if (backfillPollTimer) { clearInterval(backfillPollTimer); backfillPollTimer = null; }
			}
		} catch {
			backfillStatus = null;
		}
	}

	async function startBackfill(symbol?: string) {
		if (backfillLoading || backfillStatus?.running) return;
		backfillLoading = true;
		try {
			await triggerBackfill(symbol);
			await loadBackfillStatus();
		} catch {
			// ignore — status will reflect error
		} finally {
			backfillLoading = false;
		}
	}

	$: if (mode === 'details' && selectedDataset) {
		void loadBackfillStatus();
	}

	function streamStatusColor(status: string): string {
		if (status === 'live') return 'text-green-400';
		if (status === 'accumulating') return 'text-yellow-400';
		return 'text-red-500';
	}

	function streamStatusLabel(status: string): string {
		if (status === 'live') return 'LIVE';
		if (status === 'accumulating') return 'ACCUMULATING';
		return 'NO DATA';
	}

	const dispatch = createEventDispatcher<{
		refresh: void;
		fetched: { dataset: Dataset };
		viewSeries: { symbol: string; timeframe: string };
	}>();

	// Fetch State
	let dataSources: DataSource[] = [];
	let selectedSource = 'ccxt';
	let fetchSymbol = 'BTC/USDT';
	let fetchTimeframe = '1h';
	let fetchExchange = 'binance';
	let fetchLimit = 1000;
	let fetching = false;
	let fetchError: string | null = null;
	let fetchSince = '';
	let fetchUntil = '';
	let fetchAllAvailable = false;
	let fetchAllTimeframes = false;

	// CSV State
	let csvFile: File | null = null;
	let csvSymbol = '';
	let csvTimeframe = '1d';
	let csvPreview: CSVPreview | null = null;

	const exchanges = ['binance', 'bybit', 'okx', 'coinbase', 'kraken'];
	const timeframes = ORDERED_TIMEFRAME_VALUES;

	// Carry the currently-selected dataset's ticker into the fetch form so that
	// "Download Data" on a selected ticker pre-populates it instead of defaulting
	// to BTC/USDT. Crypto datasets fill the ccxt symbol/exchange; equity ones fill
	// the Yahoo symbol.
	function applyDatasetToFetchForm(ds: Dataset): void {
		const market = String(ds.market_type ?? ds.asset_class ?? '').toLowerCase();
		const src = String(ds.source ?? '').toLowerCase();
		const isEquity = market === 'equity' || market === 'stock' || market === 'etf' || src === 'polygon';
		if (isEquity) {
			selectedSource = 'polygon';
			fetchSymbol = ds.symbol;
			if (ds.timeframe && timeframes.includes(ds.timeframe)) fetchTimeframe = ds.timeframe;
		} else {
			selectedSource = 'ccxt';
			if (exchanges.includes(src)) fetchExchange = src;
			fetchSymbol = ds.symbol;
			if (ds.timeframe && timeframes.includes(ds.timeframe)) fetchTimeframe = ds.timeframe;
		}
	}

	// Fire the pre-fill only on the details -> fetch transition (entering the form),
	// so it never clobbers what the user is typing once the form is open.
	let _prevInspectorMode: 'details' | 'fetch' = mode;
	$: if (mode !== _prevInspectorMode) {
		if (mode === 'fetch' && selectedDataset) applyDatasetToFetchForm(selectedDataset);
		_prevInspectorMode = mode;
	}

	// Restore last-used form config on mount
	import { onMount, onDestroy } from 'svelte';
	onDestroy(() => {
		if (backfillPollTimer) { clearInterval(backfillPollTimer); backfillPollTimer = null; }
	});
	onMount(async () => {
		const saved = getDataFetchFormConfig();
		if (saved) {
			// 'yahoo' is no longer a supported source; fall back so a stale saved
			// config can't select a tab that doesn't render.
			selectedSource = saved.source === 'yahoo' ? 'ccxt' : saved.source;
			fetchSymbol = saved.symbol;
			fetchTimeframe = saved.timeframe;
			fetchExchange = saved.exchange;
			fetchLimit = saved.limit;
			fetchSince = saved.since;
			fetchUntil = saved.until;
			fetchAllAvailable = saved.allAvailable;
			fetchAllTimeframes = saved.allTimeframes;
		}

		try {
			dataSources = await getDataSources();
		} catch (e) {
			console.error(e);
		}
	});

	// Reactive: mirror the store's running state into the local `fetching` flag
	$: fetching = $dataFetchState.status === 'running';

	// 10 minutes for large all-available fetches
	const FETCH_TIMEOUT_MS = 600_000;

	function handleFetch() {
		// If the store is stuck in 'running' from a timed-out/failed fetch, force-reset it
		if ($dataFetchState.status === 'running') {
			completeDataFetchError('Previous fetch reset');
		}

		if (selectedSource === 'ccxt' || selectedSource === 'binance' || selectedSource === 'polygon') {
			const sinceMs = fetchSince ? new Date(fetchSince).getTime().toString() : undefined;
			const untilMs = fetchUntil ? new Date(fetchUntil).getTime().toString() : undefined;
			const exchange = selectedSource === 'polygon' ? 'polygon' : selectedSource === 'binance' ? 'binance' : fetchExchange;
			const tfs = fetchAllTimeframes ? timeframes : [fetchTimeframe];
			const symbol = fetchSymbol;
			const allAvail = fetchAllAvailable;
			const limit = fetchLimit;
			const useBackgroundIngestion = shouldUseBackgroundIngestion({ allAvailable: allAvail });
			const abortController = useBackgroundIngestion ? new AbortController() : null;

			fetchError = null;
			saveDataFetchFormConfig({
				source: selectedSource, symbol, timeframe: fetchTimeframe,
				exchange, limit, since: fetchSince, until: fetchUntil,
				allAvailable: allAvail, allTimeframes: fetchAllTimeframes,
			});
			setDataFetchAbortController(abortController);
			startDataFetchTask(`${symbol}`, { isBulk: tfs.length > 1 });
			updateDataFetchProgress(`Starting ${tfs[0]}...`);

			// Fire-and-forget: runs even if component unmounts
			(async () => {
				let lastResult: Dataset | null = null;
				try {
					for (let i = 0; i < tfs.length; i++) {
						const tf = tfs[i];
						updateDataFetchProgress(`Downloading ${symbol} ${tf} (${i + 1}/${tfs.length})`);
						lastResult = await fetchData(
							symbol, tf, exchange, limit,
							useBackgroundIngestion ? abortController?.signal : AbortSignal.timeout(FETCH_TIMEOUT_MS),
							sinceMs,
							allAvail,
							untilMs,
							(progress) => updateDataFetchProgress(progress.message)
						);
					}
					completeDataFetchSuccess(`Finished ${symbol} — ${tfs.length} timeframe${tfs.length > 1 ? 's' : ''}`);
					dispatch('refresh');
					if (lastResult) dispatch('fetched', { dataset: lastResult });
					mode = 'details';
				} catch (e) {
					const msg = e instanceof Error ? e.message : 'Fetch failed';
					const status = e instanceof Error && e.name === 'AbortError' ? 'cancelled' : 'error';
					completeDataFetchError(msg, status);
					fetchError = msg;
				} finally {
					setDataFetchAbortController(null);
				}
			})();
		} else {
			fetchError = null;
			const src = selectedSource;
			saveDataFetchFormConfig({
				source: src, symbol: fetchSymbol, timeframe: fetchTimeframe,
				exchange: fetchExchange, limit: fetchLimit, since: fetchSince,
				until: fetchUntil, allAvailable: fetchAllAvailable,
				allTimeframes: fetchAllTimeframes,
			});
			startDataFetchTask(csvSymbol || 'CSV');
			updateDataFetchProgress(`Downloading from ${src}...`);

			(async () => {
				try {
					let result: Dataset;
					if (src === 'csv') {
						if (!csvFile || !csvSymbol) throw new Error('Invalid CSV input');
						result = await uploadCSV(csvFile, csvSymbol, csvTimeframe);
					} else {
						throw new Error('Unknown source');
					}
					completeDataFetchSuccess(`Finished ${result.symbol}`);
					dispatch('refresh');
					dispatch('fetched', { dataset: result });
					mode = 'details';
				} catch (e) {
					const msg = e instanceof Error ? e.message : 'Fetch failed';
					completeDataFetchError(msg);
					fetchError = msg;
				}
			})();
		}
	}

	async function handleCSVSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		if (input.files?.[0]) {
			csvFile = input.files[0];
			try {
				csvPreview = await previewCSV(csvFile);
				if (!csvSymbol) csvSymbol = csvFile.name.replace('.csv', '').toUpperCase();
			} catch (e) {
				console.error(e);
			}
		}
	}
</script>

<div class="h-full flex flex-col bg-[#050505]">
	<div class="panel-header">
		<span>{mode === 'fetch' ? 'Fetch Data' : 'Inspector'}</span>
		{#if mode === 'details'}
			<button class="text-xs text-white hover:text-gray-300" on:click={() => mode = 'fetch'}>+ NEW</button>
		{:else}
			<button class="text-xs text-white hover:text-gray-300" on:click={() => mode = 'details'}>CANCEL</button>
		{/if}
	</div>

	<div class="panel-content p-4 space-y-6">
		{#if mode === 'fetch'}
			<!-- FETCH FORM -->
			<div class="space-y-4">
				<div class="flex border-b border-[#222]">
					{#each dataSources.filter(s => s.available) as source}
						<button
							class="px-3 py-1 text-xs uppercase font-bold transition-colors border-b-2 {selectedSource === source.id ? 'border-white text-white' : 'border-transparent text-gray-500 hover:text-gray-300'}"
							on:click={() => selectedSource = source.id}
						>
							{source.name}
						</button>
					{/each}
				</div>

				{#if selectedSource === 'ccxt' || selectedSource === 'binance' || selectedSource === 'polygon'}
					<div class="space-y-3">
						{#if selectedSource === 'ccxt'}
							<div>
								<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-exchange">Exchange</label>
								<select id="data-inspector-exchange" bind:value={fetchExchange} class="terminal-select">
									{#each exchanges as ex}<option value={ex}>{ex}</option>{/each}
								</select>
							</div>
						{/if}
						<div>
							<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-symbol">Symbol</label>
							{#if selectedSource === 'polygon'}
								<input id="data-inspector-symbol" bind:value={fetchSymbol} class="terminal-input" placeholder="AAPL" />
							{:else}
								<SymbolSearch
									inputId="data-inspector-symbol"
									bind:value={fetchSymbol}
									source={selectedSource === 'binance' ? 'binance' : 'ccxt'}
									exchange={selectedSource === 'ccxt' ? fetchExchange : 'binance'}
									placeholder="Search — e.g. BTC, ETH, SOL/USDT"
								/>
							{/if}
						</div>
						{#if !fetchAllTimeframes}
							<div>
								<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-timeframe">Timeframe</label>
								<select id="data-inspector-timeframe" bind:value={fetchTimeframe} class="terminal-select">
									{#each timeframes as tf}<option value={tf}>{tf}</option>{/each}
								</select>
							</div>
						{/if}
						<label class="flex items-center gap-2 cursor-pointer mt-2 mb-1">
							<input type="checkbox" bind:checked={fetchAllTimeframes} class="rounded border-[#222] bg-[#0b0b0b] text-cyan-500 focus:ring-cyan-500 focus:ring-offset-[#050505]">
							<span class="text-[10px] uppercase text-gray-500">All Timeframes</span>
						</label>
						<label class="flex items-center gap-2 cursor-pointer mt-2 mb-1">
							<input type="checkbox" bind:checked={fetchAllAvailable} class="rounded border-[#222] bg-[#0b0b0b] text-cyan-500 focus:ring-cyan-500 focus:ring-offset-[#050505]">
							<span class="text-[10px] uppercase text-gray-500">All Available Data</span>
						</label>

						{#if !fetchAllAvailable}
							<div>
								<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-bars">Or Limit Bars</label>
								<input id="data-inspector-bars" type="number" bind:value={fetchLimit} class="terminal-input" />
							</div>
							<div class="grid grid-cols-2 gap-2">
								<div>
									<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-since">Since (Optional)</label>
									<input id="data-inspector-since" type="datetime-local" bind:value={fetchSince} class="terminal-input" />
								</div>
								<div>
									<label class="text-[10px] uppercase text-gray-500 block mb-1" for="data-inspector-until">Until (Optional)</label>
									<input id="data-inspector-until" type="datetime-local" bind:value={fetchUntil} class="terminal-input" />
								</div>
							</div>
						{/if}
					</div>
				{:else if selectedSource === 'csv'}
					<div class="space-y-3">
						<input type="file" accept=".csv" on:change={handleCSVSelect} class="text-xs text-gray-400" />
						<input bind:value={csvSymbol} class="terminal-input" placeholder="Symbol Name" />
					</div>
				{/if}

				<button
					class="w-full terminal-button-primary mt-4"
					disabled={fetching}
					on:click={handleFetch}
				>
					{fetching ? 'Fetching...' : 'Fetch Data'}
				</button>

				{#if $dataFetchState.status === 'running' && $dataFetchState.progress}
					<div class="text-xs text-red-400 mt-2 font-mono animate-pulse">
						{$dataFetchState.progress}
					</div>
				{/if}

				{#if $dataFetchState.status === 'success' && $dataFetchState.message}
					<div class="text-xs text-green-400 mt-2 font-mono">
						{$dataFetchState.message}
					</div>
				{/if}

				{#if fetchError}
					<div class="text-xs text-red-500 mt-2">{fetchError}</div>
				{/if}
			</div>

		{:else if selectedDataset}
			<!-- DETAILS VIEW -->
			<div class="space-y-6">
				<div class="flex items-start justify-between gap-2">
					<div>
						<h3 class="text-lg font-bold text-white">{selectedDataset.symbol}</h3>
						<div class="flex gap-2 text-xs text-gray-500 mt-1">
							<span class="bg-[#111] px-1 rounded">{selectedDataset.timeframe}</span>
							<span class="bg-[#111] px-1 rounded uppercase">{selectedDataset.source}</span>
						</div>
					</div>
					<button
						class="text-[10px] font-mono px-2 py-1 border border-cyan-800/50 text-cyan-400 hover:text-white hover:border-cyan-500 transition-colors whitespace-nowrap"
						on:click={() => dispatch('viewSeries', { symbol: selectedDataset!.symbol, timeframe: selectedDataset!.timeframe })}
						title="Open the data viewer for this series"
					>
						VIEW DATA
					</button>
				</div>

				{#if qualityLoading}
					<div class="text-xs text-gray-500">Analyzing quality...</div>
				{:else if quality}
					<!-- Quality Stats -->
					<div class="space-y-4">
						<div class="grid grid-cols-2 gap-2">
							<div class="bg-[#111] p-2 border border-[#222]">
								<div class="text-[10px] text-gray-500 uppercase">Bars</div>
								<div class="text-sm font-bold">{quality.row_count.toLocaleString()}</div>
							</div>
							<div class="bg-[#111] p-2 border border-[#222]">
								<div class="text-[10px] text-gray-500 uppercase">Gaps</div>
								<div class="text-sm font-bold {quality.gaps > 0 ? 'text-yellow-500' : 'text-green-500'}">{quality.gaps}</div>
							</div>
						</div>

						<div>
							<div class="text-[10px] text-gray-500 uppercase mb-2">Range</div>
							<div class="text-xs font-mono text-gray-300">
								{new Date(quality.start).toLocaleDateString()}
								<br/>↓<br/>
								{new Date(quality.end).toLocaleDateString()}
							</div>
						</div>

						<div>
							<div class="text-[10px] text-gray-500 uppercase mb-2">Integrity</div>
							<div class="space-y-1">
								<div class="flex justify-between text-xs">
									<span class="text-gray-400">Nulls</span>
									<span class={quality.null_values > 0 ? 'text-red-500' : 'text-green-500'}>{quality.null_values}</span>
								</div>
								<div class="flex justify-between text-xs">
									<span class="text-gray-400">Bad H/L</span>
									<span class={quality.integrity.invalid_high_low > 0 ? 'text-red-500' : 'text-green-500'}>{quality.integrity.invalid_high_low}</span>
								</div>
							</div>
						</div>
					</div>
				{:else}
					<div class="text-xs text-gray-500">No quality data available.</div>
				{/if}

				<!-- F1, F3, F4, F5 — Streams section -->
				<div class="space-y-2">
					<div class="text-[10px] text-gray-500 uppercase tracking-wider">Streams</div>

					{#if streamsLoading}
						<div class="text-xs text-gray-500 font-mono animate-pulse">Loading streams...</div>
					{:else if streams}
						<!-- F5 — Active symbol source indicator -->
						{#if streams.collection_reason}
							<div class="text-[10px] text-cyan-600/70 font-mono mb-1">{streams.collection_reason}</div>
						{/if}

						<!-- F1 — Stream health rows -->
						{#each [
							{ key: 'ohlcv', label: 'OHLCV', info: streams.streams?.ohlcv },
							{ key: 'funding', label: 'Funding', info: streams.streams?.funding },
							{ key: 'oi', label: 'OI', info: streams.streams?.oi },
						] as row}
							<div class="bg-[#111] border border-[#222] p-2 space-y-1">
								<div class="flex items-center justify-between">
									<div class="flex items-center gap-2">
										<span class="text-[10px] text-gray-400 font-mono uppercase w-14">{row.label}</span>
										<span class="text-[9px] font-bold tracking-widest {streamStatusColor(row.info.status)}">{streamStatusLabel(row.info.status)}</span>
									</div>
									<!-- F3 — Collect Now button -->
									<button
										class="text-[9px] font-mono px-1.5 py-0.5 border border-cyan-800/50 text-cyan-600 hover:text-cyan-400 hover:border-cyan-600 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
										disabled={!!collectCooldowns[row.key] || collectingStream === row.key}
										on:click={() => collectNow(selectedDataset!.symbol, row.key)}
										title="Trigger collection now"
									>
										{#if collectingStream === row.key}
											<span class="animate-pulse">...</span>
										{:else if collectCooldowns[row.key]}
											COOLING
										{:else}
											COLLECT
										{/if}
									</button>
								</div>

								<!-- F4 — Empty state or data details -->
								{#if row.info.status === 'no_data'}
									<div class="text-[10px] text-gray-600 font-mono leading-relaxed">
										No data yet. Collection runs automatically — check back after the next scheduled pass.
									</div>
								{:else}
									<div class="flex gap-3 text-[10px] text-gray-500 font-mono">
										<span>{row.info.row_count.toLocaleString()} rows</span>
										{#if row.info.data_age_hours !== null}
											<span class={row.info.data_age_hours > 2 ? 'text-yellow-600' : 'text-gray-500'}>{row.info.data_age_hours}h ago</span>
										{/if}
									</div>
								{/if}

								<!-- F4b — last manual Collect result -->
								{#if collectResult[row.key]}
									<div class="text-[10px] font-mono {collectResult[row.key]?.ok ? 'text-green-600' : 'text-red-400'}">
										{collectResult[row.key]?.ok ? '✓' : '✗'} {collectResult[row.key]?.msg}
									</div>
								{/if}
							</div>
						{/each}
					{:else}
						<div class="text-[10px] text-gray-600">Stream data unavailable.</div>
					{/if}
				</div>

			<!-- BV Backfill section -->
			<div class="space-y-2">
				<div class="text-[10px] text-gray-500 uppercase tracking-wider">Binance Vision Backfill</div>
				<div class="bg-[#111] border border-[#222] p-2 space-y-2">
					<div class="flex items-center justify-between">
						<span class="text-[10px] font-mono text-gray-400">
							{#if backfillStatus?.running}
								<span class="text-yellow-400 animate-pulse">RUNNING</span>
							{:else if backfillStatus?.last_error}
								<span class="text-red-400">ERROR</span>
							{:else if backfillStatus?.last_result}
								<span class="text-green-400">DONE</span>
							{:else}
								<span class="text-gray-600">IDLE</span>
							{/if}
						</span>
						<div class="flex gap-1">
							<button
								class="text-[9px] font-mono px-1.5 py-0.5 border border-cyan-800/50 text-cyan-600 hover:text-cyan-400 hover:border-cyan-600 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
								disabled={backfillLoading || !!backfillStatus?.running}
								on:click={() => startBackfill(selectedDataset!.symbol)}
								title="Backfill this symbol"
							>
								{backfillStatus?.running ? '...' : 'THIS'}
							</button>
							<button
								class="text-[9px] font-mono px-1.5 py-0.5 border border-slate-700/50 text-slate-500 hover:text-slate-300 hover:border-slate-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
								disabled={backfillLoading || !!backfillStatus?.running}
								on:click={() => startBackfill()}
								title="Backfill all symbols"
							>
								ALL
							</button>
						</div>
					</div>
					{#if backfillStatus?.last_error}
						<div class="text-[10px] text-red-400 font-mono truncate">{backfillStatus.last_error}</div>
					{:else if backfillStatus?.last_result}
						{@const entries = Object.entries(backfillStatus.last_result)}
						<div class="space-y-1">
							{#each entries as [sym, streams]}
								<div class="text-[10px] font-mono text-gray-500">{sym}:</div>
								{#each Object.entries(streams) as [stream, value]}
									{#if typeof value === 'number'}
										<div class="text-[10px] font-mono text-gray-400 pl-2">
											{stream}: <span class="text-green-400">+{value} rows</span>
										</div>
									{:else if String(stream).endsWith('_skip_reason')}
										<div class="text-[10px] font-mono text-gray-600 pl-2">
											{stream.replace('_skip_reason', '')}: <span class="text-yellow-700">{value}</span>
										</div>
									{:else if String(stream).endsWith('_error')}
										<div class="text-[10px] font-mono text-red-700 pl-2">
											{stream.replace('_error', '')}: error
										</div>
									{/if}
								{/each}
							{/each}
						</div>
					{/if}
					{#if backfillStatus?.last_started_at}
						<div class="text-[10px] text-gray-700 font-mono">{new Date(backfillStatus.last_started_at).toLocaleTimeString()}</div>
					{/if}
				</div>
			</div>
		</div>
		{:else}
			<div class="flex h-full items-center justify-center text-xs text-gray-600">
				Select a dataset to view details
			</div>
		{/if}
	</div>
</div>
