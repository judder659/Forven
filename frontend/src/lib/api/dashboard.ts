import {
	asArray,
	asRecord,
	fetchApi,
	isNotFoundError,
} from './core';
import { getForvenStrategiesQuery } from './forven';
import type { ManualScannerRunResponse } from './forven';

// ============================================================================
// Dashboard
// ============================================================================

export interface DashboardKPIs {
	total_tested: number;
	best_sharpe: number;
	active_scans: number;
	signals_today: number;
	pipeline_count: number;
	data_coverage: number;
}

export interface DashboardOverview {
	kpis: DashboardKPIs;
	lifecycle_counts: Record<string, number>;
	blocked_count: number;
	last_ingestion_at: string | null;
	autopilot: {
		initialized?: boolean;
		running: boolean;
		paused: boolean;
		run_id: string | null;
		worker_concurrency: number;
		active_workers: number;
		queued_jobs: number;
		dead_letter_jobs: number;
		last_tick_error: string | null;
		health_ok: boolean | null;
		disabled_reason?: string | null;
	};
	timestamp: string;
}

export interface DashboardFunnelStage {
	state: string;
	count: number;
}

export interface DashboardExceptionItem {
	kind: string;
	id: string;
	strategy_id?: string | null;
	strategy_name?: string | null;
	job_type?: string;
	state?: string;
	message: string;
	ts: string;
	severity: 'high' | 'medium' | 'low' | string;
}

export interface DashboardActionItem {
	id: string;
	label: string;
	description: string;
	href: string;
	priority: number;
	kind: 'critical' | 'warning' | 'info' | 'ok' | string;
}

function toNumberOr(value: unknown, fallback = 0): number {
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : fallback;
}

function parseMaybeJsonValue<T = unknown>(value: unknown): T | unknown {
	if (typeof value !== 'string') return value;
	try {
		return JSON.parse(value) as T;
	} catch {
		return value;
	}
}

function asRecordOrEmpty(value: unknown): Record<string, unknown> {
	return asRecord(parseMaybeJsonValue(value)) ?? {};
}

function extractMetrics(value: unknown): Record<string, unknown> {
	return asRecordOrEmpty(value);
}

function strategyStageFromRow(row: Record<string, unknown>): string {
	const raw = String(row.stage ?? row.status ?? 'quick_screen').trim().toLowerCase().replace(/-/g, '_');
	if (!raw) return 'quick_screen';
	if (raw === 'researching' || raw === 'developing') return 'quick_screen';
	if (raw === 'backtesting') return 'gauntlet';
	if (raw === 'paper_trading' || raw === 'papertrading') return 'paper';
	if (raw === 'deployed' || raw === 'review' || raw === 'ceo_review' || raw === 'ceoreview') return 'live_graduated';
	if (raw === 'retired' || raw === 'killed' || raw === 'trash') return 'archived';
	return raw;
}

function normalizeDashboardAction(raw: unknown, index: number): DashboardActionItem | null {
	const rec = asRecord(raw);
	if (!rec) return null;
	const id = String(rec.id ?? `action-${index}`).trim() || `action-${index}`;
	const label = String(rec.label ?? rec.title ?? rec.kind ?? `Action ${index + 1}`).trim() || `Action ${index + 1}`;
	const kind = String(rec.kind ?? 'info').trim() || 'info';
	const description = String(rec.description ?? rec.detail ?? label).trim() || label;
	const href = String(rec.href ?? '').trim()
		|| (kind === 'critical' || kind === 'warning' ? '/risk' : '/lab');
	const priority = toNumberOr(rec.priority, Math.max(0, 100 - index));
	return {
		id,
		label,
		description,
		href,
		priority,
		kind,
	};
}

