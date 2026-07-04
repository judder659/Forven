import { getSystemHeartbeat } from '$lib/api';
import type { SystemHeartbeatResponse, SystemNavIndicator } from '$lib/api';
import {
	forvenDashboard,
	forvenRisk,
	forvenSentiment,
	forvenRegime,
	forvenOpenTrades,
	forvenScannerState,
} from '$lib/stores/forven';
import { setNavIndicators } from '$lib/stores/navMetrics';
import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';

let controller: RealtimeRefreshController | null = null;

function normalizeStatus(value: unknown): string {
	return String(value ?? '').trim().toLowerCase();
}

function pluralize(value: number, singular: string, plural?: string): string {
	if (value === 1) return `${value} ${singular}`;
	return `${value} ${plural ?? `${singular}s`}`;
}

function seenKey(prefix: string, values: unknown[]): string {
	const parts = values
		.map((value) => String(value ?? '').trim())
		.filter(Boolean);
	return parts.length > 0 ? `${prefix}:${parts.join('|')}` : `${prefix}:0`;
}

function indicator(
	kind: SystemNavIndicator['kind'],
	severity: SystemNavIndicator['severity'],
	label: string,
	summary: string,
	count = 0,
	seen_key = '',
): SystemNavIndicator {
	return { kind, severity, label, summary, count, seen_key };
}

function emptyIndicator(): SystemNavIndicator {
	return indicator('none', 'neutral', '', '', 0, '');
}

