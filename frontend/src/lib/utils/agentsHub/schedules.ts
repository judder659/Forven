// Background jobs, grouped by what they touch, with their cadence in words and
// a health state that never hides a failure behind a display preference.

import type { ForvenSchedulerJob } from '$lib/api';
import type { Tone } from '$lib/utils/forge/status';
import { formatIntervalMs } from '$lib/utils/schedule';

export type JobGroup = 'trading' | 'pipeline' | 'agents' | 'data' | 'maintenance';

export const GROUP_META: Record<JobGroup, { label: string; help: string }> = {
	trading: { label: 'Trading', help: 'Scanner, reconciliation and risk jobs that act on paper and live positions.' },
	pipeline: { label: 'Strategy pipeline', help: 'Creation, screening, validation and graduation of strategies.' },
	agents: { label: 'Agents & learning', help: 'Scheduled agent work: risk audits, post-mortems, summaries.' },
	data: { label: 'Market data', help: 'Collectors that keep candles, funding, open interest and other feeds fresh.' },
	maintenance: { label: 'Maintenance', help: 'Database backups, retention and housekeeping.' },
};

export const GROUP_ORDER: JobGroup[] = ['trading', 'pipeline', 'agents', 'data', 'maintenance'];

const COMMAND_GROUPS: Record<string, JobGroup> = {
	scanner: 'trading',
	'scanner-signal': 'trading',
	'reconcile-sweep': 'trading',
	'phantom-sweep': 'trading',
	'decay-kill-switch': 'trading',
	'decay-tracker': 'trading',
	'propr-mirror': 'trading',
	'slippage-monitor': 'trading',
	'exec-quality-watchdog': 'trading',
	'funding-history-reconcile': 'trading',
	'portfolio-allocation': 'trading',
	'capital-slot-dedupe': 'trading',
	'regime-gate-mtm': 'trading',
	'basket-funding-carry': 'trading',
	'universe-books': 'trading',
	'testnet-harness': 'trading',
	'strategy-creation': 'pipeline',
	'testing-cycle': 'pipeline',
	'gauntlet-step-loop': 'pipeline',
	'paper-eval': 'pipeline',
	'live-graduation-scan': 'pipeline',
	'stale-triage': 'pipeline',
	'auto-intake': 'pipeline',
	'param-optimization': 'pipeline',
	'weekly-review': 'pipeline',
	'orphan-type-scan': 'pipeline',
	'risk-audit': 'agents',
	'daily-learning': 'agents',
	'quant-skills-consolidation': 'agents',
	'overnight-summary': 'agents',
	'market-data-collect': 'data',
	'sentiment-update': 'data',
	recalibrate: 'data',
	'db-maintenance': 'maintenance',
	'db-backup': 'maintenance',
	'wal-checkpoint': 'maintenance',
};

export function jobGroup(job: Pick<ForvenSchedulerJob, 'command'>): JobGroup {
	const command = String(job.command ?? '').trim().toLowerCase();
	if (COMMAND_GROUPS[command]) return COMMAND_GROUPS[command];
	if (command.startsWith('data-')) return 'data';
	return 'maintenance';
}

const DAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];

function zoneLabel(zone: string | null | undefined): string {
	const text = String(zone ?? '').trim();
	if (!text || text.toUpperCase() === 'UTC') return 'UTC';
	return text.split('/').pop()?.replace(/_/g, ' ') ?? text;
}

/** "every 5m", "daily 05:00 Halifax", "Sundays 19:00 UTC", or the raw cron. */
export function jobCadence(job: Pick<ForvenSchedulerJob, 'schedule_type' | 'schedule_expr' | 'timezone'>): string {
	const expr = String(job.schedule_expr ?? '').trim();
	if (job.schedule_type === 'interval') return formatIntervalMs(expr);
	const parts = expr.split(/\s+/);
	if (parts.length === 5 && /^\d+$/.test(parts[0]) && /^\d+$/.test(parts[1]) && parts[2] === '*' && parts[3] === '*') {
		const time = `${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}`;
		const zone = zoneLabel(job.timezone);
		if (parts[4] === '*') return `daily ${time} ${zone}`;
		if (/^[0-6]$/.test(parts[4])) return `${DAYS[Number(parts[4])]} ${time} ${zone}`;
	}
	return expr || '—';
}

export type JobHealthKey = 'failing' | 'running' | 'off' | 'never' | 'ok';

export interface JobHealth {
	key: JobHealthKey;
	label: string;
	tone: Tone;
}

export function jobHealth(
	job: Pick<ForvenSchedulerJob, 'enabled' | 'last_status' | 'last_run_at'> & { running_since?: string | null },
): JobHealth {
	if (!job.enabled) return { key: 'off', label: 'Off', tone: 'idle' };
	if (job.running_since) return { key: 'running', label: 'Running', tone: 'info' };
	const status = String(job.last_status ?? '').toLowerCase();
	if (status === 'error' || status === 'failed') return { key: 'failing', label: 'Failing', tone: 'fail' };
	if (!job.last_run_at) return { key: 'never', label: 'Not run yet', tone: 'wait' };
	return { key: 'ok', label: 'OK', tone: 'ok' };
}

/** A job that finished OK can still leave a note in last_error ("0 eligible of 18 scanned"). */
export function jobNote(job: Pick<ForvenSchedulerJob, 'last_status' | 'last_error'>): string {
	const status = String(job.last_status ?? '').toLowerCase();
	if (status === 'error' || status === 'failed') return '';
	return String(job.last_error ?? '').trim();
}
