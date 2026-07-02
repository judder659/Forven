<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import {
		getSkill,
		getSkillHistory,
		getSkillOutcomes,
		getSkillDiff,
		type SkillDetail,
		type SkillHistoryRow,
		type SkillOutcomeEvent,
	} from '$lib/api';

	export let name: string;

	const dispatch = createEventDispatcher<{ close: void }>();

	type Tab = 'current' | 'history' | 'outcomes' | 'evidence';
	let activeTab: Tab = 'current';

	$: tabs = [
		{ key: 'current' as Tab, label: 'Current' },
		{ key: 'history' as Tab, label: `History (${history.length})` },
		{ key: 'outcomes' as Tab, label: `Outcomes (${outcomes.length})` },
		{ key: 'evidence' as Tab, label: 'Evidence' },
	];

	let detail: SkillDetail | null = null;
	let history: SkillHistoryRow[] = [];
	let outcomes: SkillOutcomeEvent[] = [];

	let loading = true;
	let error: string | null = null;

	let diffFromVersion: number | null = null;
	let diffToVersion: number | null = null;
	let diffText: string | null = null;
	let diffLoading = false;

	async function loadAll(): Promise<void> {
		loading = true;
		error = null;
		try {
			const [d, h, o] = await Promise.all([
				getSkill(name),
				getSkillHistory(name).catch(() => ({ skill_name: name, history: [], count: 0 })),
				getSkillOutcomes(name, { limit: 50 }).catch(() => ({
					skill_name: name,
					items: [],
					count: 0,
				})),
			]);
			detail = d;
			history = h.history ?? [];
			outcomes = o.items ?? [];
			if (history.length >= 2) {
				diffFromVersion = history[1].version;
				diffToVersion = history[0].version;
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load skill detail.';
		} finally {
			loading = false;
		}
	}

	async function loadDiff(): Promise<void> {
		if (diffFromVersion == null || diffToVersion == null) return;
		diffLoading = true;
		diffText = null;
		try {
			const res = await getSkillDiff(name, diffFromVersion, diffToVersion);
			diffText = res.diff || '(no textual diff recorded between these versions)';
		} catch (err) {
			diffText = err instanceof Error ? err.message : 'Failed to load diff.';
		} finally {
			diffLoading = false;
		}
	}

	function close(): void {
		dispatch('close');
	}

	function diffLineClass(line: string): string {
		if (line.startsWith('+++') || line.startsWith('---')) return 'text-[#555]';
		if (line.startsWith('@@')) return 'text-[#888]';
		if (line.startsWith('+')) return 'text-emerald-400';
		if (line.startsWith('-')) return 'text-red-400';
		return 'text-[#888]';
	}

	function confidenceColor(c: number): string {
		if (c > 0.7) return 'text-emerald-400';
		if (c > 0.4) return 'text-yellow-400';
		return 'text-red-400';
	}

	function formatDate(iso: string | null | undefined): string {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	}

	onMount(loadAll);
</script>

<div
	class="fixed inset-0 z-[60] flex justify-end bg-black/80"
	role="dialog"
	aria-label="Skill detail"
>
	<button
		type="button"
		class="flex-1"
		aria-label="Close drawer"
		on:click={close}
	></button>
	<aside
		class="flex h-full w-full max-w-2xl flex-col overflow-hidden border-l border-[#222] bg-[#050505]"
	>
		<header class="flex items-center justify-between border-b border-[#222] px-5 py-4">
			<div class="min-w-0">
				<div class="text-[10px] font-semibold uppercase tracking-widest text-[#666]">
					Quant Skill
				</div>
				<div class="mt-1 truncate text-base font-semibold text-white">{name}</div>
			</div>
			<button
				type="button"
				on:click={close}
				class="terminal-button px-3 py-1.5 text-[10px]"
			>
				Close
			</button>
		</header>

		<nav class="flex border-b border-[#222] bg-[#050505] px-2">
			{#each tabs as tab}
				<button
					type="button"
					on:click={() => (activeTab = tab.key)}
					class="border-b-2 px-3 py-3 text-[11px] font-semibold uppercase tracking-widest transition-colors {activeTab === tab.key ? 'border-white text-white' : 'border-transparent text-[#888] hover:text-white'}"
				>
					{tab.label}
				</button>
			{/each}
		</nav>

		<div class="flex-1 overflow-auto px-5 py-4">
			{#if loading}
				<div class="py-12 text-center text-sm text-[#666]">Loading…</div>
			{:else if error}
				<div class="border border-red-900 bg-red-500/5 p-4 text-sm text-red-400">
					{error}
				</div>
			{:else if !detail}
				<div class="py-12 text-center text-sm text-[#666]">Skill not found.</div>
			{:else if activeTab === 'current'}
				<div class="space-y-4">
					<div class="grid grid-cols-2 gap-3 text-xs">
						<div class="border border-[#1a1a1a] bg-[#050505] p-3">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Type</div>
							<div class="mt-1 font-semibold text-white">{detail.skill_type}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-[#050505] p-3">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Confidence</div>
							<div class="mt-1 font-semibold {confidenceColor(detail.confidence)}">
								{Math.round(detail.confidence * 100)}%
							</div>
						</div>
						<div class="border border-[#1a1a1a] bg-[#050505] p-3">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Version</div>
							<div class="mt-1 font-semibold text-white">v{detail.version}</div>
						</div>
						<div class="border border-[#1a1a1a] bg-[#050505] p-3">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Samples</div>
							<div class="mt-1 font-semibold text-white">{detail.sample_size}</div>
						</div>
					</div>

					<section>
						<h3 class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-[#666]">
							Description
						</h3>
						<p class="whitespace-pre-wrap text-sm leading-6 text-[#888]">
							{detail.description || '—'}
						</p>
					</section>

					{#if detail.what_works?.length}
						<section>
							<h3 class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-emerald-400">
								What works
							</h3>
							<ul class="list-disc space-y-1 pl-5 text-sm text-[#888]">
								{#each detail.what_works as item}
									<li>{item}</li>
								{/each}
							</ul>
						</section>
					{/if}

					{#if detail.what_doesnt_work?.length}
						<section>
							<h3 class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-red-400">
								What doesn't
							</h3>
							<ul class="list-disc space-y-1 pl-5 text-sm text-[#888]">
								{#each detail.what_doesnt_work as item}
									<li>{item}</li>
								{/each}
							</ul>
						</section>
					{/if}

					{#if detail.regime || detail.last_validated}
						<section class="grid grid-cols-2 gap-3 text-xs">
							{#if detail.regime}
								<div class="border border-[#1a1a1a] bg-[#050505] p-3">
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Regime</div>
									<div class="mt-1 font-semibold text-white">{detail.regime}</div>
								</div>
							{/if}
							{#if detail.last_validated}
								<div class="border border-[#1a1a1a] bg-[#050505] p-3">
									<div class="text-[10px] uppercase tracking-wider text-[#666]">
										Last validated
									</div>
									<div class="mt-1 text-[#888]">{formatDate(detail.last_validated)}</div>
								</div>
							{/if}
						</section>
					{/if}
				</div>
			{:else if activeTab === 'history'}
				<div class="space-y-3">
					{#if history.length >= 2}
						<div class="border border-[#1a1a1a] bg-[#050505] p-3">
							<div class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-[#666]">
								Compare versions
							</div>
							<div class="flex flex-wrap items-center gap-2 text-xs">
								<label class="flex items-center gap-1 text-[#888]">
									From v
									<select
										bind:value={diffFromVersion}
										class="terminal-select !w-auto !py-1"
									>
										{#each history as row}
											<option value={row.version}>v{row.version}</option>
										{/each}
									</select>
								</label>
								<label class="flex items-center gap-1 text-[#888]">
									To v
									<select
										bind:value={diffToVersion}
										class="terminal-select !w-auto !py-1"
									>
										{#each history as row}
											<option value={row.version}>v{row.version}</option>
										{/each}
									</select>
								</label>
								<button
									type="button"
									on:click={loadDiff}
									disabled={diffLoading}
									class="terminal-button px-3 py-1 text-[10px] disabled:opacity-50"
								>
									{diffLoading ? 'Loading…' : 'Show diff'}
								</button>
							</div>
							{#if diffText !== null}
								<pre
									class="mt-3 max-h-72 overflow-auto border border-[#1a1a1a] bg-[#050505] p-3 font-mono text-[11px] leading-5"><code
										>{#each diffText.split('\n') as line}<span class={diffLineClass(line)}
												>{line}</span
											>{'\n'}{/each}</code
									></pre>
							{/if}
						</div>
					{/if}

					{#if history.length === 0}
						<div class="py-8 text-center text-sm text-[#666]">No history recorded yet.</div>
					{:else}
						<ul class="space-y-2">
							{#each history as row}
								<li class="border border-[#1a1a1a] bg-[#050505] p-3">
									<div class="flex items-center justify-between gap-2">
										<span class="text-xs font-semibold text-white">v{row.version}</span>
										<span class="text-[10px] text-[#666]">{formatDate(row.created_at)}</span>
									</div>
									{#if row.change_summary}
										<p class="mt-1 text-xs text-[#888]">{row.change_summary}</p>
									{/if}
									<div class="mt-1 text-[10px] text-[#555]">
										by {row.created_by ?? 'unknown'}
										{#if row.parent_version}· from v{row.parent_version}{/if}
										{#if row.evidence_task_id}· task #{row.evidence_task_id}{/if}
									</div>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else if activeTab === 'outcomes'}
				{#if outcomes.length === 0}
					<div class="py-8 text-center text-sm text-[#666]">
						No closure events yet. Outcomes are recorded automatically when a strategy citing this
						skill is archived or graduated.
					</div>
				{:else}
					<ul class="space-y-2">
						{#each outcomes as ev}
							<li class="border border-[#1a1a1a] bg-[#050505] p-3">
								<div class="flex items-center justify-between gap-2">
									<span
										class="inline-flex border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest {ev.outcome ===
										'positive'
											? 'border-emerald-900 bg-emerald-500/10 text-emerald-400'
											: 'border-red-900 bg-red-500/10 text-red-400'}"
									>
										{ev.outcome}
									</span>
									<span class="text-[10px] text-[#666]">{formatDate(ev.created_at)}</span>
								</div>
								<div class="mt-1 text-xs text-[#ccc]">
									{ev.strategy_id ?? '—'}
									<span class="ml-2 text-[#666]">{ev.triggered_by ?? ''}</span>
								</div>
								<div class="mt-1 text-[10px] text-[#666]">
									Δ confidence:
									<span class={ev.confidence_delta >= 0 ? 'text-emerald-400' : 'text-red-400'}>
										{ev.confidence_delta >= 0 ? '+' : ''}{ev.confidence_delta.toFixed(3)}
									</span>
								</div>
								{#if ev.notes}
									<p class="mt-1 text-xs text-[#888]">{ev.notes}</p>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			{:else if activeTab === 'evidence'}
				{#if !detail.evidence?.length}
					<div class="py-8 text-center text-sm text-[#666]">No evidence rows recorded.</div>
				{:else}
					<ul class="space-y-2">
						{#each detail.evidence as row, i}
							<li class="border border-[#1a1a1a] bg-[#050505] p-3">
								<div class="text-[10px] font-semibold uppercase tracking-wider text-[#666]">
									Evidence #{i + 1}
								</div>
								<pre
									class="mt-2 overflow-auto whitespace-pre-wrap text-[11px] leading-5 text-[#888]">{JSON.stringify(
										row,
										null,
										2,
									)}</pre>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</div>
	</aside>
</div>