async function getLegacyDashboardOverview(): Promise<DashboardOverview> {
	const [dashboardResult, strategiesResult] = await Promise.allSettled([
		fetchApi<Record<string, unknown>>('/dashboard'),
		getForvenStrategiesQuery(),
	]);
	const dashboard = dashboardResult.status === 'fulfilled' ? (dashboardResult.value ?? {}) : {};
	const strategyRows = strategiesResult.status === 'fulfilled' ? strategiesResult.value : [];

	const lifecycleCounts: Record<string, number> = {};
	let bestSharpe = Number.NEGATIVE_INFINITY;
	for (const raw of strategyRows) {
		const row = asRecord(raw);
		if (!row) continue;
		const stage = strategyStageFromRow(row);
		lifecycleCounts[stage] = (lifecycleCounts[stage] ?? 0) + 1;
		const metrics = extractMetrics(row.metrics);
		const sharpe = toNumberOr(metrics.sharpe_ratio ?? metrics.sharpe, Number.NaN);
		if (Number.isFinite(sharpe)) bestSharpe = Math.max(bestSharpe, sharpe);
	}

	const strategyCount = strategyRows.length;
	const daemonRunning = Boolean((dashboard as Record<string, unknown>).daemon_running);
	const tradingAllowed = Boolean((dashboard as Record<string, unknown>).trading_allowed);
	const tradingReason = (dashboard as Record<string, unknown>).trading_reason;

	return {
		kpis: {
			total_tested: strategyCount,
			best_sharpe: Number.isFinite(bestSharpe) ? bestSharpe : 0,
			active_scans: toNumberOr((dashboard as Record<string, unknown>).scan_count, 0),
			signals_today: 0,
			pipeline_count: strategyCount,
			data_coverage: 0,
		},
		lifecycle_counts: lifecycleCounts,
		blocked_count: 0,
		last_ingestion_at: ((dashboard as Record<string, unknown>).last_scan as string | null) ?? null,
		autopilot: {
			initialized: true,
			running: daemonRunning,
			paused: !tradingAllowed,
			run_id: null,
			worker_concurrency: 0,
			active_workers: 0,
			queued_jobs: 0,
			dead_letter_jobs: 0,
			last_tick_error: null,
			health_ok: daemonRunning,
			disabled_reason: tradingReason ? String(tradingReason) : null,
		},
		timestamp: new Date().toISOString(),
	};
}

export async function getDashboardKPIs(): Promise<DashboardKPIs> {
	try {
		return await fetchApi('/dashboard/kpis');
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		return (await getLegacyDashboardOverview()).kpis;
	}
}

export async function getDashboardOverview(): Promise<DashboardOverview> {
	try {
		return await fetchApi('/dashboard/overview');
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		return getLegacyDashboardOverview();
	}
}

export async function getDashboardFunnel(): Promise<DashboardFunnelStage[]> {
	return fetchApi('/dashboard/funnel');
}

// ---- Live fleet (GET /api/dashboard/live-fleet) ----

/** stale wins over in_position: an open position the scanner stopped evaluating cannot exit. */
export type LiveStrategyState = 'in_position' | 'blocked' | 'watching' | 'stale';

export interface LiveFleetStrategy {
	strategy_id: string;
	name: string;
	display_name: string | null;
	symbol: string | null;
	timeframe: string | null;
	live_since: string | null;
	state: LiveStrategyState;
	open_trade_ids: string[];
	trades: {
		closed: number;
		wins: number;
		losses: number;
		failed: number;
		win_rate: number | null;
		/** Realized, net of recorded fees and funding. */
		net_pnl_usd: number;
		last_trade_at: string | null;
	};
	last_scan: {
		at: string;
		signal_type: string;
		matched: boolean;
		executed: boolean;
		reason: string | null;
	} | null;
	/** Matched entry signals the live path refused, since go-live or the window start. */
	blocked_entries: {
		window_days: number;
		count: number;
		last_at: string | null;
		last_reason: string | null;
		top_reason: string | null;
		top_count: number;
	};
}

export interface LiveRealizedWindow {
	closed: number;
	wins: number;
	losses: number;
	net_pnl_usd: number;
	win_rate: number | null;
	profit_factor: number | null;
}

export interface LiveFill {
	id: string;
	strategy_id: string;
	strategy_name: string | null;
	asset: string;
	direction: string;
	status: string;
	opened_at: string | null;
	closed_at: string | null;
	net_pnl_usd: number | null;
	close_reason: string | null;
	failure_reason: string | null;
}

