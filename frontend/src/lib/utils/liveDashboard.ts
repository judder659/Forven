/**
 * Derivations behind the live-first dashboard. Kept out of the components so
 * the live/paper split and the attention rules are unit-testable.
 */
import type { ForvenDashboardResponse, ForvenRiskStatus, ForvenTrade } from '$lib/api';
import type { LiveFleet, LiveFleetStrategy, SchedulerJobSummary } from '$lib/api/dashboard';

export function isLiveTrade(trade: ForvenTrade): boolean {
	return String(trade.execution_type ?? '').toLowerCase() === 'live';
}

export function finite(value: unknown): number | null {
	if (value === null || value === undefined || value === '') return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

export function parseTime(value: string | null | undefined): number | null {
	if (!value) return null;
	const text = value.includes('T') ? value : value.replace(' ', 'T');
	const parsed = Date.parse(text);
	return Number.isNaN(parsed) ? null : parsed;
}

/** Compact age: 45s, 12m, 5h, 3d. Empty string when the time is unknown. */
export function formatAge(value: string | null | undefined, now = Date.now()): string {
	const ts = parseTime(value);
	if (ts === null) return '';
	const seconds = Math.max(0, (now - ts) / 1000);
	if (seconds < 90) return `${Math.round(seconds)}s`;
	if (seconds < 90 * 60) return `${Math.round(seconds / 60)}m`;
	if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)}h`;
	return `${Math.round(seconds / 86400)}d`;
}

export function formatUsd(value: number | null | undefined, signed = false): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const abs = Math.abs(value).toLocaleString(undefined, {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2,
	});
	if (signed) return `${value < 0 ? '−' : '+'}$${abs}`;
	return `${value < 0 ? '−' : ''}$${abs}`;
}

export function formatPrice(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const digits = Math.abs(value) >= 1000 ? 1 : Math.abs(value) >= 1 ? 2 : 5;
	return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function formatPct(fraction: number | null | undefined, digits = 1): string {
	if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return '—';
	return `${(fraction * 100).toFixed(digits)}%`;
}

export function pnlTone(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return 'text-gray-300';
	return value > 0 ? 'text-emerald-400' : 'text-red-400';
}

/** Meter fill by how much of a limit is used: calm, then amber, then red. */
export function meterTone(usedFraction: number | null): string {
	if (usedFraction === null) return 'bg-[#333]';
	if (usedFraction >= 0.8) return 'bg-red-500';
	if (usedFraction >= 0.5) return 'bg-amber-400';
	return 'bg-emerald-500';
}

export function strategyLabel(strategy: Pick<LiveFleetStrategy, 'strategy_id' | 'display_name'>): string {
	const display = String(strategy.display_name ?? '').trim();
	return display ? `${display} · ${strategy.strategy_id}` : strategy.strategy_id;
}

export function liveNetwork(dashboard: ForvenDashboardResponse | null): 'mainnet' | 'testnet' | null {
	const raw = String(dashboard?.account?.network ?? dashboard?.recovery?.network ?? '').trim().toLowerCase();
	return raw === 'mainnet' || raw === 'testnet' ? raw : null;
}

/** The kill switch is disarmed only when the setting is explicitly off. */
export function killSwitchDisarmed(risk: ForvenRiskStatus | null): boolean {
	return risk?.kill_switch_enabled === false;
}

export function failingJobs(jobs: SchedulerJobSummary[]): SchedulerJobSummary[] {
	return jobs.filter((job) => job.enabled && job.lastStatus && job.lastStatus !== 'ok');
}

/** Merge pairwise coin/side conflicts into one entry per coin. */
export function groupConflicts(
	pairs: Array<{ coin: string; sides: string[]; strategy_ids: string[] }>,
): Array<{ coin: string; sides: string[]; strategyIds: string[] }> {
	const byCoin = new Map<string, { sides: Set<string>; ids: Set<string> }>();
	for (const pair of pairs) {
		const group = byCoin.get(pair.coin) ?? { sides: new Set<string>(), ids: new Set<string>() };
		pair.sides.forEach((side) => group.sides.add(side));
		pair.strategy_ids.forEach((id) => group.ids.add(id));
		byCoin.set(pair.coin, group);
	}
	return [...byCoin.entries()].map(([coin, group]) => ({
		coin,
		sides: [...group.sides].sort(),
		strategyIds: [...group.ids].sort(),
	}));
}

export type AttentionSeverity = 'critical' | 'warning' | 'info';

export interface AttentionItem {
	id: string;
	severity: AttentionSeverity;
	title: string;
	detail?: string;
	href?: string;
}

export interface AttentionInput {
	dashboard: ForvenDashboardResponse | null;
	risk: ForvenRiskStatus | null;
	fleet: LiveFleet | null;
	schedulerJobs: SchedulerJobSummary[];
	pendingApprovals: number;
	now?: number;
}

const SEVERITY_ORDER: Record<AttentionSeverity, number> = { critical: 0, warning: 1, info: 2 };
// Scheduler jobs on the live execution path: scanners, close/funding reconciliation,
// phantom recovery, risk audit, decay demotion, and fill-quality checks.
const TRADING_JOB_RE = /scanner|reconcile-sweep|funding-history-reconcile|phantom|risk-audit|decay-kill|exec-quality|slippage/i;

/** Only things an operator can act on. Informational state lives elsewhere on the page. */
export function buildAttentionItems(input: AttentionInput): AttentionItem[] {
	const { dashboard, risk, fleet, schedulerJobs, pendingApprovals } = input;
	const now = input.now ?? Date.now();
	const items: AttentionItem[] = [];

	if (dashboard && dashboard.daemon_running === false) {
		items.push({
			id: 'daemon-offline',
			severity: 'critical',
			title: 'Trading daemon is offline',
			detail: 'Live positions are not being scanned or managed.',
			href: '/diagnostics',
		});
	}
	if (dashboard && dashboard.trading_allowed === false) {
		items.push({
			id: 'trading-halted',
			severity: 'critical',
			title: 'Trading halted',
			detail: dashboard.trading_reason || undefined,
			href: '/risk',
		});
	}
	if (risk?.kill_switch_active) {
		items.push({ id: 'kill-switch', severity: 'critical', title: 'Kill switch tripped', href: '/risk' });
	}
	if (risk?.daily_loss_halt) {
		items.push({ id: 'daily-loss-halt', severity: 'critical', title: 'Daily loss limit hit — new entries halted', href: '/risk' });
	}
	if (dashboard?.recovery?.requires_operator) {
		items.push({
			id: 'recovery',
			severity: 'critical',
			title: 'Exchange recovery needs you',
			detail: dashboard.recovery.summary || undefined,
			href: '/live-trades',
		});
	}
	const stopsMissing = finite(risk?.portfolio_budget_live?.stops_missing) ?? 0;
	if (stopsMissing > 0) {
		items.push({
			id: 'stops-missing',
			severity: 'critical',
			title: `${stopsMissing} live position${stopsMissing === 1 ? '' : 's'} without a stop`,
			href: '/live-trades',
		});
	}
	const breakers = Object.entries(dashboard?.circuit_breakers ?? {}).filter(
		([, state]) => state && String(state).toLowerCase() !== 'closed',
	);
	if (breakers.length > 0) {
		items.push({
			id: 'breakers',
			severity: 'warning',
			title: 'Exchange circuit breaker open',
			detail: breakers.map(([name, state]) => `${name.replace(/^hl_/, '')} ${state}`).join(', '),
			href: '/risk',
		});
	}

	for (const strategy of fleet?.strategies ?? []) {
		if (strategy.state === 'stale') {
			const holding = strategy.open_trade_ids.length > 0;
			const age = formatAge(strategy.last_scan?.at, now);
			items.push({
				id: `stale-${strategy.strategy_id}`,
				severity: holding ? 'critical' : 'warning',
				title: `${strategy.strategy_id} is not being scanned${holding ? ' while holding a position' : ''}`,
				detail: age ? `Last scan ${age} ago.` : 'No scans since it went live.',
				href: `/lab/strategy/${encodeURIComponent(strategy.strategy_id)}`,
			});
		} else if (strategy.state === 'blocked') {
			const blocked = strategy.blocked_entries;
			items.push({
				id: `blocked-${strategy.strategy_id}`,
				severity: 'warning',
				title: `${strategy.strategy_id} entries blocked (${blocked.count}× in ${blocked.window_days}d)`,
				detail: blocked.last_reason || undefined,
				href: `/lab/strategy/${encodeURIComponent(strategy.strategy_id)}`,
			});
		}
	}

	// LIVE-ADMIT-1: entry refusals waiting to happen, before any signal hits them.
	for (const group of groupConflicts(fleet?.capacity?.conflicts ?? [])) {
		items.push({
			id: `conflict-${group.coin}`,
			severity: 'warning',
			title: `${group.strategyIds.join(', ')} all trade ${group.coin} ${group.sides.join('/')} live`,
			detail: 'One wallet holds one position per coin, so they refuse each other’s entries. Keep one of them live.',
			href: '/live-trades',
		});
	}
	const scale = fleet?.capacity?.capacity_scale ?? 1;
	for (const wallet of fleet?.capacity?.wallets ?? []) {
		if (!wallet.over_capacity) continue;
		items.push({
			id: `capacity-${wallet.wallet}`,
			severity: 'warning',
			title: `Live sizes scaled to ${Math.round(scale * 100)}% to fit the ${wallet.wallet} wallet`,
			detail: `At full size its worst case is ${formatUsd(wallet.worst_case_margin_usd)} of margin against a ${formatUsd(wallet.capacity_usd)} limit. Add funds to it to restore full size.`,
			href: '/risk',
		});
	}

	for (const job of failingJobs(schedulerJobs)) {
		items.push({
			id: `job-${job.id}`,
			// Research and data jobs failing never touches open money; trading jobs can.
			severity: TRADING_JOB_RE.test(job.id) ? 'warning' : 'info',
			title: `${job.name || job.id} failing`,
			detail: job.lastError || job.lastStatus,
			href: '/diagnostics',
		});
	}

	if (pendingApprovals > 0) {
		items.push({
			id: 'approvals',
			severity: 'info',
			title: `${pendingApprovals} approval${pendingApprovals === 1 ? '' : 's'} waiting`,
			href: '/approval',
		});
	}

	return items.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
}

export interface PnlPoint {
	timestamp: string;
	value: number;
}

/**
 * Cumulative realized P&L over a trailing window, starting at 0 at the window
 * start, from the equity-history curve ({time, value}; value = base + cumulative).
 */
export function cumulativePnlSeries(
	curve: Array<{ time?: string; value?: number }>,
	base: number,
	windowMs: number | null,
	now = Date.now(),
): PnlPoint[] {
	const points = curve
		.map((point) => ({ ts: parseTime(point.time ?? null), cum: finite(point.value) }))
		.filter((point): point is { ts: number; cum: number } => point.ts !== null && point.cum !== null)
		.map((point) => ({ ts: point.ts, cum: point.cum - base }))
		.sort((a, b) => a.ts - b.ts);
	if (points.length === 0) return [];
	const start = windowMs === null ? null : now - windowMs;
	let offset = 0;
	let inWindow = points;
	if (start !== null) {
		const before = points.filter((point) => point.ts < start);
		offset = before.length > 0 ? before[before.length - 1].cum : 0;
		inWindow = points.filter((point) => point.ts >= start);
	}
	const series: PnlPoint[] = [];
	if (start !== null) series.push({ timestamp: new Date(start).toISOString(), value: 0 });
	else series.push({ timestamp: new Date(points[0].ts - 1000).toISOString(), value: 0 });
	for (const point of inWindow) {
		series.push({ timestamp: new Date(point.ts).toISOString(), value: point.cum - offset });
	}
	// One point per second: the chart needs strictly increasing times.
	const bySecond = new Map<number, PnlPoint>();
	for (const point of series) {
		bySecond.set(Math.floor(Date.parse(point.timestamp) / 1000), point);
	}
	return [...bySecond.values()];
}
