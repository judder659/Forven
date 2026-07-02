<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { promoteForvenStrategy } from '$lib/api/forven';
	import { lifecycleStageLabel } from '$lib/utils/lifecyclePresentation';
	import { addToast } from '$lib/stores/processTracker';

	export let strategyId: string;
	/** Normalized current lifecycle stage (quick_screen | gauntlet | paper | live_graduated | archived | rejected | ...). */
	export let currentStage: string = '';
	/** Ordered pipeline stages; targets beyond these (archive/revive) are added automatically. */
	export let pipelineStages: ReadonlyArray<{ key: string; label: string }> = [];

	const dispatch = createEventDispatcher<{ changed: { toStage: string } }>();

	let open = false;
	let targetStage: string | null = null;
	let reason = '';
	let submitting = false;
	// Populated when the promotion gate rejects the transition; enables the
	// informed operator override path (mirrors the old Configuration-tab flow).
	let blockReason = '';
	// GO-LIVE-1: promoting into live requires typing the confirmation phrase and
	// setting an initial per-asset notional ceiling (USD). The backend refuses a
	// live promotion without both, so the UI collects them up front.
	let goLiveConfirmText = '';
	let goLiveCeilingUsd: number | null = null;

	const TERMINAL = new Set(['archived', 'rejected']);
	const GO_LIVE_PHRASE = 'GO LIVE';

	type StageOption = { key: string; label: string; kind: 'forward' | 'backward' | 'terminal' | 'revive' };

	$: currentIndex = pipelineStages.findIndex((stage) => stage.key === currentStage);
	$: stageOptions = buildOptions(currentStage, currentIndex, pipelineStages);
	$: targetOption = stageOptions.find((option) => option.key === targetStage) ?? null;
	$: isCapitalTarget = targetStage === 'paper' || targetStage === 'live_graduated';
	$: isLiveTarget = targetStage === 'live_graduated';
	$: goLiveArmed =
		!isLiveTarget ||
		(goLiveConfirmText.trim().toUpperCase() === GO_LIVE_PHRASE && (goLiveCeilingUsd ?? 0) > 0);

	function buildOptions(
		stage: string,
		index: number,
		stages: ReadonlyArray<{ key: string; label: string }>,
	): StageOption[] {
		const options: StageOption[] = [];
		if (TERMINAL.has(stage)) {
			options.push({ key: 'quick_screen', label: 'Quick Screen (revive)', kind: 'revive' });
			return options;
		}
		stages.forEach((candidate, candidateIndex) => {
			if (candidate.key === stage) return;
			options.push({
				key: candidate.key,
				label: candidate.label,
				kind: candidateIndex > index ? 'forward' : 'backward',
			});
		});
		options.push({ key: 'archived', label: 'Archived', kind: 'terminal' });
		return options;
	}

	function stageTone(stage: string): string {
		switch (stage) {
			case 'live_graduated':
				return 'border-emerald-900 bg-emerald-500/10 text-emerald-400 hover:border-emerald-700';
			case 'paper':
				return 'border-[#333] bg-black text-white hover:border-[#555]';
			case 'gauntlet':
				return 'border-[#333] bg-black text-[#888] hover:border-[#555]';
			case 'archived':
			case 'rejected':
				return 'border-red-900 bg-red-500/10 text-red-400 hover:border-red-700';
			default:
				return 'border-[#333] bg-black text-[#888] hover:border-[#555]';
		}
	}

	function optionTone(kind: StageOption['kind']): string {
		switch (kind) {
			case 'forward':
				return 'text-white hover:bg-[#111]';
			case 'backward':
				return 'text-yellow-400 hover:bg-[#111]';
			case 'terminal':
				return 'text-red-400 hover:bg-[#111]';
			case 'revive':
				return 'text-emerald-400 hover:bg-[#111]';
		}
	}

	function toggleOpen(): void {
		open = !open;
		if (!open) resetSelection();
	}

	function resetSelection(): void {
		targetStage = null;
		reason = '';
		blockReason = '';
		goLiveConfirmText = '';
		goLiveCeilingUsd = null;
	}

	function selectTarget(key: string): void {
		targetStage = key;
		reason = '';
		blockReason = '';
		goLiveConfirmText = '';
		goLiveCeilingUsd = null;
	}

	/** Open the panel pre-set to a target stage (used by "Run check" on the gauntlet card). */
	export function openFor(stage: string): void {
		open = true;
		selectTarget(stage);
	}

	async function confirm(override = false): Promise<void> {
		if (!targetStage || submitting || !goLiveArmed) return;
		const target = targetStage;
		submitting = true;
		try {
			await promoteForvenStrategy(strategyId, target, {
				fromStatus: currentStage || undefined,
				reason:
					reason.trim() ||
					(override ? 'Operator gate override' : `Manual stage change from strategy container`),
				force: true,
				override,
				...(isLiveTarget
					? {
							confirm: goLiveConfirmText.trim().toUpperCase(),
							liveNotionalCeilingUsd: goLiveCeilingUsd ?? undefined,
						}
					: {}),
			});
			addToast(`${strategyId} → ${lifecycleStageLabel(target)}`, 'success');
			open = false;
			resetSelection();
			dispatch('changed', { toStage: target });
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Stage change failed';
			if (!override && isCapitalTarget) {
				// Promotion gate rejected a capital-stage move: surface the reason and
				// let the operator explicitly override instead of silently failing.
				blockReason = message;
			} else {
				addToast(message, 'error');
			}
		} finally {
			submitting = false;
		}
	}
</script>

