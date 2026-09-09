/** Stable comparison of editable content, independent of server metadata/key order. */
function canonical(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') return Object.fromEntries(
        Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, val]) => [key, canonical(val)])
    );
    return value;
}

export function editableSnapshot(entry: {
    name: string; kind?: string; description?: string; code?: string | null;
    spec?: Record<string, unknown> | null; symbol?: string; timeframe?: string;
    params?: Record<string, unknown>;
}): string {
    return JSON.stringify(canonical({
        name: entry.name, kind: entry.kind, description: entry.description || '',
        code: entry.kind === 'code' ? entry.code : null,
        spec: entry.kind === 'code' ? null : entry.spec,
        symbol: entry.symbol, timeframe: entry.timeframe, params: entry.params || {},
    }));
}
