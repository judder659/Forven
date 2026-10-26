/**
 * In-app self-update API: compare the local checkout to what its update
 * channel offers (the newest release, or every commit on the tracked branch)
 * and fast-forward to it from inside the app.
 */

import { fetchApi } from './core';

export type UpdateChannel = 'stable' | 'latest';

export interface ReleaseNotesSection {
	/** "New", "Fixes", "Safety" — or empty for notes outside a heading. */
	title: string;
	items: string[];
}

export interface ReleaseInfo {
	version: string;
	tag: string;
	sha: string;
	sha_short: string;
	date: string;
	title: string;
	urgent: boolean;
	sections: ReleaseNotesSection[];
}

export interface UpdateStatus {
	/** False when the install isn't a git checkout (can't self-update). */
	supported: boolean;
	/** Non-fatal note: offline fetch, no releases yet, etc. */
	reason?: string;
	channel: UpdateChannel;
	/** True when FORVEN_UPDATE_CHANNEL decides the channel. */
	channel_locked: boolean;
	current_version: string;
	/** Commits this install sits past its release (latest channel). */
	commits_past_release?: number;
	current_branch?: string;
	target_branch?: string;
	target_remote?: string;
	on_target_branch?: boolean;
	current_sha?: string;
	current_sha_short?: string;
	target_sha?: string | null;
	target_sha_short?: string;
	/** Version the update brings (stable channel only). */
	target_version?: string | null;
	behind?: number;
	ahead?: number;
	dirty?: boolean;
	update_available: boolean;
	/** True when the update can be fast-forwarded with no operator intervention. */
	can_apply: boolean;
	/** When update_available but !can_apply, why (local edits, wrong branch, …). */
	blocked_reason?: string;
	/** True when a release this update brings is marked urgent. */
	urgent?: boolean;
	/** Releases the update brings, newest first (stable channel). */
	pending_releases?: ReleaseInfo[];
	/** Commit titles the update brings, newest first (latest channel). */
	pending_commits?: string[];
	latest_release?: ReleaseInfo | null;
	/** Recent releases, newest first. */
	releases: ReleaseInfo[];
	latest_commit_subject?: string;
	latest_commit_date?: string;
	checked_at?: string;
}

export type ApplyUpdateStatus = 'updated' | 'noop' | 'blocked' | 'busy' | 'unsupported' | 'failed';

export interface ApplyUpdateResult {
	status: ApplyUpdateStatus;
	/** True when new code was pulled and the backend is restarting. */
	restart_pending: boolean;
	reason?: string;
	from_version?: string;
	to_version?: string | null;
	from_sha_short?: string;
	to_sha_short?: string;
	pull_output?: string;
}

/**
 * Check for updates. Pass `fetchRemote = false` to skip the network round-trip
 * and compare against what was last downloaded (used on page load so it can't
 * hang the UI when offline).
 */
export function getUpdateStatus(fetchRemote = true): Promise<UpdateStatus> {
	return fetchApi<UpdateStatus>(`/api/system/update/check?fetch=${fetchRemote ? 'true' : 'false'}`, {
		timeoutMs: 90_000,
	});
}

/**
 * Fast-forward to the channel's target; restarts the backend if code changed.
 * `fetchRemote = false` installs what the last check downloaded.
 */
export function applyUpdate(fetchRemote = true): Promise<ApplyUpdateResult> {
	return fetchApi<ApplyUpdateResult>(`/api/system/update/apply?fetch=${fetchRemote ? 'true' : 'false'}`, {
		method: 'POST',
		timeoutMs: 90_000,
	});
}

export function setUpdateChannel(channel: UpdateChannel): Promise<{ channel: UpdateChannel; locked: boolean }> {
	return fetchApi('/api/system/update/channel', {
		method: 'POST',
		body: JSON.stringify({ channel }),
	});
}
