<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { createHypothesisManual } from '$lib/api';

	export let open = false;

	const dispatch = createEventDispatcher<{ created: { id: string }; close: void }>();

	let submitting = false;
	let title = '';
	let marketThesis = '';
	let mechanism = '';
	let whyNow = '';
	let targetAssetsRaw = '';
	let targetTimeframesRaw = '';
	let noveltyScoreRaw = '';
	let claimedEdge = '';
	let operatorNotes = '';
	let errorMsg: string | null = null;

	$: canSubmit =
		!submitting &&
		title.trim().length > 0 &&
		marketThesis.trim().length > 0 &&
		mechanism.trim().length > 0;

	function close(): void {
		submitting = false;
		title = '';
		marketThesis = '';
		mechanism = '';
		whyNow = '';
		targetAssetsRaw = '';
		targetTimeframesRaw = '';
		noveltyScoreRaw = '';
		claimedEdge = '';
		operatorNotes = '';
		errorMsg = null;
		dispatch('close');
	}

	function splitCsv(raw: string): string[] {
		return raw
			.split(/[,\n]/)
			.map((s) => s.trim())
			.filter((s) => s.length > 0);
	}

	async function handleCreate(): Promise<void> {
		errorMsg = null;
		submitting = true;
		try {
			const assets = splitCsv(targetAssetsRaw);
			const timeframes = splitCsv(targetTimeframesRaw);
			let novelty: number | undefined;
			if (noveltyScoreRaw.trim().length > 0) {
				const parsed = Number(noveltyScoreRaw);
				if (!Number.isFinite(parsed)) {
					errorMsg = 'Novelty score must be a number between 0 and 1.';
					submitting = false;
					return;
				}
				novelty = parsed;
			}

			const res = await createHypothesisManual({
				title: title.trim(),
				market_thesis: marketThesis.trim(),
				mechanism: mechanism.trim(),
				why_now: whyNow.trim() || undefined,
				target_assets: assets.length ? assets : undefined,
				target_timeframes: timeframes.length ? timeframes : undefined,
				novelty_score: novelty,
				claimed_edge: claimedEdge.trim() || undefined,
				operator_notes: operatorNotes.trim() || undefined,
			});
			dispatch('created', { id: res.hypothesis.id });
			close();
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : 'Create failed.';
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
		class="fixed inset-0 z-50 flex items-start justify-center bg-black/80 px-4 py-10"
		on:click={onBackdropClick}
		on:keydown={(e) => e.key === 'Escape' && close()}
		role="presentation"
	>
		<div
			class="w-full max-w-2xl border border-[#222] bg-[#050505] text-white"
			role="dialog"
			aria-modal="true"
			aria-labelledby="manual-ingest-title"
		>
			<header class="flex items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
				<h2 id="manual-ingest-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">
					Create crucible manually
				</h2>
				<button
					type="button"
					class="text-[#666] hover:text-white"
					aria-label="Close"
					on:click={close}
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

				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Title <span class="text-red-500">*</span>
					<input
						bind:value={title}
						placeholder="Short, specific title"
						class="terminal-input mt-2 w-full"
					/>
				</label>

				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Market thesis <span class="text-red-500">*</span>
					<textarea
						bind:value={marketThesis}
						rows="3"
						placeholder="What you believe is true about the market."
						class="terminal-input mt-2 w-full"
					></textarea>
				</label>

				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Mechanism <span class="text-red-500">*</span>
					<textarea
						bind:value={mechanism}
						rows="3"
						placeholder="How the edge is actually captured — entries, exits, filters."
						class="terminal-input mt-2 w-full"
					></textarea>
				</label>

				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Why now (optional)
					<textarea
						bind:value={whyNow}
						rows="2"
						placeholder="Regime, catalyst, or timing rationale."
						class="terminal-input mt-2 w-full"
					></textarea>
				</label>

				<div class="grid grid-cols-2 gap-3">
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Target assets
						<input
							bind:value={targetAssetsRaw}
							placeholder="BTC, ETH, SOL"
							class="terminal-input mt-2 w-full"
						/>
					</label>
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Target timeframes
						<input
							bind:value={targetTimeframesRaw}
							placeholder="15m, 1h, 4h"
							class="terminal-input mt-2 w-full"
						/>
					</label>
				</div>

				<div class="grid grid-cols-2 gap-3">
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Novelty score (0–1)
						<input
							bind:value={noveltyScoreRaw}
							inputmode="decimal"
							placeholder="0.5"
							class="terminal-input mt-2 w-full"
						/>
					</label>
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Claimed edge (optional)
						<input
							bind:value={claimedEdge}
							placeholder="e.g., funding-rate mean reversion"
							class="terminal-input mt-2 w-full"
						/>
					</label>
				</div>

				<label class="block text-[10px] uppercase tracking-wider text-[#666]">
					Operator notes (optional)
					<textarea
						bind:value={operatorNotes}
						rows="2"
						placeholder="Anything else the agent should know."
						class="terminal-input mt-2 w-full"
					></textarea>
				</label>

				<p class="text-[11px] text-[#555]">
					A research task will be queued to surface data gaps and spawn 1–3 candidate strategies.
				</p>

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
						on:click={handleCreate}
						disabled={!canSubmit}
					>
						{submitting ? 'Creating…' : 'Create crucible'}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}
