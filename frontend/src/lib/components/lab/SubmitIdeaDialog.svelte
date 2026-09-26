<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { dialogFocus } from '$lib/utils/dialogFocus';
	import { getSymbols } from '$lib/api/data';
	import { previewIdeaUrl, submitIdea, type IdeaUrlPreview } from '$lib/api/ideas';

	export let open = false;

	const dispatch = createEventDispatcher<{ submitted: { taskId: number | null }; close: void }>();

	let idea = '';
	let url = '';
	let marketsRaw = '';
	let timeframesRaw = '';
	let notes = '';
	let submitting = false;
	let previewing = false;
	let preview: IdeaUrlPreview | null = null;
	let errorMsg: string | null = null;
	let queuedTaskId: number | null | undefined = undefined;
	let marketOptions: string[] = [];
	let scopeLoaded = false;
	$: if (open && !scopeLoaded) {
		scopeLoaded = true;
		void getSymbols()
			.then((values) => (marketOptions = values))
			.catch(() => {});
	}

	$: canSubmit = !submitting && (idea.trim().length > 0 || url.trim().length > 0);

	function splitList(raw: string): string[] {
		return raw
			.split(/[,\n]/)
			.map((item) => item.trim())
			.filter((item) => item.length > 0);
	}

	function reset(): void {
		idea = '';
		url = '';
		marketsRaw = '';
		timeframesRaw = '';
		notes = '';
		preview = null;
		errorMsg = null;
		queuedTaskId = undefined;
	}

	function close(): void {
		if (submitting) return;
		reset();
		dispatch('close');
	}

	async function handlePreview(): Promise<void> {
		if (!url.trim() || previewing) return;
		previewing = true;
		errorMsg = null;
		try {
			preview = await previewIdeaUrl(url.trim());
		} catch (err) {
			preview = { ok: false, error: err instanceof Error ? err.message : 'Preview failed.' };
		} finally {
			previewing = false;
		}
	}

	async function handleSubmit(): Promise<void> {
		if (!canSubmit) return;
		submitting = true;
		errorMsg = null;
		try {
			const markets = splitList(marketsRaw);
			const timeframes = splitList(timeframesRaw);
			const res = await submitIdea({
				text: idea.trim() || undefined,
				url: url.trim() || undefined,
				target_assets: markets.length ? markets : undefined,
				target_timeframes: timeframes.length ? timeframes : undefined,
				notes: notes.trim() || undefined,
			});
			if (res.ok) {
				queuedTaskId = res.task_id;
				dispatch('submitted', { taskId: res.task_id });
			} else {
				errorMsg = res.error;
			}
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : 'Submit failed.';
		} finally {
			submitting = false;
		}
	}

	function onBackdropClick(event: MouseEvent): void {
		if (event.target === event.currentTarget) close();
	}
</script>

{#if open}
	<div
		class="fixed inset-0 z-50 flex items-start justify-center bg-black/80 px-4 py-4 sm:py-6"
		on:click={onBackdropClick}
		on:keydown={(e) => e.key === 'Escape' && close()}
		role="presentation"
	>
		<div
			use:dialogFocus
			class="flex max-h-[calc(100dvh-3rem)] w-full max-w-2xl flex-col border border-[#222] bg-[#050505] text-white"
			role="dialog"
			aria-modal="true"
			aria-labelledby="submit-idea-title"
		>
			<header class="flex shrink-0 items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
				<h2 id="submit-idea-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">
					Submit an idea
				</h2>
				<button
					type="button"
					class="text-[#666] hover:text-white"
					aria-label="Close"
					disabled={submitting}
					on:click={close}
				>
					✕
				</button>
			</header>

			{#if queuedTaskId !== undefined}
				<div class="space-y-3 px-5 py-5 text-sm text-[#ccc]">
					<p>
						Queued{queuedTaskId ? ` as task T${queuedTaskId}` : ''}. The strategy-developer will write the idea
						up and build strategies from it; they appear in The Forge as they're registered.
					</p>
					<div class="flex justify-end gap-2">
						<button type="button" class="terminal-button text-xs" on:click={reset}>Submit another</button>
						<button type="button" class="terminal-button-primary text-xs" on:click={close}>Done</button>
					</div>
				</div>
			{:else}
				<div class="min-h-0 space-y-4 overflow-y-auto px-5 py-5">
					{#if errorMsg}
						<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-xs text-red-400">{errorMsg}</div>
					{/if}

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Idea
						<textarea
							bind:value={idea}
							rows="5"
							placeholder="What the market does, and why you think it's tradable."
							class="terminal-input mt-2 w-full"
						></textarea>
					</label>

					<div>
						<label class="block text-[10px] uppercase tracking-wider text-[#666]">
							Source URL (optional)
							<div class="mt-2 flex gap-2">
								<input
									bind:value={url}
									placeholder="Article, post, video or repo"
									class="terminal-input w-full"
									on:input={() => (preview = null)}
								/>
								<button
									type="button"
									class="terminal-button shrink-0 text-xs"
									disabled={!url.trim() || previewing}
									on:click={handlePreview}
								>
									{previewing ? 'Reading…' : 'Preview'}
								</button>
							</div>
						</label>
						{#if preview}
							{#if preview.ok}
								<div class="mt-2 border border-[#222] p-3 text-xs text-[#aaa]">
									<div class="font-bold text-white">{preview.title || preview.url}</div>
									<p class="mt-1 line-clamp-4 whitespace-pre-line">{preview.content_preview}</p>
								</div>
							{:else}
								<div class="mt-2 text-xs text-red-400">{preview.error}</div>
							{/if}
						{/if}
					</div>

					<datalist id="idea-timeframe-options">
						{#each ['1m', '5m', '15m', '30m', '1h', '4h', '1d'] as tf}<option value={tf}></option>{/each}
					</datalist>
					<datalist id="idea-market-options">
						{#each marketOptions as market}<option value={market}></option>{/each}
					</datalist>
					<div class="grid grid-cols-2 gap-3">
						<label class="block text-[10px] uppercase tracking-wider text-[#666]">
							Markets (optional)
							<input
								bind:value={marketsRaw}
								placeholder="BTC/USDT, ETH/USDT"
								list="idea-market-options"
								class="terminal-input mt-2 w-full"
							/>
						</label>
						<label class="block text-[10px] uppercase tracking-wider text-[#666]">
							Timeframes (optional)
							<input
								bind:value={timeframesRaw}
								placeholder="1h, 4h"
								list="idea-timeframe-options"
								class="terminal-input mt-2 w-full"
							/>
						</label>
					</div>

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Notes (optional)
						<textarea
							bind:value={notes}
							rows="2"
							placeholder="Anything else the agent should know."
							class="terminal-input mt-2 w-full"
						></textarea>
					</label>

					<p class="text-[11px] text-[#555]">
						The strategy-developer writes the idea up (what it exploits, why, and what would prove it wrong) and
						builds strategies from it. This runs in every system mode.
					</p>
				</div>
				<div class="flex shrink-0 justify-end gap-2 border-t border-[#222] bg-[#050505] px-5 py-3">
					<button type="button" class="terminal-button text-xs" disabled={submitting} on:click={close}>Cancel</button>
					<button
						type="button"
						class="terminal-button-primary text-xs disabled:opacity-50"
						on:click={handleSubmit}
						disabled={!canSubmit}
					>
						{submitting ? 'Submitting…' : 'Submit idea'}
					</button>
				</div>
			{/if}
		</div>
	</div>
{/if}
