/**
 * Notification router — the single place realtime WS events become operator
 * notifications: a bottom-right toast (attention now) and/or a nav badge pulse
 * (attention when you get a moment; see navMetrics.navEventPulses).
 *
 * Listens to the `forven:event` CustomEvents that forvenWebSocket dispatches.
 * Only the state-diff shaped payloads are handled — the parallel activity-log
 * classified copies of the same facts are ignored so one fill / one flip never
 * notifies twice.
 */
import { get } from 'svelte/store';
import { addToast } from '$lib/stores/processTracker';
import { addNavPulse } from '$lib/stores/navMetrics';
import { pageContext } from '$lib/stores/pageContext';

interface TradeSummary {
	id?: string;
	display_id?: string;
	asset?: string;
	direction?: string;
	strategy?: string;
	execution_type?: string;
	source?: string;
	entry_price?: number | null;
	exit_price?: number | null;
	pnl_pct?: number | null;
	status?: string;
}

/** Above this many simultaneous fills (e.g. a kill-switch mass close), collapse
 *  into one aggregate toast instead of flooding the stack. */
const MAX_INDIVIDUAL_TRADE_TOASTS = 3;
const TRADE_TOAST_MS = 6_000;
const APPROVAL_TOAST_MS = 10_000;
const RISK_TOAST_MS = 10_000;

function isViewingRoute(href: string): boolean {
	const route = get(pageContext).route || '/';
	if (href === '/') return route === '/';
	return route === href || route.startsWith(`${href}/`);
}

/** Badge the tab unless the user is already looking at that page. */
function pulse(href: string, severity: 'info' | 'success' | 'warn' | 'danger', summary: string): void {
	if (isViewingRoute(href)) return;
	addNavPulse(href, severity, summary);
}

function tradeRoute(trade: TradeSummary): string {
	if (String(trade.source ?? '').startsWith('bot:')) return '/bot-factory';
	return String(trade.execution_type ?? '').toLowerCase() === 'live' ? '/live-trades' : '/paper-trades';
}

/** Landing page for an aggregate toast: the shared page when every fill routes
 *  the same way, otherwise the combined All Trades ledger. */
function aggregateTradeRoute(trades: TradeSummary[]): string {
	const routes = new Set(trades.map(tradeRoute));
	const only = routes.values().next().value;
	return routes.size === 1 && only ? only : '/all-trades';
}

function tradeLabel(trade: TradeSummary): string {
	const direction = String(trade.direction ?? '').toUpperCase();
	const asset = trade.asset ?? '?';
	const strategy = trade.strategy ? ` · ${trade.strategy}` : '';
	return `${direction} ${asset}${strategy}`;
}

