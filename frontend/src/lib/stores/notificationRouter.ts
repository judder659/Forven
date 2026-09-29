/**
 * Notification router: the one place realtime WS events become pop-ups.
 *
 * Each event maps to a notification catalog category; notify() then applies
 * the operator's switches (Settings → Notifications) and the snooze. The
 * sidebar badges are not touched here: they are real counts from the
 * heartbeat, which refreshes on the same events.
 *
 * One source per fact, so nothing pops up twice:
 *  - fills come from the open-trade diff (`trade` with opened/closed arrays;
 *    activity-log rows that also arrive as `trade` are ignored), not from the
 *    backend's trade_opened/closed notification rows;
 *  - approvals come from the pending-approval diff (`approval_created`), not
 *    from approval notification rows;
 *  - kill switch / loss halts come from the risk-state diff;
 *  - everything else the backend stores arrives as a `notification` event
 *    tagged with its category.
 */
import { notify, type ToastType } from '$lib/stores/toasts';

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

interface BackendNotification {
	id?: number;
	event_type?: string;
	category?: string | null;
	severity?: string;
	title?: string;
	summary?: string | null;
	metadata?: Record<string, unknown>;
}

/** Above this many simultaneous fills of one kind (e.g. a kill-switch mass
 *  close), collapse them into one pop-up instead of flooding the stack. */
const MAX_INDIVIDUAL_TRADE_TOASTS = 3;
const TRADE_TOAST_MS = 6_000;
const APPROVAL_TOAST_MS = 10_000;
const RISK_TOAST_MS = 10_000;

/** Backend categories whose pop-ups come from a realtime diff instead. */
const DIFF_SOURCED_CATEGORIES = new Set([
	'live_trade_opened',
	'live_trade_closed',
	'paper_trade_opened',
	'paper_trade_closed',
	'bot_trades',
	'approval_required',
	'approval_resolved',
	'risk_halt',
]);

type TradeKind = 'live' | 'paper' | 'bot';

function tradeKind(trade: TradeSummary): TradeKind {
	if (String(trade.source ?? '').startsWith('bot:')) return 'bot';
	const executionType = String(trade.execution_type ?? '').toLowerCase();
	if (executionType === 'live') return 'live';
	// Exchange-only rows (a position found on the venue) carry no execution type.
	if (!executionType && String(trade.source ?? '').toLowerCase() === 'exchange') return 'live';
	return 'paper';
}

function tradeCategory(kind: TradeKind, side: 'opened' | 'closed'): string {
	if (kind === 'bot') return 'bot_trades';
	return `${kind}_trade_${side}`;
}

function tradeRoute(kind: TradeKind): string {
	if (kind === 'bot') return '/bot-factory';
	return kind === 'live' ? '/live-trades' : '/paper-trades';
}

const KIND_LABEL: Record<TradeKind, string> = { live: 'Live', paper: 'Paper', bot: 'Bot' };

function tradeHeadline(trade: TradeSummary): string {
	const direction = String(trade.direction ?? '').toUpperCase();
	return `${direction} ${trade.asset ?? '?'}`.trim();
}

function asTrades(value: unknown): TradeSummary[] {
	return (Array.isArray(value) ? value : []).filter(
		(item): item is TradeSummary => Boolean(item && typeof item === 'object'),
	);
}

function notifyFills(trades: TradeSummary[], side: 'opened' | 'closed'): void {
	const byKind = new Map<TradeKind, TradeSummary[]>();
	for (const trade of trades) {
		const kind = tradeKind(trade);
		byKind.set(kind, [...(byKind.get(kind) ?? []), trade]);
	}

	for (const [kind, group] of byKind) {
		const category = tradeCategory(kind, side);
		const href = tradeRoute(kind);
		if (group.length > MAX_INDIVIDUAL_TRADE_TOASTS) {
			const pnls = group.map((trade) => trade.pnl_pct).filter((pnl): pnl is number => typeof pnl === 'number' && Number.isFinite(pnl));
			notify({
				category,
				message: `${KIND_LABEL[kind]}: ${side} ${group.length} positions`,
				type: side === 'opened' ? 'success' : 'info',
				pnlPct: side === 'closed' && pnls.length ? pnls.reduce((sum, pnl) => sum + pnl, 0) / pnls.length : null,
				detail: side === 'closed' && pnls.length ? 'average P&L' : undefined,
				href,
				duration: TRADE_TOAST_MS,
			});
			continue;
		}
		for (const trade of group) {
			const pnl = typeof trade.pnl_pct === 'number' && Number.isFinite(trade.pnl_pct) ? trade.pnl_pct : null;
			notify({
				category,
				message: `${KIND_LABEL[kind]} trade ${side}: ${tradeHeadline(trade)}`,
				detail: trade.strategy ? String(trade.strategy) : undefined,
				// A loss is information, not an error: the P&L chip carries the color.
				type: side === 'opened' ? 'success' : 'info',
				pnlPct: side === 'closed' ? pnl : null,
				href,
				duration: TRADE_TOAST_MS,
			});
		}
	}
}

