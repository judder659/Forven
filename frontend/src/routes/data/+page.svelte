<script lang="ts">
	// Health: is my data OK, what needs attention and why, and how do I fix it.
	// Every state, lag and allowance shown here comes from the server.
	import { onDestroy, onMount } from 'svelte';
	import {
		getCatalog,
		getCollectorStatus,
		getDataLog,
		getStorage,
		getUniversePlanDiff,
		getVenues,
		refreshSeries,
		seedUniverse,
	} from '$lib/api/dataManager';
	import type {
		CollectorStatus,
		DataLogEntry,
		SlaCensus,
		SlaSeriesRow,
		SlaState,
		StorageInventory,
		UniversePlanDiff,
		VenueHealth,
	} from '$lib/api/dataManagerTypes';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import StackedBar from '$lib/components/data-manager/StackedBar.svelte';
	import StateChip from '$lib/components/data-manager/StateChip.svelte';
	import { keyOf, runAction } from '$lib/components/data-manager/actions';
	import {
		errorText,
		formatBytes,
		formatCount,
		formatDuration,
		formatRelative,
		formatUtc,
		plural,
		STATE_LABEL,
		STATES,
		stateTextClass,
		streamLabel,
		TIER_HELP,
		TIER_LABEL,
		TIERS,
	} from '$lib/components/data-manager/format';
	import { groupAttention, healthVerdict, tierProblems, tierTotal, whyText, type Tone } from '$lib/components/data-manager/health';
	import { catalogHref, DM, seriesHref } from '$lib/components/data-manager/links';
	import {
		clock,
		createRequestGuard,
		jobsLanded,
		loadSlaCensus,
		loading,
		settle,
		slaCensus,
		type Loadable,
	} from '$lib/stores/dataManager';

	const TONE: Record<Tone, { text: string; dot: string; box: string }> = {
		ok: { text: 'text-emerald-400', dot: 'bg-emerald-400', box: 'border-emerald-900/70 bg-emerald-500/[0.03]' },
		warn: { text: 'text-amber-400', dot: 'bg-amber-400', box: 'border-amber-900/70 bg-amber-500/[0.04]' },
		bad: { text: 'text-red-400', dot: 'bg-red-500', box: 'border-red-900 bg-red-500/[0.05]' },
		neutral: { text: 'text-sc-ink', dot: 'bg-sc-ink3', box: 'border-sc-line bg-sc-panel' },
	};
	const VENUE_STATUS: Record<VenueHealth['status'], { label: string; text: string; dot: string }> = {
		healthy: { label: 'Healthy', text: 'text-emerald-400', dot: 'bg-emerald-400' },
		degraded: { label: 'Degraded', text: 'text-amber-400', dot: 'bg-amber-400' },
		down: { label: 'Down', text: 'text-red-400', dot: 'bg-red-500' },
		unknown: { label: 'Unknown', text: 'text-sc-ink2', dot: 'bg-sc-line2' },
	};
	const PER_GROUP: Record<string, number> = { live: 8, paper: 8, pipeline: 8, universe: 4, idle: 3 };

	let attention: Loadable<SlaSeriesRow[]> = loading();
	let collector: Loadable<CollectorStatus> = loading();
	let venues: Loadable<VenueHealth[]> = loading();
	let storage: Loadable<StorageInventory> = loading();
	let incidents: Loadable<DataLogEntry[]> = loading();
	let plan: Loadable<UniversePlanDiff> = loading();
	const attentionGuard = createRequestGuard();

	/** Live, paper and pipeline problems come from the catalog so none is missed:
	 * the census `worst` list ranks every tier together. */
	async function loadAttention() {
		const { signal, current } = attentionGuard.next();
		const next = await settle(
			getCatalog({ tier: ['live', 'paper', 'pipeline'], state: ['late', 'breach', 'missing'], sort: 'priority', order: 'desc', limit: 60 }, signal).then((r) => r.rows as SlaSeriesRow[]),
			attention,
		);
		if (current()) attention = next;
	}
	const loadCollector = async () => (collector = await settle(getCollectorStatus(), collector));
	const loadVenues = async () => (venues = await settle(getVenues().then((r) => r.venues), venues));
	const loadStorage = async () => (storage = await settle(getStorage(), storage));
	const loadPlan = async () => (plan = await settle(getUniversePlanDiff(), plan));
	const loadIncidents = async () =>
		(incidents = await settle(
			getDataLog({ category: ['incident'], since: new Date(Date.now() - 86_400_000).toISOString().replace(/\.\d{3}Z$/, 'Z'), limit: 6 }).then((r) => r.entries),
			incidents,
		));

	let timer: ReturnType<typeof setInterval> | undefined;
	onMount(() => {
		void loadSlaCensus({ force: true });
		void Promise.all([loadCollector(), loadVenues(), loadStorage(), loadPlan(), loadIncidents()]);
		timer = setInterval(() => {
			void loadCollector();
			void loadVenues();
			void loadIncidents();
		}, 30_000);
	});
	onDestroy(() => {
		clearInterval(timer);
		attentionGuard.cancel();
	});
	// Work landed: the collector card and incidents follow (the layout reloads the
	// census), and "Queued" marks give way to the rows' new state.
	let landedSeen = $jobsLanded;
	$: if ($jobsLanded !== landedSeen) {
		landedSeen = $jobsLanded;
		pending = {};
		void Promise.all([loadCollector(), loadIncidents()]);
	}

	// The attention list follows the census: reload it whenever a new census lands.
	let censusAt = -1;
	$: if ($slaCensus.at !== censusAt && $slaCensus.status !== 'loading') {
		censusAt = $slaCensus.at;
		void loadAttention();
	}

	$: census = $slaCensus.data;
	$: rows = [...(attention.data ?? []), ...(census?.worst ?? [])];
	$: verdict = census ? healthVerdict(census, rows) : null;
	$: tone = TONE[verdict?.tone ?? 'neutral'];
	$: groups = census ? groupAttention(rows, census, 99).map((g) => ({ ...g, rows: g.rows.slice(0, PER_GROUP[g.tier]), hidden: g.total - Math.min(g.rows.length, PER_GROUP[g.tier]) })) : [];
	$: liveOrPaperLate = census ? tierProblems(census, 'live').total + tierProblems(census, 'paper').total : 0;
	// "Fix all" only when a refresh can help one of them (a live feed's series can't be re-fetched).
	$: fixable = rows.some((r) => (r.sla.tier === 'live' || r.sla.tier === 'paper') && ['late', 'breach', 'missing'].includes(r.sla.state) && !r.frozen && r.refreshable !== false);
	$: reclaimable = storage.data?.reclaimable.reduce((sum, g) => sum + g.bytes, 0) ?? 0;
	$: capacityShort = collector.data ? collector.data.demand_per_hour > collector.data.capacity_per_hour : false;

	const idOf = (row: SlaSeriesRow) => `${row.stream}:${row.venue}:${row.symbol}:${row.timeframe}`;
	let pending: Record<string, 'sending' | 'queued'> = {};
	let fixingAll = false;

	async function refreshRow(row: SlaSeriesRow) {
		const id = idOf(row);
		pending = { ...pending, [id]: 'sending' };
		const job = await runAction('Refreshing', () => refreshSeries({ series: [keyOf(row)], mode: 'refresh' }), {
			success: () => `${row.sla.state === 'missing' ? 'Downloading' : 'Refreshing'} ${row.symbol} ${row.timeframe}. Progress is in Jobs.`,
		});
		const { [id]: _drop, ...rest } = pending;
		pending = job ? { ...rest, [id]: 'queued' } : rest;
	}

	async function fixAll() {
		fixingAll = true;
		await runAction('Refreshing late live and paper series', () => refreshSeries({ scope: 'late_live_paper', mode: 'refresh' }), {
			success: () => 'Refreshing every late live and paper series. Progress is in Jobs.',
		});
		fixingAll = false;
	}

	let seeding = false;
	async function resumeSeed() {
		seeding = true;
		const result = await runAction('Starting the universe seed', () => seedUniverse(), {
			success: (r) => (r.status === 'already_running' ? 'The universe seed is already running.' : 'Started the universe seed. It resumes where it stopped.'),
		});
		seeding = false;
		if (result) void loadPlan();
	}

	// Takes the census as an argument: a template call that only closes over it
	// is not re-run when a new census lands.
	const stateCounts = (from: SlaCensus, tier: (typeof TIERS)[number]) => from.by_tier[tier] ?? ({} as Record<SlaState, number>);
