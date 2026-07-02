<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { importStrategyContainer, type StrategyImportResult } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { parseEnvelope, readFileAsText, type ParsedEnvelope } from '$lib/utils/strategyPortability';

	const dispatch = createEventDispatcher<{ close: void; imported: StrategyImportResult }>();

	let rawText = '';
	let parsed: ParsedEnvelope | null = null;
	let parseError = '';
	let importing = false;
	let result: StrategyImportResult | null = null;
	let dragOver = false;

	function reparse(text: string): void {
		rawText = text;
		result = null;
		if (!text.trim()) {
			parsed = null;
			parseError = '';
			return;
		}
		try {
			parsed = parseEnvelope(text);
			parseError = '';
		} catch (err) {
			parsed = null;
			parseError = err instanceof Error ? err.message : 'Could not parse export.';
		}
	}

	async function onFiles(files: FileList | null): Promise<void> {
		const file = files?.[0];
		if (!file) return;
		try {
			reparse(await readFileAsText(file));
		} catch (err) {
			parsed = null;
			parseError = err instanceof Error ? err.message : 'Could not read file.';
		}
	}

	function onDrop(event: DragEvent): void {
		event.preventDefault();
		dragOver = false;
		void onFiles(event.dataTransfer?.files ?? null);
	}

	async function runImport(): Promise<void> {
		if (!parsed || importing) return;
		importing = true;
		try {
			const res = await importStrategyContainer(parsed.envelope);
			result = res;
			if (res.ok) {
				addToast(
					`Imported as ${res.display_id || res.strategy_id} (${res.stage || 'quick_screen'})`,
					'success',
					res.strategy_id ? `/lab/strategy/${res.strategy_id}` : undefined
				);
				dispatch('imported', res);
			} else {
				addToast(res.error || 'Import rejected', 'error');
			}
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Import failed', 'error');
		} finally {
			importing = false;
		}
	}

	function close(): void {
		dispatch('close');
	}

	function autofocus(node: HTMLElement) {
		node.focus();
	}
</script>

<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
<div
	class="fixed inset-0 z-[1100] flex items-center justify-center bg-black/80 p-4"
	data-testid="strategy-import-dialog"
	role="presentation"
	on:click={(e) => {
		if (e.target === e.currentTarget) close();
	}}
	on:keydown={(e) => {
		if (e.key === 'Escape') close();
	}}
