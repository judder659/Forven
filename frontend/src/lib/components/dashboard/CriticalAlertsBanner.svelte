<script lang="ts">
	/**
	 * Thin banner at the top of the dashboard surfacing critical health alerts
	 * (GET /api/health/alerts?severity=critical). Renders nothing when there
	 * are no critical alerts or when the endpoint is unavailable — the banner
	 * must never add noise or break the dashboard.
	 *
	 * The endpoint only returns alerts whose condition still holds, so the
	 * banner clears by itself on recovery. Dismiss hides the current alerts;
	 * any new alert brings the banner back.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		getCriticalHealthAlerts,
		type HealthAlertItem,
	} from '$lib/api/dashboard';

	const REFRESH_MS = 30_000;
	const FETCH_LIMIT = 25;
	const DISMISSED_KEY = 'forven.criticalAlerts.dismissed';

	let fetched: HealthAlertItem[] = [];
	let dismissed = new Set<string>(readDismissed());
	let expanded = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	$: alerts = fetched.filter((a) => !dismissed.has(alertKey(a)));
	$: latest = alerts[0] ?? null;
	$: restCount = Math.max(0, alerts.length - 1);

	function alertKey(a: HealthAlertItem): string {
		return `${a.component}|${a.message}|${a.timestamp}`;
	}

	function readDismissed(): string[] {
		try {
			const raw = localStorage.getItem(DISMISSED_KEY);
			const parsed = raw ? JSON.parse(raw) : [];
			return Array.isArray(parsed) ? parsed.map(String) : [];
		} catch {
			return [];
		}
	}

	function saveDismissed(keys: Set<string>): void {
		try {
			localStorage.setItem(DISMISSED_KEY, JSON.stringify([...keys]));
		} catch {
			// Storage unavailable: dismissal still holds for this page view.
		}
	}

	function dismiss(): void {
		// Keep only keys for alerts still being reported, so the stored list
		// never grows past the current set.
		const next = new Set(fetched.map(alertKey));
		dismissed = next;
		expanded = false;
		saveDismissed(next);
	}

	function formatRelative(iso: string): string {
		const ts = Date.parse(iso);
		if (!Number.isFinite(ts)) return '';
		const ageMin = Math.max(0, (Date.now() - ts) / 60000);
		if (ageMin < 1) return 'just now';
		if (ageMin < 60) return `${Math.round(ageMin)}m ago`;
		const ageH = ageMin / 60;
		if (ageH < 24) return `${Math.round(ageH)}h ago`;
		return `${Math.round(ageH / 24)}d ago`;
	}

	async function load(): Promise<void> {
		try {
			const res = await getCriticalHealthAlerts(FETCH_LIMIT);
			fetched = res.alerts;
		} catch {
			// Endpoint unavailable — keep last-known alerts; never surface a
			// fetch error as a fake critical banner.
		}
	}

	function tick() {
		if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
		void load();
	}

	onMount(() => {
		void load();
		timer = setInterval(tick, REFRESH_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

{#if latest}
	<div
		class="flex-shrink-0 border border-red-900 bg-red-500/5 text-xs"
		role="alert"
		data-testid="critical-alerts-banner"
	>
		<div class="flex items-center gap-2 px-4 py-1.5">
			<span
				class="border border-red-900 bg-red-500/10 px-1.5 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-red-400"
			>
				Critical
			</span>
			<span class="text-red-400 font-bold" data-testid="critical-alerts-count">
				{alerts.length} alert{alerts.length === 1 ? '' : 's'}
			</span>
			<span class="text-red-400/80 truncate min-w-0" title={latest.message}>
				{#if latest.component}<span class="font-mono text-red-400">{latest.component}</span> —{/if}
				{latest.message}
			</span>
			{#if latest.timestamp}
				<span class="text-red-400/60 whitespace-nowrap">{formatRelative(latest.timestamp)}</span>
			{/if}
			{#if restCount > 0}
				<button
					type="button"
					class="rounded-md ml-auto px-2 py-0.5 border border-red-900 text-red-400 hover:bg-red-500/10 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 whitespace-nowrap"
					on:click={() => (expanded = !expanded)}
					aria-expanded={expanded}
					data-testid="critical-alerts-toggle"
				>
					{expanded ? 'Show less' : `+${restCount} more`}
				</button>
			{/if}
			<button
				type="button"
				class="rounded-md {restCount > 0 ? '' : 'ml-auto'} px-2 py-0.5 border border-red-900 text-red-400 hover:bg-red-500/10 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 whitespace-nowrap"
				on:click={dismiss}
				title="Hide these alerts. A new alert shows the banner again."
				data-testid="critical-alerts-dismiss"
			>
				Dismiss
			</button>
		</div>
		{#if expanded && restCount > 0}
			<ul
				class="border-t border-red-900 max-h-[160px] overflow-y-auto divide-y divide-red-900/40"
				data-testid="critical-alerts-list"
			>
				{#each alerts.slice(1) as alert (alert.component + alert.message + alert.timestamp)}
					<li class="px-4 py-1 flex items-baseline gap-2 text-red-400/90">
						{#if alert.component}
							<span class="font-mono text-red-400 whitespace-nowrap">{alert.component}</span>
						{/if}
						<span class="truncate min-w-0" title={alert.message}>{alert.message}</span>
						{#if alert.timestamp}
							<span class="ml-auto text-red-400/60 whitespace-nowrap">
								{formatRelative(alert.timestamp)}
							</span>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}
