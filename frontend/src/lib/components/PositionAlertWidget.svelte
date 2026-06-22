<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { fly } from 'svelte/transition';
	import { getPaperSessions } from '$lib/api';
	import type { PaperTradingSession } from '$lib/api';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { snoozeUntil, snoozeNotifications, getSnoozeOptions } from '$lib/stores/processTracker';

	let positionAlert: {
		token: string;
		sessionId: string;
		strategyName: string;
		symbol: string;
		timeframe: string;
		entryPrice: number;
		positionSize: number;
		openedAt: string;
	} | null = null;

	let dismissedPositionTokens = new Set<string>();
	let positionAlertPoller: RealtimeRefreshController | null = null;
	let positionAlertInFlight = false;
	const POSITION_ALERT_POLL_MS = 25_000;
	const DISMISSED_STORAGE_KEY = 'forven.paper.dismissedPositionAlerts';

	let showPositionSnoozeMenu = false;
	let positionSnoozeMenuRef: HTMLDivElement | null = null;
	const snoozeOptions = getSnoozeOptions();

	function handlePositionSnooze(durationMs: number) {
		snoozeNotifications(durationMs);
		showPositionSnoozeMenu = false;
		positionAlert = null;
	}

	function handlePositionClickOutside(event: MouseEvent) {
		if (positionSnoozeMenuRef && !positionSnoozeMenuRef.contains(event.target as Node)) {
			showPositionSnoozeMenu = false;
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

	function toPositionAlert(session: PaperTradingSession, token: string) {
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

	function dismissPositionAlert() {
		if (!positionAlert) return;
		dismissedPositionTokens.add(positionAlert.token);
		persistDismissedTokens();
		positionAlert = null;
	}

	function openSessionFromAlert(sessionId: string) {
		if (typeof window === 'undefined') return;
		window.localStorage.setItem('forven.paper.selectedSessionId', sessionId);
	}

	async function refreshPositionAlert() {
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

			if (positionAlert && !activeTokens.has(positionAlert.token)) {
				positionAlert = null;
			}

			if (!positionAlert) {
				for (const session of openSessions) {
					const token = getPositionToken(session);
					if (!token || dismissedPositionTokens.has(token)) continue;
					positionAlert = toPositionAlert(session, token);
					break;
				}
			}
		} catch {
			// Ignore intermittent API failures and retry next poll.
		} finally {
			positionAlertInFlight = false;
		}
	}

	function startPositionAlertPolling() {
		if (positionAlertPoller) return;
		positionAlertPoller = createRealtimeRefresh(refreshPositionAlert, {
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

{#if positionAlert && $snoozeUntil <= Date.now()}
	{@const activeAlert = positionAlert}
	<div class="fixed bottom-4 right-4 z-[10001] pointer-events-none">
		<div class="pointer-events-auto bg-[#111] border border-[#333] border-l-4 border-l-green-500 rounded px-4 py-3 min-w-[280px] max-w-sm shadow-lg shadow-black/60">
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="text-[10px] uppercase tracking-wider text-green-400 font-bold">Position Open</div>
					<div class="text-xs text-white font-bold truncate">{activeAlert.strategyName}</div>
					<div class="text-[10px] text-gray-400 mt-0.5">{activeAlert.symbol} / {activeAlert.timeframe}</div>
				</div>
				<div class="flex items-center gap-2">
					<div class="relative" bind:this={positionSnoozeMenuRef}>
						<button
							class="text-[10px] text-gray-500 hover:text-white border border-[#333] hover:border-white px-2 py-0.5 flex items-center gap-1"
							on:click|stopPropagation={() => showPositionSnoozeMenu = !showPositionSnoozeMenu}
							title="Snooze notifications"
						>
							<svg class="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
								<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
							</svg>
							<span>Snooze</span>
						</button>

						{#if showPositionSnoozeMenu}
							<div
								class="absolute bottom-full right-0 mb-1 bg-[#111] border border-[#333] rounded shadow-lg shadow-black/50 py-1 min-w-[140px] z-[10002]"
								transition:fly={{ y: 10, duration: 150 }}
							>
								<button
									class="w-full text-left px-3 py-1.5 text-[10px] text-green-400 hover:bg-[#222] flex items-center gap-2"
									on:click|stopPropagation={() => handlePositionSnooze(24 * 60 * 60 * 1000)}
								>
									<input type="checkbox" class="accent-green-500 pointer-events-none" checked />
									<span>Pause all alerts</span>
								</button>
								<div class="border-t border-[#333] my-1"></div>
								{#each snoozeOptions as option}
									<button
										class="w-full text-left px-3 py-1.5 text-[10px] text-gray-400 hover:bg-[#222] hover:text-white transition-colors"
										on:click|stopPropagation={() => handlePositionSnooze(option.ms)}
									>
										{option.label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
					<button
						class="text-[10px] text-gray-500 hover:text-white border border-[#333] hover:border-white px-2 py-0.5"
						on:click={dismissPositionAlert}
					>
						Close
					</button>
				</div>
			</div>
			<div class="grid grid-cols-2 gap-x-3 gap-y-1 mt-3 text-[10px]">
				<div class="text-gray-500">Entry</div>
				<div class="text-gray-300 text-right">{formatPrice(activeAlert.entryPrice)}</div>
				<div class="text-gray-500">Size</div>
				<div class="text-gray-300 text-right">{activeAlert.positionSize.toLocaleString(undefined, { maximumFractionDigits: 6 })}</div>
				<div class="text-gray-500">Opened</div>
				<div class="text-gray-300 text-right">{formatDateTime(activeAlert.openedAt)}</div>
			</div>
			<a
				href="/trading"
				class="mt-3 inline-block text-[10px] uppercase tracking-wider text-white border border-white px-2 py-1 hover:bg-white hover:text-black transition-colors"
				on:click={() => openSessionFromAlert(activeAlert.sessionId)}
			>
				Open Session
			</a>
		</div>
	</div>
{/if}
