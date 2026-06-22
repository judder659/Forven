/**
 * Strategy container import/export helpers.
 *
 * Pure browser-side utilities (no API imports) for moving a strategy export
 * envelope in and out of the app via file download/upload or the clipboard,
 * plus client-side parsing/validation of a pasted-or-uploaded envelope.
 */

const EXPECTED_KIND = 'strategy_container';

export function serializeEnvelope(envelope: unknown): string {
	return JSON.stringify(envelope, null, 2);
}

export function buildExportFilename(displayId: string, name?: string): string {
	const id = (displayId || 'strategy').trim().replace(/[^A-Za-z0-9_-]+/g, '_') || 'strategy';
	const slug = (name || '')
		.trim()
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '')
		.slice(0, 48);
	return slug ? `forven-${id}-${slug}.json` : `forven-${id}.json`;
}

export function downloadJson(envelope: unknown, filename: string): void {
	const text = serializeEnvelope(envelope);
	const blob = new Blob([text], { type: 'application/json' });
	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = filename;
	document.body.appendChild(link);
	link.click();
	link.remove();
	// Revoke on the next tick so the download has a chance to start first.
	setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function copyJsonToClipboard(envelope: unknown): Promise<void> {
	const text = serializeEnvelope(envelope);
	if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
		await navigator.clipboard.writeText(text);
		return;
	}
	// Fallback for older/insecure-context browsers (mirrors TradingViewExportModal).
	const textarea = document.createElement('textarea');
	textarea.value = text;
	textarea.setAttribute('readonly', 'true');
	textarea.style.position = 'fixed';
	textarea.style.left = '-9999px';
	document.body.appendChild(textarea);
	textarea.select();
	document.execCommand('copy');
	textarea.remove();
}

export function readFileAsText(file: File): Promise<string> {
	return new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(String(reader.result ?? ''));
		reader.onerror = () => reject(reader.error ?? new Error('Failed to read file'));
		reader.readAsText(file);
	});
}

export interface ParsedEnvelopeMeta {
	kind: string;
	version: string;
	sourceId: string;
	sourceDisplay: string;
	exportedAt: string;
}

export interface ParsedEnvelopeSummary {
	name: string;
	type: string;
	symbol: string;
	timeframe: string;
	backtests: number;
	trades: number;
	events: number;
}

export interface ParsedEnvelope {
	envelope: Record<string, unknown>;
	meta: ParsedEnvelopeMeta;
	summary: ParsedEnvelopeSummary;
}

function asRecord(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

function countArray(value: unknown): number {
	return Array.isArray(value) ? value.length : 0;
}

/**
 * Parse + shallow-validate an export envelope. Throws an Error with a
 * user-facing message when the payload is not a recognizable Forven export.
 */
export function parseEnvelope(text: string): ParsedEnvelope {
	let raw: unknown;
	try {
		raw = JSON.parse(text);
	} catch {
		throw new Error('Not valid JSON.');
	}
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
		throw new Error('Expected a JSON object at the top level.');
	}

	const env = raw as Record<string, unknown>;
	const meta = asRecord(env.forven_export);
	if (!env.forven_export || typeof env.forven_export !== 'object') {
		throw new Error('Missing "forven_export" metadata — this is not a Forven strategy export.');
	}
	const kind = String(meta.kind ?? '').trim();
	if (kind !== EXPECTED_KIND) {
		throw new Error(`Unsupported export kind: ${kind || '(none)'}.`);
	}

	const config = asRecord(env.configuration);
	const strat = asRecord(env.strategy);
	const history = asRecord(env.history);
	const execution = asRecord(env.execution);

	return {
		envelope: env,
		meta: {
			kind,
			version: String(meta.version ?? '').trim(),
			sourceId: String(meta.source_strategy_id ?? strat.id ?? '').trim(),
			sourceDisplay: String(meta.source_display_id ?? meta.source_strategy_id ?? strat.display_id ?? '').trim(),
			exportedAt: String(meta.exported_at ?? '').trim(),
		},
		summary: {
			name: String(strat.name ?? config.name ?? '').trim(),
			type: String(config.type ?? strat.type ?? '').trim(),
			symbol: String(config.symbol ?? strat.symbol ?? '').trim(),
			timeframe: String(config.timeframe ?? strat.timeframe ?? '').trim(),
			backtests: countArray(history.backtests),
			trades: countArray(execution.trades),
			events: countArray(env.events),
		},
	};
}
