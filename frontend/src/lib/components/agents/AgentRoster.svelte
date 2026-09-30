<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { FleetAgent, FleetWindow } from '$lib/api/agentsHub';
	import Sparkbars from './Sparkbars.svelte';
	import { STATE_META, agentJob, canPause, modelParts, stateLine } from '$lib/utils/agentsHub/agents';
	import { fmtCost, fmtRate, fmtSeconds, fmtTokens, plural } from '$lib/utils/agentsHub/format';
	import { TONE_DOT, TONE_PILL } from '$lib/utils/forge/status';

	export let agents: FleetAgent[] = [];
	export let window: FleetWindow = '24h';
	export let now = Date.now();
	export let loading = false;
	/** Agent whose pause/resume is in flight. */
	export let busyAgent: string | null = null;
	export let autonomy: string | null = null;

	const dispatch = createEventDispatcher<{ open: string; toggle: FleetAgent }>();

	$: windowWords = window === '7d' ? 'the last 7 days' : 'the last 24 hours';

	function runsHref(agentId: string, status: string): string {
		return `/agents?tab=tasks&status=${status}&agent=${encodeURIComponent(agentId)}`;
	}
</script>

<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Agent roster" data-testid="agents-roster">
	<header class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="text-[13px] font-semibold text-sc-ink">Roster</h2>
		<span class="text-[11px] text-sc-ink3">Bars: runs finished over {windowWords} · <span class="text-[#3cc48f]">done</span> · <span class="text-[#e7b24a]">blocked</span> · <span class="text-[#f2956f]">failed</span></span>
	</header>
	<div class="overflow-x-auto">
		<table class="w-full min-w-[1080px] table-fixed border-collapse text-left">
			<colgroup>
				<col class="w-[25%]" />
				<col class="w-[24%]" />
				<col class="w-[230px]" />
				<col class="w-[120px]" />
				<col class="w-[108px]" />
				<col class="w-[128px]" />
				<col class="w-[124px]" />
			</colgroup>
			<thead>
				<tr class="border-b border-sc-line text-[11px] text-sc-ink3">
					<th class="px-3.5 py-2 font-medium">Agent</th>
					<th class="px-3 py-2 font-medium">Now</th>
					<th class="px-3 py-2 font-medium">Runs · {window}</th>
					<th class="px-3 py-2 font-medium">Needs a look</th>
					<th class="whitespace-nowrap px-3 py-2 text-right font-medium">Spend · {window === '7d' ? '7d' : 'today'}</th>
					<th class="px-3 py-2 font-medium">Model</th>
					<th class="px-3.5 py-2 text-right font-medium"><span class="sr-only">Actions</span></th>
				</tr>
			</thead>
			<tbody class="divide-y divide-sc-line">
				{#if loading && agents.length === 0}
					{#each [0, 1, 2, 3, 4, 5] as i (i)}
						<tr><td colspan="7" class="px-3.5 py-3"><div class="h-8 animate-pulse rounded bg-sc-raise/60"></div></td></tr>
					{/each}
				{:else}
					{#each agents as agent (agent.id)}
						{@const meta = STATE_META[agent.state]}
						{@const job = agentJob(agent.id)}
						{@const model = modelParts(agent)}
						{@const stats = agent.window}
						<tr class={`group align-top hover:bg-sc-hover ${agent.enabled ? '' : 'bg-[#e7b24a]/[0.03]'}`} data-testid="agents-roster-row">
							<td class="px-3.5 py-2.5">
								<div class="flex items-start gap-2.5">
									<span class={`mt-[6px] h-2 w-2 shrink-0 rounded-full ${TONE_DOT[meta.tone]} ${agent.state === 'running' ? 'animate-pulse' : ''}`} aria-hidden="true"></span>
									<div class="min-w-0">
										<div class="flex flex-wrap items-center gap-x-2">
											<button type="button" class="text-left text-[13px] font-medium text-sc-ink hover:underline" on:click={() => dispatch('open', agent.id)}>{agent.name}</button>
											<span class="text-[11px] text-sc-ink3">{job.job}</span>
										</div>
										<p class="m-0 mt-0.5 truncate text-[11px] text-sc-ink3" title={agent.role}>{agent.role || agent.id}</p>
									</div>
								</div>
							</td>
							<td class="px-3 py-2.5">
								<span class={`inline-flex items-center rounded border px-1.5 py-px text-[11px] ${TONE_PILL[meta.tone]}`}>{meta.label}</span>
								<p class="m-0 mt-1 truncate text-[11.5px] text-sc-ink2" title={stateLine(agent, now)}>{stateLine(agent, now)}</p>
								{#if agent.id === 'brain' && autonomy}
									<p class="m-0 text-[11px] text-sc-ink3">Autonomy: {autonomy === 'semi_auto' ? 'Semi' : autonomy.charAt(0).toUpperCase() + autonomy.slice(1)}</p>
								{/if}
							</td>
							<td class="px-3 py-2.5">
								<div class="flex items-center gap-2.5">
									<Sparkbars buckets={stats.buckets} label={`${plural(stats.runs, 'run')} over ${windowWords}`} />
									<div class="min-w-0 whitespace-nowrap leading-tight">
										<div class="font-plex-mono text-[12px] text-sc-ink">{stats.runs.toLocaleString('en-US')} <span class={stats.success_rate !== null && stats.success_rate < 0.85 ? 'text-[#e7b24a]' : 'text-sc-ink3'}>{fmtRate(stats.success_rate)}</span></div>
										<div class="text-[11px] text-sc-ink3" title="Median time of a successful run">{stats.median_seconds !== null ? `~${fmtSeconds(stats.median_seconds)}` : 'none finished'}</div>
									</div>
								</div>
							</td>
							<td class="whitespace-nowrap px-3 py-2.5 text-[12px]">
								{#if agent.failed_open === 0 && agent.blocked === 0}
									<span class="text-sc-ink4">—</span>
								{:else}
									<div class="grid gap-0.5">
										{#if agent.blocked > 0}
											<a class="text-[#e7b24a] hover:underline" href={runsHref(agent.id, 'blocked')} title="Runs that stopped short and wait for a resume or a dismiss">{agent.blocked.toLocaleString('en-US')} blocked</a>
										{/if}
										{#if agent.failed_open > 0}
											<a class="text-[#f2956f] hover:underline" href={runsHref(agent.id, 'failed')} title="Failed runs you have not dismissed yet, from any date">{agent.failed_open.toLocaleString('en-US')} failed</a>
										{/if}
									</div>
								{/if}
							</td>
							<td class="whitespace-nowrap px-3 py-2.5 text-right">
								<div class="font-plex-mono text-[12px] text-sc-ink">{fmtCost(window === '7d' ? agent.spend.d7 : agent.spend.today)}</div>
								<div class="text-[11px] text-sc-ink3" title="Tokens used">{fmtTokens(window === '7d' ? agent.spend.tokens_d7 : agent.spend.tokens_today)} tok</div>
							</td>
							<td class="px-3 py-2.5">
								<div class="truncate font-plex-mono text-[11.5px] text-sc-ink2" title={`${model.model} · ${model.provider}`}>{model.model}</div>
								<div class="truncate text-[11px] text-sc-ink3">{model.provider}</div>
							</td>
							<td class="whitespace-nowrap px-3.5 py-2.5 text-right">
								<div class="inline-flex items-center gap-1.5">
									{#if canPause(agent)}
										<button
											type="button"
											class={`rounded border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-50 ${agent.enabled ? 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink' : 'border-[#7fb2ff]/45 text-[#a9cbff] hover:border-[#7fb2ff]'}`}
											disabled={busyAgent !== null}
											title={agent.enabled ? job.pauseEffect : 'Let this agent take runs again'}
											on:click={() => dispatch('toggle', agent)}
										>{busyAgent === agent.id ? 'Working…' : agent.enabled ? 'Pause' : 'Resume'}</button>
									{/if}
									<button
										type="button"
										class="rounded border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
										on:click={() => dispatch('open', agent.id)}
									>Open</button>
								</div>
							</td>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	</div>
</section>
