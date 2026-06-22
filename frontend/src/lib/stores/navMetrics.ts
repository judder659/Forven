import { get, writable } from 'svelte/store';
import type { NavIndicatorKind, NavIndicatorSeverity, SystemNavIndicator } from '$lib/api';

export interface NavMetric {
	kind: NavIndicatorKind;
	severity: NavIndicatorSeverity;
	label: string;
	summary: string;
	count: number;
	seenKey: string;
	seen: boolean;
}

type NavMetricMap = Record<string, NavMetric>;

const STORAGE_KEY = 'forven.nav.seen_v1';
const NAV_HREFS = ['/', '/data', '/hypotheses', '/lab', '/risk', '/trading', '/agents', '/memory', '/tasks', '/approval', '/settings'];

function createEmptyMetric(): NavMetric {
	return {
		kind: 'none',
		severity: 'neutral',
		label: '',
		summary: '',
		count: 0,
		seenKey: '',
		seen: true,
	};
}

function createDefaultMetrics(): NavMetricMap {
	return Object.fromEntries(NAV_HREFS.map((href) => [href, createEmptyMetric()]));
}

function loadSeenKeys(): Record<string, string> {
	if (typeof window === 'undefined') return {};
	try {
		const stored = window.localStorage.getItem(STORAGE_KEY);
		return stored ? JSON.parse(stored) : {};
	} catch {
		return {};
	}
}

function saveSeenKeys(seenKeys: Record<string, string>) {
	if (typeof window === 'undefined') return;
	try {
		window.localStorage.setItem(STORAGE_KEY, JSON.stringify(seenKeys));
	} catch {
		// Ignore storage errors.
	}
}

function isKind(value: unknown): value is NavIndicatorKind {
	return value === 'none' || value === 'count' || value === 'status' || value === 'activity';
}

function isSeverity(value: unknown): value is NavIndicatorSeverity {
	return value === 'neutral' || value === 'info' || value === 'success' || value === 'warn' || value === 'danger';
}

export const navRouteMetrics = writable<NavMetricMap>(createDefaultMetrics());

export function setNavIndicators(indicators: Record<string, SystemNavIndicator> | undefined): void {
	const seenKeys = loadSeenKeys();
	const next = createDefaultMetrics();

	if (indicators && typeof indicators === 'object') {
		for (const [href, indicator] of Object.entries(indicators)) {
			if (!(href in next) || !indicator || typeof indicator !== 'object') continue;
			const kind = isKind(indicator.kind) ? indicator.kind : 'none';
			const severity = isSeverity(indicator.severity) ? indicator.severity : 'neutral';
			const seenKey = String(indicator.seen_key ?? '').trim();

			next[href] = {
				kind,
				severity,
				label: String(indicator.label ?? ''),
				summary: String(indicator.summary ?? ''),
				count: Number(indicator.count ?? 0) || 0,
				seenKey,
				seen: kind === 'none' || !seenKey || seenKeys[href] === seenKey,
			};
		}
	}

	navRouteMetrics.set(next);
}

export function markNavIndicatorSeen(href: string): void {
	const current = get(navRouteMetrics);
	const metric = current[href];
	if (!metric) return;

	navRouteMetrics.set({
		...current,
		[href]: {
			...metric,
			seen: true,
		},
	});

	if (!metric.seenKey) return;
	const seenKeys = loadSeenKeys();
	seenKeys[href] = metric.seenKey;
	saveSeenKeys(seenKeys);
}
