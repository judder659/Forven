<script lang="ts">
	import { onMount } from 'svelte';
	import {
		updateStatus,
		updateChecking,
		updateApplying,
		updateRestarting,
		updateError,
		refreshUpdateStatus,
		applyUpdateAndWait,
	} from '$lib/stores/updateStatus';

	async function handleCheck() {
		await refreshUpdateStatus(true, true);
	}

	async function handleApply() {
		const result = await applyUpdateAndWait();
		if (result.restart_pending) {
			window.location.reload();
		}
	}

	onMount(() => {
		// Show last-known state immediately without forcing a remote hit; the
		// startup banner (or a manual click) drives fresh checks.
		if ($updateStatus == null) void refreshUpdateStatus(false, true);
	});

	$: status = $updateStatus;
	$: busy = $updateChecking || $updateApplying || $updateRestarting;
	$: statusLabel = (() => {
		if (!status) return 'Not checked yet.';
		if (!status.supported) return status.reason || "This install can't self-update.";
		if (status.update_available) {
			const n = status.behind ?? 0;
			return `Update available — ${n} commit${n === 1 ? '' : 's'} behind ${status.target_remote}/${status.target_branch}.`;
		}
		return status.reason ? `Up to date (note: ${status.reason})` : 'Up to date.';
	})();
</script>

<div class="terminal-card p-4 space-y-3">
	<div class="flex items-start justify-between gap-3">
		<div class="min-w-0">
			<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Software updates</h3>
			<p class="text-[11px] text-[#666] mt-0.5">
				Fast-forward Forven to the latest code on
				<span class="text-[#888]">{status?.target_remote ?? 'origin'}/{status?.target_branch ?? 'main'}</span>.
				Applying restarts the backend.
			</p>
		</div>
		<button
			type="button"
			class="terminal-button text-[11px] shrink-0"
			on:click={handleCheck}
			disabled={busy}
		>
			{$updateChecking ? 'Checking…' : 'Check for updates'}
		</button>
	</div>

	<dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
		<dt class="text-[#666]">Current version</dt>
		<dd class="text-white font-mono">{status?.current_version ?? '—'}{#if status?.current_sha_short} ({status.current_sha_short}){/if}</dd>
		<dt class="text-[#666]">Branch</dt>
		<dd class="text-white font-mono">{status?.current_branch ?? '—'}</dd>
		{#if status?.checked_at}
			<dt class="text-[#666]">Last checked</dt>
			<dd class="text-[#888]">{new Date(status.checked_at).toLocaleString()}</dd>
		{/if}
	</dl>

	<div class="flex items-center justify-between gap-3 pt-1">
		<p
			class="text-[11px] {status?.update_available ? 'text-emerald-400' : 'text-[#666]'}"
			role="status"
		>
			{#if $updateRestarting}
				Restarting backend and waiting for it to come back…
			{:else if $updateApplying}
				Pulling the latest code…
			{:else}
				{statusLabel}
			{/if}
		</p>
		{#if status?.can_apply}
			<button
				type="button"
				class="terminal-button-primary text-[11px] shrink-0"
				on:click={handleApply}
				disabled={busy}
			>
				Update &amp; restart
			</button>
		{/if}
	</div>

	{#if status?.update_available && status?.blocked_reason}
		<p class="text-[11px] text-yellow-400">{status.blocked_reason}</p>
	{/if}
	{#if status?.update_available && status?.latest_commit_subject}
		<p class="text-[11px] text-[#666] truncate">Latest: {status.latest_commit_subject}</p>
	{/if}
	{#if $updateError}
		<p class="text-[11px] text-red-400" role="alert">{$updateError}</p>
	{/if}
</div>