function formatPnl(pnlPct: number | null | undefined): string {
	if (pnlPct === null || pnlPct === undefined || !Number.isFinite(pnlPct)) return '';
	const pct = pnlPct * 100;
	return ` ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

function handleTradeDiff(data: Record<string, unknown>): void {
	const opened = (Array.isArray(data.opened) ? data.opened : []).filter(
		(item): item is TradeSummary => Boolean(item && typeof item === 'object'),
	);
	const closed = (Array.isArray(data.closed) ? data.closed : []).filter(
		(item): item is TradeSummary => Boolean(item && typeof item === 'object'),
	);

	for (const trade of opened) {
		pulse(tradeRoute(trade), 'success', `Trade opened: ${tradeLabel(trade)}`);
	}
	for (const trade of closed) {
		pulse(tradeRoute(trade), 'danger', `Trade closed: ${tradeLabel(trade)}`);
	}

	// Paper-session opens already surface as the persistent "Position Open"
	// card (PositionAlertWidget) — a toast for the same fill would double-notify.
	// Live and bot fills have no card, so they toast.
	const openedNeedingToast = opened.filter(
		(trade) => trade.execution_type !== 'paper' || String(trade.source ?? '').startsWith('bot:'),
	);
	if (openedNeedingToast.length > MAX_INDIVIDUAL_TRADE_TOASTS) {
		addToast(`Opened ${openedNeedingToast.length} positions`, 'success', aggregateTradeRoute(openedNeedingToast), TRADE_TOAST_MS);
	} else {
		for (const trade of openedNeedingToast) {
			addToast(`Trade opened: ${tradeLabel(trade)}`, 'success', tradeRoute(trade), TRADE_TOAST_MS);
		}
	}

	if (closed.length > MAX_INDIVIDUAL_TRADE_TOASTS) {
		addToast(`Closed ${closed.length} positions`, 'info', aggregateTradeRoute(closed), TRADE_TOAST_MS);
	} else {
		for (const trade of closed) {
			const pnl = typeof trade.pnl_pct === 'number' ? trade.pnl_pct : null;
			addToast(
				`Trade closed: ${tradeLabel(trade)}${formatPnl(pnl)}`,
				pnl === null ? 'info' : pnl >= 0 ? 'success' : 'error',
				tradeRoute(trade),
				TRADE_TOAST_MS,
			);
		}
	}
}

function handleApprovalCreated(data: Record<string, unknown>): void {
	const reason = String(data.reason ?? '').trim();
	const approvalType = String(data.approval_type ?? '').trim().replace(/_/g, ' ');
	const label = reason || approvalType || 'operator decision needed';
	addToast(`Approval required: ${label}`, 'warning', '/approval', APPROVAL_TOAST_MS);
	pulse('/approval', 'warn', `Approval required: ${label}`);
}

function handleKillSwitch(data: Record<string, unknown>): void {
	// Only the state-diff payload carries the boolean; log-classified
	// "kill switch" text events don't and are skipped (no double toast).
	if (typeof data.kill_switch_active !== 'boolean') return;
	if (data.kill_switch_active) {
		addToast('Kill switch activated — trading halted', 'error', '/risk', RISK_TOAST_MS);
		pulse('/risk', 'danger', 'Kill switch activated');
	} else {
		addToast('Kill switch cleared', 'info', '/risk', RISK_TOAST_MS);
	}
}

function handleRiskAlert(data: Record<string, unknown>): void {
	const kind = String(data.kind ?? '');
	if (kind === 'daily_loss_halt') {
		if (data.daily_loss_halt) {
			addToast('Daily loss halt active — trading paused for today', 'error', '/risk', RISK_TOAST_MS);
			pulse('/risk', 'danger', 'Daily loss halt active');
		} else {
			addToast('Daily loss halt cleared', 'info', '/risk', RISK_TOAST_MS);
		}
	} else if (kind === 'drawdown_warning') {
		const drawdown = Number(data.drawdown_pct ?? 0);
		addToast(`Drawdown warning: ${(drawdown * 100).toFixed(1)}% from high-water mark`, 'warning', '/risk', RISK_TOAST_MS);
		pulse('/risk', 'warn', 'Drawdown warning');
	}
	// kind === 'kill_switch' is already toasted via kill_switch_activated/cleared.
}

function handleEvent(event: Event): void {
	const detail = (event as CustomEvent<Record<string, unknown>>).detail;
	if (!detail || typeof detail !== 'object') return;
	const data = (detail.data && typeof detail.data === 'object' ? detail.data : {}) as Record<string, unknown>;

	// Match on detail.type only: the `event`-envelope duplicates of the same
	// payloads have type 'event' and fall through, so nothing fires twice.
	switch (detail.type) {
		case 'trade':
			// Only the open-set diff shape ({opened, closed}); activity-log rows
			// with level 'trade' also arrive as type 'trade' but lack these arrays.
			if (Array.isArray(data.opened) || Array.isArray(data.closed)) {
				handleTradeDiff(data);
			}
			break;
		case 'approval_created':
			handleApprovalCreated(data);
			break;
		case 'kill_switch_activated':
		case 'kill_switch_cleared':
			handleKillSwitch(data);
			break;
		case 'risk_alert':
			handleRiskAlert(data);
			break;
		case 'strategy_promoted':
			// Lifecycle churn is frequent under autopilot — badge, don't toast.
			pulse('/lab', 'success', 'Strategy promoted');
			break;
		case 'task_failed':
			pulse('/tasks', 'danger', 'Task failed');
			break;
	}
}

let listener: ((event: Event) => void) | null = null;

export function startNotificationRouter(): void {
	if (typeof window === 'undefined' || listener) return;
	listener = handleEvent;
	window.addEventListener('forven:event', listener);
}

export function stopNotificationRouter(): void {
	if (typeof window === 'undefined' || !listener) return;
	window.removeEventListener('forven:event', listener);
	listener = null;
}