/** Worst-case wallet margin if every live strategy that can route to it entered at once. */
export interface LiveWalletCapacity {
	wallet: string;
	sides: string[];
	equity_usd: number | null;
	capacity_usd: number | null;
	worst_case_margin_usd: number | null;
	over_capacity: boolean;
}

export interface LiveCapacityReport {
	margin_cap_pct: number;
	cohort_size: number;
	slice_usd: number | null;
	wallets: LiveWalletCapacity[];
	/** CAP-FIT-1: share of the full slice live sizing uses so every wallet's worst case fits (1 = full size). */
	capacity_scale: number;
	/** Live strategies that share a coin and a direction, so they refuse each other's entries. */
	conflicts: Array<{ coin: string; sides: string[]; strategy_ids: string[] }>;
}

export interface LiveFleet {
	generated_at: string;
	stale_after_seconds: number;
	strategies: LiveFleetStrategy[];
	live_bots_armed: number;
	realized: Record<'7d' | '30d' | 'all', LiveRealizedWindow>;
	recent_fills: LiveFill[];
	capacity: LiveCapacityReport | null;
}

export async function getLiveFleet(): Promise<LiveFleet> {
	return fetchApi('/dashboard/live-fleet');
}

export async function getDashboardExceptions(limit = 30): Promise<DashboardExceptionItem[]> {
	return fetchApi(`/dashboard/exceptions?limit=${limit}`);
}

export async function getDashboardActions(): Promise<DashboardActionItem[]> {
	try {
		const payload = await fetchApi<unknown>('/dashboard/actions');
		const rows = asArray(payload);
		return rows
			.map((row, index) => normalizeDashboardAction(row, index))
			.filter((row): row is DashboardActionItem => Boolean(row));
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		const overview = await getDashboardOverview();
		const actions: DashboardActionItem[] = [];
		if (!overview.autopilot.running) {
			actions.push({
				id: 'legacy-start-daemon',
				label: 'Start Daemon',
				description: 'Daemon appears offline. Restart orchestration services.',
				href: '/lab?tab=247',
				priority: 100,
				kind: 'critical',
			});
		}
		if (overview.autopilot.disabled_reason || overview.autopilot.paused) {
			actions.push({
				id: 'legacy-review-risk',
				label: 'Review Risk',
				description: overview.autopilot.disabled_reason || 'Trading is paused. Review risk controls.',
				href: '/risk',
				priority: 90,
				kind: 'warning',
			});
		}
		actions.push({
			id: 'legacy-open-pipeline',
			label: 'Open Strategy Lab',
			description: 'Review strategy phases and pending handoffs in the Strategy Lab.',
			href: '/lab',
			priority: 70,
			kind: 'info',
		});
		return actions;
	}
}

export async function getDashboardCoverage(): Promise<{ coverage: Record<string, unknown> }> {
	try {
		return await fetchApi('/dashboard/coverage');
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		return { coverage: {} };
	}
}

export async function getDashboardSuggestions(): Promise<unknown[]> {
	return fetchApi('/dashboard/suggestions');
}

// ---- Quality Triage ----

export interface PruningFunnelStage {
	stage: string;
	count: number;
}

export async function getScanFunnel(scanId: string): Promise<PruningFunnelStage[]> {
	return fetchApi(`/scanner/scans/${scanId}/funnel`);
}

// ============================================================================
// Quant Factory Dashboard
// ============================================================================

export interface QuantFactoryAccount {
	account_value: number;
	net_exposure: number;
	daily_pnl_usd: number;
	daily_pnl_pct: number;
	execution_mode: string;
	trading_allowed: boolean;
	trading_reason: string;
	kill_switch_active: boolean;
	daemon_running: boolean;
	drawdown_pct: number;
	prices: Record<string, number>;
}

export interface QuantFactoryRadarEntry {
	id?: string;
	display_id?: string | null;
	strategy_name?: string | null;
	strategy: string;
	target: string;
	timeframe?: string;
	regime: string;
	stage: string;
	alpha: string;
	sharpe: number;
	trend: 'up' | 'down';
	model?: string;
}

