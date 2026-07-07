/**
 * API client core for Forven backend
 */

/**
 * API client for Forven backend
 */

const DEFAULT_API_ORIGIN = 'http://127.0.0.1:8004';
const FALLBACK_API_ORIGINS = ['127.0.0.1', 'localhost'];
const IS_TEST_ENV = Boolean(import.meta.env?.MODE === 'test' || import.meta.env?.VITEST);

function trimTrailingSlash(value: string): string {
	return value.endsWith('/') ? value.slice(0, -1) : value;
}

function resolveApiBase(): string {
	const configuredBase = (import.meta.env.VITE_API_BASE ?? '').trim();
	if (configuredBase) {
		if (configuredBase.startsWith('/')) {
			if (typeof window !== 'undefined' && window.location) {
				// Prefer direct backend access from browser clients; Vite proxy `/api`
				// can intermittently disconnect for remote/mobile clients.
				const protocol = window.location.protocol || 'http:';
				const host = window.location.hostname || '127.0.0.1';
				return `${protocol}//${host}:8004/api`;
			}
			return `${DEFAULT_API_ORIGIN}${trimTrailingSlash(configuredBase)}`;
		}
		if (configuredBase.startsWith('http://') || configuredBase.startsWith('https://')) {
			return trimTrailingSlash(configuredBase);
		}
	}

	if (typeof window !== 'undefined' && window.location) {
		const protocol = window.location.protocol || 'http:';
		const host = window.location.hostname || '127.0.0.1';
		return `${protocol}//${host}:8004/api`;
	}
	return `${DEFAULT_API_ORIGIN}/api`;
}

export const API_BASE = resolveApiBase();
const BASE_CANDIDATES_RAW = new Set<string>([
	trimTrailingSlash(API_BASE),
	trimTrailingSlash(DEFAULT_API_ORIGIN),
]);

if (typeof window !== 'undefined' && window.location) {
	const protocol = window.location.protocol || 'http:';
	const host = window.location.hostname || '127.0.0.1';
	const isLocalBrowserHost = host === '127.0.0.1' || host === 'localhost' || host === '::1';

	BASE_CANDIDATES_RAW.add('/api');
	BASE_CANDIDATES_RAW.add(`${protocol}//${host}/api`);
	BASE_CANDIDATES_RAW.add(`${protocol}//${host}:8004/api`);
	BASE_CANDIDATES_RAW.add(`${protocol}//${host}:8000/api`);
	if (isLocalBrowserHost) {
		for (const fallbackHost of FALLBACK_API_ORIGINS) {
			BASE_CANDIDATES_RAW.add(`${protocol}//${fallbackHost}:8004/api`);
			BASE_CANDIDATES_RAW.add(`${protocol}//${fallbackHost}:8000/api`);
		}
	}
	BASE_CANDIDATES_RAW.add(`${window.location.origin.replace(/\/$/, '')}/api`);
} else {
	BASE_CANDIDATES_RAW.add(`${DEFAULT_API_ORIGIN}/api`);
}

let preferredCandidates: string[] = [];
if (typeof window !== 'undefined' && window.location) {
	const protocol = window.location.protocol || 'http:';
	const host = window.location.hostname || '127.0.0.1';
	const originApi = `${window.location.origin.replace(/\/$/, '')}/api`;
	const directBackendApi = `${protocol}//${host}:8004/api`;
	preferredCandidates = [
		trimTrailingSlash(API_BASE),
		directBackendApi,
		`${DEFAULT_API_ORIGIN}/api`,
		originApi,
		`${protocol}//${host}/api`,
		`${protocol}//${host}:8000/api`,
		'/api',
	];
} else {
	preferredCandidates = [
		trimTrailingSlash(API_BASE),
		`${DEFAULT_API_ORIGIN}/api`,
	];
}

const API_BASE_CANDIDATES = Array.from(new Set(
	[
		...preferredCandidates,
		...Array.from(BASE_CANDIDATES_RAW),
	].map(trimTrailingSlash).filter(Boolean)
));

