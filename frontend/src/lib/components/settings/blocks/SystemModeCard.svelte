<script lang="ts">
	// System mode (manual / semi-auto / auto): applies immediately through its own
	// endpoint, not the save bar.
	import { onMount } from 'svelte';
	import { getSystemMode, setSystemMode, type PausedManualCounts, type SystemMode } from '$lib/api';

	let systemMode: SystemMode = 'manual';
	let systemModeLoading = true;
	let systemModeSaving: SystemMode | null = null;
	let systemModeBanner: { tone: 'success' | 'error'; message: string } | null = null;
	let pausedManualCounts: PausedManualCounts = emptyPausedManualCounts();

	const SYSTEM_MODE_OPTIONS: {
		value: SystemMode;
		label: string;
		short: string;
		tagline: string;
		description: string;
	}[] = [
		{
			value: 'manual',
			label: 'Manual',
			short: 'Strict freeze',
			tagline: 'Only direct operator actions run',
			description:
				'All autonomous background work freezes. Scheduled jobs stop, queued autonomous runs pause, and only direct operator actions can run until you leave manual mode.',
		},
		{
			value: 'semi_auto',
			label: 'Semi',
			short: 'User-initiated',
			tagline: 'You submit ideas; the system builds and evaluates them',
			description:
				'Agents will NOT write new ideas on their own. Ideas you submit (The Forge → Submit idea) are built into strategies and fully processed by the Gauntlet, robustness, and lifecycle machinery. Live trading stays active.',
		},
		{
			value: 'auto',
			label: 'Auto',
			short: 'Fully autonomous',
			tagline: 'Original autonomous pipeline',
			description:
				'Agents write new ideas and build strategies from them within the daily budget; the pipeline evaluates and promotes them. Live trading is active. Use only when the pipeline is healthy.',
		},
	];

	function emptyPausedManualCounts(): PausedManualCounts {
		return { agent_tasks: 0, brain_tasks: 0, total: 0 };
	}

	function normalizePausedManualCounts(value: unknown): PausedManualCounts {
		if (!value || typeof value !== 'object') return emptyPausedManualCounts();
		const raw = value as Record<string, unknown>;
		const agentTasks = Number(raw.agent_tasks ?? 0);
		const brainTasks = Number(raw.brain_tasks ?? 0);
		const total = Number(raw.total ?? agentTasks + brainTasks);
		return {
			agent_tasks: Number.isFinite(agentTasks) ? agentTasks : 0,
			brain_tasks: Number.isFinite(brainTasks) ? brainTasks : 0,
			total: Number.isFinite(total) ? total : 0,
		};
	}

	function pausedManualSummary(counts: PausedManualCounts): string {
		if (counts.total <= 0) return 'No queued autonomous work is currently paused.';
		if (counts.total === 1) return '1 queued autonomous run is currently paused.';
		return `${counts.total} queued autonomous runs are currently paused.`;
	}

	async function loadSystemMode() {
		systemModeLoading = true;
		try {
			const result = await getSystemMode();
			systemMode = (result.system_mode as SystemMode) ?? 'manual';
			pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
		} catch (err) {
			systemModeBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to load system mode.',
			};
		} finally {
			systemModeLoading = false;
		}
	}

	async function handleSystemModeChange(next: SystemMode) {
		if (next === systemMode || systemModeSaving) return;
		systemModeSaving = next;
		systemModeBanner = null;
		try {
			const result = await setSystemMode(next);
			systemMode = (result.system_mode as SystemMode) ?? next;
			pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
			systemModeBanner = {
				tone: 'success',
				message: next === 'manual' ? `System mode set to manual. ${pausedManualSummary(pausedManualCounts)}` : `System mode set to ${next}.`,
			};
			setTimeout(() => (systemModeBanner = null), 2500);
		} catch (err) {
			systemModeBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to change system mode.',
			};
		} finally {
			systemModeSaving = null;
		}
	}

	onMount(() => {
		void loadSystemMode();
	});
</script>

<div data-testid="system-mode-card" class="space-y-3 pb-3">
	<header class="flex flex-wrap items-start justify-end gap-2">
		{#if systemModeLoading}
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading…</span>
		{:else}
			<div class="text-right">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
					Current: <span class="text-sc-ink">{systemMode}</span>
				</div>
				{#if systemMode === 'manual'}
					<div class="mt-1 text-[10px] text-yellow-400">{pausedManualSummary(pausedManualCounts)}</div>
				{/if}
			</div>
		{/if}
	</header>
	{#if systemModeBanner}
		<div
			class={`border px-3 py-2 text-xs ${
				systemModeBanner.tone === 'success'
					? 'border-emerald-900 bg-emerald-500/5 text-emerald-400'
					: 'border-red-900 bg-red-500/5 text-red-400'
			}`}
		>
			{systemModeBanner.message}
		</div>
	{/if}
	<div class="grid grid-cols-1 md:grid-cols-3 gap-2">
		{#each SYSTEM_MODE_OPTIONS as option (option.value)}
			{@const active = systemMode === option.value}
			{@const saving = systemModeSaving === option.value}
			<button
				type="button"
				class={`rounded-md text-left border px-3 py-3 transition-colors ${
					active
						? option.value === 'auto'
							? 'border-red-600 bg-red-500/10 text-red-400'
							: option.value === 'semi_auto'
								? 'border-sc-line2 bg-sc-panel2 text-sc-ink'
								: 'border-yellow-600 bg-yellow-500/10 text-yellow-400'
						: 'border-sc-line bg-sc-panel text-sc-ink2 hover:border-sc-line2 hover:bg-sc-panel2'
				} ${saving ? 'opacity-60 cursor-wait' : ''}`}
				on:click={() => handleSystemModeChange(option.value)}
				disabled={systemModeLoading || saving}
				aria-pressed={active}
			>
				<div class="flex items-center justify-between mb-1">
					<span class="font-bold uppercase tracking-wider text-[11px]">{option.label}</span>
					{#if active}
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">Active</span>
					{:else if saving}
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">Saving…</span>
					{/if}
				</div>
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 mb-1">
					{option.short}
				</div>
				<p class="text-xs leading-relaxed text-sc-ink2">{option.description}</p>
			</button>
		{/each}
	</div>
</div>
