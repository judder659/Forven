import { createPoller, type Poller } from '$lib/utils/polling';
import { get } from 'svelte/store';
import { forvenWsConnected } from '$lib/stores/forvenWebSocket';

export interface RealtimeRefreshOptions {
	fallbackMs: number;
	wsDebounceMs?: number;
	wsEvents?: string[];
	/** Further narrows the matching events (e.g. only actionable notifications). */
	wsFilter?: (detail: Record<string, unknown>) => boolean;
	onReconnect?: boolean;
	/** Run interval polling only while websocket is offline (default true). */
	pollWhenWsOfflineOnly?: boolean;
}

export interface RealtimeRefreshController {
	start: () => void;
	stop: () => void;
	trigger: () => void;
}

const DEFAULT_WS_EVENTS = [
	'task_queued',
	'task_status_changed',
	'task_completed',
	'task_failed',
	'strategy_transition',
	'strategy_promoted',
	'kill_switch_activated',
	'kill_switch_cleared',
	'approval_created',
	'approval_resolved',
	'risk_alert',
	'trade',
];

export function createRealtimeRefresh(
	refreshFn: () => void | Promise<void>,
	options: RealtimeRefreshOptions,
): RealtimeRefreshController {
	const eventNames = new Set((options.wsEvents ?? DEFAULT_WS_EVENTS).map((e) => e.toLowerCase()));
	const debounceMs = options.wsDebounceMs ?? 1200;
	const listenReconnect = options.onReconnect ?? true;
	const pollWhenWsOfflineOnly = options.pollWhenWsOfflineOnly ?? true;

	let poller: Poller | null = null;
	let wsEventHandler: ((event: Event) => void) | null = null;
	let reconnectHandler: ((event: Event) => void) | null = null;
	let wsConnectedHandler: ((event: Event) => void) | null = null;
	let wsDisconnectedHandler: ((event: Event) => void) | null = null;
	let visibilityHandler: (() => void) | null = null;
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;
	let refreshInFlight = false;
	let refreshQueued = false;

	async function runRefresh(): Promise<void> {
		if (typeof document !== 'undefined' && document.hidden) return;
		if (refreshInFlight) {
			refreshQueued = true;
			return;
		}

		refreshInFlight = true;
		try {
			await refreshFn();
		} catch (error) {
			console.error('[RealtimeRefresh] refresh error:', error);
		} finally {
			refreshInFlight = false;
			if (refreshQueued) {
				refreshQueued = false;
				scheduleRefresh();
			}
		}
	}

	function scheduleRefresh(): void {
		if (typeof document !== 'undefined' && document.hidden) return;
		if (refreshTimer !== null) return;
		refreshTimer = setTimeout(() => {
			refreshTimer = null;
			void runRefresh();
		}, debounceMs);
	}

	function handleWsEvent(event: Event): void {
		const detail = (event as CustomEvent<Record<string, unknown>>).detail ?? {};
		const eventType = String(detail?.event ?? detail?.type ?? '').toLowerCase();
		if (eventNames.has(eventType) && (!options.wsFilter || options.wsFilter(detail))) {
			scheduleRefresh();
		}
	}

	function startFallbackPollerIfNeeded(): void {
		if (poller) return;
		poller = createPoller(runRefresh, options.fallbackMs);
		poller.start();
	}

	function stopFallbackPoller(): void {
		poller?.stop();
		poller = null;
	}

	function syncFallbackPoller(): void {
		if (!pollWhenWsOfflineOnly) {
			startFallbackPollerIfNeeded();
			return;
		}
		const wsConnected = get(forvenWsConnected);
		if (wsConnected) {
			stopFallbackPoller();
		} else {
			startFallbackPollerIfNeeded();
		}
	}

	function start(): void {
		syncFallbackPoller();
		scheduleRefresh();

		if (typeof window !== 'undefined' && !wsEventHandler) {
			wsEventHandler = handleWsEvent;
			window.addEventListener('forven:event', wsEventHandler);
		}

		if (typeof window !== 'undefined' && listenReconnect && !reconnectHandler) {
			reconnectHandler = () => scheduleRefresh();
			window.addEventListener('forven:reconnected', reconnectHandler);
		}

		if (typeof window !== 'undefined' && !wsConnectedHandler) {
			wsConnectedHandler = () => {
				syncFallbackPoller();
				scheduleRefresh();
			};
			window.addEventListener('forven:connected', wsConnectedHandler);
		}

		if (typeof window !== 'undefined' && !wsDisconnectedHandler) {
			wsDisconnectedHandler = () => syncFallbackPoller();
			window.addEventListener('forven:disconnected', wsDisconnectedHandler);
		}

		if (typeof document !== 'undefined' && !visibilityHandler) {
			visibilityHandler = () => {
				if (!document.hidden) scheduleRefresh();
			};
			document.addEventListener('visibilitychange', visibilityHandler);
		}
	}

	function stop(): void {
		stopFallbackPoller();

		if (refreshTimer !== null) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
		refreshQueued = false;
		refreshInFlight = false;

		if (typeof window !== 'undefined' && wsEventHandler) {
			window.removeEventListener('forven:event', wsEventHandler);
			wsEventHandler = null;
		}

		if (typeof window !== 'undefined' && reconnectHandler) {
			window.removeEventListener('forven:reconnected', reconnectHandler);
			reconnectHandler = null;
		}

		if (typeof window !== 'undefined' && wsConnectedHandler) {
			window.removeEventListener('forven:connected', wsConnectedHandler);
			wsConnectedHandler = null;
		}

		if (typeof window !== 'undefined' && wsDisconnectedHandler) {
			window.removeEventListener('forven:disconnected', wsDisconnectedHandler);
			wsDisconnectedHandler = null;
		}

		if (typeof document !== 'undefined' && visibilityHandler) {
			document.removeEventListener('visibilitychange', visibilityHandler);
			visibilityHandler = null;
		}
	}

	return {
		start,
		stop,
		trigger: scheduleRefresh,
	};
}
