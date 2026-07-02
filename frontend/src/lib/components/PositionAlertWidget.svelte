<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { fly } from 'svelte/transition';
	import { getPaperSessions } from '$lib/api';
	import type { PaperTradingSession } from '$lib/api';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { snoozeUntil, snoozeNotifications, getSnoozeOptions } from '$lib/stores/processTracker';

	interface PositionAlert {
		token: string;
		sessionId: string;
		strategyName: string;
		symbol: string;
		timeframe: string;
		entryPrice: number;
		positionSize: number;
		openedAt: string;
	}

	let positionAlerts: PositionAlert[] = [];

	let dismissedPositionTokens = new Set<string>();
	let positionAlertPoller: RealtimeRefreshController | null = null;
	let positionAlertInFlight = false;
	const POSITION_ALERT_POLL_MS = 25_000;
	const DISMISSED_STORAGE_KEY = 'forven.paper.dismissedPositionAlerts';

	let openSnoozeToken: string | null = null;
	const snoozeOptions = getSnoozeOptions();

	function handlePositionSnooze(durationMs: number) {
		snoozeNotifications(durationMs);
		openSnoozeToken = null;
		positionAlerts = [];
	}

	function handlePositionClickOutside(event: MouseEvent) {
		const target = event.target as Element | null;
		if (!target?.closest('[data-position-snooze-root]')) {
			openSnoozeToken = null;
		}
	}

	function getPositionToken(session: PaperTradingSession): string | null {
		const pos = session.position;
		if (!pos) return null;
		// The dismissal identity MUST stay stable for the life of an open position.
		// entry_time is NOT stable: when the backend trade has no opened_at it falls
		// back to a moving clock (strategy updated_at / now()), which used to mint a
		// fresh token every poll so the "Close" button never stuck. Prefer the trade
		// id; fall back to the position's invariant content (side + entry + size).
		const identity = (pos.id || '').trim() || `${pos.side}:${pos.entry_price}:${pos.size}`;
		return `${session.id}:${identity}`;
	}

	function loadDismissedTokens() {
		if (typeof window === 'undefined') return;
		try {
			const raw = window.localStorage.getItem(DISMISSED_STORAGE_KEY);
			if (!raw) return;
			const parsed = JSON.parse(raw);
			if (Array.isArray(parsed)) {
				dismissedPositionTokens = new Set(parsed.filter((t): t is string => typeof t === 'string'));
			}
		} catch {
			// Corrupt/unavailable storage — start fresh.
		}
	}

	function persistDismissedTokens() {
		if (typeof window === 'undefined') return;
		try {
			window.localStorage.setItem(DISMISSED_STORAGE_KEY, JSON.stringify(Array.from(dismissedPositionTokens)));
		} catch {
			// Ignore quota/availability errors; dismissal still holds in-memory.
		}
	}

	function toPositionAlert(session: PaperTradingSession, token: string): PositionAlert {
		return {
			token,
			sessionId: session.id,
			strategyName: session.strategy_name,
			symbol: session.symbol,
			timeframe: session.timeframe,
			entryPrice: session.position?.entry_price ?? 0,
			positionSize: session.position?.size ?? 0,
			openedAt: session.position?.entry_time ?? '',
		};
	}

	function formatPrice(value: number): string {
		return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
	}

	function formatDateTime(value: string): string {
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '--';
		return date.toLocaleString();
	}

	function dismissPositionAlert(token: string) {
		dismissedPositionTokens.add(token);
		persistDismissedTokens();
		if (openSnoozeToken === token) openSnoozeToken = null;
		positionAlerts = positionAlerts.filter((alert) => alert.token !== token);
	}

	function openSessionFromAlert(alert: PositionAlert) {
		// Acting on the alert is an acknowledgement — dismiss it like Close does.
		dismissPositionAlert(alert.token);
		if (typeof window === 'undefined') return;
		// Persist for the cross-page case: navigating to /trading mounts the page,
		// which reads this key and selects the session.
		window.localStorage.setItem('forven.paper.selectedSessionId', alert.sessionId);
		// Live channel for the already-on-/trading case: the same-URL <a> nav is a
		// no-op, so the page never re-reads the key. Tell the mounted page directly.
		window.dispatchEvent(new CustomEvent('forven:select-session', { detail: { sessionId: alert.sessionId } }));
	}

	async function refreshPositionAlerts() {
		if (positionAlertInFlight) return;
		positionAlertInFlight = true;
		try {
			const sessions = await getPaperSessions();
			const openSessions = sessions.filter((session) => session.position !== null);
			const activeTokens = new Set<string>();
			for (const session of openSessions) {
				const token = getPositionToken(session);
				if (token) activeTokens.add(token);
			}

			let prunedDismissed = false;
			for (const token of Array.from(dismissedPositionTokens)) {
				if (!activeTokens.has(token)) {
					dismissedPositionTokens.delete(token);
					prunedDismissed = true;
				}
			}
			if (prunedDismissed) persistDismissedTokens();

			const nextAlerts: PositionAlert[] = [];
			for (const session of openSessions) {
				const token = getPositionToken(session);
				if (!token || dismissedPositionTokens.has(token)) continue;
				nextAlerts.push(toPositionAlert(session, token));
			}
			positionAlerts = nextAlerts;
		} catch {
			// Ignore intermittent API failures and retry next poll.
		} finally {
			positionAlertInFlight = false;
		}
	}

	function startPositionAlertPolling() {
		if (positionAlertPoller) return;
		positionAlertPoller = createRealtimeRefresh(refreshPositionAlerts, {
			fallbackMs: POSITION_ALERT_POLL_MS,
			wsDebounceMs: 1000,
			wsEvents: ['trade', 'task_completed', 'task_failed', 'kill_switch_activated', 'kill_switch_cleared'],
		});
		positionAlertPoller.start();
	}

	function stopPositionAlertPolling() {
		positionAlertPoller?.stop();
		positionAlertPoller = null;
	}

	onMount(() => {
		if (typeof window !== 'undefined') {
			window.addEventListener('click', handlePositionClickOutside, true);
		}
		loadDismissedTokens();
		startPositionAlertPolling();
	});

	onDestroy(() => {
		stopPositionAlertPolling();
		if (typeof window !== 'undefined') {
			window.removeEventListener('click', handlePositionClickOutside, true);
		}
	});
