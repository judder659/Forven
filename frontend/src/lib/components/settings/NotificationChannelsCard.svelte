<script lang="ts">
	/**
	 * Settings → Notifications: for every kind of event, whether it pops up in
	 * the app and whether it goes to Discord; and which sidebar badges show.
	 * Rows come from the notification catalog; switches save as they are
	 * flipped (they are notification preferences, not part of the settings blob
	 * the save bar writes).
	 */
	import { onMount } from 'svelte';
	import { sendNotificationTest } from '$lib/api';
	import {
		NAV_BADGES,
		NOTIFICATION_CATEGORIES,
		NOTIFICATION_GROUPS,
		type NotificationCategory,
	} from '$lib/notifications/catalog';
	import {
		badgeEnabled,
		loadNotificationPrefs,
		notificationPrefs,
		popupEnabled,
		resetNotificationPrefs,
		setNotificationPrefs,
	} from '$lib/stores/notificationPrefs';
	import { addToast } from '$lib/stores/processTracker';

	export let settings: Record<string, unknown> = {};

	type Status = { kind: 'idle' | 'saving' | 'saved' | 'error'; text: string };
	let status: Status = { kind: 'idle', text: '' };
	let confirmReset = false;
	let testing = false;

	// Delivery goes through the bot (forven.notifications._discord_configured
	// checks the token); without one, notifications stay in the app.
	$: discordConnected = Boolean(settings.discord_bot_token_configured);
	$: discordMuted = String(settings.notification_level ?? 'all') === 'none';

	onMount(() => {
		void loadNotificationPrefs();
	});

	const groups = NOTIFICATION_GROUPS.map((group) => ({
		...group,
		categories: NOTIFICATION_CATEGORIES.filter((category) => category.group === group.id),
	})).filter((group) => group.categories.length > 0);

	function discordOn(prefs: Record<string, unknown>, category: NotificationCategory): boolean {
		const discord = category.discord;
		if (!discord) return false;
		if (discord.locked) return true;
		return discord.keys.every((key) => (typeof prefs[key] === 'boolean' ? (prefs[key] as boolean) : discord.default));
	}

	async function save(updates: Record<string, boolean>, what: string): Promise<void> {
		status = { kind: 'saving', text: 'Saving…' };
		try {
			await setNotificationPrefs(updates);
			status = { kind: 'saved', text: `Saved: ${what}` };
		} catch (err) {
			const message = err instanceof Error ? err.message : 'unknown error';
			status = { kind: 'error', text: `Could not save ${what}: ${message}` };
			addToast(`Could not save ${what}: ${message}`, 'error');
		}
	}

	function togglePopup(category: NotificationCategory, on: boolean): void {
		const key = category.popup?.key;
		if (!key) return;
		void save({ [key]: on }, `${category.label} pop-ups ${on ? 'on' : 'off'}`);
	}

	function toggleDiscord(category: NotificationCategory, on: boolean): void {
		const keys = category.discord?.keys ?? [];
		if (!keys.length) return;
		void save(Object.fromEntries(keys.map((key) => [key, on])), `${category.label} to Discord ${on ? 'on' : 'off'}`);
	}

	function toggleBadge(key: string, label: string, on: boolean): void {
		void save({ [key]: on }, `${label} badge ${on ? 'on' : 'off'}`);
	}

	async function reset(): Promise<void> {
		confirmReset = false;
		status = { kind: 'saving', text: 'Resetting…' };
		try {
			await resetNotificationPrefs();
			status = { kind: 'saved', text: 'Every switch is back to its default' };
		} catch (err) {
			const message = err instanceof Error ? err.message : 'unknown error';
			status = { kind: 'error', text: `Reset failed: ${message}` };
		}
	}

	async function sendTest(): Promise<void> {
		testing = true;
		try {
			await sendNotificationTest('notification_test');
			status = {
				kind: 'saved',
				text: discordConnected && !discordMuted ? 'Test sent: watch for a pop-up here and a message in Discord' : 'Test sent: watch for a pop-up here',
			};
		} catch (err) {
			const message = err instanceof Error ? err.message : 'unknown error';
			status = { kind: 'error', text: `Test failed: ${message}` };
		} finally {
			testing = false;
		}
	}

	$: popupsOn = NOTIFICATION_CATEGORIES.filter((c) => c.popup && popupEnabled($notificationPrefs, c.id)).length;
	$: popupsTotal = NOTIFICATION_CATEGORIES.filter((c) => c.popup).length;
</script>

{#snippet toggle(on: boolean, label: string, onChange: (next: boolean) => void, dim: boolean)}
	<button
		type="button"
		role="switch"
		aria-checked={on}
		aria-label={label}
		title={label}
		class="relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full border transition-colors {on
			? 'border-emerald-700 bg-emerald-500/30'
			: 'border-sc-line2 bg-sc-panel2'} {dim ? 'opacity-60' : ''}"
		on:click={() => onChange(!on)}
	>
		<span
			class="inline-block h-3 w-3 rounded-full transition-transform {on ? 'translate-x-[15px] bg-emerald-300' : 'translate-x-[2px] bg-sc-ink3'}"
		></span>
	</button>
{/snippet}

