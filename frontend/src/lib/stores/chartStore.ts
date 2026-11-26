/**
 * Chart Store - Centralized state management for the Chart page
 */

import { writable, derived } from 'svelte/store';
import type { Strategy, IndicatorInfo } from '$lib/api';

export interface IndicatorConfig {
	id: string;
	name: string;
	params: Record<string, unknown>;
	color: string;
	panel: 'main' | 'sub1';
	visible: boolean;
	data?: Array<{ timestamp: string; value: number }>;
	isStrategyIndicator?: boolean; // True if this indicator came from a strategy
	/** Lower-pane series with the same group share a scale; different groups each get their own. */
	group?: string;
}

export interface SignalMarker {
	timestamp: string;
	price: number;
	type: 'entry' | 'exit';
	direction?: 'long' | 'short' | string;
	label?: string;
	source?: 'trade' | 'signal' | string;
	// Self-describing visuals forwarded from the backend marker payload. When
	// present the chart renders straight from these; otherwise it derives from
	// direction + entry/exit (backward-compatible).
	side?: 'bull' | 'bear' | string;
	action?: string;
	shape?: 'arrowUp' | 'arrowDown' | string;
	color?: string;
}

export interface ChartLayout {
	id: string;
	name: string;
	symbol: string;
	timeframe: string;
	strategyName: string | null;
	strategyParams: Record<string, unknown>;
	indicators: IndicatorConfig[];
	showSignals: boolean;
	createdAt: string;
}

export interface ChartState {
	symbol: string;
	timeframe: string;
	selectedStrategy: Strategy | null;
	strategyParams: Record<string, unknown>;
	showSignals: boolean;
	activeIndicators: IndicatorConfig[];
	entryMarkers: SignalMarker[];
	exitMarkers: SignalMarker[];
	isLoadingSignals: boolean;
	signalError: string | null;
}

// Initial state
const initialState: ChartState = {
	symbol: '',
	timeframe: '',
	selectedStrategy: null,
	strategyParams: {},
	showSignals: true,
	activeIndicators: [],
	entryMarkers: [],
	exitMarkers: [],
	isLoadingSignals: false,
	signalError: null,
};

// Create the main store
function createChartStore() {
	const { subscribe, set, update } = writable<ChartState>(initialState);

	return {
		subscribe,
		set,
		update,

		// Set symbol and timeframe
		setDataset(symbol: string, timeframe: string) {
			update((state) => ({
				...state,
				symbol,
				timeframe,
				// Clear signals when dataset changes
				entryMarkers: [],
				exitMarkers: [],
			}));
		},

		// Set selected strategy
		setStrategy(strategy: Strategy | null) {
			update((state) => {
				// Initialize params from strategy defaults
				const params: Record<string, unknown> = {};
				if (strategy) {
					for (const [key, spec] of Object.entries(strategy.parameters)) {
						params[key] = spec.default;
					}
				}
				return {
					...state,
					selectedStrategy: strategy,
					strategyParams: params,
					// Clear signals when strategy changes
					entryMarkers: [],
					exitMarkers: [],
				};
			});
		},

		// Update a single strategy parameter
		setStrategyParam(key: string, value: unknown) {
			update((state) => ({
				...state,
				strategyParams: {
					...state.strategyParams,
					[key]: value,
				},
			}));
		},

		// Set all strategy parameters
		setStrategyParams(params: Record<string, unknown>) {
			update((state) => ({
				...state,
				strategyParams: params,
			}));
		},

		// Toggle signal visibility
		toggleSignals(show?: boolean) {
			update((state) => ({
				...state,
				showSignals: show !== undefined ? show : !state.showSignals,
			}));
		},

		// Set signal markers
		setSignals(entries: SignalMarker[], exits: SignalMarker[]) {
			update((state) => ({
				...state,
				entryMarkers: entries,
				exitMarkers: exits,
				isLoadingSignals: false,
				signalError: null,
			}));
		},

		// Set strategy indicators (from strategy's get_indicators)
		setStrategyIndicators(indicators: IndicatorConfig[]) {
			update((state) => {
				// Remove any existing strategy indicators
				const manualIndicators = state.activeIndicators.filter((i) => !i.isStrategyIndicator);
				// Add new strategy indicators with the flag set
				const strategyIndicators = indicators.map((i) => ({
					...i,
					isStrategyIndicator: true,
				}));
				return {
					...state,
					activeIndicators: [...strategyIndicators, ...manualIndicators],
				};
			});
		},

		// Clear strategy indicators (when strategy is deselected)
		clearStrategyIndicators() {
			update((state) => ({
				...state,
				activeIndicators: state.activeIndicators.filter((i) => !i.isStrategyIndicator),
			}));
		},

		// Set loading state for signals
		setLoadingSignals(loading: boolean) {
			update((state) => ({
				...state,
				isLoadingSignals: loading,
				signalError: loading ? null : state.signalError,
			}));
		},

		// Set signal error
		setSignalError(error: string | null) {
			update((state) => ({
				...state,
				signalError: error,
				isLoadingSignals: false,
			}));
		},

		// Add an indicator
		addIndicator(indicator: IndicatorConfig) {
			update((state) => ({
				...state,
				activeIndicators: [...state.activeIndicators, indicator],
			}));
		},

		// Remove an indicator
		removeIndicator(id: string) {
			update((state) => ({
				...state,
				activeIndicators: state.activeIndicators.filter((i) => i.id !== id),
			}));
		},

		// Update an indicator
		updateIndicator(id: string, updates: Partial<IndicatorConfig>) {
			update((state) => ({
				...state,
				activeIndicators: state.activeIndicators.map((i) =>
					i.id === id ? { ...i, ...updates } : i
				),
			}));
		},

		// Toggle indicator visibility
		toggleIndicatorVisibility(id: string) {
			update((state) => ({
				...state,
				activeIndicators: state.activeIndicators.map((i) =>
					i.id === id ? { ...i, visible: !i.visible } : i
				),
			}));
		},

		// Clear all indicators
		clearIndicators() {
			update((state) => ({
				...state,
				activeIndicators: [],
			}));
		},

		// Reset to initial state
		reset() {
			set(initialState);
		},
	};
}

