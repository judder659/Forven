/**
 * Shared indicator helpers for chart panels.
 *
 * Pure helpers that map session indicator payloads onto ChartWorkspace
 * overlays and lower panels.
 */
import type { IndicatorConfig } from '$lib/stores/chartStore';
import type { SessionIndicatorConfig, SessionIndicatorsResponse } from '$lib/api/paper';

export function getIndicatorColor(name: string): string {
	const lower = name.trim().toLowerCase();
	const explicitColors: Record<string, string> = {
		price: '#94A3B8',
		close: '#94A3B8',
		rsi: '#8B5CF6',
		prev_rsi: '#A78BFA',
		macd: '#38BDF8',
		macd_signal: '#F59E0B',
		adx: '#22D3EE',
		atr: '#FB7185',
		atr_14: '#FB7185',
		ema_fast: '#22C55E',
		ema_slow: '#C084FC',
		ema_regime: '#60A5FA',
		entry_signal: '#22C55E',
		exit_signal: '#EF4444'
	};
	if (explicitColors[lower]) return explicitColors[lower];
	if (lower.startsWith('atr')) return '#FB7185';
	if (lower.startsWith('rsi')) return '#8B5CF6';
	if (lower.startsWith('macd_signal')) return '#F59E0B';
	if (lower.startsWith('macd')) return '#38BDF8';
	if (lower.startsWith('adx')) return '#22D3EE';
	const palette = lower.includes('ema')
		? ['#22C55E', '#60A5FA', '#F59E0B', '#C084FC', '#F97316']
		: ['rsi', 'macd', 'adx', 'atr', 'cci', 'williams', 'stoch', 'mfi', 'roc', 'mom'].some((token) =>
					lower.includes(token)
			  )
			? ['#8B5CF6', '#38BDF8', '#F59E0B', '#22D3EE', '#FB7185', '#F97316']
			: ['#E5E7EB', '#22C55E', '#60A5FA', '#F59E0B', '#C084FC', '#FB7185'];
	const stableIdx = [...lower].reduce((acc, ch) => acc + ch.charCodeAt(0), 0) % palette.length;
	return palette[stableIdx];
}

export function inferIndicatorPanel(name: string): 'main' | 'sub' | 'none' {
	const key = name.toUpperCase();
	if (key === 'PRICE' || key === 'ENTRY_SIGNAL' || key === 'EXIT_SIGNAL') return 'none';
	if (
		key.includes('SIGNAL') ||
		key.includes('UPTREND') ||
		key.includes('DOWNTREND') ||
		key.includes('FLAG') ||
		key.includes('STATE')
	) {
		return 'none';
	}
	const subPanelIndicators = ['RSI', 'STOCH', 'MFI', 'WILLR', 'WILLIAMS', 'CCI', 'ADX', 'MACD', 'MOM', 'ROC'];
	const mainPanelIndicators = ['EMA', 'SMA', 'WMA', 'VWAP', 'BB', 'BOLLINGER', 'DONCHIAN', 'DC_', 'KELTNER', 'SUPER'];
	if (subPanelIndicators.some((token) => key.includes(token))) return 'sub';
	if (mainPanelIndicators.some((token) => key.includes(token))) return 'main';
	return 'none';
}

export function resolveIndicatorPanel(
	name: string,
	config?: SessionIndicatorConfig
): SessionIndicatorConfig['panel'] {
	return config?.panel ?? inferIndicatorPanel(name);
}

export function isChartRenderableIndicator(name: string, config?: SessionIndicatorConfig): boolean {
	const panel = resolveIndicatorPanel(name, config);
	return panel === 'main' || panel === 'sub';
}

export function getIndicatorSidebarGroup(
	name: string,
	config?: SessionIndicatorConfig
): 'overlays' | 'lower' | 'sidebar' {
	const panel = resolveIndicatorPanel(name, config);
	if (panel === 'main') return 'overlays';
	if (panel === 'sub') return 'lower';
	return 'sidebar';
}

export function formatIndicatorValue(value: number | null, name: string): string {
	if (value === null) return '--';
	if (name.includes('RSI') || name.includes('Williams') || name.includes('ADX') || name.includes('CCI')) {
		return value.toFixed(1);
	}
	if (value > 1000) return value.toFixed(2);
	if (Math.abs(value) < 1) return value.toFixed(4);
	return value.toFixed(2);
}

/**
 * Turn a SessionIndicatorsResponse into the main/sub IndicatorConfig arrays that
 * ChartWorkspace consumes, honoring a visibility map.
 */
export function buildChartIndicators(
	data: SessionIndicatorsResponse,
	visibility: Record<string, boolean>
): { main: IndicatorConfig[]; sub: IndicatorConfig[] } {
	const main: IndicatorConfig[] = [];
	const sub: IndicatorConfig[] = [];
	for (const [name, history] of Object.entries(data.indicators)) {
		const config = data.config[name];
		const panel = resolveIndicatorPanel(name, config);
		if (!isChartRenderableIndicator(name, config)) continue;
		const indicator: IndicatorConfig = {
			id: name,
			name,
			params: {},
			color: config?.color || getIndicatorColor(name),
			panel: panel === 'main' ? 'main' : 'sub1',
			visible: visibility[name] ?? true,
			data: history
				.filter((p) => p.value !== null && p.value !== undefined)
				.map((p) => ({ timestamp: p.timestamp, value: p.value as number }))
		};
		if (panel === 'main') main.push(indicator);
		else if (panel === 'sub') sub.push(indicator);
	}
	return { main, sub };
}