{#snippet locked(text: string, why: string)}
	<span class="inline-flex items-center gap-1 text-[11px] text-sc-ink3" title={why}>
		<svg class="h-3 w-3" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
			<path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd" />
		</svg>
		{text}
	</span>
{/snippet}

<section class="terminal-card" data-testid="notification-channels">
	<header class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-4 py-2">
		<h2 class="text-[14px] font-semibold text-sc-ink2">Where notifications go</h2>
		<div class="flex items-center gap-2">
			<button type="button" class="terminal-button text-[12px]" on:click={sendTest} disabled={testing}>
				{testing ? 'Sending…' : 'Send a test'}
			</button>
			{#if confirmReset}
				<button type="button" class="terminal-button-danger text-[12px]" on:click={reset}>Reset every switch?</button>
				<button type="button" class="terminal-button text-[12px]" on:click={() => (confirmReset = false)}>Keep</button>
			{:else}
				<button type="button" class="terminal-button text-[12px]" on:click={() => (confirmReset = true)}>Reset to defaults</button>
			{/if}
		</div>
	</header>

	<div class="space-y-4 p-4">
		<div class="flex flex-wrap items-start justify-between gap-3">
			<p class="max-w-2xl text-xs text-sc-ink2">
				For each kind of event, choose whether it pops up in the bottom-right corner and whether it goes to
				Discord. Changes save as you make them. {popupsOn} of {popupsTotal} pop-ups are on.
			</p>
			{#if status.text}
				<p
					class="text-[12px] {status.kind === 'error' ? 'text-red-400' : status.kind === 'saving' ? 'text-sc-ink3' : 'text-emerald-400'}"
					role="status"
				>
					{status.text}
				</p>
			{/if}
		</div>

		{#if !discordConnected}
			<p class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-2 text-[12px] text-sc-ink2">
				Discord isn't connected, so everything stays in the app. The Discord switches take effect once a
				Discord bot token is saved under Discord transport below.
			</p>
		{:else if discordMuted}
			<p class="rounded-md border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[12px] text-yellow-400">
				Discord delivery is switched off under Delivery policy below, so nothing goes to Discord whatever these
				switches say.
			</p>
		{/if}

		<div class="overflow-x-auto">
			<table class="w-full text-left text-xs">
				<thead>
					<tr class="border-b border-sc-line font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						<th class="py-2 pr-4 font-medium">Event</th>
						<th class="w-24 py-2 text-center font-medium">Pop-up</th>
						<th class="w-24 py-2 text-center font-medium">Discord</th>
					</tr>
				</thead>
				{#each groups as group (group.id)}
					<tbody>
						<tr>
							<th colspan="3" class="pt-4 pb-1 text-[12px] font-semibold text-sc-ink2" scope="colgroup">{group.label}</th>
						</tr>
						{#each group.categories as category (category.id)}
							<tr class="border-b border-sc-line/60" data-category={category.id}>
								<td class="py-2 pr-4">
									<div class="text-sc-ink">{category.label}</div>
									<div class="text-[11px] text-sc-ink3">{category.description}</div>
								</td>
								<td class="py-2 text-center">
									{#if !category.popup}
										<span class="text-sc-ink4" title="This event never pops up">—</span>
									{:else if category.popup.locked}
										{@render locked('Always', 'A safety alert: it always pops up, even while pop-ups are paused.')}
									{:else}
										{@render toggle(
											popupEnabled($notificationPrefs, category.id),
											`${category.label} pop-ups`,
											(next) => togglePopup(category, next),
											false,
										)}
									{/if}
								</td>
								<td class="py-2 text-center">
									{#if !category.discord}
										<span class="text-sc-ink4" title="This event is not sent to Discord">—</span>
									{:else if category.discord.locked}
										{@render locked('Always', category.discord.note || 'Always sent to Discord.')}
									{:else}
										{@render toggle(
											discordOn($notificationPrefs, category),
											`${category.label} to Discord`,
											(next) => toggleDiscord(category, next),
											!discordConnected || discordMuted,
										)}
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				{/each}
			</table>
		</div>
	</div>
</section>

<section class="terminal-card" data-testid="notification-badges">
	<header class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-4 py-2">
		<h2 class="text-[14px] font-semibold text-sc-ink2">Sidebar badges</h2>
	</header>
	<div class="space-y-3 p-4">
		<p class="max-w-2xl text-xs text-sc-ink2">
			The numbers next to pages in the left sidebar. Each is the real count and stays until the items are dealt
			with. It is bright while it holds something new since you last opened that page, and dims once you have.
			Diagnostics only counts issues raised since your last visit.
		</p>
		<ul class="divide-y divide-sc-line/60">
			{#each NAV_BADGES as badge (badge.id)}
				<li class="flex items-center justify-between gap-4 py-2" data-badge={badge.href}>
					<div>
						<div class="text-xs text-sc-ink">{badge.label}</div>
						<div class="text-[11px] text-sc-ink3">{badge.description}</div>
					</div>
					{@render toggle(
						badgeEnabled($notificationPrefs, badge.href),
						`${badge.label} badge`,
						(next) => toggleBadge(badge.key, badge.label, next),
						false,
					)}
				</li>
			{/each}
		</ul>
	</div>
</section>
