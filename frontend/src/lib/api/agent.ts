/**
 * Forven agent client (TypeScript) — a first-class typed client so the in-app
 * (Tauri) AI assistant or any browser/Node agent can drive the full Forven
 * strategy lifecycle over the same REST API the rest of the UI uses.
 *
 * This is the TS sibling of `forven/agent/client.py`. It reuses `fetchApi`
 * from ./core (auth headers + base discovery + fallback) so it works in the
 * Tauri desktop app, the browser, and tests with no extra config.
 *
 * MCP is not involved: the MCP server is just a stdio wrapper over this same
 * API. Anything the MCP can do, this can do, from inside the app.
 */

import { fetchApi } from './core';

export const STAGE_QUICK_SCREEN = 'quick_screen';
export const STAGE_GAUNTLET = 'gauntlet';
export const STAGE_PAPER = 'paper';

export interface SideMetrics {
	profit_factor: number | null;
	sharpe: number | null;
	total_trades: number | null;
	max_drawdown_pct: number | null;
	win_rate: number | null;
	total_return_pct: number | null;
}

export interface CompactBacktest {
	result_id?: string;
	asset?: string;
	trade_mode?: string;
	in_sample: SideMetrics;
	out_of_sample: SideMetrics;
}

export interface StrategyStatus {
	id: string;
	stage: string | null;
	status: string | null;
}

export interface QuickScreenResult {
	pass: boolean;
	reasons: string[];
}

export interface EnqueueVerdict {
	file: string;
	dataset_id: string;
	strategy_id?: string | null;
	lookahead_blocked?: boolean | null;
	metrics?: CompactBacktest;
	quick_screen?: QuickScreenResult;
	promotion?: unknown;
	enqueued: boolean;
	error?: string;
}

/** Quick-screen gate thresholds (mirrors forven/agent/client.py). */
export const QUICK_SCREEN_THRESHOLDS = {
	min_profit_factor: 1.05,
	min_sharpe: 0.0,
	max_sharpe: 5.0,
	max_drawdown_pct: 0.3,
	min_trades_oos: 15,
	min_trades_is: 20,
	min_total_return_pct: 0.0,
};

export interface RunBacktestOpts {
	tradeMode?: string;
	parameters?: Record<string, unknown>;
	timeframe?: string;
	start?: string;
	end?: string;
	leverage?: number;
	sessionId?: string;
	compact?: boolean;
}

function post<T>(endpoint: string, body: unknown, timeoutMs = 300_000): Promise<T> {
	return fetchApi<T>(endpoint, { method: 'POST', body: JSON.stringify(body ?? {}), timeoutMs });
}
function get<T>(endpoint: string, timeoutMs = 60_000): Promise<T> {
	return fetchApi<T>(endpoint, { method: 'GET', timeoutMs });
}