export let ACTIVE_API_BASE = IS_TEST_ENV ? '/api' : (API_BASE_CANDIDATES[0] || API_BASE);
let API_BASE_DISCOVERED = IS_TEST_ENV;
let API_BASE_DISCOVERY: Promise<void> | null = null;

const API_DISCOVERY_TIMEOUT_MS = 1_200;

function toHealthUrl(base: string): string {
	const trimmed = trimTrailingSlash(base);
	if (!trimmed) return '/api/health';
	if (trimmed.endsWith('/api')) return `${trimmed}/health`;
	return `${trimmed}/api/health`;
}

function promoteActiveApiBase(base: string): void {
	const normalized = trimTrailingSlash(base);
	if (!normalized) return;
	ACTIVE_API_BASE = normalized;
	API_BASE_DISCOVERED = true;
}

function baseHasApiPrefix(base: string): boolean {
	if (!base) return false;
	if (base === '/api') return true;
	return /\/api(?:\/|$)/.test(trimTrailingSlash(base));
}

function getRequestPath(base: string, endpoint: string): string {
	if (/^https?:\/\//.test(endpoint)) {
		return endpoint;
	}

	const normalizedPath = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
	if (baseHasApiPrefix(base)) {
		if (normalizedPath === '/api') return '';
		if (normalizedPath.startsWith('/api/')) return normalizedPath.slice(4);
		return normalizedPath;
	}

	if (normalizedPath === '/api') return '/api';
	return normalizedPath.startsWith('/api/') ? normalizedPath : `/api${normalizedPath}`;
}

function parseHealthResponse(payload: string | Record<string, unknown> | null | undefined): { status: string;[key: string]: unknown } | null {
	try {
		const parsed = typeof payload === 'string'
			? JSON.parse(payload) as { status?: string;[key: string]: unknown }
			: payload;
		if (parsed && typeof parsed === 'object' && typeof parsed.status === 'string') {
			return parsed as { status: string;[key: string]: unknown };
		}
		return null;
	} catch {
		return null;
	}
}

async function readHealthPayload(response: Response): Promise<{ status: string;[key: string]: unknown } | null> {
	try {
		if (typeof response.text === 'function') {
			const bodyText = await response.text();
			if (bodyText) {
				const parsed = parseHealthResponse(bodyText);
				if (parsed) return parsed;
			}
		}
	} catch {
		// Fall through to JSON parse path.
	}
	try {
		if (typeof response.json === 'function') {
			const bodyJson = await response.json();
			return parseHealthResponse(bodyJson);
		}
	} catch {
		return null;
	}
	return null;
}

let _discoveryLastAttempt = 0;
const _DISCOVERY_COOLDOWN_MS = 5_000;

async function detectActiveApiBase(): Promise<void> {
	if (API_BASE_DISCOVERED) return;
	if (API_BASE_DISCOVERY) {
		await API_BASE_DISCOVERY;
		return;
	}

	const now = Date.now();
	if (now - _discoveryLastAttempt < _DISCOVERY_COOLDOWN_MS) return;
	_discoveryLastAttempt = now;

	API_BASE_DISCOVERY = (async () => {
		for (const base of API_BASE_CANDIDATES) {
			const controller = new AbortController();
			const timeout = setTimeout(() => {
				controller.abort();
			}, API_DISCOVERY_TIMEOUT_MS);
			try {
				const response = await fetch(toHealthUrl(base), { signal: controller.signal });
				if (!response.ok) {
					continue;
				}
				const payload = await readHealthPayload(response);
				if (payload) {
					promoteActiveApiBase(base);
					return;
				}
			} catch {
				continue;
			} finally {
				clearTimeout(timeout);
			}
		}
		// No healthy base found — do NOT set API_BASE_DISCOVERED so retries are possible
	})();

	await API_BASE_DISCOVERY;
	API_BASE_DISCOVERY = null;
}

