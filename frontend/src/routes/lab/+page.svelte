<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		getForvenStrategiesQuery,
		getNowWorking,
		getPipelineSettings,
		transitionStage,
		reviveFromGraveyard,
		deleteStrategy,
		batchDeleteStrategies,
		batchTransitionStrategies,
		type NowWorkingRow,
	} from '$lib/api';
	import {
		parseManagerRow,
		isArchivedStage,
		isParkedStage,
		isTrueArchivedStage,
		normalizeStage,
		stageClass,
		type ManagerRow,
	} from '$lib/utils/strategy';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import StrategyLink from '$lib/components/ui/StrategyLink.svelte';
	import SortableTh from '$lib/components/ui/SortableTh.svelte';
	import StrategyExportMenu from '$lib/components/strategy/StrategyExportMenu.svelte';
	import StrategyImportDialog from '$lib/components/strategy/StrategyImportDialog.svelte';
	import { getHealthStatus } from '$lib/api/forven';
	import type { HealthStatusResponse } from '$lib/api/types';
	import type { StrategyImportResult } from '$lib/api';

	type Bucket = 'active' | 'parked' | 'trash';
	type SortField = 'created' | 'cagr' | 'in_sample_cagr' | 'out_of_sample_cagr' | 'return' | 'sharpe' | 'in_sample_sharpe' | 'out_of_sample_sharpe' | 'robustness' | 'drawdown' | 'win_rate' | 'trades' | 'profit_factor';
	type SortDirection = 'asc' | 'desc';
	type GraveyardStrategyLimitMode = 'capped' | 'unlimited';
	const STRATEGY_FETCH_PAGE_SIZE = 1000;
	const STRATEGY_FETCH_MAX_PAGES = 100;
	const GRAVEYARD_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
	// While the user is actively sitting on the Graveyard tab we refresh more often,
	// but still throttled so a stream of WS events doesn't refetch on every tick.
	const GRAVEYARD_VISIBLE_REFRESH_MS = 15 * 1000;
	const DEFAULT_GRAVEYARD_STRATEGY_LIMIT = 500;
	const FOREGROUND_STRATEGY_STATUSES = ['quick_screen', 'gauntlet', 'paper', 'live_graduated', 'research_only', 'backtest_failed'];
	const GRAVEYARD_STRATEGY_STATUSES = ['archived', 'rejected'];

	let loading = true;
	let actionMsg: string | null = null;
	let error: string | null = null;
	let realtime: RealtimeRefreshController | null = null;
	let nowWorkingRealtime: RealtimeRefreshController | null = null;
	let nowWorkingRows: NowWorkingRow[] = [];
	let nowWorkingError: string | null = null;
	let nowWorkingLoaded = false;
	let healthData: HealthStatusResponse | null = null;
	let healthError: string | null = null;
	let healthLoaded = false;
	let healthRealtime: RealtimeRefreshController | null = null;
	let graveyardLoading = false;
	let loadDataRunning = false;
	let loadDataPending = false;
	let loadDataPendingForceGraveyard = false;
	let graveyardLoadedAt = 0;
	let graveyardStrategyLimitMode: GraveyardStrategyLimitMode = 'capped';
	let graveyardStrategyLimit = DEFAULT_GRAVEYARD_STRATEGY_LIMIT;
	// Session-only "Load all" override: lets the user pull the full graveyard past the
	// configured cap without mutating persisted pipeline settings. Survives the settings
	// re-read inside loadData (which would otherwise reset the mode to 'capped').
	let graveyardLoadAllOverride = false;

	// Manager table state
	let bucket: Bucket = 'active';
	let search = '';
	let symbolFilter = 'all';
	let stageFilter = 'all';
	let sortBy: SortField = 'created';
	let sortDirection: SortDirection = 'desc';

	let activeResults: ManagerRow[] = [];
	let parkedResults: ManagerRow[] = [];
	let trashResults: ManagerRow[] = [];
	
	let highlightedId: string | null = null;
	let highlightTimer: ReturnType<typeof setTimeout> | null = null;
	let actionMsgTimer: ReturnType<typeof setTimeout> | null = null;

	let selectedIds = new Set<string>();
	let selectedInView = 0;
	let currentPage = 1;
	let pageCount = 1;
	let pageSize = 100;
	let activePageRows: ManagerRow[] = [];
	let parkedPageRows: ManagerRow[] = [];
	let trashPageRows: ManagerRow[] = [];
	let lastViewSignature = '';
	let pipelineActiveCount = 0;
	let researchOnlyCount = 0;

	function normalizeGraveyardStrategyLimitMode(value: unknown): GraveyardStrategyLimitMode {
		const normalized = String(value ?? '').trim().toLowerCase();
		return normalized === 'unlimited' ? 'unlimited' : 'capped';
	}

	function normalizeGraveyardStrategyLimit(value: unknown): number {
		const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
		return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : DEFAULT_GRAVEYARD_STRATEGY_LIMIT;
	}

	function configuredGraveyardMaxRows(): number | null {
		if (graveyardLoadAllOverride) return null;
		return graveyardStrategyLimitMode === 'unlimited' ? null : graveyardStrategyLimit;
	}

	async function loadAllGraveyard() {
		graveyardLoadAllOverride = true;
		await loadData({ forceGraveyard: true });
	}

	async function loadPipelineCapacitySettings() {
		try {
			const settings = await getPipelineSettings();
			graveyardStrategyLimitMode = normalizeGraveyardStrategyLimitMode(settings.graveyard_strategy_limit_mode);
			graveyardStrategyLimit = normalizeGraveyardStrategyLimit(settings.graveyard_strategy_limit);
		} catch {
			graveyardStrategyLimitMode = 'capped';
			graveyardStrategyLimit = DEFAULT_GRAVEYARD_STRATEGY_LIMIT;
		}
	}

	async function loadStrategyRowsForStatus(status: string, maxRows: number | null = null): Promise<ManagerRow[]> {
		const parsedRows: ManagerRow[] = [];
		const seenIds = new Set<string>();
		let previousSignature = '';
		for (let pageIndex = 0; pageIndex < STRATEGY_FETCH_MAX_PAGES; pageIndex += 1) {
			const remaining = maxRows === null ? STRATEGY_FETCH_PAGE_SIZE : maxRows - parsedRows.length;
			if (remaining <= 0) break;
			const pageLimit = maxRows === null ? STRATEGY_FETCH_PAGE_SIZE : Math.min(STRATEGY_FETCH_PAGE_SIZE, remaining);
			const offset = pageIndex * STRATEGY_FETCH_PAGE_SIZE;
			const page = await getForvenStrategiesQuery({
				status,
				limit: pageLimit,
				offset,
			});
			const pageRows = page.map((row) => parseManagerRow(row));
			const signature = pageRows.map((row) => row.id).join('|');
			const uniqueRows = pageRows.filter((row) => {
				if (!row.id || seenIds.has(row.id)) return false;
				seenIds.add(row.id);
				return true;
			});
			parsedRows.push(...uniqueRows);
			if (maxRows !== null && parsedRows.length >= maxRows) break;
			if (page.length < pageLimit) break;
			if (uniqueRows.length === 0 || signature === previousSignature) break;
			previousSignature = signature;
		}
		return parsedRows;
	}

	async function loadStrategyRowsForStatuses(statuses: string[], options: { maxRows?: number | null } = {}): Promise<ManagerRow[]> {
		const byId = new Map<string, ManagerRow>();
		const maxRows = typeof options.maxRows === 'number' ? Math.max(0, Math.floor(options.maxRows)) : null;
		if (maxRows === null) {
			const pages = await Promise.all(statuses.map((status) => loadStrategyRowsForStatus(status)));
			for (const row of pages.flat()) {
				if (row.id && !byId.has(row.id)) byId.set(row.id, row);
			}
		} else {
			for (const status of statuses) {
				const remaining = maxRows - byId.size;
				if (remaining <= 0) break;
				const rows = await loadStrategyRowsForStatus(status, remaining);
				for (const row of rows) {
					if (row.id && !byId.has(row.id)) byId.set(row.id, row);
					if (byId.size >= maxRows) break;
				}
			}
		}
		return Array.from(byId.values());
	}

	function applyForegroundRows(parsedRows: ManagerRow[]) {
		activeResults = parsedRows.filter((row) => !isArchivedStage(row.stage));
		parkedResults = parsedRows.filter((row) => isParkedStage(row.stage));
	}

	function applyGraveyardRows(parsedRows: ManagerRow[]) {
		const archivedRows = parsedRows
			.filter((row) => isTrueArchivedStage(row.stage))
			.map((row) => ({ ...row, deleted_at: row.deleted_at || row.created_at }));
		const trashById = new Map<string, ManagerRow>();
		for (const row of archivedRows) {
			if (!trashById.has(row.id)) trashById.set(row.id, row);
		}
		trashResults = Array.from(trashById.values());
	}

	function isPipelineActiveStage(stage: string): boolean {
		const normalized = normalizeStage(stage);
		return normalized === 'quick_screen'
			|| normalized === 'gauntlet'
			|| normalized === 'paper'
			|| normalized === 'live_graduated';
	}

	function clearSelection() {
		selectedIds = new Set();
	}

	function toggleSelect(id: string) {
		const next = new Set(selectedIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedIds = next;
	}

	function selectFiltered() {
		const next = new Set(selectedIds);
		for (const row of rowsInView) next.add(row.id);
		selectedIds = next;
	}

	function toggleSelectAll() {
		// Header tri-state checkbox: fully selected -> clear in-view, otherwise select all matching.
		if (rowsInView.length > 0 && selectedInView === rowsInView.length) clearFiltered();
		else selectFiltered();
	}

	function clearFiltered() {
		const filteredIds = new Set(rowsInView.map((row) => row.id));
		const next = new Set<string>();
		for (const id of selectedIds) {
			if (!filteredIds.has(id)) next.add(id);
		}
		selectedIds = next;
	}

	function formatDateTime(value: string): string {
		const d = new Date(value);
		if (Number.isNaN(d.getTime())) return '-';
		return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}

	function formatPercent(value: number | null | undefined, decimals = 1): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '-';
		return `${value.toFixed(decimals)}%`;
	}

	function formatNumber(value: number | null | undefined, decimals = 2): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '-';
		return value.toFixed(decimals);
	}

	function metricClass(kind: 'return' | 'sharpe' | 'robustness' | 'drawdown' | 'win_rate' | 'profit_factor', value: number | null): string {
		if (value === null || !Number.isFinite(value)) return 'text-gray-600';
		switch (kind) {
			case 'return':
				return value >= 0 ? 'text-emerald-400' : 'text-red-400';
			case 'sharpe':
				if (value >= 1.0) return 'text-emerald-400';
				if (value >= 0.5) return 'text-blue-300';
				if (value > 0) return 'text-yellow-300';
				return 'text-red-400';
			case 'robustness':
				if (value >= 70) return 'text-emerald-400';
				if (value >= 50) return 'text-yellow-300';
				return 'text-red-400';
			case 'drawdown':
				if (value <= 20) return 'text-emerald-400';
				if (value <= 35) return 'text-yellow-300';
				return 'text-red-400';
			case 'win_rate':
				if (value >= 55) return 'text-emerald-400';
				if (value >= 45) return 'text-yellow-300';
				return 'text-red-400';
			case 'profit_factor':
				if (value >= 1.5) return 'text-emerald-400';
				if (value >= 1.0) return 'text-yellow-300';
				return 'text-red-400';
		}
	}

	function badgeClass(kind: 'source' | 'untested'): string {
		return kind === 'source'
			? 'text-cyan-300 border-cyan-700 bg-cyan-900/20'
			: 'text-amber-300 border-amber-700 bg-amber-900/20';
	}

	function recoveryBadge(row: ManagerRow): { label: string; className: string } | null {
		const status = (row.recovery_status || '').trim().toLowerCase();
		if (!status) return null;
		if (status === 'repair_pending' || status === 'repair_running') {
			return {
				label: 'Repairing',
				className: 'text-rose-300 border-rose-700 bg-rose-950/20'
			};
		}
		if (status === 'replay_running' || status === 'final_retry_running') {
			return {
				label: 'Healing',
				className: 'text-amber-300 border-amber-700 bg-amber-900/20'
			};
		}
		if (status === 'exhausted') {
			return {
				label: 'Failed Recovery',
				className: 'text-red-300 border-red-700 bg-red-950/20'
			};
		}
		return null;
	}

	function toggleSort(field: SortField) {
		if (sortBy === field) {
			sortDirection = sortDirection === 'desc' ? 'asc' : 'desc';
			return;
		}
		sortBy = field;
		// Higher-is-better metrics (and 'created') default to descending so the first
		// click surfaces the best/newest first; drawdown is a positive magnitude where
		// lower is better, so it defaults to ascending (least drawdown first).
		sortDirection = field === 'drawdown' ? 'asc' : 'desc';
	}

	function sortIndicator(field: SortField): string {
		if (sortBy !== field) return '';
		return sortDirection === 'desc' ? ' ▼' : ' ▲';
	}

	function compareNumeric(a: number, b: number, direction: SortDirection): number {
		if (a < b) return direction === 'asc' ? -1 : 1;
		if (a > b) return direction === 'asc' ? 1 : -1;
		return 0;
	}

	function activeSortValue(row: ManagerRow, field: SortField): number {
		switch (field) {
			case 'created': return Date.parse(row.created_at) || 0;
			case 'cagr': return row.annualized_return ?? Number.NEGATIVE_INFINITY;
			case 'in_sample_cagr': return row.in_sample_cagr ?? Number.NEGATIVE_INFINITY;
			case 'out_of_sample_cagr': return row.out_of_sample_cagr ?? Number.NEGATIVE_INFINITY;
			case 'return': return row.total_return ?? Number.NEGATIVE_INFINITY;
			// Use NEGATIVE_INFINITY (not 0) so unmeasured rows sink below genuinely
			// negative values (e.g. a real Sharpe of -0.8) on a descending sort,
			// matching how the CAGR/return columns above already handle missing data.
			case 'sharpe': return row.sharpe_ratio ?? Number.NEGATIVE_INFINITY;
			case 'in_sample_sharpe': return row.in_sample_sharpe ?? Number.NEGATIVE_INFINITY;
			case 'out_of_sample_sharpe': return row.out_of_sample_sharpe ?? Number.NEGATIVE_INFINITY;
			case 'robustness': return row.robustness_score ?? Number.NEGATIVE_INFINITY;
			// Drawdown is a positive magnitude where lower is better, so unmeasured rows
			// must sort to the worst (highest) end rather than masquerade as a perfect 0%.
			case 'drawdown': return row.max_drawdown ?? Number.POSITIVE_INFINITY;
			case 'win_rate': return row.win_rate ?? Number.NEGATIVE_INFINITY;
			case 'trades': return row.total_trades ?? Number.NEGATIVE_INFINITY;
			// Infinite profit factor (no losing trades) is the strongest possible PF and
			// must sort to the top; a missing PF sinks to the bottom on a descending sort.
			case 'profit_factor': return row.profit_factor_is_infinite ? Number.POSITIVE_INFINITY : (row.profit_factor ?? Number.NEGATIVE_INFINITY);
			default: return 0;
		}
	}

	function graveyardSortValue(row: ManagerRow, field: SortField): number {
		switch (field) {
			case 'created': return Date.parse(row.created_at) || 0;
			default: return activeSortValue(row, field);
		}
	}

	function goPrevPage() { currentPage = Math.max(1, currentPage - 1); }
	function goNextPage() { currentPage = Math.min(pageCount, currentPage + 1); }

	function openContainer(row: ManagerRow) {
		goto(`/lab/strategy/${encodeURIComponent(row.id)}?returnTo=${encodeURIComponent('/lab')}`);
	}

	let showImportDialog = false;

	function onStrategyImported(result: StrategyImportResult) {
		showImportDialog = false;
		if (result.ok && result.strategy_id) {
			openContainer({ id: result.strategy_id } as ManagerRow);
		}
	}

	function healthStateLabel(state: string | null | undefined): string {
		switch (state) {
			case 'green': return 'Healthy';
			case 'amber': return 'Degraded';
			case 'red': return 'Critical';
			default: return 'Unknown';
		}
	}

	function healthDotClass(state: string | null | undefined): string {
		switch (state) {
			case 'green': return 'bg-emerald-400';
			case 'amber': return 'bg-yellow-400';
			case 'red': return 'bg-red-400';
			default: return 'bg-gray-500';
		}
	}

	function healthTextClass(state: string | null | undefined): string {
		switch (state) {
			case 'green': return 'text-emerald-400';
			case 'amber': return 'text-yellow-300';
			case 'red': return 'text-red-400';
			default: return 'text-gray-400';
		}
	}

	function friendlyHealthName(name: string): string {
		return name
			.replace(/^bot:/, '')
			.replace(/_/g, ' ')
			.replace(/\b\w/g, (c) => c.toUpperCase());
	}

	async function loadData(options: { forceGraveyard?: boolean } = {}) {
		const forceGraveyard = options.forceGraveyard === true;
		if (loadDataRunning) {
			loadDataPending = true;
			loadDataPendingForceGraveyard = loadDataPendingForceGraveyard || forceGraveyard;
			return;
		}
		loadDataRunning = true;
		error = null;
		try {
			const foregroundRows = await loadStrategyRowsForStatuses(FOREGROUND_STRATEGY_STATUSES);
			applyForegroundRows(foregroundRows);
			loading = false;

			// graveyardLoadedAt starts at 0, so the interval clause fires the initial
			// load on mount. Graveyard-mutating actions pass forceGraveyard:true for an
			// immediate refresh; otherwise we only refetch when the Graveyard tab is open
			// (throttled) or the long staleness window has elapsed — so an empty graveyard
			// no longer triggers a full re-fetch on every realtime tick.
			const shouldRefreshGraveyard = forceGraveyard
				|| (bucket === 'trash' && Date.now() - graveyardLoadedAt > GRAVEYARD_VISIBLE_REFRESH_MS)
				|| Date.now() - graveyardLoadedAt > GRAVEYARD_REFRESH_INTERVAL_MS;
			if (shouldRefreshGraveyard) {
				await loadPipelineCapacitySettings();
				graveyardLoading = trashResults.length === 0;
				const graveyardRows = await loadStrategyRowsForStatuses(
					GRAVEYARD_STRATEGY_STATUSES,
					{ maxRows: configuredGraveyardMaxRows() },
				);
				applyGraveyardRows(graveyardRows);
				graveyardLoadedAt = Date.now();
			}
		} catch (e) {
			const message = e instanceof Error ? e.message : 'Failed to load strategy containers.';
			// Only surface a blocking banner on the initial load (no rows yet). A transient
			// failure during a background/realtime refresh must not clobber an otherwise
			// healthy table full of previously-loaded rows.
			if (activeResults.length === 0 && parkedResults.length === 0) {
				error = message;
			} else {
				console.warn('[lab] background refresh failed:', message);
			}
		} finally {
			loading = false;
			graveyardLoading = false;
			loadDataRunning = false;
			if (loadDataPending) {
				const pendingForceGraveyard = loadDataPendingForceGraveyard;
				loadDataPending = false;
				loadDataPendingForceGraveyard = false;
				void loadData({ forceGraveyard: pendingForceGraveyard });
			}
		}
	}

	async function loadNowWorking() {
		try {
			nowWorkingRows = await getNowWorking();
			nowWorkingError = null;
		} catch (e) {
			nowWorkingError = e instanceof Error ? e.message : 'Failed to load active work';
		} finally {
			nowWorkingLoaded = true;
		}
	}

	async function loadHealth() {
		try {
			healthData = await getHealthStatus();
			healthError = null;
		} catch (e) {
			healthError = e instanceof Error ? e.message : 'Health monitor unavailable';
		} finally {
			healthLoaded = true;
		}
	}

	async function refreshAll() {
		await Promise.all([
			loadData({ forceGraveyard: true }),
			loadNowWorking(),
			loadHealth(),
		]);
	}

	// Run per-row writes one at a time (not Promise.all). Each transition opens a
	// write transaction and the promotion gate holds the WAL writer lock; firing
	// them concurrently serializes/stalls server-side and loses per-row results.
	// Sequential execution keeps writes orderly and reports partial failures.
	async function runSequential(ids: string[], op: (id: string) => Promise<unknown>): Promise<{ succeeded: number; failed: number }> {
		let succeeded = 0;
		let failed = 0;
		for (const id of ids) {
			try {
				await op(id);
				succeeded += 1;
			} catch {
				failed += 1;
			}
		}
		return { succeeded, failed };
	}

	async function runBatchAction(action: 'trash' | 'archive' | 'recover' | 'recover_parked' | 'delete') {
		const ids = rowsInView.map((row) => row.id).filter((id) => selectedIds.has(id));
		if (ids.length === 0) return;

		error = null;
		actionMsg = null;

		try {
			if (action === 'trash') {
				if (!confirm(`Move ${ids.length} containers to graveyard?`)) return;
				const { succeeded, failed } = await runSequential(ids, (id) => transitionStage(id, 'graveyard', 'User moved to graveyard from Lab Manager', 'manual'));
				actionMsg = `Moved ${succeeded} container${succeeded === 1 ? '' : 's'} to graveyard.`;
				if (failed > 0) actionMsg += ` ${failed} failed.`;
			} else if (action === 'archive') {
				if (!confirm(`Archive ${ids.length} strategies? They can be recovered later.`)) return;
				const result = await batchTransitionStrategies(ids, 'archived', 'Batch archived from Lab Manager');
				const count = result.transitioned.length;
				actionMsg = `Archived ${count} strateg${count === 1 ? 'y' : 'ies'}.`;
				if (result.failed.length > 0) {
					actionMsg += ` ${result.failed.length} failed.`;
				}
			} else if (action === 'recover') {
				const { succeeded, failed } = await runSequential(ids, (id) => reviveFromGraveyard(id));
				actionMsg = `Recovered ${succeeded} container${succeeded === 1 ? '' : 's'} from graveyard.`;
				if (failed > 0) actionMsg += ` ${failed} failed.`;
			} else if (action === 'recover_parked') {
				// Parked (research_only) recovery is a stage transition back to the active
				// pipeline — NOT a graveyard revive (which is reviveFromGraveyard).
				const { succeeded, failed } = await runSequential(ids, (id) => transitionStage(id, 'researching', 'User batch-recovered research_only container from Lab Manager', 'manual'));
				actionMsg = `Recovered ${succeeded} container${succeeded === 1 ? '' : 's'} to the active pipeline.`;
				if (failed > 0) actionMsg += ` ${failed} failed.`;
			} else if (action === 'delete') {
				if (!confirm(`Permanently delete ${ids.length} strategies? This cannot be undone.`)) return;
				const result = await batchDeleteStrategies(ids);
				const count = result.deleted.length;
				actionMsg = `Permanently deleted ${count} strateg${count === 1 ? 'y' : 'ies'}.`;
				if (result.not_found.length > 0) actionMsg += ` ${result.not_found.length} already gone.`;
			}
			await loadData({ forceGraveyard: true });
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Batch action failed';
		}
	}

	async function trashOne(id: string) {
		if (!confirm('Move this container to graveyard? You can recover it later.')) return;
		try {
			await transitionStage(id, 'graveyard', 'User moved to graveyard from Lab Manager', 'manual');
			actionMsg = 'Container moved to graveyard.';
			await loadData({ forceGraveyard: true });
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to move container to graveyard';
		}
	}

	async function deleteOne(id: string) {
		if (!confirm('Permanently delete this strategy? This cannot be undone.')) return;
		try {
			await deleteStrategy(id);
			actionMsg = 'Strategy permanently deleted.';
			await loadData({ forceGraveyard: true });
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete strategy';
		}
	}

	async function restoreOne(id: string) {
		try {
			await reviveFromGraveyard(id);
			actionMsg = 'Container recovered from graveyard.';
			await loadData({ forceGraveyard: true });
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to recover container';
		}
	}

	async function recoverParked(id: string) {
		try {
			await transitionStage(id, 'researching', 'User recovered research_only container from Lab Manager', 'manual');
			actionMsg = 'Container recovered to the active pipeline.';
			await loadData();
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to recover container';
		}
	}

	async function moveOneToStage(id: string, stage: string, selectEl: HTMLSelectElement) {
		if (!stage) return;
		selectEl.value = ''; // Reset select
		try {
			await transitionStage(id, stage, `User manually moved to ${stage} from Lab Manager`, 'manual');
			actionMsg = `Container moved to ${stage}.`;
			await loadData({ forceGraveyard: true });
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to move container to ${stage}`;
		}
	}

	async function moveBatchToStage(stage: string, selectEl: HTMLSelectElement) {
		if (!stage) return;
		selectEl.value = ''; // Reset select
		const ids = rowsInView.map((row) => row.id).filter((id) => selectedIds.has(id));
		if (ids.length === 0) return;
		
		if (!confirm(`Move ${ids.length} containers to ${stage}?`)) return;

		error = null;
		actionMsg = null;

		try {
			const { succeeded, failed } = await runSequential(ids, (id) => transitionStage(id, stage, `User batch moved to ${stage} from Lab Manager`, 'manual'));
			actionMsg = `Moved ${succeeded} container${succeeded === 1 ? '' : 's'} to ${stage}.`;
			if (failed > 0) actionMsg += ` ${failed} failed.`;
			await loadData({ forceGraveyard: true });
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to batch move containers to ${stage}`;
		}
	}

	$: activeFiltered = (() => {
		// Only the visible bucket is filtered/sorted; the others return [] so a single
		// keystroke doesn't re-filter+re-sort thousands of off-screen graveyard rows.
		if (bucket !== 'active') return [];
		const query = search.trim().toLowerCase();
		const sortField = sortBy;
		const direction = sortDirection;
		const filtered = activeResults.filter((row) => {
			if (symbolFilter !== 'all' && row.symbol !== symbolFilter) return false;
			if (stageFilter !== 'all' && row.stage !== stageFilter) return false;
			if (query) {
				if (
					!row.name.toLowerCase().includes(query)
					&& !row.symbol.toLowerCase().includes(query)
					&& !row.timeframe.toLowerCase().includes(query)
					&& !row.id.toLowerCase().includes(query)
				) return false;
			}
			return true;
		});

		const ranked = filtered.map((row, index) => ({ row, index, sortValue: activeSortValue(row, sortField) }));
		ranked.sort((a, b) => {
			const cmp = compareNumeric(a.sortValue, b.sortValue, direction);
			if (cmp !== 0) return cmp;
			return a.index - b.index;
		});
		return ranked.map((entry) => entry.row);
	})();

	$: trashFiltered = (() => {
		if (bucket !== 'trash') return [];
		const query = search.trim().toLowerCase();
		const sortField = sortBy;
		const direction = sortDirection;
		const filtered = trashResults.filter((row) => {
			if (!query) return true;
			return (
				row.name.toLowerCase().includes(query)
				|| row.symbol.toLowerCase().includes(query)
				|| row.timeframe.toLowerCase().includes(query)
				|| row.id.toLowerCase().includes(query)
			);
		});
		const ranked = filtered.map((row, index) => ({ row, index, sortValue: graveyardSortValue(row, sortField) }));
		ranked.sort((a, b) => {
			const cmp = compareNumeric(a.sortValue, b.sortValue, direction);
			if (cmp !== 0) return cmp;
			return a.index - b.index;
		});
		return ranked.map((entry) => entry.row);
	})();

	$: parkedFiltered = (() => {
		if (bucket !== 'parked') return [];
		const query = search.trim().toLowerCase();
		const sortField = sortBy;
		const direction = sortDirection;
		const filtered = parkedResults.filter((row) => {
			if (!query) return true;
			return (
				row.name.toLowerCase().includes(query)
				|| row.symbol.toLowerCase().includes(query)
				|| row.timeframe.toLowerCase().includes(query)
				|| row.id.toLowerCase().includes(query)
			);
		});
		const ranked = filtered.map((row, index) => ({ row, index, sortValue: graveyardSortValue(row, sortField) }));
		ranked.sort((a, b) => {
			const cmp = compareNumeric(a.sortValue, b.sortValue, direction);
			if (cmp !== 0) return cmp;
			return a.index - b.index;
		});
		return ranked.map((entry) => entry.row);
	})();

	$: rowsInView = bucket === 'active' ? activeFiltered : bucket === 'parked' ? parkedFiltered : trashFiltered;
	$: pageCount = Math.max(1, Math.ceil(rowsInView.length / pageSize));
	$: if (currentPage > pageCount) currentPage = pageCount;
	$: {
		const signature = [
			bucket,
			search.trim().toLowerCase(),
			symbolFilter,
			stageFilter,
			sortBy,
			sortDirection,
			String(pageSize),
		].join('|');
		if (signature !== lastViewSignature) {
			lastViewSignature = signature;
			currentPage = 1;
		}
	}
	$: activePageRows = activeFiltered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
	$: parkedPageRows = parkedFiltered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
	$: trashPageRows = trashFiltered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
	$: selectedInView = rowsInView.reduce((count, row) => (selectedIds.has(row.id) ? count + 1 : count), 0);
	// Auto-dismiss the success banner after a few seconds; re-arm on each new message
	// (a single tracked timer so rapid successive actions don't leak overlapping timers).
	// The error banner is intentionally sticky — failures are higher-stakes — and is
	// cleared only by the user (× button) or the next action.
	$: if (actionMsg) {
		if (actionMsgTimer) clearTimeout(actionMsgTimer);
		actionMsgTimer = setTimeout(() => {
			actionMsg = null;
			actionMsgTimer = null;
		}, 5000);
	}
	$: selectAllChecked = rowsInView.length > 0 && selectedInView === rowsInView.length;
	$: selectAllIndeterminate = selectedInView > 0 && selectedInView < rowsInView.length;
	$: pipelineActiveCount = activeResults.filter((row) => isPipelineActiveStage(row.stage)).length;
	$: researchOnlyCount = parkedResults.filter((row) => normalizeStage(row.stage) === 'research_only').length;
	$: failedHealthChecks = (healthData?.data_checks ?? []).filter((check) => !check.passed);
	$: healthSummaryLabel = healthError ? 'Unavailable' : !healthLoaded ? 'Loading' : healthStateLabel(healthData?.overall);
	$: healthSummaryClass = healthError ? 'text-red-400' : healthTextClass(healthData?.overall);
	$: healthSummaryDot = healthError ? 'bg-red-400' : healthDotClass(healthData?.overall);
	$: nowWorkingSummaryLabel = nowWorkingError ? 'Error' : !nowWorkingLoaded ? 'Loading' : `${nowWorkingRows.length} active`;
	$: graveyardCapped = !graveyardLoadAllOverride && graveyardStrategyLimitMode === 'capped' && trashResults.length >= graveyardStrategyLimit;

	$: symbolOptions = ['all', ...new Set(activeResults.map((row) => row.symbol).sort())];
	$: stageOptions = ['all', ...new Set(activeResults.map((row) => row.stage).sort())];

	async function triggerHighlight(id: string) {
		highlightedId = id;
		await tick();
		const el = document.querySelector(`[data-strategy-id="${CSS.escape(id)}"]`);
		if (el instanceof HTMLElement) {
			el.scrollIntoView({ behavior: 'smooth', block: 'center' });
		}
		if (highlightTimer) clearTimeout(highlightTimer);
		highlightTimer = setTimeout(() => {
			highlightedId = null;
			highlightTimer = null;
		}, 3000);

		const url = new URL(window.location.href);
		url.searchParams.delete('highlight');
		history.replaceState(history.state, '', url.toString());
	}

	onMount(async () => {
		const highlightParam = $page?.url?.searchParams?.get('highlight') ?? null;
		await refreshAll();
		if (highlightParam) {
			void triggerHighlight(highlightParam);
		}
		realtime = createRealtimeRefresh(loadData, {
			fallbackMs: 30_000,
			wsDebounceMs: 1200,
			// The strategy roster only changes on lifecycle/task events. Omit the
			// high-frequency 'trade' event (which fires continuously while paper/live
			// strategies trade) so we don't run a full multi-status re-fetch + re-parse
			// on every fill; the 30s fallback poll covers anything missed.
			wsEvents: [
				'task_queued',
				'task_status_changed',
				'task_completed',
				'task_failed',
				'strategy_transition',
				'strategy_promoted',
				'kill_switch_activated',
				'kill_switch_cleared',
				'risk_alert',
			],
		});
		realtime.start();
		nowWorkingRealtime = createRealtimeRefresh(loadNowWorking, {
			fallbackMs: 5_000,
			wsDebounceMs: 500,
			pollWhenWsOfflineOnly: false,
		});
		nowWorkingRealtime.start();
		healthRealtime = createRealtimeRefresh(loadHealth, {
			fallbackMs: 10_000,
			wsDebounceMs: 1200,
		});
		healthRealtime.start();
	});

	onDestroy(() => {
		if (typeof document !== 'undefined') {
			document.body.style.overflow = '';
		}
		realtime?.stop();
		realtime = null;
		nowWorkingRealtime?.stop();
		nowWorkingRealtime = null;
		healthRealtime?.stop();
		healthRealtime = null;
		if (highlightTimer) {
			clearTimeout(highlightTimer);
			highlightTimer = null;
		}
		if (actionMsgTimer) {
			clearTimeout(actionMsgTimer);
			actionMsgTimer = null;
		}
	});
