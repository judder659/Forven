/**
 * Shared self-update state. The top-bar update pill, the "What's new" panel,
 * the urgent banner and Settings → System → Updates all read/drive this, so a
 * check or an update in one place shows in the others.
 */

import { writable, derived, get } from 'svelte/store';
import { checkHealth } from '$lib/api';
import {
	getUpdateStatus,
	applyUpdate,
	setUpdateChannel,
	type UpdateStatus,
	type UpdateChannel,
} from '$lib/api/updates';

export const updateStatus = writable<UpdateStatus | null>(null);
export const updateChecking = writable(false);
export const updateError = writable<string | null>(null);
/** The "What's new" side panel. */
export const updatePanelOpen = writable(false);

/** The four steps an update walks through, in order. */
export const UPDATE_STEPS = ['Download', 'Install', 'Restart', 'Back online'] as const;

export interface UpdateProgress {
	/** Index into UPDATE_STEPS of the step running (or the one that failed). */
	step: number;
	failed: boolean;
	error: string | null;
	/** Every step finished; the page is about to reload. */
	done: boolean;
}

/** Null when no update is running. */
export const updateProgress = writable<UpdateProgress | null>(null);

export const updateBusy = derived(updateProgress, ($p) => $p != null && !$p.failed);

let lastCheckAt = 0;
const RECHECK_COOLDOWN_MS = 15_000;
const DAY_MS = 24 * 60 * 60 * 1000;
const LAST_REMOTE_CHECK_KEY = 'forven.update.last_remote_check';
const SNOOZE_KEY = 'forven.update.snooze';
const JUST_UPDATED_KEY = 'forven.update.just_updated';