function isRetryableNetworkError(error: unknown): boolean {
	if (!(error instanceof Error)) return false;
	if (error.name === 'TypeError') return true;
	if (typeof DOMException !== 'undefined' && error instanceof DOMException) {
		return error.name === 'AbortError';
	}
	return false;
}

function readSecretFromStorage(key: string): string {
	if (typeof window === 'undefined') return '';
	try {
		const value = window.localStorage.getItem(key);
		return (value ?? '').trim();
	} catch {
		return '';
	}
}

function resolveAuthHeaderValue(envKeys: string[], storageKeys: string[]): string {
	for (const envKey of envKeys) {
		const fromEnv = String(import.meta.env[envKey] ?? '').trim();
		if (fromEnv) return fromEnv;
	}
	for (const storageKey of storageKeys) {
		const fromStorage = readSecretFromStorage(storageKey);
		if (fromStorage) return fromStorage;
	}
	return '';
}

export function buildAuthHeaders(): Record<string, string> {
	const headers: Record<string, string> = {};
	const apiKey = resolveAuthHeaderValue(
		['VITE_FORVEN_API_KEY'],
		['forven_api_key']
	);
	const operatorKey = resolveAuthHeaderValue(
		['VITE_FORVEN_OPERATOR_KEY'],
		['forven_operator_key']
	);

	if (apiKey) headers['X-API-Key'] = apiKey;
	if (operatorKey) headers['X-Operator-Key'] = operatorKey;
	return headers;
}

/** Default request timeout in milliseconds */
export const DEFAULT_TIMEOUT_MS = 30_000;

/** Extended timeout for computationally expensive operations (scorecard, robustness, etc.) */
export const LONG_TIMEOUT_MS = 120_000;
export const AUTOPILOT_TIMEOUT_MS = 12_000;

const MAX_CONCURRENT = 50;
let _inFlight = 0;
const _queue: Array<() => void> = [];

function acquireSlot(): Promise<void> {
	if (_inFlight < MAX_CONCURRENT) {
		_inFlight++;
		return Promise.resolve();
	}
	return new Promise<void>((resolve) => {
		_queue.push(() => { _inFlight++; resolve(); });
	});
}

function releaseSlot(): void {
	_inFlight--;
	const next = _queue.shift();
	if (next) next();
}

export class ApiError extends Error {
	status: number;
	payload: unknown;

	constructor(status: number, message: string, payload?: unknown) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.payload = payload;
	}
}

export function isNotFoundError(error: unknown): boolean {
	if (error instanceof ApiError) return error.status === 404;
	if (!(error instanceof Error)) return false;
	return error.message === 'Not Found' || error.message.includes('HTTP 404');
}

export function isRouteMissingError(error: unknown): boolean {
	if (error instanceof ApiError) {
		if (error.status !== 404) return false;
		const message = String(error.message || '').trim().toLowerCase();
		return message === 'not found' || message === 'http 404';
	}
	if (!(error instanceof Error)) return false;
	const message = String(error.message || '').trim().toLowerCase();
	return message === 'not found' || message === 'http 404';
}

interface LimitedFetchOptions extends RequestInit {
	timeoutMs?: number;
}

export async function fetchWithLimit(url: string, options: LimitedFetchOptions = {}): Promise<Response> {
	const { timeoutMs = DEFAULT_TIMEOUT_MS, signal, ...requestInit } = options;
	const requestSignal = signal ?? AbortSignal.timeout(timeoutMs);

	await acquireSlot();
	try {
		return await fetch(url, {
			...requestInit,
			signal: requestSignal,
		});
	} finally {
		releaseSlot();
	}
}

