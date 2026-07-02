<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import {
		createHypothesisFromUrl,
		createHypothesisFromUrls,
		previewHypothesisFromUrl,
		type UrlPreviewSuccess,
		type UrlSourceResult,
	} from '$lib/api';

	export let open = false;

	const dispatch = createEventDispatcher<{
		created: { id: string };
		createdBulk: { ids: string[] };
		close: void;
	}>();

	type Step = 'input' | 'preview' | 'submitting' | 'done';
	let step: Step = 'input';

	let urlsRaw = '';

	// Single-URL rich path — set only when exactly one URL is pasted and it
	// previews OK. Keeps the original "edit metadata then create one" experience.
	let preview: UrlPreviewSuccess | null = null;
	let title = '';
	let marketThesis = '';
	let mechanism = '';
	let claimedEdge = '';

	// Multi-URL path: one row per pasted URL.
	interface PreviewRow {
		raw: string;
		ok: boolean;
		sourceType?: string | null;
		title?: string;
		canonicalUrl?: string;
		contentBytes?: number;
		errorMsg?: string;
	}
	let rows: PreviewRow[] = [];

	interface CreateResult {
		raw: string;
		ok: boolean;
		id?: string;
		errorMsg?: string;
	}
	let createResults: CreateResult[] = [];
	let createProgress: { done: number; total: number } | null = null;

	// Combine mode: merge all pasted URLs into ONE crucible with every source
	// attached, instead of one crucible per URL.
	let combineMode = false;
	let combinedResult: { id: string; sources: UrlSourceResult[] } | null = null;

	let errorMsg: string | null = null;
	let previewing = false;

	$: parsedCount = parseUrls(urlsRaw).length;
	$: canPreview = parsedCount > 0 && !previewing && step !== 'submitting';
	$: okRows = rows.filter((r) => r.ok);

	function parseUrls(raw: string): string[] {
		const seen = new Set<string>();
		const out: string[] = [];
		for (const line of raw.split(/\r?\n/)) {
			const u = line.trim();
			if (!u || seen.has(u)) continue;
			seen.add(u);
			out.push(u);
		}
		return out;
	}

	function resetState(): void {
		step = 'input';
		urlsRaw = '';
		preview = null;
		title = '';
		marketThesis = '';
		mechanism = '';
		claimedEdge = '';
		rows = [];
		createResults = [];
		createProgress = null;
		combineMode = false;
		combinedResult = null;
		errorMsg = null;
		previewing = false;
	}

	function close(): void {
		resetState();
		dispatch('close');
	}

	function backToInput(): void {
		step = 'input';
		preview = null;
		rows = [];
		createResults = [];
		createProgress = null;
		combineMode = false;
		combinedResult = null;
		errorMsg = null;
	}

	function removeRow(raw: string): void {
		rows = rows.filter((r) => r.raw !== raw);
		if (rows.length === 0) backToInput();
	}

	// Run an async fn over items with bounded concurrency, preserving order. The
	// fn must not throw (callers catch internally) so one bad item never aborts
	// the batch.
	async function mapLimit<T, R>(
		items: T[],
		limit: number,
		fn: (item: T, index: number) => Promise<R>,
	): Promise<R[]> {
		const out: R[] = new Array(items.length);
		let cursor = 0;
		async function worker(): Promise<void> {
			while (cursor < items.length) {
				const i = cursor++;
				out[i] = await fn(items[i], i);
			}
		}
		await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
		return out;
	}

	async function handlePreview(): Promise<void> {
		errorMsg = null;
		const urls = parseUrls(urlsRaw);
		if (urls.length === 0) {
			errorMsg = 'Enter at least one URL.';
			return;
		}
		previewing = true;
		preview = null;
		rows = [];
		try {
			if (urls.length === 1) {
				// Single-URL rich path — identical to the original behavior.
				const res = await previewHypothesisFromUrl(urls[0]);
				if (!res.ok) {
					errorMsg = `${res.error_code}: ${res.error}`;
					return;
				}
				preview = res;
				title = res.title || '';
				step = 'preview';
				return;
			}

			// Multi-URL: preview each (throttled — every preview is a network +
			// extraction call against external providers).
			rows = await mapLimit(urls, 4, async (u): Promise<PreviewRow> => {
				try {
					const res = await previewHypothesisFromUrl(u);
					if (res.ok) {
						return {
							raw: u,
							ok: true,
							sourceType: res.source_type,
							title: res.title,
							canonicalUrl: res.url,
							contentBytes: res.content_bytes,
						};
					}
					return {
						raw: u,
						ok: false,
						sourceType: res.source_type ?? null,
						errorMsg: `${res.error_code}: ${res.error}`,
					};
				} catch (err) {
					return {
						raw: u,
						ok: false,
						errorMsg: err instanceof Error ? err.message : 'Preview failed.',
					};
				}
			});
			step = 'preview';
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : 'Preview failed.';
		} finally {
			previewing = false;
		}
	}

	async function handleCreate(): Promise<void> {
		if (preview) {
			// Single-URL rich path — original behavior (create one, then navigate).
			errorMsg = null;
			step = 'submitting';
			try {
				const res = await createHypothesisFromUrl({
					url: preview.url,
					title: title.trim() || undefined,
					market_thesis: marketThesis.trim() || undefined,
					mechanism: mechanism.trim() || undefined,
					claimed_edge: claimedEdge.trim() || undefined,
				});
				if (!res.ok) {
					errorMsg = `${res.error_code}: ${res.error}`;
					step = 'preview';
					return;
				}
				dispatch('created', { id: res.hypothesis.id });
				close();
			} catch (err) {
				errorMsg = err instanceof Error ? err.message : 'Create failed.';
				step = 'preview';
			}
			return;
		}

		const targets = rows.filter((r) => r.ok && r.canonicalUrl);
		if (targets.length === 0) return;
		errorMsg = null;

		if (combineMode) {
			// Merge all ready sources into a single crucible (one backend call).
			createResults = [];
			createProgress = null;
			combinedResult = null;
			step = 'submitting';
			try {
				const res = await createHypothesisFromUrls({
					urls: targets.map((r) => r.canonicalUrl as string),
				});
				if (!res.ok) {
					errorMsg = `${res.error_code}: ${res.error}`;
					step = 'preview';
					return;
				}
				combinedResult = { id: res.hypothesis.id, sources: res.sources };
				dispatch('createdBulk', { ids: [res.hypothesis.id] });
				step = 'done';
			} catch (err) {
				errorMsg = err instanceof Error ? err.message : 'Create failed.';
				step = 'preview';
			}
			return;
		}

		// Multi-URL: create each ready row sequentially so partial failures are
		// independent and the active-pool cap is respected one item at a time.
		createResults = [];
		createProgress = { done: 0, total: targets.length };
		step = 'submitting';
		for (const row of targets) {
			let result: CreateResult;
			try {
				const res = await createHypothesisFromUrl({ url: row.canonicalUrl as string });
				result = res.ok
					? { raw: row.raw, ok: true, id: res.hypothesis.id }
					: { raw: row.raw, ok: false, errorMsg: `${res.error_code}: ${res.error}` };
			} catch (err) {
				result = {
					raw: row.raw,
					ok: false,
					errorMsg: err instanceof Error ? err.message : 'Create failed.',
				};
			}
			createResults = [...createResults, result];
			createProgress = { done: createResults.length, total: targets.length };
		}
		const createdIds = createResults.filter((r) => r.ok && r.id).map((r) => r.id as string);
		if (createdIds.length > 0) dispatch('createdBulk', { ids: createdIds });
		createProgress = null;
		step = 'done';
	}

	function onBackdropClick(event: MouseEvent): void {
		// Don't tear down state while a batch create loop is mid-flight.
		if (step === 'submitting') return;
		if (event.target === event.currentTarget) close();
	}
