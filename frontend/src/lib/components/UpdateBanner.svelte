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
	const POLL_MS = 24 * 60 * 60 * 1000; // re-check daily

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
		// Startup check, then re-check daily so a newly pushed update surfaces
		// without a manual reload (kept infrequent so frequent pushes don't spam
		// users). Each call is one git fetch; errors are swallowed by the store.
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
		class="border-b border-[#222] bg-[#050505] text-[#888] px-4 py-2 flex items-center justify-between gap-3"
		role="status"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg class="w-4 h-4 text-[#888] shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2a10 10 0 100 20 10 10 0 000-20zm1 5v6l5 3-.75 1.23L11 14V7h2z" />
			</svg>
			<div class="text-[11px] leading-snug min-w-0">
				{#if $updateRestarting}
					<span class="font-bold text-white">Restarting…</span> applying update and waiting for the backend to come back.
				{:else if $updateApplying}
					<span class="font-bold text-white">Updating…</span> pulling the latest code.
				{:else}
					<span class="font-bold text-white"
						>Update available{#if behind > 0} — {behind} commit{behind === 1 ? '' : 's'} behind{/if}</span
					>{#if status?.latest_commit_subject}: <span class="truncate">{status.latest_commit_subject}</span
						>{/if}{#if status?.blocked_reason}<span class="block text-[#666] mt-0.5"
							>{status.blocked_reason}</span
						>{/if}
				{/if}
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			{#if status?.can_apply && !busy}
				<button
					type="button"
					class="terminal-button-primary px-2.5 py-1 text-[11px]"
					on:click={handleApply}
				>
					Update &amp; restart
				</button>
			{:else if !busy}
				<button
					type="button"
					class="terminal-button px-2.5 py-1 text-[11px]"
					on:click={() => goto('/settings#system')}
				>
					Open Settings
				</button>
			{/if}
			{#if busy}
				<span class="w-3.5 h-3.5 border-2 border-[#333] border-t-white rounded-full animate-spin" aria-hidden="true"></span>
			{:else}
				<button
					type="button"
					class="text-[11px] text-[#555] hover:text-white px-2 transition-colors"
					on:click={handleDismiss}
					aria-label="Dismiss"
				>
					✕
				</button>
			{/if}
		</div>
	</div>
	{#if $updateError && !busy}
		<div class="border-b border-red-900 bg-red-500/5 text-red-400 px-4 py-1.5 text-[11px]" role="alert">
			{$updateError}
		</div>
	{/if}
{/if}