export const chartStore = createChartStore();

// Derived stores for convenience
export const mainPanelIndicators = derived(chartStore, ($chart) =>
	$chart.activeIndicators.filter((i) => i.panel === 'main' && i.visible)
);

export const subPanelIndicators = derived(chartStore, ($chart) =>
	$chart.activeIndicators.filter((i) => i.panel === 'sub1' && i.visible)
);

export const hasActiveStrategy = derived(
	chartStore,
	($chart) => $chart.selectedStrategy !== null && $chart.symbol && $chart.timeframe
);

// Color palette for indicators
export const INDICATOR_COLORS = [
	'#e5e7eb', // light gray (no blue)
	'#f59e0b', // amber
	'#10b981', // emerald
	'#8b5cf6', // violet
	'#ec4899', // pink
	'#f97316', // orange
	'#84cc16', // lime
	'#14b8a6', // teal
	'#a3a3a3', // gray
	'#d946ef', // fuchsia
];

let colorIndex = 0;

export function getNextColor(): string {
	const color = INDICATOR_COLORS[colorIndex % INDICATOR_COLORS.length];
	colorIndex++;
	return color;
}

export function resetColorIndex() {
	colorIndex = 0;
}

// Layout persistence using localStorage
const LAYOUTS_KEY = 'forven_chart_layouts';

export function saveLayout(layout: ChartLayout): void {
	const layouts = getLayouts();
	const existingIndex = layouts.findIndex((l) => l.id === layout.id);
	if (existingIndex >= 0) {
		layouts[existingIndex] = layout;
	} else {
		layouts.push(layout);
	}
	localStorage.setItem(LAYOUTS_KEY, JSON.stringify(layouts));
}

export function getLayouts(): ChartLayout[] {
	try {
		const stored = localStorage.getItem(LAYOUTS_KEY);
		return stored ? JSON.parse(stored) : [];
	} catch {
		return [];
	}
}

export function deleteLayout(id: string): void {
	const layouts = getLayouts().filter((l) => l.id !== id);
	localStorage.setItem(LAYOUTS_KEY, JSON.stringify(layouts));
}

export function generateLayoutId(): string {
	return `layout_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}