</script>

{#if open}
	<div
		class="fixed inset-0 z-50 flex items-start justify-center bg-black/80 px-4 py-10"
		on:click={onBackdropClick}
		on:keydown={(e) => e.key === 'Escape' && step !== 'submitting' && close()}
		role="presentation"
	>
		<div
			class="w-full max-w-2xl border border-[#222] bg-[#050505] text-white"
			role="dialog"
			aria-modal="true"
			aria-labelledby="url-ingest-title"
		>
			<header class="flex items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
				<h2 id="url-ingest-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">
					Add crucible from URL
				</h2>
				<button
					type="button"
					class="text-[#666] hover:text-white disabled:opacity-40"
					aria-label="Close"
					disabled={step === 'submitting'}
					on:click={() => step !== 'submitting' && close()}
				>
					✕
				</button>
			</header>

			<div class="space-y-4 px-5 py-5">
				{#if errorMsg}
					<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-xs text-red-400">
						{errorMsg}
					</div>
				{/if}

				{#if step === 'input'}
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						URL(s)
						<textarea
							bind:value={urlsRaw}
							rows="4"
							placeholder={'https://youtube.com/... or reddit/github/blog URL\nPaste several — one URL per line'}
							class="terminal-input mt-2 w-full resize-y font-mono"
							autocomplete="off"
						></textarea>
					</label>
					<p class="text-[11px] text-[#555]">
						We auto-detect YouTube, Reddit, GitHub, known forums, or fall back to article
						extraction. Add several at once — one URL per line.
					</p>
					{#if parsedCount > 1}
						<p class="text-[11px] text-[#888]">{parsedCount} URLs detected.</p>
					{/if}
					<div class="flex justify-end gap-2 pt-2">
						<button
							type="button"
							class="terminal-button text-xs"
							on:click={close}
						>
							Cancel
						</button>
						<button
							type="button"
							class="terminal-button-primary text-xs disabled:opacity-50"
							on:click={handlePreview}
							disabled={!canPreview}
						>
							{previewing ? 'Fetching…' : parsedCount > 1 ? `Preview ${parsedCount}` : 'Preview'}
						</button>
					</div>
				{/if}

				{#if step === 'preview' && preview}
					<div class="grid grid-cols-3 gap-3 text-[11px] text-[#666]">
						<div class="border border-[#222] bg-[#111] px-3 py-2">
							<div class="uppercase tracking-wider text-[#666]">Source</div>
							<div class="mt-1 text-sm font-semibold text-white">{preview.source_type}</div>
						</div>
						<div class="border border-[#222] bg-[#111] px-3 py-2">
							<div class="uppercase tracking-wider text-[#666]">Extracted bytes</div>
							<div class="mt-1 text-sm font-semibold text-white">{preview.content_bytes.toLocaleString()}</div>
						</div>
						<div class="border border-[#222] bg-[#111] px-3 py-2">
							<div class="uppercase tracking-wider text-[#666]">Preview clipped?</div>
							<div class="mt-1 text-sm font-semibold text-white">{preview.preview_truncated ? 'Yes' : 'No'}</div>
						</div>
					</div>

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Title
						<input
							bind:value={title}
							placeholder="Auto-detected from source; you can override"
							class="terminal-input mt-2 w-full"
						/>
					</label>

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Market thesis (optional)
						<textarea
							bind:value={marketThesis}
							rows="2"
							placeholder="One-line thesis the agent should refine, or leave blank."
							class="terminal-input mt-2 w-full"
						></textarea>
					</label>

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Mechanism (optional)
						<textarea
							bind:value={mechanism}
							rows="2"
							placeholder="How the edge is captured. Leave blank to let the agent populate."
							class="terminal-input mt-2 w-full"
						></textarea>
					</label>

					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Claimed edge (optional)
						<input
							bind:value={claimedEdge}
							placeholder="e.g., funding-rate mean reversion on BTC perps"
							class="terminal-input mt-2 w-full"
						/>
					</label>

					<details class="text-xs text-[#666]">
						<summary class="cursor-pointer select-none py-1 text-[#666] hover:text-white">
							Content preview ({Math.min(preview.content_preview.length, preview.content_bytes).toLocaleString()} chars)
						</summary>
						<pre class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap border border-[#222] bg-black p-3 text-[11px] text-[#888]">{preview.content_preview || '(no content extracted)'}</pre>
					</details>

					<div class="flex justify-end gap-2 pt-2">
						<button
							type="button"
							class="terminal-button text-xs"
							on:click={backToInput}
						>
							Back
						</button>
						<button
							type="button"
							class="terminal-button-primary text-xs"
							on:click={handleCreate}
						>
							Create crucible
						</button>
					</div>
				{/if}

				{#if step === 'preview' && !preview}
					<div class="text-[11px] uppercase tracking-wider text-[#666]">
						{okRows.length} of {rows.length} ready{rows.length - okRows.length > 0
							? ` · ${rows.length - okRows.length} failed`
							: ''}
					</div>
					<div class="max-h-80 space-y-2 overflow-auto pr-1">
						{#each rows as row (row.raw)}
							<div
								class="flex items-start gap-3 border px-3 py-2 {row.ok
									? 'border-[#262626] bg-[#111]'
									: 'border-red-900 bg-red-500/5'}"
							>
								<div class="min-w-0 flex-1">
									<div class="truncate text-sm text-white">{row.title || row.raw}</div>
									<div class="truncate text-[11px] text-[#555]">{row.raw}</div>
									{#if row.ok}
										<div class="mt-1 text-[11px] text-emerald-400">
											{row.sourceType} · {(row.contentBytes ?? 0).toLocaleString()} bytes
										</div>
									{:else}
										<div class="mt-1 text-[11px] text-red-400">{row.errorMsg}</div>
									{/if}
								</div>
								<button
									type="button"
									class="shrink-0 text-[#555] hover:text-white"
									aria-label="Remove {row.raw}"
									on:click={() => removeRow(row.raw)}
								>
									✕
								</button>
							</div>
						{/each}
					</div>
					<label class="flex items-start gap-2 border border-[#222] bg-[#111] px-3 py-2 text-[11px] text-[#888]">
						<input type="checkbox" bind:checked={combineMode} class="mt-0.5 accent-white" />
						<span>
							<span class="text-[#888]">Combine into a single crucible.</span>
							All sources are attached to one crucible (use this when the URLs cover the
							same topic). Leave unchecked to create one crucible per URL.
						</span>
					</label>
					<p class="text-[11px] text-[#555]">
						Titles and theses are filled in automatically by the research agent.
					</p>
					<div class="flex justify-end gap-2 pt-2">
						<button
							type="button"
							class="terminal-button text-xs"
							on:click={backToInput}
						>
							Back
						</button>
						<button
							type="button"
							class="terminal-button-primary text-xs disabled:opacity-50"
							on:click={handleCreate}
							disabled={okRows.length === 0}
						>
							{#if combineMode}
								Create 1 crucible from {okRows.length} source{okRows.length === 1 ? '' : 's'}
							{:else}
								Create {okRows.length} crucible{okRows.length === 1 ? '' : 's'}
							{/if}
						</button>
					</div>
				{/if}

				{#if step === 'submitting'}
					<div class="flex items-center justify-center py-6 text-xs uppercase tracking-wider text-[#666]">
						{#if createProgress}
							Creating {createProgress.done}/{createProgress.total}…
						{:else}
							Creating crucible…
						{/if}
					</div>
				{/if}

				{#if step === 'done' && combinedResult}
					{@const okSources = combinedResult.sources.filter((s) => s.ok)}
					{@const failedSources = combinedResult.sources.filter((s) => !s.ok)}
					<div class="text-sm text-white">
						Created 1 crucible from {okSources.length} source{okSources.length === 1 ? '' : 's'}.
					</div>
					{#if failedSources.length > 0}
						<div class="text-[11px] uppercase tracking-[0.18em] text-red-400">Skipped sources</div>
						<div class="max-h-48 space-y-1 overflow-auto pr-1">
							{#each failedSources as f (f.url)}
								<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px]">
									<div class="truncate text-[#888]">{f.url}</div>
									<div class="text-red-400">{f.error_code}: {f.error}</div>
								</div>
							{/each}
						</div>
					{/if}
					<div class="flex justify-end gap-2 pt-2">
						<button
							type="button"
							class="terminal-button text-xs"
							on:click={close}
						>
							Done
						</button>
						<button
							type="button"
							class="terminal-button-primary text-xs"
							on:click={() => {
								if (combinedResult) dispatch('created', { id: combinedResult.id });
								close();
							}}
						>
							Open crucible
						</button>
					</div>
				{/if}

				{#if step === 'done' && !combinedResult}
					{@const created = createResults.filter((r) => r.ok)}
					{@const failed = createResults.filter((r) => !r.ok)}
					<div class="text-sm text-white">
						Created {created.length} of {createResults.length} crucible{createResults.length === 1
							? ''
							: 's'}.
					</div>
					{#if failed.length > 0}
						<div class="text-[11px] uppercase tracking-[0.18em] text-red-400">Failed</div>
						<div class="max-h-48 space-y-1 overflow-auto pr-1">
							{#each failed as f (f.raw)}
								<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px]">
									<div class="truncate text-[#888]">{f.raw}</div>
									<div class="text-red-400">{f.errorMsg}</div>
								</div>
							{/each}
						</div>
					{/if}
					<div class="flex justify-end gap-2 pt-2">
						<button
							type="button"
							class="terminal-button text-xs"
							on:click={backToInput}
						>
							Add more
						</button>
						<button
							type="button"
							class="terminal-button-primary text-xs"
							on:click={close}
						>
							Done
						</button>
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}
