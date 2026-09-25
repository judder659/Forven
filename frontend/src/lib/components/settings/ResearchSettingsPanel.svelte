<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	import type { ResearchSettings } from '$lib/api';

	export let draft: ResearchSettings;
	export let saving = false;
	export let onsave: ((event: CustomEvent<ResearchSettings>) => void) | undefined = undefined;

	const SOURCE_TYPE_OPTIONS = [
		'reddit',
		'youtube',
		'blog',
		'github',
		'forum',
		'book',
		'paper',
	] as const;
	const KNOWN_SOURCE_TYPES = new Set<string>(SOURCE_TYPE_OPTIONS);

	const dispatch = createEventDispatcher<{
		save: ResearchSettings;
	}>();

	const DEFAULT_MIN_FEED_COVERAGE_PCT = 50;

	$: minFeedCoveragePct = draft.candidate_min_feed_coverage_pct ?? DEFAULT_MIN_FEED_COVERAGE_PCT;

	function setMinFeedCoveragePct(value: number): void {
		draft = { ...draft, candidate_min_feed_coverage_pct: value };
	}

	const RESEARCH_HOLDOUT_DEFAULTS = {
		enabled: false,
		roll: 'quarterly' as 'quarterly' | 'manual',
		lag_quarters: 2,
		cutoff: '',
		established_at: '',
		paper_mode: 'enforce' as 'off' | 'observe' | 'enforce',
		min_trades: 5,
		max_family_shots: 3,
	};

	$: researchHoldout = {
		...RESEARCH_HOLDOUT_DEFAULTS,
		...(draft.research_holdout ?? {}),
	};

	function setResearchHoldout<K extends keyof typeof RESEARCH_HOLDOUT_DEFAULTS>(
		key: K,
		value: (typeof RESEARCH_HOLDOUT_DEFAULTS)[K],
	): void {
		const current = { ...RESEARCH_HOLDOUT_DEFAULTS, ...(draft.research_holdout ?? {}) };
		draft = { ...draft, research_holdout: { ...current, [key]: value } };
	}

	/** Mirrors research_holdout.current_cutoff: quarter start minus lag quarters (UTC). */
	function holdoutCutoffPreview(holdout: typeof RESEARCH_HOLDOUT_DEFAULTS, now = new Date()): string {
		if (holdout.roll === 'manual') return holdout.cutoff ? holdout.cutoff.slice(0, 10) : 'not set';
		const quarterStartMonth = Math.floor(now.getUTCMonth() / 3) * 3;
		const cutoff = new Date(Date.UTC(now.getUTCFullYear(), quarterStartMonth - 3 * holdout.lag_quarters, 1));
		return cutoff.toISOString().slice(0, 10);
	}

	function toggleSourceType(sourceType: (typeof SOURCE_TYPE_OPTIONS)[number], enabled: boolean): void {
		const nextSourceTypes = new Set(draft.allowed_external_source_types);
		if (enabled) {
			nextSourceTypes.add(sourceType);
		} else {
			nextSourceTypes.delete(sourceType);
		}
		const preservedCustomTypes = draft.allowed_external_source_types.filter((candidate) => !KNOWN_SOURCE_TYPES.has(candidate));
		draft = {
			...draft,
			allowed_external_source_types: [
				...SOURCE_TYPE_OPTIONS.filter((candidate) => nextSourceTypes.has(candidate)),
				...preservedCustomTypes,
			],
		};
	}

	function redditSource(): Record<string, unknown> {
		const sources = draft.research_sources && typeof draft.research_sources === 'object' ? draft.research_sources : {};
		const reddit = sources.reddit && typeof sources.reddit === 'object' ? sources.reddit : {};
		return {
			enabled: true,
			subs: ['algotrading', 'quant', 'options', 'thetagang', 'systematictrading'],
			client_id: null,
			client_secret: null,
			rate_limit_per_min: 30,
			...reddit,
		};
	}

	function setRedditSource(key: string, value: unknown): void {
		const currentSources = draft.research_sources && typeof draft.research_sources === 'object' ? draft.research_sources : {};
		draft = {
			...draft,
			research_sources: {
				...currentSources,
				reddit: {
					...redditSource(),
					[key]: value,
				},
			},
		};
	}

	function setRedditSubs(value: string): void {
		setRedditSource(
			'subs',
			value
				.split(',')
				.map((item) => item.trim())
				.filter(Boolean),
		);
	}

	function emitSave(): void {
		const detail = structuredClone(draft);
		dispatch('save', detail);
		onsave?.(new CustomEvent<ResearchSettings>('save', { detail }));
	}

	$: customSourceTypes = draft.allowed_external_source_types.filter((candidate) => !KNOWN_SOURCE_TYPES.has(candidate));
	$: reddit = redditSource();
	$: redditSubsText = Array.isArray(reddit.subs) ? reddit.subs.join(', ') : '';
