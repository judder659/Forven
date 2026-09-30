<script lang="ts">
	import type { AgentYieldSummary, Reach } from '$lib/utils/agentsHub/yield';
	import { fmtCost, plural } from '$lib/utils/agentsHub/format';

	export let summaries: AgentYieldSummary[] = [];
	export let names: Record<string, string> = {};
	export let loading = false;
	export let error: string | null = null;
	/** "24 hours" / "7 days". */
	export let windowLabel = '24 hours';

	const STEPS: Array<{ key: Reach; label: string }> = [
		{ key: 'quick_screen', label: 'Created' },
		{ key: 'gauntlet', label: 'Reached gauntlet' },
		{ key: 'paper', label: 'Reached paper' },
		{ key: 'live', label: 'Went live' },
	];

	function share(part: number, whole: number): number {
		return whole > 0 ? Math.max(part > 0 ? 2 : 0, (part / whole) * 100) : 0;
	}

	function pct(part: number, whole: number): string {
		if (whole <= 0) return '';
		const value = (part / whole) * 100;
		if (value === 0) return '0%';
		if (value < 1) return '<1%';
		return `${Math.round(value)}%`;
	}
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="agents-yield">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="whitespace-nowrap text-[13px] font-semibold text-sc-ink">What they produced</h2>
		<span class="truncate text-[11px] text-sc-ink3" title="Strategies the agents created in this window, followed through the pipeline">Strategies created · last {windowLabel}</span>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
		{#if error && summaries.length === 0}
			<p class="m-0 text-[12px] text-[#f2956f]">{error}</p>
		{:else if loading && summaries.length === 0}
			<div class="grid gap-2" aria-busy="true">
				{#each [0, 1, 2] as i (i)}
					<div class="h-8 animate-pulse rounded bg-sc-raise/60"></div>
				{/each}
			</div>
		{:else if summaries.length === 0}
			<p class="m-0 py-4 text-center text-[12px] text-sc-ink3">No agent created a strategy in the last {windowLabel}.</p>
		{:else}
			<div class="grid gap-5">
				{#each summaries as summary (summary.agentId)}
					<section class="grid gap-2.5" data-testid="agents-yield-agent">
						<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
							<h3 class="m-0 text-[12.5px] font-medium text-sc-ink">{names[summary.agentId] ?? summary.agentId}</h3>
							<span class="text-[11px] text-sc-ink3">
								{#if summary.ideas > 0}{plural(summary.ideas, 'idea')} ·{' '}{/if}{#if summary.spend !== null}{fmtCost(summary.spend)} spent{#if summary.costPerStrategy !== null}{' '}· {fmtCost(summary.costPerStrategy)} per strategy{/if}{/if}
							</span>
						</div>

						<div class="grid gap-1">
							{#each STEPS as step (step.key)}
								{@const count = summary.reached[step.key]}
								<div class="grid grid-cols-[112px_minmax(0,1fr)_76px] items-center gap-2 text-[11.5px]">
									<span class="text-sc-ink2">{step.label}</span>
									<div class="h-2 overflow-hidden rounded-sm bg-sc-raise">
										<div
											class={`h-full rounded-sm ${step.key === 'quick_screen' ? 'bg-sc-ink3' : step.key === 'gauntlet' ? 'bg-[#7fb2ff]' : 'bg-[#3cc48f]'}`}
											style={`width: ${share(count, summary.created)}%`}
										></div>
									</div>
									<span class="text-right font-plex-mono text-sc-ink">{count.toLocaleString('en-US')}{#if step.key !== 'quick_screen'}<span class="ml-1 text-sc-ink3">{pct(count, summary.created)}</span>{/if}</span>
								</div>
							{/each}
						</div>

						<p class="m-0 text-[11px] text-sc-ink3">
							{summary.active.toLocaleString('en-US')} still in the pipeline · {summary.graveyard.toLocaleString('en-US')} in the graveyard
							{#if summary.graveyard > 0}({summary.judged.toLocaleString('en-US')} judged on merit, {summary.untested.toLocaleString('en-US')} never fairly tested){/if}
						</p>

						{#if summary.causes.length > 0}
							<div>
								<div class="mb-1 text-[11px] text-sc-ink3">Why they stopped</div>
								<div class="flex flex-wrap gap-1.5">
									{#each summary.causes.slice(0, 6) as cause (cause.key)}
										<span class="inline-flex items-center gap-1.5 rounded border border-sc-line2 bg-sc-panel2 px-1.5 py-0.5 text-[11px] text-sc-ink2" title={cause.topDetail ? `Most often: ${cause.topDetail}` : cause.label}>
											{cause.label}
											<span class="font-plex-mono text-sc-ink">{cause.count.toLocaleString('en-US')}</span>
										</span>
									{/each}
								</div>
							</div>
						{/if}

						{#if summary.models.length > 1}
							<div>
								<div class="mb-1 text-[11px] text-sc-ink3">By model</div>
								<div class="grid gap-0.5">
									{#each summary.models as model (model.model)}
										<div class="flex items-baseline justify-between gap-3 text-[11.5px]">
											<span class="truncate font-plex-mono text-sc-ink2">{model.model}</span>
											<span class="shrink-0 font-plex-mono text-sc-ink">{model.created} <span class="text-sc-ink3">→ {model.reachedGauntlet} to gauntlet</span></span>
										</div>
									{/each}
								</div>
							</div>
						{/if}

						{#if summary.examples.length > 0}
							<details class="text-[11px]">
								<summary class="cursor-pointer text-sc-ink3 hover:text-sc-ink">Latest verdicts</summary>
								<ul class="m-0 mt-1 grid list-none gap-1 p-0">
									{#each summary.examples as example (example.id)}
										<li class="min-w-0">
											<a class="font-plex-mono text-sc-ink hover:underline" href={`/lab/strategy/${encodeURIComponent(example.id)}`}>{example.id}</a>
											<span class="text-sc-ink3"> · {example.label} · </span>
											<span class="break-words text-sc-ink2">{example.clause}</span>
										</li>
									{/each}
								</ul>
							</details>
						{/if}
					</section>
				{/each}
			</div>
		{/if}
	</div>
</article>
