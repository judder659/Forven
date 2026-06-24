import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../lib/api';
import {
	archiveDeepdiveThread,
	createOrGetDeepdiveThread,
	listDeepdiveMessages,
	streamDeepdiveSend,
	type DeepdiveMessage,
	type DeepdiveStreamEvent,
	type DeepdiveThread,
} from '../lib/api/deepdive';

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('deepdive api client', () => {
	beforeEach(() => {
		mockFetch.mockReset();
	});
	afterEach(() => {
		vi.clearAllMocks();
	});

	it('re-exports through the api barrel', () => {
		expect(typeof api.createOrGetDeepdiveThread).toBe('function');
		expect(typeof api.archiveDeepdiveThread).toBe('function');
		expect(typeof api.listDeepdiveMessages).toBe('function');
		expect(typeof api.streamDeepdiveSend).toBe('function');
	});

	it('createOrGetDeepdiveThread posts strategy_id', async () => {
		mockFetch.mockResolvedValueOnce({
			ok: true,
			json: () => Promise.resolve({
				id: 'dd_1', strategy_id: 'S1',
				created_at: 't', updated_at: 't', archived_at: null,
			}),
		});
		const t = await createOrGetDeepdiveThread('S1');
		expect(t.id).toBe('dd_1');
		const [url, init] = mockFetch.mock.calls[0];
		expect(String(url)).toContain('/api/deepdive/threads');
		expect((init as RequestInit).method).toBe('POST');
		expect(JSON.parse(String((init as RequestInit).body))).toEqual({ strategy_id: 'S1' });
	});

	it('archiveDeepdiveThread posts to archive subpath', async () => {
		mockFetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ ok: true }) });
		await archiveDeepdiveThread('dd_1');
		const [url, init] = mockFetch.mock.calls[0];
		expect(String(url)).toContain('/api/deepdive/threads/dd_1/archive');
		expect((init as RequestInit).method).toBe('POST');
	});

	it('listDeepdiveMessages unwraps the messages array', async () => {
		const mock: DeepdiveMessage[] = [
			{ id: 'm1', thread_id: 'dd_1', role: 'user', content: 'hi',
			  tool_call: null, created_at: 't', cost_usd: null, model: null },
			{ id: 'm2', thread_id: 'dd_1', role: 'assistant', content: 'hello',
			  tool_call: null, created_at: 't', cost_usd: null, model: null },
		];
		mockFetch.mockResolvedValueOnce({
			ok: true, json: () => Promise.resolve({ messages: mock }),
		});
		const got = await listDeepdiveMessages('dd_1');
		expect(got).toEqual(mock);
	});

	it('streamDeepdiveSend parses SSE chunks and invokes callback per event', async () => {
		// Build a fake stream that emits two SSE blocks across two reader chunks
		const encoder = new TextEncoder();
		const chunks = [
			encoder.encode('data: {"type":"user_persisted"}\n\n'),
			encoder.encode('data: {"type":"assistant_token","content":"hi"}\n\ndata: {"type":"done","message_id":"m1"}\n\n'),
		];
		let i = 0;
		const reader = {
			read: vi.fn().mockImplementation(async () => {
				if (i < chunks.length) {
					return { value: chunks[i++], done: false };
				}
				return { value: undefined, done: true };
			}),
		};
		mockFetch.mockResolvedValueOnce({
			ok: true,
			body: { getReader: () => reader },
		});

		const events: DeepdiveStreamEvent[] = [];
		await streamDeepdiveSend('dd_1', 'hi', (e) => events.push(e));

		expect(events.map((e) => e.type)).toEqual([
			'user_persisted', 'assistant_token', 'done',
		]);
		const [url, init] = mockFetch.mock.calls[0];
		expect(String(url)).toContain('/api/deepdive/threads/dd_1/send');
		expect((init as RequestInit).method).toBe('POST');
		expect(JSON.parse(String((init as RequestInit).body))).toEqual({ user_text: 'hi' });
	});

	it('streamDeepdiveSend throws on non-2xx', async () => {
		mockFetch.mockResolvedValueOnce({ ok: false, status: 409, body: null });
		await expect(streamDeepdiveSend('dd_1', 'x', () => {})).rejects.toThrow();
	});

	// Type-only assertion: DeepdiveThread shape compiles
	it('types compile', () => {
		const t: DeepdiveThread = {
			id: 'x', strategy_id: 'y',
			created_at: '', updated_at: '', archived_at: null,
		};
		expect(t.id).toBe('x');
	});
});
