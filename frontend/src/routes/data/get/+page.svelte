<script lang="ts">
	// Get data: a guided flow. What (a market, the research universe, a file) →
	// the market's venue, timeframes, history and perp add-ons with a live
	// estimate → review → start. Downloads run as jobs; you can leave the page.
	import { onDestroy, onMount, tick } from 'svelte';
	import { page } from '$app/stores';
	import {
		estimateDownloads,
		getAcquireTargets,
		getJob,
		getUniversePlanDiff,
		resolveIdentity,
		seedUniverse,
		startDownloads,
	} from '$lib/api/dataManager';
	import type {
		DataJob,
		DataStream,
		DownloadEstimateResponse,
		SymbolCandidate,
		UniversePlanDiff,
		VenueTarget,
	} from '$lib/api/dataManagerTypes';
	import JobRow from '$lib/components/data-manager/JobRow.svelte';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { runAction } from '$lib/components/data-manager/actions';
	import {
		errorText,
		exchangeLabel,
		formatBytes,
		formatCompact,
		formatCount,
		formatDuration,
		formatRelative,
		formatUtc,
		historyLength,
		plural,
		streamLabel,
		VENUE_HELP,
	} from '$lib/components/data-manager/format';
	import { DM, seriesHref } from '$lib/components/data-manager/links';
	import {
		buildDownloadItems,
		DOWNLOAD_STREAMS,
		historyText,
		parseGetDataQuery,
		validateMarketDraft,
		type GetDataPrefill,
		type HistoryChoice,
		type MarketDraft,
	} from '$lib/components/data-manager/wizard';
	import { clock, createRequestGuard, jobsSummary, loading, settle, type Loadable } from '$lib/stores/dataManager';

	const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w'];
	/** Deribit publishes implied volatility (DVOL) for these bases only. */
	const IV_BASES = ['BTC', 'ETH'];
	type Step = 'what' | 'market' | 'universe' | 'review' | 'started';

	let step: Step = 'what';
	// Market
	let query = '';
	let searchTimer: ReturnType<typeof setTimeout> | undefined;
	let candidates: Loadable<SymbolCandidate[]> | null = null;
	let candidate: SymbolCandidate | null = null;
	let targets: Loadable<VenueTarget[]> | null = null;
	let venue = 'canonical';
	let timeframes: string[] = ['1h'];
	let historyMode: 'all' | 'recent' | 'range' = 'all';
	let recentAmount = 3;
	let recentUnit: 'years' | 'days' = 'years';
	/** YYYY-MM-DD from the date inputs, or an exact ISO timestamp from a deep link. */
	let rangeStart = '';
	let rangeEnd = '';
	let streams: DataStream[] = ['funding', 'oi'];
	let prefill: GetDataPrefill | null = null;
	let estimate: Loadable<DownloadEstimateResponse> | null = null;
	let estimateTimer: ReturnType<typeof setTimeout> | undefined;
	let starting = false;
	let started: DataJob[] = [];

	// The started jobs follow the summary poll (fast while anything runs) until
	// they land, so "queued" turns into done or failed here too.
	const LANDED = new Set<DataJob['status']>(['succeeded', 'failed', 'cancelled', 'interrupted']);
	let startedSeenAt = -1;
	$: if (started.length && $jobsSummary.at !== startedSeenAt) {
		startedSeenAt = $jobsSummary.at;
		void followStarted();
	}
	async function followStarted() {
		const open = started.filter((job) => !LANDED.has(job.status));
		if (!open.length) return;
		const fresh = new Map((await Promise.all(open.map((job) => getJob(job.id).catch(() => job)))).map((job) => [job.id, job]));
		started = started.map((job) => fresh.get(job.id) ?? job);
	}
	// Universe
	let plan: Loadable<UniversePlanDiff> | null = null;
	let seeding = false;
	let searchInput: HTMLInputElement | undefined;

	const searchGuard = createRequestGuard();
	const targetGuard = createRequestGuard();
	const estimateGuard = createRequestGuard();

	// Deep links (the readiness "fix" buttons, a symbol with nothing stored)
	// open the market step with the choice filled in.
	onMount(() => {
		// Warm the exchanges' market lists (the first listing check after a restart
		// or an hour loads six of them, ~10 s) while the user is still choosing.
		void getAcquireTargets('BTC-USDT').catch(() => undefined);
		prefill = parseGetDataQuery($page.url.searchParams);
		if (!prefill) return;
		query = prefill.symbol;
		if (prefill.timeframes.length) timeframes = prefill.timeframes;
		if (prefill.streams) streams = prefill.streams;
		const h = prefill.history;
		if (h?.mode === 'all') historyMode = 'all';
		else if (h?.mode === 'days') {
			historyMode = 'recent';
			recentUnit = 'days';
			recentAmount = h.days;
		} else if (h?.mode === 'range') {
			historyMode = 'range';
			rangeStart = h.start;
			rangeEnd = h.end;
		}
		void goMarket();
		void search(true);
	});
	onDestroy(() => {
		clearTimeout(searchTimer);
		clearTimeout(estimateTimer);
		searchGuard.cancel();
		targetGuard.cancel();
		estimateGuard.cancel();
	});

	async function goMarket() {
		step = 'market';
		await tick();
		searchInput?.focus();
	}
	async function goUniverse() {
		step = 'universe';
		plan = loading();
		plan = await settle(getUniversePlanDiff());
	}

	function onQuery() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => search(false), 220);
	}
	async function search(pickExact: boolean) {
		const q = query.trim();
		if (!q) {
			candidates = null;
			return;
		}
		const { signal, current } = searchGuard.next();
		candidates = loading();
		const result = await settle(resolveIdentity(q, signal).then((r) => r.candidates));
		if (!current()) return;
		candidates = result;
		const wanted = q.toUpperCase().replace(/[/_ ]+/g, '-');
		const exact = result.data?.find((c) => c.symbol === wanted || c.aliases.some((a) => a.toUpperCase() === q.toUpperCase()));
		if (pickExact && exact) void choose(exact);
	}

	async function choose(next: SymbolCandidate) {
		candidate = next;
		const { signal, current } = targetGuard.next();
		targets = loading();
		const result = await settle(getAcquireTargets(next.symbol, signal).then((r) => r.targets));
		if (!current()) return;
		targets = result;
		const listed = result.data?.filter((t) => t.listed) ?? [];
		const linked = prefill?.symbol === next.symbol && prefill.venue ? listed.find((t) => t.venue === prefill?.venue) : undefined;
		venue = linked?.venue ?? listed.find((t) => t.canonical)?.venue ?? listed[0]?.venue ?? 'canonical';
		timeframes = timeframes.length ? timeframes : ['1h'];
	}

	$: target = targets?.data?.find((t) => t.venue === venue) ?? null;
	$: history = (
		historyMode === 'recent'
			? recentUnit === 'days'
				? { mode: 'days', days: Number(recentAmount) }
				: { mode: 'years', years: Number(recentAmount) }
			: historyMode === 'range'
				? { mode: 'range', start: rangeStart, end: rangeEnd }
				: { mode: 'all' }
	) as HistoryChoice;
	$: base = candidate?.base ?? candidate?.symbol.split('-')[0] ?? '';
	$: addons = DOWNLOAD_STREAMS.filter((s) => s !== 'iv' || IV_BASES.includes(base));
	$: draft = {
		symbol: candidate?.symbol ?? '',
		venue,
		timeframes,
		history,
		streams: target?.market === 'perp' ? streams.filter((s) => addons.includes(s)) : [],
	} satisfies MarketDraft;
	$: errors = validateMarketDraft(draft, target, $clock);
	$: items = errors.length ? [] : buildDownloadItems(draft, $clock);
	$: storedFor = (tf: string) => candidate?.stored.find((s) => s.stream === 'ohlcv' && s.timeframe === tf && s.venue === venue);

	// Estimate whenever the choice is complete.
	$: estimateKey = JSON.stringify(items);
	let estimatedKey = '';
	$: if (step === 'market' && estimateKey !== estimatedKey) {
		estimatedKey = estimateKey;
		clearTimeout(estimateTimer);
		if (!items.length) estimate = null;
		else estimateTimer = setTimeout(runEstimate, 400);
	}
	async function runEstimate() {
		const { signal, current } = estimateGuard.next();
		estimate = estimate?.data ? { ...estimate, status: 'ready' } : loading();
		const next = await settle(estimateDownloads(items, signal), estimate);
		if (current()) estimate = next;
	}

	function toggleTf(tf: string) {
		timeframes = timeframes.includes(tf) ? timeframes.filter((t) => t !== tf) : [...timeframes, tf].sort((a, b) => TIMEFRAMES.indexOf(a) - TIMEFRAMES.indexOf(b));
	}
	function toggleAddon(s: DataStream) {
		streams = streams.includes(s) ? streams.filter((x) => x !== s) : [...streams, s];
	}

	async function start() {
		starting = true;
		const result = await runAction('Starting downloads', () => startDownloads(items), {
			success: (r) => `Started ${plural(r.jobs.length, 'download')}. Progress is in Jobs.`,
		});
		starting = false;
		if (result) {
			started = result.jobs;
			step = 'started';
		}
	}

	async function seed() {
		seeding = true;
		const result = await runAction('Starting the universe seed', () => seedUniverse(), {
			success: (r) => (r.status === 'already_running' ? 'The universe seed is already running.' : 'Started the universe seed. Progress is in Jobs.'),
		});
		seeding = false;
		if (result) {
			started = [result.job];
			step = 'started';
		}
	}

	function reset() {
		step = 'what';
		started = [];
		estimate = null;
		estimatedKey = '';
	}

	$: e = estimate?.data ?? null;
	$: blocked = e?.estimates.filter((x) => x.blocked) ?? [];
	$: warnings = e ? [...new Set([...e.warnings, ...e.estimates.flatMap((x) => x.warnings)])] : [];
	$: canStart = !errors.length && !!e && blocked.length < items.length;
	const stepClass = (on: boolean, done: boolean) => `flex items-center gap-1.5 ${on ? 'text-sc-ink' : done ? 'text-sc-ink2' : 'text-sc-ink4'}`;
