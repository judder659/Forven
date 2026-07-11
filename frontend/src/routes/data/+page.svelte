<script lang="ts">
	import { onMount } from 'svelte';
	import DataPanel from '$lib/components/research/DataPanel.svelte';
	import DataInspector from '$lib/components/research/DataInspector.svelte';
	import CoverageMatrix from '$lib/components/research/CoverageMatrix.svelte';
	import SourceHealth from '$lib/components/research/SourceHealth.svelte';
	import QualityLeaderboard from '$lib/components/research/QualityLeaderboard.svelte';
	import StorageMaintenance from '$lib/components/research/StorageMaintenance.svelte';
	import SeriesDrillDown from '$lib/components/research/SeriesDrillDown.svelte';
	import DataActivityLog from '$lib/components/research/DataActivityLog.svelte';
	import {
		getDatasets,
		getIngestionRuns,
		getDataQualityExtended,
		getDataEngineStatus,
		planDataEngineBackfill,
		executeDataEngineBackfill,
		getSettings,
		type Dataset,
		type DataQualityExtended,
		type DataEngineStatus,
		type DataEngineBackfillPlan,
		type DataEngineBackfillResult,
		type IngestionRun,
		type ForvenSettings,
	} from '$lib/api';
	import {
		getDataUniverse,
		refreshUniverseRegistry,
		seedUniverse,
		cancelUniverseSeed,
		getBackfillStatus,
		triggerBackfill,
		cancelBackfill,
		getCollectionHealth,
		getDataHealth,
		updateUniverseConfig,
		type DataUniverse,
		type BackfillStatus,
		type CollectionHealth,
		type DataHealth,
	} from '$lib/api/data';
	import { dataFetchState, clearDataFetchTask } from '$lib/stores/dataFetch';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';

	let loading = true;
	let refreshing = false;
	let error: string | null = null;
	let remoteDataConfigured = false;
	let drillSeries: { symbol: string; timeframe: string } | null = null;
	let remoteDataUrl: string | null = null;
	let remoteDataError: string | null = null;

	let datasets: Dataset[] = [];
	let runs: IngestionRun[] = [];
	let runsReconstructed = false;
	let selectedDataset: Dataset | null = null;
	let quality: DataQualityExtended | null = null;
	let dataEngineStatus: DataEngineStatus | null = null;
	let dataEnginePlan: DataEngineBackfillPlan | null = null;
	let qualityLoading = false;
	let dataEngineLoading = false;
	let dataEngineError: string | null = null;
	let dataEngineExecuting = false;
	let dataEngineExecResult: DataEngineBackfillResult | null = null;
	let inspectorMode: 'details' | 'fetch' = 'details';

	type DataTab = 'overview' | 'datasets' | 'maintenance' | 'data-log';
	const TABS: { id: DataTab; label: string }[] = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'datasets', label: 'Datasets' },
		{ id: 'maintenance', label: 'Maintenance' },
		{ id: 'data-log', label: 'Data Log' },
	];
	const initialTab = $page.url.searchParams.get('tab');
	let activeTab: DataTab = TABS.some((t) => t.id === initialTab) ? (initialTab as DataTab) : 'overview';

	function selectTab(tab: DataTab): void {
		activeTab = tab;
		const url = new URL($page.url);
		url.searchParams.set('tab', tab);
		goto(url.pathname + url.search, { replaceState: true, keepFocus: true, noScroll: true });
	}

	function openDownload(): void {
		inspectorMode = 'fetch';
		selectTab('datasets');
	}

	function parseTs(value: string | null | undefined): number {
		if (!value) return 0;
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? parsed : 0;
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '--';
		const ts = new Date(value);
		if (Number.isNaN(ts.getTime())) return '--';
		return ts.toLocaleString([], {
			year: 'numeric',
			month: 'short',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit',
		});
	}

	function datasetMarket(dataset: Dataset): string {
		const marketType = String(dataset.market_type || '').trim().toLowerCase();
		if (marketType) return marketType;
		const assetClass = String(dataset.asset_class || '').trim().toLowerCase();
		if (assetClass === 'stock' || assetClass === 'etf') return 'equity';
		return assetClass || 'unknown';
	}

	function marketLabel(market: string): string {
		if (market === 'equity') return 'Stocks / ETFs';
		if (market === 'crypto') return 'Crypto';
		if (market === 'forex') return 'Forex';
		if (market === 'index') return 'Indices';
		return market ? market[0].toUpperCase() + market.slice(1) : 'Unknown';
	}

	function runFromDataset(dataset: Dataset, index: number): IngestionRun {
		const completedAt = dataset.end_ts || dataset.start_ts || null;
		return {
			id: `dataset-${index}-${dataset.symbol}-${dataset.timeframe}`,
			symbol: dataset.symbol,
			timeframe: dataset.timeframe,
			source: dataset.source || 'local',
			status: 'completed',
			idempotency_key: null,
			bars_fetched: dataset.row_count,
			bars_new: dataset.row_count,
			bars_updated: 0,
			error: null,
			prior_version_id: null,
			new_version_id: null,
			started_at: completedAt || new Date().toISOString(),
			completed_at: completedAt,
			duration_ms: null,
		};
	}

	function sameDataset(a: Dataset | null, b: Dataset | null): boolean {
		if (!a || !b) return a === b;
		return a.symbol === b.symbol && a.timeframe === b.timeframe;
	}

	function pickSelection(
		rows: Dataset[],
		preferred?: { symbol: string; timeframe: string }
	): Dataset | null {
		if (preferred) {
			const preferredMatch = rows.find(
				(row) => row.symbol === preferred.symbol && row.timeframe === preferred.timeframe
			);
			if (preferredMatch) return preferredMatch;
		}
		if (selectedDataset) {
			const currentMatch = rows.find(
				(row) => row.symbol === selectedDataset?.symbol && row.timeframe === selectedDataset?.timeframe
			);
			if (currentMatch) return currentMatch;
		}
		return rows[0] ?? null;
	}

	async function loadQuality(dataset: Dataset | null): Promise<void> {
		quality = null;
		if (!dataset) return;
		qualityLoading = true;
		try {
			quality = await getDataQualityExtended(dataset.symbol, dataset.timeframe);
		} catch {
			quality = null;
		} finally {
			qualityLoading = false;
		}
	}

	async function loadData(preferred?: { symbol: string; timeframe: string }): Promise<void> {
		const failures: string[] = [];
		const datasetsPromise = getDatasets();
		// Show the dataset list as soon as its own request answers — the tab must
		// not sit on "Loading..." because one of the six status calls below is slow.
		void datasetsPromise
			.then((rows) => {
				if (Array.isArray(rows)) {
					datasets = rows;
					loading = false;
				}
			})
			.catch(() => {});
		const [settingsResult, datasetsResult, runsResult, dataEngineResult, healthResult, lakeResult, universeResult] = await Promise.allSettled([
			getSettings(),
			datasetsPromise,
			getIngestionRuns({ limit: 500 }),
			getDataEngineStatus(),
			getCollectionHealth(),
			getDataHealth(),
			getDataUniverse(),
		]);

		collectionHealth = healthResult.status === 'fulfilled' ? healthResult.value : null;
		lakeHealth = lakeResult.status === 'fulfilled' ? lakeResult.value : null;
		if (universeResult.status === 'fulfilled') {
			universe = universeResult.value;
			opsLoaded = true;
		}

		if (settingsResult.status === 'fulfilled') {
			const settings = settingsResult.value as ForvenSettings;
			const remoteUrl = String(settings.remote_engine_url || '').trim();
			remoteDataConfigured = Boolean(settings.remote_engine_enabled && remoteUrl);
			remoteDataUrl = remoteUrl || null;
		} else {
			remoteDataConfigured = false;
			remoteDataUrl = null;
		}

		let nextDatasets: Dataset[] = [];
		if (datasetsResult.status === 'fulfilled') {
			nextDatasets = Array.isArray(datasetsResult.value) ? datasetsResult.value : [];
		} else {
			failures.push(
				datasetsResult.reason instanceof Error
					? datasetsResult.reason.message
					: 'Failed to load datasets'
			);
		}
		datasets = nextDatasets;

		let nextRuns: IngestionRun[] = [];
		let usedReconstruction = false;
		if (runsResult.status === 'fulfilled') {
			const loadedRuns = Array.isArray(runsResult.value) ? runsResult.value : [];
			if (loadedRuns.length > 0) {
				nextRuns = loadedRuns;
			} else if (remoteDataConfigured) {
				nextRuns = [];
			} else {
				nextRuns = nextDatasets.map(runFromDataset);
				usedReconstruction = nextRuns.length > 0;
			}
		} else {
			nextRuns = remoteDataConfigured ? [] : nextDatasets.map(runFromDataset);
			usedReconstruction = !remoteDataConfigured && nextRuns.length > 0;
			failures.push(
				runsResult.reason instanceof Error
					? runsResult.reason.message
					: 'Failed to load ingestion history'
			);
		}

		runs = [...nextRuns].sort((a, b) => {
			const aTs = parseTs(a.completed_at || a.started_at);
			const bTs = parseTs(b.completed_at || b.started_at);
			return bTs - aTs;
		});
		runsReconstructed = usedReconstruction;
		if (dataEngineResult.status === 'fulfilled') {
			dataEngineStatus = dataEngineResult.value;
			dataEngineError = null;
		} else {
			dataEngineStatus = null;
			dataEngineError =
				dataEngineResult.reason instanceof Error
					? dataEngineResult.reason.message
					: 'Failed to load Data Engine status';
		}

		if (remoteDataConfigured) {
			const remoteFailures: string[] = [];
			if (datasetsResult.status === 'rejected') {
				remoteFailures.push(
					datasetsResult.reason instanceof Error
						? datasetsResult.reason.message
						: 'Remote datasets request failed'
				);
			}
			if (runsResult.status === 'rejected') {
				remoteFailures.push(
					runsResult.reason instanceof Error
						? runsResult.reason.message
						: 'Remote ingestion history request failed'
				);
			}
			remoteDataError = remoteFailures.length > 0 ? remoteFailures.join(' • ') : null;
		} else {
			remoteDataError = null;
		}

		error =
			!remoteDataConfigured && failures.length > 0
				? failures.join(' • ')
				: null;

		const nextSelection = pickSelection(nextDatasets, preferred);
		const selectionChanged = !sameDataset(selectedDataset, nextSelection);
		selectedDataset = nextSelection;
		if (!nextSelection) {
			inspectorMode = 'fetch';
			quality = null;
			qualityLoading = false;
			return;
		}

		if (selectionChanged || !quality) {
			await loadQuality(nextSelection);
		}
	}

	async function refreshData(preferred?: { symbol: string; timeframe: string }): Promise<void> {
		refreshing = true;
		try {
			await loadData(preferred);
		} finally {
			refreshing = false;
		}
	}

	function hasActiveRuns(list: IngestionRun[]): boolean {
		return list.some((r) => r.status === 'running' || r.status === 'pending');
	}

	// Lightweight poll: only re-fetch ingestion runs (the thing that changes during a
	// download). Skip when runs are reconstructed from the catalog — there is no real
	// run log to poll. Returns true when an active run just transitioned to a terminal
	// state, signalling the caller to do a full refresh of datasets + quality.
	async function pollRuns(): Promise<boolean> {
		if (remoteDataConfigured || runsReconstructed) return false;
		const wasActive = hasActiveRuns(runs);
		try {
			const loaded = await getIngestionRuns({ limit: 500 });
			const nextRuns = Array.isArray(loaded) ? loaded : [];
			if (nextRuns.length === 0) return false;
			runs = [...nextRuns].sort((a, b) => {
				const aTs = parseTs(a.completed_at || a.started_at);
				const bTs = parseTs(b.completed_at || b.started_at);
				return bTs - aTs;
			});
			return wasActive && !hasActiveRuns(runs);
		} catch {
			return false;
		}
	}

	function handlePanelSelect(event: CustomEvent<{ dataset: Dataset }>): void {
		selectedDataset = event.detail.dataset;
		inspectorMode = 'details';
		void loadQuality(selectedDataset);
	}

	function handlePanelRefresh(): void {
		void refreshData(
			selectedDataset
				? { symbol: selectedDataset.symbol, timeframe: selectedDataset.timeframe }
				: undefined
		);
	}

	async function handlePlanBackfill(): Promise<void> {
		dataEngineLoading = true;
		dataEngineError = null;
		try {
			dataEnginePlan = await planDataEngineBackfill();
		} catch (err) {
			dataEnginePlan = null;
			dataEngineError = err instanceof Error ? err.message : 'Failed to plan Data Engine backfill';
		} finally {
			dataEngineLoading = false;
		}
	}

	async function handleExecuteBackfill(): Promise<void> {
		dataEngineExecuting = true;
		dataEngineError = null;
		try {
			dataEngineExecResult = await executeDataEngineBackfill(10);
		} catch (err) {
			dataEngineError = err instanceof Error ? err.message : 'Failed to execute backfill plan';
			dataEngineExecuting = false;
			return;
		}
		// Re-plan (the plan endpoint rescans the lake) + refresh the panel so the
		// backlog visibly drains. A refresh failure here must NOT masquerade as an
		// execute failure — the execute already succeeded.
		try {
			dataEnginePlan = await planDataEngineBackfill();
			dataEngineStatus = await getDataEngineStatus();
		} catch {
			// keep the exec result; the count just won't refresh this round
		} finally {
			dataEngineExecuting = false;
		}
	}

	// Candle backlog from the FRESH plan — single source for the button + count.
	$: dataEngineCandleRemaining = dataEnginePlan
		? dataEnginePlan.tasks.filter((t) => t.stream === 'candles').length
		: 0;

	// --- Overview trust strip ---
	let collectionHealth: CollectionHealth | null = null;
	let lakeHealth: DataHealth | null = null;

	function formatBytes(bytes: number | null | undefined): string {
		const value = Number(bytes) || 0;
		if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
		if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(0)} MB`;
		return `${(value / 1024).toFixed(0)} KB`;
	}

	function scoreClass(score: number): string {
		if (score >= 90) return 'text-emerald-400';
		if (score >= 70) return 'text-yellow-400';
		return 'text-red-400';
	}

	// Venue split from the stamped market identity of each series.
	$: venueSplit = datasets.reduce(
		(acc, dataset) => {
			const market = String(dataset.market || 'unstamped').toLowerCase();
			if (market === 'perp') acc.perp += 1;
			else if (market === 'spot') acc.spot += 1;
			else acc.other += 1;
			return acc;
		},
		{ perp: 0, spot: 0, other: 0 }
	);

	// --- Research universe + deep-history operations (maintenance tab) ---
	let universe: DataUniverse | null = null;
	let universeError: string | null = null;
	let universeBusy = false;
	let bvStatus: BackfillStatus | null = null;
	let bvError: string | null = null;
	let bvBusy = false;
	let opsLoaded = false;

	async function loadOpsPanels(): Promise<void> {
		try {
			universe = await getDataUniverse();
			universeError = null;
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Failed to load universe';
		}
		try {
			bvStatus = await getBackfillStatus();
			bvError = null;
		} catch (err) {
			bvError = err instanceof Error ? err.message : 'Failed to load backfill status';
		}
		opsLoaded = true;
	}

	async function handleRefreshRegistry(): Promise<void> {
		universeBusy = true;
		try {
			await refreshUniverseRegistry();
			await loadOpsPanels();
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Registry refresh failed';
		} finally {
			universeBusy = false;
		}
	}

	async function handleSeedUniverse(): Promise<void> {
		universeBusy = true;
		try {
			await seedUniverse();
			await loadOpsPanels();
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Universe seed failed to start';
		} finally {
			universeBusy = false;
		}
	}

	async function handleCancelSeed(): Promise<void> {
		try {
			await cancelUniverseSeed();
			await loadOpsPanels();
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Cancel failed';
		}
	}

	async function handleTriggerBv(): Promise<void> {
		bvBusy = true;
		try {
			await triggerBackfill();
			await loadOpsPanels();
		} catch (err) {
			bvError = err instanceof Error ? err.message : 'Backfill failed to start';
		} finally {
			bvBusy = false;
		}
	}

	async function handleCancelBv(): Promise<void> {
		try {
			await cancelBackfill();
			await loadOpsPanels();
		} catch (err) {
			bvError = err instanceof Error ? err.message : 'Cancel failed';
		}
	}

	function jobPct(progress: { done: number; total: number } | null | undefined): number {
		if (!progress || !progress.total) return 0;
		return Math.min(100, Math.round((progress.done / progress.total) * 100));
	}

	// First visit to the maintenance tab loads the operations panels once;
	// the poll below keeps them fresh while a job runs.
	$: if (activeTab === 'maintenance' && !opsLoaded) void loadOpsPanels();

	$: universeSeedRunning = Boolean(universe?.seed?.running);
	$: universeMinuteTier = (universe?.plan ?? []).filter((p) => p.timeframes.includes('1m')).length;

	// Universe sizing: presets are premades — the number itself stays editable.
	const UNIVERSE_PRESETS = [
		{ label: 'Focused', size: 10 },
		{ label: 'Standard', size: 25 },
		{ label: 'Comprehensive', size: 50 },
	];
	let universeSizeInput: number | null = null;
	let universeConfigBusy = false;

	$: universeSize = universeSizeInput ?? universe?.config?.size ?? 50;

	// Rough per-tier download footprint (zstd parquet, full perp history):
	// base ladder (1h/4h/1d) ~10 MB, +intraday (15m/5m) ~50 MB, +1m ~120 MB.
	$: universeEstimate = (() => {
		const size = universeSize;
		const intradayTop = Math.min(universe?.config?.intraday_top ?? 20, size);
		const minuteTop = Math.min(universe?.config?.minute_top ?? 10, intradayTop);
		const mb = size * 10 + intradayTop * 50 + minuteTop * 120;
		return mb >= 1024 ? `~${(mb / 1024).toFixed(1)} GB` : `~${mb} MB`;
	})();

	async function applyUniverseSize(size: number): Promise<void> {
		universeConfigBusy = true;
		try {
			await updateUniverseConfig({ size });
			universeSizeInput = null;
			await loadOpsPanels();
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Failed to update universe size';
		} finally {
			universeConfigBusy = false;
		}
	}

	async function toggleUniverseEnabled(): Promise<void> {
		universeConfigBusy = true;
		try {
			await updateUniverseConfig({ enabled: !(universe?.config?.enabled ?? true) });
			await loadOpsPanels();
		} catch (err) {
			universeError = err instanceof Error ? err.message : 'Failed to toggle universe';
		} finally {
			universeConfigBusy = false;
		}
	}

	async function handleFetched(event: CustomEvent<{ dataset: Dataset }>): Promise<void> {
		const fetched = event.detail.dataset;
		await refreshData({ symbol: fetched.symbol, timeframe: fetched.timeframe });
		inspectorMode = 'details';
	}

	$: totalBars = datasets.reduce((sum, dataset) => sum + (Number(dataset.row_count) || 0), 0);
	$: latestDatasetTs = Math.max(
		...datasets.map((dataset) => parseTs(dataset.end_ts || dataset.start_ts)),
		0
	);
	$: latestDatasetLabel =
		latestDatasetTs > 0 ? formatTimestamp(new Date(latestDatasetTs).toISOString()) : '--';
	$: availableMarkets = Array.from(
		new Set(datasets.map((dataset) => datasetMarket(dataset)).filter((market) => market && market !== 'unknown'))
	).sort();
	$: availableMarketLabel =
		availableMarkets.length > 0 ? availableMarkets.map((market) => marketLabel(market)).join(' • ') : 'No local markets yet';
	$: equitySymbolCount = new Set(
		datasets.filter((dataset) => datasetMarket(dataset) === 'equity').map((dataset) => dataset.symbol)
	).size;
	$: dataEngineCoverageCount = dataEngineStatus?.coverage?.length ?? 0;
	$: dataEngineLiveCount = (dataEngineStatus?.streams ?? []).filter((stream) => stream.status === 'connected').length;
	$: dataEngineSourceCount = dataEngineStatus?.sources?.length ?? 0;

	// Source status comes from a circuit breaker, where "closed" = healthy and
	// "open" = failing — exactly backwards for a casual reader, so translate.
	const SOURCE_STATUS_LABEL: Record<string, string> = {
		closed: 'healthy',
		open: 'failing',
		'half-open': 'recovering',
	};

	onMount(() => {
		let isDestroyed = false;
		async function initialLoad() {
			try {
				await loadData();
			} finally {
				if (!isDestroyed) loading = false;
			}
		}
		initialLoad();

		let polling = false;
		const interval = setInterval(() => {
			if (polling) return;
			const fetchRunning = $dataFetchState.status === 'running';
			// Poll while a download is in flight or a run is still active. A live fetch
			// can populate the real run log even when the table is currently
			// reconstructed from the catalog, so do a full refresh in that case.
			if (!hasActiveRuns(runs) && !fetchRunning) return;
			polling = true;
			const fullRefresh = fetchRunning && runsReconstructed;
			const work = fullRefresh
				? refreshData(
						selectedDataset
							? { symbol: selectedDataset.symbol, timeframe: selectedDataset.timeframe }
							: undefined
					).then(() => false)
				: pollRuns();
			work
				.then((completed) => {
					if (isDestroyed) return;
					if (completed) {
						// A run finished: refresh datasets + quality once.
						return refreshData(
							selectedDataset
								? { symbol: selectedDataset.symbol, timeframe: selectedDataset.timeframe }
								: undefined
						);
					}
				})
				.finally(() => {
					polling = false;
				});
		}, 3000);

		// Keep the maintenance operations panels live while a long job runs
		// (universe seed / deep-history backfill) — per-symbol progress updates.
		// In-flight guard mirrors `polling` above so a load that runs longer than
		// the 4s interval can't stack overlapping requests.
		let opsPolling = false;
		const opsInterval = setInterval(() => {
			if (isDestroyed || opsPolling || activeTab !== 'maintenance') return;
			if (!(universe?.seed?.running || bvStatus?.running)) return;
			opsPolling = true;
			loadOpsPanels().finally(() => {
				opsPolling = false;
			});
		}, 4000);

		return () => {
			isDestroyed = true;
			clearInterval(interval);
			clearInterval(opsInterval);
		};
	});
</script>

<svelte:head>
	<title>Data Manager | Forven</title>
	<meta
		name="description"
		content="Download market data, inspect datasets, and review historical ingestion runs."
	/>
</svelte:head>

<div class="h-full overflow-auto text-white p-4 space-y-4">
	<header class="flex flex-col gap-3 border-b border-[#222] pb-4 md:flex-row md:items-end md:justify-between">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">Data Manager</h1>
			<p class="mt-1 text-xs text-[#666]">Download, inspect, and track historical datasets across crypto and stock-market feeds.</p>
		</div>
		<div class="flex flex-col gap-2 sm:flex-row">
			<button
				type="button"
				on:click={openDownload}
				class="terminal-button-primary text-xs"
			>
				Download Data
			</button>
			<button
				type="button"
				on:click={() =>
					refreshData(
						selectedDataset
							? { symbol: selectedDataset.symbol, timeframe: selectedDataset.timeframe }
							: undefined
					)}
				disabled={refreshing}
				class="terminal-button text-xs"
			>
				{refreshing ? 'Refreshing...' : 'Refresh'}
			</button>
		</div>
	</header>

	<div class="flex gap-0 border-b border-[#222]">
		{#each TABS as tab}
			<button
				type="button"
				class="border-b-2 px-4 py-2 text-[11px] font-bold uppercase tracking-widest transition-colors {activeTab === tab.id ? 'border-white text-white' : 'border-transparent text-[#555] hover:text-[#999]'}"
				on:click={() => selectTab(tab.id)}
			>{tab.label}</button>
		{/each}
	</div>

	{#if activeTab === 'overview'}
	<section class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3" title="Aggregate collection health across every stream (OHLCV, funding, OI, basis, IV, …)">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Data Health</div>
			{#if collectionHealth}
				<div class="text-lg font-semibold mt-1 font-mono {scoreClass(collectionHealth.score)}">{collectionHealth.score}<span class="text-[11px] text-gray-600">/100</span></div>
			{:else}
				<div class="text-lg font-semibold mt-1 text-gray-600">--</div>
			{/if}
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Datasets</div>
			<div class="text-lg font-semibold mt-1">{datasets.length}</div>
			<div class="text-[10px] text-gray-500 mt-0.5">{totalBars.toLocaleString()} bars</div>
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3" title="Total parquet lake size on disk">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Lake Size</div>
			<div class="text-lg font-semibold mt-1">{lakeHealth ? formatBytes(lakeHealth.total_parquet_bytes) : '--'}</div>
			<div class="text-[10px] text-gray-500 mt-0.5">{lakeHealth ? `${lakeHealth.total_parquet_files.toLocaleString()} files` : ''}</div>
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3" title="Venue identity of stored series (perp = the venue semantics we execute on). Unstamped = legacy files; run the market reconcile.">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Venue Split</div>
			<div class="text-sm font-semibold mt-1">
				<span class="text-cyan-300">{venueSplit.perp} perp</span>
				<span class="text-gray-600"> · </span>
				<span class="text-amber-300">{venueSplit.spot} spot</span>
			</div>
			{#if venueSplit.other > 0}
				<div class="text-[10px] text-gray-500 mt-0.5">{venueSplit.other} unstamped / other</div>
			{/if}
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3" title="Symbol registry: perps listed on the venue vs the research universe planned for deep history">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Universe</div>
			{#if universe}
				<div class="text-sm font-semibold mt-1">{universe.plan.length} planned<span class="text-gray-600"> / </span>{universe.active} listed</div>
				<div class="text-[10px] text-gray-500 mt-0.5">{universe.delisted} delisted kept</div>
			{:else}
				<div class="text-lg font-semibold mt-1 text-gray-600">--</div>
			{/if}
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] p-3">
			<div class="text-[10px] uppercase tracking-wider text-gray-500">Latest Download</div>
			<div class="text-sm font-semibold mt-1">{latestDatasetLabel}</div>
			<div class="text-[10px] text-gray-500 mt-0.5">{availableMarketLabel}</div>
		</div>
	</section>

	<CoverageMatrix on:view={(e) => (drillSeries = e.detail)} />

	<div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
		<SourceHealth />
		<QualityLeaderboard on:select={(e) => (drillSeries = e.detail)} />
	</div>
	{/if}

	{#if activeTab === 'maintenance'}
	<!-- All three download mechanisms live in ONE card, ordered from broadest
	     (add markets) to most surgical (fill gaps) — each row says plainly when
	     to use it, so users don't have to decode seed/backfill/catch-up jargon. -->
	<section class="border border-[#222] rounded bg-[#0a0a0a] overflow-hidden">
		<div class="px-3 py-2 border-b border-[#1a1a1a]">
			<div class="text-[11px] uppercase tracking-wider text-gray-400">Downloads &amp; Coverage</div>
			<div class="text-[11px] text-gray-500 mt-0.5">
				Three ways to get data: track more markets, extend their history further back, or fill small recent gaps.
			</div>
		</div>

		<!-- 1 · Track more markets (research universe) -->
		<div class="p-3 border-b border-[#171717]">
			<div class="flex flex-wrap items-center justify-between gap-2 mb-1">
				<div class="text-xs font-semibold text-gray-200">1 · Track more markets</div>
				<div class="flex gap-2">
					<button
						type="button"
						on:click={handleRefreshRegistry}
						disabled={universeBusy || universeSeedRunning}
						title="Re-read the exchange's list of tradable perpetuals"
						class="px-2 py-1 text-[11px] rounded border border-[#2b2b2b] hover:border-cyan-500 hover:text-cyan-100 transition-colors disabled:opacity-50"
					>Refresh symbol list</button>
					{#if universeSeedRunning}
						<button
							type="button"
							on:click={handleCancelSeed}
							class="px-2 py-1 text-[11px] rounded border border-red-900 text-red-300 hover:border-red-500 transition-colors"
						>Cancel</button>
					{:else}
						<button
							type="button"
							on:click={handleSeedUniverse}
							disabled={universeBusy}
							class="px-2 py-1 text-[11px] rounded border border-cyan-700 text-cyan-300 hover:text-white hover:border-cyan-400 transition-colors disabled:opacity-50"
						>Download history</button>
					{/if}
				</div>
			</div>
			<div class="text-[11px] text-gray-500 mb-2 leading-snug max-w-3xl">
				The research universe is the set of most-liquid perpetuals the system studies for strategy discovery.
				Pick a size, then <span class="text-gray-300">Download history</span> to fetch each one's complete past
				(price, funding, open interest, basis). Nothing downloads until you click; safe to cancel — everything
				already saved is kept, and the next run resumes where it stopped.
			</div>
			{#if universeError}
				<div class="text-[11px] text-red-300 mb-2">{universeError}</div>
			{/if}
			{#if universe}
				<div class="grid grid-cols-3 gap-2 text-center mb-2 max-w-md">
					<div class="rounded border border-[#1c1c1c] p-2" title="Perpetuals currently tradable on the exchange">
						<div class="text-sm font-semibold text-gray-100">{universe.active.toLocaleString()}</div>
						<div class="text-[10px] text-gray-500 uppercase">on exchange</div>
					</div>
					<div class="rounded border border-[#1c1c1c] p-2" title="Symbols selected for the research universe at the current size ({universeMinuteTier} also get 1-minute bars)">
						<div class="text-sm font-semibold text-gray-100">{universe.plan.length.toLocaleString()}</div>
						<div class="text-[10px] text-gray-500 uppercase">selected ({universeMinuteTier} w/ 1m)</div>
					</div>
					<div class="rounded border border-[#1c1c1c] p-2" title="Delisted symbols whose history is kept, so research isn't biased toward survivors">
						<div class="text-sm font-semibold text-gray-100">{universe.delisted.toLocaleString()}</div>
						<div class="text-[10px] text-gray-500 uppercase">delisted kept</div>
					</div>
				</div>

				<!-- Universe sizing: presets are premades, the number stays editable;
				     seeding is always manual, so nothing downloads until the click. -->
				<div class="flex flex-wrap items-center gap-2 mb-2 text-[11px]">
					<span class="text-gray-500 uppercase text-[10px] tracking-wider">Size</span>
					{#each UNIVERSE_PRESETS as preset}
						<button
							type="button"
							disabled={universeConfigBusy || universeSeedRunning}
							on:click={() => void applyUniverseSize(preset.size)}
							class="px-2 py-0.5 rounded border transition-colors disabled:opacity-50 {universeSize === preset.size
								? 'border-cyan-600 text-cyan-200'
								: 'border-[#2b2b2b] text-gray-400 hover:border-cyan-700 hover:text-gray-200'}"
							title={`${preset.size} most liquid perps`}
						>{preset.label} ({preset.size})</button>
					{/each}
					<input
						type="number"
						min="1"
						max="500"
						class="w-16 bg-[#111] border border-[#2b2b2b] rounded px-1.5 py-0.5 text-gray-200"
						value={universeSize}
						disabled={universeConfigBusy || universeSeedRunning}
						on:change={(e) => {
							const v = Number(e.currentTarget.value);
							if (Number.isFinite(v) && v >= 1 && v <= 500) void applyUniverseSize(Math.round(v));
						}}
						title="Custom universe size (1-500 most liquid perps)"
					/>
					<span class="text-gray-500" title="Rough full-download footprint at this size (perp history + derivatives)">
						est. {universeEstimate}
					</span>
					<button
						type="button"
						disabled={universeConfigBusy || universeSeedRunning}
						on:click={toggleUniverseEnabled}
						class="ml-auto px-2 py-0.5 rounded border transition-colors disabled:opacity-50 {universe.config?.enabled === false
							? 'border-[#2b2b2b] text-gray-500 hover:text-gray-300'
							: 'border-green-900 text-green-300'}"
						title="When off, the research universe is not planned or downloaded — only your traded symbols keep collecting."
					>{universe.config?.enabled === false ? 'Universe OFF' : 'Universe ON'}</button>
				</div>
				{#if universe.config?.enabled === false}
					<div class="text-[11px] text-amber-200/80 mb-2">
						Research universe disabled — no bulk downloads will be planned. Your traded symbols keep collecting normally.
					</div>
				{/if}
				{#if universeSeedRunning && universe.seed.progress}
					<div class="text-[11px] text-gray-300 mb-1">
						Downloading {universe.seed.progress.current_symbol} — {universe.seed.progress.done}/{universe.seed.progress.total}
					</div>
					<div class="h-1.5 rounded bg-[#161616] overflow-hidden">
						<div class="h-full bg-cyan-600 transition-all" style={`width:${jobPct(universe.seed.progress)}%`}></div>
					</div>
				{:else if universe.seed.last_error}
					<div class="text-[11px] text-red-300">Last run failed: {universe.seed.last_error}</div>
				{:else if universe.seed.last_result}
					<div class="text-[11px] text-green-400">
						Last run: {String((universe.seed.last_result as Record<string, unknown>).series_seeded ?? 0)} series downloaded,
						{String((universe.seed.last_result as Record<string, unknown>).series_current ?? 0)} already current
					</div>
				{/if}
			{:else if !universeError}
				<div class="text-xs text-gray-500">Loading…</div>
			{/if}
		</div>

		<!-- 2 · Extend history further back (deep-history backfill) -->
		<div class="p-3 border-b border-[#171717]">
			<div class="flex flex-wrap items-center justify-between gap-2 mb-1">
				<div class="text-xs font-semibold text-gray-200">2 · Extend history further back</div>
				<div class="flex gap-2">
					{#if bvStatus?.running}
						<button
							type="button"
							on:click={handleCancelBv}
							class="px-2 py-1 text-[11px] rounded border border-red-900 text-red-300 hover:border-red-500 transition-colors"
						>{bvStatus?.cancel_requested ? 'Cancelling…' : 'Cancel'}</button>
					{:else}
						<button
							type="button"
							on:click={handleTriggerBv}
							disabled={bvBusy}
							class="px-2 py-1 text-[11px] rounded border border-cyan-700 text-cyan-300 hover:text-white hover:border-cyan-400 transition-colors disabled:opacity-50"
						>Extend all symbols</button>
					{/if}
				</div>
			</div>
			<div class="text-[11px] text-gray-500 mb-2 leading-snug max-w-3xl">
				Symbols downloaded mid-history stop at their first stored bar. This walks every stored symbol back to
				its first day on the exchange (price, funding, open interest, basis) using Binance's public archive.
				Survives restarts; cancelling takes effect between symbols.
			</div>
			{#if bvError}
				<div class="text-[11px] text-red-300 mb-2">{bvError}</div>
			{/if}
			{#if bvStatus?.running && bvStatus.progress}
				<div class="text-[11px] text-gray-300 mb-1">
					Extending {bvStatus.progress.current_symbol} — {bvStatus.progress.done}/{bvStatus.progress.total} symbols
				</div>
				<div class="h-1.5 rounded bg-[#161616] overflow-hidden">
					<div class="h-full bg-cyan-600 transition-all" style={`width:${jobPct(bvStatus.progress)}%`}></div>
				</div>
			{:else if bvStatus?.running}
				<div class="text-[11px] text-gray-300">Extending history…</div>
			{:else if bvStatus?.last_error}
				<div class="text-[11px] text-red-300">Last run failed: {bvStatus.last_error}</div>
			{:else if bvStatus?.last_started_at}
				<div class="text-[11px] text-green-400">Last run: {formatTimestamp(bvStatus.last_started_at)} ✓</div>
			{/if}
		</div>

		<!-- 3 · Fill recent gaps (data-engine catch-up) -->
		<div class="p-3">
			<div class="flex flex-wrap items-center justify-between gap-2 mb-1">
				<div class="text-xs font-semibold text-gray-200">3 · Fill recent gaps</div>
				<button
					type="button"
					on:click={handlePlanBackfill}
					disabled={dataEngineLoading}
					class="px-2 py-1 text-[11px] rounded border border-[#2b2b2b] hover:border-cyan-500 hover:text-cyan-100 transition-colors disabled:opacity-50"
				>
					{dataEngineLoading ? 'Checking…' : 'Check for gaps'}
				</button>
			</div>
			<div class="text-[11px] text-gray-500 mb-2 leading-snug max-w-3xl">
				Tops up bars missed while the app was off and small holes inside stored series. Runs automatically every
				~10&nbsp;minutes when the Data Engine is on; checking here forces a pass right now.
			</div>
			{#if dataEngineStatus && dataEngineStatus.enabled === false}
				<div class="text-[11px] text-amber-200/80 mb-2">
					Automatic catch-up is paused — the Data Engine is off. Manual gap-fills here still work; enable it in
					<a href="/settings#data" class="underline hover:text-amber-100">Settings → Data</a> for hands-free catch-up.
				</div>
			{/if}
			{#if dataEngineError}
				<div class="text-[11px] text-red-300 mb-2">{dataEngineError}</div>
			{/if}
			{#if dataEnginePlan}
				{#if dataEnginePlan.task_count === 0}
					<div class="text-xs text-green-400">Everything is current — no gaps found. ✓</div>
				{:else}
					<div class="text-xs text-gray-200">
						{dataEnginePlan.task_count.toLocaleString()} gap{dataEnginePlan.task_count === 1 ? '' : 's'} to fill
					</div>
					{#if dataEnginePlan.tasks.length > 0}
						<div class="mt-2 max-h-24 overflow-auto space-y-1">
							{#each dataEnginePlan.tasks.slice(0, 6) as task}
								<div class="font-mono text-[11px] text-gray-400">
									{task.symbol} {task.timeframe} {task.start_ts} → {task.end_ts}
								</div>
							{/each}
						</div>
						<button
							type="button"
							on:click={handleExecuteBackfill}
							disabled={dataEngineExecuting}
							class="mt-2 px-3 py-1.5 text-[11px] rounded border border-cyan-700 text-cyan-300 hover:text-white hover:border-cyan-400 transition-colors disabled:opacity-50"
						>
							{dataEngineExecuting
								? 'Filling…'
								: dataEngineExecResult
									? `Fill ${dataEngineCandleRemaining} more`
									: 'Fill gaps now'}
						</button>
					{/if}
				{/if}
				{#if dataEngineExecResult}
					<div class="mt-2 text-[11px] {dataEngineExecResult.failed > 0 ? 'text-yellow-400' : dataEngineExecResult.rows_added > 0 ? 'text-green-400' : 'text-gray-400'}">
						✓ filled {dataEngineExecResult.executed}, +{dataEngineExecResult.rows_added.toLocaleString()} bars{#if dataEngineExecResult.failed > 0}, {dataEngineExecResult.failed} failed{/if}{#if dataEngineCandleRemaining === 0}, all caught up{/if}
					</div>
				{/if}
			{/if}
		</div>
	</section>

	<StorageMaintenance />
	{/if}

	{#if drillSeries}
		<SeriesDrillDown
			symbol={drillSeries.symbol}
			timeframe={drillSeries.timeframe}
			on:close={() => (drillSeries = null)}
		/>
	{/if}

	{#if remoteDataConfigured && remoteDataError}
		<div class="border-2 border-red-500 bg-red-950/60 rounded-lg p-4 md:p-5 shadow-[0_0_0_1px_rgba(239,68,68,0.25)]">
			<div class="text-red-100 font-extrabold text-sm md:text-base tracking-wider uppercase">Remote Data Source Error</div>
			<p class="text-red-200 text-sm mt-2">
				Remote Data Mode is enabled in Settings. Local dataset fallback is disabled until remote connectivity is restored.
			</p>
			<div class="mt-3 text-[11px] font-mono text-red-100 break-all">
				Endpoint: {remoteDataUrl || '--'}
			</div>
			<div class="mt-2 text-xs text-red-200 whitespace-pre-wrap">{remoteDataError}</div>
		</div>
	{/if}

	{#if error}
		<div class="border border-red-800 bg-red-900/20 text-red-300 text-xs px-3 py-2 rounded">{error}</div>
	{/if}

	{#if $dataFetchState.status === 'running'}
		<div class="flex items-start gap-3 border border-cyan-800 bg-cyan-950/30 text-cyan-100 text-xs px-3 py-2 rounded">
			<div class="mt-0.5 w-2 h-2 rounded-full bg-cyan-400 animate-ping shrink-0"></div>
			<div class="min-w-0">
				<div class="font-semibold">
					Downloading{$dataFetchState.label ? ` ${$dataFetchState.label}` : ''}{$dataFetchState.isBulk ? ' (bulk)' : ''}...
				</div>
				{#if $dataFetchState.progress}
					<div class="mt-1 font-mono text-cyan-300 break-words">{$dataFetchState.progress}</div>
				{/if}
			</div>
		</div>
	{:else if $dataFetchState.status === 'success' && $dataFetchState.message}
		<div class="flex items-start justify-between gap-3 border {$dataFetchState.warning ? 'border-amber-800 bg-amber-950/30 text-amber-200' : 'border-green-800 bg-green-950/30 text-green-200'} text-xs px-3 py-2 rounded">
			<div class="min-w-0">
				<div class="font-mono break-words">{$dataFetchState.message}</div>
				{#if $dataFetchState.warning}
					<div class="mt-1 break-words text-amber-300">⚠ {$dataFetchState.warning}</div>
				{/if}
			</div>
			<button
				type="button"
				on:click={clearDataFetchTask}
				class="shrink-0 {$dataFetchState.warning ? 'text-amber-400' : 'text-green-400'} hover:text-white transition-colors"
				aria-label="Dismiss download status"
			>
				Dismiss
			</button>
		</div>
	{:else if ($dataFetchState.status === 'error' || $dataFetchState.status === 'cancelled') && $dataFetchState.message}
		<div class="flex items-start justify-between gap-3 border border-red-800 bg-red-900/20 text-red-300 text-xs px-3 py-2 rounded">
			<div class="min-w-0 break-words">
				{$dataFetchState.status === 'cancelled' ? 'Download cancelled' : 'Download failed'}: {$dataFetchState.message}
			</div>
			<button
				type="button"
				on:click={clearDataFetchTask}
				class="shrink-0 text-red-400 hover:text-white transition-colors"
				aria-label="Dismiss download status"
			>
				Dismiss
			</button>
		</div>
	{/if}

	{#if activeTab === 'maintenance'}
	<!-- Status-only card: the catch-up ACTIONS live in "Fill recent gaps" above,
	     so this stays a read-only glance at the collection machinery. -->
	<section class="border border-[#222] rounded bg-[#0a0a0a] overflow-hidden">
		<div class="px-3 py-2 border-b border-[#1a1a1a]">
			<div class="text-[11px] uppercase tracking-wider text-gray-400">Data Engine Status</div>
			<div class="text-[11px] text-gray-500 mt-0.5">
				The machinery behind automatic collection — {dataEngineCoverageCount.toLocaleString()} tracked series • {dataEngineLiveCount.toLocaleString()} live streams • {dataEngineSourceCount.toLocaleString()} sources
			</div>
		</div>
		{#if dataEngineStatus && dataEngineStatus.enabled === false}
			<div class="px-3 py-2 text-[11px] text-amber-200/90 border-b border-amber-900/40 bg-amber-950/20">
				The Data Engine is <span class="font-semibold">disabled</span> (this is optional). The standard local data path works without it — enable it in
				<a href="/settings#data" class="underline hover:text-amber-100">Settings → Data</a> to use catalog streaming and automatic catch-up. The counts here stay at zero until it's on.
			</div>
		{/if}
		<div class="grid grid-cols-1 lg:grid-cols-2">
			<div class="p-3 border-b lg:border-b-0 lg:border-r border-[#171717]">
				<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-2">Exchange connections</div>
				{#if dataEngineStatus?.sources?.length}
					<div class="space-y-2">
						{#each dataEngineStatus.sources as source}
							<div class="flex items-center justify-between gap-3 text-xs">
								<span class="font-mono text-gray-200">{source.source}</span>
								<span
									title={`circuit ${source.status}`}
									class={`rounded border px-2 py-0.5 text-[10px] uppercase ${
										source.status === 'closed'
											? 'border-green-800 text-green-300'
											: source.status === 'open'
												? 'border-red-800 text-red-300'
												: 'border-yellow-800 text-yellow-300'
									}`}>{SOURCE_STATUS_LABEL[source.status] ?? source.status}</span>
							</div>
						{/each}
					</div>
				{:else}
					<div class="text-xs text-gray-500">No connection history yet.</div>
				{/if}
			</div>
			<div class="p-3">
				<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-2">Live streams</div>
				{#if dataEngineStatus?.streams?.length}
					<div class="space-y-2">
						{#each dataEngineStatus.streams.slice(0, 5) as stream}
							<div class="flex items-center justify-between gap-3 text-xs">
								<span class="font-mono text-gray-200">{stream.symbol} / {stream.stream}</span>
								<span class="text-gray-400">{stream.buffered_rows.toLocaleString()} buffered</span>
							</div>
						{/each}
					</div>
				{:else}
					<div class="text-xs text-gray-500">No live streams buffering right now.</div>
				{/if}
			</div>
		</div>
	</section>
	{/if}

	{#if activeTab === 'overview'}
	<div class="rounded border border-cyan-900/40 bg-cyan-950/15 px-3 py-2 text-xs text-cyan-100">
		Any symbol listed in this dataset catalog can be used for backtests and optimizations.
		{#if equitySymbolCount > 0}
			<span class="text-cyan-200"> {equitySymbolCount.toLocaleString()} stock / ETF symbols are ready in the local backtest universe.</span>
		{/if}
	</div>
	{/if}

	{#if activeTab === 'datasets'}
	{#if !loading && !remoteDataConfigured && datasets.length === 0}
		<div class="rounded-lg border border-cyan-800 bg-cyan-950/20 p-5 flex flex-col items-center text-center gap-2">
			<div class="text-base font-semibold text-white">Download your first dataset</div>
			<p class="text-xs text-gray-400 max-w-md">
				No local datasets yet. Fetch OHLCV history from ccxt, Binance, Polygon, Yahoo, or a CSV
				upload to start backtesting and optimizing.
			</p>
			<button
				type="button"
				on:click={openDownload}
				class="mt-1 px-4 py-2 text-xs rounded border border-cyan-600 bg-cyan-900/30 text-cyan-100 hover:text-white hover:border-cyan-400 transition-colors"
			>
				Download Data
			</button>
		</div>
	{/if}

	<section class="grid grid-cols-1 xl:grid-cols-[320px_minmax(0,1fr)] gap-3">
		<div class="border border-[#222] rounded bg-[#0a0a0a] overflow-hidden min-h-[420px]">
			<DataPanel
				{datasets}
				loading={loading && datasets.length === 0}
				selectedSymbol={selectedDataset?.symbol ?? null}
				selectedTimeframe={selectedDataset?.timeframe ?? null}
				on:select={handlePanelSelect}
				on:refresh={handlePanelRefresh}
			/>
		</div>
		<div class="border border-[#222] rounded bg-[#0a0a0a] overflow-hidden min-h-[420px]">
			<DataInspector
				bind:mode={inspectorMode}
				{selectedDataset}
				{quality}
				{qualityLoading}
				on:fetched={handleFetched}
				on:refresh={handlePanelRefresh}
				on:viewSeries={(e) => (drillSeries = e.detail)}
			/>
		</div>
	</section>
	{/if}

	{#if activeTab === 'data-log'}
	<DataActivityLog />
	{/if}

</div>
