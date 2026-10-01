<script lang="ts">
	import type { ForvenSchedulerJob } from '$lib/api';
	import { GROUP_META, GROUP_ORDER, jobCadence, jobGroup, jobHealth, jobNote, type JobGroup } from '$lib/utils/agentsHub/schedules';
	import { TONE_DOT, TONE_TEXT } from '$lib/utils/forge/status';
	import { ago, parseUtc, shortDateTime } from '$lib/utils/forge/time';
	import { minutesToMs, msToMinutes } from '$lib/utils/schedule';

	type Job = ForvenSchedulerJob & { running_since?: string | null };

	export let jobs: Job[] = [];
	export let loading = false;
	export let error: string | null = null;
	export let now = Date.now();
	export let onSave: (jobId: string | number, scheduleType: string, scheduleExpr: string, enabled: boolean) => Promise<void>;

	let filter: 'all' | 'failing' | 'off' = 'all';
	let search = '';
	let editing: string | null = null;
	let draftType: 'interval' | 'cron' = 'interval';
	let draftExpr = '';
	let saving: string | null = null;
	let rowError: Record<string, string> = {};

	$: query = search.trim().toLowerCase();
	$: failing = jobs.filter((job) => jobHealth(job).key === 'failing');
	$: off = jobs.filter((job) => !job.enabled);
	$: visible = jobs.filter((job) => {
		const health = jobHealth(job);
		if (filter === 'failing' && health.key !== 'failing') return false;
		if (filter === 'off' && health.key !== 'off') return false;
		if (query && !`${job.name} ${job.command} ${job.id}`.toLowerCase().includes(query)) return false;
		return true;
	});
	$: groups = GROUP_ORDER.map((group) => ({
		group,
		jobs: visible
			.filter((job) => jobGroup(job) === group)
			.sort((a, b) => Number(jobHealth(b).key === 'failing') - Number(jobHealth(a).key === 'failing') || String(a.name).localeCompare(String(b.name))),
	})).filter((entry) => entry.jobs.length > 0);
	$: nextJob = jobs
		.filter((job) => job.enabled && parseUtc(job.next_run_at) !== null)
		.sort((a, b) => (parseUtc(a.next_run_at) ?? 0) - (parseUtc(b.next_run_at) ?? 0))[0];

	function key(job: Job): string {
		return String(job.id ?? job.name ?? '');
	}

	function nextLabel(job: Job): string {
		if (!job.enabled) return 'off';
		const at = parseUtc(job.next_run_at);
		if (at === null) return '—';
		const seconds = Math.round((at - now) / 1000);
		if (seconds <= 0) return 'due now';
		if (seconds < 60) return `in ${seconds}s`;
		const minutes = Math.round(seconds / 60);
		if (minutes < 60) return `in ${minutes}m`;
		const hours = Math.round(minutes / 60);
		if (hours < 48) return `in ${hours}h`;
		return `in ${Math.round(hours / 24)}d`;
	}

	function startEdit(job: Job) {
		editing = key(job);
		draftType = job.schedule_type === 'cron' ? 'cron' : 'interval';
		draftExpr = draftType === 'interval' ? msToMinutes(job.schedule_expr ?? '') : String(job.schedule_expr ?? '');
		rowError = { ...rowError, [key(job)]: '' };
	}

	async function save(job: Job) {
		const id = key(job);
		const expr = draftType === 'interval' ? minutesToMs(draftExpr) : draftExpr.trim();
		if (!expr) {
			rowError = { ...rowError, [id]: draftType === 'interval' ? 'Enter a whole number of minutes.' : 'Enter a cron expression.' };
			return;
		}
		saving = id;
		try {
			await onSave(job.id ?? id, draftType, expr, Boolean(job.enabled));
			editing = null;
		} catch (err) {
			rowError = { ...rowError, [id]: err instanceof Error ? err.message : 'Could not save the schedule.' };
		} finally {
			saving = null;
		}
	}

	async function toggle(job: Job) {
		const id = key(job);
		const turningOff = Boolean(job.enabled);
		if (turningOff && jobGroup(job) === 'trading' && !confirm(`Turn off “${job.name}”? It acts on paper and live positions; nothing runs it until you turn it back on.`)) return;
		saving = id;
		try {
			// Keep the stored schedule exactly as it is; only the switch changes.
			await onSave(job.id ?? id, String(job.schedule_type ?? 'interval'), String(job.schedule_expr ?? ''), !turningOff);
		} catch (err) {
			rowError = { ...rowError, [id]: err instanceof Error ? err.message : 'Could not change the job.' };
		} finally {
			saving = null;
		}
	}
</script>

