<script lang="ts">
	import { fly } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { onDestroy } from 'svelte';
	import {
		addToast,
		clearSnooze,
		dismissToast,
		getSnoozeOptions,
		snoozeNotifications,
		snoozeUntil,
		toasts,
		type ToastItem,
	} from '$lib/stores/toasts';
	import { getCategory } from '$lib/notifications/catalog';
	import { setNotificationPrefs } from '$lib/stores/notificationPrefs';

	const SETTINGS_HREF = '/settings#notifications';

	// Auto-dismiss timers, keyed by toast id. A repeat folds into a visible
	// pop-up by bumping its createdAt, which restarts its timer here.
	const timers = new Map<string, { handle: ReturnType<typeof setTimeout>; createdAt: number }>();
	let hoveredId: string | null = null;

	function clearTimer(id: string) {
		const timer = timers.get(id);
		if (timer) {
			clearTimeout(timer.handle);
			timers.delete(id);
		}
	}

	function syncTimers(list: ToastItem[]) {
		const live = new Set(list.map((t) => t.id));
		for (const id of [...timers.keys()]) if (!live.has(id)) clearTimer(id);
		for (const t of list) {
			if (t.id === hoveredId) continue;
			const timer = timers.get(t.id);
			if (timer && timer.createdAt === t.createdAt) continue;
			clearTimer(t.id);
			timers.set(t.id, {
				handle: setTimeout(() => {
					timers.delete(t.id);
					dismissToast(t.id);
				}, t.duration),
				createdAt: t.createdAt,
			});
		}
	}

	$: syncTimers($toasts);

	// Reading a pop-up pauses it; leaving restarts its full time.
	function pause(t: ToastItem) {
		hoveredId = t.id;
		clearTimer(t.id);
	}

	function resume() {
		hoveredId = null;
		syncTimers($toasts);
	}

	onDestroy(() => {
		timers.forEach((timer) => clearTimeout(timer.handle));
		timers.clear();
	});

	function open(t: ToastItem) {
		clearTimer(t.id);
		dismissToast(t.id);
		if (t.href) goto(t.href);
	}

	function dismiss(t: ToastItem) {
		clearTimer(t.id);
		dismissToast(t.id);
	}

	function muteKey(t: ToastItem): string | null {
		const popup = getCategory(t.category)?.popup;
		return popup && !popup.locked && popup.key ? popup.key : null;
	}

	async function mute(t: ToastItem) {
		const key = muteKey(t);
		if (!key) return;
		dismiss(t);
		const label = getCategory(t.category)?.label ?? 'These';
		try {
			await setNotificationPrefs({ [key]: false });
			addToast(`${label} pop-ups are off. Turn them back on in Settings → Notifications.`, 'info', SETTINGS_HREF, 7000);
		} catch (err) {
			addToast(`Could not mute ${label.toLowerCase()} pop-ups: ${err instanceof Error ? err.message : 'unknown error'}`, 'error');
		}
	}

	function formatPnl(pnl: number): string {
		const pct = pnl * 100;
		return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
	}

	function borderColor(type: ToastItem['type']): string {
		switch (type) {
			case 'success': return 'border-emerald-900';
			case 'error': return 'border-red-900';
			case 'warning': return 'border-yellow-900';
			default: return 'border-sc-line2';
		}
	}

	function formatSnoozeRemaining(ms: number): string {
		const minutes = Math.ceil(ms / 60000);
		if (minutes < 60) return `${minutes}m`;
		const hours = Math.floor(minutes / 60);
		const mins = minutes % 60;
		if (mins === 0) return `${hours}h`;
		return `${hours}h ${mins}m`;
	}

	// ---- snooze
	let showSnoozeMenu = false;
	let snoozeMenuRef: HTMLDivElement | null = null;
	const snoozeOptions = getSnoozeOptions();

	function handleSnooze(durationMs: number) {
		snoozeNotifications(durationMs);
		showSnoozeMenu = false;
	}

	function openSettings() {
		showSnoozeMenu = false;
		goto(SETTINGS_HREF);
	}

	function handleClickOutside(event: MouseEvent) {
		if (snoozeMenuRef && !snoozeMenuRef.contains(event.target as Node)) {
			showSnoozeMenu = false;
		}
	}

	$: if (typeof window !== 'undefined') {
		if (showSnoozeMenu) window.addEventListener('click', handleClickOutside, true);
		else window.removeEventListener('click', handleClickOutside, true);
	}

	onDestroy(() => {
		if (typeof window !== 'undefined') window.removeEventListener('click', handleClickOutside, true);
	});

	// `$snoozeUntil > Date.now()` only re-evaluates when a store changes, so tick
	// a clock: the "Paused" strip must clear itself when the snooze runs out.
	let now = Date.now();
	const nowTimer = typeof window === 'undefined' ? null : setInterval(() => (now = Date.now()), 15_000);
	onDestroy(() => {
		if (nowTimer !== null) clearInterval(nowTimer);
	});

	$: snoozeActive = $snoozeUntil > now;
	// The pause control belongs with event notifications, not with the answer
	// to something the operator just clicked.
	$: hasEventToasts = $toasts.some((t) => Boolean(t.category));
</script>

