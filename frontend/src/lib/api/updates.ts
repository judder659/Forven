/**
 * In-app self-update API: compare the local checkout to the tracked remote
 * branch and fast-forward to it from inside the app.
 */

import { fetchApi } from './core';

export interface UpdateStatus {
	/** False when the install isn't a git checkout (can't self-update). */
	supported: boolean;
	/** Non-fatal note: offline fetch, unknown remote ref, etc. */
	reason?: string;
	current_version: string;
	current_branch?: string;
	target_branch?: string;
	target_remote?: string;
	on_target_branch?: boolean;
	current_sha?: string;
	current_sha_short?: string;
	remote_sha?: string;
	remote_sha_short?: string;
	behind?: number;
	ahead?: number;
	dirty?: boolean;
	update_available: boolean;
	/** True when the update can be fast-forwarded with no operator intervention. */
	can_apply: boolean;
	/** When update_available but !can_apply, why (dirty tree, wrong branch, …). */
	blocked_reason?: string;
	latest_commit_subject?: string;
	latest_commit_date?: string;
	checked_at?: string;
}

export type ApplyUpdateStatus = 'updated' | 'noop' | 'blocked' | 'busy' | 'unsupported';

export interface ApplyUpdateResult {
	status: ApplyUpdateStatus;
	/** True when new code was pulled and the backend is restarting. */
	restart_pending: boolean;
	reason?: string;
	from_sha_short?: string;
	to_sha_short?: string;
	pull_output?: string;
}

/**
 * Check for updates. Pass `fetchRemote = false` to skip the network round-trip
 * and compare against the last-known remote ref (used for the startup check so
 * it can't hang the UI when offline).
 */
export function getUpdateStatus(fetchRemote = true): Promise<UpdateStatus> {
	return fetchApi<UpdateStatus>(`/api/system/update/check?fetch=${fetchRemote ? 'true' : 'false'}`, {
		timeoutMs: 90_000,
	});
}

/** Fast-forward to the tracked remote branch; restarts the backend if code changed. */
export function applyUpdate(): Promise<ApplyUpdateResult> {
	return fetchApi<ApplyUpdateResult>('/api/system/update/apply', {
		method: 'POST',
		timeoutMs: 90_000,
	});
}
