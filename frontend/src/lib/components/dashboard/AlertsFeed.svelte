<script lang="ts">
	/**
	 * Always-visible alerts feed: errors and warnings from the activity log,
	 * newest first. Dismiss acknowledges everything currently shown (persisted
	 * locally), so the feed goes quiet until something NEW goes wrong — the
	 * right semantics for an always-on monitor.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getRecentSystemAlerts, type SystemAlertEntry } from '$lib/api/dashboard';

	const REFRESH_MS = 30_000;
	const DISMISS_KEY = 'dashboard.alerts.dismissedThroughId';

	let alerts: SystemAlertEntry[] = [];
	let dismissedThroughId = 0;
	let loaded = false;
	let error = '';
	let consecutiveFailures = 0;
	let timer: ReturnType<typeof setInterval> | null = null;

	$: visible = alerts.filter((alert) => Number(alert.id) > dismissedThroughId);
	$: dismissedCount = alerts.length - visible.length;

	async function load(): Promise<void> {
		try {
			alerts = await getRecentSystemAlerts(40);
			loaded = true;
			error = '';
			consecutiveFailures = 0;
		} catch (err) {
			consecutiveFailures += 1;
			if (consecutiveFailures >= 2) {
				error = err instanceof Error ? err.message : 'Alerts unavailable.';
			}
		}
	}

	function dismissAll(): void {
		const newest = alerts.reduce((max, alert) => Math.max(max, Number(alert.id) || 0), 0);
		if (newest <= 0) return;
		dismissedThroughId = newest;
		try {
			localStorage.setItem(DISMISS_KEY, String(newest));
		} catch {
			// localStorage unavailable — dismiss still applies for this session.
		}
	}

	function showDismissed(): void {
		dismissedThroughId = 0;
		try {
			localStorage.removeItem(DISMISS_KEY);
		} catch {
			// ignore
		}
	}

	function timeLabel(createdAt: string): string {
		const iso = createdAt.includes('T') ? createdAt : `${createdAt.replace(' ', 'T')}Z`;
		const parsed = Date.parse(iso);
		if (Number.isNaN(parsed)) return createdAt;
		const mins = Math.max(0, (Date.now() - parsed) / 60_000);
		if (mins < 1) return 'now';
		if (mins < 90) return `${Math.round(mins)}m`;
		if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h`;
		return `${(mins / 1440).toFixed(1)}d`;
	}

	let expandedId: string | null = null;

	function toggleExpand(id: string): void {
		expandedId = expandedId === id ? null : id;
	}

	function exactTime(createdAt: string): string {
		const iso = createdAt.includes('T') ? createdAt : `${createdAt.replace(' ', 'T')}Z`;
		const parsed = Date.parse(iso);
		return Number.isNaN(parsed) ? createdAt : new Date(parsed).toLocaleString();
	}

	/** Split a message into text and S/H/T-id tokens so ids become deep links. */
	function linkableParts(message: string): Array<{ text: string; href: string | null }> {
		const parts: Array<{ text: string; href: string | null }> = [];
		const pattern = /\b([SHT]\d{5,})\b/g;
		let cursor = 0;
		for (const match of message.matchAll(pattern)) {
			const index = match.index ?? 0;
			if (index > cursor) parts.push({ text: message.slice(cursor, index), href: null });
			const token = match[1];
			const href = token.startsWith('S')
				? `/lab/strategy/${token}`
				: token.startsWith('H')
					? `/hypotheses?focus=${token}`
					: null;
			parts.push({ text: token, href });
			cursor = index + token.length;
		}
		if (cursor < message.length) parts.push({ text: message.slice(cursor), href: null });
		return parts;
	}

	function detailEntries(details: Record<string, unknown> | null): Array<[string, string]> {
		if (!details) return [];
		return Object.entries(details).map(([key, value]) => [
			key,
			typeof value === 'string' ? value : JSON.stringify(value, null, 1),
		]);
	}

	onMount(() => {
		try {
			dismissedThroughId = Number(localStorage.getItem(DISMISS_KEY) ?? 0) || 0;
		} catch {
			dismissedThroughId = 0;
		}
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="terminal-card flex h-full min-h-0 flex-col" data-testid="alerts-feed">
	<div class="flex items-center justify-between gap-2 border-b border-[#1a1a1a] px-4 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Alerts</h2>
		<span class="flex items-center gap-2">
			{#if dismissedCount > 0}
				<button
					class="font-mono text-[10px] text-[#555] hover:text-[#888] transition-colors"
					on:click={showDismissed}
					title="Show the {dismissedCount} dismissed alert(s)"
				>{dismissedCount} hidden</button>
			{/if}
			{#if visible.length > 0}
				<button
					class="border border-[#333] px-1.5 py-0.5 font-mono text-[10px] text-[#888] hover:bg-[#111] hover:text-white transition-colors"
					on:click={dismissAll}
					data-testid="alerts-dismiss"
				>dismiss</button>
			{/if}
			<span class="font-mono text-[10px] {visible.length === 0 ? 'text-emerald-400' : 'text-[#888]'}">
				{loaded ? (visible.length === 0 ? 'clear' : `${visible.length}`) : '…'}
			</span>
		</span>
	</div>
	<div class="min-h-0 flex-1 overflow-y-auto px-2.5 py-1.5">
		{#if error && visible.length === 0}
			<div class="text-xs text-red-400">{error}</div>
		{:else if !loaded}
			<div class="text-xs uppercase tracking-widest text-[#555]">Loading…</div>
		{:else if visible.length === 0}
			<div class="text-xs text-[#555]">No new alerts. ✓</div>
		{:else}
			<ul class="space-y-1">
				{#each visible as alert (alert.id)}
					{@const expanded = expandedId === alert.id}
					<li class="text-[11px] leading-snug">
						<button
							type="button"
							class="flex w-full items-start gap-2 text-left hover:bg-[#111] transition-colors"
							on:click={() => toggleExpand(alert.id)}
							aria-expanded={expanded}
							data-testid="alert-row-{alert.id}"
						>
							<span class={alert.level === 'error' || alert.level === 'critical' ? 'mt-0.5 text-red-400' : 'mt-0.5 text-yellow-400'}>
								{alert.level === 'error' || alert.level === 'critical' ? '●' : '▲'}
							</span>
							<span class="min-w-0 flex-1 text-[#aaa] {expanded ? 'break-words' : 'truncate'}">
								<span class="text-[#666]">[{alert.source}]</span>
								{alert.message}
							</span>
							<span class="shrink-0 font-mono text-[10px] text-[#555]">{timeLabel(alert.createdAt)}</span>
						</button>
						{#if expanded}
							<div class="mb-1 ml-4 mt-1 space-y-1 border border-[#1a1a1a] bg-black px-2 py-1.5" data-testid="alert-detail-{alert.id}">
								<div class="font-mono text-[10px] text-[#666]">
									{exactTime(alert.createdAt)} · {alert.source} · {alert.level} · #{alert.id}
								</div>
								<div class="break-words text-[#aaa]">
									{#each linkableParts(alert.message) as part}
										{#if part.href}
											<a href={part.href} class="text-white hover:underline" on:click|stopPropagation>{part.text}</a>
										{:else}{part.text}{/if}
									{/each}
								</div>
								{#if alert.details}
									<dl class="max-h-[140px] space-y-0.5 overflow-y-auto font-mono text-[10px]">
										{#each detailEntries(alert.details) as [key, value]}
											<div class="flex gap-2">
												<dt class="shrink-0 text-[#666]">{key}</dt>
												<dd class="min-w-0 break-words text-[#888]">{value}</dd>
											</div>
										{/each}
									</dl>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</div>
