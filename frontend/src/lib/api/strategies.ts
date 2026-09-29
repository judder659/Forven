import type { ParamSpec, Strategy } from './types';
import {
	asArray,
	asRecord,
	fetchApi,
	isNotFoundError,
	isRouteMissingError,
} from './core';

// Strategy endpoints
function normalizeStrategyParamDefault(value: unknown): string | number | boolean {
	if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
		return value;
	}
	return JSON.stringify(value ?? '');
}

function normalizeStrategyParameters(raw: unknown): Record<string, ParamSpec> {
	let source: Record<string, unknown> | null = null;
	if (typeof raw === 'string') {
		try {
			const parsed = JSON.parse(raw);
			source = asRecord(parsed);
		} catch {
			source = null;
		}
	} else {
		source = asRecord(raw);
	}
	if (!source) return {};

	const out: Record<string, ParamSpec> = {};
	for (const [key, value] of Object.entries(source)) {
		const spec = asRecord(value);
		if (spec && ('default' in spec || 'type' in spec)) {
			out[key] = {
				type: typeof spec.type === 'string' ? spec.type : typeof spec.default,
				default: normalizeStrategyParamDefault(spec.default),
				min: typeof spec.min === 'number' ? spec.min : undefined,
				max: typeof spec.max === 'number' ? spec.max : undefined,
				step: typeof spec.step === 'number' ? spec.step : undefined,
				options: Array.isArray(spec.options)
					? spec.options.filter((option): option is string => typeof option === 'string')
					: undefined,
			};
			continue;
		}
		out[key] = {
			type: typeof value,
			default: normalizeStrategyParamDefault(value),
		};
	}
	return out;
}

function parseRecord(raw: unknown): Record<string, unknown> | null {
	if (typeof raw === 'string') {
		try {
			return asRecord(JSON.parse(raw));
		} catch {
			return null;
		}
	}
	return asRecord(raw);
}

/** Parameter values as the backend holds them: a row's `params` blob, or a catalog's spec defaults. */
function rawParameterValues(row: Record<string, unknown>): Record<string, unknown> {
	const stored = parseRecord(row.params);
	if (stored) return { ...stored };
	const specs = parseRecord(row.parameters);
	if (!specs) return {};
	const out: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(specs)) {
		const spec = asRecord(value);
		out[key] = spec && 'default' in spec ? spec.default : value;
	}
	return out;
}

function optionalText(value: unknown): string | null {
	const text = typeof value === 'string' ? value.trim() : '';
	return text || null;
}

function normalizeStrategyRecord(raw: unknown): Strategy | null {
	const row = asRecord(raw);
	if (!row) return null;

	const name = String(row.name ?? row.id ?? '').trim();
	if (!name) return null;
	const apiName = String(row.api_name ?? row.id ?? name).trim() || name;
	const description = String(row.description ?? row.notes ?? '').trim()
		|| `${String(row.type ?? 'strategy')} strategy`;

	return {
		name,
		api_name: apiName,
		version: String(row.version ?? '1.0.0'),
		description,
		parameters: normalizeStrategyParameters(row.parameters ?? row.params),
		display_id: optionalText(row.display_id),
		stage: optionalText(row.stage),
		symbol: optionalText(row.symbol),
		timeframe: optionalText(row.timeframe),
		source: optionalText(row.source),
		asset: optionalText(row.asset),
		trade_modes: Array.isArray(row.trade_modes)
			? row.trade_modes.filter((mode): mode is string => typeof mode === 'string')
			: undefined,
		default_trade_mode: optionalText(row.default_trade_mode),
		raw_params: rawParameterValues(row),
	};
}

export function normalizeStrategyPayload(payload: unknown): { strategies: Strategy[] } {
	const fromObject = asRecord(payload);
	if (fromObject && Array.isArray(fromObject.strategies)) {
		const normalized = fromObject.strategies
			.map((row) => normalizeStrategyRecord(row))
			.filter((row): row is Strategy => Boolean(row));
		return { strategies: normalized };
	}

	if (Array.isArray(payload)) {
		const normalized = payload
			.map((row) => normalizeStrategyRecord(row))
			.filter((row): row is Strategy => Boolean(row));
		return { strategies: normalized };
	}

	return { strategies: [] };
}

/**
 * Strategy rows. Without a status the backend returns its 500 most recent rows,
 * which are mostly archived; pass a status (paper, live_graduated, ...) to get
 * one stage.
 */
export async function getStrategies(options: { status?: string; limit?: number } = {}): Promise<{ strategies: Strategy[] }> {
	const query = new URLSearchParams();
	if (options.status) query.set('status', options.status);
	if (options.limit != null) query.set('limit', String(options.limit));
	const qs = query.toString();
	const payload = await fetchApi<unknown>(qs ? `/strategies?${qs}` : '/strategies');
	return normalizeStrategyPayload(payload);
}

export async function getPrebuiltStrategies(): Promise<{ strategies: Strategy[] }> {
	const payload = await fetchApi<unknown>('/strategies/prebuilt');
	return normalizeStrategyPayload(payload);
}

export async function getStrategy(name: string): Promise<Strategy> {
	try {
		const payload = await fetchApi<unknown>(`/strategies/${encodeURIComponent(name)}`);
		const normalized = normalizeStrategyRecord(payload);
		if (normalized) return normalized;
		throw new Error('Invalid strategy payload');
	} catch (error) {
		if (!isNotFoundError(error)) throw error;
		const all = await getStrategies();
		const target = name.trim().toLowerCase();
		const fallback = all.strategies.find((strategy) => {
			const strategyName = strategy.name.toLowerCase();
			const strategyApiName = (strategy.api_name ?? strategy.name).toLowerCase();
			return strategyName === target || strategyApiName === target;
		});
		if (fallback) return fallback;
		throw error;
	}
}