export interface QuantFactoryAgent {
	id: string;
	name: string;
	role: string;
	enabled: boolean;
	model: string;
	status: 'active' | 'pending' | 'idle' | 'disabled';
	status_label: string;
}

export interface QuantFactoryLogEntry {
	id?: number;
	time: string;
	tag: string;
	layer: 'exec' | 'decay' | 'brain';
	level: string;
	msg: string;
}

export interface QuantFactoryArenaEntry {
	symbol: string;
	champion: { name: string; sharpe: number; return: number };
	challenger: { name: string; sharpe: number; return: number };
	edge_pct: number;
	threshold_pct: number;
}

export interface QuantFactoryValidation {
	is: { trades: number; sharpe: number; max_dd: number; win_rate: number };
	oos: { trades: number; sharpe: number; max_dd: number; win_rate: number };
	robustness: number;
	degradation_pct: number;
	strategy_name: string;
	status: string;
}

export interface QuantFactoryIntel {
	total_strategies: number;
	live: number;
	paper: number;
	backtesting: number;
	researching: number;
	total_trades: number;
	open_trades: number;
	avg_slippage_bps: number;
	total_backtests: number;
	agent_count: number;
}

export interface QuantFactoryData {
	account: QuantFactoryAccount;
	radar: QuantFactoryRadarEntry[];
	agents: QuantFactoryAgent[];
	logs: QuantFactoryLogEntry[];
	arena: QuantFactoryArenaEntry[];
	validation: QuantFactoryValidation;
	intel: QuantFactoryIntel;
}

export async function getQuantFactoryData(): Promise<QuantFactoryData> {
	return fetchApi('/quant-factory/');
}

// ============================================================================
// Signal Monitoring
// ============================================================================

export interface Signal {
	id: string;
	strategy_name: string;
	symbol: string;
	timeframe: string;
	signal_type: string;
	price: number;
	bar_timestamp: string;
	created_at: string;
	notified: boolean;
}

export interface MonitoredStrategy {
	id: string;
	strategy_name: string;
	definition_json: Record<string, unknown>;
	symbol: string;
	timeframe: string;
	active: boolean;
	created_at: string;
}

export async function getRecentSignals(limit = 50): Promise<Signal[]> {
	return fetchApi(`/signals/recent?limit=${limit}`);
}

export async function getMonitoredStrategies(): Promise<MonitoredStrategy[]> {
	return fetchApi('/signals/monitored');
}

export async function addMonitoredStrategy(
	strategyName: string, definitionJson: Record<string, unknown>, symbol: string, timeframe: string
): Promise<{ id: string }> {
	return fetchApi('/signals/monitored', {
		method: 'POST',
		body: JSON.stringify({
			strategy_name: strategyName,
			definition_json: definitionJson,
			symbol,
			timeframe,
		}),
	});
}

export async function removeMonitoredStrategy(id: string): Promise<{ status: string }> {
	return fetchApi(`/signals/monitored/${id}`, { method: 'DELETE' });
}

export async function checkSignalsNow(): Promise<ManualScannerRunResponse> {
	return fetchApi('/system/scanner/signal-run', { method: 'POST' });
}

// ============================================================================
// Pipeline funnel report (gate rejections)
// ============================================================================

export interface GateRejectionRow {
	gate: string;
	reason_code: string;
	count: number;
}

export interface PipelineFunnelReport {
	period_days: number;
	stage_counts: Record<string, number>;
	total_strategies: number;
	flows: Array<Record<string, unknown>>;
	gate_rejections: GateRejectionRow[];
	timeout_count: number;
	backtest_results_count: number;
	heartbeat_alert: boolean;
}

