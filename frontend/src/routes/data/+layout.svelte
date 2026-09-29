<script lang="ts">
	// The Data Manager shell: one-line data status from the SLA census, market
	// search, Get data / Import file, the jobs indicator that opens the Jobs
	// drawer, and the tab bar. Tabs are routes.
	import { onDestroy, onMount, tick } from 'svelte';
	import { get } from 'svelte/store';
	import { page } from '$app/stores';
	import GlobalSearch from '$lib/components/data-manager/GlobalSearch.svelte';
	import JobsDrawer from '$lib/components/data-manager/JobsDrawer.svelte';
	import { formatCount, formatRelative, formatUtc } from '$lib/components/data-manager/format';
	import { healthVerdict, type Tone } from '$lib/components/data-manager/health';
	import { DM } from '$lib/components/data-manager/links';
	import { clock, jobsLanded, jobsSummary, loadSlaCensus, pageSearch, slaCensus, startJobsPolling } from '$lib/stores/dataManager';

	const TABS = [
		{ href: DM, label: 'Health' },
		{ href: `${DM}/catalog`, label: 'Catalog' },
		{ href: `${DM}/coverage`, label: 'Coverage' },
		{ href: `${DM}/jobs`, label: 'Jobs' },
		{ href: `${DM}/storage`, label: 'Storage' },
		{ href: `${DM}/log`, label: 'Log' },
	];
	const TONE_TEXT: Record<Tone, string> = { ok: 'text-emerald-400', warn: 'text-amber-400', bad: 'text-red-400', neutral: 'text-sc-ink2' };
	const TONE_DOT: Record<Tone, string> = { ok: 'bg-emerald-400', warn: 'bg-amber-400', bad: 'bg-red-500', neutral: 'bg-sc-ink3' };

	$: path = $page.url.pathname.replace(/\/$/, '') || '/';
	const isActive = (href: string, current: string) => (href === DM ? current === DM : current === href || current.startsWith(`${href}/`));

	let jobsOpen = false;
	let jobsButton: HTMLButtonElement | undefined;
	let headerSearch: HTMLInputElement | undefined;
	let stopJobs: (() => void) | undefined;
	let censusTimer: ReturnType<typeof setInterval> | undefined;

	onMount(() => {
		stopJobs = startJobsPolling();
		void loadSlaCensus();
		censusTimer = setInterval(() => void loadSlaCensus({ maxAgeMs: 25_000 }), 30_000);
	});
	onDestroy(() => {
		stopJobs?.();
		clearInterval(censusTimer);
	});
	// Work landed: the census (this header and Health's attention list) follows at once.
	let landedSeen = $jobsLanded;
	$: if ($jobsLanded !== landedSeen) {
		landedSeen = $jobsLanded;
		void loadSlaCensus({ force: true });
	}

	$: census = $slaCensus.data;
	// Counts only: the Health hero names the strategy when it has the rows at hand.
	$: verdict = census ? healthVerdict(census) : null;
	$: summary = $jobsSummary.data;
	$: jobsState =
		$jobsSummary.status === 'unavailable'
			? { text: 'Jobs', tone: 'text-sc-ink3', dot: '', title: 'Jobs are not available on this backend yet' }
			: summary && summary.running > 0
				? { text: `${summary.running} running${summary.queued ? ` · ${summary.queued} queued` : ''}`, tone: 'text-sky-300', dot: 'bg-sky-400 animate-pulse', title: 'Open the Jobs drawer' }
				: summary && summary.queued > 0
					? { text: `${summary.queued} queued`, tone: 'text-sc-ink', dot: 'bg-sc-ink3', title: 'Open the Jobs drawer' }
					: summary && summary.failed_24h > 0
						? { text: `${summary.failed_24h} failed`, tone: 'text-red-400', dot: 'bg-red-500', title: 'Jobs failed in the last 24 hours' }
						: { text: 'Jobs', tone: 'text-sc-ink2', dot: '', title: 'Nothing running. Open the Jobs drawer' };

	function closeJobs() {
		jobsOpen = false;
		void tick().then(() => jobsButton?.focus());
	}

	function typing(target: EventTarget | null): boolean {
		const el = target as HTMLElement | null;
		return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !typing(event.target)) {
			const target = get(pageSearch) ?? headerSearch;
			if (!target) return;
			event.preventDefault();
			target.focus();
			target.select();
		}
	}
</script>

<svelte:window on:keydown={onKeydown} />

<div class="flex h-full min-h-0 flex-col font-mono text-sc-ink">
	<header class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-sc-line px-4 py-2">
		<div class="flex items-baseline gap-2">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Data</span>
		</div>
		<a href={DM} class="flex min-w-0 max-w-full items-center gap-2 text-[11px] hover:underline decoration-sc-line2 underline-offset-4" data-testid="dm-status-line"
			title={census ? `Census generated ${formatUtc(census.generated_at, { seconds: true })}` : undefined}>
			{#if verdict && census}
				<span class="h-2 w-2 shrink-0 {TONE_DOT[verdict.tone]}" aria-hidden="true"></span>
				<span class="truncate {TONE_TEXT[verdict.tone]}">{verdict.headline}</span>
				<span class="hidden shrink-0 text-sc-ink3 lg:inline">· {formatCount(census.total)} series · checked {formatRelative(census.generated_at, $clock)}</span>
			{:else if $slaCensus.status === 'unavailable'}
				<span class="h-2 w-2 shrink-0 border border-sc-line2" aria-hidden="true"></span>
				<span class="truncate text-sc-ink3">Freshness census not available on this backend yet</span>
			{:else if $slaCensus.status === 'error'}
				<span class="h-2 w-2 shrink-0 bg-sc-line2" aria-hidden="true"></span>
				<span class="truncate text-sc-ink2">Could not check data freshness</span>
			{:else}
				<span class="h-2 w-2 shrink-0 animate-pulse bg-sc-line2" aria-hidden="true"></span>
				<span class="text-sc-ink3">Checking data…</span>
			{/if}
		</a>
		<div class="ml-auto flex flex-wrap items-center gap-2">
			<GlobalSearch bind:input={headerSearch} />
			<a href="{DM}/import" class="terminal-button text-[10px]">Import file</a>
			<a href="{DM}/get" class="terminal-button-primary text-[10px]">Get data</a>
			<button bind:this={jobsButton} type="button" on:click={() => (jobsOpen ? closeJobs() : (jobsOpen = true))}
				aria-expanded={jobsOpen} aria-haspopup="dialog" title={jobsState.title} data-testid="dm-jobs-indicator"
				class="rounded-md flex items-center gap-1.5 border px-2.5 py-1 text-[12px] transition-colors {jobsOpen ? 'border-sc-ink bg-sc-ink text-black' : `border-sc-line2 hover:border-sc-ink ${jobsState.tone}`}">
				{#if jobsState.dot}<span class="h-1.5 w-1.5 {jobsState.dot}" aria-hidden="true"></span>{/if}
				{jobsState.text}
			</button>
		</div>
	</header>

	<nav aria-label="Data Manager sections" class="flex items-stretch overflow-x-auto border-b border-sc-line px-2">
		{#each TABS as tab (tab.href)}
			{@const active = isActive(tab.href, path)}
			<a href={tab.href} aria-current={active ? 'page' : undefined}
				class="-mb-px whitespace-nowrap border-b-2 px-3 py-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] transition-colors {active ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink2'}">{tab.label}</a>
		{/each}
	</nav>

	<div class="min-h-0 flex-1 overflow-y-auto">
		<slot />
	</div>
</div>

{#if jobsOpen}
	<JobsDrawer on:close={closeJobs} />
{/if}