function storageGet(key: string): string | null {
	try {
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}

function storageSet(key: string, value: string): void {
	try {
		localStorage.setItem(key, value);
	} catch {
		// Storage unavailable (private window, blocked site data): the feature
		// still works for this page view, it just isn't remembered.
	}
}

function storageRemove(key: string): void {
	try {
		localStorage.removeItem(key);
	} catch {
		// See storageSet.
	}
}

/**
 * Refresh the cached update status.
 * @param fetchRemote hit the remote (default) or compare against the last download.
 * @param force bypass the cooldown that de-dupes rapid/automatic checks.
 */
export async function refreshUpdateStatus(fetchRemote = true, force = false): Promise<UpdateStatus | null> {
	const now = Date.now();
	if (!force && now - lastCheckAt < RECHECK_COOLDOWN_MS) {
		return null;
	}
	lastCheckAt = now;
	updateChecking.set(true);
	updateError.set(null);
	try {
		const status = await getUpdateStatus(fetchRemote);
		if (fetchRemote) storageSet(LAST_REMOTE_CHECK_KEY, String(Date.now()));
		updateStatus.set(status);
		return status;
	} catch (e) {
		updateError.set(e instanceof Error ? e.message : 'Update check failed');
		return null;
	} finally {
		updateChecking.set(false);
	}
}

/**
 * The automatic check: download from the update server at most once a day,
 * otherwise just compare against what was last downloaded (no network).
 */
export function runScheduledUpdateCheck(): Promise<UpdateStatus | null> {
	const last = Number(storageGet(LAST_REMOTE_CHECK_KEY) ?? 0);
	const due = !Number.isFinite(last) || Date.now() - last >= DAY_MS;
	return refreshUpdateStatus(due, true);
}

export async function changeUpdateChannel(channel: UpdateChannel): Promise<void> {
	updateError.set(null);
	try {
		await setUpdateChannel(channel);
	} catch (e) {
		updateError.set(e instanceof Error ? e.message : 'Could not change the update channel');
		return;
	}
	await refreshUpdateStatus(false, true);
}

/** What identifies "this" update, so a snooze or a newer update can be told apart. */
export function updateKey(status: UpdateStatus | null): string | null {
	if (!status?.update_available) return null;
	return status.target_version ?? status.target_sha ?? null;
}

function readSnoozeUntil(key: string | null): number {
	if (!key) return 0;
	try {
		const raw = JSON.parse(storageGet(SNOOZE_KEY) ?? 'null');
		return raw && raw.key === key ? Number(raw.until) || 0 : 0;
	} catch {
		return 0;
	}
}

const snoozeTick = writable(0);

/** True while "Remind me tomorrow" hides this update. Urgent updates never hide. */
export const updateSnoozed = derived([updateStatus, snoozeTick], ([$status]) => {
	if (!$status?.update_available || $status.urgent) return false;
	return readSnoozeUntil(updateKey($status)) > Date.now();
});

export function snoozeUpdate(): void {
	const key = updateKey(get(updateStatus));
	if (!key) return;
	storageSet(SNOOZE_KEY, JSON.stringify({ key, until: Date.now() + DAY_MS }));
	snoozeTick.update((n) => n + 1);
	updatePanelOpen.set(false);
}

export interface JustUpdated {
	from_version?: string;
	to_version?: string | null;
	channel: UpdateChannel;
	/** The top changes, for the "You're on …" card. */
	highlights: string[];
	at: number;
}

export function readJustUpdated(): JustUpdated | null {
	try {
		const raw = JSON.parse(storageGet(JUST_UPDATED_KEY) ?? 'null') as JustUpdated | null;
		if (!raw || Date.now() - raw.at > DAY_MS) return null;
		return raw;
	} catch {
		return null;
	}
}

export function clearJustUpdated(): void {
	storageRemove(JUST_UPDATED_KEY);
}

function highlightsOf(status: UpdateStatus): string[] {
	const fromReleases = (status.pending_releases ?? []).flatMap((r) => r.sections.flatMap((s) => s.items));
	const items = fromReleases.length > 0 ? fromReleases : (status.pending_commits ?? []);
	return items.slice(0, 3);
}

async function waitForBackendRestart(timeoutMs = 120_000): Promise<boolean> {
	const start = Date.now();
	// The backend has to drop before it comes back; give it a beat so we don't
	// immediately succeed against the still-up old process.
	await new Promise((resolve) => setTimeout(resolve, 1500));
	while (Date.now() - start < timeoutMs) {
		try {
			await checkHealth();
			return true;
		} catch {
			await new Promise((resolve) => setTimeout(resolve, 2000));
		}
	}
	return false;
}

function failStep(step: number, error: string): void {
	updateProgress.set({ step, failed: true, error, done: false });
}

/**
 * Download → Install → Restart → Back online. Each step shows in
 * `updateProgress`; a failure stops on that step with its reason so the UI can
 * offer Retry. Reloads the page once the new backend is up.
 */
export async function runUpdate(reload: () => void = () => window.location.reload()): Promise<void> {
	if (get(updateBusy)) return;
	updateError.set(null);

	// Retrying a restart that timed out: the new code is already installed, so
	// just wait for the backend again.
	if (get(updateProgress)?.failed && get(updateProgress)?.step === 2) {
		await finishRestart(reload);
		return;
	}

	updateProgress.set({ step: 0, failed: false, error: null, done: false });
	let status: UpdateStatus;
	try {
		status = await getUpdateStatus(true);
		updateStatus.set(status);
	} catch (e) {
		failStep(0, e instanceof Error ? e.message : "Couldn't reach the update server.");
		return;
	}
	if (!status.update_available) {
		updateProgress.set(null);
		return;
	}
	if (!status.can_apply) {
		failStep(0, status.blocked_reason || status.reason || "This update can't be installed from the app.");
		return;
	}

	updateProgress.set({ step: 1, failed: false, error: null, done: false });
	try {
		const result = await applyUpdate(false);
		if (result.status !== 'updated') {
			failStep(1, result.reason || 'The update could not be installed.');
			return;
		}
		if (!result.restart_pending) {
			updateProgress.set(null);
			await refreshUpdateStatus(false, true);
			return;
		}
		const record: JustUpdated = {
			from_version: result.from_version ?? status.current_version,
			to_version: result.to_version ?? status.target_version ?? null,
			channel: status.channel,
			highlights: highlightsOf(status),
			at: Date.now(),
		};
		storageSet(JUST_UPDATED_KEY, JSON.stringify(record));
	} catch (e) {
		failStep(1, e instanceof Error ? e.message : 'The update could not be installed.');
		return;
	}

	await finishRestart(reload);
}

async function finishRestart(reload: () => void): Promise<void> {
	updateProgress.set({ step: 2, failed: false, error: null, done: false });
	const cameBack = await waitForBackendRestart();
	if (!cameBack) {
		failStep(2, "Forven didn't come back after restarting. Check the launcher window or its logs, then retry.");
		return;
	}

	updateProgress.set({ step: 3, failed: false, error: null, done: true });
	// A beat so the last tick is visible, then load the new frontend.
	setTimeout(reload, 800);
}

/** Close a failed run so the panel shows the update again. */
export function dismissUpdateProgress(): void {
	if (get(updateProgress)?.failed) updateProgress.set(null);
}