</script>

<svelte:head><title>Data · Health | Forven</title></svelte:head>

<div class="space-y-3 p-4 pb-24">
	<!-- Verdict -->
	<section class="border px-4 py-3 {tone.box}" aria-labelledby="dm-verdict" data-testid="dm-verdict">
		{#if $slaCensus.status === 'loading' && !census}
			<div class="h-6 w-80 animate-pulse bg-sc-raise" aria-label="Checking data"></div>
			<div class="mt-2 h-3 w-48 animate-pulse bg-sc-panel2"></div>
		{:else if $slaCensus.status === 'unavailable'}
			<h1 id="dm-verdict" class="text-[15px] font-bold text-sc-ink">Data freshness can’t be checked on this backend yet</h1>
			<p class="mt-1 text-[11px] text-sc-ink3">
				The health verdict reads the freshness census (<span class="font-mono">GET /api/data/sla</span>), which arrives with the Data Manager backend update.
				Restart the backend to load it.
			</p>
		{:else if $slaCensus.status === 'error' && !census}
			<h1 id="dm-verdict" class="text-[15px] font-bold text-red-400">Could not check data freshness</h1>
			<p class="mt-1 text-[11px] text-sc-ink2">{$slaCensus.error}
				<button type="button" on:click={() => loadSlaCensus({ force: true })} class="ml-2 text-sc-ink underline">Retry</button></p>
		{:else if verdict && census}
			<div class="flex flex-wrap items-center gap-x-4 gap-y-2">
				<span class="h-2.5 w-2.5 shrink-0 {tone.dot}" aria-hidden="true"></span>
				<h1 id="dm-verdict" class="min-w-0 flex-1 text-[17px] font-bold leading-tight {tone.text}" aria-live="polite">{verdict.headline}</h1>
				{#if liveOrPaperLate && fixable}
					<button type="button" on:click={fixAll} disabled={fixingAll} class="terminal-button-primary text-[12px] disabled:opacity-50"
						title="Bring every late live and paper series current now">{fixingAll ? 'Starting…' : 'Fix all late live & paper'}</button>
				{/if}
			</div>
			<p class="mt-1.5 pl-[26px] text-[11px] text-sc-ink3">
				<span title={formatUtc(census.generated_at, { seconds: true })}>checked {formatRelative(census.generated_at, $clock)}</span>
				· {formatCount(census.total)} series{#each verdict.details as detail} · {detail}{/each}
			</p>
		{/if}
	</section>

	{#if census && census.total === 0}
		<section class="rounded-md border border-sc-line2 bg-sc-panel px-5 py-6">
			<h2 class="text-[13px] font-bold text-sc-ink">Your data lake is empty</h2>
			<p class="mt-1 max-w-xl text-[12px] leading-relaxed text-sc-ink2">
				Backtests, the gauntlet and paper trading all read stored market data. Pick a starter set and Forven downloads it in
				the background; you can keep working while it runs.
			</p>
			<a href="{DM}/setup" class="terminal-button-primary mt-4 inline-block text-[10px]">Set up data →</a>
		</section>
	{:else if census}
		<!-- Tier cards -->
		<section class="grid grid-cols-2 gap-2 md:grid-cols-5" aria-label="Freshness by tier">
			{#each TIERS as tier (tier)}
				{@const counts = stateCounts(census, tier)}
				{@const total = tierTotal(census, tier)}
				<a href={catalogHref({ tier })} title={TIER_HELP[tier]} data-testid="tier-{tier}"
					class="rounded-md group border border-sc-line bg-sc-panel px-3 py-2.5 transition-colors hover:border-sc-line2">
					<div class="flex items-baseline justify-between gap-2">
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2 group-hover:text-sc-ink">{TIER_LABEL[tier]}</span>
						<span class="font-mono text-[15px] tabular-nums text-sc-ink">{formatCount(total)}</span>
					</div>
					<div class="mt-2"><StackedBar {counts} /></div>
					<div class="mt-1.5 flex flex-wrap gap-x-2 text-[10px] leading-4">
						{#if total === 0}
							<span class="text-sc-ink3">no series</span>
						{:else}
							{#each STATES.filter((s) => (counts[s] ?? 0) > 0) as state (state)}
								<span class={stateTextClass(state)}>{formatCount(counts[state])} {STATE_LABEL[state].toLowerCase()}</span>
							{/each}
						{/if}
					</div>
				</a>
			{/each}
		</section>
	{/if}

	<div class="grid gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
		<!-- Needs attention -->
		<section class="rounded-md min-w-0 border border-sc-line bg-sc-panel" aria-labelledby="dm-attention">
			<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
				<h2 id="dm-attention" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Needs attention</h2>
				<span class="text-[10px] text-sc-ink3">most important first · live, then paper, pipeline, research</span>
				<a href={catalogHref({ state: ['late', 'breach', 'missing'] })} class="ml-auto text-[10px] text-sc-ink2 hover:text-sc-ink">All problems →</a>
			</header>
			<SectionState state={$slaCensus} what="The attention list" endpoint="GET /api/data/sla" rows={6} on:retry={() => loadSlaCensus({ force: true })}>
				{#if !groups.length}
					<p class="px-4 py-6 text-[12px] text-sc-ink3">Nothing needs attention. Every series is within the lag its tier allows.</p>
				{:else}
					{#each groups as group (group.tier)}
						<div class="border-b border-sc-line last:border-b-0" data-testid="attention-{group.tier}">
							<div class="flex items-center gap-2 bg-sc-panel px-3 py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">
								<span class="text-sc-ink2" title={TIER_HELP[group.tier]}>{TIER_LABEL[group.tier]}</span>
								<span class="text-sc-ink3">{plural(group.total, 'series', 'series')}</span>
								{#if group.hidden > 0}
									<a href={catalogHref({ tier: group.tier, state: ['late', 'breach', 'missing'] })} class="ml-auto font-normal normal-case tracking-normal text-sc-ink3 hover:text-sc-ink">
										+{formatCount(group.hidden)} more in the catalog →</a>
								{/if}
							</div>
							{#if group.tier === 'idle' && !group.rows.length}
								<p class="px-3 py-2 text-[11px] text-sc-ink3">Idle series nothing reads. The collector catches up on them when it has spare capacity.</p>
							{/if}
							{#each group.rows as row (idOf(row))}
								{@const id = idOf(row)}
								<div class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 hover:bg-sc-ink/[0.02]">
									<StateChip state={row.sla.state} sla={row.sla} />
									<div class="min-w-0 flex-1">
										<div class="flex flex-wrap items-baseline gap-x-2">
											<a href={seriesHref(row)} class="text-[12px] font-bold text-sc-ink hover:underline">{row.display_symbol}</a>
											<span class="font-mono text-[11px] text-sc-ink2">{row.timeframe}</span>
											<span class="text-[10px] text-sc-ink3">{streamLabel(row.stream).toLowerCase()}{row.venue !== 'canonical' ? ` · ${row.venue}` : ''}</span>
										</div>
										<div class="text-[11px] text-sc-ink2">{whyText(row)}</div>
									</div>
									<div class="flex shrink-0 items-center gap-1.5">
										{#if row.refreshable === false}
											<span class="max-w-[280px] text-right text-[10px] leading-tight text-sc-ink3">{row.refresh_note ?? 'A refresh can’t fetch this series'}</span>
										{:else if pending[id] === 'queued'}
											<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sky-300">Queued ✓</span>
										{:else}
											<button type="button" on:click={() => refreshRow(row)} disabled={pending[id] === 'sending'}
												class="rounded-md border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink hover:border-sc-ink hover:text-sc-ink disabled:opacity-40">
												{pending[id] === 'sending' ? 'Sending…' : row.sla.state === 'missing' ? 'Download now' : 'Refresh now'}</button>
										{/if}
										<a href={seriesHref(row)} class="px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 hover:text-sc-ink">Details →</a>
									</div>
								</div>
							{/each}
						</div>
					{/each}
				{/if}
			</SectionState>
			{#if attention.status === 'unavailable' && census}
				<p class="border-t border-sc-line px-3 py-1.5 text-[10px] text-sc-ink3">Live and paper rows come from the catalog, which is not available yet; this list shows the census’s most overdue series only.</p>
			{/if}
		</section>

		<div class="min-w-0 space-y-3">
			<!-- Sources -->
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-sources">
				<header class="flex items-center border-b border-sc-line px-3 py-1.5">
					<h2 id="dm-sources" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Sources</h2>
				</header>
				<SectionState state={venues} what="Source health" endpoint="GET /api/data/venues" on:retry={loadVenues}>
					{#each venues.data ?? [] as venue (venue.venue)}
						{@const status = VENUE_STATUS[venue.status] ?? VENUE_STATUS.unknown}
						<div class="border-b border-sc-line px-3 py-2 last:border-b-0" title={`Affects: ${venue.affects}`}>
							<div class="flex items-center gap-2">
								<span class="h-1.5 w-1.5 shrink-0 {status.dot}" aria-hidden="true"></span>
								<span class="text-[12px] text-sc-ink">{venue.label}</span>
								<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {status.text}">{status.label}</span>
								<span class="ml-auto text-[10px] text-sc-ink3" title={formatUtc(venue.last_success_at)}>
									{venue.last_success_at ? `ok ${formatRelative(venue.last_success_at, $clock)}` : 'never succeeded'}</span>
							</div>
							<div class="pl-3.5 text-[10px] text-sc-ink3">{venue.role}</div>
							{#if venue.status !== 'healthy'}
								{#if venue.last_error}<div class="pl-3.5 text-[10px] {status.text}">{venue.last_error}{venue.consecutive_failures > 1 ? ` · ${venue.consecutive_failures} failures in a row` : ''}</div>{/if}
								<div class="pl-3.5 text-[10px] text-sc-ink2">Affects: {venue.affects}</div>
							{/if}
						</div>
					{:else}
						<p class="px-3 py-3 text-[11px] text-sc-ink3">No sources reported.</p>
					{/each}
				</SectionState>
			</section>

			<!-- Collection -->
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-collector">
				<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
					<h2 id="dm-collector" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Collection</h2>
					{#if collector.data}
						<span class="ml-auto font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {collector.data.enabled ? 'text-emerald-400' : 'text-amber-400'}">{collector.data.enabled ? 'On' : 'Off'}</span>
					{/if}
				</header>
				<SectionState state={collector} what="Collector status" endpoint="GET /api/data/collector" on:retry={loadCollector}>
					{#if collector.data}
						{@const c = collector.data}
						<div class="space-y-1.5 px-3 py-2 text-[11px] text-sc-ink2">
							{#if c.last_tick}
								<div>
									<span class="text-sc-ink3">Last run</span>
									<span title={formatUtc(c.last_tick.finished_at, { seconds: true })}>{formatRelative(c.last_tick.finished_at, $clock)}</span>:
									<span class="font-mono tabular-nums">refreshed {formatCount(c.last_tick.refreshed)} · +{formatCount(c.last_tick.bars_added)} bars{#if c.last_tick.failed} · <span class="text-amber-400">{c.last_tick.failed} failed</span>{/if}{#if c.last_tick.deferred} · {formatCount(c.last_tick.deferred)} left for the next run{/if}</span>
								</div>
							{:else}
								<div class="text-sc-ink3">Has not run yet.</div>
							{/if}
							<div>
								<span class="text-sc-ink3">Next run</span>
								{c.next_tick_at ? formatRelative(c.next_tick_at, $clock, { upcoming: true }) : '—'} · every {formatDuration(c.tick_seconds)} · {formatCount(c.queue_depth)} waiting
							</div>
							<div><span class="text-sc-ink3">Refreshed in the last hour</span> <span class="font-mono tabular-nums">{formatCount(c.refreshed_last_hour)}</span></div>
							<div title="Refreshes per hour needed to keep every non-frozen series inside its allowance, against what the request budget allows">
								<div class="flex items-baseline gap-2">
									<span class="text-sc-ink3">Demand</span>
									<span class="font-mono tabular-nums {capacityShort ? 'text-amber-400' : ''}">needs ~{formatCount(c.demand_per_hour)}/h · can do {formatCount(c.capacity_per_hour)}/h</span>
								</div>
								<div class="mt-1 h-1 bg-sc-raise">
									<div class="h-full {capacityShort ? 'bg-amber-400/80' : 'bg-sc-ink3'}" style="width: {Math.min(100, (c.demand_per_hour / Math.max(1, c.capacity_per_hour)) * 100)}%"></div>
								</div>
								{#if capacityShort}<div class="mt-1 text-[10px] text-amber-400">More series need refreshing than the budget allows; some will fall behind. Raise the request budget in Settings → Data.</div>{/if}
							</div>
							{#if c.budget.length}
								<div class="grid grid-cols-[auto_1fr_auto] items-center gap-x-2 gap-y-0.5 pt-1 text-[10px]">
									{#each c.budget as b (b.venue)}
										<span class="text-sc-ink3">{b.venue}</span>
										<div class="h-1 bg-sc-raise"><div class="h-full bg-sc-line2" style="width: {Math.min(100, (b.used_last_minute / Math.max(1, b.limit_per_minute)) * 100)}%"></div></div>
										<span class="font-mono tabular-nums text-sc-ink2">{b.used_last_minute}/{b.limit_per_minute} per min</span>
									{/each}
								</div>
							{/if}
						</div>
					{/if}
				</SectionState>
				{#if plan.data}
					{@const p = plan.data}
					<div class="border-t border-sc-line px-3 py-2 text-[11px] text-sc-ink2" data-testid="dm-plan-line">
						<span class="text-sc-ink3">Research universe</span>
						{formatCount(p.present_series)} of {formatCount(p.planned_series)} planned series stored{#if p.planned_series > p.present_series} · <span class="text-amber-400">{formatCount(p.planned_series - p.present_series)} missing</span>{/if}
						{#if p.seed_job && (p.seed_job.status === 'failed' || p.seed_job.status === 'interrupted')}
							<div class="mt-1 text-[10px] text-amber-400">
								The last seed stopped {formatRelative(p.seed_job.finished_at ?? p.seed_job.updated_at, $clock)}: {errorText(p.seed_job.error?.code)}.
								<button type="button" on:click={resumeSeed} disabled={seeding} class="ml-1 text-sc-ink underline disabled:opacity-50">{seeding ? 'Starting…' : 'Resume the seed'}</button>
							</div>
						{:else if p.planned_series > p.present_series}
							<a href="{DM}/coverage" class="ml-1 text-sc-ink2 hover:text-sc-ink">See them →</a>
						{/if}
					</div>
				{/if}
			</section>

			<!-- Storage glance -->
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-storage">
				<header class="flex items-center border-b border-sc-line px-3 py-1.5">
					<h2 id="dm-storage" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Storage</h2>
					<a href="{DM}/storage" class="ml-auto text-[10px] text-sc-ink2 hover:text-sc-ink">Open →</a>
				</header>
				<SectionState state={storage} what="The storage inventory" endpoint="GET /api/data/storage" rows={2} on:retry={loadStorage}>
					{#if storage.data}
						{@const s = storage.data}
						<div class="grid grid-cols-2 gap-x-3 gap-y-1.5 px-3 py-2 text-[11px]">
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Lake</div><div class="font-mono tabular-nums text-sc-ink">{formatBytes(s.lake.bytes)}</div><div class="text-[10px] text-sc-ink3">{formatCount(s.lake.series)} series</div></div>
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Disk free</div>
								<div class="font-mono tabular-nums {s.disk.free_bytes < s.disk.min_free_gb * 1024 ** 3 ? 'text-red-400' : 'text-sc-ink'}">{formatBytes(s.disk.free_bytes)}</div>
								<div class="text-[10px] text-sc-ink3">of {formatBytes(s.disk.total_bytes)}</div></div>
							<div class="col-span-2 text-[11px] text-sc-ink2">
								{#if reclaimable > 0}
									<a href="{DM}/storage" class="hover:text-sc-ink"><span class="font-mono tabular-nums text-sc-ink">{formatBytes(reclaimable)}</span> can be reclaimed (backups, legacy files, old revisions) →</a>
								{:else}
									Nothing to reclaim.
								{/if}
							</div>
						</div>
					{/if}
				</SectionState>
			</section>

			<!-- Incidents -->
			<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-incidents">
				<header class="flex items-center border-b border-sc-line px-3 py-1.5">
					<h2 id="dm-incidents" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Incidents · 24 h</h2>
					<a href="{DM}/log" class="ml-auto text-[10px] text-sc-ink2 hover:text-sc-ink">Log →</a>
				</header>
				<SectionState state={incidents} what="Recent incidents" endpoint="GET /api/data/log" rows={2} on:retry={loadIncidents}>
					{#each incidents.data ?? [] as entry (entry.id)}
						<div class="flex gap-2 border-b border-sc-line px-3 py-1.5 text-[11px] last:border-b-0">
							<span class="w-14 shrink-0 text-[10px] text-sc-ink3" title={formatUtc(entry.ts, { seconds: true })}>{formatRelative(entry.ts, $clock)}</span>
							<span class="shrink-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {entry.level === 'error' ? 'text-red-400' : entry.level === 'warning' ? 'text-amber-400' : 'text-sc-ink2'}">{entry.level === 'warning' ? 'warn' : entry.level}</span>
							<span class="min-w-0 text-sc-ink2">{entry.message}</span>
						</div>
					{:else}
						<p class="px-3 py-3 text-[11px] text-sc-ink3">No incidents in the last 24 hours.</p>
					{/each}
				</SectionState>
			</section>
		</div>
	</div>
</div>
