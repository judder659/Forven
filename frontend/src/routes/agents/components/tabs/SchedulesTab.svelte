<script lang="ts">
	/**
	 * Schedules tab. One scheduler editor — reuses SchedulerJobRow and the Hub's
	 * save handler. The host page owns the jobs list and the save handler so this
	 * tab stays a thin presenter (and the duplicate editor that lived in
	 * SettingsAgents is dropped).
	 */
	import type { ForvenSchedulerJob } from '$lib/api';
	import SchedulerJobRow from '../SchedulerJobRow.svelte';

	export let jobs: ForvenSchedulerJob[] = [];
	export let onSave: (
		jobId: string | number,
		scheduleType: string,
		scheduleExpr: string,
		enabled: boolean,
	) => Promise<void>;
	export let showErrors = false;
	export let loading = false;
</script>

<section class="terminal-card">
	<header class="border-b border-[#1a1a1a] px-4 py-3">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Schedules</h2>
		<p class="text-xs text-[#666] mt-1">
			Cron / interval schedules for continuous learning and trading processes. Each job has its own cadence.
		</p>
	</header>

	{#if loading && jobs.length === 0}
		<p class="px-4 py-4 text-sm text-[#666]">Loading scheduler jobs…</p>
	{:else if jobs.length === 0}
		<p class="px-4 py-4 text-sm text-[#666]">No scheduler jobs found.</p>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full text-left text-xs">
				<thead>
					<tr class="border-b border-[#222] text-[10px] text-[#666] uppercase tracking-wider">
						<th class="px-4 py-2 font-medium">Name</th>
						<th class="px-4 py-2 font-medium">Schedule</th>
						<th class="px-4 py-2 font-medium">Next Run</th>
						<th class="px-4 py-2 font-medium">Status</th>
						<th class="px-4 py-2 font-medium">Enabled</th>
					</tr>
				</thead>
				<tbody class="divide-y divide-[#111]">
					{#each jobs as job (job.id)}
						<SchedulerJobRow {job} {onSave} {showErrors} />
					{:else}
						<tr><td colspan="5" class="px-4 py-4 text-center text-[#666]">No jobs configured</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>