</script>

<svelte:head><title>Data · Get data | Forven</title></svelte:head>

<div class="mx-auto max-w-5xl space-y-3 p-4 pb-24">
	<div class="flex items-center justify-between gap-3">
		<h1 class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Get data</h1>
		<ol class="flex items-center gap-3 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]" aria-label="Steps">
			<li class={stepClass(step === 'what', step !== 'what')}><span class="font-mono">1</span> What</li>
			<li aria-hidden="true" class="text-sc-ink4">→</li>
			<li class={stepClass(step === 'market' || step === 'universe', step === 'review' || step === 'started')}><span class="font-mono">2</span> Choose</li>
			<li aria-hidden="true" class="text-sc-ink4">→</li>
			<li class={stepClass(step === 'review', step === 'started')}><span class="font-mono">3</span> Review</li>
		</ol>
	</div>

	{#if step === 'what'}
		<div class="grid gap-2 md:grid-cols-3">
			<button type="button" on:click={goMarket} class="rounded-md border border-sc-line bg-sc-panel p-4 text-left transition-colors hover:border-sc-ink">
				<div class="text-[13px] font-bold text-sc-ink">A market</div>
				<p class="mt-1 text-[11px] leading-relaxed text-sc-ink2">One symbol, the timeframes and history you need, plus funding, open interest and basis for perps. Shows the size and time before anything starts.</p>
			</button>
			<button type="button" on:click={goUniverse} class="rounded-md border border-sc-line bg-sc-panel p-4 text-left transition-colors hover:border-sc-ink">
				<div class="text-[13px] font-bold text-sc-ink">A research universe</div>
				<p class="mt-1 text-[11px] leading-relaxed text-sc-ink2">The most liquid perps, kept current for research. See what the plan includes, what is missing, and bring it up to plan.</p>
			</button>
			<a href="{DM}/import" class="rounded-md border border-sc-line bg-sc-panel p-4 text-left transition-colors hover:border-sc-ink">
				<div class="text-[13px] font-bold text-sc-ink">A file</div>
				<p class="mt-1 text-[11px] leading-relaxed text-sc-ink2">Import candles from a CSV. You check the columns, time zone and timeframe, and see how it overlaps what is stored, before anything is written.</p>
			</a>
		</div>
		<p class="text-[11px] text-sc-ink3">New here? <a href="{DM}/setup" class="text-sc-ink underline">Start from a preset</a> with real size and time estimates.</p>
	{:else if step === 'universe'}
		<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-universe">
			<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
				<h2 id="dm-universe" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Research universe</h2>
				<button type="button" on:click={reset} class="ml-auto text-[12px] text-sc-ink3 hover:text-sc-ink">← Back</button>
			</header>
			{#if plan}
				<SectionState state={plan} what="The universe plan" endpoint="GET /api/data/universe/plan-diff" rows={4} on:retry={goUniverse}>
					{#if plan.data}
						{@const p = plan.data}
						<div class="space-y-3 px-3 py-3 text-[12px]">
							<p class="text-sc-ink">
								{#if p.enabled}The plan keeps the top <span class="text-sc-ink">{p.size}</span> perps ({p.asset_classes.join(', ')}) current:
									<span class="font-mono text-sc-ink">{formatCount(p.present_series)}</span> of <span class="font-mono text-sc-ink">{formatCount(p.planned_series)}</span> planned series are stored.
								{:else}The research universe is turned off.{/if}
								<a href="/settings#data" class="ml-1 text-sc-ink2 underline hover:text-sc-ink">Change it in Settings → Data</a>
							</p>
							{#if p.seed_job}
								<div class="border border-sc-line"><JobRow job={p.seed_job} /></div>
							{/if}
							{#if p.missing.length}
								<div>
									<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Missing ({formatCount(p.planned_series - p.present_series)} series)</div>
									<div class="mt-1 flex flex-wrap gap-1">
										{#each p.missing as m (m.symbol)}
											<span class="border border-sc-line px-1.5 py-0.5 font-mono text-[10px] text-sc-ink2" title="Rank {m.rank + 1} · {m.asset_class}">{m.symbol.replace('-', '/')} <span class="text-sc-ink3">{m.timeframes.join(' ')}</span></span>
										{/each}
									</div>
								</div>
							{:else}
								<p class="text-emerald-400">Every planned series is stored.</p>
							{/if}
							{#if p.extra.length}
								<p class="text-[11px] text-sc-ink3">{plural(p.extra.length, 'symbol')} fell out of the plan but are still stored: {p.extra.map((x) => x.symbol).join(', ')}. They stay until you delete them.</p>
							{/if}
							<div class="flex items-center gap-2">
								<button type="button" class="terminal-button-primary text-[12px]" disabled={seeding || !p.enabled || !p.missing.length} on:click={seed}>
									{seeding ? 'Starting…' : p.seed_job?.status === 'interrupted' || p.seed_job?.status === 'failed' ? 'Resume the seed' : 'Bring the universe up to plan'}</button>
								<span class="text-[10px] text-sc-ink3">Runs as one job from Binance Vision archives; it resumes where it stopped.</span>
							</div>
						</div>
					{/if}
				</SectionState>
			{/if}
		</section>
	{:else if step === 'market' || step === 'review'}
		<div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
			<div class="min-w-0 space-y-3">
				<!-- 1 Market -->
				<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-get-market">
					<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
						<h2 id="dm-get-market" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Market</h2>
						<button type="button" on:click={reset} class="ml-auto text-[12px] text-sc-ink3 hover:text-sc-ink">← Start over</button>
					</header>
					<div class="space-y-2 px-3 py-2.5">
						<input bind:this={searchInput} bind:value={query} on:input={onQuery} disabled={step === 'review'} type="search" spellcheck="false"
							placeholder="BTC, SOLUSDT, eth/usdt, XAU…" aria-label="Market" class="terminal-input font-mono text-[13px]" />
						{#if candidates && step === 'market'}
							{#if candidates.status === 'loading'}
								<p class="text-[11px] text-sc-ink3">Looking up “{query.trim()}”…</p>
							{:else if candidates.status === 'ready'}
								{#if candidates.data?.length}
									<div class="grid gap-1 sm:grid-cols-2" role="radiogroup" aria-label="Matching markets">
										{#each candidates.data as c (c.symbol)}
											{@const on = candidate?.symbol === c.symbol}
											<button type="button" role="radio" aria-checked={on} on:click={() => choose(c)}
												class="rounded-md border px-2.5 py-1.5 text-left transition-colors {on ? 'border-sc-ink bg-sc-ink/[0.06]' : 'border-sc-line hover:border-sc-line2'}">
												<div class="flex items-baseline gap-2">
													<span class="text-[12px] font-bold text-sc-ink">{c.display_symbol}</span>
													{#if c.asset_class !== 'crypto'}<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{c.asset_class}</span>{/if}
													{#if c.delisted}<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">delisted</span>{/if}
												</div>
												<div class="truncate text-[10px] text-sc-ink3">
													{c.stored.length ? `stored: ${[...new Set(c.stored.map((s) => (s.stream === 'ohlcv' ? s.timeframe : streamLabel(s.stream).toLowerCase())))].join(', ')}` : 'nothing stored yet'}
												</div>
											</button>
										{/each}
									</div>
								{:else}
									<p class="text-[11px] text-sc-ink3">No market matches “{query.trim()}”. Try the base asset alone (e.g. “SOL”).</p>
								{/if}
							{:else}
								<SectionState state={candidates} what="Symbol lookup" endpoint="GET /api/data/identity/resolve" rows={2} on:retry={() => search(false)} />
							{/if}
						{/if}
					</div>
				</section>

				{#if candidate}
					<!-- 2 Venue -->
					<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-get-venue">
						<header class="border-b border-sc-line px-3 py-1.5"><h2 id="dm-get-venue" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Where from</h2></header>
						{#if targets?.status === 'loading'}
							<p class="px-3 pt-2 text-[11px] text-sc-ink3">Checking which exchanges list {candidate?.display_symbol ?? 'it'}… the first check in an hour loads each exchange's market list and can take a few seconds.</p>
						{/if}
						{#if targets}
							<SectionState state={targets} what="Download venues" endpoint="GET /api/data/acquire/targets" rows={3} on:retry={() => candidate && choose(candidate)}>
								<div class="space-y-1 px-3 py-2" role="radiogroup" aria-label="Venue">
									{#each targets.data ?? [] as t (t.venue)}
										{@const on = venue === t.venue}
										<label class="flex cursor-pointer items-start gap-2 border px-2.5 py-1.5 transition-colors {!t.listed ? 'cursor-not-allowed opacity-40' : on ? 'border-sc-ink bg-sc-ink/[0.04]' : 'border-sc-line hover:border-sc-line2'}">
											<input type="radio" name="dm-venue" value={t.venue} bind:group={venue} disabled={!t.listed || step === 'review'} class="mt-0.5 accent-white" />
											<div class="min-w-0 flex-1">
												<div class="flex flex-wrap items-baseline gap-2">
													<span class="text-[12px] text-sc-ink">{exchangeLabel(t.exchange)} {t.market}</span>
													<span class="border px-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {t.destination === 'canonical' ? 'border-emerald-900 text-emerald-400' : 'border-sky-900 text-sky-300'}">{t.destination === 'canonical' ? 'research series' : 'separate venue series'}</span>
													{#if !t.listed}<span class="text-[10px] text-sc-ink2">not listed there</span>{/if}
												</div>
												<p class="text-[10px] text-sc-ink3">{t.note}</p>
											</div>
										</label>
									{/each}
									<p class="pt-1 text-[10px] leading-relaxed text-sc-ink3">{VENUE_HELP}</p>
								</div>
							</SectionState>
						{/if}
					</section>

					<!-- 3 Timeframes, history, add-ons -->
					<section class="rounded-md border border-sc-line bg-sc-panel" aria-labelledby="dm-get-what">
						<header class="border-b border-sc-line px-3 py-1.5"><h2 id="dm-get-what" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Timeframes & history</h2></header>
						<div class="space-y-3 px-3 py-2.5">
							<div>
								<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Timeframes</div>
								<div class="mt-1 flex flex-wrap gap-1" role="group" aria-label="Timeframes">
									{#each TIMEFRAMES as tf}
										{@const stored = storedFor(tf)}
										<button type="button" on:click={() => toggleTf(tf)} aria-pressed={timeframes.includes(tf)} disabled={step === 'review'}
											title={stored ? `${stored.rows.toLocaleString('en-US')} bars stored, last ${formatUtc(stored.last_ts)}` : 'Nothing stored yet'}
											class="rounded-md min-w-[52px] border px-2 py-1 text-center font-mono text-[11px] transition-colors {timeframes.includes(tf) ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">
											{tf}{#if stored}<span class="block text-[8px] uppercase tracking-wider {timeframes.includes(tf) ? 'text-sc-ink3' : 'text-emerald-500/80'}">stored</span>{/if}
										</button>
									{/each}
								</div>
							</div>
							<fieldset>
								<legend class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">History (UTC)</legend>
								<div class="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12px] text-sc-ink">
									<label class="flex items-center gap-1.5"><input type="radio" bind:group={historyMode} value="all" disabled={step === 'review'} class="accent-white" /> All available</label>
									<span class="flex items-center gap-1.5">
										<label class="flex items-center gap-1.5"><input type="radio" bind:group={historyMode} value="recent" disabled={step === 'review'} class="accent-white" /> Last</label>
										<input type="number" bind:value={recentAmount} min="1" step="1" on:focus={() => (historyMode = 'recent')} disabled={step === 'review'}
											aria-label="How much recent history" class="rounded-md w-16 border border-sc-line2 bg-sc-bg px-1.5 py-0.5 font-mono text-[12px] text-sc-ink outline-none focus:border-sc-ink" />
										<select bind:value={recentUnit} on:focus={() => (historyMode = 'recent')} disabled={step === 'review'} aria-label="Unit"
											class="rounded-md border border-sc-line2 bg-sc-bg px-1 py-0.5 text-[11px] text-sc-ink outline-none focus:border-sc-ink">
											<option value="years">years</option><option value="days">days</option>
										</select>
									</span>
									<label class="flex items-center gap-1.5"><input type="radio" bind:group={historyMode} value="range" disabled={step === 'review'} class="accent-white" /> From</label>
									<input type="date" value={rangeStart.slice(0, 10)} on:input={(e) => (rangeStart = e.currentTarget.value)} on:focus={() => (historyMode = 'range')}
										max={rangeEnd.slice(0, 10) || undefined} disabled={step === 'review'} aria-label="Start date (UTC)"
										class="rounded-md border border-sc-line2 bg-sc-bg px-1.5 py-0.5 font-mono text-[11px] text-sc-ink outline-none [color-scheme:dark] focus:border-sc-ink" />
									<span class="-ml-2 text-sc-ink3">to</span>
									<input type="date" value={rangeEnd.slice(0, 10)} on:input={(e) => (rangeEnd = e.currentTarget.value)} on:focus={() => (historyMode = 'range')}
										min={rangeStart.slice(0, 10) || undefined} disabled={step === 'review'} aria-label="End date (UTC)"
										class="rounded-md -ml-2 border border-sc-line2 bg-sc-bg px-1.5 py-0.5 font-mono text-[11px] text-sc-ink outline-none [color-scheme:dark] focus:border-sc-ink" />
									{#if historyMode === 'range' && (rangeStart.length > 10 || rangeEnd.length > 10)}
										<span class="text-[10px] text-sc-ink3">exactly {rangeStart} → {rangeEnd}</span>
									{/if}
								</div>
							</fieldset>
							{#if target?.market === 'perp'}
								<div>
									<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Perp add-ons</div>
									<div class="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-sc-ink">
										{#each addons as s}
											<label class="flex items-center gap-1.5"><input type="checkbox" checked={streams.includes(s)} on:change={() => toggleAddon(s)} disabled={step === 'review'} class="accent-white" /> {streamLabel(s)}</label>
										{/each}
									</div>
									<p class="mt-1 text-[10px] text-sc-ink3">Collected once alongside the {timeframes.includes('1h') ? '1h' : timeframes[0] ?? ''} candles; strategies see them as columns.</p>
								</div>
							{/if}
						</div>
					</section>
				{/if}
			</div>

			<!-- Estimate and review -->
			<aside class="min-w-0 space-y-3">
				<section class="rounded-md border border-sc-line bg-sc-panel lg:sticky lg:top-3" aria-labelledby="dm-get-est">
					<header class="border-b border-sc-line px-3 py-1.5"><h2 id="dm-get-est" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">{step === 'review' ? 'Review' : 'Estimate'}</h2></header>
					<div class="space-y-2 px-3 py-2.5 text-[11px]">
						{#if errors.length}
							<ul class="space-y-0.5 text-sc-ink3">{#each errors as error}<li>· {error}</li>{/each}</ul>
						{:else if !estimate}
							<p class="text-sc-ink3">Estimating…</p>
						{:else if estimate.status === 'ready' && e}
							<p class="text-sc-ink2">{candidate?.display_symbol} · {historyText(history)}</p>
							<table class="w-full text-[10px]">
								<thead><tr class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3"><th class="py-0.5 text-left font-normal">TF</th><th class="py-0.5 text-left font-normal">Stored</th><th class="py-0.5 text-right font-normal">New bars</th><th class="py-0.5 text-right font-normal">Size</th><th class="py-0.5 text-right font-normal">Time</th></tr></thead>
								<tbody>
									{#each e.estimates as x (x.item.timeframe)}
										<tr class="border-t border-sc-line align-top">
											<td class="py-1 font-mono text-sc-ink">{x.item.timeframe}</td>
											<td class="py-1 text-sc-ink2" title={x.existing_first ? `${formatUtc(x.existing_first)} → ${formatUtc(x.existing_last)}` : ''}>{x.existing_rows ? `${formatCompact(x.existing_rows)} · ${historyLength(x.existing_first, x.existing_last)}` : '—'}</td>
											<td class="py-1 text-right font-mono tabular-nums text-sc-ink">{x.blocked ? '—' : formatCompact(x.new_bars_estimate)}</td>
											<td class="py-1 text-right font-mono tabular-nums text-sc-ink">{x.blocked ? '—' : formatBytes(x.bytes_estimate)}</td>
											<td class="py-1 text-right font-mono tabular-nums text-sc-ink">{x.blocked ? '—' : formatDuration(x.seconds_estimate)}</td>
										</tr>
										{#if x.blocked}<tr><td colspan="5" class="pb-1 text-red-400">{x.blocked}</td></tr>{/if}
									{/each}
								</tbody>
							</table>
							<div class="border-t border-sc-line pt-1.5">
								<div class="flex justify-between"><span class="text-sc-ink2">Total</span><span class="font-mono text-sc-ink">{formatBytes(e.total_bytes)} · about {formatDuration(e.total_seconds)}</span></div>
								<div class="flex justify-between text-[10px]"><span class="text-sc-ink3">Disk free</span><span class="font-mono {e.total_bytes > e.disk_free_bytes * 0.5 ? 'text-amber-400' : 'text-sc-ink2'}">{formatBytes(e.disk_free_bytes)}</span></div>
							</div>
							{#each warnings as warning}<p class="text-amber-400">{warning}</p>{/each}
							{#if target && target.destination === 'venue'}
								<p class="text-[10px] text-sky-300/80">Stored as a separate {exchangeLabel(target.exchange)} series; the research series is not touched.</p>
							{/if}
						{:else if estimate.status === 'loading'}
							<p class="text-sc-ink3">Estimating…</p>
						{:else}
							<SectionState state={estimate} what="The estimate" endpoint="POST /api/data/acquire/estimate" rows={2} on:retry={runEstimate} />
						{/if}
						{#if step === 'market'}
							<button type="button" class="terminal-button-primary w-full text-[12px]" disabled={!canStart} on:click={() => (step = 'review')}>Review →</button>
						{:else}
							<div class="flex gap-2">
								<button type="button" class="terminal-button text-[12px]" on:click={() => (step = 'market')}>← Change</button>
								<button type="button" class="terminal-button-primary flex-1 text-[12px]" disabled={!canStart || starting} on:click={start}>
									{starting ? 'Starting…' : `Start ${plural(items.length - blocked.length, 'download')}`}</button>
							</div>
							<p class="text-[10px] text-sc-ink3">Downloads run in the background as jobs. You can leave this page; the Jobs drawer shows progress.</p>
						{/if}
					</div>
				</section>
			</aside>
		</div>
	{:else if step === 'started'}
		<section class="border border-emerald-900/70 bg-emerald-500/[0.03] px-4 py-3" aria-live="polite">
			<h2 class="text-[14px] font-bold text-emerald-400">Started {plural(started.length, 'job')}</h2>
			<p class="mt-1 text-[11px] text-sc-ink2">They run in the background: you can leave this page. Progress is in the Jobs drawer (top right) and on the Jobs tab.</p>
			<div class="rounded-md mt-2 border border-sc-line bg-sc-panel">{#each started as job (job.id)}<JobRow {job} />{/each}</div>
			<div class="mt-3 flex flex-wrap gap-2">
				<button type="button" class="terminal-button text-[12px]" on:click={reset}>Get more data</button>
				<a href="{DM}/jobs" class="terminal-button text-[10px]">Open Jobs</a>
				{#if candidate && started[0]?.series[0]?.timeframe}
					<a href={seriesHref({ symbol: candidate.symbol, timeframe: started[0].series[0].timeframe, venue })} class="terminal-button text-[10px]">Open {candidate.display_symbol} {started[0].series[0].timeframe}</a>
				{/if}
				{#if started.some((j) => j.error)}<span class="text-[11px] text-red-400">{errorText(started.find((j) => j.error)?.error?.code)}</span>{/if}
				<span class="self-center text-[10px] text-sc-ink3">{formatRelative(started[0]?.created_at, $clock)}</span>
			</div>
		</section>
	{/if}
</div>
