<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { NowWorkingRow } from '$lib/api/forven';
	import type { HealthStatusResponse } from '$lib/api/types';
	import { friendlyName, taskLabel } from '$lib/utils/forge/attention';
	import { stageLabel } from '$lib/utils/forge/status';
	import { ago, elapsed } from '$lib/utils/forge/time';
	import type { HourBucket } from '$lib/utils/forge/activity';

	export let rows: NowWorkingRow[] = [];
	export let loaded = false;
	export let error: string | null = null;
	export let health: HealthStatusResponse | null = null;
	export let healthLoaded = false;
	export let healthError: string | null = null;
	/** Newest pipeline event, for the idle state. */
	export let lastEventAt: string | null = null;
	/** Pipeline moves per hour, oldest first (see hourlyMoves). */
	export let moves: HourBucket[] = [];
	/** Oldest event we hold; earlier buckets are unknown rather than quiet. */
	export let coveredFrom: number | null = null;
	export let now = Date.now();

	const dispatch = createEventDispatcher<{ peek: string; retry: 'work' | 'health' }>();

	const STATE_LABEL: Record<string, string> = { green: 'Healthy', amber: 'Degraded', red: 'Critical' };
	const STATE_DOT: Record<string, string> = { green: 'bg-[#3cc48f]', amber: 'bg-[#e7b24a]', red: 'bg-[#e5574f]' };
	const STATE_TEXT: Record<string, string> = { green: 'text-[#3cc48f]', amber: 'text-[#e7b24a]', red: 'text-[#f2956f]' };

	$: failedChecks = (health?.data_checks ?? []).filter((check) => !check.passed);
	$: unhealthy = (health?.components ?? []).filter((comp) => comp.state !== 'green');
	$: overall = healthError ? 'red' : String(health?.overall ?? '');
	$: overallLabel = healthError ? 'Unavailable' : !healthLoaded ? 'Checking' : STATE_LABEL[overall] ?? 'Unknown';
	$: peak = Math.max(1, ...moves.map((b) => b.promoted + b.archived + b.other));
	$: totals = moves.reduce((acc, b) => ({ promoted: acc.promoted + b.promoted, archived: acc.archived + b.archived, other: acc.other + b.other }), { promoted: 0, archived: 0, other: 0 });
	$: busiest = moves.reduce<HourBucket | null>((best, b) => (!best || b.promoted + b.archived + b.other > best.promoted + best.archived + best.other ? b : best), null);

	function hourLabel(ms: number): string {
		return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
	}
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="forge-engine">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="text-[13px] font-semibold text-sc-ink">Engine</h2>
		<details class="relative" data-testid="forge-health-chip">
			<summary class="flex cursor-pointer list-none items-center gap-1.5 text-[11px] text-sc-ink2 hover:text-sc-ink">
				<span class={`h-2 w-2 rounded-full ${STATE_DOT[overall] ?? 'bg-sc-ink4'}`} aria-hidden="true"></span>
				System health
				<b class={`font-medium ${STATE_TEXT[overall] ?? 'text-sc-ink2'}`}>{overallLabel}</b>
				{#if failedChecks.length > 0}
					<span class="text-[#f2956f]">· {failedChecks.length} issue{failedChecks.length === 1 ? '' : 's'}</span>
				{/if}
			</summary>
			<div class="absolute right-0 top-full z-30 mt-1.5 max-h-72 w-80 overflow-y-auto rounded-md border border-sc-line2 bg-sc-panel2 p-2.5 text-[11px] shadow-[0_12px_32px_rgba(0,0,0,0.45)]">
				{#if healthError}
					<div class="flex items-center gap-2 text-[#f2956f]">
						<span>{healthError}</span>
						<button type="button" class="underline" on:click={() => dispatch('retry', 'health')}>retry</button>
					</div>
				{:else if !healthLoaded}
					<div class="text-sc-ink3">Loading…</div>
				{:else}
					<ul class="m-0 grid list-none gap-1 p-0">
						{#each health?.components ?? [] as comp (comp.name)}
							<li class="flex items-center gap-2">
								<span class={`h-1.5 w-1.5 shrink-0 rounded-full ${STATE_DOT[comp.state] ?? 'bg-sc-ink4'}`}></span>
								<span class="text-sc-ink2">{friendlyName(comp.name)}</span>
								<span class={STATE_TEXT[comp.state] ?? 'text-sc-ink3'}>{STATE_LABEL[comp.state] ?? comp.state}</span>
								<span class="truncate text-sc-ink3" title={comp.message}>{comp.message}</span>
							</li>
						{/each}
					</ul>
					{#if (health?.data_checks ?? []).length > 0}
						<ul class="m-0 mt-2 grid list-none gap-1 border-t border-sc-line p-0 pt-2">
							{#each health?.data_checks ?? [] as check (check.name)}
								<li class={check.passed ? 'text-sc-ink3' : check.severity === 'critical' ? 'text-[#f2956f]' : 'text-[#e7b24a]'}>
									{friendlyName(check.name)}: {check.detail}
								</li>
							{/each}
						</ul>
					{/if}
				{/if}
			</div>
		</details>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto" data-testid="forge-now-working-chip">
		<div class="flex items-center justify-between px-3.5 pb-1 pt-2.5">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Now working</span>
			<span class="text-[11px] {error ? 'text-[#f2956f]' : rows.length > 0 ? 'text-[#3cc48f]' : 'text-sc-ink3'}">
				{error ? 'Error' : !loaded ? 'Loading' : `${rows.length} active`}
			</span>
		</div>
		{#if error}
			<div class="flex items-center gap-2 px-3.5 pb-3 text-[12px] text-[#f2956f]">
				<span>Failed to load active work</span>
				<button type="button" class="underline" on:click={() => dispatch('retry', 'work')}>retry</button>
			</div>
		{:else if !loaded}
			<div class="mx-3.5 mb-3 h-8 animate-pulse rounded bg-sc-raise/60"></div>
		{:else if rows.length === 0}
			<div class="px-3.5 pb-3">
				<p class="m-0 text-[12px] text-sc-ink2">Idle between sweeps.</p>
				<p class="m-0 text-[11px] text-sc-ink3">
					The scheduler screens and validates on its own cadence{#if lastEventAt}; last pipeline move {ago(lastEventAt, now)}{/if}.
				</p>
			</div>
		{:else}
			<ul class="m-0 list-none divide-y divide-sc-line p-0 pb-1">
				{#each rows as row (`${row.strategy_id}:${row.current_task.type}`)}
					{@const hasStrategy = !String(row.strategy_id).startsWith('task-')}
					<li class="flex items-center gap-2.5 px-3.5 py-1.5">
						<span
							class={`h-2 w-2 shrink-0 rounded-full ${row.current_task.stalled ? 'bg-[#e5574f]' : row.current_task.status === 'running' ? 'animate-pulse bg-[#3cc48f]' : 'border border-sc-ink3'}`}
							aria-hidden="true"
						></span>
						<div class="min-w-0 flex-1">
							{#if hasStrategy}
								<button type="button" class="block max-w-full truncate text-left text-[12px] text-sc-ink hover:underline" on:click={() => dispatch('peek', row.strategy_id)}>{row.name}</button>
							{:else}
								<span class="block truncate text-[12px] text-sc-ink">{row.name}</span>
							{/if}
							<span class="block truncate text-[11px] text-sc-ink3">
								{taskLabel(row.current_task.type)}{#if row.stage} · {stageLabel(row.stage).toLowerCase()}{/if}
							</span>
						</div>
						<div class="shrink-0 text-right">
							<span class="block text-[11px] {row.current_task.stalled ? 'text-[#f2956f]' : row.current_task.status === 'running' ? 'text-[#3cc48f]' : 'text-[#e7b24a]'}">
								{row.current_task.stalled ? 'stalled' : row.current_task.status.replace('_', ' ')}
							</span>
							{#if row.current_task.started_at}
								<span class="block font-plex-mono text-[10.5px] text-sc-ink3">{elapsed(row.current_task.started_at, now)}</span>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
		{/if}
		{#if moves.length > 0}
			<div class="border-t border-sc-line px-3.5 pb-2.5 pt-2" data-testid="forge-engine-pulse">
				<div class="flex items-baseline justify-between gap-2">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Pipeline pulse · 24h</span>
					<span class="text-[11px] text-sc-ink3">
						<b class="font-plex-mono font-medium text-[#3cc48f]">{totals.promoted}</b> promoted ·
						<b class="font-plex-mono font-medium text-[#f2956f]">{totals.archived}</b> archived
					</span>
				</div>
				<svg class="mt-1.5 block h-12 w-full" viewBox={`0 0 ${moves.length * 10} 48`} preserveAspectRatio="none" role="img" aria-label={`Pipeline moves per hour over the last 24 hours: ${totals.promoted} promoted, ${totals.archived} archived`}>
					{#each moves as bucket, i (bucket.start)}
						{@const total = bucket.promoted + bucket.archived + bucket.other}
						{@const unknown = coveredFrom !== null && bucket.start + 3600000 <= coveredFrom}
						{@const hArchived = (bucket.archived / peak) * 44}
						{@const hOther = (bucket.other / peak) * 44}
						{@const hPromoted = (bucket.promoted / peak) * 44}
						<g>
							<title>{hourLabel(bucket.start)} — {unknown ? 'not loaded' : `${total} move${total === 1 ? '' : 's'}: ${bucket.promoted} promoted, ${bucket.archived} archived`}</title>
							<rect x={i * 10 + 1} y="0" width="8" height="48" fill="transparent" />
							{#if unknown}
								<rect x={i * 10 + 1} y="46" width="8" height="2" rx="1" fill="#2a2f38" />
							{:else}
								<rect x={i * 10 + 1} y={48 - hArchived} width="8" height={hArchived} rx="1" fill="#e5574f" fill-opacity="0.55" />
								<rect x={i * 10 + 1} y={48 - hArchived - hOther} width="8" height={hOther} fill="#747c88" fill-opacity="0.6" />
								<rect x={i * 10 + 1} y={48 - hArchived - hOther - hPromoted} width="8" height={hPromoted} rx="1" fill="#3cc48f" />
								{#if total === 0}<rect x={i * 10 + 1} y="46.5" width="8" height="1.5" rx="0.75" fill="#1c2026" />{/if}
							{/if}
						</g>
					{/each}
				</svg>
				<div class="mt-1 flex justify-between font-plex-mono text-[10px] text-sc-ink4">
					<span>{moves[0] ? hourLabel(moves[0].start) : ''}</span>
					{#if busiest && busiest.promoted + busiest.archived + busiest.other > 0}
						<span class="text-sc-ink3">busiest {hourLabel(busiest.start)}</span>
					{/if}
					<span>now</span>
				</div>
			</div>
		{/if}
		{#if unhealthy.length > 0 || failedChecks.length > 0}
			<div class="border-t border-sc-line px-3.5 py-2">
				<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Health notes</span>
				<ul class="m-0 mt-1 grid list-none gap-0.5 p-0 text-[11px]">
					{#each unhealthy.slice(0, 3) as comp (comp.name)}
						<li class="truncate {STATE_TEXT[comp.state] ?? 'text-sc-ink2'}" title={comp.message}>{friendlyName(comp.name)} — {comp.message || STATE_LABEL[comp.state]}</li>
					{/each}
					{#each failedChecks.slice(0, 3) as check (check.name)}
						<li class="truncate {check.severity === 'critical' ? 'text-[#f2956f]' : 'text-[#e7b24a]'}" title={check.detail}>{friendlyName(check.name)} — {check.detail}</li>
					{/each}
				</ul>
			</div>
		{/if}
	</div>
</article>
