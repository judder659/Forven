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
		exportStrategyContainer,
		type NowWorkingRow,
	} from '$lib/api';
	import {
		explainPipeline,
		getLifecycleEvents,
		type LifecycleEvent,
		type PipelineExplainResponse,
		type PipelineExplainStrategy,
	} from '$lib/api/lifecycle';
	import {
		getLiveFleet,
		getPaperSummary,
		getPipelineFunnelReport,
		type LiveFleet,
		type PaperSummary,
		type PipelineFunnelReport,
	} from '$lib/api/dashboard';
	import {
		parseManagerRow,
		isArchivedStage,
		normalizeStage,
		stageClass,
		tradesPerMonth,
		type ManagerRow,
	} from '$lib/utils/strategy';
	import { buildStrategyHref } from '$lib/utils/strategyLinks';
	import { humanizeStrategyType } from '$lib/utils/strategyContainer/format';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { setForgeNavList } from '$lib/stores/forgeNav';
	import { addToast } from '$lib/stores/processTracker';
	import { buildExportFilename, copyJsonToClipboard, downloadJson } from '$lib/utils/strategyPortability';
	import { forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import { PIPELINE_STAGES, graveyardTotal, stageTotals, summarizeFlows, type PipelineStage } from '$lib/utils/forge/flow';
	import { STATUS_META, TONE_DOT, TONE_PILL, stageLabel, statusMeta, type ForgeStatusKey } from '$lib/utils/forge/status';
	import { CAUSES, CAUSE_ORDER, archivedFromStage, classifyArchive, tallyCauses, type Cause, type CauseKey } from '$lib/utils/forge/causes';
	import { forwardIndex, liveTotals, paperTotals } from '$lib/utils/forge/forward';
	import { buildAttention } from '$lib/utils/forge/attention';
	import { buildActivity, hourlyMoves } from '$lib/utils/forge/activity';
	import { forgeHeadline } from '$lib/utils/forge/headline';
	import { ago, daysLabel, parseUtc, shortDate, shortDateTime } from '$lib/utils/forge/time';
	import StrategyImportDialog from '$lib/components/strategy/StrategyImportDialog.svelte';
	import SubmitIdeaDialog from '$lib/components/lab/SubmitIdeaDialog.svelte';
	import PipelineRail from '$lib/components/forge/PipelineRail.svelte';
	import NeedsYouCard from '$lib/components/forge/NeedsYouCard.svelte';
	import ActivityCard from '$lib/components/forge/ActivityCard.svelte';
	import EngineCard from '$lib/components/forge/EngineCard.svelte';
	import StatusCell from '$lib/components/forge/StatusCell.svelte';
	import ForwardCell from '$lib/components/forge/ForwardCell.svelte';
	import RowMenu from '$lib/components/forge/RowMenu.svelte';
	import SortPairTh from '$lib/components/forge/SortPairTh.svelte';
	import StrategyPeek from '$lib/components/forge/StrategyPeek.svelte';
	import { getHealthStatus } from '$lib/api/forven';
	import type { HealthStatusResponse } from '$lib/api/types';
	import type { StrategyImportResult } from '$lib/api';

	type Bucket = 'active' | 'trash';
	type StatusFilter = 'all' | 'needs_you' | 'blocked' | 'waiting' | 'ready' | 'live';
	type FlowWindow = '24h' | '7d';
	type SortField =
		| 'created'
		| 'status'
		| 'in_stage'
		| 'forward'
		| 'cagr'
		| 'in_sample_cagr'
		| 'out_of_sample_cagr'
		| 'return'
		| 'sharpe'
		| 'in_sample_sharpe'
		| 'out_of_sample_sharpe'
		| 'robustness'
		| 'dsr'
		| 'drawdown'
		| 'win_rate'
		| 'trades'
		| 'trades_per_month'
		| 'profit_factor';
	type SortDirection = 'asc' | 'desc';
	type GraveyardStrategyLimitMode = 'capped' | 'unlimited';
	const STRATEGY_FETCH_PAGE_SIZE = 1000;
	const STRATEGY_FETCH_MAX_PAGES = 100;
	const GRAVEYARD_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
	// While the user is actively sitting on the Graveyard tab we refresh more often,
	// but still throttled so a stream of WS events doesn't refetch on every tick.
	const GRAVEYARD_VISIBLE_REFRESH_MS = 15 * 1000;
	const DEFAULT_GRAVEYARD_STRATEGY_LIMIT = 500;
	const FOREGROUND_STRATEGY_STATUSES = ['quick_screen', 'gauntlet', 'paper', 'live_graduated'];
	const GRAVEYARD_STRATEGY_STATUSES = ['archived', 'rejected', 'backtest_failed'];
	// The lifecycle feed runs ~150–400 rows a day; 500 covers the last 24h on all but
	// the busiest days (the pulse chart marks any hours it could not load).
	const ACTIVITY_EVENT_LIMIT = 500;
	const FLOW_WINDOW_KEY = 'forven:forge:flow-window';

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
	let lastRefreshAt: number | null = null;

	// Overview data: every panel loads on its own, and none of them gates the table.
	let explain: PipelineExplainResponse | null = null;
	let explainError: string | null = null;
	let explainLoaded = false;
	let explainRealtime: RealtimeRefreshController | null = null;
	let funnelDay: PipelineFunnelReport | null = null;
	let funnelWeek: PipelineFunnelReport | null = null;
	let events: LifecycleEvent[] = [];
	let eventsError: string | null = null;
	let eventsLoaded = false;
	let overviewRealtime: RealtimeRefreshController | null = null;
	let fleet: LiveFleet | null = null;
	let paperSummary: PaperSummary | null = null;
	let forwardLoaded = false;
	let forwardRealtime: RealtimeRefreshController | null = null;
	let flowWindow: FlowWindow = '24h';
	let now = Date.now();
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	// Manager table state
	let bucket: Bucket = 'active';
	let search = '';
	let symbolFilter = 'all';
	let stageFilter = 'all';
	let statusFilter: StatusFilter = 'all';
	let causeFilter: CauseKey | 'all' = 'all';
	let sortBy: SortField = 'created';
	let sortDirection: SortDirection = 'desc';

	let activeResults: ManagerRow[] = [];
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
	let trashPageRows: ManagerRow[] = [];
	let lastViewSignature = '';
	let toolbarHeight = 0;

	let peekId: string | null = null;

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
	}

	function applyGraveyardRows(parsedRows: ManagerRow[]) {
		const archivedRows = parsedRows
			.filter((row) => isArchivedStage(row.stage))
			.map((row) => ({ ...row, deleted_at: row.deleted_at || row.created_at }));
		const trashById = new Map<string, ManagerRow>();
		for (const row of archivedRows) {
			if (!trashById.has(row.id)) trashById.set(row.id, row);
		}
		trashResults = Array.from(trashById.values());
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

	function formatNumber(value: number | null | undefined, decimals = 2): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '—';
		const text = Math.abs(value).toFixed(decimals);
		return value < 0 && Number(text) !== 0 ? `−${text}` : text;
	}

	function formatPercent(value: number | null | undefined, decimals = 1): string {
		const text = formatNumber(value, decimals);
		return text === '—' ? text : `${text}%`;
	}

	/** Palette tones for backtest metrics; same bands the table has always used. */
	function metricTone(kind: 'return' | 'sharpe' | 'robustness' | 'drawdown' | 'win_rate' | 'profit_factor' | 'dsr', value: number | null): string {
		if (value === null || !Number.isFinite(value)) return 'text-sc-ink3';
		const ok = 'text-[#3cc48f]';
		const warn = 'text-[#e7b24a]';
		const bad = 'text-[#f2956f]';
		switch (kind) {
			case 'return':
				return value >= 0 ? ok : bad;
			case 'sharpe':
				if (value >= 1.0) return ok;
				if (value >= 0.5) return 'text-sc-ink';
				if (value > 0) return warn;
				return bad;
			case 'robustness':
				if (value >= 70) return ok;
				if (value >= 50) return warn;
				return bad;
			case 'drawdown':
				if (value <= 20) return 'text-sc-ink';
				if (value <= 35) return warn;
				return bad;
			case 'win_rate':
				if (value >= 55) return ok;
				if (value >= 45) return 'text-sc-ink';
				return warn;
			case 'profit_factor':
				if (value >= 1.5) return ok;
				if (value >= 1.0) return 'text-sc-ink';
				return bad;
			case 'dsr':
				// ~0.95 is the conventional significance bar for the Deflated Sharpe probability.
				if (value >= 0.95) return ok;
				if (value >= 0.8) return warn;
				return bad;
		}
	}

	function toggleSort(field: SortField) {
		if (sortBy === field) {
			sortDirection = sortDirection === 'desc' ? 'asc' : 'desc';
			return;
		}
		sortBy = field;
		// Higher-is-better metrics (and 'created') default to descending so the first
		// click surfaces the best/newest first. Drawdown is a positive magnitude where
		// lower is better, and status ranks "needs you" lowest, so both start ascending.
		sortDirection = field === 'drawdown' || field === 'status' ? 'asc' : 'desc';
	}

	function compareNumeric(a: number, b: number, direction: SortDirection): number {
		if (a < b) return direction === 'asc' ? -1 : 1;
		if (a > b) return direction === 'asc' ? 1 : -1;
		return 0;
	}

	function activeSortValue(row: ManagerRow, field: SortField): number {
		switch (field) {
			case 'created': return Date.parse(row.created_at) || 0;
			case 'status': return explainById.get(row.id) ? statusMeta(explainById.get(row.id)?.status).rank : Number.POSITIVE_INFINITY;
			case 'in_stage': return daysInStage(row) ?? Number.NEGATIVE_INFINITY;
			case 'forward': return forwardById.get(row.id)?.pnlUsd ?? Number.NEGATIVE_INFINITY;
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
			// Never-computed DSR sinks below genuinely low probabilities on a
			// descending sort (same treatment as missing Sharpe above).
			case 'dsr': return row.deflated_sharpe ?? Number.NEGATIVE_INFINITY;
			// Drawdown is a positive magnitude where lower is better, so unmeasured rows
			// must sort to the worst (highest) end rather than masquerade as a perfect 0%.
			case 'drawdown': return row.max_drawdown ?? Number.POSITIVE_INFINITY;
			case 'win_rate': return row.win_rate ?? Number.NEGATIVE_INFINITY;
			case 'trades': return row.total_trades ?? Number.NEGATIVE_INFINITY;
			case 'trades_per_month': return tradesPerMonth(row) ?? Number.NEGATIVE_INFINITY;
			// Infinite profit factor (no losing trades) is the strongest possible PF and
			// must sort to the top; a missing PF sinks to the bottom on a descending sort.
			case 'profit_factor': return row.profit_factor_is_infinite ? Number.POSITIVE_INFINITY : (row.profit_factor ?? Number.NEGATIVE_INFINITY);
			default: return 0;
		}
	}

	function archivedAt(row: ManagerRow): string {
		return row.stage_changed_at || row.deleted_at || row.created_at;
	}

	/** Days in the current stage: the explainer's figure, else the row's own stamp. */
	function daysInStage(row: ManagerRow): number | null {
		const fromExplain = explainById.get(row.id)?.days_in_stage;
		if (typeof fromExplain === 'number' && Number.isFinite(fromExplain)) return fromExplain;
		const since = parseUtc(row.stage_changed_at);
		return since === null ? null : Math.max(0, (Date.now() - since) / 86_400_000);
	}

	function graveyardSortValue(row: ManagerRow, field: SortField): number {
		switch (field) {
			case 'created':
			case 'status':
			case 'in_stage':
			case 'forward':
				// The graveyard's time column is when a strategy was archived.
				return parseUtc(archivedAt(row)) ?? 0;
			default: return activeSortValue(row, field);
		}
	}

	function goPrevPage() { currentPage = Math.max(1, currentPage - 1); }
	function goNextPage() { currentPage = Math.min(pageCount, currentPage + 1); }

	function openContainer(row: Pick<ManagerRow, 'id'>) {
		goto(`/lab/strategy/${encodeURIComponent(row.id)}?returnTo=${encodeURIComponent('/lab')}`);
	}

	let showImportDialog = false;
	let showIdeaDialog = false;

	function onStrategyImported(result: StrategyImportResult) {
		showImportDialog = false;
		if (result.ok && result.strategy_id) {
			openContainer({ id: result.strategy_id });
		}
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
			lastRefreshAt = Date.now();

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
			if (activeResults.length === 0) {
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

	// The explainer dry-runs every gate for every active strategy (~5–15s on a busy
	// backend), so it refreshes on pipeline events with a long debounce, never on a
	// short poll, and it never blocks the table.
	let explainInFlight: Promise<void> | null = null;

	function loadExplain(): Promise<void> {
		// One run at a time: Refresh clicks, mutations and pipeline events share it.
		if (explainInFlight) return explainInFlight;
		explainInFlight = (async () => {
			try {
				explain = await explainPipeline({ limit: 1000 });
				explainError = null;
			} catch (e) {
				explainError = e instanceof Error ? e.message : 'The gate explainer is unavailable';
			} finally {
				explainLoaded = true;
				explainInFlight = null;
			}
		})();
		return explainInFlight;
	}

	async function loadOverview() {
		const [day, week, feed] = await Promise.allSettled([
			getPipelineFunnelReport(1),
			getPipelineFunnelReport(7),
			getLifecycleEvents(ACTIVITY_EVENT_LIMIT),
		]);
		if (day.status === 'fulfilled') funnelDay = day.value;
		if (week.status === 'fulfilled') funnelWeek = week.value;
		if (feed.status === 'fulfilled') {
			events = feed.value;
			eventsError = null;
		} else if (events.length === 0) {
			eventsError = 'Pipeline activity is unavailable';
		}
		eventsLoaded = true;
	}

	async function loadForward() {
		const [paper, live] = await Promise.allSettled([getPaperSummary(), getLiveFleet()]);
		if (paper.status === 'fulfilled') paperSummary = paper.value;
		if (live.status === 'fulfilled') fleet = live.value;
		forwardLoaded = true;
	}

	async function refreshAll() {
		// The explainer is the slowest call, so it starts first and nothing waits on it.
		void loadExplain();
		await Promise.all([
			loadData({ forceGraveyard: true }),
			loadNowWorking(),
			loadHealth(),
			loadOverview(),
			loadForward(),
		]);
	}

	// Run per-row writes one at a time (not Promise.all). Each transition opens a
	// write transaction and the promotion gate holds the WAL writer lock; firing
	// them concurrently serializes/stalls server-side and loses per-row results.
	// Sequential execution keeps writes orderly and reports partial failures.
	async function runSequential(
		ids: string[],
		op: (id: string) => Promise<unknown>
	): Promise<{ succeeded: number; failed: number; errors: string[] }> {
		let succeeded = 0;
		const errors: string[] = [];
		for (const id of ids) {
			try {
				await op(id);
				succeeded += 1;
			} catch (e) {
				// Capture the per-id reason instead of swallowing it — a blocked
				// transition (invalid stage move, ghost-container protection, …) must
				// be visible, not silently counted as "N failed".
				errors.push(`${id}: ${e instanceof Error ? e.message : 'failed'}`);
			}
		}
		return { succeeded, failed: errors.length, errors };
	}

	function summarizeReasons(reasons: string[], max = 3): string {
		if (reasons.length === 0) return '';
		const head = reasons.slice(0, max).join(' · ');
		return reasons.length > max ? `${head} (+${reasons.length - max} more)` : head;
	}

	async function afterMutation() {
		await loadData({ forceGraveyard: true });
		void loadOverview();
		void loadExplain();
	}

	async function runBatchAction(action: 'trash' | 'archive' | 'recover' | 'delete') {
		const ids = rowsInView.map((row) => row.id).filter((id) => selectedIds.has(id));
		if (ids.length === 0) return;

		error = null;
		actionMsg = null;

		try {
			if (action === 'trash') {
				if (!confirm(`Move ${ids.length} containers to graveyard?`)) return;
				const { succeeded, failed, errors } = await runSequential(ids, (id) => transitionStage(id, 'graveyard', 'User moved to graveyard from Lab Manager', 'manual'));
				actionMsg = `Moved ${succeeded} container${succeeded === 1 ? '' : 's'} to graveyard.`;
				if (failed > 0) {
					actionMsg += ` ${failed} failed.`;
					error = summarizeReasons(errors);
				}
			} else if (action === 'archive') {
				if (!confirm(`Archive ${ids.length} strategies? They can be recovered later.`)) return;
				const result = await batchTransitionStrategies(ids, 'archived', 'Batch archived from Lab Manager');
				const count = result.transitioned.length;
				actionMsg = `Archived ${count} strateg${count === 1 ? 'y' : 'ies'}.`;
				if (result.failed.length > 0) {
					actionMsg += ` ${result.failed.length} failed.`;
					error = summarizeReasons(
						result.failed.map(
							(f) => `${f.id}: ${f.error}${f.approval_id ? ` (approval #${f.approval_id})` : ''}`
						)
					);
				}
			} else if (action === 'recover') {
				const { succeeded, failed, errors } = await runSequential(ids, (id) => reviveFromGraveyard(id));
				actionMsg = `Recovered ${succeeded} container${succeeded === 1 ? '' : 's'} from graveyard.`;
				if (failed > 0) {
					actionMsg += ` ${failed} failed.`;
					error = summarizeReasons(errors);
				}
			} else if (action === 'delete') {
				if (!confirm(`Permanently delete ${ids.length} strategies? This cannot be undone.`)) return;
				const result = await batchDeleteStrategies(ids);
				const count = result.deleted.length;
				actionMsg = `Permanently deleted ${count} strateg${count === 1 ? 'y' : 'ies'}.`;
				if (result.not_found.length > 0) actionMsg += ` ${result.not_found.length} already gone.`;
			}
			await afterMutation();
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
			await afterMutation();
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
			if (peekId === id) peekId = null;
			await afterMutation();
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to delete strategy';
		}
	}

	async function restoreOne(id: string) {
		try {
			await reviveFromGraveyard(id);
			actionMsg = 'Container recovered from graveyard.';
			await afterMutation();
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to recover container';
		}
	}

	async function moveOneToStage(id: string, stage: string) {
		if (!stage) return;
		try {
			await transitionStage(id, stage, `User manually moved to ${stage} from Lab Manager`, 'manual');
			actionMsg = `Container moved to ${stageLabel(normalizeStage(stage)).toLowerCase()}.`;
			await afterMutation();
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to move container to ${stage}`;
		}
	}

	async function exportOne(row: Pick<ManagerRow, 'id' | 'name'>, mode: 'download' | 'clipboard') {
		try {
			const envelope = await exportStrategyContainer(row.id);
			if (mode === 'download') {
				downloadJson(envelope, buildExportFilename(row.id, row.name));
				addToast(`Exported ${row.id} to file`, 'success');
			} else {
				await copyJsonToClipboard(envelope);
				addToast(`Copied ${row.id} export to clipboard`, 'success');
			}
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Export failed', 'error');
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
			const { succeeded, failed, errors } = await runSequential(ids, (id) => transitionStage(id, stage, `User batch moved to ${stage} from Lab Manager`, 'manual'));
			actionMsg = `Moved ${succeeded} container${succeeded === 1 ? '' : 's'} to ${stage}.`;
			if (failed > 0) {
				actionMsg += ` ${failed} failed.`;
				error = summarizeReasons(errors);
			}
			await afterMutation();
			clearSelection();
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to batch move containers to ${stage}`;
		}
	}

	function titleOf(row: ManagerRow): string {
		return row.display_name || humanizeStrategyType(row.type) || row.name;
	}


	function openPeek(id: string) {
		if (rowById.has(id)) {
			peekId = id;
			return;
		}
		// Not in the loaded rows (e.g. an older graveyard entry): go straight to its page.
		openContainer({ id });
	}

	function onRowClick(event: MouseEvent, id: string) {
		const target = event.target as HTMLElement | null;
		if (target?.closest('a, button, input, select, label, summary, [role="menu"]')) return;
		peekId = peekId === id ? null : id;
	}

	function onRowKey(event: KeyboardEvent, id: string) {
		if (event.key === 'Enter' && event.target === event.currentTarget) {
			event.preventDefault();
			peekId = id;
		}
	}

	function selectStage(stage: string) {
		if (stage === 'graveyard') {
			bucket = 'trash';
			stageFilter = 'all';
		} else {
			bucket = 'active';
			stageFilter = stage;
		}
		clearSelection();
	}

	let tableSection: HTMLElement;

	async function showNeedsYou() {
		bucket = 'active';
		stageFilter = 'all';
		statusFilter = 'needs_you';
		clearSelection();
		await tick();
		tableSection?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	function setBucket(next: Bucket) {
		if (bucket === next) return;
		bucket = next;
		clearSelection();
		if (next === 'trash') void loadData();
	}

	function setFlowWindow(next: FlowWindow) {
		flowWindow = next;
		try {
			localStorage.setItem(FLOW_WINDOW_KEY, next);
		} catch {
			// Storage unavailable — the choice just won't persist.
		}
	}

	// ── Derived overview ──────────────────────────────────────────────────────────
	$: rowById = new Map([...trashResults, ...activeResults].map((row) => [row.id, row] as const));
	$: activeIds = new Set(activeResults.map((row) => row.id));
	$: explainById = new Map((explain?.strategies ?? []).map((entry) => [entry.id, entry] as const));
	$: explainLoading = !explainLoaded;
	// Reactive so every panel that names strategies re-renders once rows or the explainer land.
	$: nameOf = (id: string): string => {
		const row = rowById.get(id);
		if (row) return titleOf(row);
		const entry = explainById.get(id);
		if (entry) return humanizeStrategyType(entry.type) || entry.name || id;
		return id;
	};
	$: stageCounts = PIPELINE_STAGES.reduce(
		(acc, stage) => ({ ...acc, [stage]: activeResults.filter((row) => normalizeStage(row.stage) === stage).length }),
		{} as Record<PipelineStage, number>,
	);
	$: statusByStage = (() => {
		const out = Object.fromEntries(PIPELINE_STAGES.map((s) => [s, {}])) as Record<PipelineStage, Partial<Record<ForgeStatusKey, number>>>;
		for (const row of activeResults) {
			const entry = explainById.get(row.id);
			if (!entry) continue;
			const stage = normalizeStage(row.stage) as PipelineStage;
			if (!out[stage]) continue;
			const key = statusMeta(entry.status).key;
			out[stage][key] = (out[stage][key] ?? 0) + 1;
		}
		return out;
	})();
	$: activeFunnel = flowWindow === '24h' ? funnelDay : funnelWeek;
	$: flow = activeFunnel ? summarizeFlows(activeFunnel.flows) : null;
	$: totals = stageTotals((funnelWeek ?? funnelDay)?.stage_counts);
	$: graveyardCount = funnelWeek || funnelDay ? graveyardTotal(totals) : null;
	$: forwardById = forwardIndex(paperSummary, fleet);
	$: paperBook = paperTotals(paperSummary);
	$: liveBook = liveTotals(fleet);
	$: attention = buildAttention({
		activeIds,
		explain: explain?.strategies ?? [],
		fleet,
		nowWorking: nowWorkingRows,
		rows: activeResults,
		health: healthData,
		nameOf,
	});
	$: attentionIds = new Set(attention.map((item) => item.strategyId).filter((id): id is string => Boolean(id)));
	$: activity = buildActivity(events, { nameOf, limit: 40 });
	$: pulse = hourlyMoves(events, now);
	$: causeById = new Map(trashResults.map((row) => [row.id, classifyArchive(row.notes, row.status_reason)] as const));
	$: causeTally = tallyCauses([...causeById.values()]);
	$: causeCounts = new Map(causeTally.map((c) => [c.key, c.count] as const));
	$: windowStart = now - (flowWindow === '24h' ? 1 : 7) * 86_400_000;
	// The graveyard loads its newest rows only; say so when they don't reach back a full window.
	$: windowCovered = graveyardLoadAllOverride || !graveyardCapped || (parseUtc(oldestTrashAt) ?? Number.POSITIVE_INFINITY) <= windowStart;
	$: windowCauses = tallyCauses(
		trashResults
			.filter((row) => (parseUtc(archivedAt(row)) ?? 0) >= windowStart)
			.map((row) => causeById.get(row.id))
			.filter((cause): cause is Cause => Boolean(cause)),
	);
	$: topCause = windowCauses.find((c) => c.key !== 'other') ?? null;
	$: topCauseScope = windowCovered ? `last ${flowWindow}` : `newest ${trashResults.length.toLocaleString('en-US')} loaded`;
	$: headline = forgeHeadline({
		live: stageCounts.live_graduated ?? 0,
		paper: stageCounts.paper ?? 0,
		gauntlet: stageCounts.gauntlet ?? 0,
		quickScreen: stageCounts.quick_screen ?? 0,
		flow,
		windowLabel: flowWindow === '24h' ? '24 hours' : '7 days',
		attention: explainLoaded ? attention.length : null,
	});
	$: statusCounts = (() => {
		void explainById;
		void attentionIds;
		const counts: Record<StatusFilter, number> = { all: activeResults.length, needs_you: 0, blocked: 0, waiting: 0, ready: 0, live: 0 };
		for (const row of activeResults) {
			if (matchesStatus(row, 'needs_you')) counts.needs_you += 1;
			if (matchesStatus(row, 'blocked')) counts.blocked += 1;
			if (matchesStatus(row, 'waiting')) counts.waiting += 1;
			if (matchesStatus(row, 'ready')) counts.ready += 1;
			if (matchesStatus(row, 'live')) counts.live += 1;
		}
		return counts;
	})();
	$: oldestTrashAt = trashResults.reduce<string | null>((oldest, row) => {
		const at = archivedAt(row);
		return !oldest || (at && (parseUtc(at) ?? 0) < (parseUtc(oldest) ?? 0)) ? at : oldest;
	}, null);
	$: lastEventAt = events[0]?.created_at ?? null;
	$: peekRow = peekId ? rowById.get(peekId) ?? null : null;
	$: peekBucket = (peekRow && isArchivedStage(peekRow.stage) ? 'trash' : 'active') as Bucket;

	function matchesStatus(row: ManagerRow, filter: StatusFilter): boolean {
		if (filter === 'all') return true;
		if (filter === 'needs_you') return attentionIds.has(row.id);
		const status = explainById.get(row.id)?.status;
		switch (filter) {
			case 'blocked': return status === 'blocked_merit' || status === 'slot_contention';
			case 'waiting': return status === 'waiting_evidence' || status === 'in_flight';
			case 'ready': return status === 'ready';
			case 'live': return status === 'live';
		}
		return true;
	}

	function matchesSearch(row: ManagerRow, query: string): boolean {
		if (!query) return true;
		return (
			row.name.toLowerCase().includes(query)
			|| (row.display_name ?? '').toLowerCase().includes(query)
			|| titleOf(row).toLowerCase().includes(query)
			|| row.symbol.toLowerCase().includes(query)
			|| row.timeframe.toLowerCase().includes(query)
			|| row.id.toLowerCase().includes(query)
		);
	}

	$: activeFiltered = (() => {
		// Only the visible bucket is filtered/sorted; the others return [] so a single
		// keystroke doesn't re-filter+re-sort thousands of off-screen graveyard rows.
		if (bucket !== 'active') return [];
		const query = search.trim().toLowerCase();
		const sortField = sortBy;
		const direction = sortDirection;
		// Referenced so the sort re-runs when explainer / forward data lands.
		void explainById;
		void forwardById;
		void attentionIds;
		const filtered = activeResults.filter((row) => {
			if (symbolFilter !== 'all' && row.symbol !== symbolFilter) return false;
			if (stageFilter !== 'all' && normalizeStage(row.stage) !== stageFilter) return false;
			if (!matchesStatus(row, statusFilter)) return false;
			return matchesSearch(row, query);
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
			if (causeFilter !== 'all' && causeById.get(row.id)?.key !== causeFilter) return false;
			return matchesSearch(row, query);
		});
		const ranked = filtered.map((row, index) => ({ row, index, sortValue: graveyardSortValue(row, sortField) }));
		ranked.sort((a, b) => {
			const cmp = compareNumeric(a.sortValue, b.sortValue, direction);
			if (cmp !== 0) return cmp;
			return a.index - b.index;
		});
		return ranked.map((entry) => entry.row);
	})();

	$: rowsInView = bucket === 'active' ? activeFiltered : trashFiltered;
	// Keep the detail page's prev/next context in sync with whatever this view
	// currently shows (full filtered+sorted order, all pages) — captured on every
	// view change, not on click, so EVERY route into a container (name link,
	// Details button, middle-click) walks the same order. Empty views don't
	// clobber a previously captured list.
	$: if (rowsInView.length > 0) {
		setForgeNavList(rowsInView.map((r) => ({ id: r.id, label: r.display_name || r.name })));
	}
	$: pageCount = Math.max(1, Math.ceil(rowsInView.length / pageSize));
	$: if (currentPage > pageCount) currentPage = pageCount;
	$: {
		const signature = [
			bucket,
			search.trim().toLowerCase(),
			symbolFilter,
			stageFilter,
			statusFilter,
			causeFilter,
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
	$: graveyardCapped = !graveyardLoadAllOverride && graveyardStrategyLimitMode === 'capped' && trashResults.length >= graveyardStrategyLimit;

	$: symbolOptions = ['all', ...new Set(activeResults.map((row) => row.symbol).sort())];
	$: stageOptions = ['all', ...PIPELINE_STAGES];
	$: selectedRail = bucket === 'trash' ? 'graveyard' : stageFilter;

	const STATUS_CHIPS: Array<{ key: StatusFilter; label: string }> = [
		{ key: 'all', label: 'All' },
		{ key: 'needs_you', label: 'Needs you' },
		{ key: 'blocked', label: 'Blocked' },
		{ key: 'waiting', label: 'Gathering evidence' },
		{ key: 'ready', label: 'Ready' },
		{ key: 'live', label: 'Live' },
	];

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
		try {
			const stored = localStorage.getItem(FLOW_WINDOW_KEY);
			if (stored === '24h' || stored === '7d') flowWindow = stored;
		} catch {
			// Storage unavailable — keep the default window.
		}
		clockTimer = setInterval(() => {
			now = Date.now();
		}, 30_000);
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
		// Paper days and trades accrue without any event, so the explainer also re-runs
		// on a slow clock even while the websocket is up.
		explainRealtime = createRealtimeRefresh(loadExplain, {
			fallbackMs: 120_000,
			wsDebounceMs: 30_000,
			wsEvents: ['strategy_transition', 'strategy_promoted', 'approval_created', 'approval_resolved'],
			pollWhenWsOfflineOnly: false,
		});
		explainRealtime.start();
		// The 24h / 7d windows roll forward with time, not only with events.
		overviewRealtime = createRealtimeRefresh(loadOverview, {
			fallbackMs: 60_000,
			wsDebounceMs: 5_000,
			wsEvents: ['strategy_transition', 'strategy_promoted', 'approval_created', 'approval_resolved'],
			pollWhenWsOfflineOnly: false,
		});
		overviewRealtime.start();
		forwardRealtime = createRealtimeRefresh(loadForward, {
			fallbackMs: 60_000,
			wsDebounceMs: 5_000,
			wsEvents: ['trade', 'strategy_transition', 'strategy_promoted'],
			pollWhenWsOfflineOnly: false,
		});
		forwardRealtime.start();
	});

	onDestroy(() => {
		if (typeof document !== 'undefined') {
			document.body.style.overflow = '';
		}
		for (const controller of [realtime, nowWorkingRealtime, healthRealtime, explainRealtime, overviewRealtime, forwardRealtime]) {
			controller?.stop();
		}
		realtime = null;
		nowWorkingRealtime = null;
		healthRealtime = null;
		explainRealtime = null;
		overviewRealtime = null;
		forwardRealtime = null;
		if (clockTimer) {
			clearInterval(clockTimer);
			clockTimer = null;
		}
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
	<meta name="description" content="Where every strategy is, why it is there, and what moved — the strategy factory at a glance." />
</svelte:head>

<div class="min-h-full bg-sc-bg">
	<header class="border-b border-sc-line bg-sc-panel">
		<div class="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 px-5 pb-4 pt-4">
			<div class="min-w-0 max-w-[920px] flex-1">
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1">
					<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">The Forge</h1>
					<span
						class="inline-flex items-center gap-1.5 rounded-full border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2"
						title={$forvenWsConnected ? 'Streaming pipeline events; panels refresh as strategies move.' : 'Live stream offline; panels poll instead.'}
					>
						<span class={`h-1.5 w-1.5 rounded-full ${$forvenWsConnected ? 'animate-pulse bg-[#3cc48f]' : 'bg-[#e7b24a]'}`} aria-hidden="true"></span>
						{$forvenWsConnected ? 'Live' : 'Polling'}{#if lastRefreshAt} · updated {ago(lastRefreshAt, now)}{/if}
					</span>
				</div>
				<p class="m-0 mt-1.5 text-[13px] leading-relaxed text-sc-ink2" data-testid="forge-headline">
					{#if loading && activeResults.length === 0}
						Reading the pipeline…
					{:else}
						{#each headline as sentence, i (i)}
							{#if i === headline.length - 1 && explainLoaded && attention.length > 0}
								<button
									type="button"
									class="rounded bg-[#e7b24a]/10 px-1.5 py-px font-medium text-[#e7b24a] transition-colors hover:bg-[#e7b24a]/20"
									title="Show them in the table"
									on:click={showNeedsYou}
								>{sentence}</button>
							{:else}
								<span class={i === 0 ? 'text-sc-ink' : ''}>{sentence}</span>{' '}
							{/if}
						{/each}
					{/if}
				</p>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				<a
					href="/lab/backtests"
					class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
				>
					All backtests
				</a>
				<button
					type="button"
					data-testid="forge-import-strategy"
					on:click={() => (showImportDialog = true)}
					class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
				>
					Import
				</button>
				<button
					type="button"
					on:click={refreshAll}
					class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
				>
					Refresh
				</button>
				<button
					type="button"
					data-testid="forge-submit-idea"
					on:click={() => (showIdeaDialog = true)}
					class="rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black transition-colors hover:bg-white"
				>
					+ Submit idea
				</button>
			</div>
		</div>
	</header>

	<div class="grid gap-3 px-5 pb-5 pt-4">
		<div class="flex flex-wrap items-end justify-between gap-2">
			<div>
				<h2 class="m-0 text-[14px] font-semibold text-sc-ink">Pipeline</h2>
				<p class="m-0 text-[11px] text-sc-ink3">Counts are right now; flow is the last {flowWindow === '24h' ? '24 hours' : '7 days'}. Click a stage to filter the table below.</p>
			</div>
			<div role="group" aria-label="Flow window" class="flex rounded-md border border-sc-line bg-sc-panel p-0.5">
				{#each ['24h', '7d'] as windowKey (windowKey)}
					<button
						type="button"
						aria-pressed={flowWindow === windowKey}
						class="rounded px-2.5 py-0.5 text-[12px] transition-colors {flowWindow === windowKey ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}"
						on:click={() => setFlowWindow(windowKey === '24h' ? '24h' : '7d')}
					>{windowKey}</button>
				{/each}
			</div>
		</div>

		<PipelineRail
			counts={stageCounts}
			{statusByStage}
			{flow}
			windowLabel={flowWindow}
			paper={paperBook}
			live={liveBook}
			graveyardTotal={graveyardCount}
			{topCause}
			{topCauseScope}
			selected={selectedRail}
			statusReady={explainLoaded}
			on:stage={(e) => selectStage(e.detail)}
		/>

		<div class="grid gap-3 lg:h-[292px] lg:grid-cols-3">
			<NeedsYouCard items={attention} loading={explainLoading} error={explainError} on:peek={(e) => openPeek(e.detail)} />
			<ActivityCard items={activity} loading={!eventsLoaded} error={eventsError} {now} on:peek={(e) => openPeek(e.detail)} />
			<EngineCard
				rows={nowWorkingRows}
				loaded={nowWorkingLoaded}
				error={nowWorkingError}
				health={healthData}
				{healthLoaded}
				{healthError}
				{lastEventAt}
				moves={pulse.buckets}
				coveredFrom={pulse.coveredFrom}
				{now}
				on:peek={(e) => openPeek(e.detail)}
				on:retry={(e) => (e.detail === 'health' ? loadHealth() : loadNowWorking())}
			/>
		</div>
	</div>

	<section class="scroll-mt-2 px-5 pb-10" aria-label="Strategies" bind:this={tableSection}>
		<div
			class="sticky top-0 z-20 -mx-5 grid gap-2 border-b border-sc-line bg-sc-bg/95 px-5 pb-2 pt-2.5 backdrop-blur"
			data-testid="forge-manager-toolbar"
			bind:offsetHeight={toolbarHeight}
		>
			<div class="flex flex-wrap items-center gap-2">
				<div role="group" aria-label="Strategy buckets" class="flex items-center gap-1">
					<button
						type="button"
						aria-pressed={bucket === 'active'}
						on:click={() => setBucket('active')}
						class="rounded-md px-3 py-1 text-[13px] font-semibold transition-colors {bucket === 'active' ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}"
					>
						In the pipeline <span class="ml-0.5 font-plex-mono text-[12px] font-normal text-sc-ink3">{activeResults.length}</span>
					</button>
					<button
						type="button"
						aria-pressed={bucket === 'trash'}
						on:click={() => setBucket('trash')}
						class="rounded-md px-3 py-1 text-[13px] font-semibold transition-colors {bucket === 'trash' ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}"
					>
						Graveyard <span class="ml-0.5 font-plex-mono text-[12px] font-normal text-sc-ink3">{(graveyardCount ?? trashResults.length).toLocaleString('en-US')}</span>
						{#if graveyardLoading}<span class="ml-1 text-[11px] font-normal text-sc-ink3">loading</span>{/if}
					</button>
				</div>
				<div class="ml-auto flex flex-wrap items-center gap-2">
					<input
						type="text"
						bind:value={search}
						placeholder={bucket === 'active' ? 'Search name, symbol, timeframe, id…' : 'Search graveyard…'}
						class="w-full rounded-md border border-sc-line2 bg-sc-panel px-3 py-1.5 text-[12px] text-sc-ink placeholder:text-sc-ink4 focus:border-sc-ink focus:outline-none sm:w-64"
					/>
					{#if bucket === 'active'}
						<select aria-label="Filter by symbol" bind:value={symbolFilter} class="terminal-input !w-auto !py-1 !px-2 text-[12px]">
							{#each symbolOptions as symbol}
								<option value={symbol}>{symbol === 'all' ? 'All symbols' : symbol}</option>
							{/each}
						</select>
						<select aria-label="Filter by stage" bind:value={stageFilter} class="terminal-input !w-auto !py-1 !px-2 text-[12px]">
							{#each stageOptions as stage}
								<option value={stage}>{stage === 'all' ? 'All stages' : stageLabel(stage)}</option>
							{/each}
						</select>
					{/if}
				</div>
			</div>

			<div class="flex flex-wrap items-center gap-1.5">
				{#if bucket === 'active'}
					{#each STATUS_CHIPS as chip (chip.key)}
						{@const count = statusCounts[chip.key]}
						{@const pending = chip.key !== 'all' && chip.key !== 'needs_you' && !explainLoaded}
						<button
							type="button"
							aria-pressed={statusFilter === chip.key}
							disabled={pending}
							on:click={() => (statusFilter = statusFilter === chip.key && chip.key !== 'all' ? 'all' : chip.key)}
							class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] transition-colors disabled:cursor-wait disabled:opacity-50 {statusFilter === chip.key ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink'}"
						>
							{chip.label}
							{#if !pending && (chip.key === 'all' || explainLoaded)}
								<span class="font-plex-mono text-[11px] {statusFilter === chip.key ? 'text-black/70' : chip.key === 'needs_you' && count > 0 ? 'text-[#e7b24a]' : 'text-sc-ink3'}">{count}</span>
							{/if}
						</button>
					{/each}
				{:else}
					<button
						type="button"
						aria-pressed={causeFilter === 'all'}
						on:click={() => (causeFilter = 'all')}
						class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] transition-colors {causeFilter === 'all' ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink'}"
					>All <span class="font-plex-mono text-[11px] {causeFilter === 'all' ? 'text-black/70' : 'text-sc-ink3'}">{trashResults.length}</span></button>
					{#each CAUSE_ORDER.filter((key) => (causeCounts.get(key) ?? 0) > 0) as key (key)}
						<button
							type="button"
							aria-pressed={causeFilter === key}
							title={CAUSES[key].help}
							on:click={() => (causeFilter = causeFilter === key ? 'all' : key)}
							class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] transition-colors {causeFilter === key ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink'}"
						>
							{#if !CAUSES[key].merit}<span class="h-1.5 w-1.5 rounded-full bg-[#e7b24a]" aria-hidden="true"></span>{/if}
							{CAUSES[key].label}
							<span class="font-plex-mono text-[11px] {causeFilter === key ? 'text-black/70' : 'text-sc-ink3'}">{causeCounts.get(key)}</span>
						</button>
					{/each}
				{/if}
				<div class="ml-auto flex items-center gap-2 text-[11px] text-sc-ink3">
					<span>
						{rowsInView.length.toLocaleString('en-US')} shown
						{#if bucket === 'trash' && trashResults.length > 0}
							· newest {trashResults.length.toLocaleString('en-US')} loaded{#if oldestTrashAt} (since {shortDateTime(oldestTrashAt)}){/if}
						{/if}
					</span>
					{#if bucket === 'trash' && graveyardCapped}
						<button type="button" class="text-[#e7b24a] underline underline-offset-2 hover:text-[#f1c46a] disabled:opacity-50" on:click={loadAllGraveyard} disabled={graveyardLoading}>
							{graveyardLoading ? 'Loading…' : 'Load all'}
						</button>
					{/if}
					<label for="manager-page-size" class="ml-2">Rows</label>
					<select
						id="manager-page-size"
						class="terminal-input !w-auto !py-0.5 !px-1.5 text-[11px]"
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
					<button type="button" class="rounded border border-sc-line2 px-1.5 py-0.5 text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:cursor-not-allowed disabled:opacity-40" on:click={goPrevPage} disabled={currentPage <= 1} aria-label="Previous page">‹</button>
					<span class="font-plex-mono">{currentPage}/{pageCount}</span>
					<button type="button" class="rounded border border-sc-line2 px-1.5 py-0.5 text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:cursor-not-allowed disabled:opacity-40" on:click={goNextPage} disabled={currentPage >= pageCount} aria-label="Next page">›</button>
				</div>
			</div>

			{#if selectedInView > 0}
				<div class="flex flex-wrap items-center gap-3 rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px]">
					<span class="font-medium text-sc-ink">{selectedInView} selected</span>
					<button type="button" class="text-sc-ink2 transition-colors hover:text-sc-ink" on:click={selectFiltered}>Select all {rowsInView.length} matching</button>
					<button type="button" class="text-sc-ink2 transition-colors hover:text-sc-ink" on:click={clearSelection}>Clear</button>
					<div class="ml-auto flex items-center gap-2">
						{#if bucket === 'active'}
							<select
								aria-label="Move selected strategies to stage"
								class="cursor-pointer rounded-md border border-sc-line2 bg-sc-panel2 px-2 py-1 text-[12px] text-sc-ink2 outline-none"
								on:change={(e) => moveBatchToStage(e.currentTarget.value, e.currentTarget)}
							>
								<option value="" disabled selected>Move to…</option>
								<option value="researching">Quick screen</option>
								<option value="backtesting">Gauntlet</option>
								<option value="paper_trading">Paper</option>
								<option value="deployed">Live</option>
								<option value="rejected">Rejected</option>
							</select>
							<button type="button" class="rounded-md border border-sc-line2 px-2 py-1 text-sc-ink2 hover:bg-sc-raise" on:click={() => runBatchAction('archive')}>Archive</button>
							<button type="button" class="rounded-md border border-[#e7b24a]/40 px-2 py-1 text-[#e7b24a] hover:bg-[#e7b24a]/10" on:click={() => runBatchAction('trash')}>Graveyard</button>
							<button type="button" class="rounded-md border border-[#e5574f]/45 px-2 py-1 text-[#f2956f] hover:bg-[#e5574f]/10" on:click={() => runBatchAction('delete')}>Delete</button>
						{:else}
							<button type="button" class="rounded-md border border-[#3cc48f]/40 px-2 py-1 text-[#3cc48f] hover:bg-[#3cc48f]/10" on:click={() => runBatchAction('recover')}>Recover</button>
							<button type="button" class="rounded-md border border-[#e5574f]/45 px-2 py-1 text-[#f2956f] hover:bg-[#e5574f]/10" on:click={() => runBatchAction('delete')}>Delete permanently</button>
						{/if}
					</div>
				</div>
			{/if}
		</div>

		{#if actionMsg}
			<div role="status" aria-live="polite" class="mt-3 flex items-start gap-2 rounded-md border border-[#3cc48f]/40 bg-[#3cc48f]/5 px-3 py-2 text-[12px] text-[#3cc48f]">
				<span class="flex-1">{actionMsg}</span>
				<button type="button" class="shrink-0 leading-none text-[#3cc48f]/70 hover:text-sc-ink" aria-label="Dismiss message" on:click={() => (actionMsg = null)}>×</button>
			</div>
		{/if}
		{#if error}
			<div role="alert" aria-live="assertive" class="mt-3 flex items-start gap-2 rounded-md border border-[#e5574f]/45 bg-[#e5574f]/5 px-3 py-2 text-[12px] text-[#f2956f]">
				<span class="flex-1">{error}</span>
				<button type="button" class="shrink-0 leading-none text-[#f2956f]/70 hover:text-sc-ink" aria-label="Dismiss error" on:click={() => (error = null)}>×</button>
			</div>
		{/if}

		<div class="mt-2 overflow-x-clip">
			<table class="w-full table-fixed border-separate border-spacing-0 text-[12px]">
				{#if bucket === 'active'}
					<colgroup>
						<col class="w-9" />
						<col />
						<col class="w-[92px]" />
						<col class="w-[26%] 2xl:w-[24%]" />
						<col class="w-[116px]" />
						<col class="w-[74px]" />
						<col class="w-[80px]" />
						<col class="w-[72px]" />
						<col class="w-[64px]" />
						<col class="hidden w-[56px] xl:table-column" />
						<col class="hidden w-[64px] 2xl:table-column" />
						<col class="w-[44px]" />
					</colgroup>
					<thead class="sticky z-10" style={`top:${toolbarHeight}px`}>
						<tr class="bg-sc-panel2 text-left text-sc-ink3">
							<th class="rounded-tl-md border-y border-l border-sc-line px-2 py-1.5">
								<input
									type="checkbox"
									class="h-3 w-3 accent-white align-middle"
									aria-label="Select all matching strategies"
									checked={selectAllChecked}
									indeterminate={selectAllIndeterminate}
									on:change={toggleSelectAll}
								/>
							</th>
							<th class="border-y border-sc-line px-2 py-1.5 text-[12px] font-normal">Strategy</th>
							<SortPairTh thClass="border-y border-sc-line" align="left" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'in_stage', label: 'Stage', title: 'Sort by days in the current stage' }}
								secondary={{ field: 'created', label: 'Created', title: 'Sort by when the strategy was created' }} />
							<th class="border-y border-sc-line px-2 py-1.5 font-normal" aria-sort={sortBy === 'status' ? (sortDirection === 'desc' ? 'descending' : 'ascending') : 'none'}>
								<button type="button" class="text-[12px] hover:text-sc-ink {sortBy === 'status' ? 'text-sc-ink' : ''}" title="Sort by what needs attention first" on:click={() => toggleSort('status')}>Status &amp; why{#if sortBy === 'status'}<span class="ml-0.5 text-[9px]">{sortDirection === 'desc' ? '▼' : '▲'}</span>{/if}</button>
							</th>
							<th class="border-y border-sc-line px-2 py-1.5 font-normal" aria-sort={sortBy === 'forward' ? (sortDirection === 'desc' ? 'descending' : 'ascending') : 'none'}>
								<button type="button" class="text-[12px] hover:text-sc-ink {sortBy === 'forward' ? 'text-sc-ink' : ''}" title="Realized forward P&L — paper book or live wallet (never mixed)" on:click={() => toggleSort('forward')}>Forward{#if sortBy === 'forward'}<span class="ml-0.5 text-[9px]">{sortDirection === 'desc' ? '▼' : '▲'}</span>{/if}</button>
							</th>
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'sharpe', label: 'Sharpe', title: 'Full-window Sharpe (approximate: month-weighted average of IS and OOS). ≥1.0 strong, ≥0.5 good, >0 weak. "~" marks the approximation; muted = fewer than 20 trades.' }}
								secondary={{ field: 'out_of_sample_sharpe', label: 'OOS', title: 'Out-of-sample annualized Sharpe.' }} />
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'cagr', label: 'CAGR', title: 'Full-window CAGR (annualized over IS + OOS). Italic = window under a month.' }}
								secondary={{ field: 'out_of_sample_cagr', label: 'OOS', title: 'Out-of-sample CAGR (annualized).' }} />
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'drawdown', label: 'Max DD', title: 'Full-window max drawdown (approximate: max of IS and OOS). ≤20% fine, ≤35% marginal.' }}
								secondary={{ field: 'win_rate', label: 'Win', title: 'Combined win rate across IS + OOS.' }} />
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'trades', label: 'Trades', title: 'Completed backtest trades across IS + OOS. Muted = fewer than 20.' }}
								secondary={{ field: 'trades_per_month', label: '/mo', title: 'Backtest trades per month: how active the strategy is, comparable across test windows.' }} />
							<th class="hidden border-y border-sc-line px-2 py-1.5 text-right align-bottom font-normal xl:table-cell" aria-sort={sortBy === 'profit_factor' ? (sortDirection === 'desc' ? 'descending' : 'ascending') : 'none'}>
								<button type="button" class="text-[12px] hover:text-sc-ink {sortBy === 'profit_factor' ? 'text-sc-ink' : ''}" title="Full-window profit factor. ≥1.5 good, ≥1.0 marginal. ∞ = no losing trades." on:click={() => toggleSort('profit_factor')}>PF{#if sortBy === 'profit_factor'}<span class="ml-0.5 text-[9px]">{sortDirection === 'desc' ? '▼' : '▲'}</span>{/if}</button>
							</th>
							<SortPairTh thClass="hidden border-y border-sc-line 2xl:table-cell" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'dsr', label: 'DSR', title: 'Deflated Sharpe (0–1): chance the edge survives selection bias. ≥0.95 significant. Last computed value.' }}
								secondary={{ field: 'robustness', label: 'Rob', title: 'Gauntlet robustness score (0–100).' }} />
							<th class="rounded-tr-md border-y border-r border-sc-line px-2 py-1.5"><span class="sr-only">Actions</span></th>
						</tr>
					</thead>
					<tbody>
						{#if !loading}
							{#each activePageRows as row (row.id)}
								{@const entry = explainById.get(row.id) ?? null}
								{@const perMonth = tradesPerMonth(row)}
								{@const fwd = forwardById.get(row.id) ?? null}
								{@const stage = normalizeStage(row.stage)}
								<tr
									data-strategy-id={row.id}
									tabindex="0"
									class="group cursor-pointer outline-none transition-colors hover:bg-sc-panel focus-visible:bg-sc-panel {peekId === row.id ? 'bg-sc-panel2' : ''}"
									class:strategy-row-highlight={row.id === highlightedId}
									on:click={(e) => onRowClick(e, row.id)}
									on:keydown={(e) => onRowKey(e, row.id)}
								>
									<td class="border-b border-l border-sc-line px-2 py-2.5 align-top">
										<input type="checkbox" class="h-3 w-3 accent-white" aria-label={`Select ${titleOf(row)}`} checked={selectedIds.has(row.id)} on:change={() => toggleSelect(row.id)} />
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<div class="min-w-0">
											<a
												href={buildStrategyHref(row.id, { returnTo: '/lab' })}
												class="block truncate text-[13px] font-medium text-sc-ink hover:underline"
												title={`${row.name} — open the strategy page`}
											>{titleOf(row)}</a>
											<div class="mt-0.5 truncate font-plex-mono text-[10.5px] text-sc-ink3">{row.id} · {row.symbol} · {row.timeframe}</div>
											{#if row.hypothesis_id || row.source === 'ai_dropzone' || !row.has_backtest_results || row.recovery_status}
												<div class="mt-1 flex flex-wrap gap-1">
													{#if row.hypothesis_id}
														<span class="rounded border border-sc-line2 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-sc-ink2" title="The idea this strategy tests">Idea {row.hypothesis_display_id || row.hypothesis_id}</span>
													{/if}
													{#if row.source === 'ai_dropzone'}
														<span class="rounded border border-sc-line2 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-sc-ink2">AI Drop Zone</span>
													{/if}
													{#if !row.has_backtest_results}
														<span class="rounded border border-[#e7b24a]/40 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-[#e7b24a]">Untested</span>
													{/if}
													{#if ['repair_pending', 'repair_running'].includes((row.recovery_status || '').toLowerCase())}
														<span class="rounded border border-[#e7b24a]/40 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-[#e7b24a]">Repairing</span>
													{:else if ['replay_running', 'final_retry_running'].includes((row.recovery_status || '').toLowerCase())}
														<span class="rounded border border-[#e7b24a]/40 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-[#e7b24a]">Healing</span>
													{:else if (row.recovery_status || '').toLowerCase() === 'exhausted'}
														<span class="rounded border border-[#e5574f]/45 px-1.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] text-[#f2956f]">Failed recovery</span>
													{/if}
												</div>
											{/if}
										</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<span class={`inline-block rounded border px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] ${stageClass(row.stage)}`}>{stageLabel(stage)}</span>
										<div class="mt-1 font-plex-mono text-[10.5px] text-sc-ink3" title="Days in this stage">{daysLabel(entry?.days_in_stage ?? daysInStage(row))} in stage</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink4" title={`Created ${shortDateTime(row.created_at)}`}>{shortDate(row.created_at)}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<StatusCell {entry} loading={!explainLoaded} forward={fwd} {stage} />
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<ForwardCell record={fwd} {stage} loading={!forwardLoaded} />
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${row.sharpe_is_reliable ? metricTone('sharpe', row.sharpe_ratio) : 'text-sc-ink3'}`} title={row.sharpe_is_reliable ? 'Full-window Sharpe' : 'Low trade count (<20) — Sharpe may be noisy'}>{formatNumber(row.sharpe_ratio, 2)}{row.sharpe_is_approximation ? '~' : ''}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3" title="Out-of-sample Sharpe">{formatNumber(row.out_of_sample_sharpe, 2)}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${row.cagr_is_reliable ? metricTone('return', row.annualized_return) : 'italic text-sc-ink3'}`} title={row.cagr_is_reliable ? 'Full-window CAGR' : 'Window too short (<1 month) — annualized value may be unreliable'}>{formatPercent(row.annualized_return, 1)}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3" title="Out-of-sample CAGR">{formatPercent(row.out_of_sample_cagr, 1)}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${metricTone('drawdown', row.max_drawdown)}`} title={row.max_drawdown_is_approximation ? 'Full-window max DD (approximate: max of IS and OOS halves)' : 'Maximum peak-to-trough drawdown'}>{formatPercent(row.max_drawdown, 1)}{row.max_drawdown_is_approximation ? '~' : ''}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3" title="Win rate">{formatPercent(row.win_rate, 0)}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${row.total_trades !== null && row.total_trades < 20 ? 'text-sc-ink3' : 'text-sc-ink2'}`} title="Completed backtest trades (IS + OOS)">{formatNumber(row.total_trades, 0)}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3" title="Backtest trades per month">{perMonth === null ? '—' : formatNumber(perMonth, perMonth < 10 ? 1 : 0)}</div>
									</td>
									<td class="hidden border-b border-sc-line px-2 py-2 text-right align-top xl:table-cell">
										<div class={`font-plex-mono text-[12px] ${row.profit_factor_is_infinite ? 'text-[#3cc48f]' : metricTone('profit_factor', row.profit_factor)}`}>{row.profit_factor_is_infinite ? '∞' : formatNumber(row.profit_factor, 2)}</div>
									</td>
									<td class="hidden border-b border-sc-line px-2 py-2 text-right align-top 2xl:table-cell">
										<div class={`font-plex-mono text-[12px] ${metricTone('dsr', row.deflated_sharpe)}`} title="Deflated Sharpe (last computed value)">{formatNumber(row.deflated_sharpe, 2)}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3" title="Robustness score">{row.robustness_score === null ? '—' : Math.round(row.robustness_score)}</div>
									</td>
									<td class="border-b border-r border-sc-line px-1 py-1.5 text-right align-top">
										<div class="flex flex-col items-end gap-1">
											<RowMenu
												bucket="active"
												stage={stage}
												label={`Actions for ${titleOf(row)}`}
												on:open={() => openContainer(row)}
												on:move={(e) => moveOneToStage(row.id, e.detail)}
												on:graveyard={() => trashOne(row.id)}
												on:delete={() => deleteOne(row.id)}
												on:export={(e) => exportOne(row, e.detail)}
											/>
										</div>
									</td>
								</tr>
							{/each}
						{/if}
					</tbody>
				{:else}
					<colgroup>
						<col class="w-9" />
						<col />
						<col class="w-[96px]" />
						<col class="w-[34%]" />
						<col class="w-[104px]" />
						<col class="w-[74px]" />
						<col class="w-[80px]" />
						<col class="hidden w-[72px] xl:table-column" />
						<col class="w-[44px]" />
					</colgroup>
					<thead class="sticky z-10" style={`top:${toolbarHeight}px`}>
						<tr class="bg-sc-panel2 text-left text-sc-ink3">
							<th class="rounded-tl-md border-y border-l border-sc-line px-2 py-1.5">
								<input
									type="checkbox"
									class="h-3 w-3 accent-white align-middle"
									aria-label="Select all matching strategies"
									checked={selectAllChecked}
									indeterminate={selectAllIndeterminate}
									on:change={toggleSelectAll}
								/>
							</th>
							<th class="border-y border-sc-line px-2 py-1.5 text-[12px] font-normal">Strategy</th>
							<th class="border-y border-sc-line px-2 py-1.5 text-[12px] font-normal" title="The stage it was archived from">Left at</th>
							<th class="border-y border-sc-line px-2 py-1.5 text-[12px] font-normal">Why it was archived</th>
							<th class="border-y border-sc-line px-2 py-1.5 font-normal" aria-sort={sortBy === 'created' ? (sortDirection === 'desc' ? 'descending' : 'ascending') : 'none'}>
								<button type="button" class="text-[12px] hover:text-sc-ink {sortBy === 'created' ? 'text-sc-ink' : ''}" on:click={() => toggleSort('created')}>When{#if sortBy === 'created'}<span class="ml-0.5 text-[9px]">{sortDirection === 'desc' ? '▼' : '▲'}</span>{/if}</button>
							</th>
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'sharpe', label: 'Sharpe' }} secondary={{ field: 'out_of_sample_sharpe', label: 'OOS' }} />
							<SortPairTh thClass="border-y border-sc-line" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'cagr', label: 'CAGR' }} secondary={{ field: 'out_of_sample_cagr', label: 'OOS' }} />
							<SortPairTh thClass="hidden border-y border-sc-line xl:table-cell" sortBy={sortBy} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)}
								primary={{ field: 'profit_factor', label: 'PF' }} secondary={{ field: 'trades', label: 'Trades' }} />
							<th class="rounded-tr-md border-y border-r border-sc-line px-2 py-1.5"><span class="sr-only">Actions</span></th>
						</tr>
					</thead>
					<tbody>
						{#if !(loading || graveyardLoading)}
							{#each trashPageRows as row (row.id)}
								{@const cause = causeById.get(row.id) ?? null}
								{@const leftAt = archivedFromStage(row.notes)}
								<tr
									data-strategy-id={row.id}
									tabindex="0"
									class="group cursor-pointer outline-none transition-colors hover:bg-sc-panel focus-visible:bg-sc-panel {peekId === row.id ? 'bg-sc-panel2' : ''}"
									class:strategy-row-highlight={row.id === highlightedId}
									on:click={(e) => onRowClick(e, row.id)}
									on:keydown={(e) => onRowKey(e, row.id)}
								>
									<td class="border-b border-l border-sc-line px-2 py-2.5 align-top">
										<input type="checkbox" class="h-3 w-3 accent-white" aria-label={`Select ${titleOf(row)}`} checked={selectedIds.has(row.id)} on:change={() => toggleSelect(row.id)} />
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<a href={buildStrategyHref(row.id, { returnTo: '/lab' })} class="block truncate text-[13px] font-medium text-sc-ink2 hover:text-sc-ink hover:underline" title={`${row.name} — open the strategy page`}>{titleOf(row)}</a>
										<div class="mt-0.5 truncate font-plex-mono text-[10.5px] text-sc-ink3">{row.id} · {row.symbol} · {row.timeframe}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										{#if leftAt}
											<span class="text-[12px] text-sc-ink2">{stageLabel(leftAt)}</span>
										{:else}
											<span class={`inline-block rounded border px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] ${stageClass(row.stage)}`}>{stageLabel(normalizeStage(row.stage))}</span>
										{/if}
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										{#if cause}
											<div class="flex min-w-0 items-center gap-1.5">
												<span class={`shrink-0 rounded-full border px-2 py-px text-[11px] ${CAUSES[cause.key].merit ? TONE_PILL.fail : TONE_PILL.caution}`} title={CAUSES[cause.key].help}>{cause.label}</span>
												{#if cause.detail}<span class="truncate text-[11px] text-sc-ink2">{cause.detail}</span>{/if}
											</div>
											{#if cause.clause}
												<div class="mt-0.5 truncate font-plex-mono text-[10.5px] text-sc-ink3" title={cause.clause}>{cause.clause}</div>
											{/if}
										{/if}
									</td>
									<td class="border-b border-sc-line px-2 py-2 align-top">
										<div class="text-[12px] text-sc-ink2">{ago(archivedAt(row), now)}</div>
										<div class="text-[10.5px] text-sc-ink3">{shortDateTime(archivedAt(row))}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${row.sharpe_is_reliable ? metricTone('sharpe', row.sharpe_ratio) : 'text-sc-ink3'}`}>{formatNumber(row.sharpe_ratio, 2)}{row.sharpe_is_approximation ? '~' : ''}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3">{formatNumber(row.out_of_sample_sharpe, 2)}</div>
									</td>
									<td class="border-b border-sc-line px-2 py-2 text-right align-top">
										<div class={`font-plex-mono text-[12px] ${row.cagr_is_reliable ? metricTone('return', row.annualized_return) : 'italic text-sc-ink3'}`}>{formatPercent(row.annualized_return, 1)}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3">{formatPercent(row.out_of_sample_cagr, 1)}</div>
									</td>
									<td class="hidden border-b border-sc-line px-2 py-2 text-right align-top xl:table-cell">
										<div class={`font-plex-mono text-[12px] ${row.profit_factor_is_infinite ? 'text-[#3cc48f]' : metricTone('profit_factor', row.profit_factor)}`}>{row.profit_factor_is_infinite ? '∞' : formatNumber(row.profit_factor, 2)}</div>
										<div class="font-plex-mono text-[10.5px] text-sc-ink3">{formatNumber(row.total_trades, 0)}</div>
									</td>
									<td class="border-b border-r border-sc-line px-1 py-1.5 text-right align-top">
										<RowMenu
											bucket="trash"
											stage={normalizeStage(row.stage)}
											label={`Actions for ${titleOf(row)}`}
											on:open={() => openContainer(row)}
											on:recover={() => restoreOne(row.id)}
											on:delete={() => deleteOne(row.id)}
											on:export={(e) => exportOne(row, e.detail)}
										/>
									</td>
								</tr>
							{/each}
						{/if}
					</tbody>
				{/if}
			</table>
			{#if bucket === 'active' && (loading || activeFiltered.length === 0)}
				<div class="rounded-b-md border-x border-b border-sc-line py-10 text-center text-[12px] text-sc-ink3">
					{#if loading}
						Loading strategies…
					{:else}
						{activeResults.length === 0 ? 'No strategies in the pipeline right now.' : 'No strategies match this view.'}
						{#if statusFilter !== 'all' || stageFilter !== 'all' || symbolFilter !== 'all' || search}
							<button type="button" class="ml-1 underline underline-offset-2 hover:text-sc-ink" on:click={() => { statusFilter = 'all'; stageFilter = 'all'; symbolFilter = 'all'; search = ''; }}>Clear filters</button>
						{/if}
					{/if}
				</div>
			{:else if bucket === 'trash' && (loading || graveyardLoading || trashFiltered.length === 0)}
				<div class="rounded-b-md border-x border-b border-sc-line py-10 text-center text-[12px] text-sc-ink3">
					{#if loading || graveyardLoading}
						Loading graveyard…
					{:else}
						{trashResults.length === 0 ? 'Graveyard is empty.' : 'Nothing in the graveyard matches this view.'}
						{#if causeFilter !== 'all' || search}
							<button type="button" class="ml-1 underline underline-offset-2 hover:text-sc-ink" on:click={() => { causeFilter = 'all'; search = ''; }}>Clear filters</button>
						{/if}
					{/if}
				</div>
			{/if}
		</div>
	</section>
</div>

{#if peekRow}
	<StrategyPeek
		row={peekRow}
		title={titleOf(peekRow)}
		bucket={peekBucket}
		entry={explainById.get(peekRow.id) ?? null}
		explainLoading={!explainLoaded}
		forward={forwardById.get(peekRow.id) ?? null}
		cause={peekBucket === 'trash' ? (causeById.get(peekRow.id) ?? null) : null}
		archivedFrom={peekBucket === 'trash' ? archivedFromStage(peekRow.notes) : null}
		{now}
		on:close={() => (peekId = null)}
		on:open={() => peekRow && openContainer(peekRow)}
		on:move={(e) => peekRow && moveOneToStage(peekRow.id, e.detail)}
		on:graveyard={() => peekRow && trashOne(peekRow.id)}
		on:delete={() => peekRow && deleteOne(peekRow.id)}
		on:recover={() => peekRow && restoreOne(peekRow.id)}
		on:export={(e) => peekRow && exportOne(peekRow, e.detail)}
	/>
{/if}

{#if showImportDialog}
	<StrategyImportDialog
		on:close={() => (showImportDialog = false)}
		on:imported={(e) => onStrategyImported(e.detail)}
	/>
{/if}

<SubmitIdeaDialog open={showIdeaDialog} on:close={() => (showIdeaDialog = false)} />

<style>
	tr.strategy-row-highlight > td {
		animation: strategy-highlight-pulse 0.9s ease-in-out 3;
		background-color: rgba(60, 196, 143, 0.1);
	}
	@keyframes strategy-highlight-pulse {
		0%, 100% {
			background-color: rgba(60, 196, 143, 0.06);
		}
		50% {
			background-color: rgba(60, 196, 143, 0.2);
		}
	}
</style>