>
	<div
		class="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden border border-[#222] bg-[#050505]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="strategy-import-title"
	>
		<div class="flex items-center justify-between gap-3 border-b border-[#1a1a1a] px-4 py-3">
			<div>
				<div id="strategy-import-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">
					Import Strategy
				</div>
				<div class="mt-1 text-xs text-[#666]">Creates a new quick_screen container</div>
			</div>
			<button
				type="button"
				data-testid="strategy-import-close"
				class="border border-[#333] bg-black px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-[#888] transition-colors hover:border-[#555] hover:text-white"
				use:autofocus
				on:click={close}
			>
				Close
			</button>
		</div>

		<div class="flex-1 space-y-4 overflow-y-auto p-4">
			{#if result?.ok}
				<div class="border border-emerald-900 bg-emerald-500/5 p-4 text-sm text-emerald-300">
					<div class="font-bold">Imported successfully</div>
					<div class="mt-1 text-xs text-emerald-400/80">
						New container <span class="font-mono">{result.display_id || result.strategy_id}</span>
						· stage {result.stage || 'quick_screen'}
					</div>
					{#if result.strategy_id}
						<a
							href={`/lab/strategy/${result.strategy_id}`}
							class="mt-3 inline-block border border-emerald-900 px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-emerald-300 transition-colors hover:bg-emerald-500/10"
							on:click={close}
						>
							Open container →
						</a>
					{/if}
					{#if result.warnings?.length}
						<ul class="mt-3 list-disc space-y-1 pl-4 text-[11px] text-emerald-400/70">
							{#each result.warnings as warning}
								<li>{warning}</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else}
				<!-- Drop zone + file picker -->
				<!-- svelte-ignore a11y-no-static-element-interactions -->
				<div
					class={`border border-dashed p-4 text-center transition-colors ${
						dragOver ? 'border-[#555] bg-[#111]' : 'border-[#333] bg-[#0a0a0a]'
					}`}
					on:dragover={(e) => {
						e.preventDefault();
						dragOver = true;
					}}
					on:dragleave={() => (dragOver = false)}
					on:drop={onDrop}
				>
					<div class="text-xs text-[#888]">Drop a <span class="font-mono">.json</span> export here, or</div>
					<label
						class="mt-2 inline-block cursor-pointer border border-[#333] bg-black px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-[#888] transition-colors hover:border-[#555] hover:text-white"
					>
						Choose file
						<input
							type="file"
							accept="application/json,.json"
							class="hidden"
							data-testid="strategy-import-file"
							on:change={(e) => void onFiles((e.target as HTMLInputElement).files)}
						/>
					</label>
				</div>

				<div class="text-center text-[10px] uppercase tracking-widest text-[#666]">or paste JSON</div>

				<textarea
					data-testid="strategy-import-textarea"
					class="min-h-[160px] w-full resize-y border border-[#333] bg-black p-3 font-mono text-[11px] leading-relaxed text-[#ccc] outline-none transition-colors focus:border-white"
					spellcheck="false"
					placeholder="Paste a Forven strategy export here…"
					value={rawText}
					on:input={(e) => reparse((e.target as HTMLTextAreaElement).value)}
				></textarea>

				{#if parseError}
					<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-xs text-red-400">
						{parseError}
					</div>
				{/if}

				{#if parsed}
					<div class="border border-[#222] bg-[#050505] p-3">
						<div class="text-[10px] uppercase tracking-wider text-[#666]">Will import</div>
						<div class="mt-2 grid grid-cols-2 gap-y-1 text-xs">
							<div class="text-[#666]">Name</div>
							<div class="text-right text-white">{parsed.summary.name || '--'}</div>
							<div class="text-[#666]">Type</div>
							<div class="text-right text-white font-mono">{parsed.summary.type || '--'}</div>
							<div class="text-[#666]">Symbol / TF</div>
							<div class="text-right text-white">{parsed.summary.symbol || '--'} · {parsed.summary.timeframe || '--'}</div>
							<div class="text-[#666]">Source</div>
							<div class="text-right text-white font-mono">{parsed.meta.sourceDisplay || parsed.meta.sourceId || '--'}</div>
							<div class="text-[#666]">Snapshot</div>
							<div class="text-right text-[#888]">
								{parsed.summary.backtests} backtests · {parsed.summary.trades} trades
							</div>
							<div class="text-[#666]">Source code</div>
							<div class="text-right {parsed.summary.hasCode ? 'text-emerald-400' : 'text-[#666]'}">
								{parsed.summary.hasCode ? `bundled${parsed.summary.codeModule ? ` (${parsed.summary.codeModule})` : ''}` : 'none (param-only)'}
							</div>
						</div>
						{#if parsed.summary.hasCode}
							<div class="mt-2 border border-[#333] bg-[#111] px-2 py-1.5 text-[10px] text-[#888]">
								<span class="font-bold text-white">Runs sandboxed.</span>
								This export bundles custom code. It is security-scanned and then executed
								ONLY inside an isolated worker (secret-free, network-denied, filesystem-confined)
								— never in the main app. It imports as a sandbox-only strategy.
							</div>
						{/if}
						{#if parsed.summary.backtests > 0 || parsed.summary.trades > 0 || parsed.summary.events > 0}
							<div class="mt-2 text-[10px] text-yellow-500">
								History, trades, and events are kept for reference but not replayed — only the definition is recreated.
							</div>
						{/if}
					</div>
				{/if}

				{#if result && !result.ok}
					<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-xs text-red-400">
						{result.error || 'Import rejected'}
					</div>
				{/if}
			{/if}
		</div>

		{#if !result?.ok}
			<div class="flex items-center justify-end gap-2 border-t border-[#1a1a1a] px-4 py-3">
				<button
					type="button"
					data-testid="strategy-import-submit"
					class="border border-white bg-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest text-black transition-colors hover:bg-[#ccc] hover:border-[#ccc] disabled:opacity-40"
					disabled={!parsed || importing}
					on:click={() => void runImport()}
				>
					{importing ? 'Importing…' : parsed?.summary.hasCode ? 'Import (sandboxed)' : 'Import as new container'}
				</button>
			</div>
		{/if}
	</div>
</div>
