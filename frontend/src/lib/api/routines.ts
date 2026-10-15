import { fetchApi } from './core';

export interface Routine {
	id: number;
	name: string;
	prompt: string;
	cron_expr: string;
	tools_context: string;
	channel: string | null;
	enabled: number | boolean;
	created_by: string | null;
	approval_id: number | null;
	last_run_at: string | null;
	last_status: string | null;
	last_error: string | null;
	/** Next scheduled fire (UTC ISO); null while paused. */
	next_run_at?: string | null;
	created_at: string;
	updated_at: string;
}

export interface RoutineCreatePayload {
	name: string;
	prompt: string;
	cron_expr: string;
	tools_context?: string;
	channel?: string | null;
	enabled?: boolean;
}

export interface RoutineUpdatePayload {
	name?: string;
	prompt?: string;
	cron_expr?: string;
	tools_context?: string;
	/** Discord channel alias or raw id; send '' to clear. */
	channel?: string | null;
	enabled?: boolean;
}

export async function listRoutines(enabledOnly = false): Promise<Routine[]> {
	const path = enabledOnly ? '/routines?enabled_only=true' : '/routines';
	const res = await fetchApi<{ routines: Routine[] }>(path);
	return res.routines || [];
}

export async function createRoutine(payload: RoutineCreatePayload): Promise<Routine> {
	const res = await fetchApi<{ routine: Routine }>('/routines', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
	return res.routine;
}

export async function getRoutine(id: number): Promise<Routine> {
	const res = await fetchApi<{ routine: Routine }>(`/routines/${id}`);
	return res.routine;
}

export async function updateRoutine(id: number, payload: RoutineUpdatePayload): Promise<Routine> {
	const res = await fetchApi<{ routine: Routine }>(`/routines/${id}`, {
		method: 'PUT',
		body: JSON.stringify(payload),
	});
	return res.routine;
}

export async function deleteRoutine(id: number): Promise<void> {
	await fetchApi(`/routines/${id}`, { method: 'DELETE' });
}

export async function pauseRoutine(id: number): Promise<Routine> {
	const res = await fetchApi<{ routine: Routine }>(`/routines/${id}/pause`, { method: 'POST' });
	return res.routine;
}

export async function resumeRoutine(id: number): Promise<Routine> {
	const res = await fetchApi<{ routine: Routine }>(`/routines/${id}/resume`, { method: 'POST' });
	return res.routine;
}

export interface RoutineRunResult {
	task_id: number;
	display_id: string;
	routine_id: number;
}

export async function runRoutine(id: number): Promise<RoutineRunResult> {
	return await fetchApi<RoutineRunResult>(`/routines/${id}/run`, {
		method: 'POST',
		body: JSON.stringify({}),
	});
}

export async function previewRoutineSchedule(id: number, count = 5): Promise<string[]> {
	const res = await fetchApi<{ upcoming: string[] }>(
		`/routines/${id}/preview?count=${encodeURIComponent(String(count))}`,
		{ method: 'POST', body: JSON.stringify({}) },
	);
	return res.upcoming || [];
}

/** A Discord channel the bot can post to. `id` is what gets stored on the
 * routine (raw channel id from the live guild list, or an alias name when
 * the backend falls back to the static alias map). */
export interface RoutineChannel {
	id: string;
	label: string;
}

/** Discord channels the bot can deliver routine results to — live guild list
 * when the bot has connected, alias-map fallback otherwise. */
export async function listRoutineChannels(): Promise<RoutineChannel[]> {
	const res = await fetchApi<{ channels: RoutineChannel[] }>('/routines/channels');
	return res.channels || [];
}

export async function previewCronExpression(cron_expr: string, count = 5): Promise<string[]> {
	const res = await fetchApi<{ upcoming: string[] }>('/routines/preview', {
		method: 'POST',
		body: JSON.stringify({ cron_expr, count }),
	});
	return res.upcoming || [];
}
