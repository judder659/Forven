<script lang="ts">
	import type { HypothesisStatus, VerdictMemo, VerdictSignals } from '$lib/api';

	export let status: HypothesisStatus | string;
	export let memo: VerdictMemo | null | undefined = null;
	export let memoAt: string | null | undefined = null;
	export let memoBy: string | null | undefined = null;
	export let signals: VerdictSignals | null | undefined = null;
	export let canReopen = false;
	export let onReopen: (rationale: string) => Promise<void> = async () => {};

	let reopenRationale = '';
	let busy = false;

	async function doReopen() {
		busy = true;
		try {
			await onReopen(reopenRationale.trim());
			reopenRationale = '';
		} finally {
			busy = false;
		}
	}

	function statusLabel(s: string): string {
		switch (s) {
			case 'proven':
				return 'Proven';
			case 'disproven':
				return 'Disproven';
			case 'researching':
				return 'Researching';
			default:
				return 'Proposed';
		}
	}

	function statusTone(s: string): string {
		switch (s) {
			case 'proven':
				return 'text-emerald-400';
			case 'disproven':
				return 'text-[#555] line-through';
			case 'researching':
				return 'text-white';
			default:
				return 'text-yellow-400';
		}
	}

	function claimLabel(v: string): string {
		switch (v) {
			case 'confirmed':
				return 'Claim confirmed';
			case 'partially_confirmed':
				return 'Claim partially confirmed';
			case 'disproven':
				return 'Claim busted';
			case 'unverified':
				return 'Claim unverified';
			default:
				return '';
		}
	}

	function claimClasses(v: string): string {
		switch (v) {
			case 'confirmed':
				return 'border-emerald-900 bg-emerald-500/10 text-emerald-400';
			case 'partially_confirmed':
				return 'border-yellow-900 bg-yellow-500/10 text-yellow-400';
			case 'disproven':
				return 'border-red-900 bg-red-500/10 text-red-400';
			default:
				return 'border-[#333] text-[#888]';
		}
	}

	function formatStamp(value: string | null | undefined): string {
		if (!value) return '';
		const normalized = value.includes('T') ? value : `${value}Z`;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return value;
		return parsed.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
	}
</script>

<section class="terminal-card p-4" data-testid="verdict-memo-card">
	<div class="flex items-center justify-between gap-3">
		<div class="text-[10px] uppercase tracking-widest text-[#666]">Current verdict</div>
		{#if canReopen}
			<button
				type="button"
				class="border border-yellow-900 bg-yellow-500/10 px-3 py-1 text-xs font-bold uppercase tracking-widest text-yellow-400 transition-colors hover:bg-yellow-500/20 disabled:opacity-50"
				on:click={doReopen}
				disabled={busy}
				data-action="reopen-hypothesis"
			>
				{busy ? 'Reopening…' : 'Reopen'}
			</button>
		{/if}
	</div>

	<div class={`mt-2 text-lg font-semibold ${statusTone(status)}`}>{statusLabel(status)}</div>

	{#if memo?.claim_verdict && memo.claim_verdict !== 'no_claim'}
		<div class="mt-2">
			<span class={`inline-flex items-center border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest ${claimClasses(memo.claim_verdict)}`} data-claim-verdict={memo.claim_verdict}>
				{claimLabel(memo.claim_verdict)}
			</span>
			{#if memo.claim_assessment}
				<p class="mt-1.5 text-xs text-[#666]">{memo.claim_assessment}</p>
			{/if}
		</div>
	{/if}

	{#if memo?.rationale}
		<p class="mt-2 text-sm text-[#888]">{memo.rationale}</p>
	{:else if !memo}
		<p class="mt-2 text-sm text-[#666]">No verdict memo has been written yet.</p>
	{/if}

	{#if memo?.evidence_summary}
		<p class="mt-2 text-xs text-[#666]">{memo.evidence_summary}</p>
	{/if}

	{#if signals && signals.rolling_window_size > 0}
		<div class="mt-3 grid grid-cols-3 gap-px bg-[#1a1a1a] text-center">
			<div class="bg-[#050505] px-2 py-1.5">
				<div class="text-[9px] uppercase tracking-wider text-[#666]">Hit rate</div>
				<div class="mt-0.5 text-sm font-semibold text-[#888]">{Math.round(signals.hit_rate * 100)}%</div>
				<div class="text-[9px] text-[#555]">need ≥ {Math.round(signals.hit_rate_threshold * 100)}%</div>
			</div>
			<div class="bg-[#050505] px-2 py-1.5">
				<div class="text-[9px] uppercase tracking-wider text-[#666]">Diversity</div>
				<div class="mt-0.5 text-sm font-semibold text-[#888]">{signals.diversity_cells}</div>
				<div class="text-[9px] text-[#555]">need ≥ {signals.effective_min_diversity_cells ?? signals.min_diversity_cells} cells</div>
			</div>
			<div class="bg-[#050505] px-2 py-1.5">
				<div class="text-[9px] uppercase tracking-wider text-[#666]">Window</div>
				<div class="mt-0.5 text-sm font-semibold text-[#888]">{signals.rolling_window_size}/{signals.rolling_window_setting}</div>
				<div class="text-[9px] text-[#555]">children</div>
			</div>
		</div>
		<div class="mt-1.5 text-[10px] text-[#666]">
			Evidence floor: <span class="text-[#888]">{signals.mathematical_verdict}</span>
		</div>
	{/if}

	{#if memo?.next_step_suggestions?.length}
		<div class="mt-3">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Next steps</div>
			<ul class="mt-1 list-disc pl-5 text-sm text-[#888]">
				{#each memo.next_step_suggestions as suggestion}
					<li>{suggestion}</li>
				{/each}
			</ul>
		</div>
	{/if}

	{#if memo?.garbage_signal}
		<div class="mt-3 inline-flex items-center border border-red-900 bg-red-500/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-red-400">
			Garbage signal
		</div>
	{/if}

	{#if memoAt}
		<div class="mt-3 text-[11px] text-[#666]">Written {formatStamp(memoAt)} by {memoBy ?? 'unknown'}</div>
	{/if}

	{#if canReopen}
		<label class="mt-3 block text-[10px] uppercase tracking-wider text-[#666]" for="reopen-rationale">
			Optional rationale
		</label>
		<input
			id="reopen-rationale"
			type="text"
			bind:value={reopenRationale}
			placeholder="Why reopen this crucible?"
			class="terminal-input mt-1 w-full text-xs"
		/>
	{/if}
</section>
