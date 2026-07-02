<script lang="ts">
	/**
	 * Per-agent detail drawer for the Roster tab. Edits role + instructions,
	 * the per-agent documents SOUL.md / AGENTS.md / ROLE.md (PUT
	 * /api/agents/{id}/documents/{doc}; GET via getForvenAgentDocuments), the
	 * Discord bot token, and a test-discord button.
	 *
	 * The backend is making SOUL/AGENTS per-agent; this drawer degrades
	 * gracefully if a document is empty/missing.
	 */
	import { onMount } from 'svelte';
	import {
		getForvenAgentDocuments,
		updateForvenAgent,
		updateForvenAgentDocument,
		testForvenAgentDiscord,
		type ForvenAgent,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { createEventDispatcher } from 'svelte';

	export let agent: ForvenAgent;

	const dispatch = createEventDispatcher<{ close: void; saved: ForvenAgent }>();

	type DocKind = 'soul' | 'agents' | 'role';
	const DOC_KINDS: DocKind[] = ['soul', 'agents', 'role'];

	let role = String(agent.role ?? '');
	let instructions = String(agent.instructions ?? '');
	let discordToken = '';
	let hasDiscordToken = Boolean(agent.has_discord_token);

	let docs: Record<DocKind, string> = { soul: '', agents: '', role: '' };
	let docsLoading = true;
	let docSaving: Record<DocKind, boolean> = { soul: false, agents: false, role: false };

	let savingAgent = false;
	let discordTesting = false;

	$: agentId = String(agent.id ?? '').trim();
	$: agentName = String(agent.name ?? agentId);

	async function loadDocs() {
		if (!agentId) return;
		docsLoading = true;
		try {
			const res = await getForvenAgentDocuments(agentId);
			docs = { soul: res.soul ?? '', agents: res.agents ?? '', role: res.role ?? '' };
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to load agent documents', 'error');
			docs = { soul: '', agents: '', role: '' };
		} finally {
			docsLoading = false;
		}
	}

	onMount(() => {
		void loadDocs();
	});

	async function saveAgent() {
		if (!agentId) return;
		savingAgent = true;
		try {
			const payload: Record<string, unknown> = {
				role: role.trim(),
				instructions: instructions.trimEnd(),
			};
			if (discordToken.trim()) payload.discord_token = discordToken.trim();
			const updated = await updateForvenAgent(agentId, payload);
			hasDiscordToken = Boolean(updated.has_discord_token ?? hasDiscordToken ?? discordToken.trim());
			discordToken = '';
			addToast(`${agentName} updated`, 'success');
			dispatch('saved', updated);
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to update agent', 'error');
		} finally {
			savingAgent = false;
		}
	}

	async function saveDoc(doc: DocKind) {
		if (!agentId) return;
		docSaving = { ...docSaving, [doc]: true };
		try {
			await updateForvenAgentDocument(agentId, doc, docs[doc]);
			addToast(`${doc.toUpperCase()}.md saved`, 'success');
		} catch (e) {
			addToast(e instanceof Error ? e.message : `Failed to save ${doc}`, 'error');
		} finally {
			docSaving = { ...docSaving, [doc]: false };
		}
	}

	async function testDiscord() {
		if (!agentId) return;
		discordTesting = true;
		try {
			const result = await testForvenAgentDiscord(agentId, discordToken.trim() || undefined);
			addToast(`Test sent to #${result.channel} as ${result.agent_name ?? agentId}`, 'success');
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to send agent test message', 'error');
		} finally {
			discordTesting = false;
		}
	}

	function close() {
		dispatch('close');
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') close();
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<div
	class="fixed inset-0 z-[110] flex justify-end bg-black/80"
	role="button"
	tabindex="0"
	aria-label="Close agent detail"
	on:click={close}
	on:keydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); close(); } }}
>
	<!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<div
		class="w-full max-w-xl h-full bg-[#050505] border-l border-[#222] overflow-y-auto"
		role="dialog"
		aria-modal="true"
		aria-label={`${agentName} details`}
		tabindex="-1"
		on:click|stopPropagation
	>
		<header class="sticky top-0 z-10 bg-[#050505] border-b border-[#222] px-5 py-3 flex items-center justify-between">
			<div>
				<h2 class="text-sm font-bold uppercase tracking-widest text-white">{agentName}</h2>
				<p class="mt-0.5 text-[11px] font-mono text-[#666]">{agentId}</p>
			</div>
			<button
				type="button"
				class="terminal-button-icon"
				aria-label="Close"
				on:click={close}
			>
				<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<line x1="18" y1="6" x2="6" y2="18" />
					<line x1="6" y1="6" x2="18" y2="18" />
				</svg>
			</button>
		</header>

		<div class="p-5 space-y-6">
			<!-- Role + instructions + discord -->
			<section class="space-y-3">
				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Role
					<input
						type="text"
						bind:value={role}
						class="terminal-input mt-1 w-full text-sm"
					/>
				</label>
				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Instructions
					<textarea
						rows="6"
						bind:value={instructions}
						class="terminal-input mt-1 w-full text-xs font-mono resize-y"
						placeholder="Optional system-prompt guidance"
					></textarea>
				</label>
				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Discord bot token
					{#if hasDiscordToken}
						<span class="text-[#555] normal-case tracking-normal">(saved — enter a new value to overwrite)</span>
					{/if}
					<input
						type="password"
						bind:value={discordToken}
						placeholder={hasDiscordToken ? '•••••••• (saved)' : ''}
						class="terminal-input mt-1 w-full text-sm"
					/>
				</label>
				<div class="flex gap-2">
					<button
						type="button"
						on:click={saveAgent}
						disabled={savingAgent}
						class="terminal-button-primary text-xs"
					>
						{savingAgent ? 'Saving…' : 'Save'}
					</button>
					<button
						type="button"
						on:click={testDiscord}
						disabled={discordTesting || !hasDiscordToken}
						class="terminal-button text-xs"
					>
						{discordTesting ? 'Sending…' : 'Send Discord test'}
					</button>
				</div>
			</section>

			<!-- Per-agent documents -->
			<section class="terminal-card p-4 space-y-4">
				<div>
					<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Agent docs</h3>
					<p class="text-xs text-[#666] mt-1">
						SOUL.md, AGENTS.md, and ROLE.md are saved per-agent. Restart background services if
						behavior updates need to propagate.
					</p>
				</div>

				{#each DOC_KINDS as doc (doc)}
					<div class="space-y-2">
						<div class="flex items-center justify-between">
							<span class="block text-[10px] uppercase tracking-wider text-[#666]">{doc.toUpperCase()}.md</span>
							<button
								type="button"
								on:click={() => saveDoc(doc)}
								disabled={docSaving[doc] || docsLoading}
								class="terminal-button text-[10px] px-2 py-1 disabled:opacity-60"
							>
								{docSaving[doc] ? 'Saving…' : 'Save'}
							</button>
						</div>
						{#if docsLoading}
							<p class="text-xs text-[#666]">Loading {doc.toUpperCase()}.md…</p>
						{:else}
							<textarea
								rows="8"
								bind:value={docs[doc]}
								aria-label={`${doc.toUpperCase()}.md content`}
								class="terminal-input w-full text-xs font-mono resize-y"
							></textarea>
						{/if}
					</div>
				{/each}
			</section>
		</div>
	</div>
</div>