function buildFallbackNavIndicators(heartbeat: SystemHeartbeatResponse): Record<string, SystemNavIndicator> {
	const routes: Record<string, SystemNavIndicator> = {
		'/': emptyIndicator(),
		'/data': emptyIndicator(),
		'/lab': emptyIndicator(),
		'/hypotheses': emptyIndicator(),
		'/risk': emptyIndicator(),
		'/paper-trades': emptyIndicator(),
		'/live-trades': emptyIndicator(),
		'/agents': emptyIndicator(),
		'/approval': emptyIndicator(),
		'/settings': emptyIndicator(),
	};

	const tasks = Array.isArray(heartbeat.agent_tasks)
		? heartbeat.agent_tasks.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
		: [];
	const failedAgentTasks = tasks.filter(
		(task) => normalizeStatus(task.source) === 'agent_tasks' && normalizeStatus(task.status) === 'failed',
	);
	const blockedAgentTasks = tasks.filter(
		(task) => normalizeStatus(task.source) === 'agent_tasks' && ['blocked', 'rejected'].includes(normalizeStatus(task.status)),
	);
	const runningAgents = Array.from(
		new Set(
			tasks
				.filter((task) => normalizeStatus(task.source) === 'agent_tasks' && normalizeStatus(task.status) === 'running')
				.map((task) => String(task.agent_id ?? '').trim())
				.filter(Boolean),
		),
	).sort();

	if (failedAgentTasks.length > 0) {
		routes['/agents'] = indicator(
			'count',
			'danger',
			String(failedAgentTasks.length),
			pluralize(failedAgentTasks.length, 'agent failure', 'agent failures'),
			failedAgentTasks.length,
			seenKey('agents-failed', failedAgentTasks.map((task) => task.id).slice(0, 8)),
		);
	} else if (blockedAgentTasks.length > 0) {
		routes['/agents'] = indicator(
			'count',
			'warn',
			String(blockedAgentTasks.length),
			`${pluralize(blockedAgentTasks.length, 'agent task')} blocked`,
			blockedAgentTasks.length,
			seenKey('agents-blocked', blockedAgentTasks.map((task) => task.id).slice(0, 8)),
		);
	} else if (runningAgents.length > 0) {
		routes['/agents'] = indicator(
			'activity',
			'success',
			String(runningAgents.length),
			`${pluralize(runningAgents.length, 'agent')} active`,
			runningAgents.length,
			seenKey('agents-running', runningAgents.slice(0, 8)),
		);
	}

	const approvals = Array.isArray(heartbeat.approvals) ? heartbeat.approvals : [];
	if (approvals.length > 0) {
		routes['/approval'] = indicator(
			'count',
			'warn',
			String(approvals.length),
			`${pluralize(approvals.length, 'approval')} waiting`,
			approvals.length,
			seenKey('approvals', approvals.map((item) => (item as Record<string, unknown>).id).slice(0, 8)),
		);
	}

	const scans = Array.isArray(heartbeat.scans) ? heartbeat.scans : [];
	const failedScans = scans.filter((scan) => ['cancelled', 'error', 'failed'].includes(normalizeStatus(scan.status)));
	const activeScans = scans.filter((scan) => ['queued', 'running'].includes(normalizeStatus(scan.status)));
	if (failedScans.length > 0) {
		routes['/lab'] = indicator(
			'count',
			'danger',
			String(failedScans.length),
			`${pluralize(failedScans.length, 'scan')} failed`,
			failedScans.length,
			seenKey('lab-failed', failedScans.map((scan) => scan.id).slice(0, 8)),
		);
	} else if (activeScans.length > 0) {
		routes['/lab'] = indicator(
			'activity',
			'info',
			String(activeScans.length),
			`${pluralize(activeScans.length, 'scan')} running`,
			activeScans.length,
			seenKey('lab-active', activeScans.map((scan) => scan.id).slice(0, 8)),
		);
	}

	// Paper and live each have their own page now, so each carries its own badge:
	// open live positions on Live Trades, paper session activity on Paper Trades.
	const liveTrades = Array.isArray(heartbeat.open_trades) ? heartbeat.open_trades : [];
	const paperSessions = Array.isArray(heartbeat.paper_sessions) ? heartbeat.paper_sessions : [];
	const activePaperSessions = paperSessions.filter((session) =>
		['position_open', 'warming_up', 'watching'].includes(normalizeStatus(session.status)),
	);
	if (liveTrades.length > 0) {
		routes['/live-trades'] = indicator(
			'count',
			'info',
			String(liveTrades.length),
			`${pluralize(liveTrades.length, 'live trade')} open`,
			liveTrades.length,
			seenKey('trades-live', liveTrades.map((trade) => trade.id).slice(0, 8)),
		);
	}
	if (activePaperSessions.length > 0) {
		routes['/paper-trades'] = indicator(
			'activity',
			'success',
			'SIM',
			`${pluralize(activePaperSessions.length, 'paper session')} active`,
			activePaperSessions.length,
			seenKey('paper-trades-active', activePaperSessions.map((session) => session.id).slice(0, 8)),
		);
	}

	if (heartbeat.risk?.kill_switch_active || heartbeat.risk?.daily_loss_halt) {
		routes['/risk'] = indicator(
			'status',
			'danger',
			'HALT',
			heartbeat.risk.kill_switch_active && heartbeat.risk.daily_loss_halt
				? 'Kill switch and daily loss halt active'
				: heartbeat.risk.kill_switch_active
					? 'Kill switch active'
					: 'Daily loss halt active',
			0,
			seenKey('risk', [heartbeat.risk.kill_switch_active, heartbeat.risk.daily_loss_halt]),
		);
	}

	return routes;
}

async function refreshHeartbeat(): Promise<void> {
	try {
		const heartbeat = await getSystemHeartbeat();

		if (heartbeat.dashboard) forvenDashboard.set(heartbeat.dashboard);
		if (heartbeat.risk) forvenRisk.set(heartbeat.risk);
		if (heartbeat.sentiment) forvenSentiment.set(heartbeat.sentiment);
		if (heartbeat.regime) forvenRegime.set(heartbeat.regime);
		if (Array.isArray(heartbeat.open_trades)) forvenOpenTrades.set(heartbeat.open_trades);
		if (heartbeat.scanner_state) forvenScannerState.set(heartbeat.scanner_state);
		setNavIndicators(
			heartbeat.nav_indicators && Object.keys(heartbeat.nav_indicators).length > 0
				? heartbeat.nav_indicators
				: buildFallbackNavIndicators(heartbeat),
		);
	} catch (error) {
		console.error('[Heartbeat] refresh error:', error);
	}
}

export function startHeartbeat(): void {
	if (controller) return;
	controller = createRealtimeRefresh(refreshHeartbeat, {
		fallbackMs: 180_000,
		wsDebounceMs: 1_250,
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
