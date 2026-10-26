<script lang="ts">
	import { onMount } from 'svelte';
	import type { UpdateChannel } from '$lib/api/updates';
	import {
		updateStatus,
		updateChecking,
		updateBusy,
		updateError,
		updatePanelOpen,
		refreshUpdateStatus,
		changeUpdateChannel,
	} from '$lib/stores/updateStatus';
	import ReleaseNotes from '$lib/components/updates/ReleaseNotes.svelte';

	const CHANNELS: { value: UpdateChannel; label: string; hint: string }[] = [
		{ value: 'stable', label: 'Stable', hint: 'A tested release once a week, with notes. Recommended.' },
		{ value: 'latest', label: 'Latest', hint: 'Every change as soon as it lands. For testers.' },
	];

	let openRelease: string | null = null;
	let switching = false;

	async function handleCheck() {
		await refreshUpdateStatus(true, true);
	}

	async function pickChannel(value: UpdateChannel) {
		if (switching || status?.channel === value || status?.channel_locked) return;
		switching = true;
		try {
			await changeUpdateChannel(value);
		} finally {
			switching = false;
		}
	}

	function formatDate(value: string | undefined): string {
		if (!value) return '';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime())
			? value
			: dt.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
	}

	onMount(() => {
		// Show last-known state immediately without a network hit; the automatic
		// daily check (or Check now) downloads.
		if ($updateStatus == null) void refreshUpdateStatus(false, true);
	});

	$: status = $updateStatus;
	$: channel = status?.channel ?? 'stable';
	$: versionLabel = status?.current_version
		? `${status.current_version}${status.commits_past_release ? ` + ${status.commits_past_release} change${status.commits_past_release === 1 ? '' : 's'}` : ''}`
		: '—';
	$: statusLine = (() => {
		if (!status) return 'Not checked yet.';
		if (!status.supported) return status.reason || "This install can't update itself.";
		if (status.update_available) {
			return status.target_version
				? `Forven ${status.target_version} is available.`
				: `${status.behind ?? 0} new change${status.behind === 1 ? '' : 's'} available.`;
		}
		return status.reason ? `Up to date. ${status.reason}` : 'Up to date.';
	})();
</script>

<div class="terminal-card divide-y divide-sc-line" data-testid="updates-card">
	<!-- Version + status -->
	<div class="flex flex-wrap items-start justify-between gap-4 p-4">
		<div class="min-w-0">
			<p class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Installed version</p>
			<p class="mt-1 font-plex-mono text-[22px] font-medium leading-none text-sc-ink">{versionLabel}</p>
			<p class="mt-2 text-[12px] {status?.update_available ? (status.urgent ? 'text-amber-300' : 'text-emerald-400') : 'text-sc-ink3'}" role="status">
				{statusLine}
			</p>
			{#if status?.checked_at}
				<p class="mt-0.5 text-[11px] text-sc-ink4">Last checked {new Date(status.checked_at).toLocaleString()}</p>
			{/if}
		</div>
		<div class="flex shrink-0 items-center gap-2">
			<button type="button" class="terminal-button" on:click={handleCheck} disabled={$updateChecking || $updateBusy}>
				{$updateChecking ? 'Checking…' : 'Check now'}
			</button>
			{#if status?.update_available}
				<button type="button" class="terminal-button-primary" on:click={() => updatePanelOpen.set(true)}>
					{$updateBusy ? 'Updating…' : "See what's new"}
				</button>
			{/if}
		</div>
	</div>

	{#if status?.update_available && status?.blocked_reason}
		<p class="px-4 py-2 text-[12px] text-yellow-300">{status.blocked_reason}</p>
	{/if}
	{#if $updateError}
		<p class="px-4 py-2 text-[12px] text-red-400" role="alert">{$updateError}</p>
	{/if}

	<!-- Channel -->
	<div class="p-4">
		<p class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Update channel</p>
		<div class="mt-2 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Update channel">
			{#each CHANNELS as option (option.value)}
				<button
					type="button"
					role="radio"
					aria-checked={channel === option.value}
					class="rounded-md border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed {channel === option.value
						? 'border-sc-ink bg-sc-raise'
						: 'border-sc-line2 bg-sc-panel hover:border-sc-ink4'}"
					on:click={() => pickChannel(option.value)}
					disabled={switching || Boolean(status?.channel_locked)}
				>
					<span class="flex items-center gap-2 text-[13px] font-medium text-sc-ink">
						<span class="flex h-3.5 w-3.5 items-center justify-center rounded-full border {channel === option.value ? 'border-sc-ink' : 'border-sc-ink4'}">
							{#if channel === option.value}<span class="h-1.5 w-1.5 rounded-full bg-sc-ink"></span>{/if}
						</span>
						{option.label}
					</span>
					<span class="mt-0.5 block pl-[22px] text-[11.5px] leading-snug text-sc-ink3">{option.hint}</span>
				</button>
			{/each}
		</div>
		{#if status?.channel_locked}
			<p class="mt-2 text-[11px] text-sc-ink3">Set by FORVEN_UPDATE_CHANNEL in your .env file.</p>
		{/if}
	</div>

	<!-- Release history -->
	<div class="p-4">
		<p class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Releases</p>
		{#if status?.releases?.length}
			<ul class="mt-2 divide-y divide-sc-line rounded-md border border-sc-line">
				{#each status.releases as release (release.tag)}
					{@const isCurrent = release.version === status.current_version}
					<li>
						<button
							type="button"
							class="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-sc-hover transition-colors"
							on:click={() => (openRelease = openRelease === release.tag ? null : release.tag)}
							aria-expanded={openRelease === release.tag}
						>
							<span class="flex min-w-0 items-center gap-2">
								<span class="font-plex-mono text-[13px] text-sc-ink">{release.version}</span>
								{#if isCurrent}
									<span class="rounded border border-sc-line2 px-1.5 text-[10px] font-medium uppercase tracking-[0.06em] text-sc-ink2">Installed</span>
								{/if}
								{#if release.urgent}
									<span class="rounded border border-amber-500/50 px-1.5 text-[10px] font-medium uppercase tracking-[0.06em] text-amber-300">Urgent</span>
								{/if}
							</span>
							<span class="flex shrink-0 items-center gap-2 text-[11.5px] text-sc-ink3">
								{formatDate(release.date)}
								<svg class="h-3 w-3 transition-transform {openRelease === release.tag ? 'rotate-180' : ''}" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
									<path fill-rule="evenodd" d="M5.3 7.3a1 1 0 0 1 1.4 0L10 10.6l3.3-3.3a1 1 0 1 1 1.4 1.4l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 0 1 0-1.4Z" clip-rule="evenodd" />
								</svg>
							</span>
						</button>
						{#if openRelease === release.tag}
							<div class="border-t border-sc-line bg-sc-panel2/50 px-3 py-3">
								{#if release.sections.length > 0}
									<ReleaseNotes sections={release.sections} />
								{:else}
									<p class="text-[12px] text-sc-ink3">{release.title}</p>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{:else}
			<p class="mt-2 text-[12px] text-sc-ink3">No releases yet. The first one appears here once it's published.</p>
		{/if}
	</div>
</div>
