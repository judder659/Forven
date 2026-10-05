<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getBrainOverview,
		type BrainActivityRow,
		type BrainAttentionItem,
		type BrainOverview,
		type BrainOverviewTask,
		type BrainRepeatedFailure
	} from '$lib/api/brain';

	let overview: BrainOverview | null = null;
	let loading = true;
	let error = '';
	let refreshing = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function load(silent = false): Promise<void> {
		if (silent) refreshing = true;
		else loading = true;
		try {
			overview = await getBrainOverview();
			error = '';
		} catch (err) {
			// A failed background poll should not blow away the rendered view;
			// only surface load errors for explicit (non-silent) loads.
			if (!silent) {
				error = err instanceof Error ? err.message : 'Failed to load Brain overview.';
			}
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '-';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function memoryLines(body: string): string[] {
		return body
			.split('\n')
			.map((line) => line.trim())
			.filter(Boolean)
			.slice(0, 10);
	}

	function taskHref(task: BrainOverviewTask): string {
		// The /tasks/[id] route resolves by display_id (LOWER(display_id)); a numeric id 404s.
		return `/tasks/${task.display_id ?? task.id}`;
	}

	function strategyHref(task: BrainOverviewTask): string | null {
		return task.strategy_id ? `/lab/strategy/${task.strategy_id}` : null;
	}

	function taskLabel(task: BrainOverviewTask): string {
		return task.display_id || `T${task.id}`;
	}

	const PILL = 'rounded border px-1.5 py-px font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.06em]';
	const PANEL = 'min-w-0 rounded-md border border-sc-line bg-sc-panel p-3.5';
	const PANEL_HEAD = 'mb-3 flex items-baseline justify-between gap-3';
	const PANEL_TITLE = 'm-0 text-[13px] font-medium text-sc-ink';
	const PANEL_AUX = 'text-[11px] text-sc-ink3';
	const PANEL_LINK = 'text-[12px] text-sc-ink2 transition-colors hover:text-sc-ink hover:underline';
	const ROW = 'rounded-md border border-sc-line bg-sc-panel2 px-3 py-2.5';
	const EMPTY_INLINE = 'm-0 text-[12.5px] text-sc-ink3';
	const BUTTON =
		'rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink disabled:cursor-not-allowed disabled:opacity-50';

	function attentionClass(item: BrainAttentionItem): string {
		const base = 'flex justify-between gap-3 rounded-md border px-3 py-2.5';
		const severity = item.severity || 'info';
		if (severity === 'critical') return `${base} border-[#e5574f]/40 bg-[#e5574f]/10`;
		if (severity === 'warning') return `${base} border-[#e7b24a]/40 bg-[#e7b24a]/10`;
		return `${base} border-sc-line bg-sc-panel2`;
	}

	function statusClass(status: string | null | undefined): string {
		const normalized = (status || 'pending').toLowerCase();
		if (normalized === 'failed') return `${PILL} border-[#e5574f]/40 bg-[#e5574f]/10 text-[#f2956f]`;
		if (normalized === 'blocked' || normalized === 'paused_manual')
			return `${PILL} border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]`;
		if (normalized === 'running') return `${PILL} border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]`;
		if (normalized === 'done' || normalized === 'reviewed')
			return `${PILL} border-[#3cc48f]/25 bg-[#3cc48f]/5 text-[#3cc48f]`;
		return `${PILL} border-sc-line2 bg-sc-raise text-sc-ink2`;
	}

	function shortMessage(row: BrainActivityRow): string {
		const max = 180;
		return row.message.length > max ? `${row.message.slice(0, max)}...` : row.message;
	}

	function failureLabel(row: BrainRepeatedFailure): string {
		return row.type || 'unknown';
	}

	onMount(() => {
		void load(false);
		// Keep attention signals / active-task counts live while the tab is
		// visible (mirrors the visibility-gated poll in BrainMemoryTab).
		pollTimer = setInterval(() => {
			if (document.visibilityState === 'visible') {
				void load(true);
			}
		}, 30000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
	});
</script>

<div class="flex flex-col gap-3">
	{#if loading}
		<div class="rounded-md border border-dashed border-sc-line p-4 text-center text-[13px] text-sc-ink3">Loading Brain overview...</div>
	{:else if error}
		<div class="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12.5px] text-[#f2956f]" role="alert">
			<strong class="font-medium">Failed to load overview:</strong>
			{error}
			<button type="button" class="rounded-md border border-[#e5574f]/40 px-2.5 py-1 text-[12px] text-[#f2956f] transition-colors hover:border-[#f2956f]" on:click={() => load(false)}>Retry</button>
		</div>
	{:else if overview}
		<section class="flex flex-col items-start justify-between gap-3 rounded-md border border-sc-line bg-sc-panel p-4 sm:flex-row sm:items-center">
			<div class="min-w-0">
				<p class="m-0 mb-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Current Brain State</p>
				<h2 class="m-0 text-[16px] font-semibold text-sc-ink">Autonomy state, actions, blockers, and memory.</h2>
				<p class="m-0 mt-1 text-[12.5px] text-sc-ink2">
					Memory updated by {overview.memory.updated_by ?? '-'} at
					{formatTimestamp(overview.memory.updated_at)}
				</p>
			</div>
			<button type="button" class={`shrink-0 ${BUTTON}`} on:click={() => load(true)} disabled={refreshing}>
				{refreshing ? 'Refreshing...' : 'Refresh'}
			</button>
		</section>

		<section class="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-sc-line bg-sc-line md:grid-cols-4" aria-label="Brain overview stats">
			<a class="flex flex-col gap-0.5 bg-sc-panel px-3.5 py-2.5 transition-colors hover:bg-sc-hover" href="/brain?tab=memory">
				<span class="text-[11px] text-sc-ink3">Memory</span>
				<strong class="font-plex-mono text-[18px] font-normal text-sc-ink">{overview.memory.char_count}/{overview.memory.cap}</strong>
			</a>
			<a class="flex flex-col gap-0.5 bg-sc-panel px-3.5 py-2.5 transition-colors hover:bg-sc-hover" href="/agents?tab=tasks">
				<span class="text-[11px] text-sc-ink3">Active runs</span>
				<strong class="font-plex-mono text-[18px] font-normal text-sc-ink">{overview.stats.active_tasks}</strong>
			</a>
			<a class="flex flex-col gap-0.5 bg-sc-panel px-3.5 py-2.5 transition-colors hover:bg-sc-hover" href="/approval">
				<span class="text-[11px] text-sc-ink3">Approvals</span>
				<strong class="font-plex-mono text-[18px] font-normal text-sc-ink">{overview.stats.pending_approvals}</strong>
			</a>
			<a class="flex flex-col gap-0.5 bg-sc-panel px-3.5 py-2.5 transition-colors hover:bg-sc-hover" href="/brain?tab=decisions">
				<span class="text-[11px] text-sc-ink3">Decisions</span>
				<strong class="font-plex-mono text-[18px] font-normal text-sc-ink">{overview.stats.decisions}</strong>
			</a>
		</section>

		<div class="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
			<section class={PANEL}>
				<header class={PANEL_HEAD}>
					<h3 class={PANEL_TITLE}>Needs Attention</h3>
					<span class={`font-plex-mono ${PANEL_AUX}`}>{overview.attention.length}</span>
				</header>
				{#if overview.attention.length === 0}
					<p class={EMPTY_INLINE}>No attention signals right now.</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-2 p-0">
						{#each overview.attention as item}
							<li class={attentionClass(item)}>
								<div class="min-w-0">
									<strong class="block text-[13px] font-medium text-sc-ink">{item.title}</strong>
									<p class="m-0 mt-1 break-words text-[12.5px] leading-snug text-sc-ink2">{item.detail}</p>
								</div>
								<span class="whitespace-nowrap font-plex-cond text-[11px] uppercase tracking-[0.06em] text-sc-ink3">{item.kind}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class={PANEL}>
				<header class={PANEL_HEAD}>
					<h3 class={PANEL_TITLE}>Memory Snapshot</h3>
					<a class={PANEL_LINK} href="/brain?tab=memory">Edit</a>
				</header>
				{#if overview.memory.body}
					<ul class="m-0 flex list-none flex-col gap-1.5 p-0">
						{#each memoryLines(overview.memory.body) as line}
							<li class="break-words text-[13px] leading-snug text-sc-ink">{line}</li>
						{/each}
					</ul>
				{:else}
					<p class={EMPTY_INLINE}>Brain memory is empty.</p>
				{/if}
			</section>
		</div>

		<section class={PANEL}>
			<header class={PANEL_HEAD}>
				<h3 class={PANEL_TITLE}>Active Brain-Assigned Runs</h3>
				<a class={PANEL_LINK} href="/agents?tab=tasks">Open Runs</a>
			</header>
			{#if overview.active_tasks.length === 0}
				<p class={EMPTY_INLINE}>No pending, running, blocked, or failed Brain-assigned tasks.</p>
			{:else}
				<ul class="m-0 flex list-none flex-col gap-2 p-0">
					{#each overview.active_tasks as task (task.id)}
						<li class={ROW}>
							<div class="flex flex-wrap items-center gap-2 text-[11.5px] text-sc-ink2">
								<a class="font-plex-mono text-sc-ink2 transition-colors hover:text-sc-ink hover:underline" href={taskHref(task)}>{taskLabel(task)}</a>
								<span class={statusClass(task.status)}>{task.status ?? 'pending'}</span>
								<span class={`${PILL} border-sc-line2 bg-sc-raise text-sc-ink2`}>{task.type ?? '-'}</span>
								<span class="text-sc-ink3 sm:ml-auto">{formatTimestamp(task.created_at)}</span>
							</div>
							<div class="mt-1.5 text-[13px] leading-snug text-sc-ink">{task.title ?? 'Untitled task'}</div>
							<div class="mt-1.5 flex min-w-0 flex-wrap items-center gap-2 text-[11.5px] text-sc-ink2">
								<span>{task.agent_id ?? '-'}</span>
								{#if task.strategy_id}
									<a class="font-plex-mono text-sc-ink2 transition-colors hover:text-sc-ink hover:underline" href={strategyHref(task)}>{task.strategy_id}</a>
								{/if}
								{#if task.error}
									<span class="max-w-full truncate text-[#f2956f]">{task.error}</span>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<div class="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
			<section class={PANEL}>
				<header class={PANEL_HEAD}>
					<h3 class={PANEL_TITLE}>Recent Brain Activity</h3>
					<span class={`font-plex-mono ${PANEL_AUX}`}>{overview.activity.length}</span>
				</header>
				{#if overview.activity.length === 0}
					<p class={EMPTY_INLINE}>No Brain activity rows found.</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-2 p-0">
						{#each overview.activity.slice(0, 12) as row (row.id)}
							<li class={ROW}>
								<div class="flex flex-wrap items-center gap-2 text-[11.5px] text-sc-ink2">
									<span class={`${PILL} border-sc-line2 bg-sc-raise text-sc-ink2`}>{row.level}</span>
									<span>{row.source ?? '-'}</span>
									<span class="text-sc-ink3 sm:ml-auto">{formatTimestamp(row.created_at)}</span>
								</div>
								<p class="m-0 mt-1.5 break-words text-[13px] leading-snug text-sc-ink">{shortMessage(row)}</p>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class={PANEL}>
				<header class={PANEL_HEAD}>
					<h3 class={PANEL_TITLE}>Repeated Failures</h3>
					<span class={`font-plex-mono ${PANEL_AUX}`}>{overview.repeated_failures.length}</span>
				</header>
				{#if overview.repeated_failures.length === 0}
					<p class={EMPTY_INLINE}>No task types with 3 or more Brain-assigned failures.</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-2 p-0">
						{#each overview.repeated_failures as failure}
							<li class={`flex items-center justify-between gap-4 ${ROW}`}>
								<span class="min-w-0 break-words text-[13px] text-sc-ink">{failureLabel(failure)}</span>
								<strong class="font-plex-mono text-[15px] font-normal text-[#f2956f]">{failure.count}</strong>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>
	{/if}
</div>
