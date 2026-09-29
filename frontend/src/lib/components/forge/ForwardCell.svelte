<script lang="ts">
	import type { ForwardRecord } from '$lib/utils/forge/forward';
	import { TONE_DOT } from '$lib/utils/forge/status';
	import { fmtUsd, signClass } from '$lib/utils/strategyContainer/format';

	export let record: ForwardRecord | null = null;
	export let stage = '';
	export let loading = false;

	$: trading = stage === 'paper' || stage === 'live_graduated';
	$: bookNote =
		record?.book === 'live'
			? 'Live wallet dollars, realized, net of recorded fees and funding'
			: 'Realized on the simulated paper book, net of costs — not wallet money';
</script>

{#if record}
	<div class="grid gap-0.5" title={bookNote}>
		<span class="inline-flex items-baseline gap-1.5">
			<b class={`font-plex-mono text-[12px] font-medium ${signClass(record.pnlUsd) || 'text-sc-ink'}`}>{fmtUsd(record.pnlUsd, record.book === 'live' ? 2 : 0)}</b>
			<span class="font-plex-cond text-[10px] uppercase tracking-[0.08em] text-sc-ink4">{record.book}</span>
		</span>
		<span class="text-[10.5px] text-sc-ink3">
			{record.closed} closed{#if record.winRate !== null} · {Math.round(record.winRate)}% win{/if}
		</span>
		<span class="inline-flex items-center gap-1 text-[10.5px] text-sc-ink2">
			<span class={`h-1.5 w-1.5 rounded-full ${TONE_DOT[record.tone]}`} aria-hidden="true"></span>{record.stateLabel}
		</span>
	</div>
{:else if trading && loading}
	<span class="block h-3 w-16 animate-pulse rounded bg-sc-raise/70" aria-busy="true"></span>
{:else if trading}
	<span class="text-[11px] text-sc-ink3">No session yet</span>
{:else}
	<span class="text-[11px] text-sc-ink4">Not trading yet</span>
{/if}
