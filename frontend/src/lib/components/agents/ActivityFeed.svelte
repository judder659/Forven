<script lang="ts">
	import type { ActivityRun } from '$lib/api/agentsHub';
	import { fmtCost, fmtSeconds, fmtTokens } from '$lib/utils/agentsHub/format';
	import { TONE_TEXT, type Tone } from '$lib/utils/forge/status';
	import { ago, shortDateTime } from '$lib/utils/forge/time';

	export let runs: ActivityRun[] = [];
	export let names: Record<string, string> = {};
	export let now = Date.now();
	export let loading = false;
	export let error: string | null = null;

	const OUTCOME: Record<ActivityRun['outcome'], { glyph: string; tone: Tone; label: string }> = {
		ok: { glyph: '✓', tone: 'ok', label: 'Done' },
		failed: { glyph: '✕', tone: 'fail', label: 'Failed' },
		blocked: { glyph: '‖', tone: 'caution', label: 'Blocked' },
		stopped: { glyph: '–', tone: 'idle', label: 'Cancelled' },
		open: { glyph: '·', tone: 'wait', label: 'Open' },
	};

	function runHref(run: ActivityRun): string {
		return `/tasks/${encodeURIComponent(run.display_id)}?returnTo=${encodeURIComponent('/agents')}`;
	}
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="agents-activity">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="text-[13px] font-semibold text-sc-ink">Latest runs</h2>
		<a href="/agents?tab=tasks" class="text-[11px] text-sc-ink3 hover:text-sc-ink">All runs →</a>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if error && runs.length === 0}
			<p class="m-0 px-3.5 py-4 text-[12px] text-[#f2956f]">{error}</p>
		{:else if loading && runs.length === 0}
			<div class="grid gap-2 px-3.5 py-3" aria-busy="true">
				{#each [0, 1, 2, 3, 4] as i (i)}
					<div class="h-7 animate-pulse rounded bg-sc-raise/60"></div>
				{/each}
			</div>
		{:else if runs.length === 0}
			<p class="m-0 px-3.5 py-6 text-center text-[12px] text-sc-ink3">No finished runs yet.</p>
		{:else}
			<ol class="m-0 list-none p-0">
				{#each runs as run (run.display_id)}
					{@const outcome = OUTCOME[run.outcome] ?? OUTCOME.open}
					<li class="grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-2 px-3.5 py-1.5 hover:bg-sc-hover" data-testid="agents-activity-row">
						<span class={`mt-px text-center font-plex-mono text-[12px] leading-5 ${TONE_TEXT[outcome.tone]}`} title={outcome.label} aria-label={outcome.label}>{outcome.glyph}</span>
						<div class="min-w-0">
							{#if run.type === 'brain_invoke'}
								<span class="block truncate text-[12px] leading-5 text-sc-ink" title={run.summary ?? run.title}>{run.title}</span>
							{:else}
								<a class="block truncate text-[12px] leading-5 text-sc-ink hover:underline" href={runHref(run)} title={run.title}>{run.title}</a>
							{/if}
							<span class="block truncate text-[11px] text-sc-ink3" title={run.error ?? run.summary ?? ''}>
								{names[run.agent_id] ?? run.agent_id}{run.seconds !== null ? ` · ${fmtSeconds(run.seconds)}` : ''}{run.tokens > 0 ? ` · ${fmtTokens(run.tokens)} tok` : ''}{run.cost_usd !== null && run.cost_usd > 0 ? ` · ${fmtCost(run.cost_usd)}` : ''}{#if run.error}<span class="text-[#f2956f]">{` · ${run.error}`}</span>{:else if run.summary}{` · ${run.summary}`}{/if}
							</span>
						</div>
						<span class="whitespace-nowrap pt-0.5 text-[11px] text-sc-ink3" title={shortDateTime(run.completed_at)}>{ago(run.completed_at, now)}</span>
					</li>
				{/each}
			</ol>
		{/if}
	</div>
</article>