</script>

<div class="terminal-card p-6 space-y-6 lg:col-span-2">
	<div class="border-b border-[#1a1a1a] pb-3">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Research Orchestration</h2>
		<p class="mt-2 text-sm text-[#888]">
			Control which sources agents may read for ideas, how new candidates are screened, and the held-back data test.
		</p>
	</div>

	<div class="grid gap-4 lg:grid-cols-2">
		<label class="border border-[#222] bg-[#050505] px-4 py-3 text-sm text-[#888]">
			<div class="flex items-center justify-between gap-3">
				<div>
					<div class="font-medium text-white">External Benchmarking</div>
					<div class="mt-1 text-xs text-[#666]">Allow benchmarking cycles to browse public sources like books, blogs, and videos.</div>
				</div>
				<input
					data-testid="research-external-benchmarking"
					type="checkbox"
					bind:checked={draft.external_benchmarking_enabled}
					class="border-[#333] bg-black"
				/>
			</div>
		</label>

		<label class="border border-[#222] bg-[#050505] px-4 py-3 text-sm text-[#888]">
			<div class="font-medium text-white">Min candidate feed coverage (%)</div>
			<div class="mt-1 text-xs text-[#666]">
				A new candidate whose input feeds cover less of the quick-screen window is archived as untestable at
				registration (0 = off).
			</div>
			<input
				data-testid="research-min-feed-coverage-pct"
				type="number"
				min="0"
				max="100"
				value={minFeedCoveragePct}
				on:input={(event) => setMinFeedCoveragePct(Number((event.currentTarget as HTMLInputElement).value))}
				class="terminal-input mt-2 w-full"
			/>
		</label>

		<div class="border border-[#222] bg-[#050505] px-4 py-3 text-sm text-[#888] lg:col-span-2" data-testid="research-holdout">
			<div class="flex items-center justify-between gap-3">
				<div>
					<div class="font-medium text-white">Held-back data (research holdout)</div>
					<div class="mt-1 text-xs text-[#666]">
						Research — agents, backtests, the optimizer and walk-forward — only sees data before the cutoff.
						Each new strategy then gets one test on the held-back period before paper, and that verdict is
						never re-run for the same parameters. Strategies created before this was switched on have seen the
						data, so they are exempt and judged on forward results.
					</div>
				</div>
				<input
					data-testid="research-holdout-enabled"
					type="checkbox"
					checked={researchHoldout.enabled}
					on:change={(e) => setResearchHoldout('enabled', (e.currentTarget as HTMLInputElement).checked)}
					class="border-[#333] bg-black"
				/>
			</div>
			<div class="mt-3 text-xs text-[#aaa]" data-testid="research-holdout-cutoff">
				Research data currently ends: <span class="font-mono text-white">{holdoutCutoffPreview(researchHoldout)}</span>
				{#if researchHoldout.established_at}
					<span class="text-[#555]"> · established {researchHoldout.established_at.slice(0, 10)}</span>
				{/if}
			</div>
			<div class="mt-3 grid gap-3 sm:grid-cols-3">
				<label class="block text-xs text-[#888]">
					Cutoff
					<select
						data-testid="research-holdout-roll"
						value={researchHoldout.roll}
						on:change={(e) => setResearchHoldout('roll', (e.currentTarget as HTMLSelectElement).value as 'quarterly' | 'manual')}
						class="terminal-select mt-1 w-full"
					>
						<option value="quarterly">Rolls quarterly (automatic)</option>
						<option value="manual">Fixed date (manual)</option>
					</select>
				</label>
				{#if researchHoldout.roll === 'manual'}
					<label class="block text-xs text-[#888]">
						Cutoff date
						<input
							type="date"
							value={researchHoldout.cutoff.slice(0, 10)}
							on:change={(e) => setResearchHoldout('cutoff', (e.currentTarget as HTMLInputElement).value)}
							class="terminal-input mt-1 w-full"
						/>
					</label>
				{:else}
					<label class="block text-xs text-[#888]">
						Quarters held back (6–9 months at 2)
						<input
							type="number"
							min="1"
							max="8"
							value={researchHoldout.lag_quarters}
							on:change={(e) => setResearchHoldout('lag_quarters', Number((e.currentTarget as HTMLInputElement).value))}
							class="terminal-input mt-1 w-full"
						/>
					</label>
				{/if}
				<label class="block text-xs text-[#888]">
					At the paper gate
					<select
						data-testid="research-holdout-mode"
						value={researchHoldout.paper_mode}
						on:change={(e) => setResearchHoldout('paper_mode', (e.currentTarget as HTMLSelectElement).value as 'off' | 'observe' | 'enforce')}
						class="terminal-select mt-1 w-full"
					>
						<option value="enforce">Enforce — a pass is required for paper</option>
						<option value="observe">Observe — run and show, never block</option>
						<option value="off">Off — don't run the test</option>
					</select>
				</label>
				<label class="block text-xs text-[#888]">
					Min trades in the held-back period
					<input
						type="number"
						min="1"
						value={researchHoldout.min_trades}
						on:change={(e) => setResearchHoldout('min_trades', Number((e.currentTarget as HTMLInputElement).value))}
						class="terminal-input mt-1 w-full"
					/>
				</label>
				<label class="block text-xs text-[#888]">
					Tests per strategy family per quarter (0 = no cap)
					<input
						type="number"
						min="0"
						value={researchHoldout.max_family_shots}
						on:change={(e) => setResearchHoldout('max_family_shots', Number((e.currentTarget as HTMLInputElement).value))}
						class="terminal-input mt-1 w-full"
					/>
				</label>
			</div>
		</div>

		<div class="border border-[#222] bg-[#050505] px-4 py-3">
			<div class="text-sm font-medium text-white">Allowed External Sources</div>
			<p class="mt-1 text-xs text-[#666]">Source types agents may read when researching ideas.</p>
			<div class="mt-3 grid gap-2 sm:grid-cols-2">
				{#each SOURCE_TYPE_OPTIONS as sourceType}
					<label class="flex items-center justify-between gap-3 border border-[#222] bg-black px-3 py-2 text-xs text-[#888]">
						<span class="uppercase tracking-[0.18em]">{sourceType}</span>
						<input
							data-testid={`research-source-${sourceType}`}
							type="checkbox"
							checked={draft.allowed_external_source_types.includes(sourceType)}
							on:change={(event) => toggleSourceType(sourceType, (event.currentTarget as HTMLInputElement).checked)}
							class="border-[#333] bg-black"
						/>
					</label>
				{/each}
			</div>
			{#if customSourceTypes.length > 0}
				<div class="mt-3 border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400">
					<div class="font-bold uppercase tracking-[0.18em] text-yellow-400">Additional Enabled Sources</div>
					<div class="mt-2 flex flex-wrap gap-2">
						{#each customSourceTypes as sourceType}
							<span class="border border-yellow-900 bg-black px-2.5 py-1 uppercase tracking-[0.18em]">{sourceType}</span>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	</div>

	<div class="border border-[#222] bg-[#050505] p-4">
		<div class="mb-1 text-sm font-medium text-white">Reddit Source</div>
		<p class="mb-3 text-xs text-[#666]">
			Reddit often blocks anonymous JSON requests. Add app credentials to let URL ingest use OAuth for pasted threads.
		</p>
		<div class="grid gap-3 md:grid-cols-2">
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Client ID</span>
				<input
					type="text"
					value={typeof reddit.client_id === 'string' ? reddit.client_id : ''}
					on:input={(event) => setRedditSource('client_id', (event.currentTarget as HTMLInputElement).value.trim() || null)}
					class="terminal-input w-full"
					autocomplete="off"
				/>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Client Secret</span>
				<input
					type="password"
					value={typeof reddit.client_secret === 'string' ? reddit.client_secret : ''}
					on:input={(event) => setRedditSource('client_secret', (event.currentTarget as HTMLInputElement).value.trim() || null)}
					class="terminal-input w-full"
					autocomplete="new-password"
				/>
			</label>
			<label class="text-xs text-[#888] md:col-span-2">
				<span class="mb-1 block uppercase tracking-[0.18em]">Subreddits</span>
				<input
					type="text"
					value={redditSubsText}
					on:input={(event) => setRedditSubs((event.currentTarget as HTMLInputElement).value)}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Comma-separated, used for Reddit discovery searches.</span>
			</label>
		</div>
	</div>

	<div class="flex justify-end">
		<button
			data-testid="research-save"
			type="button"
			on:click={emitSave}
			disabled={saving}
			class="terminal-button-primary text-xs"
		>
			{saving ? 'Saving...' : 'Save Research Settings'}
		</button>
	</div>
</div>