</script>

<svelte:head>
	<title>The Forge | Forven</title>
	<meta name="description" content="Manage all containers of the strategy type, complete with research metrics." />
</svelte:head>

<div class="h-full flex flex-col overflow-hidden">
	<div class="px-4 py-3 bg-[#050505] border-b border-[#222] flex-shrink-0">
		<div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
			<div>
				<h1 class="text-xl font-bold tracking-tight text-white">The Forge</h1>
				<p class="text-xs text-gray-500 mt-1">
					{rowsInView.length} in view · Pipeline {pipelineActiveCount} · Research-only {researchOnlyCount}
				</p>
			</div>
			<div class="flex items-center gap-2 self-start md:self-auto">
				<a
					href="/lab/backtests"
					class="text-xs border border-[#333] px-3 py-1.5 text-gray-400 hover:text-white hover:border-white transition-colors"
				>
					View All Backtests
				</a>
				<button
					type="button"
					data-testid="forge-import-strategy"
					on:click={() => (showImportDialog = true)}
					class="text-xs border border-[#333] px-3 py-1.5 text-gray-400 hover:text-white hover:border-white transition-colors"
				>
					⤒ Import
				</button>
				<button
					type="button"
					on:click={refreshAll}
					class="text-xs border border-[#333] px-3 py-1.5 text-gray-400 hover:text-white hover:border-white transition-colors"
				>
					Refresh
				</button>
			</div>
		</div>
	</div>

	<!-- Main Manager UI -->
	<section class="flex-1 flex flex-col overflow-hidden min-h-[500px]">
		<!-- Toolbar: search, filters, status, pagination -->
		<div data-testid="forge-manager-toolbar" class="border-b border-[#222] px-4 py-2 flex items-center gap-2 flex-wrap bg-[#050505]">
			<input
				type="text"
				bind:value={search}
				placeholder={bucket === 'active' ? 'Search container, symbol, timeframe, id…' : bucket === 'parked' ? 'Search parked…' : 'Search graveyard…'}
				class="bg-black border border-[#333] px-3 py-1.5 text-xs w-full focus:outline-none focus:border-white sm:w-72"
			/>
			{#if bucket === 'active'}
				<select
					aria-label="Filter by symbol"
					bind:value={symbolFilter}
					class="terminal-input !w-full !py-1 !px-2 text-xs sm:!w-44"
				>
					{#each symbolOptions as symbol}
						<option value={symbol}>{symbol === 'all' ? 'All symbols' : symbol}</option>
					{/each}
				</select>
				<select
					aria-label="Filter by stage"
					bind:value={stageFilter}
					class="terminal-input !w-full !py-1 !px-2 text-xs sm:!w-44"
				>
					{#each stageOptions as stage}
						<option value={stage}>{stage === 'all' ? 'All stages' : stage}</option>
					{/each}
				</select>
			{/if}
			<details
				data-testid="forge-health-chip"
				class="relative w-full text-xs border border-[#333] bg-[#0a0a0a] text-gray-400 open:border-[#555] xl:w-auto"
			>
				<summary class="list-none cursor-pointer px-2.5 py-1.5 inline-flex w-full items-center gap-2 xl:w-auto">
					<span class={`inline-flex h-2 w-2 rounded-full ${healthSummaryDot}`}></span>
					<span class="uppercase tracking-[0.14em] text-[10px]">System Health</span>
					<span class={`font-medium ${healthSummaryClass}`}>{healthSummaryLabel}</span>
					{#if failedHealthChecks.length > 0}
						<span class="text-[10px] text-red-300">{failedHealthChecks.length} issue{failedHealthChecks.length !== 1 ? 's' : ''}</span>
					{/if}
				</summary>
				<div class="absolute left-0 top-full z-30 mt-1 max-h-[min(24rem,calc(100vh-12rem))] w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] overflow-y-auto border-t border-[#222] bg-[#080808] p-2 text-[11px] shadow-xl xl:w-80 xl:max-w-[420px]">
					{#if healthError}
						<div class="text-red-400 flex items-center gap-2">
							<span>{healthError}</span>
							<button type="button" class="underline" on:click={loadHealth}>retry</button>
						</div>
					{:else if !healthLoaded}
						<div class="text-gray-500">Loading…</div>
					{:else}
						<div class="space-y-1">
							{#each healthData?.components ?? [] as comp (comp.name)}
								<div class="flex items-center gap-2">
									<span class={`inline-flex h-1.5 w-1.5 rounded-full ${healthDotClass(comp.state)}`}></span>
									<span class="text-gray-300">{friendlyHealthName(comp.name)}</span>
									<span class={healthTextClass(comp.state)}>{healthStateLabel(comp.state)}</span>
									<span class="text-gray-600 truncate">{comp.message}</span>
								</div>
							{/each}
							{#if (healthData?.components ?? []).length === 0}
								<div class="text-gray-600">No components registered.</div>
							{/if}
						</div>
						{#if (healthData?.data_checks ?? []).length > 0}
							<div class="mt-2 border-t border-[#1a1a1a] pt-2 space-y-1">
								{#each healthData?.data_checks ?? [] as check (check.name)}
									<div class={check.passed ? 'text-gray-500' : check.severity === 'critical' ? 'text-red-400' : 'text-yellow-300'}>
										{friendlyHealthName(check.name)}: {check.detail}
									</div>
								{/each}
							</div>
						{/if}
					{/if}
				</div>
			</details>
			<details
				data-testid="forge-now-working-chip"
				class="relative w-full text-xs border border-[#333] bg-[#0a0a0a] text-gray-400 open:border-[#555] xl:w-auto"
			>
				<summary class="list-none cursor-pointer px-2.5 py-1.5 inline-flex w-full items-center gap-2 xl:w-auto">
					<span class={`inline-flex h-2 w-2 rounded-full ${nowWorkingError ? 'bg-red-400' : nowWorkingLoaded && nowWorkingRows.length > 0 ? 'bg-cyan-400' : 'bg-gray-500'}`}></span>
					<span class="uppercase tracking-[0.14em] text-[10px]">Now Working</span>
					<span class={nowWorkingError ? 'text-red-400 font-medium' : 'text-cyan-300 font-medium'}>{nowWorkingSummaryLabel}</span>
				</summary>
				<div class="absolute left-0 top-full z-30 mt-1 max-h-[min(24rem,calc(100vh-12rem))] w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] overflow-y-auto border-t border-[#222] bg-[#080808] p-2 text-[11px] shadow-xl xl:w-[32rem] xl:max-w-[520px]">
					{#if nowWorkingError}
						<div class="text-red-400 flex items-center gap-2">
							<span>Failed to load active work</span>
							<button type="button" class="underline" on:click={loadNowWorking}>retry</button>
						</div>
					{:else if !nowWorkingLoaded}
						<div class="text-gray-500">Loading…</div>
					{:else if nowWorkingRows.length === 0}
						<div class="text-gray-500">Engine idle.</div>
					{:else}
						<ul class="space-y-1">
							{#each nowWorkingRows as row (`${row.strategy_id}:${row.current_task.type}`)}
								{@const hasStrategy = !String(row.strategy_id).startsWith('task-')}
								<li>
									{#if hasStrategy}
										<button
											type="button"
											class="w-full text-left hover:bg-[#111] px-2 py-1 flex items-center gap-3"
											on:click={() => goto(`/lab/strategy/${encodeURIComponent(row.strategy_id)}?returnTo=${encodeURIComponent('/lab')}`)}
										>
											<span class="font-mono text-gray-300 truncate flex-1">{row.name}</span>
											{#if row.stage}
												<span class={`text-[10px] px-1.5 py-0.5 border rounded uppercase ${stageClass(row.stage)}`}>{row.stage}</span>
											{/if}
											<span class="text-gray-500">{row.current_task.type}</span>
											<span class="uppercase {row.current_task.status === 'running' ? 'text-emerald-400' : 'text-yellow-400'}">
												{row.current_task.status}
											</span>
											{#if row.current_task.stalled}
												<span class="uppercase text-red-400 border border-red-900 px-1">stalled</span>
											{/if}
										</button>
									{:else}
										<div class="w-full text-left px-2 py-1 flex items-center gap-3">
											<span class="font-mono text-gray-400 truncate flex-1">{row.name}</span>
											<span class="text-gray-500">{row.current_task.type}</span>
											<span class="uppercase {row.current_task.status === 'running' ? 'text-emerald-400' : 'text-yellow-400'}">
												{row.current_task.status}
											</span>
											{#if row.current_task.stalled}
												<span class="uppercase text-red-400 border border-red-900 px-1">stalled</span>
											{/if}
										</div>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			</details>
			<span class="text-[10px] text-gray-500 ml-1">
				{rowsInView.length} items
				{#if rowsInView.length > (bucket === 'active' ? activePageRows.length : bucket === 'parked' ? parkedPageRows.length : trashPageRows.length)}
					(showing {bucket === 'active' ? activePageRows.length : bucket === 'parked' ? parkedPageRows.length : trashPageRows.length})
				{/if}
			</span>
			<div class="ml-auto flex items-center gap-2 text-[10px] text-gray-500">
				<label for="manager-page-size">Rows</label>
				<select
					id="manager-page-size"
					class="terminal-input !py-1 !px-2"
					value={String(pageSize)}
					on:change={(event) => {
						const value = Number((event.currentTarget as HTMLSelectElement).value);
						pageSize = Number.isFinite(value) && value > 0 ? value : 100;
					}}
				>
					<option value="50">50</option>
					<option value="100">100</option>
					<option value="200">200</option>
					<option value="500">500</option>
				</select>
				<button
					type="button"
					class="px-2 py-1 border border-[#333] text-gray-400 hover:text-white hover:border-white disabled:opacity-40 disabled:cursor-not-allowed"
					on:click={goPrevPage}
					disabled={currentPage <= 1}
				>
					Prev
				</button>
				<span>{currentPage}/{pageCount}</span>
				<button
					type="button"
					class="px-2 py-1 border border-[#333] text-gray-400 hover:text-white hover:border-white disabled:opacity-40 disabled:cursor-not-allowed"
					on:click={goNextPage}
					disabled={currentPage >= pageCount}
				>
					Next
				</button>
			</div>
		</div>

		<!-- Bucket filter buttons (aria-pressed toggle semantics rather than a full
		     APG tablist, since there is no arrow-key tab navigation). -->
		<div role="group" aria-label="Strategy buckets" class="border-b border-[#222] bg-[#070707] px-4 flex items-end gap-1 flex-wrap">
			<button
				type="button"
				aria-pressed={bucket === 'active'}
				on:click={() => { bucket = 'active'; clearSelection(); }}
				class="relative px-4 py-2 text-xs font-medium transition-colors border-b-2 {bucket === 'active' ? 'border-white text-white' : 'border-transparent text-gray-400 hover:text-gray-200'}"
			>
				Open <span class="ml-1 text-gray-500">({activeResults.length})</span>
			</button>
			<button
				type="button"
				aria-pressed={bucket === 'parked'}
				on:click={() => { bucket = 'parked'; clearSelection(); }}
				class="relative px-4 py-2 text-xs font-medium transition-colors border-b-2 {bucket === 'parked' ? 'border-white text-white' : 'border-transparent text-gray-400 hover:text-gray-200'}"
			>
				Parked <span class="ml-1 text-gray-500">({parkedResults.length})</span>
			</button>
			<button
				type="button"
				aria-pressed={bucket === 'trash'}
				on:click={() => { bucket = 'trash'; clearSelection(); }}
				class="relative px-4 py-2 text-xs font-medium transition-colors border-b-2 {bucket === 'trash' ? 'border-white text-white' : 'border-transparent text-gray-400 hover:text-gray-200'}"
			>
				Graveyard <span class="ml-1 text-gray-500">({trashResults.length})</span>
				{#if graveyardLoading}
					<span class="ml-1 text-gray-600">loading</span>
				{/if}
			</button>
			{#if bucket === 'trash' && graveyardCapped}
				<span class="ml-3 pb-2 text-[10px] tracking-wide text-amber-400/80">
					Showing first {graveyardStrategyLimit} archived/rejected — older ones are not loaded.
				</span>
				<button
					type="button"
					class="ml-2 pb-2 text-[10px] underline text-amber-300 hover:text-amber-200 disabled:opacity-50"
					on:click={loadAllGraveyard}
					disabled={graveyardLoading}
				>
					{graveyardLoading ? 'Loading…' : 'Load all'}
				</button>
			{/if}
		</div>

		{#if actionMsg}
			<div role="status" aria-live="polite" class="mx-4 mt-3 bg-green-900/20 border border-green-800 text-green-300 text-xs px-3 py-2 rounded flex items-start gap-2">
				<span class="flex-1">{actionMsg}</span>
				<button type="button" class="shrink-0 text-green-300/70 hover:text-green-100 leading-none" aria-label="Dismiss message" on:click={() => (actionMsg = null)}>×</button>
			</div>
		{/if}
		{#if error}
			<div role="alert" aria-live="assertive" class="mx-4 mt-3 bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded flex items-start gap-2">
				<span class="flex-1">{error}</span>
				<button type="button" class="shrink-0 text-red-300/70 hover:text-red-100 leading-none" aria-label="Dismiss error" on:click={() => (error = null)}>×</button>
			</div>
		{/if}

		<!-- Bulk-action bar: only visible when rows are selected -->
		{#if selectedInView > 0}
			<div class="border-b border-[#222] bg-[#0c0c0c] px-4 py-2 flex items-center gap-3 text-xs">
				<span class="text-white font-medium">{selectedInView} selected</span>
				<button
					type="button"
					class="text-gray-400 hover:text-white transition-colors"
					on:click={selectFiltered}
				>
					Select all {rowsInView.length} matching
				</button>
				<button
					type="button"
					class="text-gray-400 hover:text-white transition-colors"
					on:click={clearSelection}
				>
					Clear selection
				</button>
				<div class="ml-auto flex items-center gap-2">
					{#if bucket === 'active'}
						<select
							aria-label="Move selected strategies to stage"
							class="px-2 py-1 border border-blue-700 bg-blue-900/20 text-blue-300 text-xs outline-none cursor-pointer"
							on:change={(e) => moveBatchToStage(e.currentTarget.value, e.currentTarget)}
						>
							<option value="" disabled selected>Move stage…</option>
							<option value="researching">Researching</option>
							<option value="developing">Developing</option>
							<option value="backtesting">Backtesting</option>
							<option value="paper_trading">Paper Trading</option>
							<option value="deployed">Deployed</option>
							<option value="rejected">Rejected</option>
						</select>
						<button type="button" class="px-2 py-1 border border-gray-600 text-gray-300 hover:bg-gray-900/20" on:click={() => runBatchAction('archive')}>Archive</button>
						<button type="button" class="px-2 py-1 border border-yellow-700 text-yellow-300 hover:bg-yellow-900/20" on:click={() => runBatchAction('trash')}>Graveyard</button>
						<button type="button" class="px-2 py-1 border border-red-700 text-red-300 hover:bg-red-900/20" on:click={() => runBatchAction('delete')}>Delete</button>
					{:else if bucket === 'parked'}
						<button type="button" class="px-2 py-1 border border-cyan-700 text-cyan-300 hover:bg-cyan-900/20" on:click={() => runBatchAction('recover_parked')}>Recover</button>
						<button type="button" class="px-2 py-1 border border-red-700 text-red-300 hover:bg-red-900/20" on:click={() => runBatchAction('delete')}>Delete</button>
					{:else if bucket === 'trash'}
						<button type="button" class="px-2 py-1 border border-cyan-700 text-cyan-300 hover:bg-cyan-900/20" on:click={() => runBatchAction('recover')}>Recover</button>
						<button type="button" class="px-2 py-1 border border-red-700 text-red-300 hover:bg-red-900/20" on:click={() => runBatchAction('delete')}>Delete permanently</button>
					{/if}
				</div>
			</div>
		{:else if bucket === 'parked'}
			<div class="border-b border-[#222] bg-[#0a0a0a] px-4 py-2 text-[10px] text-gray-500">
				Research-only containers. Select rows to batch-recover them into the active pipeline, or use the per-row Recover button.
			</div>
		{/if}

		<div class="flex-1 overflow-auto bg-black">
				<table class="w-full min-w-[1100px] text-xs">
					{#if bucket === 'active'}
						<thead class="sticky top-0 bg-[#0d0d0d] z-10">
							<tr class="text-gray-500 border-b border-[#222]">
								<th class="py-2 px-2 text-left w-8">
									<input
										type="checkbox"
										class="accent-white w-3 h-3 align-middle"
										aria-label="Select all matching strategies"
										checked={selectAllChecked}
										indeterminate={selectAllIndeterminate}
										on:change={toggleSelectAll}
									/>
								</th>
								<th class="py-2 px-2 text-left">Strategy</th>
								<th class="py-2 px-2 text-left">Pair/TF</th>
								<th class="py-2 px-2 text-left">Stage</th>
								<SortableTh field="cagr" label="CAGR" active={sortBy === 'cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window CAGR (annualized over IS + OOS). Short windows are shown with muted styling." />
								<SortableTh field="sharpe" label="Sharpe ⓘ" active={sortBy === 'sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window Sharpe (approximate: month-weighted average of IS and OOS Sharpe, not recomputed from the combined return stream). ≥1.0 strong, ≥0.5 good, >0 weak, ≤0 poor. Low-trade samples are shown with muted styling." />
								<SortableTh field="drawdown" label="Max DD ⓘ" active={sortBy === 'drawdown'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window max drawdown (approximate: max of IS and OOS max drawdowns; a drawdown that straddles the IS/OOS boundary is understated). ≤20% good, ≤35% marginal, >35% poor." />
								<SortableTh field="win_rate" label="Win%" active={sortBy === 'win_rate'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window win rate = combined wins / combined closed trades. ≥55% good, ≥45% marginal." />
								<SortableTh field="trades" label="Trades" active={sortBy === 'trades'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Total completed trades across IS + OOS." />
								<SortableTh field="profit_factor" label="PF" active={sortBy === 'profit_factor'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window profit factor = combined gross profit / combined gross loss. ≥1.5 good, ≥1.0 marginal. ∞ if no losing trades." />
								<SortableTh field="robustness" label="Rob%" active={sortBy === 'robustness'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Gauntlet robustness score; below 70 fails gate." />
								<SortableTh field="out_of_sample_cagr" label="OOS CAGR" active={sortBy === 'out_of_sample_cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} thClass="border-l border-[#222] pl-3" title="Out-of-sample CAGR (annualized). Short windows are shown with muted styling." />
								<SortableTh field="out_of_sample_sharpe" label="OOS Sharpe" active={sortBy === 'out_of_sample_sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Out-of-sample annualized Sharpe. Low-trade samples are shown with muted styling." />
								<SortableTh field="created" label="Created" active={sortBy === 'created'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
								<th class="py-2 px-2 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#if loading}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">Loading containers...</td></tr>
							{:else if activeFiltered.length === 0}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">No active containers match this view.</td></tr>
							{:else}
								{#each activePageRows as row (row.id)}
									{@const recovery = recoveryBadge(row)}
									<tr
										data-strategy-id={row.id}
										class="border-t border-[#181818] hover:bg-[#0f0f0f]"
										class:strategy-row-highlight={row.id === highlightedId}
									>
										<td class="py-2 px-2">
											<input type="checkbox" class="accent-white w-3 h-3" checked={selectedIds.has(row.id)} on:change={() => toggleSelect(row.id)} />
										</td>
										<td class="py-2 px-2 text-white font-medium max-w-[360px]">
											<StrategyLink
												strategyId={row.id}
												label={row.name}
												returnTo="/lab"
												className="max-w-full truncate bg-transparent border-0 px-0 py-0 text-left text-white hover:text-cyan-300"
											/>
											<div class="text-[10px] text-gray-600 font-mono mt-0.5">{row.id}</div>
											{#if row.hypothesis_id}
												<a href={`/hypotheses/${encodeURIComponent(row.hypothesis_display_id || row.hypothesis_id)}`} class="mt-1 inline-flex items-center gap-1 rounded border border-[#2a2a2a] bg-black/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-gray-400 transition hover:border-cyan-500/40 hover:text-cyan-200">
													Hypothesis {row.hypothesis_display_id || row.hypothesis_id}
												</a>
											{/if}
											{#if row.source === 'ai_dropzone' || !row.has_backtest_results || recovery}
												<div class="mt-1 flex flex-wrap gap-1">
													{#if row.source === 'ai_dropzone'}
														<span class={`text-[9px] px-1.5 py-0.5 border rounded uppercase ${badgeClass('source')}`}>AI Drop Zone</span>
													{/if}
													{#if !row.has_backtest_results}
														<span class={`text-[9px] px-1.5 py-0.5 border rounded uppercase ${badgeClass('untested')}`}>Untested</span>
													{/if}
													{#if recovery}
														<span class={`text-[9px] px-1.5 py-0.5 border rounded uppercase ${recovery.className}`}>{recovery.label}</span>
													{/if}
												</div>
											{/if}
										</td>
										<td class="py-2 px-2 text-gray-300 font-mono">{row.symbol} / {row.timeframe}</td>
										<td class="py-2 px-2">
											<span class={`text-[10px] px-1.5 py-0.5 border rounded uppercase ${stageClass(row.stage)}`}>{row.stage}</span>
										</td>
										<td class={`py-2 px-2 font-mono ${row.cagr_is_reliable ? metricClass('return', row.annualized_return) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Full-window CAGR (annualized over IS + OOS)' : 'Window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.annualized_return, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.sharpe_ratio) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Full-window Sharpe (approximate: month-weighted average of IS and OOS)' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.sharpe_ratio, 2)}{row.sharpe_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('drawdown', row.max_drawdown)}`} title={row.max_drawdown_is_approximation ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{formatPercent(row.max_drawdown, 2)}{row.max_drawdown_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('win_rate', row.win_rate)}`}>{formatPercent(row.win_rate, 1)}</td>
										<td class="py-2 px-2 font-mono text-gray-300">{formatNumber(row.total_trades, 0)}</td>
										<td class={`py-2 px-2 font-mono ${row.profit_factor_is_infinite ? 'text-emerald-400' : metricClass('profit_factor', row.profit_factor)}`} title={row.profit_factor_is_infinite ? 'No losing trades — profit factor is mathematically infinite' : 'Full-window profit factor'}>{row.profit_factor_is_infinite ? '∞' : formatNumber(row.profit_factor, 2)}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('robustness', row.robustness_score)}`}>{formatPercent(row.robustness_score, 1)}</td>
										<td class={`py-2 px-2 font-mono border-l border-[#222] pl-3 ${row.cagr_is_reliable ? metricClass('return', row.out_of_sample_cagr) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Out-of-sample CAGR (annualized)' : 'OOS window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.out_of_sample_cagr, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.out_of_sample_sharpe) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Out-of-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.out_of_sample_sharpe, 2)}</td>
										<td class="py-2 px-2 text-gray-500">{formatDateTime(row.created_at)}</td>
										<td class="py-2 px-2 text-right">
											<div class="inline-flex flex-wrap justify-end items-center gap-2">
												<select
													aria-label="Move strategy to stage"
													class="text-xs bg-transparent text-gray-400 hover:text-white outline-none cursor-pointer border border-[#333] hover:border-[#555] rounded px-1 py-0.5"
													on:change={(e) => moveOneToStage(row.id, e.currentTarget.value, e.currentTarget)}
													title="Move to another stage"
												>
													<option value="" disabled selected>Move to...</option>
													<option value="researching">Researching</option>
													<option value="developing">Developing</option>
													<option value="backtesting">Backtesting</option>
													<option value="paper_trading">Paper Trading</option>
													<option value="deployed">Deployed</option>
													<option value="rejected">Rejected</option>
												</select>
												<StrategyExportMenu strategyId={row.id} displayId={row.id} name={row.name} compact />
												<button type="button" class="text-cyan-300 hover:text-cyan-200" on:click={() => openContainer(row)}>Details</button>
												<button type="button" class="text-yellow-300 hover:text-yellow-200" on:click={() => trashOne(row.id)}>Graveyard</button>
												<button type="button" class="text-red-400 hover:text-red-300" on:click={() => deleteOne(row.id)}>Delete</button>
											</div>
										</td>
									</tr>
								{/each}
							{/if}
						</tbody>
					{:else if bucket === 'parked'}
						<thead class="sticky top-0 bg-[#0d0d0d] z-10">
							<tr class="text-gray-500 border-b border-[#222]">
									<th class="py-2 px-2 text-left w-8"><input type="checkbox" class="accent-white w-3 h-3 align-middle" aria-label="Select all matching strategies" checked={selectAllChecked} indeterminate={selectAllIndeterminate} on:change={toggleSelectAll} /></th>
								<th class="py-2 px-2 text-left">Strategy</th>
								<th class="py-2 px-2 text-left">Pair/TF</th>
								<th class="py-2 px-2 text-left">Stage</th>
								<SortableTh field="cagr" label="CAGR" active={sortBy === 'cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window CAGR (annualized over IS + OOS). Short windows are shown with muted styling." />
								<SortableTh field="sharpe" label="Sharpe ⓘ" active={sortBy === 'sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window Sharpe (approximate: month-weighted average of IS and OOS Sharpe). ≥1.0 strong, ≥0.5 good, >0 weak, ≤0 poor." />
								<SortableTh field="drawdown" label="Max DD ⓘ" active={sortBy === 'drawdown'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window max drawdown (approximate: max of IS and OOS halves). ≤20% good, ≤35% marginal, >35% poor." />
								<SortableTh field="win_rate" label="Win%" active={sortBy === 'win_rate'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window win rate = combined wins / combined closed trades." />
								<SortableTh field="trades" label="Trades" active={sortBy === 'trades'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Total completed trades across IS + OOS." />
								<SortableTh field="profit_factor" label="PF" active={sortBy === 'profit_factor'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window profit factor. ≥1.5 good, ≥1.0 marginal. ∞ if no losing trades." />
								<SortableTh field="robustness" label="Rob%" active={sortBy === 'robustness'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Gauntlet robustness score; below 70 fails gate." />
								<SortableTh field="out_of_sample_cagr" label="OOS CAGR" active={sortBy === 'out_of_sample_cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} thClass="border-l border-[#222] pl-3" title="Out-of-sample CAGR (annualized). Short windows are shown with muted styling." />
								<SortableTh field="out_of_sample_sharpe" label="OOS Sharpe" active={sortBy === 'out_of_sample_sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Out-of-sample annualized Sharpe. Low-trade samples are shown with muted styling." />
								<SortableTh field="created" label="Created" active={sortBy === 'created'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
								<th class="py-2 px-2 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#if loading}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">Loading parked containers...</td></tr>
							{:else if parkedFiltered.length === 0}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">No parked strategies.</td></tr>
							{:else}
								{#each parkedPageRows as row (row.id)}
									<tr class="border-t border-[#181818] hover:bg-[#0f0f0f]">
										<td class="py-2 px-2"><input type="checkbox" class="accent-white w-3 h-3" checked={selectedIds.has(row.id)} on:change={() => toggleSelect(row.id)} /></td>
										<td class="py-2 px-2 text-white font-medium max-w-[420px]">
											<StrategyLink
												strategyId={row.id}
												label={row.name}
												returnTo="/lab"
												className="max-w-full truncate bg-transparent border-0 px-0 py-0 text-left text-white hover:text-cyan-300"
											/>
											<div class="text-[10px] text-gray-600 font-mono mt-0.5">{row.id}</div>
											{#if row.hypothesis_id}
												<a href={`/hypotheses/${encodeURIComponent(row.hypothesis_display_id || row.hypothesis_id)}`} class="mt-1 inline-flex items-center gap-1 rounded border border-[#2a2a2a] bg-black/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-gray-400 transition hover:border-cyan-500/40 hover:text-cyan-200">
													Hypothesis {row.hypothesis_display_id || row.hypothesis_id}
												</a>
											{/if}
										</td>
										<td class="py-2 px-2 text-gray-300 font-mono">{row.symbol} / {row.timeframe}</td>
										<td class="py-2 px-2">
											<span class={`text-[10px] px-1.5 py-0.5 border rounded uppercase ${stageClass(row.stage)}`}>{row.stage}</span>
										</td>
										<td class={`py-2 px-2 font-mono ${row.cagr_is_reliable ? metricClass('return', row.annualized_return) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Full-window CAGR (annualized over IS + OOS)' : 'Window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.annualized_return, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.sharpe_ratio) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Full-window Sharpe (approximate: month-weighted average of IS and OOS)' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.sharpe_ratio, 2)}{row.sharpe_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('drawdown', row.max_drawdown)}`} title={row.max_drawdown_is_approximation ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{formatPercent(row.max_drawdown, 2)}{row.max_drawdown_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('win_rate', row.win_rate)}`}>{formatPercent(row.win_rate, 1)}</td>
										<td class="py-2 px-2 font-mono text-gray-300">{formatNumber(row.total_trades, 0)}</td>
										<td class={`py-2 px-2 font-mono ${row.profit_factor_is_infinite ? 'text-emerald-400' : metricClass('profit_factor', row.profit_factor)}`} title={row.profit_factor_is_infinite ? 'No losing trades — profit factor is mathematically infinite' : 'Full-window profit factor'}>{row.profit_factor_is_infinite ? '∞' : formatNumber(row.profit_factor, 2)}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('robustness', row.robustness_score)}`}>{formatPercent(row.robustness_score, 1)}</td>
										<td class={`py-2 px-2 font-mono border-l border-[#222] pl-3 ${row.cagr_is_reliable ? metricClass('return', row.out_of_sample_cagr) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Out-of-sample CAGR (annualized)' : 'OOS window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.out_of_sample_cagr, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.out_of_sample_sharpe) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Out-of-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.out_of_sample_sharpe, 2)}</td>
										<td class="py-2 px-2 text-gray-500">{formatDateTime(row.created_at)}</td>
										<td class="py-2 px-2 text-right space-x-2 whitespace-nowrap">
											<button type="button" class="text-cyan-300 hover:text-cyan-200" on:click={() => openContainer(row)}>Details</button>
											<button type="button" class="text-cyan-300 hover:text-cyan-200" on:click={() => recoverParked(row.id)} title="Move this research-only container back into the active pipeline">Recover</button>
										</td>
									</tr>
								{/each}
							{/if}
						</tbody>
					{:else}
						<thead class="sticky top-0 bg-[#0d0d0d] z-10">
							<tr class="text-gray-500 border-b border-[#222]">
								<th class="py-2 px-2 text-left w-8">
									<input
										type="checkbox"
										class="accent-white w-3 h-3 align-middle"
										aria-label="Select all matching strategies"
										checked={selectAllChecked}
										indeterminate={selectAllIndeterminate}
										on:change={toggleSelectAll}
									/>
								</th>
								<th class="py-2 px-2 text-left">Strategy</th>
								<th class="py-2 px-2 text-left">Pair/TF</th>
								<th class="py-2 px-2 text-left">Stage</th>
								<SortableTh field="cagr" label="CAGR" active={sortBy === 'cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window CAGR (annualized over IS + OOS). Short windows are shown with muted styling." />
								<SortableTh field="sharpe" label="Sharpe ⓘ" active={sortBy === 'sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window Sharpe (approximate: month-weighted average of IS and OOS Sharpe, not recomputed from the combined return stream). ≥1.0 strong, ≥0.5 good, >0 weak, ≤0 poor. Low-trade samples are shown with muted styling." />
								<SortableTh field="drawdown" label="Max DD ⓘ" active={sortBy === 'drawdown'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window max drawdown (approximate: max of IS and OOS max drawdowns; a drawdown that straddles the IS/OOS boundary is understated). ≤20% good, ≤35% marginal, >35% poor." />
								<SortableTh field="win_rate" label="Win%" active={sortBy === 'win_rate'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window win rate = combined wins / combined closed trades. ≥55% good, ≥45% marginal." />
								<SortableTh field="trades" label="Trades" active={sortBy === 'trades'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Total completed trades across IS + OOS." />
								<SortableTh field="profit_factor" label="PF" active={sortBy === 'profit_factor'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Full-window profit factor = combined gross profit / combined gross loss. ≥1.5 good, ≥1.0 marginal. ∞ if no losing trades." />
								<SortableTh field="robustness" label="Rob%" active={sortBy === 'robustness'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Gauntlet robustness score; below 70 fails gate." />
								<SortableTh field="out_of_sample_cagr" label="OOS CAGR" active={sortBy === 'out_of_sample_cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} thClass="border-l border-[#222] pl-3" title="Out-of-sample CAGR (annualized). Short windows are shown with muted styling." />
								<SortableTh field="out_of_sample_sharpe" label="OOS Sharpe" active={sortBy === 'out_of_sample_sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Out-of-sample annualized Sharpe. Low-trade samples are shown with muted styling." />
								<SortableTh field="created" label="Created" active={sortBy === 'created'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
								<th class="py-2 px-2 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#if loading || graveyardLoading}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">Loading graveyard...</td></tr>
							{:else if trashFiltered.length === 0}
								<tr><td colspan="15" class="py-8 text-center text-gray-600">Graveyard is empty.</td></tr>
							{:else}
								{#each trashPageRows as row (row.id)}
									<tr class="border-t border-[#181818] hover:bg-[#0f0f0f]">
										<td class="py-2 px-2">
											<input type="checkbox" class="accent-white w-3 h-3" checked={selectedIds.has(row.id)} on:change={() => toggleSelect(row.id)} />
										</td>
										<td class="py-2 px-2 text-white font-medium max-w-[420px]">
											<StrategyLink
												strategyId={row.id}
												label={row.name}
												returnTo="/lab"
												className="max-w-full truncate bg-transparent border-0 px-0 py-0 text-left text-white hover:text-cyan-300"
											/>
											<div class="text-[10px] text-gray-600 font-mono mt-0.5">{row.id}</div>
											{#if row.hypothesis_id}
												<a href={`/hypotheses/${encodeURIComponent(row.hypothesis_display_id || row.hypothesis_id)}`} class="mt-1 inline-flex items-center gap-1 rounded border border-[#2a2a2a] bg-black/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-gray-400 transition hover:border-cyan-500/40 hover:text-cyan-200">
													Hypothesis {row.hypothesis_display_id || row.hypothesis_id}
												</a>
											{/if}
											{#if row.source === 'ai_dropzone' || !row.has_backtest_results}
												<div class="mt-1 flex flex-wrap gap-1">
													{#if row.source === 'ai_dropzone'}
														<span class={`text-[9px] px-1.5 py-0.5 border rounded uppercase ${badgeClass('source')}`}>AI Drop Zone</span>
													{/if}
													{#if !row.has_backtest_results}
														<span class={`text-[9px] px-1.5 py-0.5 border rounded uppercase ${badgeClass('untested')}`}>Untested</span>
													{/if}
												</div>
											{/if}
										</td>
										<td class="py-2 px-2 text-gray-300 font-mono">{row.symbol} / {row.timeframe}</td>
										<td class="py-2 px-2">
											<span class={`text-[10px] px-1.5 py-0.5 border rounded uppercase ${stageClass(row.stage)}`}>{row.stage}</span>
										</td>
										<td class={`py-2 px-2 font-mono ${row.cagr_is_reliable ? metricClass('return', row.annualized_return) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Full-window CAGR (annualized over IS + OOS)' : 'Window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.annualized_return, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.sharpe_ratio) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Full-window Sharpe (approximate: month-weighted average of IS and OOS)' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.sharpe_ratio, 2)}{row.sharpe_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('drawdown', row.max_drawdown)}`} title={row.max_drawdown_is_approximation ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{formatPercent(row.max_drawdown, 2)}{row.max_drawdown_is_approximation ? ' ~' : ''}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('win_rate', row.win_rate)}`}>{formatPercent(row.win_rate, 1)}</td>
										<td class="py-2 px-2 font-mono text-gray-300">{formatNumber(row.total_trades, 0)}</td>
										<td class={`py-2 px-2 font-mono ${row.profit_factor_is_infinite ? 'text-emerald-400' : metricClass('profit_factor', row.profit_factor)}`} title={row.profit_factor_is_infinite ? 'No losing trades — profit factor is mathematically infinite' : 'Full-window profit factor'}>{row.profit_factor_is_infinite ? '∞' : formatNumber(row.profit_factor, 2)}</td>
										<td class={`py-2 px-2 font-mono ${metricClass('robustness', row.robustness_score)}`}>{formatPercent(row.robustness_score, 1)}</td>
										<td class={`py-2 px-2 font-mono border-l border-[#222] pl-3 ${row.cagr_is_reliable ? metricClass('return', row.out_of_sample_cagr) : 'text-gray-500 italic'}`} title={row.cagr_is_reliable ? 'Out-of-sample CAGR (annualized)' : 'OOS window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.out_of_sample_cagr, 2)}</td>
										<td class={`py-2 px-2 font-mono ${row.sharpe_is_reliable ? metricClass('sharpe', row.out_of_sample_sharpe) : 'text-gray-500'}`} title={row.sharpe_is_reliable ? 'Out-of-sample annualized Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.out_of_sample_sharpe, 2)}</td>
										<td class="py-2 px-2 text-gray-500">
											<div>{formatDateTime(row.created_at)}</div>
											<div class="text-[10px] text-gray-600">Archived {formatDateTime(row.deleted_at || row.created_at)}</div>
										</td>
										<td class="py-2 px-2 text-right space-x-2 whitespace-nowrap">
											<button type="button" class="text-cyan-300 hover:text-cyan-200" on:click={() => openContainer(row)}>Details</button>
											<button type="button" class="text-cyan-300 hover:text-cyan-200" on:click={() => restoreOne(row.id)}>Recover</button>
											<button type="button" class="text-red-400 hover:text-red-300" on:click={() => deleteOne(row.id)}>Delete</button>
										</td>
									</tr>
								{/each}
							{/if}
						</tbody>
					{/if}
			</table>
		</div>
	</section>
</div>

{#if showImportDialog}
	<StrategyImportDialog
		on:close={() => (showImportDialog = false)}
		on:imported={(e) => onStrategyImported(e.detail)}
	/>
{/if}

<style>
	tr.strategy-row-highlight {
		animation: strategy-highlight-pulse 0.9s ease-in-out 3;
		background-color: rgba(34, 211, 238, 0.12);
		outline: 1px solid rgba(34, 211, 238, 0.6);
		outline-offset: -1px;
	}
	@keyframes strategy-highlight-pulse {
		0%, 100% {
			background-color: rgba(34, 211, 238, 0.08);
		}
		50% {
			background-color: rgba(34, 211, 238, 0.22);
		}
	}
</style>
