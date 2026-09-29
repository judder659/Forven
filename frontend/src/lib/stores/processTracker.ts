/**
 * Global process tracker store.
 *
 * Provides a single polling loop that tracks jobs, scans, and tournaments
 * regardless of which route the user is viewing. Emits a "Background jobs"
 * notification on terminal-status transitions and exposes activity counts per
 * route.
 *
 * The pop-up stack itself lives in $lib/stores/toasts; it is re-exported here
 * because most pages import addToast from this module.
 */

import { writable, derived, get } from 'svelte/store';
import { getJob, getJobs, getScan, listScans, getTournament, listTournaments } from '$lib/api';
import type { Job, Scan, Tournament } from '$lib/api';
import { createPoller, type Poller } from '$lib/utils/polling';
import { notify } from '$lib/stores/toasts';

export {
	addToast,
	clearSnooze,
	dismissToast,
	getSnoozeOptions,
	snoozeNotifications,
	snoozeUntil,
	toasts,
	type ToastItem,
} from '$lib/stores/toasts';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ProcessType = 'job' | 'scan' | 'tournament';

export interface TrackedProcess {
	id: string;
	type: ProcessType;
	label: string;
	status: string;
	href: string;
	data: Job | Scan | Tournament;
	addedAt: number;
	lastPoll: number;
}

// ---------------------------------------------------------------------------
// Terminal status sets
// ---------------------------------------------------------------------------

const TERMINAL: Record<ProcessType, Set<string>> = {
	job: new Set(['succeeded', 'failed', 'cancelled']),
	scan: new Set(['completed', 'failed', 'cancelled']),
	tournament: new Set(['completed', 'failed', 'cancelled']),
};

function isTerminal(type: ProcessType, status: string): boolean {
	return TERMINAL[type].has(status);
}

function hasMeaningfulDataChange(
	type: ProcessType,
	prevData: Job | Scan | Tournament,
	nextData: Job | Scan | Tournament
): boolean {
	if (type === 'job') {
		const prev = prevData as Job;
		const next = nextData as Job;
		return (
			prev.status !== next.status
			|| prev.progress !== next.progress
			|| prev.error !== next.error
			|| prev.result_id !== next.result_id
		);
	}

	if (type === 'scan') {
		const prev = prevData as Scan;
		const next = nextData as Scan;
		return (
			prev.status !== next.status
			|| prev.completed_count !== next.completed_count
			|| prev.total_combinations !== next.total_combinations
			|| prev.error !== next.error
			|| prev.progress_json?.pct_complete !== next.progress_json?.pct_complete
			|| prev.progress_json?.best_sharpe !== next.progress_json?.best_sharpe
			|| prev.progress_json?.pruned_count !== next.progress_json?.pruned_count
		);
	}

	const prev = prevData as Tournament;
	const next = nextData as Tournament;
	return (
		prev.status !== next.status
		|| prev.completed_at !== next.completed_at
	);
}

// ---------------------------------------------------------------------------
// Stores
// ---------------------------------------------------------------------------

export const trackedProcesses = writable<TrackedProcess[]>([]);

/** Non-terminal processes only. */
export const activeProcesses = derived(trackedProcesses, ($tp) =>
	$tp.filter((p) => !isTerminal(p.type, p.status))
);

/** Count of active (non-terminal) processes keyed by route prefix. */
export const activityByRoute = derived(trackedProcesses, ($tp) => {
	const counts: Record<string, number> = {};
	for (const p of $tp) {
		if (isTerminal(p.type, p.status)) continue;
		const route = p.href.split('?')[0];
		counts[route] = (counts[route] || 0) + 1;
	}
	return counts;
});

// ---------------------------------------------------------------------------
// Track / untrack
// ---------------------------------------------------------------------------

export function trackProcess(
	id: string,
	type: ProcessType,
	label: string,
	href: string,
	data: Job | Scan | Tournament
) {
	const now = Date.now();
	trackedProcesses.update((list) => {
		const idx = list.findIndex((p) => p.id === id && p.type === type);
		const entry: TrackedProcess = {
			id,
			type,
			label,
			status: (data as any).status ?? 'unknown',
			href,
			data,
			addedAt: idx >= 0 ? list[idx].addedAt : now,
			lastPoll: now,
		};
		if (idx >= 0) {
			const updated = [...list];
			updated[idx] = entry;
			return updated;
		}
		return [...list, entry];
	});
	ensurePolling();
}

