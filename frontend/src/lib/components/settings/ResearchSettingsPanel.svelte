<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	import type { ResearchSettings } from '$lib/api';

	export let draft: ResearchSettings;
	export let saving = false;
	export let onsave: ((event: CustomEvent<ResearchSettings>) => void) | undefined = undefined;

	const MEMORY_MODE_LANES = ['exploration', 'exploitation', 'benchmarking'] as const;
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

	const HYPOTHESIS_DISCIPLINE_DEFAULTS = {
		active_pool_cap: 100,
		min_strategies_per_pick: 3,
		revisit_interval_days: 90,
		verdict_hit_rate_threshold: 0.4,
		verdict_min_diversity_cells: 4,
		verdict_rolling_window: 10,
		max_unrefined_active: 30,
		unstarted_ageout_days: 7,
		refine_in_flight_budget: 2,
		disproven_dedup_lookback_days: 30,
	};

	$: hypothesisDiscipline = {
		...HYPOTHESIS_DISCIPLINE_DEFAULTS,
		...(draft.hypothesis_discipline ?? {}),
	};

	function setHypothesisDiscipline<K extends keyof typeof HYPOTHESIS_DISCIPLINE_DEFAULTS>(
		key: K,
		value: number,
	): void {
		// Read the freshest state from `draft` directly — `hypothesisDiscipline` is
		// recomputed via $: which doesn't re-fire between synchronous input events.
		const current = { ...HYPOTHESIS_DISCIPLINE_DEFAULTS, ...(draft.hypothesis_discipline ?? {}) };
		draft = {
			...draft,
			hypothesis_discipline: {
				...current,
				[key]: value,
			},
		};
	}

	const AUTONOMOUS_DISCOVERY_DEFAULTS = {
		enabled: false,
		mode: 'operator_approves' as 'operator_approves' | 'autonomous',
		max_open_discovery_tasks: 1,
	};

	$: autonomousDiscovery = {
		...AUTONOMOUS_DISCOVERY_DEFAULTS,
		...(draft.autonomous_discovery ?? {}),
	};

	function setAutonomousDiscovery<K extends keyof typeof AUTONOMOUS_DISCOVERY_DEFAULTS>(
		key: K,
		value: (typeof AUTONOMOUS_DISCOVERY_DEFAULTS)[K],
	): void {
		const current = { ...AUTONOMOUS_DISCOVERY_DEFAULTS, ...(draft.autonomous_discovery ?? {}) };
		draft = {
			...draft,
			autonomous_discovery: {
				...current,
				[key]: value,
			},
		};
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
			Control how strategy-developer agents explore, benchmark, and reuse memory during crucible creation.
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

		<div class="border border-[#222] bg-[#050505] px-4 py-3 text-sm text-[#888]">
			<div class="flex items-center justify-between gap-3">
				<div>
					<div class="font-medium text-white">Autonomous Discovery</div>
					<div class="mt-1 text-xs text-[#666]">
						Let the scheduled job harvest new crucibles from external sources on a timer.
						The "Discover" button on the Crucibles page works on demand regardless of this toggle.
					</div>
				</div>
				<input
					data-testid="research-autonomous-discovery-enabled"
					type="checkbox"
					checked={autonomousDiscovery.enabled}
					on:change={(e) => setAutonomousDiscovery('enabled', (e.currentTarget as HTMLInputElement).checked)}
					class="border-[#333] bg-black"
				/>
			</div>
			<label class="mt-3 block text-xs text-[#888]">
				Mode
				<select
					data-testid="research-autonomous-discovery-mode"
					value={autonomousDiscovery.mode}
					on:change={(e) => setAutonomousDiscovery('mode', (e.currentTarget as HTMLSelectElement).value as 'operator_approves' | 'autonomous')}
					class="terminal-select mt-1 w-full"
				>
					<option value="operator_approves">Operator approves — discovered crucibles wait as Proposed for review</option>
					<option value="autonomous">Autonomous — discovered crucibles proceed through the pipeline</option>
				</select>
			</label>
		</div>

		<div class="border border-[#222] bg-[#050505] px-4 py-3">
			<div class="text-sm font-medium text-white">Allowed External Sources</div>
			<p class="mt-1 text-xs text-[#666]">These source categories can be attached as verifiable crucible artifacts.</p>
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

	<div class="border border-[#222] bg-[#050505] p-4">
		<div class="mb-3 text-sm font-medium text-white">Lane Weights</div>
		<div class="grid gap-3 md:grid-cols-3">
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Exploration</span>
				<input type="number" min="0" max="1" step="0.1" bind:value={draft.lane_weights.exploration} class="terminal-input w-full" />
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Exploitation</span>
				<input type="number" min="0" max="1" step="0.1" bind:value={draft.lane_weights.exploitation} class="terminal-input w-full" />
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Benchmarking</span>
				<input type="number" min="0" max="1" step="0.1" bind:value={draft.lane_weights.benchmarking} class="terminal-input w-full" />
			</label>
		</div>
	</div>

	<div class="border border-[#222] bg-[#050505] p-4">
		<div class="mb-3 text-sm font-medium text-white">Spawn Guardrails</div>
		<div class="grid gap-3 md:grid-cols-3">
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Per Run</span>
				<input data-testid="research-per-run" type="number" min="1" bind:value={draft.spawn_limits.per_run} class="terminal-input w-full" />
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Rolling Window</span>
				<input type="number" min="1" bind:value={draft.spawn_limits.rolling_window} class="terminal-input w-full" />
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Window Days</span>
				<input type="number" min="1" bind:value={draft.spawn_limits.window_days} class="terminal-input w-full" />
			</label>
		</div>
	</div>

	<div class="border border-[#222] bg-[#050505] p-4">
		<div class="mb-3 text-sm font-medium text-white">Memory Modes</div>
		<div class="grid gap-4 lg:grid-cols-3">
			{#each MEMORY_MODE_LANES as lane}
				{@const mode = draft.memory_modes[lane]}
				<div class="border border-[#222] bg-black p-3">
					<div class="text-xs font-semibold uppercase tracking-[0.18em] text-[#888]">{lane}</div>
					<label class="mt-3 flex items-center justify-between gap-2 text-sm text-[#888]">
						<span>Constraint Memory</span>
						<input type="checkbox" bind:checked={mode.constraint_memory} class="border-[#333] bg-black" />
					</label>
					<label class="mt-3 block text-xs text-[#888]">
						<span class="mb-1 block uppercase tracking-[0.18em]">Inspiration Memory</span>
						<select
							data-testid={lane === 'exploration' ? 'research-exploration-inspiration' : undefined}
							bind:value={mode.inspiration_memory}
							class="terminal-input w-full"
						>
							<option value="disabled">Disabled</option>
							<option value="optional">Optional</option>
							<option value="bounded">Bounded</option>
						</select>
					</label>
				</div>
			{/each}
		</div>
	</div>

	<div class="border border-[#222] bg-[#050505] p-4">
		<div class="mb-1 text-sm font-medium text-white">Crucible Discipline</div>
		<p class="mb-3 text-xs text-[#666]">
			Bound how many crucibles can be active, how deeply each is worked, and what the verdict pass requires before a crucible graduates.
		</p>
		<div class="grid gap-3 md:grid-cols-3">
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Active Pool Cap</span>
				<input
					data-testid="hypothesis-active-pool-cap"
					type="number"
					min="1"
					max="500"
					value={hypothesisDiscipline.active_pool_cap}
					on:input={(event) => setHypothesisDiscipline('active_pool_cap', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Max crucibles in the active pool. Weakest is evicted to admit a new one once full.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Min Strategies / Pick</span>
				<input
					data-testid="hypothesis-min-strategies-per-pick"
					type="number"
					min="1"
					max="20"
					value={hypothesisDiscipline.min_strategies_per_pick}
					on:input={(event) => setHypothesisDiscipline('min_strategies_per_pick', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Round-robin depth before another crucible can be picked.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Revisit Interval (days)</span>
				<input
					data-testid="hypothesis-revisit-interval-days"
					type="number"
					min="7"
					max="365"
					value={hypothesisDiscipline.revisit_interval_days}
					on:input={(event) => setHypothesisDiscipline('revisit_interval_days', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">How often a graduated crucible returns to the active pool.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Verdict Hit Rate</span>
				<input
					data-testid="hypothesis-verdict-hit-rate-threshold"
					type="number"
					min="0"
					max="1"
					step="0.05"
					value={hypothesisDiscipline.verdict_hit_rate_threshold}
					on:input={(event) => setHypothesisDiscipline('verdict_hit_rate_threshold', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Min fraction of children reaching gauntlet/paper for "proven".</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Min Diversity Cells</span>
				<input
					data-testid="hypothesis-verdict-min-diversity-cells"
					type="number"
					min="1"
					max="50"
					value={hypothesisDiscipline.verdict_min_diversity_cells}
					on:input={(event) => setHypothesisDiscipline('verdict_min_diversity_cells', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Distinct (asset × timeframe) cells with a passing child to graduate.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Rolling Window</span>
				<input
					data-testid="hypothesis-verdict-rolling-window"
					type="number"
					min="3"
					max="100"
					value={hypothesisDiscipline.verdict_rolling_window}
					on:input={(event) => setHypothesisDiscipline('verdict_rolling_window', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Number of most recent children evaluated by the verdict pass.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Max Un-refined Active</span>
				<input
					data-testid="hypothesis-max-unrefined-active"
					type="number"
					min="1"
					max="500"
					value={hypothesisDiscipline.max_unrefined_active}
					on:input={(event) => setHypothesisDiscipline('max_unrefined_active', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Above this many un-started (0-strategy) proposals, agents refine/expand instead of minting new ones.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Unstarted Age-out (days)</span>
				<input
					data-testid="hypothesis-unstarted-ageout-days"
					type="number"
					min="1"
					max="365"
					value={hypothesisDiscipline.unstarted_ageout_days}
					on:input={(event) => setHypothesisDiscipline('unstarted_ageout_days', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">A 'proposed' crucible idle this long with no strategy is archived so the pool reflects real research.</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Disproven Dedup Lookback (days)</span>
				<input
					data-testid="hypothesis-disproven-dedup-lookback-days"
					type="number"
					min="0"
					max="365"
					value={hypothesisDiscipline.disproven_dedup_lookback_days}
					on:input={(event) => setHypothesisDiscipline('disproven_dedup_lookback_days', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Agents can't re-mint a crucible disproven within this window (0 = dedup against the active pool only).</span>
			</label>
			<label class="text-xs text-[#888]">
				<span class="mb-1 block uppercase tracking-[0.18em]">Refine In-flight Budget</span>
				<input
					data-testid="hypothesis-refine-in-flight-budget"
					type="number"
					min="0"
					max="10"
					value={hypothesisDiscipline.refine_in_flight_budget}
					on:input={(event) => setHypothesisDiscipline('refine_in_flight_budget', Number((event.currentTarget as HTMLInputElement).value))}
					class="terminal-input w-full"
				/>
				<span class="mt-1 block text-[11px] text-[#666]">Reserved strategy-developer slots for refine work so the proposed→researching funnel isn't starved.</span>
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
