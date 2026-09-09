<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { dialogFocus, dispatchNotice, intakeKeys } from '$lib/utils/crucibleIntake';
	import { getSymbols } from '$lib/api/data';
	import { createHypothesisManual } from '$lib/api';

	export let open = false;

	const dispatch = createEventDispatcher<{ created: { id: string; intake?: string }; close: void }>();

	let submitting = false;
	let requestKey = intakeKeys();
	let title = '';
	let marketThesis = '';
	let mechanism = '';
	let whyNow = '';
	let targetAssetsRaw = '';
	let marketOptions: string[] = [];
	let scopeLoaded = false;
	$: if (open && !scopeLoaded) { scopeLoaded = true; void getSymbols().then(values => marketOptions = values).catch(() => {}); }
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
		if (submitting) return;
		requestKey = intakeKeys();
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
		if (!canSubmit) return;
		errorMsg = null;
		submitting = true;
		try {
			const assets = splitCsv(targetAssetsRaw);
			const timeframes = splitCsv(targetTimeframesRaw);
			let novelty: number | undefined;
			if (noveltyScoreRaw.trim().length > 0) {
				const parsed = Number(noveltyScoreRaw);
				if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
					errorMsg = 'Novelty score must be a number between 0 and 1.';
					submitting = false;
					return;
				}
				novelty = parsed;
			}

			const body = {
				title: title.trim(),
				market_thesis: marketThesis.trim(),
				mechanism: mechanism.trim(),
				why_now: whyNow.trim() || undefined,
				target_assets: assets.length ? assets : undefined,
				target_timeframes: timeframes.length ? timeframes : undefined,
				novelty_score: novelty,
				claimed_edge: claimedEdge.trim() || undefined,
				operator_notes: operatorNotes.trim() || undefined,
			};
			const res = await createHypothesisManual({...body,request_id:requestKey(body)});
			submitting = false;
			dispatch('created', { id: res.hypothesis.id, intake: dispatchNotice(res).state });
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
			aria-labelledby="manual-ingest-title"
		>
			<header class="flex shrink-0 items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
				<h2 id="manual-ingest-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">
					Create crucible manually
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

			<div class="min-h-0 space-y-4 overflow-y-auto px-5 py-5">
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

				<datalist id="crucible-timeframe-options">{#each ['1m','5m','15m','30m','1h','4h','1d'] as tf}<option value={tf}></option>{/each}</datalist>
				<datalist id="crucible-market-options">{#each marketOptions as market}<option value={market}></option>{/each}</datalist>
				<p class="text-xs text-[#aaa]">Select collected markets where possible. Leave scope blank for research to resolve it; put holding periods and reference data in the mechanism.</p>
				<div class="grid grid-cols-2 gap-3">
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Target assets
						<input
							bind:value={targetAssetsRaw}
							placeholder="BTC/USDT, ETH/USDT"
							list="crucible-market-options"
							class="terminal-input mt-2 w-full"
						/>
					</label>
					<label class="block text-[10px] uppercase tracking-wider text-[#666]">
						Target timeframes
						<input
							bind:value={targetTimeframesRaw}
							placeholder="15m, 1h, 4h"
							list="crucible-timeframe-options"
							class="terminal-input mt-2 w-full"
						/>
					</label>
				</div>

				<details class="border border-[#222] p-3"><summary class="cursor-pointer text-xs text-[#aaa]">Optional research metadata</summary>
				<div class="mt-3 grid grid-cols-2 gap-3">
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

				</details>
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
					Save an idea for research. Automatic modes queue research; manual mode waits for you. Candidate development follows after the idea and its inputs are ready.
				</p>

				</div>
			<div class="flex shrink-0 justify-end gap-2 border-t border-[#222] bg-[#050505] px-5 py-3">
					<button
						type="button"
						class="terminal-button text-xs"
						disabled={submitting}
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
{/if}
