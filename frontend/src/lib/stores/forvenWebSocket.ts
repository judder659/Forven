import { writable, readonly } from 'svelte/store';
import { getForvenLiveWebSocketUrl, getForvenLiveWebSocketUrls } from '$lib/api';

export const forvenDaemonState = writable<Record<string, unknown> | null>(null);
export const forvenLivePrices = writable<Record<string, number>>({});
export const forvenPositionPnl = writable<Array<Record<string, unknown>>>([]);
export const forvenRealtimeLogs = writable<Array<Record<string, unknown>>>([]);
export const forvenLastTrade = writable<Record<string, unknown> | null>(null);
export const forvenRealtimeEvents = writable<Array<Record<string, unknown>>>([]);

const _wsConnected = writable(false);
export const forvenWsConnected = readonly(_wsConnected);

const MAX_LOGS = 200;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const HEARTBEAT_TIMEOUT_MS = 60_000; // Allow heavy backend operations without forced churn.

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
let intentionalClose = false;
let removeSocketListeners: (() => void) | null = null;
let reconnectAttempts = 0;
let wsUrlCandidates: string[] = [];
let wsUrlIndex = 0;
let currentWsUrl: string | null = null;
let lastStableWsUrl: string | null = null;
let lastOpenTimestamp = 0;

const STABLE_CONNECTION_MS = 15_000;
const SHORT_LIVED_CLOSE_MS = 5_000;

function resetHeartbeat() {
	if (heartbeatTimer !== null) clearTimeout(heartbeatTimer);
	heartbeatTimer = setTimeout(() => {
		// No message received in HEARTBEAT_TIMEOUT_MS → connection is stale
		if (ws && ws.readyState === WebSocket.OPEN) {
			ws.close(4000, 'heartbeat timeout');
		}
	}, HEARTBEAT_TIMEOUT_MS);
}

function clearHeartbeat() {
	if (heartbeatTimer !== null) {
		clearTimeout(heartbeatTimer);
		heartbeatTimer = null;
	}
}

function refreshWsUrlCandidates(): void {
	const urls = getForvenLiveWebSocketUrls();
	if (urls.length > 0) {
		wsUrlCandidates = urls;
		return;
	}
	wsUrlCandidates = [getForvenLiveWebSocketUrl()];
}

function pickWsUrl(): string {
	if (lastStableWsUrl) {
		return lastStableWsUrl;
	}
	refreshWsUrlCandidates();
	if (wsUrlCandidates.length === 0) return getForvenLiveWebSocketUrl();
	const idx = ((wsUrlIndex % wsUrlCandidates.length) + wsUrlCandidates.length) % wsUrlCandidates.length;
	return wsUrlCandidates[idx] ?? wsUrlCandidates[0] ?? getForvenLiveWebSocketUrl();
}

function handlePayload(data: Record<string, unknown>) {
	if (data.type === 'batch') {
		const messages = Array.isArray(data.messages)
			? data.messages
			: [];
		for (const message of messages) {
			if (message && typeof message === 'object') {
				handlePayload(message as Record<string, unknown>);
			}
		}
		return;
	}

	switch (data.type) {
		case 'init':
			// Backend wraps the daemon snapshot as {type:'init', data:{...}} — store the
			// inner daemon dict, not the envelope, so consumers read top-level fields.
			forvenDaemonState.set((data.data as Record<string, unknown>) ?? data);
			break;

		case 'ping':
			// Application-level keepalive from backend.
			// Reply so middleboxes also see client-to-server traffic.
			if (ws && ws.readyState === WebSocket.OPEN) {
				try {
					ws.send(JSON.stringify({ type: 'pong', ts: data.ts ?? Date.now() }));
				} catch {
					// Let the close event drive reconnect if the socket is already degraded.
				}
			}
			break;

		case 'prices':
			forvenLivePrices.set((data.prices ?? data) as Record<string, number>);
			break;

		case 'position_pnl':
			forvenPositionPnl.set((data.positions ?? data.data ?? []) as Array<Record<string, unknown>>);
			break;

		case 'logs': {
			const entries = (data.entries ?? data.logs ?? []) as Array<Record<string, unknown>>;
			forvenRealtimeLogs.update((current) => {
				const merged = [...entries, ...current];
				return merged.slice(0, MAX_LOGS);
			});
			break;
		}

		case 'trade':
			forvenLastTrade.set(data);
			if (typeof window !== 'undefined') {
				window.dispatchEvent(new CustomEvent('forven:event', { detail: data }));
			}
			break;

		case 'event':
		case 'task_queued':
		case 'task_status_changed':
		case 'task_completed':
		case 'task_failed':
		case 'strategy_transition':
		case 'strategy_promoted':
		case 'kill_switch_activated':
		case 'kill_switch_cleared':
		case 'approval_created':
		case 'approval_resolved':
		case 'certification_change':
		case 'mcp_session_opened':
		case 'notification':
		case 'risk_alert': {
			forvenRealtimeEvents.update((current) => {
				const merged = [data, ...current];
				return merged.slice(0, MAX_LOGS);
			});
			if (typeof window !== 'undefined') {
				window.dispatchEvent(new CustomEvent('forven:event', { detail: data }));
			}
			break;
		}
	}
}

