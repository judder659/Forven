<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		acknowledgeNotification,
		acknowledgeNotifications,
		getNotificationFeed,
		type ForvenNotification,
	} from '$lib/api';
	import { triggerHeartbeat } from '$lib/stores/heartbeat';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import {
		loadLastSeenNotificationId,
		persistLastSeenNotificationId,
		splitBySeenBaseline,
	} from '$lib/utils/notificationSeen';

	// Same page size as the nav-badge summary (get_actionable_notification_summary
	// limit=50) so the inbox is exactly what the badge counted.
	const FETCH_LIMIT = 50;
	const LOG_PAGE_SIZE = 50;
	const REFRESH_FALLBACK_MS = 60_000;

	type Tab = 'inbox' | 'log';
	let activeTab: Tab = 'inbox';

	let items: ForvenNotification[] = [];
	let loading = true;
	let error = '';
	let ackInFlight = new Set<number>();
	let ackAllInFlight = false;
	let expanded = new Set<number>();
	let poller: RealtimeRefreshController | null = null;

	// New-since-last-visit baseline: captured once on mount, so items streaming
	// in while the page is open still count as NEW; the next visit resets it.
	// 0 = first-ever visit → nothing is "new", just establish the baseline.
	let seenBaseline = 0;

	let logItems: ForvenNotification[] = [];
	let logLoading = false;
	let logLoadingMore = false;
	let logHasMore = false;
	let logError = '';

	function severityChip(severity: string): string {
		switch ((severity || '').toLowerCase()) {
			case 'critical':
				return 'text-red-400 border-red-900 bg-red-500/10';
			case 'fail':
				return 'text-red-400 border-red-900 bg-red-500/10';
			case 'warn':
				return 'text-yellow-400 border-yellow-900 bg-yellow-500/10';
			default:
				return 'text-[#888] border-[#333] bg-[#111]';
		}
	}

	function severityDot(severity: string): string {
		switch ((severity || '').toLowerCase()) {
			case 'critical':
			case 'fail':
				return 'bg-red-500';
			case 'warn':
				return 'bg-yellow-400';
			default:
				return 'bg-[#444]';
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function toggleExpanded(id: number) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expanded = next;
	}

	async function refreshInbox(): Promise<void> {
		try {
			const feed = await getNotificationFeed({ limit: FETCH_LIMIT, actionable: true });
			items = feed.items ?? [];
			error = '';
			const maxId = items.reduce((acc, item) => Math.max(acc, item.id), 0);
			if (seenBaseline === 0 && maxId > 0) {
				// First-ever visit: establish the baseline without flagging history.
				seenBaseline = maxId;
			}
			persistLastSeenNotificationId(maxId);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load notifications.';
		} finally {
			loading = false;
		}
	}

	async function loadLog(reset: boolean): Promise<void> {
		if (reset) {
			logLoading = true;
			logError = '';
		} else {
			if (logLoadingMore || !logHasMore) return;
			logLoadingMore = true;
		}
		try {
			const beforeId = reset ? undefined : logItems[logItems.length - 1]?.id;
			const feed = await getNotificationFeed({ limit: LOG_PAGE_SIZE, before_id: beforeId });
			const page = feed.items ?? [];
			logItems = reset ? page : [...logItems, ...page];
			logHasMore = page.length >= LOG_PAGE_SIZE;
		} catch (err) {
			logError = err instanceof Error ? err.message : 'Failed to load the notification log.';
		} finally {
			logLoading = false;
			logLoadingMore = false;
		}
	}

	function switchTab(tab: Tab): void {
		if (activeTab === tab) return;
		activeTab = tab;
		if (tab === 'log') void loadLog(true);
	}

	async function handleAcknowledge(notification: ForvenNotification): Promise<void> {
		if (ackInFlight.has(notification.id)) return;
		ackInFlight = new Set(ackInFlight).add(notification.id);
		const previous = items;
		items = items.filter((item) => item.id !== notification.id);
		try {
			await acknowledgeNotification(notification.id);
			triggerHeartbeat();
		} catch (err) {
			items = previous;
			error = `Acknowledge of #${notification.id} failed: ${err instanceof Error ? err.message : 'unknown error'}`;
		} finally {
			const next = new Set(ackInFlight);
			next.delete(notification.id);
			ackInFlight = next;
		}
	}

	async function handleAcknowledgeAll(): Promise<void> {
		if (ackAllInFlight || items.length === 0) return;
		ackAllInFlight = true;
		const previous = items;
		const ids = items.map((item) => item.id);
		items = [];
		try {
			await acknowledgeNotifications(ids);
			triggerHeartbeat();
		} catch (err) {
			items = previous;
			error = `Acknowledge all failed: ${err instanceof Error ? err.message : 'unknown error'}`;
		} finally {
			ackAllInFlight = false;
		}
	}

	onMount(() => {
		seenBaseline = loadLastSeenNotificationId();
		poller = createRealtimeRefresh(refreshInbox, {
			fallbackMs: REFRESH_FALLBACK_MS,
			wsDebounceMs: 1_500,
		});
		poller.start();
	});

	onDestroy(() => {
		poller?.stop();
		poller = null;
	});

	$: inboxSplit = splitBySeenBaseline(items, seenBaseline);
	$: inboxEntries = [
		...inboxSplit.fresh.map((item) => ({ item, isNew: true })),
		...inboxSplit.earlier.map((item) => ({ item, isNew: false })),
	];
</script>

<div class="border border-[#222] bg-[#050505]">
	<div class="px-4 py-3 border-b border-[#222] flex items-center justify-between gap-4">
		<div class="flex items-center gap-4">
			<h2 class="text-sm font-bold uppercase tracking-wider text-[#888]">Notifications</h2>
			<div class="flex items-center gap-1">
				<button
					class="text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors {activeTab === 'inbox'
						? 'border-white text-white bg-[#1a1a1a]'
						: 'border-[#333] text-[#666] hover:text-white'}"
					on:click={() => switchTab('inbox')}
				>
					Inbox
					{#if items.length > 0}
						<span class="ml-1 text-[10px] font-bold px-1.5 {inboxSplit.fresh.length > 0 ? 'bg-red-500 text-white' : 'bg-[#333] text-[#888]'}">{items.length}</span>
					{/if}
				</button>
				<button
					class="text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors {activeTab === 'log'
						? 'border-white text-white bg-[#1a1a1a]'
						: 'border-[#333] text-[#666] hover:text-white'}"
					on:click={() => switchTab('log')}
				>
					Log
				</button>
			</div>
		</div>
		{#if activeTab === 'inbox' && items.length > 0}
			<button
				class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-[#555] transition-colors disabled:opacity-60"
				on:click={() => void handleAcknowledgeAll()}
				disabled={ackAllInFlight}
				title="Mark every listed notification as acknowledged"
			>
				{ackAllInFlight ? 'Acknowledging…' : 'Acknowledge all'}
			</button>
		{/if}
	</div>

	{#if activeTab === 'inbox'}
		{#if error}
			<div class="px-4 py-3 text-xs text-red-400 border-b border-[#222]">{error}</div>
		{/if}

		{#if loading}
			<div class="px-4 py-6 text-center text-xs text-[#666]">Loading notifications…</div>
		{:else if items.length === 0}
			<div class="px-4 py-6 text-center text-xs text-[#666]">
				No actionable notifications. New critical issues (risk, trade failures, system health) appear here.
			</div>
		{:else}
			<div class="divide-y divide-[#1a1a1a]">
				{#each inboxEntries as entry, i (entry.item.id)}
					{@const item = entry.item}
					{@const isOpen = expanded.has(item.id)}
					{#if i > 0 && !entry.isNew && inboxEntries[i - 1].isNew}
						<div class="px-4 py-1.5 text-[10px] uppercase tracking-[0.18em] text-[#555] bg-[#0a0a0a]">
							Seen on an earlier visit
						</div>
					{/if}
					<div class="px-4 py-3 {entry.isNew ? 'border-l-2 border-l-white bg-white/[0.02]' : ''}">
						<div class="flex items-start justify-between gap-4">
							<button
								class="flex items-start gap-3 min-w-0 text-left flex-1"
								on:click={() => toggleExpanded(item.id)}
								aria-expanded={isOpen}
							>
								<span class="mt-1 inline-block w-2 h-2 rounded-full shrink-0 {severityDot(item.severity)}"></span>
								<div class="min-w-0">
									<div class="text-xs font-bold text-white truncate">
										{#if entry.isNew}
											<span class="text-[9px] font-bold uppercase tracking-wider text-white border border-[#555] px-1 py-px mr-1.5 align-middle">New</span>
										{/if}
										{item.title}
									</div>
									{#if item.summary}
										<div class="text-[11px] text-[#888] mt-0.5 {isOpen ? '' : 'truncate'}">{item.summary}</div>
									{/if}
									<div class="text-[10px] text-[#555] mt-0.5">
										{item.source} · {item.event_type} · {formatTimestamp(item.created_at)}
									</div>
								</div>
							</button>
							<div class="flex items-center gap-2 shrink-0">
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border {severityChip(item.severity)}">
									{item.severity}
								</span>
								<button
									class="text-xs border border-[#333] px-2 py-1 text-[#888] hover:text-white hover:border-[#555] transition-colors disabled:opacity-60"
									on:click={() => void handleAcknowledge(item)}
									disabled={ackInFlight.has(item.id)}
									title="Mark as acknowledged"
								>
									Ack
								</button>
							</div>
						</div>
						{#if isOpen && (item.body || item.delivery_error)}
							<div class="mt-3 ml-5 border-l border-[#222] pl-4 space-y-2">
								{#if item.body}
									<div class="text-[11px] text-[#888] whitespace-pre-wrap break-words">{item.body}</div>
								{/if}
								{#if item.delivery_error}
									<div class="text-[11px] text-red-400 break-all">delivery error: {item.delivery_error}</div>
								{/if}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	{:else}
		{#if logError}
			<div class="px-4 py-3 text-xs text-red-400 border-b border-[#222]">{logError}</div>
		{/if}

		{#if logLoading}
			<div class="px-4 py-6 text-center text-xs text-[#666]">Loading log…</div>
		{:else if logItems.length === 0}
			<div class="px-4 py-6 text-center text-xs text-[#666]">No notifications recorded yet.</div>
		{:else}
			<div class="divide-y divide-[#1a1a1a]">
				{#each logItems as item (item.id)}
					{@const isOpen = expanded.has(item.id)}
					{@const acked = Boolean(item.acknowledged_at)}
					<div class="px-4 py-2.5 {acked ? 'opacity-50' : ''}">
						<div class="flex items-start justify-between gap-4">
							<button
								class="flex items-start gap-3 min-w-0 text-left flex-1"
								on:click={() => toggleExpanded(item.id)}
								aria-expanded={isOpen}
							>
								<span class="mt-1 inline-block w-2 h-2 rounded-full shrink-0 {severityDot(item.severity)}"></span>
								<div class="min-w-0">
									<div class="text-xs font-bold text-white truncate">{item.title}</div>
									{#if item.summary}
										<div class="text-[11px] text-[#888] mt-0.5 {isOpen ? '' : 'truncate'}">{item.summary}</div>
									{/if}
									<div class="text-[10px] text-[#555] mt-0.5">
										{item.source} · {item.event_type} · {formatTimestamp(item.created_at)}
										{#if acked}
											· acked {formatTimestamp(item.acknowledged_at)}
										{/if}
									</div>
								</div>
							</button>
							<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border shrink-0 {severityChip(item.severity)}">
								{item.severity}
							</span>
						</div>
						{#if isOpen && (item.body || item.delivery_error)}
							<div class="mt-3 ml-5 border-l border-[#222] pl-4 space-y-2">
								{#if item.body}
									<div class="text-[11px] text-[#888] whitespace-pre-wrap break-words">{item.body}</div>
								{/if}
								{#if item.delivery_error}
									<div class="text-[11px] text-red-400 break-all">delivery error: {item.delivery_error}</div>
								{/if}
							</div>
						{/if}
					</div>
				{/each}
			</div>
			{#if logHasMore}
				<div class="px-4 py-3 border-t border-[#222] text-center">
					<button
						class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-[#555] transition-colors disabled:opacity-60"
						on:click={() => void loadLog(false)}
						disabled={logLoadingMore}
					>
						{logLoadingMore ? 'Loading…' : 'Load older'}
					</button>
				</div>
			{/if}
		{/if}
	{/if}
</div>
