/**
 * Shared self-update state. The startup banner and the Settings → System panel
 * both read/drive this so a check or apply in one place reflects in the other.
 */

import { writable } from 'svelte/store';
import { checkHealth } from '$lib/api';
import {
	getUpdateStatus,
	applyUpdate,
	type UpdateStatus,
	type ApplyUpdateResult,
} from '$lib/api/updates';

export const updateStatus = writable<UpdateStatus | null>(null);
export const updateChecking = writable(false);
export const updateApplying = writable(false);
export const updateError = writable<string | null>(null);
/** Set while the backend is restarting after an applied update. */
export const updateRestarting = writable(false);

let lastCheckAt = 0;
const RECHECK_COOLDOWN_MS = 15_000;

/**
 * Refresh the cached update status.
 * @param fetchRemote hit the remote (default) or compare against the last fetch.
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
		updateStatus.set(status);
		return status;
	} catch (e) {
		updateError.set(e instanceof Error ? e.message : 'Update check failed');
		return null;
	} finally {
		updateChecking.set(false);
	}
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

/**
 * Apply the pending update. When code was pulled the backend restarts; this
 * resolves once it is healthy again (caller typically reloads the page).
 */
export async function applyUpdateAndWait(): Promise<ApplyUpdateResult> {
	updateApplying.set(true);
	updateError.set(null);
	try {
		const result = await applyUpdate();
		if (!result.restart_pending) {
			if (result.status === 'blocked' || result.status === 'busy') {
				updateError.set(result.reason ?? 'Update could not be applied.');
			}
			await refreshUpdateStatus(false, true);
			return result;
		}
		updateRestarting.set(true);
		const cameBack = await waitForBackendRestart();
		if (!cameBack) {
			updateError.set('Backend did not come back after restart. Check the launcher logs.');
		}
		return result;
	} catch (e) {
		updateError.set(e instanceof Error ? e.message : 'Update failed');
		throw e;
	} finally {
		updateApplying.set(false);
	}
}