export async function getPipelineFunnelReport(days = 7): Promise<PipelineFunnelReport> {
	const payload = asRecord(await fetchApi<unknown>(`/pipeline/funnel-report?days=${days}`)) ?? {};
	const rejections = asArray(payload.gate_rejections)
		.map((row) => {
			const rec = asRecord(row);
			if (!rec) return null;
			return {
				gate: String(rec.gate ?? 'unknown').trim() || 'unknown',
				reason_code: String(rec.reason_code ?? 'unknown').trim() || 'unknown',
				count: toNumberOr(rec.count, 0),
			};
		})
		.filter((row): row is GateRejectionRow => Boolean(row));
	rejections.sort((a, b) => b.count - a.count);
	return {
		period_days: toNumberOr(payload.period_days, days),
		stage_counts: (asRecord(payload.stage_counts) ?? {}) as Record<string, number>,
		total_strategies: toNumberOr(payload.total_strategies, 0),
		flows: asArray(payload.flows).map((row) => asRecord(row) ?? {}),
		gate_rejections: rejections,
		timeout_count: toNumberOr(payload.timeout_count, 0),
		backtest_results_count: toNumberOr(payload.backtest_results_count, 0),
		heartbeat_alert: Boolean(payload.heartbeat_alert),
	};
}

// ============================================================================
// Task / queue health (from GET /api/health runtime summary)
// ============================================================================

export interface TaskHealthQueues {
	agent_pending: number;
	agent_running: number;
	agent_stale_pending: number;
	agent_stale_running: number;
	brain_pending: number;
	brain_running: number;
	brain_stale_pending: number;
	brain_stale_running: number;
}

export interface TaskHealth {
	status: string;
	issues: string[];
	queues: TaskHealthQueues;
	long_running_scheduler_jobs: number;
	overdue_due_scheduler_jobs: number;
}

export async function getTaskHealth(): Promise<TaskHealth> {
	const payload = asRecord(await fetchApi<unknown>('/health')) ?? {};
	const details = asRecord(payload.details) ?? {};
	const queues = asRecord(details.queues) ?? {};
	return {
		status: String(payload.status ?? 'unknown').trim() || 'unknown',
		issues: asArray(payload.issues).map((issue) => String(issue ?? '')).filter(Boolean),
		queues: {
			agent_pending: toNumberOr(queues.agent_pending, 0),
			agent_running: toNumberOr(queues.agent_running, 0),
			agent_stale_pending: toNumberOr(queues.agent_stale_pending, 0),
			agent_stale_running: toNumberOr(queues.agent_stale_running, 0),
			brain_pending: toNumberOr(queues.brain_pending, 0),
			brain_running: toNumberOr(queues.brain_running, 0),
			brain_stale_pending: toNumberOr(queues.brain_stale_pending, 0),
			brain_stale_running: toNumberOr(queues.brain_stale_running, 0),
		},
		long_running_scheduler_jobs: toNumberOr(details.long_running_scheduler_jobs, 0),
		overdue_due_scheduler_jobs: toNumberOr(details.overdue_due_scheduler_jobs, 0),
	};
}

// ============================================================================
// Scheduler watch (GET /api/scheduler)
// ============================================================================

export interface SchedulerJobSummary {
	id: string;
	name: string;
	enabled: boolean;
	lastRunAt: string | null;
	nextRunAt: string | null;
	runningSince: string | null;
	lastStatus: string;
	lastError: string | null;
}

export async function getSchedulerJobs(): Promise<SchedulerJobSummary[]> {
	const rows = asArray(await fetchApi<unknown>('/scheduler'));
	const jobs: SchedulerJobSummary[] = [];
	for (const row of rows) {
		const rec = asRecord(row);
		if (!rec) continue;
		jobs.push({
			id: String(rec.id ?? ''),
			name: String(rec.name ?? rec.id ?? '').trim(),
			enabled: Boolean(Number(rec.enabled ?? 0)),
			lastRunAt: rec.last_run_at ? String(rec.last_run_at) : null,
			nextRunAt: rec.next_run_at ? String(rec.next_run_at) : null,
			runningSince: rec.running_since ? String(rec.running_since) : null,
			lastStatus: String(rec.last_status ?? '').trim().toLowerCase(),
			lastError: rec.last_error ? String(rec.last_error) : null,
		});
	}
	return jobs;
}

// ============================================================================
// Critical health alerts (GET /api/health/alerts)
// ============================================================================

export interface HealthAlertItem {
	severity: string;
	component: string;
	message: string;
	timestamp: string;
	action_taken: string;
}

