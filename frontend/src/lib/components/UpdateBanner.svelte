<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		updateStatus,
		updateBusy,
		updatePanelOpen,
		runScheduledUpdateCheck,
	} from '$lib/stores/updateStatus';

	// Updates normally show only as the pill in the top bar. This banner is for
	// urgent releases (a safety or money fix), and it also owns the automatic
	// update check, since it is mounted once for the whole app.
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	const POLL_MS = 60 * 60 * 1000; // hourly look; downloads at most once a day
	const SESSION_KEY = 'forven.update_banner.dismissed';

	let dismissedKey: string | null = null;

	function handleDismiss() {
		dismissedKey = status?.target_version ?? null;
		try {
			sessionStorage.setItem(SESSION_KEY, dismissedKey ?? '1');
		} catch {
			// sessionStorage unavailable; dismiss for the lifetime of this mount.
		}
	}

	onMount(() => {
		try {
			dismissedKey = sessionStorage.getItem(SESSION_KEY);
		} catch {
			dismissedKey = null;
		}
		void runScheduledUpdateCheck();
		pollTimer = setInterval(() => void runScheduledUpdateCheck(), POLL_MS);
	});

	onDestroy(() => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	});

	$: status = $updateStatus;
	$: firstNote = status?.pending_releases?.[0]?.sections?.[0]?.items?.[0] ?? '';
	$: visible =
		Boolean(status?.supported && status?.update_available && status?.urgent) &&
		!$updateBusy &&
		dismissedKey !== (status?.target_version ?? null);
</script>

{#if visible}
	<div
		class="flex items-center justify-between gap-3 border-b border-amber-900/70 bg-amber-500/10 px-4 py-1.5 text-amber-200"
		role="status"
		data-testid="urgent-update-banner"
	>
		<p class="min-w-0 truncate text-[12px]">
			<span class="font-semibold">Urgent update: Forven {status?.target_version}</span>{#if firstNote}<span class="text-amber-200/80"> · {firstNote}</span>{/if}
		</p>
		<div class="flex shrink-0 items-center gap-2">
			<button
				type="button"
				class="rounded-md border border-amber-500/60 px-2.5 py-0.5 text-[11px] font-medium hover:bg-amber-500/20 transition-colors"
				on:click={() => updatePanelOpen.set(true)}
			>
				See what's new
			</button>
			<button
				type="button"
				class="px-1.5 text-[11px] text-amber-200/70 hover:text-amber-100 transition-colors"
				on:click={handleDismiss}
				aria-label="Dismiss"
			>
				✕
			</button>
		</div>
	</div>
{/if}
