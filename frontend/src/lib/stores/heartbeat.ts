import { getSystemHeartbeat } from '$lib/api';
import type { SystemNavIndicator } from '$lib/api';
import {
	forvenDashboard,
	forvenRisk,
	forvenSentiment,
	forvenRegime,
	forvenOpenTrades,
	forvenScannerState,
} from '$lib/stores/forven';
import { setNavIndicators } from '$lib/stores/navMetrics';
import { loadSlaCensus } from '$lib/stores/dataManager';
import { buildDataNavIndicator } from '$lib/components/data-manager/health';
import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';

let controller: RealtimeRefreshController | null = null;

// Events that change a sidebar count: fills, approvals, risk and task state,
// and new actionable notifications (the Diagnostics badge). Routine info
// notifications (agent task completions, ~250 a day) do not.
const BADGE_EVENTS = [
	'task_queued',
	'task_status_changed',
	'task_completed',
	'task_failed',
	'strategy_transition',
	'strategy_promoted',
	'kill_switch_activated',
	'kill_switch_cleared',
	'approval_created',
	'approval_resolved',
	'risk_alert',
	'trade',
	'notification',
];

// A slow poll even while the WebSocket is up: counts with no event of their
// own (bots started elsewhere, data going stale, issues acknowledged in another
// tab) still settle within two minutes instead of waiting for unrelated
// traffic. Kept slow on purpose: the heartbeat is a heavy composite (~3.5s,
// ~500KB on a busy instance) and the events above already refresh it on
// nearly every badge change.
const HEARTBEAT_POLL_MS = 120_000;

/** The /data badge: live and paper series past their freshness allowance, from
 * the SLA census (cached for a minute; null until the census endpoint exists). */
async function dataNavIndicator(): Promise<SystemNavIndicator | null> {
	try {
		const census = await loadSlaCensus({ maxAgeMs: 60_000 });
		return census ? buildDataNavIndicator(census) : null;
	} catch {
		return null;
	}
}

async function refreshHeartbeat(): Promise<void> {
	try {
		const [heartbeat, dataIndicator] = await Promise.all([getSystemHeartbeat(), dataNavIndicator()]);

		if (heartbeat.dashboard) forvenDashboard.set(heartbeat.dashboard);
		if (heartbeat.risk) forvenRisk.set(heartbeat.risk);
		if (heartbeat.sentiment) forvenSentiment.set(heartbeat.sentiment);
		if (heartbeat.regime) forvenRegime.set(heartbeat.regime);
		if (Array.isArray(heartbeat.open_trades)) forvenOpenTrades.set(heartbeat.open_trades);
		if (heartbeat.scanner_state) forvenScannerState.set(heartbeat.scanner_state);
		const indicators = heartbeat.nav_indicators ?? {};
		setNavIndicators(dataIndicator ? { ...indicators, '/data': dataIndicator } : indicators);
	} catch (error) {
		console.error('[Heartbeat] refresh error:', error);
	}
}

export function startHeartbeat(): void {
	if (controller) return;
	controller = createRealtimeRefresh(refreshHeartbeat, {
		fallbackMs: HEARTBEAT_POLL_MS,
		pollWhenWsOfflineOnly: false,
		wsDebounceMs: 1_250,
		wsEvents: BADGE_EVENTS,
		wsFilter: (detail) => detail.type !== 'notification' || Boolean((detail.data as { actionable?: unknown } | undefined)?.actionable),
	});
	controller.start();
}

export function stopHeartbeat(): void {
	controller?.stop();
	controller = null;
}

/** Debounced immediate refresh — call after actions that change badge counts
 *  (e.g. acknowledging notifications) so the sidebar reflects it right away. */
export function triggerHeartbeat(): void {
	controller?.trigger();
}
