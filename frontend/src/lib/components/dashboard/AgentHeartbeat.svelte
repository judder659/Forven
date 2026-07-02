<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { getForvenAgents, getForvenAgentTasks, getForvenLogs } from '$lib/api';
	import type { ForvenAgent, ForvenAgentTask } from '$lib/api';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';

	interface AgentStyle {
		badgeClass: string;
		dotClass: string;
		icon: string;
	}

	interface FeedEntry {
		id: string;
		timestamp: string;
		level: string;
		source: string;
		message: string;
		agentId: string;
		agentName: string;
		style: AgentStyle;
	}

	const AGENT_STYLES: Record<string, AgentStyle> = {
		brain: {
			badgeClass: 'text-white border-[#333]',
			dotClass: 'bg-[#888]',
			icon: '◎',
		},
		'strategy-developer': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '◆',
		},
		'simulation-agent': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '▣',
		},
		'backtest-engineer': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '▣',
		},
		'quant-researcher': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '◈',
		},
		'risk-manager': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '▲',
		},
		'execution-trader': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '▶',
		},
		'full-stack-engineer': {
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '◍',
		},
	};

	const FALLBACK_STYLES: AgentStyle[] = [
		{
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '•',
		},
		{
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '○',
		},
		{
			badgeClass: 'text-[#888] border-[#333]',
			dotClass: 'bg-[#666]',
			icon: '◌',
		},
	];

	interface RosterItem {
		id: string;
		name: string;
		model: string;
		running: boolean;
		activeTaskCount: number;
	}

	let entries: FeedEntry[] = [];
	let roster: RosterItem[] = [];
	let loading = true;
	let errorMessage = '';
	let realtime: RealtimeRefreshController | null = null;

	$: rosterNameById = roster.reduce<Record<string, string>>((acc, item) => {
		if (item.id && item.name) acc[item.id] = item.name;
		return acc;
	}, {});

	function parseAgentId(source: string): string {
		if (!source) return 'unknown';
		if (source === 'brain') return 'brain';
		if (source.startsWith('agent:')) return source.slice(6) || 'unknown';
		return source;
	}

	function toAgentName(agentId: string): string {
		if (agentId === 'backtest-engineer') return 'Simulation Agent';
		if (agentId === 'brain') return 'Brain';
		return agentId
			.split(/[-_]/g)
			.filter(Boolean)
			.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
			.join(' ') || 'Unknown';
	}

	function getAgentStyle(agentId: string): AgentStyle {
		if (AGENT_STYLES[agentId]) return AGENT_STYLES[agentId];
		let hash = 0;
		for (let i = 0; i < agentId.length; i += 1) hash = (hash + agentId.charCodeAt(i)) % 2048;
		return FALLBACK_STYLES[hash % FALLBACK_STYLES.length];
	}

	function formatTime(timestamp: string): string {
		if (!timestamp) return '--:--:--';
		const normalized = timestamp.includes(' ') ? timestamp.replace(' ', 'T') : timestamp;
		const parsed = new Date(normalized);
		if (Number.isNaN(parsed.getTime())) return '--:--:--';
		return parsed.toLocaleTimeString([], { hour12: false });
	}

	function normalizeEntry(raw: unknown, index: number): FeedEntry | null {
		if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
		const rec = raw as Record<string, unknown>;
		const level = String(rec.level ?? '').toLowerCase();
		if (level !== 'heartbeat' && level !== 'info') return null;

		const source = String(rec.source ?? rec.module ?? '').trim();
		if (!source) return null;
		if (!(source.startsWith('agent:') || source === 'brain')) return null;

		const message = String(rec.message ?? rec.msg ?? '').trim();
		if (!message) return null;

		const timestamp = String(rec.created_at ?? rec.ts ?? '');
		const id = String(rec.id ?? `${source}-${timestamp}-${index}-${message.slice(0, 24)}`);
		const agentId = parseAgentId(source);
		return {
			id,
			timestamp,
			level,
			source,
			message,
			agentId,
			agentName: toAgentName(agentId),
			style: getAgentStyle(agentId),
		};
	}

	async function loadRoster() {
		try {
			const [agentsResult, tasksResult] = await Promise.allSettled([
				getForvenAgents(),
				getForvenAgentTasks(),
			]);
			const rawAgents: ForvenAgent[] =
				agentsResult.status === 'fulfilled' && Array.isArray(agentsResult.value)
					? agentsResult.value
					: [];
			const rawTasks: ForvenAgentTask[] =
				tasksResult.status === 'fulfilled' && Array.isArray(tasksResult.value)
					? tasksResult.value
					: [];

			roster = rawAgents
				.map((agent): RosterItem | null => {
					const id = String(agent.id ?? '').trim();
					if (!id) return null;
					const name = String(agent.name ?? '').trim() || toAgentName(id);
					const model = String(agent.model_id ?? agent.model ?? '').trim();
					const agentTasks = rawTasks.filter((task) => String(task.agent_id ?? '') === id);
					const running =
						String(agent.status ?? '').toLowerCase() === 'running' ||
						agentTasks.some((task) => String(task.status ?? '').toLowerCase() === 'running');
					const activeTaskCount = agentTasks.filter((task) => {
						const status = String(task.status ?? '').toLowerCase();
						return status === 'running' || status === 'pending';
					}).length;
					return { id, name, model, running, activeTaskCount };
				})
				.filter((item): item is RosterItem => Boolean(item));
		} catch {
			// Roster fetch failure should not break the heartbeat feed; leave roster untouched.
		}
	}

	async function loadEntries() {
		try {
			const rawLogs = await getForvenLogs(120);
			const list = Array.isArray(rawLogs) ? rawLogs : [];
			entries = list
				.map((entry, index) => normalizeEntry(entry, index))
				.filter((entry): entry is FeedEntry => Boolean(entry))
				.slice(0, 60);
			errorMessage = '';
		} catch (error) {
			errorMessage = error instanceof Error ? error.message : 'Failed to load heartbeat feed';
		} finally {
			loading = false;
		}
		await loadRoster();
	}

	onMount(() => {
		realtime = createRealtimeRefresh(loadEntries, {
			fallbackMs: 60_000,
			wsDebounceMs: 6000,
			wsEvents: ['task_completed', 'task_failed', 'strategy_promoted', 'kill_switch_activated', 'kill_switch_cleared'],
			pollWhenWsOfflineOnly: false,
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
		realtime = null;
	});
</script>

<section class="terminal-card h-full flex flex-col">
	<header class="flex items-center justify-between border-b border-[#1a1a1a] px-4 py-2">
		<div class="flex items-center gap-2">
			<span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
			<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Heartbeat / Agent Activity</h3>
		</div>
		<span class="text-[10px] text-[#555] font-mono">ws live + fallback</span>
	</header>

	<div class="flex-1 min-h-0 overflow-y-auto px-2 py-2 font-mono text-xs">
		{#if loading && entries.length === 0}
			<div class="h-full flex items-center justify-center text-[#555] uppercase tracking-widest">Booting activity stream...</div>
		{:else if entries.length === 0}
			<div class="h-full flex items-center justify-center text-[#555]">No heartbeat events yet.</div>
		{:else}
			<div>
				{#each entries as entry (entry.id)}
					<div
						class="group flex items-start gap-2 px-2 py-1 border-b border-[#111] hover:bg-[#111] transition-colors"
					>
						<span class="text-[10px] text-[#555] w-[74px] flex-shrink-0 pt-[2px]">{formatTime(entry.timestamp)}</span>
						<span class="w-1.5 h-1.5 rounded-full mt-[7px] flex-shrink-0 {entry.style.dotClass}"></span>
						<span class="inline-flex items-center gap-1 px-2 py-0.5 border text-[10px] uppercase tracking-wide flex-shrink-0 {entry.style.badgeClass}">
							<span>{entry.style.icon}</span>
							{rosterNameById[entry.agentId] ?? entry.agentName}
						</span>
						<span class="text-[#aaa] leading-5 break-words">{entry.message}</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Roster as a single chip strip: one footer row instead of one row per
	     agent, so the activity feed keeps the panel's height. Full detail
	     (model, task count) lives in each chip's tooltip. -->
	<div
		data-testid="agent-roster"
		class="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-[#1a1a1a] px-2.5 py-1.5"
	>
		{#if roster.length === 0}
			<span class="text-[10px] italic text-[#555]">No agents registered.</span>
		{:else}
			{#each roster as agent (agent.id)}
				<span
					class="inline-flex items-center gap-1 font-mono text-[10px] {agent.running ? 'text-[#888]' : 'text-[#555]'}"
					title="{agent.name}{agent.model ? ` · ${agent.model}` : ''} · {agent.activeTaskCount} active task(s) · {agent.running ? 'running' : 'idle'}"
				>
					<span class={`inline-block h-1.5 w-1.5 flex-shrink-0 rounded-full ${agent.running ? 'bg-emerald-400' : 'bg-gray-700'}`}></span>
					{agent.name}{#if agent.activeTaskCount > 0}<span class="text-white">·{agent.activeTaskCount}</span>{/if}
				</span>
			{/each}
			<span class="ml-auto font-mono text-[10px] text-[#555]">{roster.length} agents</span>
		{/if}
	</div>

	{#if errorMessage}
		<div class="px-4 py-2 border-t border-red-900 bg-red-500/5 text-[10px] text-red-400">
			{errorMessage}
		</div>
	{/if}
</section>
