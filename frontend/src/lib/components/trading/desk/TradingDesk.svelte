<script lang="ts">
	/**
	 * The trading desk behind /live-trades and /paper-trades. One component, two modes:
	 * live trades real money on Hyperliquid; paper trades simulated books at the live mid.
	 *
	 * Data: sessions (positions, prices, signals) every 15 s, the fleet scorecard every
	 * 30 s, fills / journal / funding and OI / scanner timing every 60 s, and the price
	 * stream continuously. A trade event from the websocket refreshes everything at once.
	 * Every order goes through a confirmation; the backend re-checks every limit.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		adjustPaperStopLoss,
		adjustPaperTakeProfit,
		closePaperPosition,
		flipPaperPosition,
		getForvenDashboard,
		getForvenEquityHistory,
		getForvenRisk,
		getLifecycleStrategy,
		getPaperSessions,
		listLifecycleStrategies,
		openManualPaperPosition,
		partialClosePaperPosition,
		setPaperAutoManagement,
	} from '$lib/api';
	import type {
		ForvenDashboardResponse,
		ForvenEquityHistory,
		LifecycleEvent,
		LifecycleStrategy,
		PaperTradingSession,
	} from '$lib/api';
	import { setLiveNotionalCeiling } from '$lib/api/forven';
	import { getSchedulerJobs, type LiveFleet, type SchedulerJobSummary } from '$lib/api/dashboard';
	import { getDeskFills, getFleet, getJournal, getMarketContext, type DeskFill, type DeskMode, type JournalEvent, type MarketContext } from '$lib/api/desk';
	import { forvenDashboard, forvenRisk } from '$lib/stores/forven';
	import { forvenLivePrices, forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import { setPageContext } from '$lib/stores/pageContext';
	import { buildAttention, type SideTab } from '$lib/utils/tradingDesk/attention';
	import { cap1, fmtPct, fmtPx, fmtQty, fmtUsd, num, parseTs } from '$lib/utils/tradingDesk/format';
	import { blockingGates } from '$lib/utils/tradingDesk/gates';
	import { buildRows, livePrice, sortRows, statsByStrategy, type RailFilter } from '$lib/utils/tradingDesk/rows';
	import DeskAccountStrip from './DeskAccountStrip.svelte';
	import DeskArchivedPanel from './DeskArchivedPanel.svelte';
	import DeskAttention from './DeskAttention.svelte';
	import DeskBlotter, { type BlotScope, type BlotTab } from './DeskBlotter.svelte';
	import DeskChart from './DeskChart.svelte';
	import DeskConfirmDialog, { type ConfirmSpec } from './DeskConfirmDialog.svelte';
	import DeskDetailsPanel from './DeskDetailsPanel.svelte';
	import DeskExpectationPanel from './DeskExpectationPanel.svelte';
	import DeskOrderTicket, { type TicketReview } from './DeskOrderTicket.svelte';
	import DeskPositionCard, { type PositionAction } from './DeskPositionCard.svelte';
	import DeskStatusLine from './DeskStatusLine.svelte';
	import DeskStrategyRail from './DeskStrategyRail.svelte';
	import DeskWhyPanel from './DeskWhyPanel.svelte';

	export let mode: DeskMode;
	export let dashboard: ForvenDashboardResponse | null = null;

	const STORAGE_KEY = mode === 'live' ? 'forven.live.selectedSessionId' : 'forven.paper.selectedSessionId';
	const SIDE_TABS: Array<[SideTab, string]> = [['position', 'Position'], ['why', 'Why'], ['expect', 'Expectation'], ['details', 'Details']];
	const BLOT_TABS: BlotTab[] = ['positions', 'orders', 'fills', 'decisions', 'performance', 'risk'];

	let sessions: PaperTradingSession[] = [];
	let fleet: LiveFleet | null = null;
	let journal: JournalEvent[] = [];
	let journalDays = 30;
	let fills: DeskFill[] = [];
	let market: MarketContext | null = null;
	let jobs: SchedulerJobSummary[] = [];
	let equity: ForvenEquityHistory | null = null;
	let archived: LifecycleStrategy[] = [];
	let archivedLoaded = false;
	let archivedLoading = false;
	let archivedDetail: { strategy: LifecycleStrategy; events: LifecycleEvent[] } | null = null;
	let archivedDetailLoading = false;
	let sessionsLoaded = false;
	let loadError: string | null = null;
	let actionError: string | null = null;
	let actionNotice: string | null = null;
	let selectedId: string | null = null;
	let railFilter: RailFilter = 'all';
	let sideTab: SideTab = 'position';
	let blotTab: BlotTab = 'fills';
	let blotScope: BlotScope = 'all';
	let busy = false;
	let confirmSpec: ConfirmSpec | null = null;
	let confirmBusy = false;
	let clock = Date.now();
	let rowsNow = Date.now();
	let prices: Record<string, number> = {};
	let chartAtr: number | null = null;
	let chartRefresh = 0;
	let preselect: string | null = null;
	let fleetAttempted = false;
	let initialPicked = false;
	let stopTimers: Array<() => void> = [];
	let clockTimer: ReturnType<typeof setInterval> | null = null;
	let eventTimer: ReturnType<typeof setTimeout> | null = null;
	let unsubscribePrices: (() => void) | null = null;

	$: dash = $forvenDashboard ?? dashboard;
	$: risk = $forvenRisk;
	$: statsMap = statsByStrategy(fills);
	$: rows = sortRows(buildRows({ mode, sessions, fleet, stats: statsMap, prices, now: rowsNow }));
	$: selected = rows.find((row) => row.session.id === selectedId) ?? null;
	$: selectedSid = selected?.sid ?? null;
	$: expectations = Object.fromEntries(rows.map((row) => [row.sid, row.expectation]));
	$: attention = buildAttention({ mode, dashboard: dash, risk, fleet, journal, expectations, now: rowsNow });
	$: selectedRefusals = selectedSid ? journal.filter((event) => event.strategy_id === selectedSid && (event.kind === 'entry_refused' || event.kind === 'exit_refused')) : [];
	$: allLegs = rows.flatMap((row) => row.legs.map((leg, index) => ({ leg, math: row.legMath[index] })));
	$: openPnl = allLegs.reduce((sum, entry) => sum + (entry.math?.pnl ?? 0), 0);
	$: openLong = allLegs.filter((entry) => entry.leg.side === 'long').length;
	$: openShort = allLegs.filter((entry) => entry.leg.side === 'short').length;
	$: riskAtStops = allLegs.reduce((sum, entry) => sum + (entry.math?.risk ?? 0), 0);
	$: maxBookRiskPct = rows.reduce<number | null>((max, row) => {
		const capital = num(row.session.capital);
		if (!capital || !row.legs.length) return max;
		const pct = (row.legMath.reduce((sum, math) => sum + (math.risk ?? 0), 0) / capital) * 100;
		return max === null || pct > max ? pct : max;
	}, null);
	$: selectedMarket = selected ? market?.assets?.[selected.asset] ?? null : null;
	$: selectedLivePrice = selected ? livePrice(prices, selected.session.symbol) : null;
	$: budget = risk?.portfolio_budget_live ?? null;
	$: sliceUsd = num(budget?.capital_slice?.slice_usd);
	$: profile = (selected?.session.decision_params ?? selected?.session.params ?? {}).execution_profile as Record<string, unknown> | undefined;
	$: ticketEquity = mode === 'live' ? num(dash?.account?.accountValue) : num(selected?.session.capital);
	$: ticketLimits = mode === 'live' && budget
		? {
			perTradeRiskPct: num(budget.limits_pct?.live_hard_max_per_trade_risk_pct) ?? 2,
			orderNotionalPct: num(budget.limits_pct?.live_hard_max_order_notional_pct) ?? 100,
			openRiskUsed: num(budget.total_open_risk_usd) ?? 0,
			openRiskLimit: num(budget.total_open_risk_limit_usd),
		}
		: undefined;
	$: walletFree = {
		long: freeMargin('long'),
		short: freeMargin('short'),
	};
	$: gatesBlocking = mode === 'live' ? blockingGates(dash).map((gate) => gate.name) : [];
	$: setPageContext({
		summary: selected
			? `${mode === 'live' ? 'Live Trading' : 'Paper Trading'}: ${selected.sid} on ${selected.session.symbol} (${selected.state.replace(/_/g, ' ')})`
			: archivedDetail
				? `Paper Trading: archived ${archivedDetail.strategy.display_id || archivedDetail.strategy.id}`
				: mode === 'live' ? 'Live Trading' : 'Paper Trading',
	});

	function freeMargin(side: 'long' | 'short'): number | null {
		const book = budget?.per_book?.[side];
		const limit = num(book?.limit_usd);
		return limit === null ? null : limit - (num(book?.margin_usd) ?? 0);
	}

	// ---- loading ----

	async function loadSessions(): Promise<void> {
		if (busy) return; // a returning action carries the truth; don't let a poll clobber it
		try {
			const next = await getPaperSessions(mode === 'live' ? { includeDeployed: true, onlyDeployed: true } : {});
			sessions = next;
			loadError = null;
			rowsNow = Date.now();
			if (!sessionsLoaded) {
				sessionsLoaded = true;
				void loadMarket();
			} else if (selectedId && !next.some((session) => session.id === selectedId) && !archivedDetail) {
				selectedId = sortRows(buildRows({ mode, sessions: next, fleet, stats: statsMap, prices, now: rowsNow }))[0]?.session.id ?? null;
			}
		} catch (error) {
			if (!sessionsLoaded) loadError = error instanceof Error ? error.message : 'Could not load the strategies.';
		}
	}

	async function loadStatus(): Promise<void> {
		const [d, r] = await Promise.allSettled([getForvenDashboard(), getForvenRisk()]);
		if (d.status === 'fulfilled' && d.value) forvenDashboard.set(d.value);
		if (r.status === 'fulfilled' && r.value) forvenRisk.set(r.value);
	}

	async function loadFleet(): Promise<void> {
		try {
			fleet = await getFleet(mode);
		} catch {
			// Keep the last good scorecard through a transient miss.
		} finally {
			fleetAttempted = true;
		}
	}

	async function loadJournal(): Promise<void> {
		try {
			const result = await getJournal(mode, { days: journalDays, limit: 600 });
			journal = result.events;
		} catch {
			/* keep the previous journal */
		}
	}

	async function loadFills(): Promise<void> {
		try {
			fills = (await getDeskFills(mode, 1000)).fills;
		} catch {
			/* keep the previous fills */
		}
	}

	async function loadMarket(): Promise<void> {
		const assets = [...new Set(sessions.map((session) => String(session.symbol ?? '').toUpperCase().split(/[/:_-]/)[0]).filter(Boolean))];
		if (!assets.length) return;
		try {
			market = await getMarketContext(assets, 72);
		} catch {
			/* funding and OI are optional context */
		}
	}

	async function loadSlow(): Promise<void> {
		const tasks: Array<Promise<unknown>> = [loadJournal(), loadFills(), loadMarket()];
		tasks.push(getSchedulerJobs().then((result) => { jobs = result; }).catch(() => undefined));
		if (mode === 'live' && blotTab === 'performance') tasks.push(loadEquity());
		await Promise.allSettled(tasks);
	}

	async function loadEquity(): Promise<void> {
		try {
			equity = await getForvenEquityHistory();
		} catch {
			/* optional */
		}
	}

	async function loadArchived(): Promise<void> {
		if (archivedLoaded || archivedLoading) return;
		archivedLoading = true;
		try {
			const list = await listLifecycleStrategies({ state: 'archived', limit: 200, offset: 0 });
			archived = [...list].sort((a, b) => (parseTs(b.updated_at ?? b.created_at) ?? 0) - (parseTs(a.updated_at ?? a.created_at) ?? 0));
			archivedLoaded = true;
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Could not load archived strategies.';
		} finally {
			archivedLoading = false;
		}
	}

	async function openArchived(strategy: LifecycleStrategy): Promise<void> {
		archivedDetail = { strategy, events: [] };
		archivedDetailLoading = true;
		try {
			const detail = await getLifecycleStrategy(strategy.id);
			if (archivedDetail?.strategy.id === strategy.id) archivedDetail = { strategy: detail.strategy, events: detail.events };
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Could not load that strategy.';
		} finally {
			archivedDetailLoading = false;
		}
	}

	$: if (railFilter === 'archived' && mode === 'paper') void loadArchived();
	// Pick the first selection once sessions and the scorecard are in, so a stuck
	// or blocked strategy leads rather than whichever id sorts first.
	$: if (!initialPicked && sessionsLoaded && fleetAttempted) {
		initialPicked = true;
		pickInitialSelection();
	}
	$: if (blotTab === 'performance' && mode === 'live' && !equity) void loadEquity();

	// ---- selection ----

	function pickInitialSelection(): void {
		const sorted = sortRows(buildRows({ mode, sessions, fleet, stats: statsMap, prices, now: rowsNow }));
		let stored: string | null = null;
		try {
			stored = window.localStorage.getItem(STORAGE_KEY);
		} catch {
			stored = null;
		}
		const wanted = preselect ?? stored;
		const match = wanted
			? sorted.find((row) => row.session.id === wanted || row.sid === wanted)
			: null;
		selectedId = match?.session.id ?? sorted[0]?.session.id ?? null;
		if (preselect && match) writeStored(match.session.id);
		preselect = null;
		if (sorted.some((row) => row.legs.length)) blotTab = 'positions';
	}

	function writeStored(id: string | null): void {
		try {
			if (id) window.localStorage.setItem(STORAGE_KEY, id);
			else window.localStorage.removeItem(STORAGE_KEY);
		} catch {
			/* storage can be blocked */
		}
	}

	function selectSession(id: string, tab?: SideTab): void {
		selectedId = id;
		archivedDetail = null;
		if (railFilter === 'archived') railFilter = 'all';
		if (tab) sideTab = tab;
		writeStored(id);
	}

	function selectStrategy(strategyId: string, tab?: SideTab): void {
		const row = rows.find((candidate) => candidate.sid === strategyId);
		if (row) selectSession(row.session.id, tab);
	}

	async function handleSelectEvent(event: Event): Promise<void> {
		const sessionId = String((event as CustomEvent<{ sessionId?: string }>).detail?.sessionId ?? '').trim();
		if (!sessionId) return;
		if (!sessions.some((session) => session.id === sessionId)) await loadSessions();
		if (sessions.some((session) => session.id === sessionId)) selectSession(sessionId);
	}

	function handleTradeEvent(): void {
		if (eventTimer) return;
		eventTimer = setTimeout(() => {
			eventTimer = null;
			void Promise.allSettled([loadSessions(), loadFills(), loadJournal(), loadFleet()]);
			chartRefresh += 1;
		}, 900);
	}

	// ---- actions ----

	function updateSession(next: PaperTradingSession): void {
		sessions = sessions.map((session) => (session.id === next.id ? next : session));
		rowsNow = Date.now();
	}

	async function runAction(label: string, fn: () => Promise<unknown>, notice: string): Promise<void> {
		busy = true;
		actionError = null;
		actionNotice = null;
		try {
			const result = await fn();
			if (result && typeof result === 'object' && 'id' in (result as Record<string, unknown>) && 'symbol' in (result as Record<string, unknown>)) {
				updateSession(result as PaperTradingSession);
			}
			actionNotice = notice;
		} catch (error) {
			actionError = `${label} failed: ${error instanceof Error ? error.message : 'unknown error'}`;
		} finally {
			busy = false;
		}
		chartRefresh += 1;
		void Promise.allSettled([loadSessions(), loadFills(), loadJournal(), loadFleet(), mode === 'live' ? loadStatus() : Promise.resolve()]);
	}

	function openConfirm(spec: ConfirmSpec): void {
		actionError = null;
		confirmSpec = spec;
	}

	async function confirmNow(): Promise<void> {
		if (!confirmSpec) return;
		confirmBusy = true;
		try {
			await confirmSpec.run();
		} finally {
			confirmBusy = false;
			confirmSpec = null;
		}
	}

	const liveWarn = (text: string) => (mode === 'live' ? `Live on Hyperliquid ${dash?.account?.network ?? 'mainnet'}. ${text}` : `Paper: ${text}`);

	function positionAction(sessionId: string, action: PositionAction): void {
		const row = rows.find((candidate) => candidate.session.id === sessionId);
		const leg = row?.legs[0];
		const math = row?.legMath[0];
		if (!row || !leg || !math) return;
		const label = `${cap1(leg.side)} ${fmtQty(leg.size)} ${row.asset}`;
		const est = (fraction: number) => `${fmtUsd(math.pnl * fraction, { signed: true })} at ${fmtPx(math.mark)}`;
		const fill = mode === 'live' ? 'Market, reduce-only' : 'Paper fill at the mid';
		switch (action.kind) {
			case 'close':
				openConfirm({
					title: `Close ${leg.side} ${row.asset}`,
					warn: liveWarn(mode === 'live' ? 'This sends a real reduce-only market order.' : 'the simulated position closes at the current mid.'),
					rows: [['Strategy', row.sid], ['Order', `${fill}, ${fmtQty(leg.size)} ${row.asset}`], ['Estimated P&L', est(1)]],
					cta: 'Close position',
					danger: true,
					run: () => runAction('Close', () => closePaperPosition(sessionId), `Closed ${label}.`),
				});
				break;
			case 'partial': {
				const pct = action.pct ?? 50;
				openConfirm({
					title: `Close ${pct}% of ${leg.side} ${row.asset}`,
					warn: liveWarn(mode === 'live' ? 'This sends a real reduce-only market order.' : 'part of the simulated position closes at the mid.'),
					rows: [['Order', `${fill}, ${fmtQty((leg.size * pct) / 100)} ${row.asset}`], ['Estimated P&L', est(pct / 100)], ['Left open', `${fmtQty(leg.size * (1 - pct / 100))} ${row.asset}`]],
					cta: `Close ${pct}%`,
					danger: true,
					run: () => runAction('Partial close', () => partialClosePaperPosition(sessionId, { pct }), `Closed ${pct}% of ${label}.`),
				});
				break;
			}
			case 'breakeven':
				openConfirm({
					title: 'Move the stop to entry',
					warn: liveWarn(mode === 'live' ? 'This replaces the resting stop order on Hyperliquid.' : 'the simulated stop moves.'),
					rows: [['Stop', `${fmtPx(leg.stop)} → ${fmtPx(leg.entry)}`], ['Loss if it fills', `${math.risk !== null ? fmtUsd(-math.risk) : 'no stop'} → about $0 before fees`]],
					cta: 'Move stop',
					run: () => runAction('Stop move', () => adjustPaperStopLoss(sessionId, leg.entry), `Stop moved to entry on ${row.sid}.`),
				});
				break;
			case 'flip': {
				const opposite = leg.side === 'long' ? 'short' : 'long';
				openConfirm({
					title: `Flip ${row.asset} to ${opposite}`,
					warn: liveWarn(mode === 'live' ? 'This closes the position and opens the opposite side with real money.' : 'the simulated position closes and the opposite side opens at the mid.'),
					rows: [['Step 1', `Close ${label} (${est(1)})`], ['Step 2', `Open ${opposite} ${fmtQty(leg.size)} ${row.asset}`], ['Stop', 'The new side needs its own stop']],
					cta: `Flip to ${opposite}`,
					danger: true,
					run: () => runAction('Flip', () => flipPaperPosition(sessionId), `Flipped ${row.sid} to ${opposite}.`),
				});
				break;
			}
			case 'pause':
			case 'resume': {
				const paused = action.kind === 'pause';
				openConfirm({
					title: paused ? 'Pause auto-management' : 'Resume auto-management',
					warn: 'No order is placed.',
					rows: [['Effect', paused ? 'The strategy stops managing this position; you own the exit' : 'The strategy manages the exit again'], ['Stop', 'Stays where it is']],
					cta: paused ? 'Pause' : 'Resume',
					run: () => runAction('Auto-management', () => setPaperAutoManagement(sessionId, paused), paused ? `${row.sid} is yours to manage.` : `${row.sid} is back under the strategy.`),
				});
				break;
			}
			case 'setStop':
			case 'clearStop': {
				const price = action.kind === 'setStop' ? action.price ?? null : null;
				openConfirm({
					title: price !== null ? 'Move the stop' : 'Remove the stop',
					warn: liveWarn(mode === 'live' ? 'This replaces the resting stop order on Hyperliquid.' : 'the simulated stop changes.'),
					rows: [['Stop', `${fmtPx(leg.stop)} → ${price !== null ? fmtPx(price) : 'none'}`], ['Loss if it fills', price !== null ? fmtUsd(-Math.abs(leg.entry - price) * leg.size) : 'unbounded']],
					cta: price !== null ? 'Move stop' : 'Remove stop',
					danger: price === null,
					run: () => runAction('Stop change', () => adjustPaperStopLoss(sessionId, price), price !== null ? `Stop set to ${fmtPx(price)} on ${row.sid}.` : `Stop removed on ${row.sid}.`),
				});
				break;
			}
			case 'setTarget':
			case 'clearTarget': {
				const price = action.kind === 'setTarget' ? action.price ?? null : null;
				openConfirm({
					title: price !== null ? 'Set a target' : 'Remove the target',
					warn: liveWarn(mode === 'live' ? 'This rests a reduce-only take-profit on Hyperliquid.' : 'the simulated target changes.'),
					rows: [['Target', `${leg.takeProfit !== null ? fmtPx(leg.takeProfit) : 'none'} → ${price !== null ? fmtPx(price) : 'none'}`]],
					cta: price !== null ? 'Set target' : 'Remove target',
					run: () => runAction('Target change', () => adjustPaperTakeProfit(sessionId, price), price !== null ? `Target set to ${fmtPx(price)} on ${row.sid}.` : `Target removed on ${row.sid}.`),
				});
				break;
			}
		}
	}

	function reviewOrder(review: TicketReview): void {
		if (!selected) return;
		const row = selected;
		const result = review.result;
		const options = review.options;
		openConfirm({
			title: `Open ${review.direction} ${row.asset}`,
			warn: liveWarn(mode === 'live'
				? `This places a real market order with real money, then rests a reduce-only stop at ${fmtPx(review.stop)}.`
				: 'a simulated position opens at the current mid; no real order is sent.'),
			rows: [
				['Strategy', `${row.sid} · ${row.name}`],
				['Size', options.riskPct !== undefined ? `${fmtPct(options.riskPct, 2, false)} risk, about ${fmtQty(result.size)} ${row.asset}` : `${fmtQty(options.size)} ${row.asset}`],
				['Order value', `about ${fmtUsd(result.notional)}`],
				['Leverage · margin', `${result.leverage}× · ${fmtUsd(result.margin)}`],
				['Stop · loss if it fills', review.stop !== null ? `${fmtPx(review.stop)} · ${fmtUsd(result.riskUsd !== null ? -result.riskUsd : null)}` : 'none'],
				['Target', review.takeProfit !== null ? `${fmtPx(review.takeProfit)}${result.rr !== null ? ` · ${result.rr.toFixed(2)}R` : ''}` : 'strategy exit'],
				...(mode === 'live' ? [['Wallet', `${cap1(review.direction)} wallet`] as [string, string]] : []),
			],
			cta: mode === 'live' ? `Place live ${review.direction}` : `Open paper ${review.direction}`,
			danger: mode === 'live',
			run: () => runAction('Open', () => openManualPaperPosition(row.session.id, options), `Opened ${review.direction} ${row.asset} on ${row.sid}.`),
		});
	}

	function saveCeiling(value: number): void {
		if (!selected) return;
		const row = selected;
		openConfirm({
			title: `Set ${row.sid}'s go-live ceiling`,
			warn: `Live opens above ${fmtUsd(value)} will be refused, not downsized.`,
			rows: [['Current', num(row.session.live_notional_ceiling_usd) !== null ? fmtUsd(row.session.live_notional_ceiling_usd) : 'not set'], ['New', fmtUsd(value)]],
			cta: 'Save ceiling',
			run: () => runAction('Ceiling', async () => {
				await setLiveNotionalCeiling(row.sid, value);
				updateSession({ ...row.session, live_notional_ceiling_usd: value });
			}, `Ceiling for ${row.sid} set to ${fmtUsd(value)}.`),
		});
	}

	// ---- keyboard ----

	function onKey(event: KeyboardEvent): void {
		if (confirmSpec) return;
		const target = event.target as HTMLElement | null;
		const tag = (target?.tagName ?? '').toLowerCase();
		if (tag === 'input' || tag === 'textarea' || tag === 'select' || target?.isContentEditable || event.metaKey || event.ctrlKey || event.altKey) return;
		const visible = rows;
		const index = visible.findIndex((row) => row.session.id === selectedId);
		if (event.key === 'ArrowDown' || event.key === 'j') {
			const next = visible[Math.min(visible.length - 1, index + 1)];
			if (next) { event.preventDefault(); selectSession(next.session.id); }
		} else if (event.key === 'ArrowUp' || event.key === 'k') {
			const next = visible[Math.max(0, index - 1)];
			if (next) { event.preventDefault(); selectSession(next.session.id); }
		} else if (['1', '2', '3', '4'].includes(event.key)) {
			sideTab = SIDE_TABS[Number(event.key) - 1][0];
		} else if (event.key === ']' || event.key === '[') {
			const at = BLOT_TABS.indexOf(blotTab);
			blotTab = BLOT_TABS[(at + (event.key === ']' ? 1 : BLOT_TABS.length - 1)) % BLOT_TABS.length];
		} else if (event.key === 'a') {
			blotScope = blotScope === 'all' ? 'strategy' : 'all';
		}
	}

	// ---- lifecycle ----

	onMount(() => {
		preselect = $page.url.searchParams.get('select');
		if (preselect) {
			const url = new URL(window.location.href);
			url.searchParams.delete('select');
			window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
		}
		unsubscribePrices = forvenLivePrices.subscribe((map) => {
			prices = map ?? {};
		});
		window.addEventListener('forven:event', handleTradeEvent);
		window.addEventListener('forven:select-session', handleSelectEvent);
		clockTimer = setInterval(() => (clock = Date.now()), 1000);
		// First load runs even in a background tab; the refresh cadence then pauses
		// while the tab is hidden and catches up when it is shown again.
		void Promise.allSettled([loadFast(), loadFleet(), loadSlow()]);
		stopTimers = [every(15_000, loadFast), every(30_000, loadFleet), every(60_000, loadSlow)];
		document.addEventListener('visibilitychange', onVisible);
	});

	async function loadFast(): Promise<void> {
		await Promise.allSettled([loadSessions(), loadStatus()]);
	}

	function every(ms: number, fn: () => Promise<void>): () => void {
		let inFlight = false;
		const id = setInterval(async () => {
			if (document.hidden || inFlight) return;
			inFlight = true;
			try {
				await fn();
			} finally {
				inFlight = false;
			}
		}, ms);
		return () => clearInterval(id);
	}

	function onVisible(): void {
		if (!document.hidden) void Promise.allSettled([loadFast(), loadFleet()]);
	}

	onDestroy(() => {
		stopTimers.forEach((stop) => stop());
		unsubscribePrices?.();
		if (clockTimer) clearInterval(clockTimer);
		if (eventTimer) clearTimeout(eventTimer);
		if (typeof window !== 'undefined') {
			window.removeEventListener('forven:event', handleTradeEvent);
			window.removeEventListener('forven:select-session', handleSelectEvent);
			document.removeEventListener('visibilitychange', onVisible);
		}
	});

	const tabClass = (on: boolean) => `whitespace-nowrap border-b-2 px-2.5 pb-2.5 pt-2 text-[13px] ${on ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink2'}`;