function parseErrorPayload(raw: string): { detail: string; payload: unknown } {
	const fallback = raw.trim();
	if (!fallback) {
		return { detail: 'Unknown error', payload: null };
	}
	try {
		const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown; error?: unknown };
		const detailSource = parsed?.detail ?? parsed?.message ?? parsed?.error;
		if (typeof detailSource === 'string' && detailSource.trim()) {
			return { detail: detailSource.trim(), payload: parsed };
		}
		if (detailSource && typeof detailSource === 'object') {
			return { detail: JSON.stringify(detailSource), payload: parsed };
		}
		return { detail: fallback, payload: parsed };
	} catch {
		// Response isn't JSON. Preserve text for debugging instead of "Unknown error".
		return { detail: fallback, payload: { raw: fallback } };
	}
}

export async function fetchApi<T>(endpoint: string, options?: RequestInit & { timeoutMs?: number }): Promise<T> {
	const { timeoutMs, ...restOptions } = (options ?? {}) as RequestInit & { timeoutMs?: number };
	const headers = new Headers(restOptions?.headers);
	const authHeaders = buildAuthHeaders();
	for (const [key, value] of Object.entries(authHeaders)) {
		if (value && !headers.has(key)) headers.set(key, value);
	}

	const isFormData = typeof FormData !== 'undefined' && restOptions?.body instanceof FormData;
	if (!isFormData && !headers.has('Content-Type')) {
		headers.set('Content-Type', 'application/json');
	}

	let lastError: unknown;
	await detectActiveApiBase();
	const orderedCandidates = Array.from(new Set([ACTIVE_API_BASE, ...API_BASE_CANDIDATES]));
	for (let index = 0; index < orderedCandidates.length; index += 1) {
		const base = orderedCandidates[index];
		const requestPath = getRequestPath(base, endpoint);
		const requestUrl = /^https?:\/\//.test(requestPath) ? requestPath : `${base}${requestPath}`;
		const isLast = index === orderedCandidates.length - 1;
		try {
			const response = await fetchWithLimit(requestUrl, {
				...restOptions,
				headers,
				...(timeoutMs != null ? { timeoutMs } : {}),
			});
			if (!response.ok) {
				const rawError = await response.text().catch(() => '');
				const parsed = parseErrorPayload(rawError);
				let detail = parsed.detail || response.statusText || `HTTP ${response.status}`;
				if (!detail || detail === 'Unknown error') {
					detail = response.statusText || `HTTP ${response.status}`;
				}
				// HTTP responses are authoritative for this base; only retry on
				// network-level failures, not 4xx/5xx status codes.
				throw new ApiError(response.status, detail, parsed.payload);
			}

			promoteActiveApiBase(base);
			return response.json();
		} catch (error) {
			lastError = error;
			if (error instanceof ApiError) {
				throw error;
			}
			if (isRetryableNetworkError(error)) {
				if (!isLast) {
					continue;
				}
			}
			if (isLast) {
				throw error;
			}
		}
	}

	if (lastError instanceof Error) {
		throw lastError;
	}
	throw new Error('Request failed');
}

// Health check
export async function checkHealth(): Promise<{ status: string;[key: string]: unknown }> {
	await detectActiveApiBase();
	const orderedCandidates = Array.from(new Set([ACTIVE_API_BASE, ...API_BASE_CANDIDATES]));
	for (let index = 0; index < orderedCandidates.length; index += 1) {
		const base = orderedCandidates[index];
		const isLast = index === orderedCandidates.length - 1;
		try {
			const response = await fetchWithLimit(toHealthUrl(base), { timeoutMs: 12000 });
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			const payload = await readHealthPayload(response);
			if (payload) {
				promoteActiveApiBase(base);
				return payload;
			}
			if (isLast) {
				throw new Error('Invalid health response');
			}
			continue;
		} catch {
			if (isLast) throw new Error('Backend not reachable');
			continue;
		}
	}

	throw new Error('Backend not reachable');
}

export function asRecord(value: unknown): Record<string, unknown> | null {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
	return value as Record<string, unknown>;
}

export function asArray<T = unknown>(value: unknown): T[] {
	return Array.isArray(value) ? (value as T[]) : [];
}