</script>

{#if $snoozeUntil <= Date.now()}
	{#each positionAlerts as alert (alert.token)}
		<div
			class="pointer-events-auto bg-[#050505] border border-emerald-900 px-4 py-3 min-w-[280px] max-w-sm"
			transition:fly={{ x: 300, duration: 250 }}
		>
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="text-[10px] uppercase tracking-wider text-emerald-400 font-bold">Position Open</div>
					<div class="text-xs text-white font-bold truncate">{alert.strategyName}</div>
					<div class="text-[10px] text-[#888] mt-0.5">{alert.symbol} / {alert.timeframe}</div>
				</div>
				<div class="flex items-center gap-2">
					<div class="relative" data-position-snooze-root>
						<button
							class="text-[10px] text-[#666] hover:text-white border border-[#333] hover:border-white px-2 py-0.5 flex items-center gap-1 transition-colors"
							on:click|stopPropagation={() => openSnoozeToken = openSnoozeToken === alert.token ? null : alert.token}
							title="Snooze notifications"
						>
							<svg class="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
								<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
							</svg>
							<span>Snooze</span>
						</button>

						{#if openSnoozeToken === alert.token}
							<div
								class="absolute bottom-full right-0 mb-1 bg-[#050505] border border-[#333] py-1 min-w-[140px] z-[10002]"
								transition:fly={{ y: 10, duration: 150 }}
							>
								<button
									class="w-full text-left px-3 py-1.5 text-[10px] text-emerald-400 hover:bg-[#111] flex items-center gap-2"
									on:click|stopPropagation={() => handlePositionSnooze(24 * 60 * 60 * 1000)}
								>
									<input type="checkbox" class="accent-emerald-500 pointer-events-none" checked />
									<span>Pause all alerts</span>
								</button>
								<div class="border-t border-[#222] my-1"></div>
								{#each snoozeOptions as option}
									<button
										class="w-full text-left px-3 py-1.5 text-[10px] text-[#888] hover:bg-[#111] hover:text-white transition-colors"
										on:click|stopPropagation={() => handlePositionSnooze(option.ms)}
									>
										{option.label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
					<button
						class="text-[10px] text-[#666] hover:text-white border border-[#333] hover:border-white px-2 py-0.5 transition-colors"
						on:click={() => dismissPositionAlert(alert.token)}
					>
						Close
					</button>
				</div>
			</div>
			<div class="grid grid-cols-2 gap-x-3 gap-y-1 mt-3 text-[10px]">
				<div class="text-[#666]">Entry</div>
				<div class="text-[#999] text-right">{formatPrice(alert.entryPrice)}</div>
				<div class="text-[#666]">Size</div>
				<div class="text-[#999] text-right">{alert.positionSize.toLocaleString(undefined, { maximumFractionDigits: 6 })}</div>
				<div class="text-[#666]">Opened</div>
				<div class="text-[#999] text-right">{formatDateTime(alert.openedAt)}</div>
			</div>
			<a
				href="/trading"
				class="mt-3 inline-block text-[10px] uppercase tracking-wider text-white border border-white px-2 py-1 hover:bg-white hover:text-black transition-colors"
				on:click={() => openSessionFromAlert(alert)}
			>
				Open Session
			</a>
		</div>
	{/each}
{/if}