<div class="relative flex items-center gap-1.5" data-testid="stage-control">
	<button
		type="button"
		data-testid="stage-control-toggle"
		class={`flex items-center gap-1.5 border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest transition-colors ${stageTone(currentStage)}`}
		title="Change lifecycle stage"
		aria-expanded={open}
		on:click={toggleOpen}
	>
		{lifecycleStageLabel(currentStage)}
		<svg class="h-3 w-3 opacity-70" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
			<path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 10.94l3.71-3.71a.75.75 0 111.06 1.06l-4.24 4.24a.75.75 0 01-1.06 0L5.21 8.29a.75.75 0 01.02-1.08z" clip-rule="evenodd" />
		</svg>
	</button>

	{#if open}
		<div
			data-testid="stage-control-panel"
			class="absolute left-0 top-full z-30 mt-1.5 w-72 border border-[#222] bg-[#050505] p-2"
		>
			{#if !targetStage}
				<div class="px-1 pb-1.5 text-[10px] uppercase tracking-wider text-[#666]">Move to stage</div>
				<div class="space-y-0.5">
					{#each stageOptions as option (option.key)}
						<button
							type="button"
							data-testid={`stage-option-${option.key}`}
							class={`flex w-full items-center justify-between px-2 py-1.5 text-left text-xs transition-colors ${optionTone(option.kind)}`}
							on:click={() => selectTarget(option.key)}
						>
							<span>{option.label}</span>
							<span class="text-[9px] uppercase tracking-[0.14em] opacity-60">
								{option.kind === 'forward' ? 'Promote' : option.kind === 'backward' ? 'Demote' : option.kind === 'revive' ? 'Revive' : 'Archive'}
							</span>
						</button>
					{/each}
				</div>
			{:else}
				<div class="space-y-2 p-1">
					<div class="text-xs text-[#ccc]">
						Move <span class="font-mono text-white">{strategyId}</span> to
						<span class="font-semibold">{targetOption?.label ?? lifecycleStageLabel(targetStage)}</span>?
					</div>
					{#if targetStage === 'archived'}
						<div class="rounded border border-red-900/40 bg-red-950/15 px-2 py-1.5 text-[11px] text-red-200">
							Archiving removes the strategy from the active pipeline and stops scanning it.
						</div>
					{/if}
					{#if isLiveTarget}
						<div class="space-y-1.5 rounded border border-emerald-800/50 bg-emerald-950/20 p-2" data-testid="stage-control-go-live">
							<div class="text-[11px] font-semibold text-emerald-200">
								Going live trades real capital.
							</div>
							<div class="text-[11px] text-emerald-100/80">
								Set the initial per-asset notional ceiling — the largest position (USD) this strategy may hold live. Enforced on every order; editable later.
							</div>
							<input
								type="number"
								min="1"
								step="any"
								bind:value={goLiveCeilingUsd}
								data-testid="stage-control-live-ceiling"
								placeholder="Notional ceiling (USD), e.g. 1000"
								class="w-full rounded border border-[#2b2b2b] bg-black px-2 py-1 text-xs text-gray-200 placeholder:text-gray-600 focus:border-emerald-700 focus:outline-none"
							/>
							<input
								type="text"
								bind:value={goLiveConfirmText}
								data-testid="stage-control-go-live-confirm"
								placeholder={`Type ${GO_LIVE_PHRASE} to confirm`}
								class="w-full rounded border border-[#2b2b2b] bg-black px-2 py-1 text-xs text-gray-200 placeholder:text-gray-600 focus:border-emerald-700 focus:outline-none"
							/>
						</div>
					{/if}
					<textarea
						bind:value={reason}
						data-testid="stage-control-reason"
						placeholder="Reason (optional)"
						rows="2"
						class="w-full border border-[#333] bg-black px-2 py-1 text-xs text-[#aaa] placeholder:text-[#555] focus:border-white focus:outline-none"
					></textarea>
					{#if blockReason}
						<div class="rounded border border-amber-700/50 bg-amber-950/30 p-2 text-[11px]" data-testid="stage-control-block-reason">
							<div class="font-semibold text-amber-200">Promotion gate blocked this:</div>
							<div class="mt-0.5 text-amber-100/90">{blockReason}</div>
							<div class="mt-1 text-amber-300/70">Overriding promotes anyway (logged). The mainnet hard-gate is separate and unaffected.</div>
						</div>
					{/if}
					<div class="flex gap-1.5">
						{#if blockReason}
							<button
								type="button"
								data-testid="stage-control-override"
								disabled={submitting || !goLiveArmed}
								class="rounded bg-amber-600 px-3 py-1 text-xs text-white transition hover:bg-amber-500 disabled:opacity-50"
								on:click={() => void confirm(true)}
							>{submitting ? 'Overriding…' : 'Override gate & promote'}</button>
						{:else}
							<button
								type="button"
								data-testid="stage-control-confirm"
								disabled={submitting || !goLiveArmed}
								class="terminal-button-primary px-3 py-1 text-xs disabled:opacity-50"
								on:click={() => void confirm(false)}
							>{submitting ? 'Moving…' : 'Confirm'}</button>
						{/if}
						<button
							type="button"
							class="rounded border border-[#2b2b2b] bg-black px-3 py-1 text-xs text-gray-400 transition hover:text-gray-200"
							on:click={resetSelection}
						>Back</button>
						<button
							type="button"
							class="ml-auto rounded border border-[#2b2b2b] bg-black px-3 py-1 text-xs text-gray-400 transition hover:text-gray-200"
							on:click={toggleOpen}
						>Cancel</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