export function untrackProcess(id: string, type: ProcessType) {
	trackedProcesses.update((list) => list.filter((p) => !(p.id === id && p.type === type)));
}

// ---------------------------------------------------------------------------
// Polling loop
// ---------------------------------------------------------------------------

const STALE_MS = 30 * 60 * 1000; // 30 min
const MAX_ACTIVE_POLLS = 4; // Reduced from 8 to limit concurrent requests
const MAX_BOOTSTRAP_TRACKED_PER_TYPE = 5;
// FE-09: the interval between fallback polls of tracked processes. Long enough
// not to add load next to the WS-triggered polls, short enough that a job whose
// terminal WS event never arrives still resolves within a few seconds.
const FALLBACK_POLL_MS = 8000;
let pollInFlight = false;
let wsEventHandler: ((event: Event) => void) | null = null;
let wsPollTimer: ReturnType<typeof setTimeout> | null = null;
// FE-09: this module's docstring promised "a single polling loop", but the only
// trigger was a whitelisted websocket event. A job whose terminal event was
// dropped (socket down, event not in the whitelist, backend restart mid-run) sat
// in 'running' forever, its badge never cleared and its completion/failure toast
// never fired. Back the loop with a real interval while anything is active.
let fallbackPoller: Poller | null = null;

function ensurePolling() {
	if (!fallbackPoller) {
		fallbackPoller = createPoller(pollOnce, FALLBACK_POLL_MS);
		fallbackPoller.start();
	}
	if (typeof window !== 'undefined' && !wsEventHandler) {
		wsEventHandler = (event: Event) => {
			const detail = (event as CustomEvent<Record<string, unknown>>).detail ?? {};
			const eventType = String(detail?.event ?? detail?.type ?? '').toLowerCase();
			if (
				eventType === 'task_queued'
				|| eventType === 'task_status_changed'
				|| eventType === 'task_completed'
				|| eventType === 'task_failed'
				|| eventType === 'strategy_transition'
				|| eventType === 'strategy_promoted'
				|| eventType === 'risk_alert'
				|| eventType === 'trade'
			) {
				scheduleWsPoll();
			}
		};
		window.addEventListener('forven:event', wsEventHandler);
	}
}

function scheduleWsPoll() {
	if (typeof document !== 'undefined' && document.hidden) return;
	if (wsPollTimer !== null) return;
	wsPollTimer = setTimeout(() => {
		wsPollTimer = null;
		void pollOnce();
	}, 900);
}

function stopPolling() {
	// FE-09: torn down by pollOnce once nothing is active, and re-armed by the
	// next trackProcess() — the loop only runs while it has something to watch.
	if (fallbackPoller) {
		fallbackPoller.stop();
		fallbackPoller = null;
	}
	if (wsPollTimer !== null) {
		clearTimeout(wsPollTimer);
		wsPollTimer = null;
	}
	if (typeof window !== 'undefined' && wsEventHandler) {
		window.removeEventListener('forven:event', wsEventHandler);
		wsEventHandler = null;
	}
}