</script>

<svelte:window on:keydown={onKey} />

<div class="flex h-full min-h-0 flex-col overflow-hidden bg-sc-bg" data-testid={`trading-desk-${mode}`}>
	<div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4 py-3">
		<div class="mx-auto grid max-w-[1680px] gap-2.5">
			<header class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
				<div class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
					<h1 class="text-[22px] font-semibold text-sc-ink">{mode === 'live' ? 'Live trades' : 'Paper trades'}</h1>
					<span class="text-[12.5px] text-sc-ink3">{mode === 'live' ? 'Strategies trading real money on Hyperliquid. Orders placed here are real.' : 'Strategies trading simulated books at the live mid. Nothing here sends a real order.'}</span>
				</div>
				<a href="/all-trades" data-sveltekit-preload-data="hover" class="text-[12.5px] text-sc-ink2 hover:text-sc-ink">All trades →</a>
			</header>

			{#if actionError}
				<div class="flex items-start justify-between gap-3 rounded-md border border-[#e5574f]/45 bg-[#e5574f]/10 px-3 py-2 text-[12.5px] text-[#f3b1ab]" role="alert">
					<span>{actionError}</span>
					<button type="button" class="text-[12px] underline underline-offset-2" on:click={() => (actionError = null)}>Dismiss</button>
				</div>
			{:else if actionNotice}
				<div class="flex items-start justify-between gap-3 rounded-md border border-[#3cc48f]/40 bg-[#3cc48f]/10 px-3 py-2 text-[12.5px] text-[#9fe3c4]" role="status">
					<span>{actionNotice}</span>
					<button type="button" class="text-[12px] underline underline-offset-2" on:click={() => (actionNotice = null)}>Dismiss</button>
				</div>
			{/if}

			<DeskStatusLine {mode} dashboard={dash} {jobs} wsConnected={$forvenWsConnected} now={clock} />
			<DeskAccountStrip {mode} dashboard={dash} {risk} {fleet} {sessions} {openPnl} openLegs={allLegs.length} {openLong} {openShort} {riskAtStops} {maxBookRiskPct} now={clock} />
			<DeskAttention items={attention} on:show={(event) => selectStrategy(event.detail.strategyId, event.detail.tab)} />

			<div class="grid gap-2.5 lg:grid-cols-[272px_minmax(0,1fr)] 2xl:grid-cols-[292px_minmax(0,1fr)_372px]">
				<DeskStrategyRail
					{mode}
					{rows}
					selectedId={archivedDetail ? null : selectedId}
					filter={railFilter}
					{archived}
					{archivedLoading}
					selectedArchivedId={archivedDetail?.strategy.id ?? null}
					now={clock}
					on:select={(event) => selectSession(event.detail)}
					on:filter={(event) => (railFilter = event.detail)}
					on:selectArchived={(event) => void openArchived(event.detail)}
				/>

				{#if archivedDetail}
					<section class="grid min-h-[440px] place-items-center rounded-md border border-sc-line bg-sc-panel px-6 text-center text-[12.5px] text-sc-ink3">
						Archived strategies have no live chart. Their history is in the panel{archivedDetail ? ` for ${archivedDetail.strategy.display_id || archivedDetail.strategy.id}` : ''}.
					</section>
				{:else if selected}
					<DeskChart
						session={selected.session}
						name={selected.name}
						legs={selected.legs}
						refusals={selectedRefusals}
						market={selectedMarket}
						asset={selected.asset}
						livePrice={selectedLivePrice}
						refreshToken={chartRefresh}
						now={clock}
						bind:atr={chartAtr}
					/>
				{:else}
					<section class="grid min-h-[440px] place-items-center rounded-md border border-sc-line bg-sc-panel px-6 text-center text-[12.5px] text-sc-ink3">
						{#if loadError}
							Could not load the strategies: {loadError}
						{:else if !sessionsLoaded}
							Loading {mode === 'live' ? 'live' : 'paper'} strategies…
						{:else}
							{mode === 'live' ? 'No strategy is trading real money. Promote one from paper to see it here.' : 'No strategy is in paper right now. Move one into paper to watch it here.'}
						{/if}
					</section>
				{/if}

				<aside class="flex min-w-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel lg:col-span-2 2xl:col-span-1" aria-label="Strategy panel">
					{#if archivedDetail}
						<div class="border-b border-sc-line px-3 py-2 text-[13px] font-semibold text-sc-ink">Archived</div>
						<div class="grid content-start gap-3 overflow-y-auto p-3 2xl:max-h-[820px]">
							<DeskArchivedPanel strategy={archivedDetail.strategy} events={archivedDetail.events} loading={archivedDetailLoading} />
						</div>
					{:else}
						<div class="flex gap-0.5 overflow-x-auto border-b border-sc-line px-1.5" role="tablist" aria-label="Strategy panel">
							{#each SIDE_TABS as [key, text]}
								<button type="button" role="tab" class={tabClass(sideTab === key)} aria-selected={sideTab === key} on:click={() => (sideTab = key)}>{text}</button>
							{/each}
						</div>
						<div class="grid content-start gap-3 overflow-y-auto p-3 2xl:max-h-[820px]">
							{#if !selected}
								<p class="py-6 text-center text-[12.5px] text-sc-ink3">Select a strategy.</p>
							{:else if sideTab === 'position'}
								{#if selected.legs.length}
									{#each selected.legs as leg, index (leg.id || index)}
										<DeskPositionCard
											{mode}
											{leg}
											math={selected.legMath[index]}
											asset={selected.asset}
											timeframe={selected.timeframe}
											leverage={num(selected.session.leverage) ?? 1}
											market={selectedMarket}
											{busy}
											legIndex={index}
											legCount={selected.legs.length}
											on:action={(event) => selected && positionAction(selected.session.id, event.detail)}
										/>
									{/each}
								{:else}
									<div class="grid gap-1 rounded-md border border-sc-line bg-sc-panel2 px-3 py-2.5">
										<span class="font-semibold text-sc-ink">Flat · no {mode} position</span>
										<span class="text-[12px] text-sc-ink3">Watching for {selected.session.trade_mode === 'short_only' ? 'a short' : selected.session.trade_mode === 'long_only' ? 'a long' : 'a long or short'}. The strategy acts on {selected.timeframe} bar closes.</span>
									</div>
									<DeskOrderTicket
										{mode}
										strategyId={selected.sid}
										asset={selected.asset}
										tradeMode={selected.session.trade_mode}
										price={selectedLivePrice ?? (num(selected.session.current_price) || null)}
										equity={ticketEquity}
										takerBps={num(selected.session.taker_fee_bps) ?? 4.5}
										validatedLeverage={num(selected.session.leverage) ?? 1}
										riskPerTrade={num(profile?.risk_per_trade)}
										{sliceUsd}
										atr={chartAtr}
										atrMultiplier={num(profile?.atr_stop_multiplier) ?? 2}
										ceilingUsd={num(selected.session.live_notional_ceiling_usd)}
										{walletFree}
										limits={ticketLimits}
										{gatesBlocking}
										{busy}
										on:review={(event) => reviewOrder(event.detail)}
									/>
								{/if}
							{:else if sideTab === 'why'}
								<DeskWhyPanel {mode} row={selected} dashboard={dash} {risk} {fleet} refusals={selectedRefusals} now={rowsNow} />
							{:else if sideTab === 'expect'}
								<DeskExpectationPanel {mode} row={selected} />
							{:else}
								<DeskDetailsPanel {mode} row={selected} {sliceUsd} {busy} on:saveCeiling={(event) => saveCeiling(event.detail)} />
							{/if}
						</div>
					{/if}
				</aside>
			</div>

			<DeskBlotter
				{mode}
				{rows}
				{fills}
				{journal}
				{journalDays}
				{fleet}
				{risk}
				{equity}
				{selectedSid}
				selectedSessionId={selectedId}
				tab={blotTab}
				scope={blotScope}
				{busy}
				now={rowsNow}
				on:tab={(event) => (blotTab = event.detail)}
				on:scope={(event) => (blotScope = event.detail)}
				on:select={(event) => selectSession(event.detail)}
				on:close={(event) => { selectSession(event.detail, 'position'); positionAction(event.detail, { kind: 'close' }); }}
			/>

			<p class="pb-2 text-[11.5px] text-sc-ink3">Keys: ↑ ↓ strategies · 1–4 panel tabs · [ ] blotter tabs · A blotter scope.</p>
		</div>
	</div>
</div>

{#if confirmSpec}
	<DeskConfirmDialog spec={confirmSpec} busy={confirmBusy} live={mode === 'live'} on:cancel={() => { if (!confirmBusy) confirmSpec = null; }} on:confirm={confirmNow} />
{/if}