{#snippet body(t: ToastItem)}
	<div class="flex items-center gap-1.5 min-w-0">
		<span class="text-xs text-sc-ink truncate">{t.message}</span>
		{#if t.repeat > 1}
			<span class="shrink-0 text-[10px] text-sc-ink3 tabular-nums" title="{t.repeat} times">×{t.repeat}</span>
		{/if}
	</div>
	{#if t.detail || typeof t.pnlPct === 'number'}
		<div class="mt-0.5 flex items-center gap-2 min-w-0 text-[11px]">
			{#if typeof t.pnlPct === 'number'}
				<span class="shrink-0 font-mono tabular-nums {t.pnlPct >= 0 ? 'text-emerald-400' : 'text-red-400'}">{formatPnl(t.pnlPct)}</span>
			{/if}
			{#if t.detail}
				<span class="text-sc-ink3 truncate">{t.detail}</span>
			{/if}
		</div>
	{/if}
{/snippet}

{#if $toasts.length > 0 || snoozeActive}
	<div class="flex flex-col items-end gap-2 pointer-events-none" data-testid="toast-stack">
		{#each $toasts as t (t.id)}
			<div
				class="rounded-md pointer-events-auto bg-sc-panel border {borderColor(t.type)} pl-3 pr-2 py-2.5 w-[320px] max-w-[calc(100vw-2rem)] text-left flex items-start gap-2.5 group transition-colors {t.href ? 'hover:bg-sc-panel2' : ''}"
				transition:fly={{ x: 300, duration: 250 }}
				role="group"
				data-category={t.category ?? ''}
				on:mouseenter={() => pause(t)}
				on:mouseleave={resume}
			>
				<div class="flex-shrink-0 mt-0.5">
					{#if t.type === 'success'}
						<svg class="w-4 h-4 text-emerald-400" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
							<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
						</svg>
					{:else if t.type === 'error'}
						<svg class="w-4 h-4 text-red-500" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
							<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
						</svg>
					{:else if t.type === 'warning'}
						<svg class="w-4 h-4 text-yellow-400" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
							<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-sc-ink2" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
							<path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
						</svg>
					{/if}
				</div>

				{#if t.href}
					<button type="button" class="flex-1 min-w-0 text-left cursor-pointer" title="Open" on:click={() => open(t)}>
						{@render body(t)}
					</button>
				{:else}
					<div class="flex-1 min-w-0" role="status">
						{@render body(t)}
					</div>
				{/if}

				<div class="flex-shrink-0 flex items-center gap-0.5">
					{#if muteKey(t)}
						<button
							type="button"
							class="opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity text-[11px] text-sc-ink3 hover:text-sc-ink px-1.5 py-0.5 rounded"
							title="Stop showing pop-ups like this one"
							on:click|stopPropagation={() => mute(t)}
						>
							Mute
						</button>
					{/if}
					<button
						type="button"
						aria-label="Dismiss notification"
						class="text-sc-ink3 hover:text-sc-ink transition-colors p-1"
						on:click|stopPropagation={() => dismiss(t)}
					>
						<svg class="w-3 h-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
							<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
						</svg>
					</button>
				</div>
			</div>
		{/each}

		{#if snoozeActive}
			<!-- Always reachable while a pause runs, even with nothing on screen. -->
			<div class="rounded-md pointer-events-auto bg-sc-panel border border-sc-line2 px-3 py-2 flex items-center gap-2" data-testid="snooze-strip">
				<svg class="w-3 h-3 text-yellow-400" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
					<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
				</svg>
				<span class="text-[11px] text-sc-ink2">
					Pop-ups paused for {formatSnoozeRemaining($snoozeUntil - now)} · safety alerts still show
				</span>
				<button class="text-[12px] text-sc-ink3 hover:text-sc-ink ml-2" on:click={clearSnooze}>Resume</button>
			</div>
		{:else if hasEventToasts}
			<div class="pointer-events-auto relative" bind:this={snoozeMenuRef}>
				<button
					class="rounded-md bg-sc-panel border border-sc-line2 px-3 py-1.5 flex items-center gap-2 hover:bg-sc-panel2 transition-colors"
					on:click|stopPropagation={() => (showSnoozeMenu = !showSnoozeMenu)}
					title="Pause pop-ups"
				>
					<svg class="w-3 h-3 text-sc-ink2" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
						<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
					</svg>
					<span class="text-[11px] text-sc-ink2">Pause pop-ups</span>
				</button>

				{#if showSnoozeMenu}
					<div
						class="rounded-md absolute bottom-full right-0 mb-1 bg-sc-panel border border-sc-line2 py-1 min-w-[180px]"
						transition:fly={{ y: 10, duration: 150 }}
					>
						{#each snoozeOptions as option}
							<button
								class="w-full text-left px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-panel2 hover:text-sc-ink transition-colors"
								on:click|stopPropagation={() => handleSnooze(option.ms)}
							>
								For {option.label}
							</button>
						{/each}
						<div class="border-t border-sc-line my-1"></div>
						<button
							class="w-full text-left px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-panel2 hover:text-sc-ink transition-colors"
							on:click|stopPropagation={openSettings}
						>
							Notification settings…
						</button>
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
