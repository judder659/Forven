<script lang="ts">
	import { fade, fly } from 'svelte/transition';
	import {
		updateStatus,
		updatePanelOpen,
		updateProgress,
		updateBusy,
		runUpdate,
		snoozeUpdate,
		dismissUpdateProgress,
	} from '$lib/stores/updateStatus';
	import ReleaseNotes from './ReleaseNotes.svelte';
	import UpdateSteps from './UpdateSteps.svelte';

	function close() {
		if ($updateBusy) return;
		dismissUpdateProgress();
		updatePanelOpen.set(false);
	}

	function handleKey(e: KeyboardEvent) {
		if (e.key === 'Escape' && $updatePanelOpen) close();
	}

	function formatDate(value: string | undefined): string {
		if (!value) return '';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime())
			? value
			: dt.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
	}

	$: status = $updateStatus;
	$: available = Boolean(status?.update_available);
	$: pendingReleases = status?.pending_releases ?? [];
	$: pendingCommits = status?.pending_commits ?? [];
	$: heading = (() => {
		if (!status) return "What's new";
		if (available && status.target_version) return `Forven ${status.target_version}`;
		if (available) return `${status.behind ?? 0} new change${status.behind === 1 ? '' : 's'}`;
		return "You're up to date";
	})();
	$: subheading = (() => {
		if (!status) return '';
		const current = `You're on ${status.current_version || 'an unknown version'}`;
		if (available && pendingReleases[0]) return `Released ${formatDate(pendingReleases[0].date)} · ${current}`;
		if (available) return `Latest channel · ${current}`;
		return current;
	})();
</script>

<svelte:window on:keydown={handleKey} />

{#if $updatePanelOpen}
	<div
		class="fixed inset-0 z-[1000] bg-black/50"
		transition:fade={{ duration: 150 }}
		on:click={close}
		aria-hidden="true"
	></div>
	<div
		class="fixed right-0 top-0 z-[1001] flex h-screen w-full flex-col border-sc-line bg-sc-bg shadow-2xl sm:max-w-[440px] sm:border-l"
		transition:fly={{ x: 440, duration: 220 }}
		role="dialog"
		aria-modal="true"
		aria-labelledby="whats-new-title"
		data-testid="whats-new-panel"
	>
		<header class="flex items-start justify-between gap-3 border-b border-sc-line px-5 pb-4 pt-5">
			<div class="min-w-0">
				<p class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">What's new</p>
				<h2 id="whats-new-title" class="mt-1 flex items-center gap-2 text-[20px] font-semibold text-sc-ink">
					{heading}
					{#if available && status?.urgent}
						<span class="rounded border border-amber-500/50 bg-amber-500/10 px-1.5 py-px text-[10.5px] font-semibold uppercase tracking-[0.06em] text-amber-300">Urgent</span>
					{/if}
				</h2>
				{#if subheading}
					<p class="mt-0.5 text-[12px] text-sc-ink3">{subheading}</p>
				{/if}
			</div>
			<button
				type="button"
				class="terminal-button-icon -mr-2 -mt-1"
				on:click={close}
				disabled={$updateBusy}
				aria-label="Close"
			>
				<svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
					<path d="M5.3 5.3a1 1 0 0 1 1.4 0L10 8.6l3.3-3.3a1 1 0 1 1 1.4 1.4L11.4 10l3.3 3.3a1 1 0 0 1-1.4 1.4L10 11.4l-3.3 3.3a1 1 0 0 1-1.4-1.4L8.6 10 5.3 6.7a1 1 0 0 1 0-1.4Z" />
				</svg>
			</button>
		</header>

		<div class="flex-1 space-y-5 overflow-y-auto px-5 py-4">
			{#if $updateProgress}
				<section class="rounded-md border border-sc-line bg-sc-panel p-4" aria-live="polite">
					<p class="mb-3 text-[13px] font-medium text-sc-ink">
						{$updateProgress.done
							? 'Update installed. Reloading…'
							: $updateProgress.failed
								? 'The update stopped.'
								: 'Updating Forven. Keep this tab open.'}
					</p>
					<UpdateSteps progress={$updateProgress} />
					{#if $updateProgress.failed && $updateProgress.error}
						<p class="mt-3 rounded-md border border-red-900 bg-red-500/5 px-3 py-2 text-[12px] leading-snug text-red-300" role="alert">
							{$updateProgress.error}
						</p>
					{/if}
				</section>
			{/if}

			{#if available && status?.blocked_reason && !$updateProgress}
				<p class="rounded-md border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[12px] leading-snug text-yellow-300">
					{status.blocked_reason}
				</p>
			{/if}

			{#if available && pendingReleases.length > 0}
				{#each pendingReleases as release, i (release.tag)}
					<section class={i > 0 ? 'border-t border-sc-line pt-4' : ''}>
						{#if pendingReleases.length > 1}
							<p class="mb-2 text-[12px] text-sc-ink3">
								<span class="font-medium text-sc-ink2">{release.version}</span> · {formatDate(release.date)}
							</p>
						{/if}
						{#if release.sections.length > 0}
							<ReleaseNotes sections={release.sections} limit={i === 0 ? 8 : 4} />
						{:else}
							<p class="text-[13px] text-sc-ink3">{release.title}</p>
						{/if}
					</section>
				{/each}
			{:else if available && pendingCommits.length > 0}
				<section>
					<p class="mb-2 text-[12px] text-sc-ink3">
						You're on the Latest channel, so every change is offered as soon as it lands.
					</p>
					<ReleaseNotes sections={[{ title: 'Changes', items: pendingCommits }]} limit={8} />
				</section>
			{:else if status?.latest_release}
				<section>
					<p class="mb-2 text-[12px] text-sc-ink3">
						In {status.latest_release.version}, released {formatDate(status.latest_release.date)}:
					</p>
					<ReleaseNotes sections={status.latest_release.sections} limit={8} />
				</section>
			{:else if status?.reason}
				<p class="text-[13px] text-sc-ink3">{status.reason}</p>
			{/if}
		</div>

		<footer class="flex flex-wrap items-center justify-end gap-2 border-t border-sc-line px-5 py-3">
			{#if $updateProgress?.failed}
				<button type="button" class="terminal-button" on:click={close}>Close</button>
				<button type="button" class="terminal-button-primary" on:click={() => runUpdate()}>Retry</button>
			{:else if available && !$updateProgress}
				{#if !status?.urgent}
					<button type="button" class="terminal-button" on:click={snoozeUpdate}>Remind me tomorrow</button>
				{/if}
				<button
					type="button"
					class="terminal-button-primary"
					on:click={() => runUpdate()}
					disabled={!status?.can_apply}
					title={status?.can_apply ? 'Download, install and restart Forven' : status?.blocked_reason}
				>
					Update now
				</button>
			{:else if !$updateProgress}
				<button type="button" class="terminal-button" on:click={close}>Close</button>
			{/if}
		</footer>
	</div>
{/if}
