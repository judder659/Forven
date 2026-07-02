import type { PageLoad } from './$types';
import { getTaskAudit } from '$lib/api';
import type { TaskContainer, TaskAuditEvent, TaskTranscriptMessage } from '$lib/api';

export const ssr = false;

interface TaskDetailData {
	taskId: string;
	task: TaskContainer | null;
	auditLog: TaskAuditEvent[];
	toolCalls: Array<Record<string, unknown>>;
	transcript: TaskTranscriptMessage[];
	loadError: string | null;
}

export const load: PageLoad = async ({ params }) => {
	const taskId = params.id ?? '';
	try {
		const details = await getTaskAudit(taskId);
		return {
			taskId,
			task: details.task,
			auditLog: Array.isArray(details.audit_log) ? details.audit_log : [],
			toolCalls: Array.isArray(details.tool_calls) ? details.tool_calls : [],
			transcript: Array.isArray(details.transcript) ? details.transcript : [],
			loadError: null,
		} satisfies TaskDetailData;
	} catch (e) {
		return {
			taskId,
			task: null,
			auditLog: [] as TaskAuditEvent[],
			toolCalls: [] as Array<Record<string, unknown>>,
			transcript: [] as TaskTranscriptMessage[],
			loadError: e instanceof Error ? e.message : 'Failed to load task details',
		} satisfies TaskDetailData;
	}
};
