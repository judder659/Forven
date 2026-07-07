import { ACTIVE_API_BASE, API_BASE, buildAuthHeaders, fetchApi } from './core';
import type { PageContext } from '$lib/stores/pageContext';

export type AssistantThread = {
	id: string;
	scope_kind: string | null;
	scope_id: string | null;
	page_route: string | null;
	title: string | null;
	created_at: string;
	updated_at: string;
	archived_at: string | null;
};

export type AssistantMessageRole = 'user' | 'assistant' | 'tool' | 'action';

export type AssistantToolCall = {
	id?: string;
	name?: string;
	input?: Record<string, unknown>;
	summary?: string;
	calls?: Array<{ id?: string; name?: string; input?: Record<string, unknown> }>;
};

export type AssistantMessage = {
	id: string;
	thread_id: string;
	seq: number;
	role: AssistantMessageRole;
	content: string;
	tool_call: AssistantToolCall | null;
	status: string | null;
	created_at: string;
	cost_usd: number | null;
	model: string | null;
};

export type AssistantStreamEvent =
	| { type: 'user_persisted' }
	| { type: 'assistant_token'; content: string }
	| { type: 'tool_call'; name: string; input: Record<string, unknown> }
	| { type: 'tool_result'; name: string; output: string }
	| { type: 'action_proposed'; action_id: string; name: string; input: Record<string, unknown>; summary: string }
	| { type: 'navigate'; route: string }
	| { type: 'done'; message_id: string }
	| { type: 'error'; code: string; message: string };

export type AssistantConfirmResult = {
	ok: boolean;
	status: string;
	message: string;
	output?: string;
};

export async function createOrGetAssistantThread(
	scope?: { kind?: string; id?: string; pageRoute?: string },
): Promise<AssistantThread> {
	return fetchApi<AssistantThread>('/assistant/threads', {
		method: 'POST',
		body: JSON.stringify({
			scope_kind: scope?.kind ?? 'global',
			scope_id: scope?.id ?? null,
			page_route: scope?.pageRoute ?? null,
		}),
	});
}

export async function listAssistantMessages(threadId: string): Promise<AssistantMessage[]> {
	const resp = await fetchApi<{ messages: AssistantMessage[] }>(
		`/assistant/threads/${encodeURIComponent(threadId)}/messages`,
	);
	return resp.messages;
}

export async function archiveAssistantThread(threadId: string): Promise<void> {
	await fetchApi<{ ok: boolean }>(`/assistant/threads/${encodeURIComponent(threadId)}/archive`, {
		method: 'POST',
	});
}

export async function confirmAssistantAction(
	threadId: string,
	actionId: string,
	approve: boolean,
): Promise<AssistantConfirmResult> {
	return fetchApi<AssistantConfirmResult>(
		`/assistant/threads/${encodeURIComponent(threadId)}/actions/${encodeURIComponent(actionId)}/confirm`,
		{ method: 'POST', body: JSON.stringify({ approve }) },
	);
}

export async function getAssistantCostCap(): Promise<number> {
	const r = await fetchApi<{ cap_usd: number }>('/assistant/cost-cap');
	return r.cap_usd;
}

export async function setAssistantCostCap(capUsd: number): Promise<number> {
	const r = await fetchApi<{ cap_usd: number }>('/assistant/cost-cap', {
		method: 'PUT',
		body: JSON.stringify({ cap_usd: capUsd }),
	});
	return r.cap_usd;
}

function resolveStreamBase(): string {
	const base = (ACTIVE_API_BASE && ACTIVE_API_BASE.trim()) || API_BASE;
	return base.endsWith('/') ? base.slice(0, -1) : base;
}

export async function streamAssistantSend(
	threadId: string,
	userText: string,
	pageContext: PageContext | null,
	onEvent: (event: AssistantStreamEvent) => void,
	allowActions = true,
): Promise<void> {
	const url = `${resolveStreamBase()}/assistant/threads/${encodeURIComponent(threadId)}/send`;
	const r = await fetch(url, {
		method: 'POST',
		headers: {
			'content-type': 'application/json',
			...buildAuthHeaders(),
		},
		body: JSON.stringify({
			user_text: userText,
			page_context: pageContext ?? null,
			allow_actions: allowActions,
		}),
	});
	if (!r.ok || !r.body) {
		throw new Error(`assistant send failed: ${r.status}`);
	}
	const reader = r.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	while (true) {
		const { value, done } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });
		let sepIdx = buffer.indexOf('\n\n');
		while (sepIdx !== -1) {
			const block = buffer.slice(0, sepIdx);
			buffer = buffer.slice(sepIdx + 2);
			const dataLine = block.split('\n').find((line) => line.startsWith('data: '));
			if (dataLine) {
				try {
					onEvent(JSON.parse(dataLine.slice('data: '.length)) as AssistantStreamEvent);
				} catch {
					// malformed event — ignore
				}
			}
			sepIdx = buffer.indexOf('\n\n');
		}
	}
}
