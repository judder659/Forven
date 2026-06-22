<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		updateStatus,
		updateApplying,
		updateRestarting,
		updateError,
		refreshUpdateStatus,
		applyUpdateAndWait,
	} from '$lib/stores/updateStatus';

	let dismissed = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	const SESSION_KEY = 'forven.update_banner.dismissed';
	const POLL_MS = 60 * 60 * 1000; // re-check hourly

	function handleDismiss() {
		dismissed = true;
		try {
			// Re-key per available remote sha so a *newer* update re-surfaces the
			// banner even after the operator dismissed an earlier one this session.
			sessionStorage.setItem(SESSION_KEY, $updateStatus?.remote_sha ?? '1');
		} catch {
			// sessionStorage unavailable; dismiss for the lifetime of this mount.
		}
	}

	async function handleApply() {
		const result = await applyUpdateAndWait();
		if (result.restart_pending) {
			// New code is live on the freshly restarted backend — reload so the
			// browser picks up the new frontend assets too.
			window.location.reload();
		}
	}

	onMount(() => {
		// Startup check, then re-check hourly so a newly pushed update surfaces
		// without a manual reload. Each call is one git fetch; errors are swallowed
		// by the store.
		void refreshUpdateStatus(true);
		pollTimer = setInterval(() => void refreshUpdateStatus(true), POLL_MS);
	});

	onDestroy(() => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	});

	$: status = $updateStatus;
	$: dismissedSha = (() => {
		try {
			return sessionStorage.getItem(SESSION_KEY);
		} catch {
			return null;
		}
	})();
	$: alreadyDismissed =
		dismissed || (status?.remote_sha != null && dismissedSha === status.remote_sha);
	$: busy = $updateApplying || $updateRestarting;
	$: visible =
		busy || (!alreadyDismissed && Boolean(status?.supported && status?.update_available));
	$: behind = status?.behind ?? 0;
</script>

{#if visible}
	<div
		class="border-b border-cyan-800/70 bg-cyan-950/30 text-cyan-100 px-4 py-2 flex items-center justify-between gap-3"
		role="status"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg class="w-4 h-4 text-cyan-400 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2a10 10 0 100 20 10 10 0 000-20zm1 5v6l5 3-.75 1.23L11 14V7h2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				{#if $updateRestarting}
					<span class="font-bold">Restarting…</span> applying update and waiting for the backend to come back.
				{:else if $updateApplying}
					<span class="font-bold">Updating…</span> pulling the latest code.
				{:else}
					<span class="font-bold"
						>Update available{#if behind > 0} — {behind} commit{behind === 1 ? '' : 's'} behind{/if}</span
					>{#if status?.latest_commit_subject}: <span class="truncate">{status.latest_commit_subject}</span
						>{/if}{#if status?.blocked_reason}<span class="block text-cyan-300/80 mt-0.5"
							>{status.blocked_reason}</span
						>{/if}
				{/if}
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			{#if status?.can_apply && !busy}
				<button
					type="button"
					class="text-[11px] border border-cyan-600 text-cyan-50 px-2.5 py-1 rounded hover:bg-cyan-900/40 transition-colors"
					on:click={handleApply}
				>
					Update &amp; restart
				</button>
			{:else if !busy}
				<button
					type="button"
					class="text-[11px] border border-cyan-700 text-cyan-100 px-2.5 py-1 rounded hover:bg-cyan-900/40 transition-colors"
					on:click={() => goto('/settings#system')}
				>
					Open Settings
				</button>
			{/if}
			{#if busy}
				<span class="w-3.5 h-3.5 border-2 border-cyan-400/40 border-t-cyan-300 rounded-full animate-spin" aria-hidden="true"></span>
			{:else}
				<button
					type="button"
					class="text-[11px] text-cyan-300/70 hover:text-cyan-200 px-2"
					on:click={handleDismiss}
					aria-label="Dismiss"
				>
					✕
				</button>
			{/if}
		</div>
	</div>
	{#if $updateError && !busy}
		<div class="border-b border-rose-800/70 bg-rose-950/30 text-rose-100 px-4 py-1.5 text-[11px]" role="alert">
			{$updateError}
		</div>
	{/if}
{/if}
