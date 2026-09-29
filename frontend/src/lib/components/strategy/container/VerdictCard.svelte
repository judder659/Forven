<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Finding, Verdict } from '$lib/utils/strategyContainer/verdict';

	export let verdict: Verdict;
	export let findings: Finding[] = [];
	export let loading = false;

	const dispatch = createEventDispatcher<{ navigate: { tab: string; anchor?: string } }>();

	const PILL: Record<Verdict['tone'], string> = {
		ok: 'border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]',
		caution: 'border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]',
		fail: 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#e5574f]',
		idle: 'border-sc-line2 text-sc-ink2',
	};
	const MARK: Record<Finding['tone'], { text: string; cls: string }> = {
		ok: { text: '✓', cls: 'bg-[#3cc48f]/15 text-[#3cc48f]' },
		caution: { text: '!', cls: 'bg-[#e7b24a]/15 text-[#e7b24a]' },
		fail: { text: '×', cls: 'bg-[#e5574f]/15 text-[#e5574f]' },
		info: { text: 'i', cls: 'bg-sc-raise text-sc-ink2' },
	};
	$: cautions = findings.filter((finding) => finding.tone === 'caution').length;
	$: pillIcon = verdict.tone === 'ok' ? '✓' : verdict.tone === 'fail' ? '×' : verdict.tone === 'caution' ? '!' : '·';
</script>

<article class="grid content-start gap-3 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" data-testid="verdict-card">
	<div class="flex flex-wrap items-center gap-x-3.5 gap-y-2">
		<span class={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] font-semibold ${PILL[verdict.tone]}`} data-testid="verdict-label">
			<span aria-hidden="true">{pillIcon}</span>{verdict.label}
		</span>
		<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			{loading ? 'Reading the evidence…' : `${findings.length} checks · ${cautions} to watch`}
		</span>
	</div>
	<p class="m-0 max-w-[80ch] text-[15px] leading-snug text-sc-ink" data-testid="verdict-headline">{verdict.headline}</p>
	<ol class="m-0 grid list-none border-t border-sc-line p-0" data-testid="verdict-findings">
		{#each findings as finding (finding.key)}
			<li class="grid grid-cols-[22px_minmax(0,1fr)_auto] items-start gap-2.5 border-b border-sc-line py-2" data-testid={`finding-${finding.key}`}>
				<span class={`mt-0.5 grid h-[18px] w-[18px] place-items-center rounded-full text-[11px] font-semibold ${MARK[finding.tone].cls}`} aria-hidden="true">{MARK[finding.tone].text}</span>
				<div class="min-w-0 text-[12px] leading-relaxed">
					<span class="font-semibold text-sc-ink">{finding.title}</span>
					<span class="text-sc-ink2"> {finding.body}</span>
					<span class="block text-[10.5px] text-sc-ink4" title="The rule behind this finding">rule: {finding.rule}</span>
				</div>
				<button
					type="button"
					class="whitespace-nowrap text-[11px] text-sc-ink3 underline underline-offset-2 hover:text-sc-ink"
					on:click={() => dispatch('navigate', finding.target)}
				>Evidence</button>
			</li>
		{/each}
	</ol>
</article>