function handleTradeDiff(data: Record<string, unknown>): void {
	notifyFills(asTrades(data.opened), 'opened');
	notifyFills(asTrades(data.closed), 'closed');
}

function handleApprovalCreated(data: Record<string, unknown>): void {
	const reason = String(data.reason ?? '').trim();
	const approvalType = String(data.approval_type ?? '').trim().replace(/_/g, ' ');
	notify({
		category: 'approval_required',
		message: 'Approval needed',
		detail: reason || approvalType || 'An operator decision is waiting',
		type: 'warning',
		href: '/approval',
		duration: APPROVAL_TOAST_MS,
	});
}

function handleKillSwitch(data: Record<string, unknown>): void {
	// Only the state-diff payload carries the boolean; log-classified
	// "kill switch" text events don't and are skipped (no double pop-up).
	if (typeof data.kill_switch_active !== 'boolean') return;
	if (data.kill_switch_active) {
		notify({ category: 'risk_halt', message: 'Kill switch activated: trading halted', type: 'error', href: '/risk', critical: true });
	} else {
		notify({ category: 'risk_halt', message: 'Kill switch cleared', type: 'info', href: '/risk', duration: RISK_TOAST_MS });
	}
}

function handleRiskAlert(data: Record<string, unknown>): void {
	const kind = String(data.kind ?? '');
	if (kind === 'daily_loss_halt') {
		if (data.daily_loss_halt) {
			notify({ category: 'risk_halt', message: 'Daily loss halt: trading paused for today', type: 'error', href: '/risk', critical: true });
		} else {
			notify({ category: 'risk_halt', message: 'Daily loss halt cleared', type: 'info', href: '/risk', duration: RISK_TOAST_MS });
		}
	} else if (kind === 'drawdown_warning') {
		const drawdown = Number(data.drawdown_pct ?? 0);
		notify({
			category: 'risk_alerts',
			message: `Drawdown ${(drawdown * 100).toFixed(1)}% from the high-water mark`,
			type: 'warning',
			href: '/risk',
			duration: RISK_TOAST_MS,
		});
	}
	// kind === 'kill_switch' is already covered by kill_switch_activated/cleared.
}

const CATEGORY_ROUTE: Record<string, string> = {
	live_order_failure: '/live-trades',
	live_entry_blocked: '/live-trades',
	risk_alerts: '/risk',
	pipeline_transition: '/pipeline',
	test: '/settings#notifications',
};

function severityType(severity: string): ToastType {
	if (severity === 'critical' || severity === 'fail') return 'error';
	if (severity === 'warn') return 'warning';
	return 'info';
}

function handleBackendNotification(data: BackendNotification): void {
	const category = String(data.category ?? '');
	if (!category || DIFF_SOURCED_CATEGORIES.has(category)) return;
	const severity = String(data.severity ?? 'info');
	const title = String(data.title ?? '').trim() || 'Forven update';
	const summary = String(data.summary ?? '').trim();
	notify({
		category,
		message: title,
		detail: summary && summary !== title ? summary : undefined,
		type: category === 'system_recovered' || category === 'test' ? 'success' : severityType(severity),
		href: CATEGORY_ROUTE[category] ?? '/diagnostics',
		// A critical alert in a safety category gets through a snooze.
		critical: severity === 'critical' && (category === 'live_order_failure' || category === 'risk_alerts' || category === 'system_critical'),
	});
}

/** Who connected, from the activity row behind mcp_session_opened (its `data`
 *  column is a JSON string: {session_id, actor, label}). */
function sessionActor(entry: Record<string, unknown>): string {
	const raw = entry.data;
	try {
		const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
		const actor = parsed && typeof parsed === 'object' ? String((parsed as { actor?: unknown }).actor ?? '').trim() : '';
		if (actor) return actor;
	} catch {
		// Fall through to the message text.
	}
	const match = /\bopened by (.+)$/.exec(String(entry.message ?? ''));
	return match ? match[1].trim() : '';
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
		case 'notification':
			handleBackendNotification(data as BackendNotification);
			break;
		case 'mcp_session_opened': {
			const who = sessionActor(data);
			notify({
				category: 'mcp_session',
				message: 'AI client connected',
				detail: who || undefined,
				type: 'info',
				href: '/integrations',
			});
			break;
		}
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
