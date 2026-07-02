<script lang="ts">
	import { fly } from 'svelte/transition';
	import {
		createOrGetAssistantThread,
		listAssistantMessages,
		archiveAssistantThread,
		confirmAssistantAction,
		streamAssistantSend,
		type AssistantMessage,
		type AssistantStreamEvent,
	} from '$lib/api/assistant';
	import { assistantUI, closeAssistant } from '$lib/stores/assistantUI';
	import { pageContext, type PageContext } from '$lib/stores/pageContext';
	import { chatUnreadCount, incrementChatUnread, markChatRead } from '$lib/stores/chatStore';
	import { renderMarkdown } from '$lib/utils/markdown';

	type ActionStatus = 'pending' | 'confirming' | 'executed' | 'failed' | 'rejected' | 'approved';

	type UIMsg = {
		kind: 'user' | 'assistant' | 'tool' | 'action' | 'error';
		content: string;
		toolName?: string;
		actionId?: string;
		actionName?: string;
		actionStatus?: ActionStatus;
		summary?: string;
		ts: string;
	};

	let threadId: string | null = null;
	let messages: UIMsg[] = [];
	let input = '';
	let sending = false;
	let allowActions = true;
	let loadingHistory = false;
	let initError = '';
	let liveAssistantIdx: number | null = null;
	let messagesEl: HTMLDivElement;
	let openedOnce = false;
	let lastHandledSendKey = 0;

	$: open = $assistantUI.open;
	$: contextLabel = buildContextLabel($pageContext);
	$: suggestions = buildSuggestions($pageContext.page_kind);

	function buildContextLabel(pc: PageContext): string {
		const kind = (pc?.page_kind || '').replace(/_/g, ' ');
		const ent = pc?.entity?.label || pc?.entity?.id;
		return ent ? `${kind} · ${ent}` : kind;
	}

	function buildSuggestions(kind: string): string[] {
		switch (kind) {
			case 'strategy_detail':
				return ['How is this strategy doing?', 'Backtest this strategy', 'How could I improve it?'];
			case 'paper_trading':
				return ['How is the paper book doing?', 'Any open positions?', "What's the market regime?"];
			case 'lab':
				return ['Create a BTC mean-reversion strategy', "What's in the pipeline?", 'Top strategies right now'];
			case 'data_engine':
				return ['What datasets do we have?', 'Any data gaps?'];
			case 'pipeline':
				return ["What's in the pipeline?", 'Anything waiting on me?'];
			default:
				return ["How's the portfolio?", "What's in the pipeline?", 'Create a BTC 15m mean-reversion strategy'];
		}
	}

	function fmtTime(iso: string): string {
		try {
			return new Date(iso).toLocaleTimeString();
		} catch {
			return iso;
		}
	}

	function compactJson(obj: unknown): string {
		try {
			const s = JSON.stringify(obj);
			return s.length > 140 ? s.slice(0, 140) + '…' : s;
		} catch {
			return '';
		}
	}

	function scrollToBottom() {
		if (messagesEl) {
			requestAnimationFrame(() => {
				messagesEl.scrollTop = messagesEl.scrollHeight;
			});
		}
	}

	function pushMsg(m: UIMsg): number {
		messages = [...messages, m];
		scrollToBottom();
		return messages.length - 1;
	}

	function updateMsg(i: number, patch: Partial<UIMsg>) {
		if (i < 0 || i >= messages.length) return;
		const copy = [...messages];
		copy[i] = { ...copy[i], ...patch };
		messages = copy;
		scrollToBottom();
	}

	function mapHistory(history: AssistantMessage[]): UIMsg[] {
		const out: UIMsg[] = [];
		for (const m of history) {
			if (m.role === 'user') {
				out.push({ kind: 'user', content: m.content, ts: m.created_at });
			} else if (m.role === 'assistant') {
				if (m.content.trim()) out.push({ kind: 'assistant', content: m.content, ts: m.created_at });
			} else if (m.role === 'tool') {
				if (m.content.startsWith('PENDING_CONFIRMATION')) continue; // the action card carries this
				out.push({ kind: 'tool', content: m.content, toolName: m.tool_call?.name, ts: m.created_at });
			} else if (m.role === 'action') {
				out.push({
					kind: 'action',
					content: m.content,
					actionId: m.id,
					actionName: m.tool_call?.name,
					summary: m.tool_call?.summary || m.content,
					actionStatus: (m.status as ActionStatus) || 'pending',
					ts: m.created_at,
				});
			}
		}
		return out;
	}

	async function ensureThread(): Promise<string | null> {
		if (threadId) return threadId;
		loadingHistory = true;
		initError = '';
		try {
			const t = await createOrGetAssistantThread({ pageRoute: $pageContext.route });
			threadId = t.id;
			messages = mapHistory(await listAssistantMessages(t.id));
			return t.id;
		} catch (err) {
			initError = String(err);
			return null;
		} finally {
			loadingHistory = false;
			scrollToBottom();
		}
	}

	function onEvent(ev: AssistantStreamEvent) {
		if (ev.type === 'assistant_token') {
			// `content` is an incremental token delta — append it to the live bubble.
			if (liveAssistantIdx === null) {
				liveAssistantIdx = pushMsg({ kind: 'assistant', content: ev.content, ts: new Date().toISOString() });
			} else {
				updateMsg(liveAssistantIdx, { content: messages[liveAssistantIdx].content + ev.content });
			}
		} else if (ev.type === 'tool_call') {
			liveAssistantIdx = null;
			pushMsg({ kind: 'tool', content: `→ ${ev.name}(${compactJson(ev.input)})`, toolName: ev.name, ts: new Date().toISOString() });
		} else if (ev.type === 'tool_result') {
			liveAssistantIdx = null;
			pushMsg({ kind: 'tool', content: ev.output, toolName: ev.name, ts: new Date().toISOString() });
		} else if (ev.type === 'action_proposed') {
			liveAssistantIdx = null;
			pushMsg({
				kind: 'action',
				content: ev.summary,
				actionId: ev.action_id,
				actionName: ev.name,
				summary: ev.summary,
				actionStatus: 'pending',
				ts: new Date().toISOString(),
			});
		} else if (ev.type === 'error') {
			liveAssistantIdx = null;
			pushMsg({ kind: 'error', content: ev.message, ts: new Date().toISOString() });
		} else if (ev.type === 'done') {
			liveAssistantIdx = null;
		}
	}

	async function send(text?: string) {
		const body = (text ?? input).trim();
		if (!body || sending) return;
		const tid = await ensureThread();
		if (!tid) {
			pushMsg({ kind: 'error', content: initError || 'Could not open the assistant.', ts: new Date().toISOString() });
			return;
		}
		if (text === undefined) input = '';
		sending = true;
		pushMsg({ kind: 'user', content: body, ts: new Date().toISOString() });
		liveAssistantIdx = null;
		try {
			await streamAssistantSend(tid, body, $pageContext, onEvent, allowActions);
		} catch (err) {
			pushMsg({ kind: 'error', content: String(err), ts: new Date().toISOString() });
		} finally {
			sending = false;
			liveAssistantIdx = null;
			if (!open) incrementChatUnread();
		}
	}

	async function confirm(idx: number, approve: boolean) {
		const m = messages[idx];
		if (!m?.actionId || !threadId || m.actionStatus !== 'pending') return;
		updateMsg(idx, { actionStatus: 'confirming' });
		try {
			const r = await confirmAssistantAction(threadId, m.actionId, approve);
			updateMsg(idx, { actionStatus: (r.status as ActionStatus) || (approve ? 'executed' : 'rejected') });
			const note = approve && r.output ? `${r.message}\n\n${r.output}` : r.message;
			if (note) pushMsg({ kind: 'assistant', content: note, ts: new Date().toISOString() });
		} catch (err) {
			updateMsg(idx, { actionStatus: 'failed' });
			pushMsg({ kind: 'error', content: String(err), ts: new Date().toISOString() });
		}
	}

	async function newThread() {
		if (sending) return;
		if (threadId) {
			try {
				await archiveAssistantThread(threadId);
			} catch {
				// best-effort
			}
		}
		threadId = null;
		messages = [];
		await ensureThread();
	}

	function sendChip(text: string) {
		if (sending) return;
		void send(text);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			void send();
		}
		if (e.key === 'Escape') {
			closeAssistant();
		}
	}

	// Lifecycle: open the (persistent) thread + history once, on first open.
	$: if (open && !openedOnce) {
		openedOnce = true;
		markChatRead();
		void ensureThread();
	}
	$: if (open) {
		markChatRead();
		scrollToBottom();
	}
	// Quick-action auto-send from openAssistant(prefill, true).
	$: if (open && $assistantUI.sendKey !== lastHandledSendKey) {
		lastHandledSendKey = $assistantUI.sendKey;
		if ($assistantUI.prefill && !sending) void send($assistantUI.prefill);
	}

	function actionStatusLabel(s?: ActionStatus): string {
		switch (s) {
			case 'executed':
				return '✓ Done';
			case 'approved':
				return '✓ Approved';
			case 'failed':
				return '✗ Failed';
			case 'rejected':
				return 'Cancelled';
			case 'confirming':
				return 'Working…';
			default:
				return '';
		}
	}
