<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import {
		getStrategies,
		getDatasets,
		getPaperSession,
		getPaperSessions,
		startPaperService,
		stopPaperService,
		getReplayBars,
		getSessionIndicators,
		getTradeMarkers,
		getPaperSessionChart,
		getPaperTrades,
		closePaperPosition,
		partialClosePaperPosition,
		openManualPaperPosition,
		adjustPaperStopLoss,
		adjustPaperTakeProfit,
		flipPaperPosition,
		setPaperAutoManagement,
		listLifecycleStrategies,
		getLifecycleStrategy
	} from '$lib/api';
	import type {
		Strategy,
		Dataset,
		PaperTradingSession,
		PaperTrade,
		PendingSignal,
		OHLCVBar,
		SessionIndicatorConfig,
		SessionIndicatorsResponse,
		TradeMarker,
		TradeMarkersResponse,
		OpenManualPaperPositionOptions,
		LifecycleStrategy,
		LifecycleEvent,
		ForvenDashboardResponse
	} from '$lib/api';
	import type { IndicatorConfig, SignalMarker } from '$lib/stores/chartStore';
	import type { ChartDrawing, ChartDrawingPoint, ChartDrawingTool } from '$lib/components/chart/types';
	import ChartWorkspace from '$lib/components/chart/ChartWorkspace.svelte';
	import Skeleton from '$lib/components/Skeleton.svelte';
	import DataTable from '$lib/components/DataTable.svelte';
	import { ORDERED_TIMEFRAME_VALUES } from '$lib/config/timeframes';
	import { workspaceContext, selectedDataset as selectedDatasetStore } from '$lib/stores';
	import { forvenLivePrices } from '$lib/stores/forvenWebSocket';
	import { applyTickToBars } from '$lib/utils/liveBars';
	import { setPageContext } from '$lib/stores/pageContext';
	import { createPoller, type Poller } from '$lib/utils/polling';

	type SessionView = 'paper' | 'live' | 'all';
	export let view: SessionView = 'paper';
	export let dashboard: ForvenDashboardResponse | null = null;

	// Trading gates — runtime checks that can block new trades
	$: gates = (() => {
		const d = dashboard;
		return {
			system_paused: d?.paused ?? false,
			kill_switch: d?.risk?.kill_switch_active ?? false,
			daily_loss_halt: d?.risk?.daily_loss_halt ?? false,
			recovery_active: d?.recovery?.active ?? false,
			hl_price: d?.circuit_breakers?.hl_price ?? 'closed',
			hl_trade: d?.circuit_breakers?.hl_trade ?? 'closed',
			hl_account: d?.circuit_breakers?.hl_account ?? 'closed',
		};
	})();
	$: anyGateBlocking = gates.system_paused || gates.kill_switch || gates.daily_loss_halt || gates.recovery_active
		|| gates.hl_price !== 'closed' || gates.hl_trade !== 'closed' || gates.hl_account !== 'closed';

	const supportsStandalonePaperSessions = false;
	const supportsReplayControls = false;

	$: isLiveView = view === 'live';
	$: allowsSessionEditing = !isLiveView && supportsStandalonePaperSessions;
	$: sessionListTitle = isLiveView ? 'Live Strategies' : 'Sessions';
	$: emptySessionTitle = isLiveView ? 'No live strategies' : 'No paper trading sessions';
	$: emptySessionHint = isLiveView
		? 'No deployed strategies are available'
		: 'Move a strategy into paper stage to start monitoring it';
	$: emptyStateTitle = isLiveView ? 'Live Trading' : 'Paper Trading';
	$: panelContextLabel = isLiveView ? 'Live Trading' : 'Paper Trading';
	// Publish what's selected here to the global assistant so "this session" works.
	$: setPageContext({
		summary: selectedSession
			? `${panelContextLabel}: ${selectedSession.strategy_name} on ${selectedSession.symbol}`
			: selectedArchivedStrategy
				? `${panelContextLabel}: archived ${selectedArchivedStrategy.display_id || selectedArchivedStrategy.name || selectedArchivedStrategy.id}`
				: panelContextLabel,
	});

	let strategies: Strategy[] = [];
	let datasets: Dataset[] = [];
	let sessions: PaperTradingSession[] = [];
	let archivedStrategies: LifecycleStrategy[] = [];
	let loading = true;
	let archivedLoading = false;
	let error: string | null = null;
	let highActivityTestEnabled = false;
	let highActivityToggleBusy = false;

	// ── Manual position controls ────────────────────────────────────────────
	// requestInFlight locks every control button (and gates the 10s poller) while
	// a mutation is outstanding, so rapid clicks and stale polls can't clobber it.
	let requestInFlight = false;
	let slInput = '';
	let tpInput = '';
	let partialPctInput = '';
	let openDirection: 'long' | 'short' = 'long';
	let openSizeMode: 'size' | 'risk' = 'risk';
	let openSizeInput = '';
	let openRiskPctInput = '1';
	let openLeverageInput = '1';
	let openSlInput = '';
	let openTpInput = '';
	let pendingConfirm: { label: string; detail: string; run: () => Promise<PaperTradingSession> } | null = null;
	const MANUAL_INPUT_CLASS =
		'bg-[#0a0a0a] border border-[#333] text-white px-1 py-0.5 text-[10px] focus:outline-none focus:border-gray-500';
	const MANUAL_BTN_CLASS =
		'border border-[#333] text-gray-300 hover:text-white hover:border-gray-500 px-1.5 py-0.5 text-[10px] uppercase disabled:opacity-40 disabled:cursor-not-allowed';
	const MANUAL_BTN_ACCENT_CLASS =
		'border border-emerald-700 text-emerald-400 hover:text-emerald-200 hover:border-emerald-500 px-2 py-0.5 text-[10px] uppercase font-bold disabled:opacity-40 disabled:cursor-not-allowed';
	const MANUAL_SEG_CLASS = 'px-2 py-0.5 text-[10px] uppercase text-gray-400';
	let archivedDetailLoading = false;
	let selectedArchivedStrategy: LifecycleStrategy | null = null;
	let selectedArchivedEvents: LifecycleEvent[] = [];
	let selectedArchivedReason: ArchivedReasonSnapshot = {
		event: null,
		reason: 'No archived/demotion reason was recorded.',
		fromState: null,
		toState: null,
		timestamp: null,
		actor: null,
	};
	let archivedTimelineEvents: LifecycleEvent[] = [];

	// New session form
	let showNewSession = false;
	let newSessionStrategy = '';
	let newSessionSymbol = 'BTC/USDT';
	let newSessionTimeframe = '1h';
	let newSessionCapital = 10000;
	let newSessionMode: 'live' | 'replay' = 'live';
	let newSessionLiveFeed: 'default' | 'ibkr' = 'default';
	let newSessionIBKRSecType = 'STK';
	let newSessionIBKRExchange = 'SMART';
	let newSessionIBKRCurrency = 'USD';
	let newSessionIBKRWhatToShow: 'TRADES' | 'MIDPOINT' | 'BID' | 'ASK' = 'TRADES';
	let newSessionReplayStart = '';
	let newSessionReplayEnd = '';
	let newSessionReplaySpeed = 1;
	let creating = false;

	// Edit session form
	let showEditSession = false;
	let editSessionId = '';
	let editSessionStrategy = '';
	let editSessionSymbol = '';
	let editSessionTimeframe = '1h';
	let editSessionCapital = 10000;
	let editSessionPositionSize = 100;
	let editSessionStopLoss: number | null = null;
	let editSessionTakeProfit: number | null = null;
	let editSessionTrailingStop: number | null = null;
	let editSessionMode: 'live' | 'replay' = 'live';
	let editSessionLiveFeed: 'default' | 'ibkr' = 'default';
	let editSessionIBKRSecType = 'STK';
	let editSessionIBKRExchange = 'SMART';
	let editSessionIBKRCurrency = 'USD';
	let editSessionIBKRWhatToShow: 'TRADES' | 'MIDPOINT' | 'BID' | 'ASK' = 'TRADES';
	let editSessionParams: Record<string, unknown> = {};
	let editSessionReplayStart = '';
	let editSessionReplayEnd = '';
	let editSessionReplaySpeed = 1;
	let editSessionFeeMode: 'taker' | 'maker' | 'auto' = 'taker';
	let editSessionTakerFeeBps = 4.5;
	let editSessionMakerFeeBps = 1.5;
	let editSessionFundingMode: 'off' | 'fixed' | 'exchange' = 'off';
	let editSessionFundingRateBps = 0;
	let editSessionFundingIntervalHours = 8;
	let editing = false;

	// Selected session
	let selectedSession: PaperTradingSession | null = null;
	let sessionTrades: PaperTrade[] = [];
	// One-shot: a strategy id from a ?select= deep-link (e.g. the All Trades blotter)
	// to select THAT strategy's existing session on the next session load.
	let preselectStrategyId: string | null = null;

	// Visual replay state
	let showVisualReplay = false;
	let chartBars: OHLCVBar[] = [];
	let loadingBars = false;
	let replaySpeedInput = 1;
	let chartKey = 0;
	let lastReplayCursor: number | null = null;
	let lastIndicatorCursor: number | null = null;
	let loadBarsGeneration = 0;

	// Chart indicators and markers
	let mainIndicators: IndicatorConfig[] = [];
	let subIndicators: IndicatorConfig[] = [];
	let entryMarkers: SignalMarker[] = [];
	let exitMarkers: SignalMarker[] = [];
	// Full-history strategy triggers (entry/exit signals incl. pre-live) + active
	// position levels (SL/TP/trailing) — fed to the chart by the parity bundle.
	let triggerMarkers: SignalMarker[] = [];
	let positionPriceLines: Array<{ id: string; price: number; color?: string; title?: string; dashed?: boolean }> = [];
	let blockedMarkers: TradeMarker[] = [];
	let indicatorConfig: Record<string, SessionIndicatorConfig> = {};
	let sessionIndicatorHistory: SessionIndicatorsResponse['indicators'] = {};
	let indicatorVisibility: Record<string, boolean> = {};
	let showIndicatorPanel = false;
	let showParams = false;
	let preferredChartTimeframe = '';
	let activeDrawingTool: ChartDrawingTool = 'cursor';
	let chartDrawings: ChartDrawing[] = [];
	let pendingTrendLineStart: ChartDrawingPoint | null = null;
	let fitContentToken = 0;

	const timeframes = ORDERED_TIMEFRAME_VALUES.filter((value) => value !== '1w');
	const ibkrSecTypes = ['STK', 'IND', 'CFD', 'FUT', 'OPT', 'CASH', 'CRYPTO'];
	const ibkrWhatToShowOptions: Array<'TRADES' | 'MIDPOINT' | 'BID' | 'ASK'> = ['TRADES', 'MIDPOINT', 'BID', 'ASK'];
	const paperStockSymbols = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'SPY', 'QQQ'];
	const speedPresets = [0.5, 1, 2, 5, 10, 20];
	const CHART_MARKER_TIMEOUT_MS = 5_000;
	const tradeHistoryColumns = [
		{ key: 'side', label: 'Side' },
		{ key: 'entry_time', label: 'Entry Time' },
		{ key: 'exit_time', label: 'Exit Time' },
		{ key: 'entry_price', label: 'Entry' },
		{ key: 'exit_price', label: 'Exit' },
		{ key: 'pnl', label: '$ P&L', align: 'right' as const },
		{ key: 'pnl_pct', label: 'P&L %', align: 'right' as const },
	];

	let liveChartPoller: Poller | null = null;
	let selectedSessionPoller: Poller | null = null;
	let liveChartRefreshTimer: ReturnType<typeof setTimeout> | null = null;
	let selectedSessionRefreshTimer: ReturnType<typeof setTimeout> | null = null;
	let livePriceUnsubscribe: (() => void) | null = null;
	let forvenEventUnsubscribe: (() => void) | null = null;
	let selectSessionUnsubscribe: (() => void) | null = null;
	let latestLivePrices: Record<string, number> = {};
	const compatSessionPrefix = 'compat:strategy:';

	function getSelectedSessionStorageKey(): string {
		if (view === 'live') return 'forven.live.selectedSessionId';
		if (view === 'all') return 'forven.sessions.selectedSessionId';
		return 'forven.paper.selectedSessionId';
	}

	function isCompatSessionId(sessionId: string | null | undefined): boolean {
		return String(sessionId || '').startsWith(compatSessionPrefix);
	}

	function isCompatSession(session: PaperTradingSession | null | undefined): boolean {
		return isCompatSessionId(session?.id);
	}

	function isDeployedCompatSession(session: PaperTradingSession | null | undefined): boolean {
		if (!isCompatSession(session)) return false;
		const kind = String((session as Record<string, unknown> | null | undefined)?.compat_kind ?? '').toLowerCase();
		return kind === 'deployed';
	}

	// Capital to render on a card: deployed/live -> the REAL Hyperliquid wallet equity
	// (account_value); paper -> the simulated sandbox capital. Returns null for a
	// deployed session whose real balance has not synced yet, so the UI shows
	// "balance unavailable" instead of the fabricated $10k sandbox base.
	function displayCapital(session: PaperTradingSession | null | undefined): number | null {
		if (!session) return null;
		if (isDeployedCompatSession(session)) {
			// Nullish FIRST — Number(null) === 0 would render a fabricated "$0.00 · real"
			// wallet for a deployed session whose balance hasn't synced (account_value null).
			if (session.account_value == null) return null;
			const v = Number(session.account_value);
			return Number.isFinite(v) ? v : null;
		}
		const c = Number(session.capital);
		return Number.isFinite(c) ? c : null;
	}

	// 'real' when a deployed session is showing a genuine exchange balance,
	// 'unavailable' when it is deployed but the balance has not synced, else null
	// (paper). Drives the small tag next to the live Capital figure.
	function balanceState(session: PaperTradingSession | null | undefined): 'real' | 'unavailable' | null {
		if (!isDeployedCompatSession(session)) return null;
		const src = String(session?.balance_source ?? '').toLowerCase();
		return src === 'exchange' || src === 'books_only' || src === 'books_aggregate' ? 'real' : 'unavailable';
	}

	// Manual controls are enabled for every compat session — paper AND deployed/live.
	// On a deployed session the backend routes actions to REAL Hyperliquid orders
	// (close = reduce-only, open = market + resting SL/TP). The backend re-checks
	// server-side and gates live opens — the UI gate is convenience, not the boundary.
	function supportsManualControl(session: PaperTradingSession | null | undefined): boolean {
		return isCompatSession(session);
	}

	// True when the selected session is a deployed/live strategy — drives the
	// real-money warnings on the manual controls and confirm modal.
	$: isLiveSelected = isDeployedCompatSession(selectedSession);

	function toPaperTrade(row: unknown): PaperTrade {
		return row as PaperTrade;
	}

	function getPaperTradeRowKey(row: unknown, index: number): string | number {
		const trade = toPaperTrade(row);
		return trade.id ?? index;
	}

	type LegacyTradeMarker = TradeMarker & { time?: string };

	function markerTimestamp(marker: TradeMarker): string {
		const legacy = marker as LegacyTradeMarker;
		return legacy.time ?? marker.timestamp;
	}

	function markerDirection(marker: TradeMarker): 'long' | 'short' {
		return String(marker.direction || 'long').toLowerCase() === 'short' ? 'short' : 'long';
	}

	function markerSource(marker: TradeMarker): 'trade' | 'signal' {
		return String(marker.marker_kind || 'trade').toLowerCase() === 'signal' ? 'signal' : 'trade';
	}

	function markerLabel(marker: TradeMarker, type: 'entry' | 'exit'): string {
		const direction = markerDirection(marker);
		const source = markerSource(marker);
		if (type === 'entry') {
			if (source === 'signal') return direction === 'short' ? 'Short signal' : 'Buy signal';
			return direction === 'short' ? 'Short fill' : 'Buy fill';
		}
		if (source === 'signal') return direction === 'short' ? 'Cover signal' : 'Sell signal';
		return direction === 'short' ? 'Cover fill' : 'Sell fill';
	}

	function toSignalMarker(marker: TradeMarker, type: 'entry' | 'exit'): SignalMarker {
		return {
			timestamp: markerTimestamp(marker),
			price: marker.price,
			type,
			direction: markerDirection(marker),
			// Prefer the backend's short on-chart text (BUY/SELL/SHORT/COVER); fall
			// back to the derived verbose label for legacy payloads.
			label: marker.label ?? markerLabel(marker, type),
			source: markerSource(marker),
			// Forward the self-describing visuals so the chart renders straight from
			// the payload (ChartWorkspace falls back to direction when absent).
			...(marker.side ? { side: marker.side } : {}),
			...(marker.action ? { action: marker.action } : {}),
			...(marker.shape ? { shape: marker.shape } : {}),
			...(marker.color ? { color: marker.color } : {}),
		};
	}

	function applyTradeMarkers(markers: TradeMarkersResponse) {
		entryMarkers = markers.entries.map((marker) => toSignalMarker(marker, 'entry'));
		exitMarkers = markers.exits.map((marker) => toSignalMarker(marker, 'exit'));
		blockedMarkers = [...(markers.blocked ?? [])];
	}

	function latestBlockedMarker(): TradeMarker | null {
		if (blockedMarkers.length === 0) return null;
		return blockedMarkers[blockedMarkers.length - 1] ?? null;
	}

	function sortSessionsForDisplay(items: PaperTradingSession[]): PaperTradingSession[] {
		return [...items].sort((left, right) => {
			// 1. Open positions first
			const leftOpen = left.status === 'position_open' ? 0 : 1;
			const rightOpen = right.status === 'position_open' ? 0 : 1;
			if (leftOpen !== rightOpen) return leftOpen - rightOpen;

			// 2. More trades first
			const leftTrades = left.total_trades ?? 0;
			const rightTrades = right.total_trades ?? 0;
			if (leftTrades !== rightTrades) return rightTrades - leftTrades;

			// 3. Alphabetical by strategy name
			return (left.strategy_name || '').localeCompare(right.strategy_name || '');
		});
	}

	function getPreferredSession(items: PaperTradingSession[], preferredId: string | null): PaperTradingSession | null {
		if (items.length === 0) return null;
		if (preferredId) {
			const matched = items.find((session) => session.id === preferredId);
			if (matched) return matched;
		}
		return sortSessionsForDisplay(items)[0] ?? null;
	}

	function readStoredSelectedSessionId(): string | null {
		if (typeof window === 'undefined') return null;
		try {
			return window.localStorage.getItem(getSelectedSessionStorageKey());
		} catch {
			return null;
		}
	}

	function writeStoredSelectedSessionId(sessionId: string | null): void {
		if (typeof window === 'undefined') return;
		try {
			const key = getSelectedSessionStorageKey();
			if (sessionId) {
				window.localStorage.setItem(key, sessionId);
				return;
			}
			window.localStorage.removeItem(key);
		} catch {
			// localStorage can fail in strict browser modes
		}
	}

	function clearPrefillQueryParams(): void {
		if (typeof window === 'undefined') return;
		const nextUrl = new URL(window.location.href);
		nextUrl.searchParams.delete('strategy');
		nextUrl.searchParams.delete('symbol');
		nextUrl.searchParams.delete('timeframe');
		nextUrl.searchParams.delete('select');
		nextUrl.searchParams.delete('view');
		const nextPath = `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
		const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
		if (nextPath !== currentPath) {
			window.history.replaceState(window.history.state, '', nextPath);
		}
	}

	function parseTimestampMs(value: string | null | undefined): number | null {
		if (!value) return null;
		const parsed = new Date(value).getTime();
		return Number.isNaN(parsed) ? null : parsed;
	}

	function mergeBarsByTimestamp(baseBars: OHLCVBar[], liveBars: OHLCVBar[], limit: number): OHLCVBar[] {
		const merged = new Map<string, OHLCVBar>();
		for (const bar of baseBars) {
			merged.set(bar.timestamp, bar);
		}
		for (const bar of liveBars) {
			merged.set(bar.timestamp, bar);
		}
		return [...merged.values()]
			.sort((left, right) => (parseTimestampMs(left.timestamp) ?? 0) - (parseTimestampMs(right.timestamp) ?? 0))
			.slice(-limit);
	}

	function normalizeAssetKey(value: string | null | undefined): string {
		let normalized = String(value || '').trim().toUpperCase();
		if (!normalized) return '';

		const suffixes = [
			'/USDT', '/USD', '/USDC',
			'-USDT', '-USD', '-USDC',
			'_USDT', '_USD', '_USDC',
			'USDT', 'USD', 'USDC',
		];
		for (const suffix of suffixes) {
			if (!normalized.endsWith(suffix)) continue;
			normalized = normalized.slice(0, -suffix.length);
			break;
		}
		return normalized.replace(/[\/\-_]/g, '').trim();
	}

	function resolveLivePriceForSymbol(symbol: string | null | undefined, priceMap: Record<string, number>): number | null {
		const normalizedSymbol = String(symbol || '').trim().toUpperCase();
		if (!normalizedSymbol) return null;

		const directCandidates = [
			normalizedSymbol,
			normalizedSymbol.replaceAll('-', '/'),
			normalizedSymbol.replaceAll('_', '/'),
		];
		for (const candidate of directCandidates) {
			const directValue = Number(priceMap[candidate]);
			if (Number.isFinite(directValue) && directValue > 0) {
				return directValue;
			}
		}

		const assetKey = normalizeAssetKey(normalizedSymbol);
		if (!assetKey) return null;

		for (const [rawKey, rawValue] of Object.entries(priceMap)) {
			if (normalizeAssetKey(rawKey) !== assetKey) continue;
			const parsedValue = Number(rawValue);
			if (Number.isFinite(parsedValue) && parsedValue > 0) {
				return parsedValue;
			}
		}
		return null;
	}

	function inferPositionPnlMultiplier(session: PaperTradingSession): number {
		if (!session.position) return 1;
		const entryPrice = Number(session.position.entry_price);
		const size = Number(session.position.size);
		const currentPrice = Number(session.position.current_price ?? session.current_price);
		const currentPnl = Number(session.position.unrealized_pnl);
		const signed = String(session.position.side || '').toLowerCase() === 'short' ? -1 : 1;
		const basePnl = (currentPrice - entryPrice) * size * signed;
		if (!Number.isFinite(basePnl) || Math.abs(basePnl) < 1e-9) return 1;
		const multiplier = currentPnl / basePnl;
		return Number.isFinite(multiplier) && Math.abs(multiplier) > 0 ? multiplier : 1;
	}

	function inferPositionPctMultiplier(session: PaperTradingSession): number {
		if (!session.position) return 1;
		const entryPrice = Number(session.position.entry_price);
		const currentPrice = Number(session.position.current_price ?? session.current_price);
		const currentPct = Number(session.position.unrealized_pnl_pct);
		const signed = String(session.position.side || '').toLowerCase() === 'short' ? -1 : 1;
		if (!Number.isFinite(entryPrice) || entryPrice <= 0) return inferPositionPnlMultiplier(session);
		const basePct = ((currentPrice - entryPrice) / entryPrice) * signed * 100;
		if (!Number.isFinite(basePct) || Math.abs(basePct) < 1e-9) return inferPositionPnlMultiplier(session);
		const multiplier = currentPct / basePct;
		return Number.isFinite(multiplier) && Math.abs(multiplier) > 0 ? multiplier : inferPositionPnlMultiplier(session);
	}

	function buildRealtimeSessionSnapshot(session: PaperTradingSession, priceMap: Record<string, number>): PaperTradingSession {
		if (session.mode === 'replay') return session;
		const nextPrice = resolveLivePriceForSymbol(session.symbol, priceMap);
		if (nextPrice === null) return session;

		const indicatorTimestamp = session.indicators?.price?.timestamp ?? new Date().toISOString();
		const nextIndicators = {
			...(session.indicators ?? {}),
			price: {
				name: 'price',
				value: nextPrice,
				timestamp: indicatorTimestamp,
			},
		};

		// Deployed/live sessions: capital, total_pnl and the position's unrealized P&L
		// are REAL exchange values supplied by the server. A price tick must NOT
		// recompute them off the synthetic initial_capital — that would re-fake the
		// live equity between server refreshes. Update only the live price line/marker.
		if (isDeployedCompatSession(session)) {
			if (Math.abs((session.current_price ?? 0) - nextPrice) < 1e-9) {
				return session;
			}
			return {
				...session,
				current_price: nextPrice,
				position: session.position ? { ...session.position, current_price: nextPrice } : session.position,
				indicators: nextIndicators,
			};
		}

		if (!session.position) {
			if (Math.abs((session.current_price ?? 0) - nextPrice) < 1e-9) {
				return session;
			}
			return {
				...session,
				current_price: nextPrice,
				indicators: nextIndicators,
			};
		}

		const entryPrice = Number(session.position.entry_price);
		const size = Number(session.position.size);
		const signed = String(session.position.side || '').toLowerCase() === 'short' ? -1 : 1;
		const pnlMultiplier = inferPositionPnlMultiplier(session);
		const pctMultiplier = inferPositionPctMultiplier(session);
		const basePnl = (nextPrice - entryPrice) * size * signed;
		const nextUnrealizedPnl = Number.isFinite(basePnl) ? basePnl * pnlMultiplier : session.position.unrealized_pnl;
		const basePct = Number.isFinite(entryPrice) && entryPrice > 0
			? ((nextPrice - entryPrice) / entryPrice) * signed * 100
			: session.position.unrealized_pnl_pct;
		const nextUnrealizedPct = Number.isFinite(basePct) ? basePct * pctMultiplier : session.position.unrealized_pnl_pct;
		const previousUnrealized = Number(session.position.unrealized_pnl) || 0;
		const closedPnl = (Number(session.total_pnl) || 0) - previousUnrealized;
		const nextTotalPnl = closedPnl + (Number(nextUnrealizedPnl) || 0);
		const nextCapital = (Number(session.initial_capital) || 0) + nextTotalPnl;
		const nextTotalPnlPct = Number(session.initial_capital) > 0
			? (nextTotalPnl / Number(session.initial_capital)) * 100
			: session.total_pnl_pct;
		const nextPosition = {
			...session.position,
			current_price: nextPrice,
			unrealized_pnl: nextUnrealizedPnl,
			unrealized_pnl_pct: nextUnrealizedPct,
		};
		const positionUnchanged = Math.abs((session.position.current_price ?? 0) - nextPrice) < 1e-9
			&& Math.abs((session.position.unrealized_pnl ?? 0) - (nextUnrealizedPnl ?? 0)) < 1e-9
			&& Math.abs((session.position.unrealized_pnl_pct ?? 0) - (nextUnrealizedPct ?? 0)) < 1e-9;
		if (Math.abs((session.current_price ?? 0) - nextPrice) < 1e-9 && positionUnchanged) {
			return session;
		}
		return {
			...session,
			current_price: nextPrice,
			position: nextPosition,
			total_pnl: nextTotalPnl,
			total_pnl_pct: nextTotalPnlPct,
			capital: nextCapital,
			indicators: nextIndicators,
		};
	}

	function applyLivePriceToChart(nextPrice: number): void {
		if (!selectedSession || selectedSession.mode === 'replay' || chartBars.length === 0) return;
		// Exchange-style live candles: each WS tick paints the FORMING bar and ROLLS a
		// new candle at the timeframe boundary — previously ticks kept mutating the old
		// last bar until the 15s bundle poll delivered the new one.
		const nextBars = applyTickToBars(chartBars, nextPrice, Date.now(), activeVisualChartTimeframe);
		if (nextBars !== chartBars) {
			chartBars = nextBars;
		}
	}

	function applyRealtimePriceSnapshot(priceMap: Record<string, number>): void {
		latestLivePrices = priceMap;
		if (Object.keys(priceMap).length === 0) return;

		let sessionsChanged = false;
		const nextSessions = sessions.map((session) => {
			const nextSession = buildRealtimeSessionSnapshot(session, priceMap);
			if (nextSession !== session) {
				sessionsChanged = true;
			}
			return nextSession;
		});
		if (sessionsChanged) {
			sessions = nextSessions;
		}

		if (!selectedSession) return;
		const resolvedSelected = (sessionsChanged ? nextSessions : sessions).find((session) => session.id === selectedSession?.id)
			?? buildRealtimeSessionSnapshot(selectedSession, priceMap);
		if (resolvedSelected === selectedSession) return;
		const nextPrice = resolvedSelected.current_price;
		selectedSession = resolvedSelected;
		if (Number.isFinite(nextPrice) && resolvedSelected.mode !== 'replay') {
			applyLivePriceToChart(nextPrice);
		}
	}

	function applyLatestRealtimeSnapshot(): void {
		if (Object.keys(latestLivePrices).length === 0) return;
		applyRealtimePriceSnapshot(latestLivePrices);
	}

	type ArchivedReasonSnapshot = {
		event: LifecycleEvent | null;
		reason: string;
		fromState: string | null;
		toState: string | null;
		timestamp: string | null;
		actor: string | null;
	};

	const archivedLifecycleStates = new Set(['retired', 'archived', 'rejected', 'trash', 'killed']);
	const paperLifecycleStates = new Set(['paper', 'paper_trading', 'paper_challenger']);

	function normalizeLifecycleState(state: string | null | undefined): string {
		return String(state || '').trim().toLowerCase();
	}

	function prettyLifecycleState(state: string | null | undefined): string {
		const normalized = normalizeLifecycleState(state);
		if (!normalized) return '--';
		return normalized.replaceAll('_', ' ');
	}

	function compactReason(reason: string | null | undefined, maxLength = 220): string {
		const text = String(reason || '')
			.replaceAll('\n', ' ')
			.replaceAll('\r', ' ')
			.replace(/\s+/g, ' ')
			.trim();
		if (!text) return 'No archived/demotion reason was recorded.';
		if (text.length <= maxLength) return text;
		return `${text.slice(0, maxLength - 3)}...`;
	}

	function findPaperDemotionEvent(events: LifecycleEvent[]): LifecycleEvent | null {
		for (let idx = events.length - 1; idx >= 0; idx -= 1) {
			const event = events[idx];
			const fromState = normalizeLifecycleState(event?.from_state);
			const toState = normalizeLifecycleState(event?.to_state);
			if (!fromState || !toState) continue;
			if (paperLifecycleStates.has(fromState) && toState !== fromState) {
				return event;
			}
		}
		for (let idx = events.length - 1; idx >= 0; idx -= 1) {
			const event = events[idx];
			const toState = normalizeLifecycleState(event?.to_state);
			if (archivedLifecycleStates.has(toState)) {
				return event;
			}
		}
		return events.length > 0 ? events[events.length - 1] : null;
	}

	function summarizeArchivedReason(
		strategy: LifecycleStrategy | null | undefined,
		events: LifecycleEvent[]
	): ArchivedReasonSnapshot {
		const event = findPaperDemotionEvent(events);
		const reason = compactReason(
			event?.reason
				|| strategy?.blocked_reason
				|| 'No archived/demotion reason was recorded.'
		);
		return {
			event,
			reason,
			fromState: event?.from_state ?? null,
			toState: event?.to_state ?? strategy?.state ?? null,
			timestamp: event?.created_at ?? strategy?.updated_at ?? null,
			actor: event?.actor ?? null,
		};
	}

	function sortArchivedStrategies(items: LifecycleStrategy[]): LifecycleStrategy[] {
		return [...items].sort((left, right) => {
			const leftTs = parseTimestampMs(left.updated_at ?? left.created_at) ?? 0;
			const rightTs = parseTimestampMs(right.updated_at ?? right.created_at) ?? 0;
			return rightTs - leftTs;
		});
	}

	async function loadArchivedStrategies() {
		if (isLiveView) {
			archivedStrategies = [];
			selectedArchivedStrategy = null;
			selectedArchivedEvents = [];
			return;
		}

		archivedLoading = true;
		try {
			const rows = await listLifecycleStrategies({
				state: 'archived',
				limit: 200,
				offset: 0,
			});
			archivedStrategies = sortArchivedStrategies(rows);
			if (selectedArchivedStrategy) {
				const refreshed = archivedStrategies.find((row) => row.id === selectedArchivedStrategy?.id) ?? null;
				if (!refreshed) {
					selectedArchivedStrategy = null;
					selectedArchivedEvents = [];
				} else {
					selectedArchivedStrategy = refreshed;
				}
			}
		} catch (err) {
			console.error('Failed to load archived strategies:', err);
			error = err instanceof Error ? err.message : 'Failed to load archived strategy history';
		} finally {
			archivedLoading = false;
		}
	}

	async function selectArchivedSession(strategy: LifecycleStrategy) {
		stopLiveChartPolling();
		stopSelectedSessionSync();
		selectedSession = null;
		sessionTrades = [];
		showVisualReplay = false;
		showParams = false;
		writeStoredSelectedSessionId(null);

		selectedArchivedStrategy = strategy;
		selectedArchivedEvents = [];
		archivedDetailLoading = true;
		error = null;
		try {
			const detail = await getLifecycleStrategy(strategy.id);
			selectedArchivedStrategy = detail.strategy;
			selectedArchivedEvents = detail.events ?? [];
		} catch (err) {
			console.error('Failed to load archived strategy detail:', err);
			error = err instanceof Error ? err.message : 'Failed to load archived strategy detail';
		} finally {
			archivedDetailLoading = false;
		}
	}

	async function loadLiveChart() {
		if (!selectedSession) return;
		const sessionId = selectedSession.id;
		if (chartBars.length === 0) {
			loadingBars = true;
		}
		try {
			await loadChartBundle(sessionId);
		} catch (e) {
			console.error('Failed to load live chart:', e);
		} finally {
			if (selectedSession?.id === sessionId) {
				loadingBars = false;
			}
		}
	}

	function scheduleLiveChartRefresh(delayMs = 1200) {
		if (liveChartRefreshTimer !== null) return;
		liveChartRefreshTimer = setTimeout(() => {
			liveChartRefreshTimer = null;
			void loadLiveChart();
		}, delayMs);
	}

	function startLiveChartPolling() {
		stopLiveChartPolling();
		liveChartPoller = createPoller(loadLiveChart, 15_000);
		liveChartPoller.start();
	}

	function stopLiveChartPolling() {
		liveChartPoller?.stop();
		liveChartPoller = null;
		if (liveChartRefreshTimer !== null) {
			clearTimeout(liveChartRefreshTimer);
			liveChartRefreshTimer = null;
		}
	}

	async function refreshSelectedSessionSnapshot() {
		if (!selectedSession || selectedSession.mode === 'replay') return;
		// Don't let a background poll overwrite fresh state while a manual action
		// (close/open/flip/adjust) is mid-flight — the action returns the truth.
		if (requestInFlight) return;
		const sessionId = selectedSession.id;
		const previousTradeCount = selectedSession.total_trades ?? 0;
		try {
			const refreshed = await getPaperSession(sessionId);
			if (selectedSession?.id !== sessionId) return;
			updateSession(refreshed);
			applyLatestRealtimeSnapshot();
			if ((refreshed.total_trades ?? 0) !== previousTradeCount) {
				void loadSessionTrades(sessionId);
			}
			if (showVisualReplay) {
				scheduleLiveChartRefresh(250);
			}
		} catch (err) {
			console.warn('Failed to refresh selected paper session:', err);
		}
	}

	function scheduleSelectedSessionRefresh(delayMs = 1200) {
		if (!selectedSession || selectedSession.mode === 'replay') return;
		if (selectedSessionRefreshTimer !== null) return;
		selectedSessionRefreshTimer = setTimeout(() => {
			selectedSessionRefreshTimer = null;
			void refreshSelectedSessionSnapshot();
		}, delayMs);
	}

	function startSelectedSessionSync() {
		stopSelectedSessionSync();
		if (!selectedSession || selectedSession.mode === 'replay') return;
		selectedSessionPoller = createPoller(refreshSelectedSessionSnapshot, 10_000);
		selectedSessionPoller.start();
	}

	function stopSelectedSessionSync() {
		selectedSessionPoller?.stop();
		selectedSessionPoller = null;
		if (selectedSessionRefreshTimer !== null) {
			clearTimeout(selectedSessionRefreshTimer);
			selectedSessionRefreshTimer = null;
		}
	}

	function handleKeyDown(event: KeyboardEvent) {
		if (!supportsReplayControls || !showVisualReplay || !selectedSession || selectedSession.mode !== 'replay') {
			return;
		}
		if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) {
			return;
		}

		switch (event.code) {
			case 'Space':
				event.preventDefault();
				if (selectedSession.replay_state?.is_playing) {
					handleReplayPause();
				} else {
					handleReplayPlay();
				}
				break;
			case 'ArrowRight':
				event.preventDefault();
				if (event.shiftKey) {
					handleReplayStep(10);
				} else {
					handleReplayStep(1);
				}
				break;
			case 'ArrowLeft':
				event.preventDefault();
				if (selectedSession.replay_state && selectedSession.replay_state.cursor > 0) {
					const newIndex = Math.max(0, selectedSession.replay_state.cursor - (event.shiftKey ? 10 : 1));
					handleReplaySeek(newIndex);
				}
				break;
			case 'KeyR':
				if (event.ctrlKey || event.metaKey) {
					return;
				}
				event.preventDefault();
				handleReplayReset();
				break;
			case 'Home':
				event.preventDefault();
				handleReplaySeek(0);
				break;
			case 'End':
				event.preventDefault();
				if (selectedSession.replay_state) {
					handleReplaySeek(selectedSession.replay_state.total_bars - 1);
				}
				break;
		}
	}

	function handleForvenRealtimeEvent(): void {
		if (!selectedSession || selectedSession.mode === 'replay') return;
		scheduleSelectedSessionRefresh(900);
		if (showVisualReplay) {
			scheduleLiveChartRefresh(1200);
		}
	}

	onMount(async () => {
		livePriceUnsubscribe = forvenLivePrices.subscribe((priceMap) => {
			applyRealtimePriceSnapshot(priceMap ?? {});
		});
		if (typeof window !== 'undefined') {
			const eventHandler = () => handleForvenRealtimeEvent();
			window.addEventListener('forven:event', eventHandler);
			forvenEventUnsubscribe = () => window.removeEventListener('forven:event', eventHandler);

			const selectHandler = (event: Event) => handleSelectSessionRequest(event);
			window.addEventListener('forven:select-session', selectHandler);
			selectSessionUnsubscribe = () => window.removeEventListener('forven:select-session', selectHandler);
		}

		const strategyParam = $page.url.searchParams.get('strategy');
		const symbolParam = $page.url.searchParams.get('symbol');
		const timeframeParam = $page.url.searchParams.get('timeframe');
		const hasExplicitCreateIntent = Boolean(strategyParam || symbolParam || timeframeParam);
		let hasWorkspacePrefillIntent = false;
		// ?select=<strategyId> means "select that strategy's existing session" (a
		// deep-link from the All Trades blotter) — distinct from ?strategy= which
		// pre-fills the NEW-session form.
		preselectStrategyId = $page.url.searchParams.get('select');
		if (hasExplicitCreateIntent || preselectStrategyId) {
			clearPrefillQueryParams();
		}

		if (strategyParam && allowsSessionEditing) {
			newSessionStrategy = strategyParam;
		}
		if (symbolParam && allowsSessionEditing) newSessionSymbol = symbolParam;
		if (timeframeParam && allowsSessionEditing) newSessionTimeframe = timeframeParam;
		if (!strategyParam && !symbolParam && !timeframeParam && allowsSessionEditing) {
			const ctx = $workspaceContext;
			const savedDataset = $selectedDatasetStore;
			if (ctx.strategy || savedDataset?.strategy) {
				newSessionStrategy = ctx.strategy ?? savedDataset?.strategy ?? newSessionStrategy;
				hasWorkspacePrefillIntent = true;
			}
			if (ctx.symbol || savedDataset?.symbol) {
				newSessionSymbol = ctx.symbol ?? savedDataset?.symbol ?? newSessionSymbol;
			}
			if (ctx.timeframe || savedDataset?.timeframe) {
				newSessionTimeframe = ctx.timeframe ?? savedDataset?.timeframe ?? newSessionTimeframe;
			}
		}

		if (allowsSessionEditing) {
			await Promise.all([loadStrategies(), loadDatasets()]);
			try {
				const service = await startPaperService();
				highActivityTestEnabled = Boolean(service?.high_activity_test);
			} catch {
				console.log('Paper service may already be running');
			}
		}
		await loadSessions();
		if (!allowsSessionEditing) {
			showNewSession = false;
		} else if (sessions.length > 0) {
			showNewSession = false;
		} else {
			showNewSession = hasExplicitCreateIntent || hasWorkspacePrefillIntent;
		}

		if (typeof window !== 'undefined') {
			window.addEventListener('keydown', handleKeyDown);
		}
	});

	onDestroy(() => {
		livePriceUnsubscribe?.();
		livePriceUnsubscribe = null;
		forvenEventUnsubscribe?.();
		forvenEventUnsubscribe = null;
		selectSessionUnsubscribe?.();
		selectSessionUnsubscribe = null;
		stopLiveChartPolling();
		stopSelectedSessionSync();
		if (typeof window !== 'undefined') {
			window.removeEventListener('keydown', handleKeyDown);
		}
	});

	async function loadStrategies() {
		try {
			const response = await getStrategies();
			strategies = response.strategies || [];
			if (strategies.length > 0 && !newSessionStrategy) {
				newSessionStrategy = strategies[0].name;
			}
		} catch (e) {
			console.error('Failed to load strategies:', e);
		}
	}

	async function loadDatasets() {
		try {
			datasets = await getDatasets();
		} catch (e) {
			console.error('Failed to load datasets:', e);
		}
	}

	async function loadSessions() {
		loading = true;
		try {
			const loadedSessions = await getPaperSessions({
				includeDeployed: view !== 'paper',
				onlyDeployed: view === 'live',
			});
			sessions = loadedSessions;

			// Resolve a ?select= deep-link (strategy id -> its session) ONCE on the
			// initial load; it overrides the stored/first-session default so the
			// blotter lands the operator on that strategy's trade.
			let preselectId: string | null = null;
			if (preselectStrategyId) {
				const match = loadedSessions.find(
					(s) => s.strategy_id === preselectStrategyId || s.id === preselectStrategyId
				);
				if (match) preselectId = match.id;
				preselectStrategyId = null;
			}
			const keepArchivedSelection = Boolean(selectedArchivedStrategy && !selectedSession) && !preselectId;
			const preferredId = preselectId ?? selectedSession?.id ?? readStoredSelectedSessionId();
			const preferredSession = getPreferredSession(loadedSessions, preferredId);
			if (preferredSession && !keepArchivedSelection) {
				if (preselectId) writeStoredSelectedSessionId(preferredSession.id);
				if (selectedSession?.id !== preferredSession.id) {
					selectSession(preferredSession);
				} else {
					selectedSession = preferredSession;
					startSelectedSessionSync();
					applyLatestRealtimeSnapshot();
				}
			} else if (!preferredSession) {
				stopLiveChartPolling();
				stopSelectedSessionSync();
				selectedSession = null;
				writeStoredSelectedSessionId(null);
			}
			await loadArchivedStrategies();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load sessions';
		} finally {
			loading = false;
		}
	}

	async function enableHighActivityTest() {
		highActivityToggleBusy = true;
		try {
			const service = await startPaperService({
				highActivityTest: true,
				runScanNow: true,
			});
			highActivityTestEnabled = Boolean(service?.high_activity_test);
			await loadSessions();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to start high-activity test mode';
		} finally {
			highActivityToggleBusy = false;
		}
	}

	async function disableHighActivityTest() {
		highActivityToggleBusy = true;
		try {
			await stopPaperService({ disableTestMode: true });
			highActivityTestEnabled = false;
			await startPaperService({ runScanNow: false });
			await loadSessions();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to stop high-activity test mode';
		} finally {
			highActivityToggleBusy = false;
		}
	}

	$: sortedSessions = sortSessionsForDisplay(sessions);
	$: selectedArchivedReason = summarizeArchivedReason(selectedArchivedStrategy, selectedArchivedEvents);
	$: archivedTimelineEvents = [...selectedArchivedEvents].reverse();
	$: activeVisualChartTimeframe = normalizeChartTimeframe(preferredChartTimeframe || selectedSession?.timeframe || '1h');
	$: canChangeVisualChartTimeframe = Boolean(selectedSession && selectedSession.mode !== 'replay');
	$: indicatorConfigNames = Object.keys(indicatorConfig);
	$: overlayIndicatorNames = indicatorConfigNames.filter((name) => getIndicatorSidebarGroup(name, indicatorConfig[name]) === 'overlays');
	$: lowerPaneIndicatorNames = indicatorConfigNames.filter((name) => getIndicatorSidebarGroup(name, indicatorConfig[name]) === 'lower');
	$: sidebarOnlyIndicatorNames = indicatorConfigNames.filter((name) => getIndicatorSidebarGroup(name, indicatorConfig[name]) === 'sidebar');
	$: bottomIndicatorNames = Array.from(
		new Set([
			...indicatorConfigNames,
			...Object.keys(selectedSession?.indicators ?? {}),
		]),
	);

	function toLocalInput(value: string | null): string {
		if (!value) return '';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '';
		const tzOffset = date.getTimezoneOffset() * 60000;
		return new Date(date.getTime() - tzOffset).toISOString().slice(0, 16);
	}

	function openEditSession(session: PaperTradingSession) {
		editSessionId = session.id;
		editSessionStrategy = session.strategy_name;
		editSessionSymbol = session.symbol;
		editSessionTimeframe = session.timeframe;
		editSessionCapital = session.initial_capital ?? 10000;
		editSessionPositionSize = session.position_size_pct;
		editSessionStopLoss = session.stop_loss_pct;
		editSessionTakeProfit = session.take_profit_pct;
		editSessionTrailingStop = session.trailing_stop_pct;
		editSessionParams = { ...session.params };
		editSessionMode = session.mode ?? 'live';
		editSessionLiveFeed = session.live_feed ?? 'default';
		editSessionIBKRSecType = session.ibkr_sec_type ?? 'STK';
		editSessionIBKRExchange = session.ibkr_exchange ?? 'SMART';
		editSessionIBKRCurrency = session.ibkr_currency ?? 'USD';
		editSessionIBKRWhatToShow = session.ibkr_what_to_show ?? 'TRADES';
		editSessionReplayStart = toLocalInput(session.replay_start ?? null);
		editSessionReplayEnd = toLocalInput(session.replay_end ?? null);
		editSessionReplaySpeed = session.replay_speed ?? 1;
		editSessionFeeMode = session.fee_mode ?? 'taker';
		editSessionTakerFeeBps = session.taker_fee_bps ?? 4.5;
		editSessionMakerFeeBps = session.maker_fee_bps ?? 1.5;
		editSessionFundingMode = session.funding_mode ?? 'off';
		editSessionFundingRateBps = session.funding_rate_bps_per_interval ?? 0;
		editSessionFundingIntervalHours = session.funding_interval_hours ?? 8;
		showEditSession = true;
	}

	function unsupportedPaperControlMessage(): string {
		return 'Standalone paper sessions are disabled. Paper sessions are projected from strategies in paper stage.';
	}

	function showUnsupportedPaperControlMessage(): void {
		error = unsupportedPaperControlMessage();
	}

	async function handleCreateSession() {
		creating = false;
		showNewSession = false;
		showUnsupportedPaperControlMessage();
	}

	async function handleUpdateSession() {
		if (!editSessionId) return;
		editing = false;
		showEditSession = false;
		showUnsupportedPaperControlMessage();
	}

	async function handleStartSession(session: PaperTradingSession) {
		void session;
		showUnsupportedPaperControlMessage();
	}

	async function handleStopSession(session: PaperTradingSession) {
		void session;
		showUnsupportedPaperControlMessage();
	}

	async function handleDeleteSession(session: PaperTradingSession) {
		void session;
		showUnsupportedPaperControlMessage();
	}

	// ── Manual control action runner ────────────────────────────────────────
	async function runManualAction(fn: () => Promise<PaperTradingSession>): Promise<void> {
		if (requestInFlight) return;
		requestInFlight = true;
		error = null;
		try {
			const updated = await fn();
			updateSession(updated);
			applyLatestRealtimeSnapshot();
			void loadSessionTrades(updated.id);
			if (showVisualReplay) scheduleLiveChartRefresh(250);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Manual action failed.';
		} finally {
			requestInFlight = false;
		}
	}

	function confirmAction(label: string, detail: string, run: () => Promise<PaperTradingSession>): void {
		pendingConfirm = { label, detail, run };
	}

	async function executeConfirm(): Promise<void> {
		const pending = pendingConfirm;
		pendingConfirm = null;
		if (pending) await runManualAction(pending.run);
	}

	function cancelConfirm(): void {
		pendingConfirm = null;
	}

	function positionPnlText(): string {
		const pos = selectedSession?.position;
		if (!pos) return '';
		return `${formatDollarPnl(pos.unrealized_pnl)} (${formatPercent(pos.unrealized_pnl_pct)})`;
	}

	function handleClosePosition(): void {
		const pos = selectedSession?.position;
		if (!selectedSession || !pos) return;
		confirmAction(
			'Close position',
			`Close ${pos.side.toUpperCase()} ${formatQty(pos.size)} @ ~${formatPrice(selectedSession.current_price)} · est P&L ${positionPnlText()}`,
			() => closePaperPosition(selectedSession!.id),
		);
	}

	function handleFlipPosition(): void {
		const pos = selectedSession?.position;
		if (!selectedSession || !pos) return;
		const opposite = pos.side === 'long' ? 'SHORT' : 'LONG';
		confirmAction(
			'Flip position',
			`Close ${pos.side.toUpperCase()} and open ${opposite} ${formatQty(pos.size)} @ ~${formatPrice(selectedSession.current_price)}`,
			() => flipPaperPosition(selectedSession!.id),
		);
	}

	function handlePartialClose(): void {
		const pos = selectedSession?.position;
		if (!selectedSession || !pos) return;
		const pct = Number(partialPctInput);
		if (!Number.isFinite(pct) || pct <= 0 || pct > 100) {
			error = 'Enter a partial percent in (0, 100].';
			return;
		}
		confirmAction(
			'Partial close',
			`Close ${pct}% of ${pos.side.toUpperCase()} (${formatQty((pos.size * pct) / 100)}) @ ~${formatPrice(selectedSession.current_price)}`,
			async () => {
				const updated = await partialClosePaperPosition(selectedSession!.id, { pct });
				partialPctInput = '';
				return updated;
			},
		);
	}

	function handleToggleAutoManagement(): void {
		const pos = selectedSession?.position;
		if (!selectedSession || !pos) return;
		const paused = !pos.manual_pause;
		void runManualAction(() => setPaperAutoManagement(selectedSession!.id, paused));
	}

	function handleAdjustLevel(kind: 'sl' | 'tp', clear = false): void {
		if (!selectedSession?.position) return;
		let price: number | null = null;
		if (!clear) {
			price = Number(kind === 'sl' ? slInput : tpInput);
			if (!Number.isFinite(price) || price <= 0) {
				error = 'Enter a price > 0 (or use Clear).';
				return;
			}
		}
		const submit = kind === 'sl' ? adjustPaperStopLoss : adjustPaperTakeProfit;
		void runManualAction(async () => {
			const updated = await submit(selectedSession!.id, price);
			if (kind === 'sl') slInput = '';
			else tpInput = '';
			return updated;
		});
	}

	function handleOpenManual(): void {
		if (!selectedSession) return;
		const leverage = Number(openLeverageInput) || 1;
		const options: OpenManualPaperPositionOptions = { direction: openDirection, leverage };
		if (openSizeMode === 'size') {
			const size = Number(openSizeInput);
			if (!Number.isFinite(size) || size <= 0) {
				error = 'Enter a size > 0.';
				return;
			}
			options.size = size;
		} else {
			const riskPct = Number(openRiskPctInput);
			if (!Number.isFinite(riskPct) || riskPct <= 0 || riskPct > 100) {
				error = 'Enter risk % in (0, 100].';
				return;
			}
			options.riskPct = riskPct;
		}
		if (openSlInput) options.stopLossPrice = Number(openSlInput);
		if (openTpInput) options.takeProfitPrice = Number(openTpInput);
		const sizeText = openSizeMode === 'size' ? `${formatQty(options.size!)} units` : `${options.riskPct}% risk`;
		confirmAction(
			'Open position',
			`Open ${openDirection.toUpperCase()} ${sizeText} @ ~${formatPrice(selectedSession.current_price)} · ${leverage}x`,
			async () => {
				const updated = await openManualPaperPosition(selectedSession!.id, options);
				openSizeInput = '';
				openSlInput = '';
				openTpInput = '';
				return updated;
			},
		);
	}

	async function hydrateVisualReplayForSession(session: PaperTradingSession) {
		if (session.mode === 'replay') {
			lastReplayCursor = session.replay_state?.cursor ?? null;
			lastIndicatorCursor = null;
			await loadChartBars(session.id);
			await loadIndicatorsAndMarkersIfNeeded(session.id, session.replay_state?.cursor ?? null);
			return;
		}

		lastIndicatorCursor = null;
		await loadIndicatorsAndMarkers(session.id);
		startLiveChartPolling();
	}

	async function loadSessionTrades(sessionId: string) {
		try {
			sessionTrades = await getPaperTrades(sessionId, 500);
			if (sessionTrades.length === 0 && isCompatSessionId(sessionId)) {
				const localSession = selectedSession?.id === sessionId
					? selectedSession
					: sessions.find((session) => session.id === sessionId) ?? null;
				sessionTrades = [...(localSession?.trades ?? [])];
			}
		} catch (err) {
			console.error('Failed to load trades:', err);
			if (isCompatSessionId(sessionId)) {
				const localSession = selectedSession?.id === sessionId
					? selectedSession
					: sessions.find((session) => session.id === sessionId) ?? null;
				sessionTrades = [...(localSession?.trades ?? [])];
			} else {
				sessionTrades = [];
			}
		}
	}

	function selectSession(session: PaperTradingSession) {
		stopLiveChartPolling();
		stopSelectedSessionSync();
		selectedArchivedStrategy = null;
		selectedArchivedEvents = [];
		selectedSession = session;
		sessionTrades = [];
		writeStoredSelectedSessionId(session.id);
		showVisualReplay = true;
		showNewSession = false;
		chartBars = [];
		preferredChartTimeframe = '';
		lastReplayCursor = null;
		lastIndicatorCursor = null;
		mainIndicators = [];
		subIndicators = [];
		entryMarkers = [];
		exitMarkers = [];
		blockedMarkers = [];
		indicatorConfig = {};
		sessionIndicatorHistory = {};
		indicatorVisibility = {};
		showIndicatorPanel = false;
		showParams = false;
		fitContentToken += 1;
		clearChartDrawings();
		if (session.mode === 'replay' && session.replay_speed) {
			replaySpeedInput = session.replay_speed;
		}
		if (showVisualReplay) {
			void hydrateVisualReplayForSession(session);
		}
		startSelectedSessionSync();
		applyLatestRealtimeSnapshot();
		void loadSessionTrades(session.id);
	}

	async function handleSelectSessionRequest(event: Event) {
		const detail = (event as CustomEvent<{ sessionId?: string }>).detail ?? {};
		const sessionId = String(detail.sessionId ?? '').trim();
		if (!sessionId) return;
		// Already selected — nothing to switch, just make sure it's the visible panel.
		if (selectedSession?.id === sessionId) {
			showNewSession = false;
			return;
		}
		let target = sessions.find((s) => s.id === sessionId);
		if (!target) {
			// Stale list (e.g. position opened since the last poll) — refresh once.
			await loadSessions();
			target = sessions.find((s) => s.id === sessionId);
		}
		if (target) selectSession(target);
	}

	function updateSession(updated: PaperTradingSession) {
		sessions = sessions.map(s => s.id === updated.id ? updated : s);
		if (selectedSession?.id === updated.id) {
			selectedSession = updated;
		}
	}

	async function loadChartBars(
		sessionId: string,
		force: boolean = false,
		overrideBars: OHLCVBar[] | null = null
	) {
		if (overrideBars && overrideBars.length > 0) {
			loadBarsGeneration += 1;
			chartBars = [...overrideBars];
			loadingBars = false;
			return;
		}

		if (loadingBars && !force) return;

		loadBarsGeneration += 1;
		const thisGeneration = loadBarsGeneration;
		loadingBars = true;

		try {
			const bars = await getReplayBars(sessionId, 500);
			if (thisGeneration === loadBarsGeneration) {
				chartBars = [...bars];
			}
		} catch (e) {
			console.error('Failed to load chart bars:', e);
		} finally {
			loadingBars = false;
		}
	}

	async function loadIndicatorsAndMarkers(sessionId: string) {
		const chartTimeframe = activeVisualChartTimeframe;
		const [indicatorResult, markerResult] = await Promise.allSettled([
			getSessionIndicators(sessionId, undefined, 1000, chartTimeframe),
			getTradeMarkers(sessionId, { timeoutMs: CHART_MARKER_TIMEOUT_MS })
		]);

		if (selectedSession?.id !== sessionId) return;

		if (indicatorResult.status === 'fulfilled') {
			const indicatorData = indicatorResult.value;
			indicatorConfig = indicatorData.config;
			sessionIndicatorHistory = indicatorData.indicators;

			for (const name of Object.keys(indicatorData.indicators)) {
				if (!(name in indicatorVisibility)) {
					indicatorVisibility[name] = true;
				}
			}

			const mainInds: IndicatorConfig[] = [];
			const subInds: IndicatorConfig[] = [];

			for (const [name, history] of Object.entries(indicatorData.indicators)) {
				const config = indicatorData.config[name];
				const panel = resolveIndicatorPanel(name, config);
				const color = config?.color || getIndicatorColor(name);
				const isVisible = indicatorVisibility[name] ?? true;
				if (!isChartRenderableIndicator(name, config)) {
					continue;
				}

				const indicatorForChart: IndicatorConfig = {
					id: name,
					name: name,
					params: {},
					color: color,
					panel: panel === 'main' ? 'main' : 'sub1',
					visible: isVisible,
					data: history
						.filter(p => p.value !== null && p.value !== undefined)
						.map(p => ({
							timestamp: p.timestamp,
							value: p.value as number
						}))
				};

				if (panel === 'main') {
					mainInds.push(indicatorForChart);
				} else if (panel === 'sub') {
					subInds.push(indicatorForChart);
				}
			}

			mainIndicators = mainInds;
			subIndicators = subInds;
		} else {
			console.warn('Failed to load indicators:', indicatorResult.reason);
		}

		if (markerResult.status === 'fulfilled') {
			applyTradeMarkers(markerResult.value);
		} else {
			console.warn('Failed to load trade markers:', markerResult.reason);
		}
	}

	async function loadIndicatorsAndMarkersIfNeeded(sessionId: string, cursor: number | null) {
		if (cursor === null) return;
		if (lastIndicatorCursor !== null && cursor - lastIndicatorCursor < 10) {
			return;
		}
		lastIndicatorCursor = cursor;
		await loadIndicatorsAndMarkers(sessionId);
	}

	// Parity overhaul: one bundle call returns bars + REAL registry indicators +
	// full-history triggers + actual trade markers — all matching what paper trades.
	async function loadChartBundle(sessionId: string) {
		const chartTimeframe = activeVisualChartTimeframe;
		let bundle;
		try {
			bundle = await getPaperSessionChart(sessionId, { limit: 2000, timeframe: chartTimeframe });
		} catch (e) {
			console.error('Failed to load chart bundle:', e);
			return;
		}
		if (selectedSession?.id !== sessionId) return;

		chartBars = [...(bundle.bars ?? [])];
		// The bundle's bars are closed-bar data up to ~15s stale — immediately re-apply
		// the freshest WS tick so the reload never clobbers the forming live candle.
		if (selectedSession && selectedSession.mode !== 'replay' && chartBars.length > 0) {
			const livePrice = resolveLivePriceForSymbol(selectedSession.symbol, latestLivePrices);
			if (livePrice !== null) {
				chartBars = applyTickToBars(chartBars, livePrice, Date.now(), chartTimeframe);
			}
		}
		applyLatestRealtimeSnapshot();
		loadingBars = false;

		const toConfig = (s: { name: string; panel: string; color?: string; data: Array<{ timestamp: string; value: number | null }> }): IndicatorConfig => ({
			id: s.name,
			name: s.name,
			params: {},
			color: s.color || getIndicatorColor(s.name),
			panel: s.panel === 'main' ? 'main' : 'sub1',
			visible: indicatorVisibility[s.name] ?? true,
			data: s.data
				.filter((p) => p.value !== null && p.value !== undefined)
				.map((p) => ({ timestamp: p.timestamp, value: p.value as number })),
		});
		mainIndicators = (bundle.main_indicators ?? []).map(toConfig);
		subIndicators = (bundle.sub_indicators ?? []).map(toConfig);
		for (const s of [...(bundle.main_indicators ?? []), ...(bundle.sub_indicators ?? [])]) {
			if (!(s.name in indicatorVisibility)) indicatorVisibility[s.name] = true;
		}
		indicatorVisibility = indicatorVisibility;

		applyTradeMarkers({ entries: bundle.entry_markers ?? [], exits: bundle.exit_markers ?? [], blocked: [] });
		triggerMarkers = [
			...(bundle.trigger_entries ?? []).map((m) => toSignalMarker(m, 'entry')),
			...(bundle.trigger_exits ?? []).map((m) => toSignalMarker(m, 'exit')),
		];

		// Active order lines for the open position: a solid blue ENTRY line drawn for
		// the whole hold, plus DASHED red SL / green TP / amber Trail lines (the
		// TradingView-standard look for active orders). Prefer the backend's
		// self-describing levels; fall back to the open position's entry price.
		const levels = bundle.active_levels ?? { stop: [], take_profit: [], trail: [], entry: [] };
		const lines: typeof positionPriceLines = [];
		const entryLevel = (levels.entry ?? [])[0];
		const fallbackEntry = Number(selectedSession?.position?.entry_price);
		const entryPrice = entryLevel?.price ?? (Number.isFinite(fallbackEntry) ? fallbackEntry : undefined);
		if (entryPrice !== undefined && (entryLevel || selectedSession?.position)) {
			lines.push({ id: 'entry', price: entryPrice, color: '#3b82f6', title: 'ENTRY', dashed: false });
		}
		for (const s of levels.stop ?? []) lines.push({ id: 'sl', price: s.price, color: '#ef4444', title: 'SL', dashed: true });
		for (const t of levels.take_profit ?? []) lines.push({ id: 'tp', price: t.price, color: '#22c55e', title: 'TP', dashed: true });
		for (const tr of levels.trail ?? []) lines.push({ id: 'trail', price: tr.price, color: '#f59e0b', title: 'Trail', dashed: true });
		positionPriceLines = lines;
	}

	function toggleIndicatorVisibility(name: string) {
		if (!isChartRenderableIndicator(name, indicatorConfig[name])) return;
		indicatorVisibility[name] = !indicatorVisibility[name];
		indicatorVisibility = indicatorVisibility;

		mainIndicators = mainIndicators.map(ind => ({
			...ind,
			visible: ind.name === name ? indicatorVisibility[name] : ind.visible
		}));
		subIndicators = subIndicators.map(ind => ({
			...ind,
			visible: ind.name === name ? indicatorVisibility[name] : ind.visible
		}));
	}

	function getCurrentIndicatorValue(name: string): number | null {
		const history = sessionIndicatorHistory[name];
		if (history && history.length > 0) {
			for (let idx = history.length - 1; idx >= 0; idx -= 1) {
				const value = history[idx]?.value;
				if (typeof value === 'number' && Number.isFinite(value)) {
					return value;
				}
			}
		}
		const mainInd = mainIndicators.find(i => i.name === name);
		if (mainInd?.data && mainInd.data.length > 0) {
			return mainInd.data[mainInd.data.length - 1].value;
		}
		const subInd = subIndicators.find(i => i.name === name);
		if (subInd?.data && subInd.data.length > 0) {
			return subInd.data[subInd.data.length - 1].value;
		}
		return null;
	}

	function formatIndicatorValue(value: number | null, name: string): string {
		if (value === null) return '--';
		if (name.includes('RSI') || name.includes('Williams') || name.includes('ADX') || name.includes('CCI')) {
			return value.toFixed(1);
		}
		if (value > 1000) {
			return value.toFixed(2);
		}
		if (Math.abs(value) < 1) {
			return value.toFixed(4);
		}
		return value.toFixed(2);
	}

	function getIndicatorColor(name: string): string {
		const lower = name.trim().toLowerCase();
		const explicitColors: Record<string, string> = {
			price: '#94A3B8',
			close: '#94A3B8',
			rsi: '#8B5CF6',
			prev_rsi: '#A78BFA',
			macd: '#38BDF8',
			macd_signal: '#F59E0B',
			adx: '#22D3EE',
			atr: '#FB7185',
			atr_14: '#FB7185',
			ema_fast: '#22C55E',
			ema_slow: '#C084FC',
			ema_regime: '#60A5FA',
			entry_signal: '#22C55E',
			exit_signal: '#EF4444',
		};
		if (explicitColors[lower]) {
			return explicitColors[lower];
		}
		if (lower.startsWith('atr')) {
			return '#FB7185';
		}
		if (lower.startsWith('rsi')) {
			return '#8B5CF6';
		}
		if (lower.startsWith('macd_signal')) {
			return '#F59E0B';
		}
		if (lower.startsWith('macd')) {
			return '#38BDF8';
		}
		if (lower.startsWith('adx')) {
			return '#22D3EE';
		}
		const palette = lower.includes('ema')
			? ['#22C55E', '#60A5FA', '#F59E0B', '#C084FC', '#F97316']
			: (['rsi', 'macd', 'adx', 'atr', 'cci', 'williams', 'stoch', 'mfi', 'roc', 'mom'].some((token) => lower.includes(token))
				? ['#8B5CF6', '#38BDF8', '#F59E0B', '#22D3EE', '#FB7185', '#F97316']
				: ['#E5E7EB', '#22C55E', '#60A5FA', '#F59E0B', '#C084FC', '#FB7185']);
		const stableIdx = [...lower].reduce((acc, ch) => acc + ch.charCodeAt(0), 0) % palette.length;
		return palette[stableIdx];
	}

	function inferIndicatorPanel(name: string): 'main' | 'sub' | 'none' {
		const key = name.toUpperCase();
		if (key === 'PRICE' || key === 'ENTRY_SIGNAL' || key === 'EXIT_SIGNAL') {
			return 'none';
		}
		if (key.includes('SIGNAL') || key.includes('UPTREND') || key.includes('DOWNTREND') || key.includes('FLAG') || key.includes('STATE')) {
			return 'none';
		}
		const subPanelIndicators = ['RSI', 'STOCH', 'MFI', 'WILLR', 'WILLIAMS', 'CCI', 'ADX', 'MACD', 'MOM', 'ROC'];
		const mainPanelIndicators = ['EMA', 'SMA', 'WMA', 'VWAP', 'BB', 'BOLLINGER', 'DONCHIAN', 'DC_', 'KELTNER', 'SUPER'];
		if (subPanelIndicators.some(token => key.includes(token))) {
			return 'sub';
		}
		if (mainPanelIndicators.some(token => key.includes(token))) {
			return 'main';
		}
		return 'none';
	}

	function resolveIndicatorPanel(name: string, config?: SessionIndicatorConfig): SessionIndicatorConfig['panel'] {
		return config?.panel ?? inferIndicatorPanel(name);
	}

	function isChartRenderableIndicator(name: string, config?: SessionIndicatorConfig): boolean {
		const panel = resolveIndicatorPanel(name, config);
		return panel === 'main' || panel === 'sub';
	}

	function getIndicatorSidebarGroup(name: string, config?: SessionIndicatorConfig): 'overlays' | 'lower' | 'sidebar' {
		const panel = resolveIndicatorPanel(name, config);
		if (panel === 'main') return 'overlays';
		if (panel === 'sub') return 'lower';
		return 'sidebar';
	}

	function getIndicatorNamesForGroup(group: 'overlays' | 'lower' | 'sidebar'): string[] {
		return Object.keys(indicatorConfig).filter((name) => getIndicatorSidebarGroup(name, indicatorConfig[name]) === group);
	}

	function resetChartView(): void {
		fitContentToken += 1;
	}

	async function toggleVisualReplay() {
		showVisualReplay = !showVisualReplay;
		if (showVisualReplay && selectedSession) {
			await hydrateVisualReplayForSession(selectedSession);
		} else {
			stopLiveChartPolling();
		}
	}

	async function handleReplayStep(count: number = 1) {
		void count;
		showUnsupportedPaperControlMessage();
	}

	async function handleReplaySeek(index: number) {
		void index;
		showUnsupportedPaperControlMessage();
	}

	async function handleReplayPlay() {
		showUnsupportedPaperControlMessage();
	}

	async function handleReplayPause() {
		showUnsupportedPaperControlMessage();
	}

	async function handleReplayReset() {
		showUnsupportedPaperControlMessage();
	}

	async function handleSpeedChange(speed: number) {
		replaySpeedInput = speed;
		showUnsupportedPaperControlMessage();
	}

	function handleSeekSlider(event: Event) {
		const target = event.target as HTMLInputElement;
		const index = parseInt(target.value, 10);
		handleReplaySeek(index);
	}

	function normalizeChartTimeframe(value: string | null | undefined): string {
		const raw = String(value || '').trim().toLowerCase();
		return raw || '1h';
	}

	function buildChartDrawingId(prefix: string): string {
		return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
	}

	function timeframeButtonClass(timeframe: string, activeTimeframe: string): string {
		const base = 'rounded-sm border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide transition-colors disabled:cursor-not-allowed disabled:opacity-50';
		return timeframe === activeTimeframe
			? `${base} border-emerald-700 bg-emerald-950/40 text-emerald-200`
			: `${base} border-[#232323] bg-[#080808] text-gray-400 hover:border-[#353535] hover:text-gray-200`;
	}

	function chartToolButtonClass(active = false): string {
		const base = 'rounded-sm border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide transition-colors disabled:cursor-not-allowed disabled:opacity-50';
		return active
			? `${base} border-amber-600 bg-amber-950/40 text-amber-200`
			: `${base} border-[#232323] bg-[#080808] text-gray-400 hover:border-[#353535] hover:text-gray-200`;
	}

	function clearChartDrawings(): void {
		chartDrawings = [];
		pendingTrendLineStart = null;
		activeDrawingTool = 'cursor';
	}

	function toggleDrawingTool(tool: Exclude<ChartDrawingTool, 'cursor'>): void {
		if (activeDrawingTool === tool) {
			activeDrawingTool = 'cursor';
			pendingTrendLineStart = null;
			return;
		}
		activeDrawingTool = tool;
		pendingTrendLineStart = null;
	}

	function handleChartDrawingPoint(event: CustomEvent<ChartDrawingPoint>): void {
		if (activeDrawingTool === 'cursor') return;
		const point = event.detail;
		if (activeDrawingTool === 'horizontalLine') {
			chartDrawings = [
				...chartDrawings,
				{
					id: buildChartDrawingId('hline'),
					type: 'horizontalLine',
					price: point.price,
					color: '#f59e0b',
					label: formatPrice(point.price).replace('$', ''),
				},
			];
			return;
		}

		if (!pendingTrendLineStart) {
			pendingTrendLineStart = point;
			return;
		}

		chartDrawings = [
			...chartDrawings,
			{
				id: buildChartDrawingId('trend'),
				type: 'trendLine',
				start: pendingTrendLineStart,
				end: point,
				color: '#38bdf8',
			},
		];
		pendingTrendLineStart = null;
	}

	function chartToolHint(): string {
		if (activeDrawingTool === 'horizontalLine') {
			return 'Horizontal line mode: click the chart to place a level.';
		}
		if (activeDrawingTool === 'trendLine') {
			return pendingTrendLineStart
				? 'Trend line mode: click a second point to finish the line.'
				: 'Trend line mode: click a first point to start the line.';
		}
		return 'Cursor mode: inspect candles without chart overlays covering controls.';
	}

	async function setVisualChartTimeframe(timeframe: string): Promise<void> {
		if (!selectedSession || selectedSession.mode === 'replay') return;
		const normalized = normalizeChartTimeframe(timeframe);
		const sessionTimeframe = normalizeChartTimeframe(selectedSession.timeframe);
		preferredChartTimeframe = normalized === sessionTimeframe ? '' : normalized;
		clearChartDrawings();
		chartBars = [];
		loadingBars = true;
		resetChartView();
		if (showVisualReplay) {
			await loadLiveChart();
		}
	}

	function formatPrice(price: number | null | undefined): string {
		if (typeof price !== 'number' || !Number.isFinite(price)) return '--';
		if (price >= 1000) {
			return '$' + price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
		}
		return '$' + price.toFixed(8).replace(/\.?0+$/, '');
	}

	function formatPercent(value: number | null | undefined): string {
		if (typeof value !== 'number' || !Number.isFinite(value)) return '--';
		return (value >= 0 ? '+' : '') + value.toFixed(2) + '%';
	}

	function formatDollarPnl(value: number | null | undefined): string {
		if (typeof value !== 'number' || !Number.isFinite(value)) return '--';
		const abs = Math.abs(value);
		return `${value >= 0 ? '+' : '-'}$${abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
	}

	function formatRatio(value: number | null | undefined): string {
		if (typeof value !== 'number' || !Number.isFinite(value)) return '--';
		return value.toFixed(2);
	}

	function formatDateTime(dateStr: string | null | undefined): string {
		if (!dateStr) return '--';
		const date = new Date(dateStr);
		if (Number.isNaN(date.getTime())) return '--';
		return date.toLocaleDateString() + ' ' + date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
	}

	function getPnlTone(value: number | null | undefined): string {
		if (typeof value !== 'number' || !Number.isFinite(value)) return 'text-gray-500';
		if (value > 0) return 'text-green-400';
		if (value < 0) return 'text-red-400';
		return 'text-gray-400';
	}

	function normalizeParamKey(key: string): string {
		return key.toLowerCase().replace(/[\s.-]+/g, '_');
	}

	function toNumeric(value: unknown): number | null {
		if (typeof value === 'number' && Number.isFinite(value)) return value;
		if (typeof value === 'string' && value.trim().length > 0) {
			const parsed = Number(value);
			return Number.isFinite(parsed) ? parsed : null;
		}
		return null;
	}

	function findNumericParam(
		params: Record<string, unknown> | undefined,
		aliases: string[],
	): { key: string; normalized: string; value: number } | null {
		if (!params) return null;
		for (const [rawKey, rawValue] of Object.entries(params)) {
			const normalized = normalizeParamKey(rawKey);
			const matched = aliases.some((alias) => normalized === alias || normalized.endsWith(`_${alias}`));
			if (!matched) continue;
			const numeric = toNumeric(rawValue);
			if (numeric === null) continue;
			return { key: rawKey, normalized, value: numeric };
		}
		return null;
	}

	function derivePriceLevel(
		entryPrice: number,
		source: { normalized: string; value: number } | null,
		direction: 'down' | 'up',
	): number | null {
		if (!source) return null;
		const key = source.normalized;
		const value = source.value;

		if (key.includes('price') || key.includes('level') || key.endsWith('_value')) {
			return value;
		}
		if (key.includes('pct') || key.includes('percent')) {
			const delta = value / 100;
			return direction === 'down'
				? entryPrice * (1 - delta)
				: entryPrice * (1 + delta);
		}
		if (key.includes('bps')) {
			const delta = value / 10_000;
			return direction === 'down'
				? entryPrice * (1 - delta)
				: entryPrice * (1 + delta);
		}
		return null;
	}

	function formatRawParam(source: { normalized: string; value: number } | null): string {
		if (!source) return '\u2014';
		if (source.normalized.includes('pct') || source.normalized.includes('percent')) {
			return `${source.value}%`;
		}
		if (source.normalized.includes('bps')) {
			return `${source.value} bps`;
		}
		return String(source.value);
	}

	function humanizeLabel(value: string | null | undefined): string {
		const text = String(value || '')
			.replaceAll('_', ' ')
			.replaceAll('-', ' ')
			.trim();
		if (!text) return '\u2014';
		return text.charAt(0).toUpperCase() + text.slice(1);
	}

	function formatQty(value: number): string {
		if (!Number.isFinite(value)) return '\u2014';
		return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
	}

	function getOpenDuration(entryTime: string): string {
		const openedAt = new Date(entryTime);
		if (Number.isNaN(openedAt.getTime())) return '\u2014';
		const elapsedMs = Date.now() - openedAt.getTime();
		const totalMinutes = Math.max(0, Math.floor(elapsedMs / 60000));
		const hours = Math.floor(totalMinutes / 60);
		const minutes = totalMinutes % 60;
		if (hours <= 0) return `${minutes}m`;
		return `${hours}h ${minutes}m`;
	}

	function getExitSignalSummary(session: PaperTradingSession): string {
		const exitSignal = session.pending_signals.find((signal) => signal.signal_type === 'exit');
		if (!exitSignal) return 'Strategy exit';
		return `${exitSignal.description} (${exitSignal.trigger_value.toFixed(2)})`;
	}

	function getPositionDetail(session: PaperTradingSession): {
		stopSource: { key: string; normalized: string; value: number } | null;
		takeSource: { key: string; normalized: string; value: number } | null;
		stopPrice: number | null;
		takePrice: number | null;
		stopLabel: string;
		takeLabel: string;
		notional: number;
		exitSummary: string;
	} | null {
		if (!session.position) return null;

		const stopSource = findNumericParam(session.params, [
			'stop_loss_price', 'stop_price', 'sl_price',
			'stop_loss_pct', 'stop_loss_percent', 'stop_loss_bps',
			'sl_pct', 'sl_bps', 'stop_loss', 'stoploss', 'sl',
		]);
		const takeSource = findNumericParam(session.params, [
			'take_profit_price', 'tp_price', 'target_price',
			'take_profit_pct', 'take_profit_percent', 'take_profit_bps',
			'tp_pct', 'tp_bps', 'take_profit', 'takeprofit', 'tp',
		]);

		const positionStopPrice = toNumeric(session.position.stop_loss_price);
		const positionTakePrice = toNumeric(session.position.take_profit_price);
		const positionStopSource = String(session.position.stop_loss_source ?? '').trim();
		const positionTakeSource = String(session.position.take_profit_source ?? '').trim();

		const stopPrice = positionStopPrice ?? derivePriceLevel(session.position.entry_price, stopSource, 'down');
		const takePrice = positionTakePrice ?? derivePriceLevel(session.position.entry_price, takeSource, 'up');

		return {
			stopSource,
			takeSource,
			stopPrice,
			takePrice,
			stopLabel: positionStopSource ? humanizeLabel(positionStopSource) : formatRawParam(stopSource),
			takeLabel: positionTakeSource ? humanizeLabel(positionTakeSource) : formatRawParam(takeSource),
			notional: session.position.size * session.current_price,
			exitSummary: getExitSignalSummary(session),
		};
	}

	function hasIncompleteTradeClose(trade: PaperTrade): boolean {
		if (trade.close_incomplete) return true;
		const hasExitTime = Boolean(String(trade.exit_time ?? '').trim());
		const hasExitPrice = typeof trade.exit_price === 'number' && Number.isFinite(trade.exit_price);
		const hasPnl = typeof trade.pnl === 'number' && Number.isFinite(trade.pnl);
		const hasNetPnl = typeof trade.net_pnl === 'number' && Number.isFinite(trade.net_pnl);
		return hasExitTime && !hasExitPrice && !hasPnl && !hasNetPnl;
	}

	function getTradeCloseBadge(trade: PaperTrade): { label: string; tone: string; title: string } | null {
		const reason = String(trade.close_reason ?? '').trim();
		if (hasIncompleteTradeClose(trade)) {
			return {
				label: 'Unknown close',
				tone: 'border-yellow-700/60 bg-yellow-900/20 text-yellow-300',
				title: reason ? humanizeLabel(reason) : 'Closed without a reliable exit price',
			};
		}
		if (!reason) return null;
		if (reason.includes('reconcile') || reason.includes('stale_missing_on_exchange')) {
			return {
				label: 'Reconciled',
				tone: 'border-amber-700/60 bg-amber-900/20 text-amber-300',
				title: humanizeLabel(reason),
			};
		}
		return null;
	}

	function getSessionChartParams(session: PaperTradingSession): Record<string, unknown> {
		const explicit = getSessionDecisionParams(session);
		if (Object.keys(explicit).length > 0) {
			return explicit;
		}

		const indicatorNames = Object.keys(session.indicators || {});
		if (indicatorNames.length > 0) {
			return { indicator_signature: indicatorNames.join(' | ') };
		}

		return { strategy_expression: session.strategy_name };
	}

	function getSessionDecisionParams(session: PaperTradingSession): Record<string, unknown> {
		const decisionParams = session.decision_params && typeof session.decision_params === 'object'
			? { ...session.decision_params }
			: {};
		if (Object.keys(decisionParams).length > 0) return decisionParams;
		return session.params && typeof session.params === 'object' ? { ...session.params } : {};
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'watching': return 'text-yellow-400';
			case 'gated': return 'text-yellow-400';
			case 'blocked': return 'text-red-400';
			case 'warming_up': return 'text-yellow-400';
			case 'position_open': return 'text-green-400';
			case 'replay_finished': return 'text-gray-400';
			case 'stopped': return 'text-gray-400';
			default: return 'text-gray-400';
		}
	}

	function getPositionSide(side: string | null | undefined): 'long' | 'short' | null {
		const normalized = String(side || '').trim().toLowerCase();
		if (normalized === 'long' || normalized === 'short') return normalized;
		return null;
	}

	function getPositionSideColor(side: string | null | undefined): string {
		const normalized = getPositionSide(side);
		if (normalized === 'short') return 'text-red-400';
		if (normalized === 'long') return 'text-green-400';
		return 'text-gray-400';
	}

	function getSessionStatusLabel(session: PaperTradingSession): string {
		const positionSide = getPositionSide(session.position?.side);
		if (positionSide) return positionSide;
		if (session.status === 'gated') return 'gated';
		if (session.status === 'blocked') return 'blocked';
		return session.status.replaceAll('_', ' ');
	}

	function getSessionStatusColor(session: PaperTradingSession): string {
		const positionSide = getPositionSide(session.position?.side);
		if (positionSide) return getPositionSideColor(positionSide);
		return getStatusColor(session.status);
	}

	function getSignalIcon(signalType: string): string {
		switch (signalType) {
			case 'entry': return 'text-green-400';
			case 'exit': return 'text-red-400';
			default: return 'text-gray-400';
		}
	}

</script>

<!-- Top action bar -->
<div class="h-10 flex items-center border-b border-[#222] bg-[#0a0a0a] px-4 flex-shrink-0">
	<div class="ml-auto flex items-center gap-3">
		{#if error}
			<span class="text-red-500 text-xs">{error}</span>
			<button class="text-red-500 hover:text-red-300 text-xs" on:click={() => error = null}>dismiss</button>
		{/if}
	</div>
</div>

<!-- New Session Modal -->
{#if showNewSession}
	<div class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
		<div class="bg-[#050505] border border-[#222] p-6 w-full max-w-md">
			<h2 class="text-sm font-bold text-white uppercase tracking-wider mb-4">Create Paper Trading Session</h2>

			<div class="space-y-4">
				<div>
					<label for="strategy" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Strategy</label>
					<select
						id="strategy"
						bind:value={newSessionStrategy}
						class="terminal-select"
					>
						{#each strategies as s}
							<option value={s.name}>{s.name}</option>
						{/each}
					</select>
				</div>

				<div>
					<label for="symbol" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Symbol</label>
					<select
						id="symbol"
						bind:value={newSessionSymbol}
						class="terminal-select"
					>
						{#each datasets as ds}
							<option value={ds.symbol}>{ds.symbol}</option>
						{/each}
						<option value="BTC/USDT">BTC/USDT (Live)</option>
						<option value="ETH/USDT">ETH/USDT (Live)</option>
						{#each paperStockSymbols as stock}
							<option value={stock}>{stock} (IBKR Stock)</option>
						{/each}
					</select>
				</div>

				<div>
					<label for="timeframe" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Timeframe</label>
					<select
						id="timeframe"
						bind:value={newSessionTimeframe}
						class="terminal-select"
					>
						{#each timeframes as tf}
							<option value={tf}>{tf}</option>
						{/each}
					</select>
				</div>

				<div>
					<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-2">Trading Mode</div>
					<div class="space-y-2">
						<label class="flex items-start gap-3 p-3 border cursor-pointer transition-colors {newSessionMode === 'replay' ? 'border-white bg-[#111]' : 'border-[#222] hover:border-[#333]'}">
							<input
								type="radio"
								bind:group={newSessionMode}
								value="replay"
								class="mt-1 accent-white"
							/>
							<div>
								<span class="text-white text-sm font-bold">Historic Replay</span>
								<p class="text-xs text-gray-500 mt-0.5">Scroll through stored data, test strategies on past price action</p>
							</div>
						</label>
						<label class="flex items-start gap-3 p-3 border cursor-pointer transition-colors {newSessionMode === 'live' ? 'border-white bg-[#111]' : 'border-[#222] hover:border-[#333]'}">
							<input
								type="radio"
								bind:group={newSessionMode}
								value="live"
								class="mt-1 accent-white"
							/>
							<div>
								<span class="text-white text-sm font-bold">Live Paper Trading</span>
								<p class="text-xs text-gray-500 mt-0.5">Real-time prices, simulated order execution</p>
							</div>
						</label>
					</div>
					{#if newSessionMode === 'replay' && datasets.length === 0}
						<p class="text-xs text-red-400 mt-2">No stored datasets available for replay.</p>
					{/if}
				</div>

				{#if newSessionMode === 'live'}
					<div class="border border-[#222] p-3 space-y-3">
						<div>
							<label for="new-live-feed" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Live Feed</label>
							<select id="new-live-feed" bind:value={newSessionLiveFeed} class="terminal-select">
								<option value="default">Default (App Exchange)</option>
								<option value="ibkr">IBKR (TWS / Gateway)</option>
							</select>
						</div>
						{#if newSessionLiveFeed === 'ibkr'}
							<div class="grid grid-cols-2 gap-3">
								<div>
									<label for="new-ibkr-sec-type" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Sec Type</label>
									<select id="new-ibkr-sec-type" bind:value={newSessionIBKRSecType} class="terminal-select">
										{#each ibkrSecTypes as secType}
											<option value={secType}>{secType}</option>
										{/each}
									</select>
								</div>
								<div>
									<label for="new-ibkr-what" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Bars</label>
									<select id="new-ibkr-what" bind:value={newSessionIBKRWhatToShow} class="terminal-select">
										{#each ibkrWhatToShowOptions as option}
											<option value={option}>{option}</option>
										{/each}
									</select>
								</div>
								<div>
									<label for="new-ibkr-exchange" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Exchange</label>
									<input id="new-ibkr-exchange" type="text" bind:value={newSessionIBKRExchange} class="terminal-input" />
								</div>
								<div>
									<label for="new-ibkr-currency" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Currency</label>
									<input id="new-ibkr-currency" type="text" bind:value={newSessionIBKRCurrency} class="terminal-input" />
								</div>
							</div>
							<p class="text-[10px] text-gray-600">Typical US stock setup: STK + SMART + USD.</p>
						{/if}
					</div>
				{/if}

				{#if newSessionMode === 'replay'}
					<div>
						<label for="replay-start" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay Start (optional)</label>
						<input
							id="replay-start"
							type="datetime-local"
							bind:value={newSessionReplayStart}
							class="terminal-input"
						/>
					</div>

					<div>
						<label for="replay-end" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay End (optional)</label>
						<input
							id="replay-end"
							type="datetime-local"
							bind:value={newSessionReplayEnd}
							class="terminal-input"
						/>
					</div>

					<div>
						<label for="replay-speed" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay Speed (bars/sec)</label>
						<input
							id="replay-speed"
							type="number"
							min="0.1"
							step="0.1"
							bind:value={newSessionReplaySpeed}
							class="terminal-input"
						/>
					</div>
				{/if}

				<div>
					<label for="capital" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Initial Capital ($)</label>
					<input
						id="capital"
						type="number"
						bind:value={newSessionCapital}
						min="100"
						step="100"
						class="terminal-input"
					/>
				</div>
			</div>

			<div class="flex justify-end gap-3 mt-6">
				<button
					class="terminal-button"
					on:click={() => showNewSession = false}
				>
					Cancel
				</button>
				<button
					class="terminal-button-primary"
					disabled={creating || !newSessionStrategy || (newSessionMode === 'replay' && datasets.length === 0)}
					on:click={handleCreateSession}
				>
					{creating ? 'Creating...' : 'Create'}
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- Edit Session Modal -->
{#if showEditSession}
	<div class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
		<div class="bg-[#050505] border border-[#222] p-6 w-full max-w-md">
			<h2 class="text-sm font-bold text-white uppercase tracking-wider mb-4">Edit Paper Trading Session</h2>

			<div class="space-y-4">
				<div>
					<label for="edit-strategy" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Strategy</label>
					<select
						id="edit-strategy"
						bind:value={editSessionStrategy}
						class="terminal-select"
					>
						{#each strategies as s}
							<option value={s.name}>{s.name}</option>
						{/each}
					</select>
				</div>

				<div>
					<label for="edit-symbol" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Symbol</label>
					<select
						id="edit-symbol"
						bind:value={editSessionSymbol}
						class="terminal-select"
					>
						{#each datasets as ds}
							<option value={ds.symbol}>{ds.symbol}</option>
						{/each}
						<option value="BTC/USDT">BTC/USDT (Live)</option>
						<option value="ETH/USDT">ETH/USDT (Live)</option>
						{#each paperStockSymbols as stock}
							<option value={stock}>{stock} (IBKR Stock)</option>
						{/each}
					</select>
				</div>

				<div>
					<label for="edit-timeframe" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Timeframe</label>
					<select
						id="edit-timeframe"
						bind:value={editSessionTimeframe}
						class="terminal-select"
					>
						{#each timeframes as tf}
							<option value={tf}>{tf}</option>
						{/each}
					</select>
				</div>

				<div>
					<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-2">Trading Mode</div>
					<div class="space-y-2">
						<label class="flex items-start gap-3 p-3 border cursor-pointer transition-colors {editSessionMode === 'replay' ? 'border-white bg-[#111]' : 'border-[#222] hover:border-[#333]'}">
							<input
								type="radio"
								bind:group={editSessionMode}
								value="replay"
								class="mt-1 accent-white"
							/>
							<div>
								<span class="text-white text-sm font-bold">Historic Replay</span>
								<p class="text-xs text-gray-500 mt-0.5">Replay stored market data for strategy tuning</p>
							</div>
						</label>
						<label class="flex items-start gap-3 p-3 border cursor-pointer transition-colors {editSessionMode === 'live' ? 'border-white bg-[#111]' : 'border-[#222] hover:border-[#333]'}">
							<input
								type="radio"
								bind:group={editSessionMode}
								value="live"
								class="mt-1 accent-white"
							/>
							<div>
								<span class="text-white text-sm font-bold">Live Paper Trading</span>
								<p class="text-xs text-gray-500 mt-0.5">Use real-time feed for paper execution</p>
							</div>
						</label>
					</div>
					{#if editSessionMode === 'replay' && datasets.length === 0}
						<p class="text-xs text-red-400 mt-2">No stored datasets available for replay.</p>
					{/if}
				</div>

				{#if editSessionMode === 'live'}
					<div class="border border-[#222] p-3 space-y-3">
						<div>
							<label for="edit-live-feed" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Live Feed</label>
							<select id="edit-live-feed" bind:value={editSessionLiveFeed} class="terminal-select">
								<option value="default">Default (App Exchange)</option>
								<option value="ibkr">IBKR (TWS / Gateway)</option>
							</select>
						</div>
						{#if editSessionLiveFeed === 'ibkr'}
							<div class="grid grid-cols-2 gap-3">
								<div>
									<label for="edit-ibkr-sec-type" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Sec Type</label>
									<select id="edit-ibkr-sec-type" bind:value={editSessionIBKRSecType} class="terminal-select">
										{#each ibkrSecTypes as secType}
											<option value={secType}>{secType}</option>
										{/each}
									</select>
								</div>
								<div>
									<label for="edit-ibkr-what" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Bars</label>
									<select id="edit-ibkr-what" bind:value={editSessionIBKRWhatToShow} class="terminal-select">
										{#each ibkrWhatToShowOptions as option}
											<option value={option}>{option}</option>
										{/each}
									</select>
								</div>
								<div>
									<label for="edit-ibkr-exchange" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Exchange</label>
									<input id="edit-ibkr-exchange" type="text" bind:value={editSessionIBKRExchange} class="terminal-input" />
								</div>
								<div>
									<label for="edit-ibkr-currency" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Currency</label>
									<input id="edit-ibkr-currency" type="text" bind:value={editSessionIBKRCurrency} class="terminal-input" />
								</div>
							</div>
							<p class="text-[10px] text-gray-600">Typical US stock setup: STK + SMART + USD.</p>
						{/if}
					</div>
				{/if}

				{#if editSessionMode === 'replay'}
					<div>
						<label for="edit-replay-start" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay Start (optional)</label>
						<input
							id="edit-replay-start"
							type="datetime-local"
							bind:value={editSessionReplayStart}
							class="terminal-input"
						/>
					</div>

					<div>
						<label for="edit-replay-end" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay End (optional)</label>
						<input
							id="edit-replay-end"
							type="datetime-local"
							bind:value={editSessionReplayEnd}
							class="terminal-input"
						/>
					</div>

					<div>
						<label for="edit-replay-speed" class="block text-[10px] uppercase tracking-wider text-gray-500 mb-1">Replay Speed (bars/sec)</label>
						<input
							id="edit-replay-speed"
							type="number"
							min="0.1"
							step="0.1"
							bind:value={editSessionReplaySpeed}
							class="terminal-input"
						/>
					</div>
				{/if}
			</div>

			<!-- Strategy Parameters -->
			{#if strategies.some((s) => s.name === editSessionStrategy && Object.keys(s.parameters).length > 0)}
				<div class="border-t border-[#333] pt-4 mt-4">
					<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-3">Strategy Parameters</div>
					<div class="space-y-3">
						{#each Object.entries(strategies.find((s) => s.name === editSessionStrategy)?.parameters ?? {}) as [paramName, spec]}
							{@const paramId = `edit_param_${paramName}`}
							<div>
								<div class="flex justify-between mb-1">
									<label for={paramId} class="text-[10px] text-gray-400">{paramName}</label>
									{#if spec.min !== undefined && spec.max !== undefined}
										<span class="text-[10px] text-gray-600">[{spec.min}-{spec.max}]</span>
									{/if}
								</div>
								{#if spec.type === 'bool'}
									<div class="flex items-center">
										<input
											id={paramId}
											type="checkbox"
											checked={!!editSessionParams[paramName]}
											on:change={(e) => editSessionParams[paramName] = e.currentTarget.checked}
											class="mr-2"
										/>
										<span class="text-xs text-white">{editSessionParams[paramName] ? 'True' : 'False'}</span>
									</div>
								{:else if spec.type === 'select' && spec.options}
									<select
										id={paramId}
										value={editSessionParams[paramName] ?? spec.default}
										on:change={(e) => editSessionParams[paramName] = e.currentTarget.value}
										class="terminal-input"
									>
										{#each spec.options as opt}
											<option value={opt}>{opt}</option>
										{/each}
									</select>
								{:else}
									<input
										id={paramId}
										type="number"
										value={editSessionParams[paramName] ?? spec.default}
										on:change={(e) => editSessionParams[paramName] = spec.type === 'int' ? parseInt(e.currentTarget.value) : parseFloat(e.currentTarget.value)}
										class="terminal-input"
										step={spec.type === 'int' ? 1 : 0.01}
										min={spec.min}
										max={spec.max}
									/>
								{/if}
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<div class="flex justify-end gap-3 mt-6">
				<button
					class="terminal-button"
					on:click={() => showEditSession = false}
				>
					Cancel
				</button>
				<button
					class="terminal-button-primary"
					disabled={editing || !editSessionStrategy || (editSessionMode === 'replay' && datasets.length === 0)}
					on:click={handleUpdateSession}
				>
					{editing ? 'Saving...' : 'Save Changes'}
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- Main Content Area -->
	<div class="flex-1 flex overflow-hidden">
		<!-- Left: Sessions Panel -->
		<div class="w-72 border-r border-[#222] bg-[#050505] flex flex-col flex-shrink-0">
			<div class="panel-header">
				<span>{sessionListTitle}</span>
			</div>
			<div class="flex-1 overflow-y-auto">
				{#if loading}
					<div class="p-3"><Skeleton rows={6} /></div>
				{:else}
					{#if sessions.length === 0}
						<div class="px-3 py-8 text-center">
							<p class="text-gray-500 text-xs">{emptySessionTitle}</p>
							<p class="text-gray-600 text-xs mt-1">{emptySessionHint}</p>
						</div>
					{:else}
						{#each sortedSessions as session}
							<button
								class="terminal-list-item w-full text-left flex-col items-start gap-0 {selectedSession?.id === session.id ? 'active' : ''}"
								on:click={() => selectSession(session)}
							>
								<div class="flex justify-between items-center w-full">
									<span class="text-white text-xs font-bold truncate flex items-center gap-1">
										{#if session.gated_by_regime}
											<span class="text-yellow-500 cursor-help" title={session.gated_reason || "Strategy execution is currently gated by market regime"}>⚠️</span>
										{/if}
										<a
											href="/lab/strategy/{encodeURIComponent(session.strategy_name)}"
											class="hover:text-yellow-400 hover:underline transition-colors"
											on:click|stopPropagation
											title="Open strategy detail"
										>{session.strategy_name}</a>
									</span>
									<span class="text-[10px] uppercase {getSessionStatusColor(session)} flex-shrink-0">
										{getSessionStatusLabel(session)}
									</span>
								</div>
								<div class="text-[10px] text-gray-500 w-full truncate">
									{session.symbol} | {session.timeframe} | {session.mode}
								</div>
								<div class="text-[10px] w-full flex justify-between">
									{#if isDeployedCompatSession(session)}
										{#if balanceState(session) === 'real'}
											<span class="text-gray-600" title="Real Hyperliquid balance">{formatPrice(displayCapital(session))}</span>
										{:else}
											<span class="text-amber-500" title="Live balance not synced yet">— bal n/a</span>
										{/if}
									{:else}
										<span class="text-gray-600">{formatPrice(session.capital)}</span>
									{/if}
									{#if session.position}
										<span class="{session.position.unrealized_pnl > 0 ? 'text-green-400' : session.position.unrealized_pnl < 0 ? 'text-red-400' : 'text-gray-400'}">
											[{formatDollarPnl(session.position.unrealized_pnl)} {formatPercent(session.position.unrealized_pnl_pct)}]
										</span>
									{:else if session.total_trades > 0}
										<span class="{session.total_pnl > 0 ? 'text-green-400' : session.total_pnl < 0 ? 'text-red-400' : 'text-gray-400'}">
											{formatDollarPnl(session.total_pnl)} ({session.winning_trades}/{session.total_trades})
										</span>
									{/if}
								</div>
							</button>
						{/each}
					{/if}

					{#if !isLiveView}
						<div class="border-t border-[#222] mt-2">
							<div class="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-gray-500">Archived History</div>
							{#if archivedLoading}
								<div class="px-3 pb-3"><Skeleton rows={3} /></div>
							{:else if archivedStrategies.length === 0}
								<div class="px-3 pb-3 text-[11px] text-gray-600">No archived strategies yet.</div>
							{:else}
								{#each archivedStrategies as strategy}
									<button
										class="terminal-list-item w-full text-left flex-col items-start gap-0 {selectedArchivedStrategy?.id === strategy.id ? 'active' : ''}"
										on:click={() => selectArchivedSession(strategy)}
									>
										<div class="flex justify-between items-center w-full">
											<a
												href="/lab/strategy/{encodeURIComponent(strategy.id)}"
												class="text-white text-xs font-bold truncate hover:text-yellow-400 hover:underline transition-colors"
												on:click|stopPropagation
												title="Open strategy detail"
											>{strategy.display_id || strategy.name || strategy.id}</a>
											<span class="text-[10px] uppercase text-red-400 flex-shrink-0">
												{prettyLifecycleState(strategy.state)}
											</span>
										</div>
										<div class="text-[10px] text-gray-500 w-full truncate">
											{strategy.symbol || '--'} | {formatDateTime(strategy.updated_at)}
										</div>
										<div class="text-[10px] text-gray-600 w-full truncate" title={compactReason(strategy.blocked_reason)}>
											{compactReason(strategy.blocked_reason, 120)}
										</div>
									</button>
								{/each}
							{/if}
						</div>
					{/if}
				{/if}
			</div>
		</div>
	<!-- Right: Session Detail -->
	<div class="flex-1 bg-black overflow-hidden flex flex-col min-w-0">
		{#if selectedSession}
			<!-- Session Header Bar -->
			<div class="border-b border-[#222] bg-[#0a0a0a] px-4 py-2 flex-shrink-0">
				<div class="flex justify-between items-center">
						<div class="flex items-center gap-3 min-w-0">
							{#if selectedSession.gated_by_regime}
								<div class="bg-yellow-900/50 border border-yellow-700 text-yellow-400 text-[10px] px-2 py-0.5 rounded cursor-help" title={selectedSession.gated_reason || "Strategy execution is currently gated by market regime"}>
									⚠️ Gated by Regime
								</div>
							{:else if selectedSession.blocked_reason}
								<div class="bg-red-950/60 border border-red-800 text-red-400 text-[10px] px-2 py-0.5 rounded cursor-help" title={selectedSession.blocked_reason}>
									Blocked: {compactReason(selectedSession.blocked_reason, 60)}
								</div>
							{/if}
							<a
								href="/lab/strategy/{encodeURIComponent(selectedSession.strategy_name)}"
								class="text-sm font-bold text-white truncate hover:text-yellow-400 hover:underline transition-colors"
								title="Open strategy detail"
							>{selectedSession.strategy_name}</a>
							<span class="text-xs text-gray-500 flex-shrink-0">{selectedSession.symbol} | {selectedSession.timeframe}</span>
							<span class="text-[10px] uppercase font-bold flex-shrink-0 {getSessionStatusColor(selectedSession)}">
								{getSessionStatusLabel(selectedSession)}
							</span>
						</div>
						{#if allowsSessionEditing}
							<div class="flex gap-2 flex-shrink-0">
								{#if selectedSession.status === 'stopped' || selectedSession.status === 'replay_finished'}
									<button
										class="terminal-button text-green-400 hover:text-black text-xs py-0.5"
										disabled={isCompatSession(selectedSession)}
										on:click={() => selectedSession && handleStartSession(selectedSession)}
									>Start</button>
								{:else}
									<button
										class="terminal-button text-yellow-400 hover:text-black text-xs py-0.5"
										disabled={isCompatSession(selectedSession)}
										on:click={() => selectedSession && handleStopSession(selectedSession)}
									>Stop</button>
								{/if}
								<button
									class="terminal-button text-xs py-0.5"
									disabled={(selectedSession.status !== 'stopped' && selectedSession.status !== 'replay_finished') || isCompatSession(selectedSession)}
									on:click={() => selectedSession && openEditSession(selectedSession)}
								>Edit</button>
								<button
									class="terminal-button-danger text-xs py-0.5"
									disabled={isCompatSession(selectedSession)}
									on:click={() => selectedSession && handleDeleteSession(selectedSession)}
								>Delete</button>
							</div>
						{/if}
					</div>

				<!-- Trading gates -->
				<div class="flex flex-wrap items-center gap-1.5 mt-1.5">
					{#if anyGateBlocking}
						<span class="text-[9px] font-bold text-red-400 mr-1">BLOCKED</span>
					{:else}
						<span class="text-[9px] font-bold text-green-500 mr-1">ALL GATES OK</span>
					{/if}
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.system_paused ? 'bg-red-900/60 text-red-300 border border-red-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						System {gates.system_paused ? 'PAUSED' : '\u2713'}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.kill_switch ? 'bg-red-900/60 text-red-300 border border-red-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Kill Switch {gates.kill_switch ? 'ACTIVE' : '\u2713'}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.daily_loss_halt ? 'bg-red-900/60 text-red-300 border border-red-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Daily Loss {gates.daily_loss_halt ? 'HALT' : '\u2713'}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.recovery_active ? 'bg-red-900/60 text-red-300 border border-red-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Recovery {gates.recovery_active ? 'ACTIVE' : '\u2713'}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.hl_price === 'open' ? 'bg-red-900/60 text-red-300 border border-red-700' : gates.hl_price === 'half_open' ? 'bg-yellow-900/60 text-yellow-300 border border-yellow-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Price API {gates.hl_price === 'closed' ? '\u2713' : gates.hl_price.toUpperCase()}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.hl_trade === 'open' ? 'bg-red-900/60 text-red-300 border border-red-700' : gates.hl_trade === 'half_open' ? 'bg-yellow-900/60 text-yellow-300 border border-yellow-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Trade API {gates.hl_trade === 'closed' ? '\u2713' : gates.hl_trade.toUpperCase()}
					</span>
					<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono {gates.hl_account === 'open' ? 'bg-red-900/60 text-red-300 border border-red-700' : gates.hl_account === 'half_open' ? 'bg-yellow-900/60 text-yellow-300 border border-yellow-700' : 'bg-[#111] text-gray-500 border border-[#222]'}">
						Account API {gates.hl_account === 'closed' ? '\u2713' : gates.hl_account.toUpperCase()}
					</span>
				</div>

				<div class="flex items-center gap-4 mt-2 text-xs">
					<span>
						<span class="text-gray-500">Price</span>
						<span class="text-white font-bold ml-1">{selectedSession.current_price > 0 ? formatPrice(selectedSession.current_price) : '--'}</span>
					</span>
					<span>
						<span class="text-gray-500">Capital</span>
						{#if isLiveSelected}
							{#if balanceState(selectedSession) === 'real'}
								<span class="text-white font-bold ml-1">{formatPrice(displayCapital(selectedSession))}</span>
								<span
									class="text-[9px] uppercase tracking-wider text-green-500 ml-1"
									title="Real Hyperliquid balance{selectedSession.account_synced_at ? ` (synced ${selectedSession.account_synced_at})` : ''}"
								>real{selectedSession.account_network ? ` · ${selectedSession.account_network}` : ''}</span>
							{:else}
								<span class="text-amber-400 font-bold ml-1">—</span>
								<span
									class="text-[9px] uppercase tracking-wider text-amber-400 ml-1"
									title="The live Hyperliquid balance has not synced yet (daemon/exchange). No fabricated value is shown."
								>balance unavailable</span>
							{/if}
						{:else}
							<span class="text-white font-bold ml-1">{formatPrice(selectedSession.capital)}</span>
						{/if}
					</span>
					<span>
						<span class="text-gray-500">P&L</span>
						<span class="font-bold ml-1 {selectedSession.total_pnl > 0 ? 'text-green-400' : selectedSession.total_pnl < 0 ? 'text-red-400' : 'text-gray-400'}">
							{formatDollarPnl(selectedSession.total_pnl)} ({formatPercent(selectedSession.total_pnl_pct)})
						</span>
					</span>
					<span>
						<span class="text-gray-500">Win</span>
						<span class="text-white font-bold ml-1">{formatPercent(selectedSession.performance?.win_rate_pct ?? selectedSession.win_rate_pct)}</span>
					</span>
					<span>
						<span class="text-gray-500">PF</span>
						<span class="text-white font-bold ml-1">{formatRatio(selectedSession.performance?.profit_factor ?? selectedSession.profit_factor)}</span>
					</span>
					<span>
						<span class="text-gray-500">Avg</span>
						<span class="font-bold ml-1 {getPnlTone(selectedSession.performance?.avg_pnl ?? selectedSession.avg_pnl)}">
							{formatDollarPnl(selectedSession.performance?.avg_pnl ?? selectedSession.avg_pnl)}
						</span>
					</span>
					<span>
						<span class="text-gray-500">Expect</span>
						<span class="font-bold ml-1 {getPnlTone(selectedSession.performance?.expectancy ?? selectedSession.expectancy)}">
							{formatDollarPnl(selectedSession.performance?.expectancy ?? selectedSession.expectancy)}
						</span>
					</span>
					<span>
						<span class="text-gray-500">Leverage</span>
						<span class="text-white font-bold ml-1">{selectedSession.leverage ?? 1}x</span>
					</span>
					<span>
						<span class="text-gray-500">Default</span>
						<span class="text-white font-bold ml-1">{humanizeLabel(selectedSession.trade_mode ?? 'long_only')}</span>
					</span>
					{#if blockedMarkers.length > 0}
						{@const lastBlocked = latestBlockedMarker()}
						<span title={lastBlocked?.reason ?? 'Blocked signal'}>
							<span class="text-gray-500">Blocked</span>
							<span class="text-yellow-300 font-bold ml-1">{blockedMarkers.length}</span>
						</span>
					{/if}
					{#if selectedSession.position}
						<span class="border-l border-[#333] pl-4">
							<span class="text-gray-500">Pos</span>
							<span class="{getPositionSideColor(selectedSession.position.side)} font-bold uppercase ml-1">{selectedSession.position.side}</span>
							<span class="text-white font-bold ml-1">{formatPrice(selectedSession.position.entry_price)}</span>
							<span class="font-bold ml-1 {selectedSession.position.unrealized_pnl > 0 ? 'text-green-400' : selectedSession.position.unrealized_pnl < 0 ? 'text-red-400' : 'text-gray-400'}">
								{formatDollarPnl(selectedSession.position.unrealized_pnl)} ({formatPercent(selectedSession.position.unrealized_pnl_pct)})
							</span>
							<span class="border-l border-[#333] pl-4">
								<span class="text-gray-500">Size</span>
								<span class="text-white font-bold ml-1">{formatQty(selectedSession.position.size)} {selectedSession.symbol.split('/')[0]}</span>
								<span class="text-gray-400 ml-1">({formatPrice(selectedSession.position.size * (selectedSession.position.current_price || selectedSession.current_price))})</span>
							</span>
								<span class="inline-flex items-center gap-2 ml-2">
									<button
										class="text-red-500 hover:text-red-300 text-[10px] uppercase font-bold disabled:opacity-40 disabled:cursor-not-allowed"
										disabled={!supportsManualControl(selectedSession) || requestInFlight}
										on:click={handleClosePosition}
									>Close</button>
									<button
										class="text-amber-400 hover:text-amber-200 text-[10px] uppercase font-bold disabled:opacity-40 disabled:cursor-not-allowed"
										disabled={!supportsManualControl(selectedSession) || requestInFlight}
										on:click={handleFlipPosition}
									>Flip</button>
									<button
										class="text-[#888] hover:text-white text-[10px] uppercase font-bold disabled:opacity-40 disabled:cursor-not-allowed"
										disabled={!supportsManualControl(selectedSession) || requestInFlight}
										on:click={handleToggleAutoManagement}
										title={selectedSession.position.manual_pause ? 'Resume scanner auto-management' : 'Pause scanner auto-management (you own this position)'}
									>{selectedSession.position.manual_pause ? 'Resume' : 'Pause'}</button>
									{#if selectedSession.position.manual_pause}
										<span class="text-[9px] uppercase tracking-wider text-[#888] border border-[#333] px-1">Manual</span>
									{/if}
								</span>
							</span>
						{/if}
						<span class="ml-auto flex items-center gap-2 flex-shrink-0">
						<button
							class="terminal-button text-[10px] py-0 px-2 {showParams ? 'bg-[#111] text-white border-white' : ''}"
							on:click={() => showParams = !showParams}
						>Params</button>
							<button
								class="terminal-button text-[10px] py-0 px-2 {showVisualReplay ? 'bg-[#111] text-white border-white' : ''}"
								on:click={toggleVisualReplay}
							>{showVisualReplay ? 'Hide Chart' : 'Chart'}</button>
						{#if selectedSession.mode === 'replay' && selectedSession.replay_state}
							<span class="text-[10px] text-gray-500">
								{selectedSession.replay_state.cursor}/{selectedSession.replay_state.total_bars}
							</span>
						{/if}
					</span>
					</div>
					{#if isDeployedCompatSession(selectedSession)}
						<div class="mt-2 text-[10px] text-red-400 font-bold">
							⚠ LIVE session — manual actions place REAL reduce-only / market orders on Hyperliquid.
							{#if selectedSession.position?.book && selectedSession.position.book !== 'main'}
								<span class="ml-1 text-amber-300">Routed to the {selectedSession.position.book} book (sub-account).</span>
							{/if}
						</div>
					{/if}

					{#if selectedSession.position}
					{@const positionDetail = getPositionDetail(selectedSession)}
					{@const stopPrice = positionDetail?.stopPrice ?? null}
					{@const takePrice = positionDetail?.takePrice ?? null}
					<div class="mt-2 pt-2 border-t border-[#222] grid grid-cols-2 lg:grid-cols-6 gap-2 text-[10px]">
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Entry</div>
							<div class="text-white font-bold">{formatPrice(selectedSession.position.entry_price)}</div>
							<div class="text-gray-600">{getOpenDuration(selectedSession.position.entry_time)} open</div>
						</div>
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Position Size</div>
							<div class="text-white font-bold">{formatQty(selectedSession.position.size)} {selectedSession.symbol.split('/')[0]}</div>
							<div class="text-gray-600">{positionDetail ? formatPrice(positionDetail.notional) : '\u2014'} notional</div>
						</div>
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Stop Loss</div>
							<div class="font-bold {stopPrice !== null ? 'text-red-400' : 'text-gray-500'}">
								{stopPrice !== null ? formatPrice(stopPrice) : '\u2014'}
							</div>
							<div class="text-gray-600">{positionDetail?.stopLabel ?? '\u2014'}</div>
						</div>
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Take Profit</div>
							<div class="font-bold {takePrice !== null ? 'text-green-400' : 'text-gray-500'}">
								{takePrice !== null ? formatPrice(takePrice) : '\u2014'}
							</div>
							<div class="text-gray-600">{positionDetail?.takeLabel ?? '\u2014'}</div>
						</div>
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Unrealized</div>
							<div class="font-bold {selectedSession.position.unrealized_pnl > 0 ? 'text-green-400' : selectedSession.position.unrealized_pnl < 0 ? 'text-red-400' : 'text-gray-400'}">
								{formatPrice(selectedSession.position.unrealized_pnl)}
							</div>
							<div class="text-gray-600">{formatPercent(selectedSession.position.unrealized_pnl_pct)}</div>
						</div>
						<div class="bg-[#050505] border border-[#222] px-2 py-1.5">
							<div class="text-gray-500 uppercase tracking-wider">Exit</div>
							<div class="text-gray-300 truncate" title={positionDetail?.exitSummary}>{positionDetail?.exitSummary ?? 'Strategy exit'}</div>
							<div class="text-gray-600">Current {formatPrice(selectedSession.current_price)}</div>
						</div>
					</div>

					{#if supportsManualControl(selectedSession)}
						<div class="mt-2 pt-2 border-t border-[#222] flex flex-wrap items-center gap-x-4 gap-y-2 text-[10px]">
							<span class="uppercase tracking-wider {isLiveSelected ? 'text-red-400 font-bold' : 'text-gray-500'}">{isLiveSelected ? 'Manual · LIVE' : 'Manual'}</span>
							<span class="inline-flex items-center gap-1">
								<span class="text-gray-500">Partial</span>
								<input class="{MANUAL_INPUT_CLASS} w-14" type="number" min="0" max="100" step="1" placeholder="%" bind:value={partialPctInput} disabled={requestInFlight} />
								<button class={MANUAL_BTN_CLASS} disabled={requestInFlight} on:click={handlePartialClose}>Close %</button>
							</span>
							<span class="inline-flex items-center gap-1">
								<span class="text-gray-500">SL</span>
								<input class="{MANUAL_INPUT_CLASS} w-20" type="number" min="0" step="any" placeholder={stopPrice !== null ? formatPrice(stopPrice) : 'price'} bind:value={slInput} disabled={requestInFlight} />
								<button class={MANUAL_BTN_CLASS} disabled={requestInFlight} on:click={() => handleAdjustLevel('sl')}>Set</button>
								<button class={MANUAL_BTN_CLASS} disabled={requestInFlight} on:click={() => handleAdjustLevel('sl', true)}>Clear</button>
							</span>
							<span class="inline-flex items-center gap-1">
								<span class="text-gray-500">TP</span>
								<input class="{MANUAL_INPUT_CLASS} w-20" type="number" min="0" step="any" placeholder={takePrice !== null ? formatPrice(takePrice) : 'price'} bind:value={tpInput} disabled={requestInFlight} />
								<button class={MANUAL_BTN_CLASS} disabled={requestInFlight} on:click={() => handleAdjustLevel('tp')}>Set</button>
								<button class={MANUAL_BTN_CLASS} disabled={requestInFlight} on:click={() => handleAdjustLevel('tp', true)}>Clear</button>
							</span>
							{#if requestInFlight}<span class="text-gray-500">working…</span>{/if}
						</div>
					{/if}
				{/if}

				{#if !selectedSession.position && supportsManualControl(selectedSession)}
					<div class="mt-2 pt-2 border-t border-[#222] flex flex-wrap items-end gap-x-3 gap-y-2 text-[10px]">
						<span class="uppercase tracking-wider self-center {isLiveSelected ? 'text-red-400 font-bold' : 'text-gray-500'}">{isLiveSelected ? 'Open LIVE' : 'Open manual'}</span>
						<span class="inline-flex rounded overflow-hidden border border-[#333]">
							<button class="{MANUAL_SEG_CLASS} {openDirection === 'long' ? 'bg-[#111] text-emerald-400' : ''}" on:click={() => (openDirection = 'long')}>Long</button>
							<button class="{MANUAL_SEG_CLASS} {openDirection === 'short' ? 'bg-[#111] text-red-400' : ''}" on:click={() => (openDirection = 'short')}>Short</button>
						</span>
						<span class="inline-flex rounded overflow-hidden border border-[#333]">
							<button class="{MANUAL_SEG_CLASS} {openSizeMode === 'risk' ? 'bg-[#111] text-white' : ''}" on:click={() => (openSizeMode = 'risk')}>Risk %</button>
							<button class="{MANUAL_SEG_CLASS} {openSizeMode === 'size' ? 'bg-[#111] text-white' : ''}" on:click={() => (openSizeMode = 'size')}>Size</button>
						</span>
						{#if openSizeMode === 'risk'}
							<label class="inline-flex items-center gap-1"><span class="text-gray-500">Risk %</span><input class="{MANUAL_INPUT_CLASS} w-14" type="number" min="0" max="100" step="0.1" bind:value={openRiskPctInput} /></label>
						{:else}
							<label class="inline-flex items-center gap-1"><span class="text-gray-500">Size</span><input class="{MANUAL_INPUT_CLASS} w-20" type="number" min="0" step="any" bind:value={openSizeInput} /></label>
						{/if}
						<label class="inline-flex items-center gap-1"><span class="text-gray-500">Lev</span><input class="{MANUAL_INPUT_CLASS} w-12" type="number" min="1" step="0.5" bind:value={openLeverageInput} /></label>
						<label class="inline-flex items-center gap-1"><span class="text-gray-500">SL</span><input class="{MANUAL_INPUT_CLASS} w-20" type="number" min="0" step="any" placeholder="opt" bind:value={openSlInput} /></label>
						<label class="inline-flex items-center gap-1"><span class="text-gray-500">TP</span><input class="{MANUAL_INPUT_CLASS} w-20" type="number" min="0" step="any" placeholder="opt" bind:value={openTpInput} /></label>
						<button class={MANUAL_BTN_ACCENT_CLASS} disabled={requestInFlight} on:click={handleOpenManual}>Open</button>
					</div>
				{/if}
			</div>

			{#if pendingConfirm}
				<div
					class="fixed inset-0 z-50 bg-black/60 flex items-center justify-center"
					role="presentation"
					on:click={(e) => { if (e.target === e.currentTarget) cancelConfirm(); }}
				>
					<div
						class="bg-[#0a0a0a] border border-[#333] rounded p-4 max-w-sm w-full mx-4"
						role="dialog"
						aria-modal="true"
						tabindex="-1"
					>
						<div class="text-[11px] font-bold uppercase tracking-wider text-white mb-2">{pendingConfirm.label}</div>
						{#if isLiveSelected}
							<div class="text-[11px] font-bold text-red-400 mb-2">⚠ LIVE — this places a REAL order on Hyperliquid with real money.</div>
						{/if}
						<div class="text-[11px] text-gray-300 mb-3">{pendingConfirm.detail}</div>
						{#if isLiveSelected}
							<div class="text-[10px] text-gray-500 mb-3">Routed to Hyperliquid on the configured network; fill price is the exchange's, not this estimate.</div>
						{:else}
							<div class="text-[10px] text-gray-500 mb-3">Paper fill at the current mid — no slippage or fees modeled.</div>
						{/if}
						<div class="flex justify-end gap-2">
							<button class={MANUAL_BTN_CLASS} on:click={cancelConfirm}>Cancel</button>
							<button class={MANUAL_BTN_ACCENT_CLASS} disabled={requestInFlight} on:click={executeConfirm}>Confirm</button>
						</div>
					</div>
				</div>
			{/if}

			<!-- Strategy Parameters Panel -->
			{#if showParams && selectedSession}
				{@const decisionParams = getSessionDecisionParams(selectedSession)}
				<div class="border-b border-[#222] bg-[#050505] px-4 py-3 flex-shrink-0 overflow-y-auto max-h-48">
					<div class="flex items-center justify-between mb-2">
						<h4 class="text-[10px] font-bold text-gray-500 uppercase tracking-wider">Strategy Decision Parameters</h4>
						<button class="text-gray-600 hover:text-gray-300" aria-label="Close strategy parameters" title="Close strategy parameters" on:click={() => showParams = false}>
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
					<div class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
						<span class="text-gray-500">Strategy</span>
						<span class="text-white font-bold">{selectedSession.strategy_name}</span>
						<span class="text-gray-500">Version</span>
						<span class="text-gray-300">{selectedSession.strategy_version || '\u2014'}</span>
						<span class="text-gray-500">Symbol</span>
						<span class="text-gray-300">{selectedSession.symbol}</span>
						<span class="text-gray-500">Timeframe</span>
						<span class="text-gray-300">{selectedSession.timeframe}</span>
						<span class="text-gray-500">Runtime</span>
						<span class="text-gray-300">{selectedSession.runtime_type ?? selectedSession.strategy_type ?? '\u2014'}</span>
						<span class="text-gray-500">Source</span>
						<span class="text-gray-300">{selectedSession.runtime_source ?? '\u2014'}</span>
						<span class="text-gray-500">Mode</span>
						<span class="text-gray-300 uppercase">{selectedSession.mode}</span>
						{#if selectedSession.mode === 'live'}
							<span class="text-gray-500">Live Feed</span>
							<span class="text-gray-300 uppercase">{selectedSession.live_feed ?? 'default'}</span>
							{#if selectedSession.live_feed === 'ibkr'}
								<span class="text-gray-500">IBKR Contract</span>
								<span class="text-gray-300">{selectedSession.ibkr_sec_type}:{selectedSession.ibkr_exchange}:{selectedSession.ibkr_currency}</span>
								<span class="text-gray-500">IBKR Bars</span>
								<span class="text-gray-300">{selectedSession.ibkr_what_to_show}</span>
							{/if}
						{/if}
						<span class="text-gray-500">Capital</span>
						{#if isLiveSelected}
							{#if balanceState(selectedSession) === 'real'}
								<span class="text-gray-300">{formatPrice(displayCapital(selectedSession))}</span>
							{:else}
								<span class="text-amber-400">balance unavailable</span>
							{/if}
						{:else}
							<span class="text-gray-300">{formatPrice(selectedSession.initial_capital)}</span>
						{/if}
						<span class="text-gray-500">Position Size</span>
						<span class="text-gray-300">{selectedSession.position_size_pct}%</span>
						{#if selectedSession.mode === 'replay'}
							<span class="text-gray-500">Replay Range</span>
							<span class="text-gray-300">{selectedSession.replay_start ?? '\u2014'} -> {selectedSession.replay_end ?? '\u2014'}</span>
							<span class="text-gray-500">Replay Speed</span>
							<span class="text-gray-300">{selectedSession.replay_speed ?? 1}x</span>
						{/if}
					</div>
					{#if Object.keys(decisionParams).length > 0}
						<div class="mt-2 pt-2 border-t border-[#111]">
							<h5 class="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Runtime Rules</h5>
							<div class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
								{#each Object.entries(decisionParams) as [key, value]}
									<span class="text-gray-500">{key}</span>
									<span class="text-gray-300 font-mono">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>
								{/each}
							</div>
						</div>
					{/if}
				</div>
			{/if}

			<!-- Content area uses CSS Grid for stable layout -->
			<div
				class="flex-1 overflow-hidden"
				style="display: grid; grid-template-rows: auto 1fr 192px; min-height: 0;"
			>
				<!-- Row 1: Replay Controls -->
				{#if supportsReplayControls && showVisualReplay && selectedSession.mode === 'replay'}
					<div class="px-4 py-2 border-b border-[#111] bg-[#0a0a0a]">
						<div class="flex items-center gap-3 flex-wrap">
							{#if selectedSession.replay_state?.is_playing}
								<button
									class="terminal-button text-yellow-400 hover:text-black text-xs py-0.5"
									on:click={handleReplayPause}
									title="Pause (Space)"
								>Pause</button>
							{:else}
								<button
									class="terminal-button text-green-400 hover:text-black text-xs py-0.5"
									on:click={handleReplayPlay}
									title="Play (Space)"
								>Play</button>
							{/if}

							<div class="flex items-center gap-1">
								<button class="terminal-button text-xs py-0.5 px-2" on:click={() => handleReplayStep(1)} title="Step +1 (Right)">+1</button>
								<button class="terminal-button text-xs py-0.5 px-2" on:click={() => handleReplayStep(10)} title="Step +10 (Shift+Right)">+10</button>
								<button class="terminal-button text-xs py-0.5 px-2" on:click={() => handleReplayStep(50)} title="Step +50">+50</button>
							</div>

							<div class="flex items-center gap-1">
								<span class="text-[10px] text-gray-500 uppercase">Spd</span>
								<select
									class="terminal-select w-auto py-0 text-xs"
									bind:value={replaySpeedInput}
									on:change={() => handleSpeedChange(replaySpeedInput)}
								>
									{#each speedPresets as speed}
										<option value={speed}>{speed}x</option>
									{/each}
								</select>
							</div>

							<button class="terminal-button-danger text-xs py-0.5" on:click={handleReplayReset} title="Reset (R)">Reset</button>

							<span class="text-[10px] text-gray-600">{chartBars.length} bars</span>

							<div class="ml-auto text-[10px] text-gray-600 hidden lg:flex items-center gap-2">
								<kbd class="px-1 border border-[#333] text-gray-500">Space</kbd>
								<kbd class="px-1 border border-[#333] text-gray-500">Arrows</kbd>
								<kbd class="px-1 border border-[#333] text-gray-500">R</kbd>
							</div>
						</div>

						{#if selectedSession.replay_state && selectedSession.replay_state.total_bars > 0}
							<div class="mt-1.5 flex items-center gap-2">
								<span class="text-[10px] text-gray-600 w-8 text-right">{selectedSession.replay_state.cursor}</span>
								<input
									type="range"
									min="0"
									max={selectedSession.replay_state.total_bars - 1}
									value={selectedSession.replay_state.cursor}
									on:change={handleSeekSlider}
									class="flex-1 h-1.5 cursor-pointer accent-white"
									style="background: linear-gradient(to right, #555 {(selectedSession.replay_state.cursor / (selectedSession.replay_state.total_bars - 1)) * 100}%, #222 {(selectedSession.replay_state.cursor / (selectedSession.replay_state.total_bars - 1)) * 100}%);"
								/>
								<span class="text-[10px] text-gray-600 w-10">{selectedSession.replay_state.total_bars}</span>
							</div>
						{/if}
					</div>
				{:else}
					<div></div>
				{/if}

				<!-- Row 2: Chart area -->
				<div class="flex overflow-hidden min-h-0" style="min-height: 200px;">
					<!-- Indicator Panel Sidebar -->
					{#if showVisualReplay && showIndicatorPanel && Object.keys(indicatorConfig).length > 0}
						<div class="w-52 border-r border-[#222] bg-[#050505] p-3 overflow-y-auto flex-shrink-0">
							<div class="flex justify-between items-center mb-3">
								<h4 class="text-[10px] font-bold text-gray-500 uppercase tracking-wider">Indicators</h4>
								<button
									class="text-gray-600 hover:text-gray-300"
									aria-label="Close indicators panel"
									title="Close indicators panel"
									on:click={() => showIndicatorPanel = false}
								>
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
									</svg>
								</button>
							</div>

							{#if overlayIndicatorNames.length > 0}
								<div class="mb-3">
									<h5 class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Overlays</h5>
									{#each overlayIndicatorNames as name}
										{@const config = indicatorConfig[name]}
										{@const value = getCurrentIndicatorValue(name)}
										<label class="flex items-center gap-2 py-1 border-b border-[#111] hover:bg-[#111] px-1 cursor-pointer">
											<input
												type="checkbox"
												checked={indicatorVisibility[name] ?? true}
												on:change={() => toggleIndicatorVisibility(name)}
												class="accent-white bg-transparent border-[#333]"
											/>
											<span class="w-2 h-2 flex-shrink-0" style="background-color: {config?.color || getIndicatorColor(name)}"></span>
											<span class="min-w-0 flex-1 truncate text-[11px] text-gray-400" title={name}>{name}</span>
											<span class="text-[11px] font-mono text-white">{formatIndicatorValue(value, name)}</span>
										</label>
									{/each}
								</div>
							{/if}

							{#if lowerPaneIndicatorNames.length > 0}
								<div class="mb-3">
									<h5 class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Lower Pane</h5>
									{#each lowerPaneIndicatorNames as name}
										{@const config = indicatorConfig[name]}
										{@const value = getCurrentIndicatorValue(name)}
										<label class="flex items-center gap-2 py-1 border-b border-[#111] hover:bg-[#111] px-1 cursor-pointer">
											<input
												type="checkbox"
												checked={indicatorVisibility[name] ?? true}
												on:change={() => toggleIndicatorVisibility(name)}
												class="accent-white bg-transparent border-[#333]"
											/>
											<span class="w-2 h-2 flex-shrink-0" style="background-color: {config?.color || getIndicatorColor(name)}"></span>
											<span class="min-w-0 flex-1 truncate text-[11px] text-gray-400" title={name}>{name}</span>
											<span class="text-[11px] font-mono text-white">{formatIndicatorValue(value, name)}</span>
										</label>
									{/each}
								</div>
							{/if}

							{#if sidebarOnlyIndicatorNames.length > 0}
								<div>
									<h5 class="text-[10px] text-gray-600 uppercase tracking-wider mb-1">Sidebar Only</h5>
									{#each sidebarOnlyIndicatorNames as name}
										{@const config = indicatorConfig[name]}
										{@const value = getCurrentIndicatorValue(name)}
										<div class="flex items-center gap-2 py-1 border-b border-[#111] px-1">
											<span class="w-2 h-2 flex-shrink-0" style="background-color: {config?.color || getIndicatorColor(name)}"></span>
											<span class="min-w-0 flex-1 truncate text-[11px] text-gray-400" title={name}>{name}</span>
											<span class="text-[9px] uppercase tracking-wider text-gray-600">Sidebar</span>
											<span class="text-[11px] font-mono text-white">{formatIndicatorValue(value, name)}</span>
										</div>
									{/each}
								</div>
							{/if}
						</div>
					{/if}

					<!-- Chart or placeholder -->
					<div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
						{#if showVisualReplay}
							<div class="border-b border-[#171717] bg-[#050505] px-3 py-2">
								<div class="flex flex-col gap-2 2xl:flex-row 2xl:items-center 2xl:justify-between">
									<div class="min-w-0">
										<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
											<span class="text-[10px] font-bold uppercase tracking-[0.24em] text-gray-500">Chart</span>
											<span class="text-[11px] text-white">{selectedSession.symbol} / {activeVisualChartTimeframe}</span>
											<span class="text-[10px] text-gray-600 uppercase">{selectedSession.mode}</span>
										</div>
										<div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-500">
											<span>{selectedSession.strategy_name}</span>
											<span>{chartToolHint()}</span>
										</div>
									</div>
									<div class="flex flex-wrap items-center gap-3">
										<div class="flex flex-wrap items-center gap-1">
											{#each timeframes as tf}
												<button
													type="button"
													class={timeframeButtonClass(tf, activeVisualChartTimeframe)}
													on:click={() => setVisualChartTimeframe(tf)}
													disabled={!canChangeVisualChartTimeframe}
													title={canChangeVisualChartTimeframe ? `Switch chart to ${tf}` : 'Replay charts stay on the session timeframe'}
												>
													{tf}
												</button>
											{/each}
										</div>
										<div class="flex flex-wrap items-center gap-1">
											{#if Object.keys(indicatorConfig).length > 0}
												<button
													type="button"
													class={chartToolButtonClass(showIndicatorPanel)}
													on:click={() => (showIndicatorPanel = !showIndicatorPanel)}
												>
													{showIndicatorPanel ? 'Hide Ind' : 'Indicators'}
												</button>
											{/if}
											<button
												type="button"
												class={chartToolButtonClass(activeDrawingTool === 'horizontalLine')}
												on:click={() => toggleDrawingTool('horizontalLine')}
											>
												H-Line
											</button>
											<button
												type="button"
												class={chartToolButtonClass(activeDrawingTool === 'trendLine')}
												on:click={() => toggleDrawingTool('trendLine')}
											>
												Trend
											</button>
											<button
												type="button"
												class={chartToolButtonClass(false)}
												on:click={resetChartView}
											>
												Reset View
											</button>
											<button
												type="button"
												class={chartToolButtonClass(false)}
												on:click={clearChartDrawings}
												disabled={chartDrawings.length === 0 && !pendingTrendLineStart}
											>
												Clear
											</button>
										</div>
									</div>
								</div>
								<div class="mt-2 flex flex-wrap items-center gap-2 text-[10px]">
									<span class="rounded-sm border border-[#1e293b] bg-[#020617] px-2 py-1 text-slate-300">
										{activeDrawingTool === 'cursor' ? 'Cursor' : activeDrawingTool === 'horizontalLine' ? 'Horizontal lines' : 'Trend lines'}
									</span>
									{#if pendingTrendLineStart}
										<span class="text-white">Trend line anchor locked. Click a second point to finish.</span>
									{/if}
									{#if chartDrawings.length > 0}
										<span class="text-gray-600">{chartDrawings.length} drawing{chartDrawings.length === 1 ? '' : 's'} on chart</span>
									{/if}
								</div>
							</div>

							<div class="relative min-h-0 min-w-0 flex-1">
								{#if loadingBars}
									<div class="h-full p-4"><Skeleton rows={10} /></div>
								{:else if chartBars.length > 0}
									{#key chartKey}
										<ChartWorkspace
											data={chartBars}
											entryMarkers={entryMarkers}
											exitMarkers={exitMarkers}
											triggerMarkers={triggerMarkers}
											priceLines={positionPriceLines}
											mainIndicators={mainIndicators.filter(i => i.visible !== false)}
											subIndicators={subIndicators.filter(i => i.visible !== false)}
											strategyName={selectedSession.strategy_name}
											strategyMeta={`${selectedSession.symbol} / ${activeVisualChartTimeframe} / ${selectedSession.mode.toUpperCase()}`}
											strategyParams={getSessionChartParams(selectedSession)}
											showStrategyInfo={true}
											autoScroll={true}
											windowSize={200}
											drawings={chartDrawings}
											activeTool={activeDrawingTool}
											{fitContentToken}
											on:drawingPoint={handleChartDrawingPoint}
										/>
									{/key}
								{:else}
									<div class="flex items-center justify-center h-full text-gray-600 text-xs">
										No chart data. Start the session or step forward.
									</div>
								{/if}
							</div>
						{:else}
							<div class="flex items-center justify-center h-full text-gray-700">
								<div class="text-center">
									<p class="text-xs uppercase tracking-wider mb-1">
										Click "Chart" to view price action
									</p>
									<p class="text-[10px] text-gray-600">
										{selectedSession.total_trades} trades | Win rate: {selectedSession.total_trades > 0 ? ((selectedSession.winning_trades / selectedSession.total_trades) * 100).toFixed(0) : '0'}%
									</p>
								</div>
							</div>
						{/if}
					</div>
				</div>

				<!-- Row 3: Bottom Panels -->
				<div class="border-t border-[#222] grid grid-cols-3 overflow-hidden">
					<!-- Live Indicators -->
					<div class="border-r border-[#222] p-2 overflow-y-auto">
						<h3 class="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1.5">Indicators</h3>
						{#if bottomIndicatorNames.length > 0}
							{#each bottomIndicatorNames as name}
								{@const runtimeIndicator = selectedSession.indicators[name]}
								{@const value = getCurrentIndicatorValue(name) ?? runtimeIndicator?.value ?? null}
								{@const group = getIndicatorSidebarGroup(name, indicatorConfig[name])}
								<div class="flex items-center gap-2 py-1 border-b border-[#111] hover:bg-[#111] px-1">
									<span class="min-w-0 flex-1 truncate text-[11px] text-gray-400" title={name}>{name}</span>
									<span class="text-[9px] uppercase tracking-wider {group === 'overlays' ? 'text-emerald-400' : group === 'lower' ? 'text-white' : 'text-[#555]'}">
										{group === 'overlays' ? 'OVR' : group === 'lower' ? 'LOW' : 'SIDE'}
									</span>
									<span class="text-[11px] text-white font-mono">{formatIndicatorValue(value, name)}</span>
								</div>
							{/each}
						{:else}
							<p class="text-gray-600 text-[11px]">No indicator data yet</p>
						{/if}
					</div>

					<!-- Pending Signals -->
					<div class="border-r border-[#222] p-2 overflow-y-auto">
						<h3 class="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1.5">Signals</h3>
						{#if selectedSession.pending_signals.length > 0}
							{#each selectedSession.pending_signals as signal}
								<div class="py-1 border-b border-[#111]">
									<div class="flex items-center gap-1.5">
										<span class="{getSignalIcon(signal.signal_type)} text-[11px]">
											{signal.signal_type === 'entry' ? '>' : '<'}
										</span>
										<span class="text-[11px] text-gray-400">{signal.description}</span>
									</div>
									<div class="text-[10px] text-gray-600 mt-0.5 pl-4">
										{signal.indicator_name}: {signal.current_value.toFixed(2)} -> {signal.trigger_value.toFixed(2)}
										({signal.distance_pct.toFixed(1)}%)
									</div>
								</div>
							{/each}
						{:else}
							<p class="text-gray-600 text-[11px]">No signals approaching</p>
						{/if}
					</div>

					<!-- Trade History -->
					<div class="p-2 overflow-y-auto">
						<h3 class="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1.5">Trades</h3>
						{#if sessionTrades.length > 0}
							<DataTable
								columns={tradeHistoryColumns}
								rows={sessionTrades}
								rowKey={getPaperTradeRowKey}
								tableClass="w-full text-[11px]"
								headerClass="text-gray-500 border-b border-[#222]"
								rowClass="border-b border-[#111] hover:bg-[#111]"
								emptyText="No trades yet"
								emptyClass="py-3 text-center text-gray-600 text-[11px]"
							>
								<svelte:fragment slot="cell" let:row let:column>
									{@const trade = toPaperTrade(row)}
									{#if column.key === 'side'}
										<span class="font-bold {trade.side === 'long' || trade.side === 'LONG' || trade.side === 'Long' ? 'text-green-400' : 'text-red-400'}">
											{trade.side?.toUpperCase() === 'SHORT' ? 'Short' : 'Long'}
										</span>
									{:else if column.key === 'entry_time'}
										<span class="text-gray-400">{formatDateTime(trade.entry_time)}</span>
									{:else if column.key === 'exit_time'}
										<span class="text-gray-400">{formatDateTime(trade.exit_time)}</span>
									{:else if column.key === 'entry_price'}
										<span class="text-gray-400">{formatPrice(trade.entry_price)}</span>
									{:else if column.key === 'exit_price'}
										{@const closeBadge = getTradeCloseBadge(trade)}
										<div class="flex flex-col items-end">
											<span class="text-gray-400">{formatPrice(trade.exit_price)}</span>
											{#if closeBadge}
												<span
													class="mt-1 inline-flex rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-wider {closeBadge.tone}"
													title={closeBadge.title}
												>
													{closeBadge.label}
												</span>
											{/if}
										</div>
									{:else if column.key === 'pnl'}
										<div class="relative group cursor-default">
											<span class="font-bold {getPnlTone(trade.pnl)}">
												{formatDollarPnl(trade.pnl)}
											</span>
											<div class="absolute right-0 top-full z-10 hidden group-hover:block bg-[#111] border border-[#333] p-2 text-[10px] min-w-[140px]">
												<div class="flex justify-between gap-4 mb-1">
													<span class="text-gray-500">Gross:</span>
													<span class="text-gray-300">{formatDollarPnl(trade.gross_pnl)}</span>
												</div>
												<div class="flex justify-between gap-4 mb-1">
													<span class="text-gray-500">Fees:</span>
													<span class="text-red-400">{formatDollarPnl((trade.fees_paid ?? 0) * -1)}</span>
												</div>
												<div class="flex justify-between gap-4 mb-1">
													<span class="text-gray-500">Funding:</span>
													<span class="{getPnlTone(trade.funding_pnl)}">{formatDollarPnl(trade.funding_pnl)}</span>
												</div>
												<div class="border-t border-[#333] pt-1 mt-1 flex justify-between gap-4 font-bold">
													<span class="text-gray-400">Net:</span>
													<span class="{getPnlTone(trade.net_pnl)}">{formatDollarPnl(trade.net_pnl)}</span>
												</div>
											</div>
										</div>
									{:else if column.key === 'pnl_pct'}
										<span class="font-bold {getPnlTone(trade.pnl_pct)}">
											{formatPercent(trade.pnl_pct)}
										</span>
									{/if}
								</svelte:fragment>
							</DataTable>
							{#if sessionTrades.length < selectedSession.total_trades}
								<p class="text-[10px] text-gray-600 mt-1 text-center">Showing {sessionTrades.length} of {selectedSession.total_trades} trades</p>
							{/if}
						{:else}
							<p class="text-gray-600 text-[11px] text-center py-3">No trades yet</p>
						{/if}
					</div>
				</div>
			</div>
			{:else if selectedArchivedStrategy}
				<div class="border-b border-[#222] bg-[#0a0a0a] px-4 py-2 flex-shrink-0">
					<div class="flex items-center gap-3 min-w-0">
						<a
							href="/lab/strategy/{encodeURIComponent(selectedArchivedStrategy.id)}"
							class="text-sm font-bold text-white truncate hover:text-yellow-400 hover:underline transition-colors"
							title="Open strategy detail"
						>{selectedArchivedStrategy.display_id || selectedArchivedStrategy.name || selectedArchivedStrategy.id}</a>
						<span class="text-[10px] uppercase font-bold text-red-400">
							{prettyLifecycleState(selectedArchivedStrategy.state)}
						</span>
					</div>
					<div class="mt-1 text-xs text-gray-500">
						{selectedArchivedStrategy.symbol || '--'} | Updated {formatDateTime(selectedArchivedStrategy.updated_at)}
					</div>
				</div>

				<div class="flex-1 overflow-y-auto p-4 space-y-4">
					{#if archivedDetailLoading}
						<Skeleton rows={10} />
					{:else}
						<div class="bg-[#050505] border border-[#222] p-3">
							<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">
								Why It Was Demoted / Archived
							</div>
							<div class="text-sm text-gray-200 leading-relaxed">
								{selectedArchivedReason.reason}
							</div>
							<div class="mt-2 text-[11px] text-gray-500">
								Transition: {prettyLifecycleState(selectedArchivedReason.fromState)} -> {prettyLifecycleState(selectedArchivedReason.toState)}
								{#if selectedArchivedReason.actor}
									| Actor: {selectedArchivedReason.actor}
								{/if}
							</div>
							{#if selectedArchivedReason.timestamp}
								<div class="text-[11px] text-gray-600 mt-1">
									{formatDateTime(selectedArchivedReason.timestamp)}
								</div>
							{/if}
						</div>

						<div class="bg-[#050505] border border-[#222] p-3">
							<div class="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
								Lifecycle Timeline
							</div>
							{#if archivedTimelineEvents.length === 0}
								<p class="text-[11px] text-gray-600">No lifecycle events recorded for this strategy.</p>
							{:else}
								{#each archivedTimelineEvents as event}
									<div class="py-2 border-b border-[#151515] last:border-b-0">
										<div class="flex items-start justify-between gap-2">
											<div class="text-[11px] text-white">
												{prettyLifecycleState(event.from_state)} -> {prettyLifecycleState(event.to_state)}
											</div>
											<div class="text-[10px] text-gray-600 flex-shrink-0">
												{formatDateTime(event.created_at)}
											</div>
										</div>
										<div class="text-[10px] text-gray-500 mt-0.5">
											Actor: {event.actor || 'system'}
										</div>
										{#if event.reason}
											<div class="text-[11px] text-gray-300 mt-1">
												{compactReason(event.reason, 420)}
											</div>
										{/if}
									</div>
								{/each}
							{/if}
						</div>
					{/if}
				</div>
			{:else}
				<!-- Empty State -->
				<div class="flex-1 flex flex-col items-center justify-center text-gray-800">
					<svg class="w-20 h-20 mb-4 opacity-20" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
					</svg>
					<h3 class="text-lg font-bold uppercase tracking-widest mb-1">{emptyStateTitle}</h3>
					<p class="text-xs text-gray-600 max-w-sm text-center">
						{#if isLiveView}
							Select a deployed strategy to inspect live chart, signals, indicators, and trade history.
						{:else}
							Select a session from the list or create a new one to start simulating trades.
						{/if}
					</p>
				</div>
			{/if}
		</div>
	</div>