function handleMessage(event: MessageEvent) {
	let data: Record<string, unknown>;
	try {
		data = JSON.parse(event.data as string) as Record<string, unknown>;
	} catch {
		return;
	}
	handlePayload(data);
}

function scheduleReconnect(closeCode?: number) {
	if (intentionalClose) return;
	if (reconnectTimer !== null) return;
	const connectionLifetimeMs = lastOpenTimestamp > 0 ? Date.now() - lastOpenTimestamp : 0;
	const shouldRotateEndpoint =
		!lastStableWsUrl ||
		(connectionLifetimeMs > 0 && connectionLifetimeMs < SHORT_LIVED_CLOSE_MS);
	if (shouldRotateEndpoint) {
		// Rotate endpoints only after failed/short-lived connections to avoid bouncing off a good route.
		wsUrlIndex += 1;
	} else {
		wsUrlIndex = 0;
	}
	const baseDelay = Math.min(RECONNECT_BASE_MS * Math.pow(2, reconnectAttempts), RECONNECT_MAX_MS);
	const delay = closeCode === 4000 ? Math.min(baseDelay, 3_000) : baseDelay;
	const jitter = delay * (0.5 + Math.random() * 0.5);
	reconnectAttempts++;
	reconnectTimer = setTimeout(() => {
		reconnectTimer = null;
		connectForvenWs();
	}, jitter);
}

function detachSocketListeners(): void {
	if (!removeSocketListeners) return;
	removeSocketListeners();
	removeSocketListeners = null;
}

function attachSocketListeners(socket: WebSocket, url: string): void {
	const onOpen = () => {
		if (ws !== socket) return;
		currentWsUrl = url;
		lastOpenTimestamp = Date.now();
		const wasReconnect = reconnectAttempts > 0;
		reconnectAttempts = 0;
		wsUrlIndex = 0;
		_wsConnected.set(true);
		resetHeartbeat();
		if (typeof window !== 'undefined') {
			window.dispatchEvent(new CustomEvent('forven:connected'));
			if (wasReconnect) {
				window.dispatchEvent(new CustomEvent('forven:reconnected'));
			}
		}
	};
	const onMessage = (event: MessageEvent) => {
		if (ws !== socket) return;
		resetHeartbeat();
		handleMessage(event);
	};
	const onClose = (event: CloseEvent) => {
		if (ws !== socket) return;
		const lifetimeMs = lastOpenTimestamp > 0 ? Date.now() - lastOpenTimestamp : 0;
		if (currentWsUrl && lifetimeMs >= STABLE_CONNECTION_MS) {
			lastStableWsUrl = currentWsUrl;
		} else if (currentWsUrl && lastStableWsUrl === currentWsUrl) {
			lastStableWsUrl = null;
		}
		currentWsUrl = null;
		lastOpenTimestamp = 0;
		clearHeartbeat();
		detachSocketListeners();
		_wsConnected.set(false);
		if (typeof window !== 'undefined') {
			window.dispatchEvent(new CustomEvent('forven:disconnected'));
		}
		ws = null;
		scheduleReconnect(event?.code);
	};
	const onError = () => {
		// The close event will fire after error, triggering reconnect.
		console.warn('[ForvenWS] websocket error', { url, readyState: socket.readyState });
	};

	socket.addEventListener('open', onOpen);
	socket.addEventListener('message', onMessage);
	socket.addEventListener('close', onClose);
	socket.addEventListener('error', onError);

	removeSocketListeners = () => {
		socket.removeEventListener('open', onOpen);
		socket.removeEventListener('message', onMessage);
		socket.removeEventListener('close', onClose);
		socket.removeEventListener('error', onError);
	};
}

export function connectForvenWs() {
	if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
		return;
	}

	if (ws) {
		detachSocketListeners();
		try {
			ws.close();
		} catch {
			// Ignore socket close errors during reconnect.
		}
		ws = null;
	}

	intentionalClose = false;
	const url = pickWsUrl();

	try {
		ws = new WebSocket(url);
	} catch {
		if (lastStableWsUrl === url) {
			lastStableWsUrl = null;
		}
		scheduleReconnect();
		return;
	}

	attachSocketListeners(ws, url);
}

export function disconnectForvenWs() {
	intentionalClose = true;
	clearHeartbeat();
	currentWsUrl = null;
	lastOpenTimestamp = 0;

	if (reconnectTimer !== null) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}

	if (ws) {
		detachSocketListeners();
		ws.close();
		ws = null;
	}

	_wsConnected.set(false);
	if (typeof window !== 'undefined') {
		window.dispatchEvent(new CustomEvent('forven:disconnected'));
	}
}