export interface HealthAlertsResponse {
	alerts: HealthAlertItem[];
	count: number;
}

export async function getCriticalHealthAlerts(limit = 25): Promise<HealthAlertsResponse> {
	const payload = asRecord(
		await fetchApi<unknown>(`/health/alerts?severity=critical&limit=${limit}`)
	) ?? {};
	const alerts = asArray(payload.alerts)
		.map((row) => {
			const rec = asRecord(row);
			if (!rec) return null;
			return {
				severity: String(rec.severity ?? 'critical').trim() || 'critical',
				component: String(rec.component ?? '').trim(),
				message: String(rec.message ?? '').trim(),
				timestamp: String(rec.timestamp ?? '').trim(),
				action_taken: String(rec.action_taken ?? '').trim(),
			};
		})
		.filter((row): row is HealthAlertItem => Boolean(row));
	return { alerts, count: toNumberOr(payload.count, alerts.length) };
}

// ============================================================================
// Paper session PnL rollup (GET /api/paper/summary)
// ============================================================================

export interface PaperSessionSummaryRow {
	session_id: string;
	strategy_id: string;
	strategy_name: string;
	symbol: string;
	timeframe: string;
	status: string;
	closed_count: number;
	open_count: number;
	realized_pnl_usd: number;
	win_rate_pct: number | null;
	close_reasons: Record<string, number>;
}

export interface PaperSummaryTotals {
	session_count: number;
	closed_count: number;
	open_count: number;
	realized_pnl_usd: number;
	win_rate_pct: number | null;
	close_reasons: Record<string, number>;
}

export interface PaperSummary {
	sessions: PaperSessionSummaryRow[];
	totals: PaperSummaryTotals;
	include_deployed: boolean;
	timestamp: string;
}

function normalizeCloseReasons(value: unknown): Record<string, number> {
	const rec = asRecord(value) ?? {};
	const out: Record<string, number> = {};
	for (const [reason, count] of Object.entries(rec)) {
		const key = String(reason ?? '').trim();
		if (!key) continue;
		out[key] = toNumberOr(count, 0);
	}
	return out;
}

function normalizeWinRate(value: unknown): number | null {
	if (value === null || value === undefined) return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

export async function getPaperSummary(includeDeployed = false): Promise<PaperSummary> {
	const payload = asRecord(
		await fetchApi<unknown>(`/paper/summary?include_deployed=${includeDeployed}`)
	) ?? {};
	const totalsRec = asRecord(payload.totals) ?? {};
	const sessions = asArray(payload.sessions)
		.map((row) => {
			const rec = asRecord(row);
			if (!rec) return null;
			return {
				session_id: String(rec.session_id ?? '').trim(),
				strategy_id: String(rec.strategy_id ?? '').trim(),
				strategy_name: String(rec.strategy_name ?? '').trim(),
				symbol: String(rec.symbol ?? '').trim(),
				timeframe: String(rec.timeframe ?? '').trim(),
				status: String(rec.status ?? '').trim(),
				closed_count: toNumberOr(rec.closed_count, 0),
				open_count: toNumberOr(rec.open_count, 0),
				realized_pnl_usd: toNumberOr(rec.realized_pnl_usd, 0),
				win_rate_pct: normalizeWinRate(rec.win_rate_pct),
				close_reasons: normalizeCloseReasons(rec.close_reasons),
			};
		})
		.filter((row): row is PaperSessionSummaryRow => Boolean(row));
	return {
		sessions,
		totals: {
			session_count: toNumberOr(totalsRec.session_count, sessions.length),
			closed_count: toNumberOr(totalsRec.closed_count, 0),
			open_count: toNumberOr(totalsRec.open_count, 0),
			realized_pnl_usd: toNumberOr(totalsRec.realized_pnl_usd, 0),
			win_rate_pct: normalizeWinRate(totalsRec.win_rate_pct),
			close_reasons: normalizeCloseReasons(totalsRec.close_reasons),
		},
		include_deployed: Boolean(payload.include_deployed ?? includeDeployed),
		timestamp: String(payload.timestamp ?? '').trim(),
	};
}