async function pollOnce() {
	if (pollInFlight) return;
	// Skip while tab is hidden
	if (typeof document !== 'undefined' && document.hidden) return;

	pollInFlight = true;

	try {
		const list = get(trackedProcesses);
		const now = Date.now();

		// Remove stale terminal processes
		const fresh = list.filter(
			(p) => !(isTerminal(p.type, p.status) && now - p.lastPoll > STALE_MS)
		);
		let changed = fresh.length !== list.length;

		const active = fresh.filter((p) => !isTerminal(p.type, p.status)).slice(0, MAX_ACTIVE_POLLS);
		if (active.length === 0) {
			if (changed) trackedProcesses.set(fresh);
			stopPolling();
			return;
		}

		const updates: TrackedProcess[] = [...fresh];

		// Process sequentially in batches of 2 to avoid flooding connections
		for (let i = 0; i < active.length; i += 2) {
			const batch = active.slice(i, i + 2);
			await Promise.allSettled(
				batch.map(async (proc) => {
					try {
						let newData: Job | Scan | Tournament;
						if (proc.type === 'job') {
							newData = await getJob(proc.id);
						} else if (proc.type === 'scan') {
							newData = await getScan(proc.id);
						} else {
							newData = await getTournament(proc.id);
						}
						const newStatus = (newData as any).status ?? proc.status;
						const idx = updates.findIndex((p) => p.id === proc.id && p.type === proc.type);
						if (idx >= 0) {
							const current = updates[idx];
							const wasTerminal = isTerminal(proc.type, current.status);
							const nowTerminal = isTerminal(proc.type, newStatus);
							const statusChanged = newStatus !== current.status;
							const dataChanged = hasMeaningfulDataChange(proc.type, current.data, newData);
							if (!statusChanged && !dataChanged) return;

							changed = true;
							updates[idx] = {
								...current,
								status: newStatus,
								data: newData,
								lastPoll: Date.now(),
							};

							// Emit toast on transition to terminal
							if (!wasTerminal && nowTerminal) {
								emitTerminalToast(proc, newStatus);
							}
						}
					} catch {
						// network blip — skip
					}
				})
			);
		}

		if (changed) trackedProcesses.set(updates);
	} finally {
		pollInFlight = false;
	}
}

function emitTerminalToast(proc: TrackedProcess, newStatus: string) {
	const success = newStatus === 'succeeded' || newStatus === 'completed';
	const failed = newStatus === 'failed';
	const type = success ? 'success' : failed ? 'error' : 'info';
	const verb = success ? 'completed' : failed ? 'failed' : 'finished';
	notify({ category: 'background_jobs', message: `${proc.label} ${verb}`, type, href: proc.href });
}

// ---------------------------------------------------------------------------
// Bootstrap — recover running processes on app load
// ---------------------------------------------------------------------------

let bootstrapped = false;

interface BootstrapOptions {
	includeJobs?: boolean;
	includeScans?: boolean;
	includeTournaments?: boolean;
}

export async function bootstrapActiveProcesses(options: BootstrapOptions = {}) {
	if (bootstrapped) return;
	bootstrapped = true;

	const {
		includeJobs = true,
		// Disable scanner/tournament rehydration by default to avoid reviving stale
		// pre-restart records as "active" UI processes.
		includeScans = false,
		includeTournaments = false,
	} = options;

	try {
		const [runningJobs, scans, tournaments] = await Promise.allSettled([
			includeJobs ? getJobs('running') : Promise.resolve([]),
			includeScans ? listScans() : Promise.resolve([]),
			includeTournaments ? listTournaments() : Promise.resolve([]),
		]);

		if (includeJobs && runningJobs.status === 'fulfilled') {
			for (const job of runningJobs.value) {
				if (!isTerminal('job', job.status)) {
					trackProcess(job.id, 'job', `Job ${job.type}`, '/lab', job);
				}
			}
		}

		if (includeScans && scans.status === 'fulfilled') {
			const activeScans = scans.value
				.filter((scan) => !isTerminal('scan', scan.status))
				.slice(0, MAX_BOOTSTRAP_TRACKED_PER_TYPE);
			for (const scan of activeScans) {
				if (!isTerminal('scan', scan.status)) {
					trackProcess(scan.id, 'scan', scan.name || 'Scan', '/lab?tab=scan', scan);
				}
			}
		}

		if (includeTournaments && tournaments.status === 'fulfilled') {
			const activeTournaments = tournaments.value
				.filter((t) => !isTerminal('tournament', t.status))
				.slice(0, MAX_BOOTSTRAP_TRACKED_PER_TYPE);
			for (const t of activeTournaments) {
				if (!isTerminal('tournament', t.status)) {
					trackProcess(t.id, 'tournament', t.name || 'Tournament', '/lab?tab=scan', t);
				}
			}
		}
	} catch {
		// best-effort
	}
}
