<script lang="ts">
	import { onMount } from 'svelte';
	import {
		createRoutine,
		deleteRoutine,
		listRoutines,
		listRoutineChannels,
		pauseRoutine,
		previewCronExpression,
		previewRoutineSchedule,
		resumeRoutine,
		runRoutine,
		updateRoutine,
		type Routine,
		type RoutineChannel,
		type RoutineCreatePayload,
	} from '$lib/api/routines';
	import {
		cronToFriendly,
		defaultFriendlySchedule,
		describeCronLocal,
		describeFriendly,
		friendlyToCron,
		WEEKDAY_NAMES,
		type FriendlySchedule,
	} from '$lib/utils/schedule';

	let routines: Routine[] = [];
	let channels: RoutineChannel[] = [];
	let loading = true;
	let error: string | null = null;
	let actionMessage: string | null = null;
	let busyId: number | null = null;

	let createForm: RoutineCreatePayload = {
		name: '',
		prompt: '',
		cron_expr: '',
		tools_context: 'scheduled',
		channel: '',
		enabled: true,
	};
	let creating = false;
	let cronPreview: string[] = [];
	let cronPreviewError: string | null = null;
	let createSched: FriendlySchedule = defaultFriendlySchedule();
	let createAdvanced = false;

	let editingId: number | null = null;
	let editDraft: RoutineCreatePayload = { name: '', prompt: '', cron_expr: '', channel: '' };
	let editError: string | null = null;
	let editPreview: string[] = [];
	let editSched: FriendlySchedule = defaultFriendlySchedule();
	let editAdvanced = false;

	const VALID_CONTEXTS = ['scheduled', 'interactive', 'recovery', 'research'];

	const FREQ_OPTIONS: { value: FriendlySchedule['freq']; label: string }[] = [
		{ value: 'minutes', label: 'Every N minutes' },
		{ value: 'hours', label: 'Every N hours' },
		{ value: 'daily', label: 'Every day' },
		{ value: 'weekly', label: 'Every week' },
		{ value: 'monthly', label: 'Every month' },
	];

	function fmtDate(value: string | null | undefined): string {
		if (!value) return '--';
		const d = new Date(String(value));
		if (Number.isNaN(d.getTime())) return '--';
		return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function statusClass(status: string | null): string {
		switch ((status || '').toLowerCase()) {
			case 'dispatched':
			case 'ok':
			case 'completed':
				return 'text-emerald-400 border-emerald-900 bg-emerald-500/10';
			case 'error':
			case 'failed':
				return 'text-red-400 border-red-900 bg-red-500/10';
			default:
				return 'text-sc-ink2 border-sc-line2 bg-sc-panel2';
		}
	}

	function scheduleLabel(routine: Routine): string {
		return describeCronLocal(routine.cron_expr) || routine.cron_expr;
	}

	// Stored channel values are raw ids (live guild list) or alias names
	// (fallback map / Brain proposals) — show the friendly label when known.
	function channelLabel(value: string | null | undefined): string {
		if (!value) return '';
		const match = channels.find((c) => c.id === value);
		return match ? match.label : `#${value}`;
	}

	async function load() {
		loading = true;
		error = null;
		try {
			routines = await listRoutines();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function loadChannels() {
		// Older backends don't have the endpoint yet — fall back to free text.
		try {
			channels = await listRoutineChannels();
		} catch {
			channels = [];
		}
	}

	async function refreshCronPreview(expr: string) {
		cronPreviewError = null;
		if (!expr.trim()) { cronPreview = []; return; }
		try {
			cronPreview = await previewCronExpression(expr.trim(), 5);
		} catch (err) {
			cronPreview = [];
			cronPreviewError = err instanceof Error ? err.message : String(err);
		}
	}

	async function handleCreate() {
		creating = true;
		error = null;
		actionMessage = null;
		try {
			await createRoutine({ ...createForm, channel: (createForm.channel || '').trim() });
			actionMessage = `Routine '${createForm.name}' created.`;
			createForm = { name: '', prompt: '', cron_expr: '', tools_context: 'scheduled', channel: '', enabled: true };
			createSched = defaultFriendlySchedule();
			createAdvanced = false;
			cronPreview = [];
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			creating = false;
		}
	}

	async function startEdit(routine: Routine) {
		editingId = routine.id;
		editDraft = {
			name: routine.name,
			prompt: routine.prompt,
			cron_expr: routine.cron_expr,
			tools_context: routine.tools_context,
			channel: routine.channel || '',
			enabled: !!routine.enabled,
		};
		editError = null;
		// Re-open in friendly mode when the stored cron fits one of the plain
		// shapes; otherwise (e.g. a Brain-proposed expression) keep raw cron.
		const friendly = cronToFriendly(routine.cron_expr);
		if (friendly) {
			editSched = friendly;
			editAdvanced = false;
		} else {
			editAdvanced = true;
		}
		try {
			editPreview = await previewRoutineSchedule(routine.id, 5);
		} catch (err) {
			editPreview = [];
		}
	}

	async function saveEdit() {
		if (editingId === null) return;
		busyId = editingId;
		editError = null;
		try {
			await updateRoutine(editingId, { ...editDraft, channel: (editDraft.channel || '').trim() });
			actionMessage = `Routine #${editingId} updated.`;
			editingId = null;
			await load();
		} catch (err) {
			editError = err instanceof Error ? err.message : String(err);
		} finally {
			busyId = null;
		}
	}

	async function togglePause(routine: Routine) {
		busyId = routine.id;
		try {
			if (routine.enabled) await pauseRoutine(routine.id);
			else await resumeRoutine(routine.id);
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busyId = null;
		}
	}

	async function handleRun(routine: Routine) {
		busyId = routine.id;
		error = null;
		actionMessage = null;
		try {
			const res = await runRoutine(routine.id);
			actionMessage = `Routine '${routine.name}' dispatched (task ${res.display_id || res.task_id}).`;
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busyId = null;
		}
	}

	async function handleDelete(routine: Routine) {
		if (!window.confirm(`Delete routine '${routine.name}'? This cannot be undone.`)) return;
		busyId = routine.id;
		try {
			await deleteRoutine(routine.id);
			actionMessage = `Routine '${routine.name}' deleted.`;
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busyId = null;
		}
	}

	let cronPreviewTimer: ReturnType<typeof setTimeout> | null = null;
	function scheduleCronPreview(expr: string) {
		if (cronPreviewTimer) clearTimeout(cronPreviewTimer);
		cronPreviewTimer = setTimeout(() => void refreshCronPreview(expr), 300);
	}

	// The friendly builder is the source of truth unless advanced mode is on;
	// the backend still stores (and the preview still reads) a UTC cron.
	$: if (!createAdvanced) createForm.cron_expr = friendlyToCron(createSched);
	$: if (editingId !== null && !editAdvanced) editDraft.cron_expr = friendlyToCron(editSched);

	$: scheduleCronPreview(createForm.cron_expr || '');

	onMount(() => {
		void load();
		void loadChannels();
	});
</script>

<svelte:head><title>Routines | Forven</title></svelte:head>

<div class="space-y-6 p-6">
	<header class="flex items-center justify-between">
		<div>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Brain</div>
			<h1 class="text-lg font-bold uppercase tracking-widest text-sc-ink">Routines</h1>
			<p class="mt-1 text-xs text-sc-ink3 max-w-2xl">
				Scheduled instructions the Brain runs autonomously — optionally posting the result to a
				Discord channel. Operator-authored routines are live immediately; Brain-proposed routines
				must be approved on the <a href="/approval" class="underline">/approval</a> page first.
			</p>
		</div>
		<button type="button" class="rounded-md text-[12px] border border-sc-line2 px-3 py-1.5 text-sc-ink2 hover:text-sc-ink hover:border-sc-line2 transition-colors" on:click={() => void load()}>Reload</button>
	</header>

	{#if actionMessage}<div class="border border-emerald-900 bg-emerald-500/5 text-emerald-400 text-xs px-3 py-2">{actionMessage}</div>{/if}
	{#if error}<div class="border border-red-900 bg-red-500/5 text-red-400 text-xs px-3 py-2">{error}</div>{/if}

	<section class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
		<h2 class="text-sm uppercase tracking-wider text-sc-ink2">Create routine</h2>
		<div class="grid sm:grid-cols-2 gap-3">
			<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Name</span>
				<input type="text" bind:value={createForm.name} placeholder="hourly-status-report" class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
			</label>
			<div class="text-xs">
				<div class="flex items-center justify-between gap-2">
					<span class="text-sc-ink3 uppercase tracking-wider">Schedule</span>
					<button type="button" class="rounded-md text-[12px] px-2 py-0.5 border {createAdvanced ? 'bg-sc-raise text-sc-ink border-sc-line2' : 'text-sc-ink3 border-sc-line2 hover:text-sc-ink2'}" on:click={() => (createAdvanced = !createAdvanced)}>Advanced</button>
				</div>
				{#if createAdvanced}
					<input type="text" bind:value={createForm.cron_expr} placeholder="0 14 * * 1" class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink font-mono" />
					<div class="mt-1 text-[11px] text-sc-ink3">Raw 5-field cron, UTC.</div>
				{:else}
					<div class="mt-1 flex flex-wrap items-center gap-2">
						<select bind:value={createSched.freq} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
							{#each FREQ_OPTIONS as opt}<option value={opt.value}>{opt.label}</option>{/each}
						</select>
						{#if createSched.freq === 'minutes' || createSched.freq === 'hours'}
							<span class="text-sc-ink2">every</span>
							<input type="number" min="1" max={createSched.freq === 'minutes' ? 59 : 23} step="1" bind:value={createSched.every} class="rounded-md w-16 bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
							<span class="text-sc-ink2">{createSched.freq}</span>
						{:else}
							{#if createSched.freq === 'weekly'}
								<span class="text-sc-ink2">on</span>
								<select bind:value={createSched.weekday} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
									{#each WEEKDAY_NAMES as day, i}<option value={i}>{day}</option>{/each}
								</select>
							{:else if createSched.freq === 'monthly'}
								<span class="text-sc-ink2">on day</span>
								<input type="number" min="1" max="31" step="1" bind:value={createSched.dom} class="rounded-md w-16 bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
							{/if}
							<span class="text-sc-ink2">at</span>
							<input type="time" bind:value={createSched.time} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
						{/if}
					</div>
					<div class="mt-1 text-[11px] text-sc-ink2">{describeFriendly(createSched)} (your local time)</div>
				{/if}
			</div>
		</div>
		<label class="text-xs block"><span class="text-sc-ink3 uppercase tracking-wider">Prompt</span>
			<textarea rows="3" bind:value={createForm.prompt} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" placeholder="What should the Brain do when this fires?"></textarea>
		</label>
		<div class="grid sm:grid-cols-3 gap-3">
			<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Post result to Discord</span>
				{#if channels.length > 0}
					<select bind:value={createForm.channel} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
						<option value="">— don't post —</option>
						{#each channels as ch}<option value={ch.id}>{ch.label}</option>{/each}
					</select>
				{:else}
					<input type="text" bind:value={createForm.channel} placeholder="channel alias or id (optional)" class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
				{/if}
			</label>
			<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Tools context</span>
				<select bind:value={createForm.tools_context} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
					{#each VALID_CONTEXTS as ctx}<option value={ctx}>{ctx}</option>{/each}
				</select>
			</label>
			<label class="text-xs flex items-center gap-2 mt-5">
				<input type="checkbox" bind:checked={createForm.enabled} />
				<span class="text-sc-ink2 uppercase tracking-wider">Enabled</span>
			</label>
		</div>
		<div class="text-[11px] text-sc-ink3">
			Next 5 fire times (local):
			{#if cronPreviewError}<span class="text-red-400 ml-2">{cronPreviewError}</span>
			{:else if cronPreview.length === 0}<span class="ml-2">--</span>
			{:else}
				<ul class="ml-2 inline-flex flex-wrap gap-2">
					{#each cronPreview as t}<li class="rounded-md border border-sc-line bg-sc-bg px-2 py-0.5 font-mono">{fmtDate(t)}</li>{/each}
				</ul>
			{/if}
		</div>
		<div>
			<button type="button" disabled={creating || !createForm.name.trim() || !createForm.prompt.trim() || !createForm.cron_expr.trim()} class="rounded-md border border-emerald-900 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 px-4 py-2 text-[12px] font-medium disabled:opacity-40" on:click={() => void handleCreate()}>{creating ? 'Creating...' : 'Create routine'}</button>
		</div>
	</section>

	<section class="rounded-md border border-sc-line bg-sc-panel">
		<header class="px-4 py-3 border-b border-sc-line"><h2 class="text-sm uppercase tracking-wider text-sc-ink2">Active routines</h2></header>
		{#if loading}
			<div class="px-4 py-6 text-xs text-sc-ink3">Loading...</div>
		{:else if routines.length === 0}
			<div class="px-4 py-6 text-xs text-sc-ink3">No routines yet.</div>
		{:else}
			<ul class="divide-y divide-sc-line">
				{#each routines as routine (routine.id)}
					<li class="px-4 py-3 space-y-2">
						<div class="flex items-start justify-between gap-3">
							<div>
								<div class="flex items-center gap-2">
									<div class="text-sm font-semibold text-sc-ink">{routine.name}</div>
									{#if routine.approval_id !== null}
										<span class="rounded-md border border-sc-line2 bg-sc-panel2 text-sc-ink2 px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">brain · approval #{routine.approval_id}</span>
									{:else}
										<span class="rounded-md border border-sc-line2 bg-sc-panel2 text-sc-ink2 px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">{routine.created_by ? `operator · ${routine.created_by}` : 'operator'}</span>
									{/if}
									{#if routine.channel}
										<span class="rounded-md border border-sc-line2 bg-sc-panel2 text-sc-ink2 px-1.5 py-0.5 text-[10px]" title="Result is posted to this Discord channel">→ {channelLabel(routine.channel)}</span>
									{/if}
								</div>
								<div class="text-[11px] text-sc-ink3 mt-0.5" title={routine.cron_expr}>{scheduleLabel(routine)} · {routine.tools_context}</div>
							</div>
							<div class="flex items-center gap-2 text-[11px]">
								<span class="rounded-md border px-2 py-0.5 uppercase tracking-wider {routine.enabled ? 'border-emerald-900 bg-emerald-500/10 text-emerald-400' : 'border-sc-line2 bg-sc-panel2 text-sc-ink2'}">
									{routine.enabled ? 'enabled' : 'paused'}
								</span>
								{#if routine.last_status}
									<span class="border px-2 py-0.5 uppercase tracking-wider {statusClass(routine.last_status)}">{routine.last_status}</span>
								{/if}
								<span class="text-sc-ink3">last: {fmtDate(routine.last_run_at)}</span>
							</div>
						</div>
						{#if routine.last_error && ['error', 'failed'].includes((routine.last_status || '').toLowerCase())}
							<div class="text-[11px] text-red-400 border border-red-900 bg-red-500/5 px-2 py-1 whitespace-pre-wrap break-words" title={routine.last_error}>{routine.last_error}</div>
						{/if}
						<div class="text-xs text-sc-ink2 whitespace-pre-wrap line-clamp-3">{routine.prompt}</div>
						<div class="flex flex-wrap gap-2 text-xs pt-1">
							<button type="button" disabled={busyId === routine.id || !routine.enabled} title={routine.enabled ? 'Dispatch this routine now' : 'Resume the routine before running it'} class="rounded-md border border-sc-line2 text-sc-ink2 hover:text-sc-ink hover:border-sc-line2 px-3 py-1 disabled:opacity-40" on:click={() => void handleRun(routine)}>Run now</button>
							<button type="button" disabled={busyId === routine.id} class="rounded-md border border-sc-line2 text-sc-ink2 hover:text-sc-ink px-3 py-1 disabled:opacity-40" on:click={() => void startEdit(routine)}>Edit</button>
							<button type="button" disabled={busyId === routine.id} class="rounded-md border border-sc-line2 text-sc-ink2 hover:text-yellow-400 px-3 py-1 disabled:opacity-40" on:click={() => void togglePause(routine)}>{routine.enabled ? 'Pause' : 'Resume'}</button>
							<button type="button" disabled={busyId === routine.id} class="rounded-md border border-sc-line2 text-sc-ink2 hover:text-red-400 px-3 py-1 disabled:opacity-40" on:click={() => void handleDelete(routine)}>Delete</button>
						</div>

						{#if editingId === routine.id}
							<div class="rounded-md border border-sc-line bg-sc-panel p-3 space-y-2 mt-2">
								<div class="grid sm:grid-cols-2 gap-3">
									<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Name</span>
										<input type="text" bind:value={editDraft.name} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
									</label>
									<div class="text-xs">
										<div class="flex items-center justify-between gap-2">
											<span class="text-sc-ink3 uppercase tracking-wider">Schedule</span>
											<button type="button" class="rounded-md text-[12px] px-2 py-0.5 border {editAdvanced ? 'bg-sc-raise text-sc-ink border-sc-line2' : 'text-sc-ink3 border-sc-line2 hover:text-sc-ink2'}" on:click={() => (editAdvanced = !editAdvanced)}>Advanced</button>
										</div>
										{#if editAdvanced}
											<input type="text" bind:value={editDraft.cron_expr} placeholder="0 14 * * 1" class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink font-mono" />
											<div class="mt-1 text-[11px] text-sc-ink3">Raw 5-field cron, UTC.</div>
										{:else}
											<div class="mt-1 flex flex-wrap items-center gap-2">
												<select bind:value={editSched.freq} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
													{#each FREQ_OPTIONS as opt}<option value={opt.value}>{opt.label}</option>{/each}
												</select>
												{#if editSched.freq === 'minutes' || editSched.freq === 'hours'}
													<span class="text-sc-ink2">every</span>
													<input type="number" min="1" max={editSched.freq === 'minutes' ? 59 : 23} step="1" bind:value={editSched.every} class="rounded-md w-16 bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
													<span class="text-sc-ink2">{editSched.freq}</span>
												{:else}
													{#if editSched.freq === 'weekly'}
														<span class="text-sc-ink2">on</span>
														<select bind:value={editSched.weekday} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
															{#each WEEKDAY_NAMES as day, i}<option value={i}>{day}</option>{/each}
														</select>
													{:else if editSched.freq === 'monthly'}
														<span class="text-sc-ink2">on day</span>
														<input type="number" min="1" max="31" step="1" bind:value={editSched.dom} class="rounded-md w-16 bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
													{/if}
													<span class="text-sc-ink2">at</span>
													<input type="time" bind:value={editSched.time} class="rounded-md bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
												{/if}
											</div>
											<div class="mt-1 text-[11px] text-sc-ink2">{describeFriendly(editSched)} (your local time)</div>
										{/if}
									</div>
								</div>
								<label class="text-xs block"><span class="text-sc-ink3 uppercase tracking-wider">Prompt</span>
									<textarea rows="3" bind:value={editDraft.prompt} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink"></textarea>
								</label>
								<div class="grid sm:grid-cols-3 gap-3">
									<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Post result to Discord</span>
										{#if channels.length > 0}
											<select bind:value={editDraft.channel} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
												<option value="">— don't post —</option>
												{#if editDraft.channel && !channels.some((c) => c.id === editDraft.channel)}
													<option value={editDraft.channel}>{channelLabel(editDraft.channel)}</option>
												{/if}
												{#each channels as ch}<option value={ch.id}>{ch.label}</option>{/each}
											</select>
										{:else}
											<input type="text" bind:value={editDraft.channel} placeholder="channel alias or id (optional)" class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink" />
										{/if}
									</label>
									<label class="text-xs"><span class="text-sc-ink3 uppercase tracking-wider">Context</span>
										<select bind:value={editDraft.tools_context} class="rounded-md mt-1 w-full bg-sc-bg border border-sc-line2 px-2 py-1.5 text-sc-ink">
											{#each VALID_CONTEXTS as ctx}<option value={ctx}>{ctx}</option>{/each}
										</select>
									</label>
									<label class="text-xs flex items-center gap-2 mt-5"><input type="checkbox" bind:checked={editDraft.enabled} /><span class="text-sc-ink2 uppercase tracking-wider">Enabled</span></label>
								</div>
								{#if editPreview.length > 0}
									<div class="text-[11px] text-sc-ink3">Upcoming fires (local): {editPreview.slice(0, 3).map((t) => fmtDate(t)).join(' · ')}</div>
								{/if}
								{#if editError}<div class="text-[11px] text-red-400">{editError}</div>{/if}
								<div class="flex gap-2">
									<button type="button" disabled={busyId === routine.id} class="rounded-md border border-emerald-900 bg-emerald-500/10 text-emerald-400 px-3 py-1 text-[12px] disabled:opacity-40" on:click={() => void saveEdit()}>{busyId === routine.id ? 'Saving...' : 'Save'}</button>
									<button type="button" class="rounded-md border border-sc-line2 text-sc-ink2 hover:text-sc-ink px-3 py-1 text-[12px]" on:click={() => (editingId = null)}>Cancel</button>
								</div>
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