<div class="grid gap-3" data-testid="agents-schedules">
	<div class="flex flex-wrap items-center gap-2">
		<p class="m-0 text-[12.5px] text-sc-ink2">
			{jobs.length} background jobs · <span class={failing.length > 0 ? 'text-[#f2956f]' : ''}>{failing.length} failing</span> · {off.length} off{#if nextJob}{' '}· next: <span class="text-sc-ink">{nextJob.name}</span> {nextLabel(nextJob)}{/if}
		</p>
		<div class="ml-auto flex flex-wrap items-center gap-2">
			<div class="flex rounded-md border border-sc-line bg-sc-panel p-0.5" role="group" aria-label="Job filter">
				{#each [['all', 'All'], ['failing', `Failing ${failing.length}`], ['off', `Off ${off.length}`]] as [id, label] (id)}
					<button type="button" aria-pressed={filter === id} class={`rounded px-2.5 py-0.5 text-[12px] ${filter === id ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3 hover:text-sc-ink'}`} on:click={() => (filter = id === 'failing' ? 'failing' : id === 'off' ? 'off' : 'all')}>{label}</button>
				{/each}
			</div>
			<input type="search" class="w-48 rounded-md border border-sc-line2 bg-sc-panel px-2.5 py-1 text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder="Search jobs…" bind:value={search} aria-label="Search jobs" />
		</div>
	</div>

	{#if error}
		<p class="m-0 rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12px] text-[#f2956f]">{error}</p>
	{/if}

	{#if loading && jobs.length === 0}
		<div class="grid gap-2" aria-busy="true">{#each [0, 1, 2, 3] as i (i)}<div class="h-9 animate-pulse rounded bg-sc-raise/60"></div>{/each}</div>
	{:else if groups.length === 0}
		<p class="m-0 rounded-md border border-sc-line bg-sc-panel px-3 py-6 text-center text-[12px] text-sc-ink3">No jobs match.</p>
	{:else}
		{#each groups as entry (entry.group)}
			<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label={GROUP_META[entry.group].label}>
				<header class="flex flex-wrap items-baseline justify-between gap-2 border-b border-sc-line px-3.5 py-2">
					<h3 class="m-0 text-[12.5px] font-semibold text-sc-ink">{GROUP_META[entry.group].label} <span class="font-normal text-sc-ink3">{entry.jobs.length}</span></h3>
					<span class="text-[11px] text-sc-ink3">{GROUP_META[entry.group].help}</span>
				</header>
				<ul class="m-0 list-none divide-y divide-sc-line p-0">
					{#each entry.jobs as job (key(job))}
						{@const health = jobHealth(job)}
						{@const note = jobNote(job)}
						{@const id = key(job)}
						<li class={`px-3.5 py-2 ${job.enabled ? '' : 'opacity-60'}`} data-testid="agents-job-row">
							<div class="grid grid-cols-[10px_minmax(0,1.6fr)_minmax(0,1fr)_minmax(0,1.1fr)_72px_84px] items-center gap-3 text-[12px]">
								<span class={`h-2 w-2 rounded-full ${TONE_DOT[health.tone]} ${health.key === 'running' ? 'animate-pulse' : ''}`} title={health.label} aria-label={health.label}></span>
								<div class="min-w-0">
									<div class="truncate text-sc-ink" title={job.name}>{job.name}</div>
									<div class="truncate font-plex-mono text-[10.5px] text-sc-ink4">{job.command}</div>
								</div>
								<div class="min-w-0">
									{#if editing === id}
										<div class="flex items-center gap-1">
											<select class="rounded border border-sc-line2 bg-sc-bg px-1 py-0.5 text-[11.5px] text-sc-ink" bind:value={draftType} on:change={() => (draftExpr = '')} aria-label="Schedule type">
												<option value="interval">every N min</option>
												<option value="cron">cron</option>
											</select>
											<input class="w-24 rounded border border-sc-line2 bg-sc-bg px-1.5 py-0.5 font-plex-mono text-[11.5px] text-sc-ink" bind:value={draftExpr} placeholder={draftType === 'interval' ? 'minutes' : '0 9 * * *'} aria-label="Schedule" />
										</div>
									{:else}
										<button type="button" class="truncate text-left text-sc-ink2 hover:text-sc-ink hover:underline" title="Change the schedule" on:click={() => startEdit(job)}>{jobCadence(job)}</button>
									{/if}
								</div>
								<div class="min-w-0 text-sc-ink3">
									{#if job.running_since}
										<span class="text-[#7fb2ff]">running {ago(job.running_since, now).replace(' ago', '')}</span>
									{:else if job.last_run_at}
										<span title={shortDateTime(job.last_run_at)}>ran {ago(job.last_run_at, now)}</span>
										<span class={TONE_TEXT[health.tone]}> · {health.label.toLowerCase()}</span>
									{:else}
										<span>never ran</span>
									{/if}
								</div>
								<div class="text-right text-sc-ink3" title={job.next_run_at ? shortDateTime(job.next_run_at) : ''}>{nextLabel(job)}</div>
								<div class="flex items-center justify-end gap-2">
									{#if editing === id}
										<button type="button" class="text-[11.5px] text-sc-ink hover:underline disabled:opacity-40" disabled={saving === id} on:click={() => save(job)}>{saving === id ? 'Saving…' : 'Save'}</button>
										<button type="button" class="text-[11.5px] text-sc-ink3 hover:text-sc-ink" on:click={() => (editing = null)}>Cancel</button>
									{:else}
										<button
											type="button"
											role="switch"
											aria-checked={Boolean(job.enabled)}
											aria-label={`${job.enabled ? 'Turn off' : 'Turn on'} ${job.name}`}
											class={`relative h-[18px] w-8 rounded-full transition-colors disabled:opacity-50 ${job.enabled ? 'bg-[#3cc48f]/80' : 'bg-sc-line2'}`}
											disabled={saving === id}
											on:click={() => toggle(job)}
										><span class={`absolute top-[2px] h-[14px] w-[14px] rounded-full bg-sc-ink transition-transform ${job.enabled ? 'translate-x-[16px]' : 'translate-x-[2px]'}`}></span></button>
									{/if}
								</div>
							</div>
							{#if health.key === 'failing' && job.last_error}
								<p class="m-0 ml-[22px] mt-1 break-words rounded border border-[#e5574f]/30 bg-[#e5574f]/[0.06] px-2 py-1 font-plex-mono text-[11px] text-[#f4b5a8]">{job.last_error}</p>
							{:else if note}
								<p class="m-0 ml-[22px] mt-0.5 text-[11px] text-sc-ink3">Note from the last run: {note}</p>
							{/if}
							{#if rowError[id]}
								<p class="m-0 ml-[22px] mt-1 text-[11px] text-[#f2956f]">{rowError[id]}</p>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/each}
	{/if}
</div>
