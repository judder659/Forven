<script lang="ts">
	import type { AgentActivityEntry } from '$lib/api';

	export let activity: AgentActivityEntry[] = [];
	export let returnTo = '';

	let expandedId: number | null = null;

	function toggle(id: number): void {
		expandedId = expandedId === id ? null : id;
	}

	function taskLabel(entry: AgentActivityEntry): string {
		return entry.display_id || `T${entry.task_id}`;
	}

	function taskHref(entry: AgentActivityEntry): string {
		const target = taskLabel(entry);
		const qs = returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : '';
		return `/tasks/${encodeURIComponent(target)}${qs}`;
	}

	function statusTone(status: string): string {
		const s = status.toLowerCase();
		if (s === 'running') return 'border-emerald-900 bg-emerald-500/10 text-emerald-400';
		if (s === 'pending') return 'border-yellow-900 bg-yellow-500/10 text-yellow-400';
		if (s === 'completed' || s === 'success' || s === 'approved')
			return 'border-emerald-900 bg-emerald-500/10 text-emerald-400';
		if (s === 'failed' || s === 'error' || s === 'denied')
			return 'border-red-900 bg-red-500/10 text-red-400';
		return 'border-[#333] text-[#888]';
	}

	function formatStamp(value: string | null | undefined): string {
		if (!value) return '—';
		const normalized = value.includes('T') ? value : `${value}Z`;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return value;
		return parsed.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
	}

	function auditSummary(event: Record<string, unknown>): string {
		const type = String(event['type'] ?? event['event'] ?? 'event');
		const message = event['message'] ?? event['detail'] ?? event['note'];
		if (typeof message === 'string' && message) {
			return `${type}: ${message}`;
		}
		return type;
	}
</script>

<section class="terminal-card">
	<header class="flex items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
		<div>
			<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Agent Activity</h2>
			<p class="mt-1 text-[11px] text-[#666]">Research tasks the strategy-developer has run against this crucible.</p>
		</div>
		<span class="text-[10px] uppercase tracking-wider text-[#666]">{activity.length} task{activity.length === 1 ? '' : 's'}</span>
	</header>

	{#if activity.length === 0}
		<div class="px-4 py-6 text-sm text-[#666]">
			No agent task history yet. Click <em class="not-italic text-[#888]">Re-research</em> to queue one.
		</div>
	{:else}
		<ul class="divide-y divide-[#1a1a1a]">
			{#each activity as entry (entry.task_id)}
				<li class="px-4 py-3 text-sm text-[#888]">
					<div class="flex w-full items-start justify-between gap-3">
						<div class="min-w-0 flex-1">
							<a
								href={taskHref(entry)}
								class="group block min-w-0 outline-none transition-colors hover:text-white focus-visible:ring-1 focus-visible:ring-[#555]"
								aria-label={`Open task ${taskLabel(entry)}`}
							>
								<div class="flex flex-wrap items-center gap-2">
									<span class={`inline-flex border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest ${statusTone(entry.status)}`}>
										{entry.status}
									</span>
									<span class="text-[11px] uppercase tracking-wider text-[#888]">
										{taskLabel(entry)} · {entry.type}
									</span>
									{#if entry.origin_mode}
										<span class="text-[11px] text-[#666]">({entry.origin_mode})</span>
									{/if}
								</div>
								<div class="mt-1 truncate text-sm text-[#888] group-hover:text-white">{entry.title}</div>
								<div class="mt-1 text-[11px] text-[#666]">{formatStamp(entry.created_at)}</div>
							</a>
						</div>
						<button
							type="button"
							class="mt-0.5 border border-transparent px-2 py-1 text-[11px] text-[#666] transition-colors hover:border-[#333] hover:text-[#888]"
							on:click={() => toggle(entry.task_id)}
							aria-expanded={expandedId === entry.task_id}
							aria-label={expandedId === entry.task_id ? `Hide ${taskLabel(entry)} details` : `Show ${taskLabel(entry)} details`}
						>
							{expandedId === entry.task_id ? '▾' : '▸'}
						</button>
					</div>

					{#if expandedId === entry.task_id}
						<div class="mt-3 space-y-2 border-t border-[#1a1a1a] pt-3 text-[12px] text-[#888]">
							{#if entry.decision}
								<div>
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Decision</div>
									<div class="mt-1 whitespace-pre-wrap text-[#888]">{entry.decision}</div>
								</div>
							{/if}
							{#if entry.feedback}
								<div>
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Feedback</div>
									<div class="mt-1 whitespace-pre-wrap text-[#888]">{entry.feedback}</div>
								</div>
							{/if}
							{#if entry.audit_events.length > 0}
								<div>
									<div class="text-[10px] uppercase tracking-wider text-[#666]">Recent events</div>
									<ul class="mt-1 space-y-1 text-[11px] text-[#666]">
										{#each entry.audit_events as event}
											<li class="border-l border-[#333] pl-2">{auditSummary(event)}</li>
										{/each}
									</ul>
								</div>
							{/if}
							{#if !entry.decision && !entry.feedback && entry.audit_events.length === 0}
								<div class="text-[#666] italic">No output recorded yet.</div>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>
