import { get, writable } from 'svelte/store';

export type DataFetchStatus = 'idle' | 'running' | 'success' | 'error' | 'cancelled';

export interface DataFetchState {
	status: DataFetchStatus;
	taskId: string | null;
	label: string;
	message: string | null;
	// Non-fatal completion note (e.g. a venue-cap warning: Kraken only serves the
	// most recent ~720 candles). Shown alongside a success message.
	warning: string | null;
	progress: string;
	startedAt: number | null;
	finishedAt: number | null;
	isBulk: boolean;
}

const initialState: DataFetchState = {
	status: 'idle',
	taskId: null,
	label: '',
	message: null,
	warning: null,
	progress: '',
	startedAt: null,
	finishedAt: null,
	isBulk: false
};

export const dataFetchState = writable<DataFetchState>({ ...initialState });

let activeAbortController: AbortController | null = null;

export function setDataFetchAbortController(controller: AbortController | null) {
	activeAbortController = controller;
}

export function startDataFetchTask(label: string, options?: { taskId?: string; isBulk?: boolean }): string {
	const current = get(dataFetchState);
	if (current.status === 'running') {
		throw new Error('A data fetch is already running');
	}

	const taskId = options?.taskId ?? `data-fetch-${Date.now()}`;
	dataFetchState.set({
		status: 'running',
		taskId,
		label,
		message: null,
		warning: null,
		progress: '',
		startedAt: Date.now(),
		finishedAt: null,
		isBulk: Boolean(options?.isBulk)
	});
	return taskId;
}

export function updateDataFetchProgress(progress: string) {
	dataFetchState.update((state) => {
		if (state.status !== 'running') return state;
		return {
			...state,
			progress
		};
	});
}

export function completeDataFetchSuccess(message: string, warning: string | null = null) {
	activeAbortController = null;
	dataFetchState.update((state) => ({
		...state,
		status: 'success',
		message,
		warning,
		progress: '',
		finishedAt: Date.now()
	}));
}

export function completeDataFetchError(message: string, status: Extract<DataFetchStatus, 'error' | 'cancelled'> = 'error') {
	activeAbortController = null;
	dataFetchState.update((state) => ({
		...state,
		status,
		message,
		progress: '',
		finishedAt: Date.now()
	}));
}

export function cancelDataFetchTask() {
	const current = get(dataFetchState);
	if (current.status !== 'running') return;
	if (activeAbortController) {
		activeAbortController.abort();
		activeAbortController = null;
	}
	dataFetchState.set({
		...current,
		status: 'cancelled',
		message: 'Fetch cancelled',
		progress: '',
		finishedAt: Date.now()
	});
}

export function clearDataFetchTask() {
	dataFetchState.set({ ...initialState });
}

// ============== Last-used form config ==============
// Persists across component mounts within the same SPA session

export interface DataFetchFormConfig {
	source: string;
	symbol: string;
	timeframe: string;
	exchange: string;
	limit: number;
	since: string;
	until: string;
	allAvailable: boolean;
	allTimeframes: boolean;
}

let lastFormConfig: DataFetchFormConfig | null = null;

export function saveDataFetchFormConfig(config: DataFetchFormConfig) {
	lastFormConfig = { ...config };
}

export function getDataFetchFormConfig(): DataFetchFormConfig | null {
	return lastFormConfig;
}