</script>

{#if open}
	<!-- Backdrop -->
	<div class="fixed inset-0 bg-black/80 z-[9998] pointer-events-none"></div>

	<!-- Panel -->
	<div
		class="fixed top-0 right-0 h-full w-[440px] max-w-[92vw] bg-[#050505] border-l border-[#222] z-[9999] flex flex-col"
		transition:fly={{ x: 440, duration: 250 }}
	>
		<!-- Header -->
		<div class="flex items-center justify-between px-4 py-3 border-b border-[#222]">
			<div class="flex items-center gap-2 min-w-0">
				<div class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></div>
				<span class="text-sm font-bold text-white uppercase tracking-wider">Forven</span>
				{#if contextLabel}
					<span class="text-[10px] text-[#666] uppercase tracking-wider truncate">· {contextLabel}</span>
				{/if}
			</div>
			<div class="flex items-center gap-2">
				<button
					class="text-[10px] text-[#888] hover:text-white border border-[#333] hover:border-[#555] px-2 py-0.5 transition-colors disabled:opacity-40 uppercase tracking-wider"
					on:click={newThread}
					disabled={sending}
					title="Archive this conversation and start fresh"
				>
					New
				</button>
				<button
					class="text-[#555] hover:text-white transition-colors"
					aria-label="Close assistant"
					title="Close assistant"
					on:click={closeAssistant}
				>
					<svg class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
						<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
					</svg>
				</button>
			</div>
		</div>

		<!-- Messages -->
		<div class="flex-1 overflow-y-auto px-4 py-3 space-y-3" bind:this={messagesEl}>
			{#if loadingHistory && messages.length === 0}
				<div class="text-center text-[#555] text-xs uppercase tracking-widest mt-8">Opening…</div>
			{:else if messages.length === 0}
				<div class="text-center text-[#666] text-xs mt-8">
					<div class="text-lg font-bold uppercase tracking-widest mb-2 text-white">Forven</div>
					<div>Ask anything, or tell me what to do — I can see {contextLabel || 'this page'}.</div>
					<div class="mt-4 flex flex-wrap justify-center gap-2">
						{#each suggestions as suggestion}
							<button
								type="button"
								class="px-2.5 py-1 text-[11px] border border-[#333] bg-[#111] text-[#888] hover:text-white hover:border-[#555] transition-colors disabled:opacity-40"
								on:click={() => sendChip(suggestion)}
								disabled={sending}
							>
								{suggestion}
							</button>
						{/each}
					</div>
				</div>
			{/if}

			{#each messages as msg, idx}
				<div class="flex flex-col {msg.kind === 'user' ? 'items-end' : 'items-start'}">
					{#if msg.kind === 'tool'}
						<div class="max-w-[94%] border border-[#222] bg-[#111] px-3 py-2 font-mono text-[11px] text-[#888] whitespace-pre-wrap">
							<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-[#666] mb-0.5">{msg.toolName ?? 'tool'}</div>
							{msg.content && msg.content.length > 700 ? msg.content.slice(0, 700) + '\n…' : msg.content}
						</div>
					{:else if msg.kind === 'action'}
						<div class="max-w-[94%] w-full border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-[#ccc]">
							<div class="text-[9px] font-semibold uppercase tracking-[0.18em] text-yellow-400 mb-1">Confirm action</div>
							<div class="mb-2 whitespace-pre-wrap">{msg.summary || msg.content}</div>
							{#if msg.actionStatus === 'pending'}
								<div class="flex items-center gap-2">
									<button
										class="terminal-button-primary px-2.5 py-1 text-[11px]"
										on:click={() => confirm(idx, true)}
									>
										Approve
									</button>
									<button
										class="terminal-button px-2.5 py-1 text-[11px]"
										on:click={() => confirm(idx, false)}
									>
										Reject
									</button>
								</div>
							{:else}
								<div class="text-[11px] {msg.actionStatus === 'failed' ? 'text-red-400' : msg.actionStatus === 'rejected' ? 'text-[#888]' : 'text-emerald-400'}">
									{actionStatusLabel(msg.actionStatus)}
								</div>
							{/if}
						</div>
					{:else}
						<div class="max-w-[88%] px-3 py-2 text-xs {msg.kind === 'user' ? 'bg-white text-black' : msg.kind === 'error' ? 'bg-red-500/5 border border-red-900 text-red-400' : 'bg-[#111] border border-[#222] text-[#888]'}">
							{#if msg.kind === 'assistant' && !msg.content && sending}
								<div class="flex items-center gap-2 text-[#666]">
									<div class="w-3 h-3 border border-[#555] border-t-transparent rounded-full animate-spin"></div>
									<span>Thinking…</span>
								</div>
							{:else if msg.kind === 'user' || msg.kind === 'error'}
								<div class="whitespace-pre-wrap">{msg.content}</div>
							{:else}
								<div class="chat-markdown prose prose-invert prose-xs">{@html renderMarkdown(msg.content)}</div>
							{/if}
						</div>
					{/if}
					<div class="text-[9px] text-[#555] mt-0.5 px-1">{fmtTime(msg.ts)}</div>
				</div>
			{/each}

			{#if sending && liveAssistantIdx === null}
				<div class="flex items-start">
					<div class="px-3 py-2 text-xs bg-[#111] border border-[#222] text-[#666] flex items-center gap-2">
						<div class="w-3 h-3 border border-[#555] border-t-transparent rounded-full animate-spin"></div>
						<span>Working…</span>
					</div>
				</div>
			{/if}
		</div>

		<!-- Input -->
		<div class="border-t border-[#222] px-4 py-3">
			<div class="flex items-center justify-between mb-2">
				<label class="flex items-center gap-1.5 text-[10px] text-[#666] cursor-pointer select-none" title="When off, the assistant answers and advises but takes no actions.">
					<input type="checkbox" bind:checked={allowActions} class="accent-emerald-500 h-3 w-3" />
					Allow actions
				</label>
				<span class="text-[9px] text-[#555]">Create + backtest run directly · promotions ask first</span>
			</div>
			<div class="flex items-center gap-2">
				<input
					type="text"
					bind:value={input}
					on:keydown={handleKeydown}
					placeholder="Ask, or tell me what to do…"
					class="flex-1 terminal-input text-xs"
					disabled={sending}
				/>
				<button
					class="terminal-button-primary px-3 py-2 text-xs disabled:opacity-30"
					on:click={() => send()}
					disabled={!input.trim() || sending}
				>
					Send
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.chat-markdown :global(p) { margin: 0.25em 0; }
	.chat-markdown :global(ul), .chat-markdown :global(ol) { margin: 0.25em 0; padding-left: 1.25em; }
	.chat-markdown :global(li) { margin: 0.1em 0; }
	.chat-markdown :global(code) { background: #1a1a1a; padding: 0.1em 0.3em; border-radius: 0; font-size: 0.9em; }
	.chat-markdown :global(pre) { background: #1a1a1a; padding: 0.5em; border-radius: 0; overflow-x: auto; margin: 0.4em 0; }
	.chat-markdown :global(pre code) { background: none; padding: 0; }
	.chat-markdown :global(h1), .chat-markdown :global(h2), .chat-markdown :global(h3) { font-size: 1em; font-weight: 600; margin: 0.4em 0 0.2em; }
	.chat-markdown :global(a) { color: #fff; text-decoration: underline; }
	.chat-markdown :global(blockquote) { border-left: 2px solid #333; padding-left: 0.5em; margin: 0.3em 0; color: #999; }
	.chat-markdown :global(table) { border-collapse: collapse; margin: 0.3em 0; font-size: 0.9em; }
	.chat-markdown :global(th), .chat-markdown :global(td) { border: 1px solid #333; padding: 0.2em 0.5em; }
	.chat-markdown :global(hr) { border-color: #333; margin: 0.5em 0; }
</style>