export const ForvenAgent = {
	// ── read ─────────────────────────────────────────────────────────
	health: () => get<Record<string, unknown>>('/health', 15_000),
	getContext: () => get<Record<string, unknown>>('/ai-dropzone/context'),
	getQuantSkills: (regime?: string) =>
		get<unknown>(`/quant-skills${regime ? `?regime=${encodeURIComponent(regime)}` : ''}`),
	listStrategies: (status?: string) =>
		get<unknown>(`/strategies${status ? `?status=${encodeURIComponent(status)}` : ''}`),
	getStrategy: (id: string) => get<Record<string, unknown>>(`/strategies/${id}/container`),
	getRecentRuns: (limit = 20) => get<unknown>(`/backtesting/runs?limit=${limit}`),
	getResult: (id: string) => get<unknown>(`/results/${id}`),

	async getStatus(id: string): Promise<StrategyStatus> {
		const c = (await this.getStrategy(id)) as Record<string, any>;
		let stage: string | null = null;
		let status: string | null = null;
		for (const key of ['configuration', 'strategy']) {
			const obj = c?.[key];
			if (obj && (obj.stage || obj.status)) {
				stage = obj.stage ?? null;
				status = obj.status ?? null;
				break;
			}
		}
		if (stage === null && status === null) {
			stage = c?.stage ?? null;
			status = c?.status ?? null;
		}
		return { id, stage, status };
	},

	async getGateReport(id: string): Promise<Record<string, unknown>> {
		const report: Record<string, unknown> = { strategy_id: id };
		let stage = '';
		try {
			const container = (await this.getStrategy(id)) as { strategy?: { stage?: unknown } } | null;
			report.container = container;
			stage = String(container?.strategy?.stage ?? '').trim().toLowerCase();
		} catch (e) { report.container_error = String(e); }
		// A paper strategy's next hop is live: its checklist ends in the real paper->live gate.
		const isPaper = stage.startsWith('paper');
		report.target_stage = isPaper ? 'live_graduated' : 'paper';
		const path = isPaper ? 'paper-live-readiness' : 'readiness';
		try { report.readiness = await get(`/lifecycle/strategies/${id}/${path}`); } catch { report.readiness = null; }
		return report;
	},

	// ── write / lifecycle ────────────────────────────────────────────
	createSession: (label = '', actor = 'in-app-agent', objective = '') =>
		post<{ id: string }>('/ai-dropzone/sessions', { label, actor, objective }),
	closeSession: (id: string) => post<unknown>(`/ai-dropzone/sessions/${id}/close`, {}),
	registerFile: (filePath: string, sessionId?: string) =>
		post<Record<string, unknown>>('/strategies/intake/register-file',
			{ file_path: filePath, source: 'in_app_agent', ...(sessionId ? { session_id: sessionId } : {}) }),

	async runBacktest(strategyId: string, datasetId: string, opts: RunBacktestOpts = {}) {
		const body: Record<string, unknown> = {
			strategy_id: strategyId, dataset_id: datasetId, request_source: 'in_app_agent',
		};
		if (opts.tradeMode) body.trade_mode = opts.tradeMode;
		if (opts.parameters) body.parameters = opts.parameters;
		if (opts.timeframe) body.timeframe = opts.timeframe;
		if (opts.start) body.start = opts.start;
		if (opts.end) body.end = opts.end;
		if (opts.leverage != null) body.leverage = opts.leverage;
		if (opts.sessionId) body.session_id = opts.sessionId;
		const res = await post<Record<string, unknown>>('/backtesting/run', body);
		return opts.compact ? ForvenAgent.compactResult(res) : res;
	},
	runOptimization: (strategyId: string, datasetId: string, opts: { nTrials?: number; objective?: string; parameterRanges?: Record<string, unknown> } = {}) =>
		post('/backtesting/optimize', {
			strategy_id: strategyId, dataset_id: datasetId,
			...(opts.nTrials != null ? { n_trials: opts.nTrials } : {}),
			...(opts.objective ? { objective: opts.objective } : {}),
			...(opts.parameterRanges ? { parameter_ranges: opts.parameterRanges } : {}),
		}),
	runVerdict: (strategyId: string, datasetId: string, tests?: string[]) =>
		post('/backtesting/verdict/run', { strategy_id: strategyId, dataset_id: datasetId, ...(tests ? { tests } : {}) }),
	promote: (strategyId: string, toStatus: string, opts: { fromStatus?: string; reason?: string; force?: boolean } = {}) =>
		post<Record<string, unknown>>(`/strategies/${strategyId}/promote`, {
			to_status: toStatus, reason: opts.reason ?? 'in_app_agent', force: opts.force ?? false,
			...(opts.fromStatus ? { from_status: opts.fromStatus } : {}),
		}),

	// ── helpers (mirror the Python client) ───────────────────────────
	compactResult(result: Record<string, any>): CompactBacktest {
		const m = (result?.metrics && typeof result.metrics === 'object') ? result.metrics : result;
		const keys: (keyof SideMetrics)[] = ['profit_factor', 'sharpe', 'total_trades', 'max_drawdown_pct', 'win_rate', 'total_return_pct'];
		const side = (name: string): SideMetrics => {
			const s = (m?.[name] && typeof m[name] === 'object') ? m[name] : {};
			return Object.fromEntries(keys.map((k) => [k, s[k] ?? null])) as unknown as SideMetrics;
		};
		return { result_id: result?.result_id, asset: result?.asset, trade_mode: result?.trade_mode, in_sample: side('in_sample'), out_of_sample: side('out_of_sample') };
	},

	quickScreen(c: CompactBacktest, thresholds = QUICK_SCREEN_THRESHOLDS): QuickScreenResult {
		const t = { ...QUICK_SCREEN_THRESHOLDS, ...thresholds };
		const reasons: string[] = [];
		const num = (x: unknown) => (typeof x === 'number' ? x : null);
		for (const [side, minTr] of [['in_sample', t.min_trades_is], ['out_of_sample', t.min_trades_oos]] as const) {
			const s = (c as any)?.[side] ?? {};
			const pf = num(s.profit_factor), sh = num(s.sharpe), tr = num(s.total_trades), dd = num(s.max_drawdown_pct), ret = num(s.total_return_pct);
			if (pf === null || pf < t.min_profit_factor) reasons.push(`${side} profit_factor ${pf} < ${t.min_profit_factor}`);
			if (sh === null || sh < t.min_sharpe || sh > t.max_sharpe) reasons.push(`${side} sharpe ${sh} out of [${t.min_sharpe},${t.max_sharpe}]`);
			if (dd === null || dd >= t.max_drawdown_pct) reasons.push(`${side} max_drawdown_pct ${dd} >= ${t.max_drawdown_pct}`);
			if (tr === null || tr < minTr) reasons.push(`${side} total_trades ${tr} < ${minTr}`);
			if (ret === null || ret < t.min_total_return_pct) reasons.push(`${side} total_return_pct ${ret} < ${t.min_total_return_pct}`);
		}
		return { pass: reasons.length === 0, reasons };
	},

	/** register -> 365d backtest -> quick-screen -> promote to gauntlet (force=false). */
	async enqueueCandidate(filePath: string, datasetId: string, opts: { sessionId?: string; tradeMode?: string; parameters?: Record<string, unknown> } = {}): Promise<EnqueueVerdict> {
		const verdict: EnqueueVerdict = { file: filePath, dataset_id: datasetId, enqueued: false };
		const reg = await this.registerFile(filePath, opts.sessionId);
		const sid = (reg as any)?.strategy_id as string | undefined;
		verdict.strategy_id = sid ?? null;
		verdict.lookahead_blocked = (reg as any)?.lookahead_blocked ?? null;
		if (!sid) { verdict.error = 'registration returned no strategy_id'; return verdict; }
		const res = (await this.runBacktest(sid, datasetId, { tradeMode: opts.tradeMode, parameters: opts.parameters, sessionId: opts.sessionId, compact: true })) as CompactBacktest;
		verdict.metrics = res;
		const screen = this.quickScreen(res);
		verdict.quick_screen = screen;
		if (!screen.pass) return verdict;
		const promo = await this.promote(sid, STAGE_GAUNTLET, { fromStatus: STAGE_QUICK_SCREEN, reason: 'in_app_agent enqueue', force: false });
		verdict.promotion = promo;
		const msg = JSON.stringify(promo);
		verdict.enqueued = Boolean((promo as any)?.ok) || msg.includes('found gauntlet');
		return verdict;
	},

	/** Poll until each strategy reaches paper or a terminal state. */
	async waitForPaper(strategyIds: string[], opts: { timeoutMs?: number; intervalMs?: number } = {}): Promise<Record<string, StrategyStatus>> {
		const deadline = Date.now() + (opts.timeoutMs ?? 3_600_000);
		const interval = opts.intervalMs ?? 90_000;
		let snap: Record<string, StrategyStatus> = {};
		while (Date.now() < deadline) {
			snap = {};
			for (const id of strategyIds) {
				try { snap[id] = await this.getStatus(id); } catch { snap[id] = { id, stage: null, status: '?' }; }
			}
			if (Object.values(snap).every((s) => s.status === 'paper' || s.status === 'archived')) break;
			await new Promise((r) => setTimeout(r, interval));
		}
		return snap;
	},
};

export default ForvenAgent;
