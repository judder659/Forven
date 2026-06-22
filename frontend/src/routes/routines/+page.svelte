<script lang="ts">
	import { onMount } from 'svelte';
	import {
		createRoutine,
		deleteRoutine,
		listRoutines,
		pauseRoutine,
		previewCronExpression,
		previewRoutineSchedule,
		resumeRoutine,
		runRoutine,
		updateRoutine,
		type Routine,
		type RoutineCreatePayload,
	} from '$lib/api/routines';
	import { minutesToCron, cronToMinutes } from '$lib/utils/schedule';

	type ScheduleMode = 'cron' | 'minutes';

	let routines: Routine[] = [];
	let loading = true;
	let error: string | null = null;
	let actionMessage: string | null = null;
	let busyId: number | null = null;

	let createForm: RoutineCreatePayload = {
		name: '',
		prompt: '',
		cron_expr: '0 14 * * *',
		tools_context: 'scheduled',
		skills: [],
		enabled: true,
	};
	let creating = false;
	let cronPreview: string[] = [];
	let cronPreviewError: string | null = null;
	let skillsInput = '';
	let createMode: ScheduleMode = 'cron';
	let createMinutes = 30;

	let editingId: number | null = null;
	let editDraft: RoutineCreatePayload = { name: '', prompt: '', cron_expr: '' };
	let editSkillsInput = '';
	let editError: string | null = null;
	let editPreview: string[] = [];
	let editMode: ScheduleMode = 'cron';
	let editMinutes = 30;

	const VALID_CONTEXTS = ['scheduled', 'interactive', 'recovery', 'research'];

	const CRON_PRESETS: { label: string; expr: string }[] = [
		{ label: 'Hourly', expr: '0 * * * *' },
		{ label: 'Daily 14:00 UTC', expr: '0 14 * * *' },
		{ label: 'Weekdays 14:00 UTC', expr: '0 14 * * MON-FRI' },
		{ label: 'Weekly (Mon 14:00 UTC)', expr: '0 14 * * MON' },
		{ label: 'Monthly (1st 14:00 UTC)', expr: '0 14 1 * *' },
	];

	function fmtDate(value: string | null | undefined): string {
		if (!value) return '--';
		const d = new Date(String(value));
		if (Number.isNaN(d.getTime())) return '--';
		return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	const DOW_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

	// Plain-English summary of common 5-field cron expressions. Falls back to ''
	// for anything it can't confidently describe (the live preview still covers it).
	function describeCron(expr: string): string {
		const parts = (expr || '').trim().split(/\s+/);
		if (parts.length !== 5) return '';
		const [min, hour, dom, mon, dow] = parts;
		if (mon !== '*') return '';
		const num = (v: string) => (/^\d+$/.test(v) ? Number(v) : null);
		const m = num(min);
		const h = num(hour);
		const timeAt = m !== null && h !== null
			? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')} UTC`
			: null;

		if (min === '*' && hour === '*' && dom === '*' && dow === '*') return 'Every minute';
		if (m !== null && hour === '*' && dom === '*' && dow === '*') return `Every hour at :${String(m).padStart(2, '0')}`;
		if (!timeAt) return '';
		if (dom === '*' && dow === '*') return `Every day at ${timeAt}`;
		if (dom === '*' && dow !== '*') {
			const label = dowLabel(dow);
			return label ? `Every ${label} at ${timeAt}` : '';
		}
		if (dom !== '*' && dow === '*') {
			const d = num(dom);
			return d !== null ? `On day ${d} of the month at ${timeAt}` : '';
		}
		return '';
	}

	function dowLabel(dow: string): string {
		const named: Record<string, string> = {
			MON: 'Monday', TUE: 'Tuesday', WED: 'Wednesday', THU: 'Thursday',
			FRI: 'Friday', SAT: 'Saturday', SUN: 'Sunday',
		};
		const upper = dow.toUpperCase();
		if (upper === 'MON-FRI' || dow === '1-5') return 'weekday';
		if (named[upper]) return named[upper];
		const n = Number(dow);
		if (Number.isInteger(n) && n >= 0 && n <= 6) return DOW_FULL[n];
		return '';
	}

	function statusClass(status: string | null): string {
		switch ((status || '').toLowerCase()) {
			case 'dispatched':
			case 'ok':
			case 'completed':
				return 'text-emerald-300 border-emerald-700 bg-emerald-900/20';
			case 'error':
			case 'failed':
				return 'text-red-300 border-red-700 bg-red-900/20';
			default:
				return 'text-gray-400 border-[#333] bg-[#111]';
		}
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
			const skills = skillsInput.split(',').map((s) => s.trim()).filter(Boolean);
			await createRoutine({ ...createForm, skills });
			actionMessage = `Routine '${createForm.name}' created.`;
			createForm = { name: '', prompt: '', cron_expr: '0 14 * * *', tools_context: 'scheduled', skills: [], enabled: true };
			skillsInput = '';
			cronPreview = [];
			createMode = 'cron';
			createMinutes = 30;
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
			skills: routine.skills,
			enabled: !!routine.enabled,
		};
		editSkillsInput = (routine.skills || []).join(', ');
		editError = null;
		// Re-open in "every N minutes" mode when the stored cron is a simple
		// interval; otherwise keep the raw cron editor.
		const asMinutes = cronToMinutes(routine.cron_expr);
		if (asMinutes !== null) {
			editMode = 'minutes';
			editMinutes = asMinutes;
		} else {
			editMode = 'cron';
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
			const skills = editSkillsInput.split(',').map((s) => s.trim()).filter(Boolean);
			await updateRoutine(editingId, { ...editDraft, skills });
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

	// In "every N minutes" mode the cron field is derived from the minutes value;
	// the backend still stores (and the preview still reads) a cron expression.
	$: if (createMode === 'minutes') createForm.cron_expr = minutesToCron(createMinutes);
	$: if (editMode === 'minutes') editDraft.cron_expr = minutesToCron(editMinutes);

	$: scheduleCronPreview(createForm.cron_expr || '');

	onMount(load);
</script>

<svelte:head><title>Routines | Forven</title></svelte:head>

<div class="space-y-6 p-6">
	<header class="flex items-center justify-between">
		<div>
			<div class="text-[11px] uppercase tracking-[0.18em] text-gray-500">Brain</div>
			<h1 class="text-2xl font-semibold text-gray-100">Routines</h1>
			<p class="mt-1 text-xs text-gray-500 max-w-2xl">
				Scheduled NL prompts the Brain runs autonomously. Operator-authored routines are
				live immediately; Brain-proposed routines must be approved on the
				<a href="/approval" class="underline">/approval</a> page first.
			</p>
		</div>
		<button type="button" class="text-xs border border-[#333] px-3 py-1.5 rounded text-gray-300" on:click={() => void load()}>Reload</button>
	</header>

	{#if actionMessage}<div class="bg-emerald-900/20 border border-emerald-800 text-emerald-300 text-xs px-3 py-2 rounded">{actionMessage}</div>{/if}
	{#if error}<div class="bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{error}</div>{/if}

	<section class="border border-[#222] bg-[#0a0a0a] rounded p-4 space-y-3">
		<h2 class="text-sm uppercase tracking-wider text-gray-400">Create routine</h2>
		<div class="grid sm:grid-cols-2 gap-3">
			<label class="text-xs"><span class="text-gray-500 uppercase tracking-wider">Name</span>
				<input type="text" bind:value={createForm.name} placeholder="weekly-postmortem" class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
			</label>
			<div class="text-xs">
				<div class="flex items-center justify-between gap-2">
					<span class="text-gray-500 uppercase tracking-wider">Schedule (UTC)</span>
					<div class="inline-flex rounded overflow-hidden border border-[#222] text-[10px]">
						<button type="button" class="px-2 py-0.5 {createMode === 'minutes' ? 'bg-[#1a1a1a] text-gray-100' : 'text-gray-500 hover:text-gray-300'}" on:click={() => (createMode = 'minutes')}>Every N min</button>
						<button type="button" class="px-2 py-0.5 {createMode === 'cron' ? 'bg-[#1a1a1a] text-gray-100' : 'text-gray-500 hover:text-gray-300'}" on:click={() => (createMode = 'cron')}>Cron</button>
					</div>
				</div>
				{#if createMode === 'minutes'}
					<div class="mt-1 flex items-center gap-2">
						<span class="text-gray-400">Run every</span>
						<input type="number" min="1" step="1" bind:value={createMinutes} class="w-20 bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
						<span class="text-gray-400">minutes</span>
					</div>
					<div class="mt-1 text-[11px] text-gray-500 font-mono">cron: {createForm.cron_expr || '--'}</div>
				{:else}
					<input type="text" bind:value={createForm.cron_expr} placeholder="0 14 * * MON" class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200 font-mono" />
					<div class="mt-1 flex flex-wrap gap-1">
						{#each CRON_PRESETS as preset}
							<button type="button" class="border border-[#333] text-gray-400 hover:text-gray-100 hover:border-[#555] px-1.5 py-0.5 rounded text-[10px]" on:click={() => (createForm.cron_expr = preset.expr)}>{preset.label}</button>
						{/each}
					</div>
					{#if describeCron(createForm.cron_expr || '')}
						<div class="mt-1 text-[11px] text-gray-400">{describeCron(createForm.cron_expr || '')}</div>
					{/if}
				{/if}
			</div>
		</div>
		<label class="text-xs block"><span class="text-gray-500 uppercase tracking-wider">Prompt</span>
			<textarea rows="3" bind:value={createForm.prompt} class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200" placeholder="What should the Brain do when this fires?"></textarea>
		</label>
		<div class="grid sm:grid-cols-3 gap-3">
			<label class="text-xs"><span class="text-gray-500 uppercase tracking-wider">Tools context</span>
				<select bind:value={createForm.tools_context} class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200">
					{#each VALID_CONTEXTS as ctx}<option value={ctx}>{ctx}</option>{/each}
				</select>
			</label>
			<label class="text-xs"><span class="text-gray-500 uppercase tracking-wider">Skills (comma-separated)</span>
				<input type="text" bind:value={skillsInput} placeholder="postmortem-review, decay-watch" class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
			</label>
			<label class="text-xs flex items-center gap-2 mt-5">
				<input type="checkbox" bind:checked={createForm.enabled} />
				<span class="text-gray-300 uppercase tracking-wider">Enabled</span>
			</label>
		</div>
		<div class="text-[11px] text-gray-500">
			Next 5 fire times (local):
			{#if cronPreviewError}<span class="text-red-400 ml-2">{cronPreviewError}</span>
			{:else if cronPreview.length === 0}<span class="ml-2">--</span>
			{:else}
				<ul class="ml-2 inline-flex flex-wrap gap-2">
					{#each cronPreview as t}<li class="border border-[#222] bg-black px-2 py-0.5 rounded font-mono">{fmtDate(t)}</li>{/each}
				</ul>
			{/if}
		</div>
		<div>
			<button type="button" disabled={creating || !createForm.name.trim() || !createForm.prompt.trim() || !createForm.cron_expr.trim()} class="border border-emerald-700 bg-emerald-900/20 hover:bg-emerald-900/40 text-emerald-300 px-4 py-2 rounded text-xs disabled:opacity-40" on:click={() => void handleCreate()}>{creating ? 'Creating...' : 'Create routine'}</button>
		</div>
	</section>

	<section class="border border-[#222] bg-[#0a0a0a] rounded">
		<header class="px-4 py-3 border-b border-[#222]"><h2 class="text-sm uppercase tracking-wider text-gray-400">Active routines</h2></header>
		{#if loading}
			<div class="px-4 py-6 text-xs text-gray-500">Loading...</div>
		{:else if routines.length === 0}
			<div class="px-4 py-6 text-xs text-gray-500">No routines yet.</div>
		{:else}
			<ul class="divide-y divide-[#1a1a1a]">
				{#each routines as routine (routine.id)}
					<li class="px-4 py-3 space-y-2">
						<div class="flex items-start justify-between gap-3">
							<div>
								<div class="flex items-center gap-2">
									<div class="text-sm font-semibold text-gray-100">{routine.name}</div>
									{#if routine.approval_id !== null}
										<span class="border border-[#333] bg-[#111] text-gray-400 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider">brain · approval #{routine.approval_id}</span>
									{:else}
										<span class="border border-[#333] bg-[#111] text-gray-400 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider">{routine.created_by ? `operator · ${routine.created_by}` : 'operator'}</span>
									{/if}
								</div>
								<div class="text-[11px] text-gray-500 font-mono mt-0.5">{routine.cron_expr} · {routine.tools_context}</div>
							</div>
							<div class="flex items-center gap-2 text-[11px]">
								<span class="border rounded px-2 py-0.5 uppercase tracking-wider {routine.enabled ? 'border-emerald-700 bg-emerald-900/20 text-emerald-300' : 'border-[#333] bg-[#111] text-gray-400'}">
									{routine.enabled ? 'enabled' : 'paused'}
								</span>
								{#if routine.last_status}
									<span class="border rounded px-2 py-0.5 uppercase tracking-wider {statusClass(routine.last_status)}">{routine.last_status}</span>
								{/if}
								<span class="text-gray-500">last: {fmtDate(routine.last_run_at)}</span>
							</div>
						</div>
						{#if routine.last_error && ['error', 'failed'].includes((routine.last_status || '').toLowerCase())}
							<div class="text-[11px] text-red-400 border border-red-900/40 bg-red-900/10 rounded px-2 py-1 whitespace-pre-wrap break-words" title={routine.last_error}>{routine.last_error}</div>
						{/if}
						<div class="text-xs text-gray-300 whitespace-pre-wrap line-clamp-3">{routine.prompt}</div>
						{#if routine.skills && routine.skills.length > 0}
							<div class="flex flex-wrap gap-1 text-[10px]">
								{#each routine.skills as s}<span class="border border-[#222] bg-black px-2 py-0.5 rounded font-mono">{s}</span>{/each}
							</div>
						{/if}
						<div class="flex flex-wrap gap-2 text-xs pt-1">
							<button type="button" disabled={busyId === routine.id || !routine.enabled} title={routine.enabled ? 'Dispatch this routine now' : 'Resume the routine before running it'} class="border border-sky-700 bg-sky-900/20 text-sky-300 hover:bg-sky-900/40 px-3 py-1 rounded disabled:opacity-40" on:click={() => void handleRun(routine)}>Run now</button>
							<button type="button" disabled={busyId === routine.id} class="border border-[#333] text-gray-300 hover:text-gray-100 px-3 py-1 rounded disabled:opacity-40" on:click={() => void startEdit(routine)}>Edit</button>
							<button type="button" disabled={busyId === routine.id} class="border border-[#333] text-gray-300 hover:text-amber-300 px-3 py-1 rounded disabled:opacity-40" on:click={() => void togglePause(routine)}>{routine.enabled ? 'Pause' : 'Resume'}</button>
							<button type="button" disabled={busyId === routine.id} class="border border-[#333] text-gray-300 hover:text-red-300 px-3 py-1 rounded disabled:opacity-40" on:click={() => void handleDelete(routine)}>Delete</button>
						</div>

						{#if editingId === routine.id}
							<div class="border border-[#333] bg-[#0d0d0d] rounded p-3 space-y-2 mt-2">
								<div class="grid sm:grid-cols-2 gap-3">
									<label class="text-xs"><span class="text-gray-500 uppercase tracking-wider">Name</span>
										<input type="text" bind:value={editDraft.name} class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
									</label>
									<div class="text-xs">
										<div class="flex items-center justify-between gap-2">
											<span class="text-gray-500 uppercase tracking-wider">Schedule (UTC)</span>
											<div class="inline-flex rounded overflow-hidden border border-[#222] text-[10px]">
												<button type="button" class="px-2 py-0.5 {editMode === 'minutes' ? 'bg-[#1a1a1a] text-gray-100' : 'text-gray-500 hover:text-gray-300'}" on:click={() => (editMode = 'minutes')}>Every N min</button>
												<button type="button" class="px-2 py-0.5 {editMode === 'cron' ? 'bg-[#1a1a1a] text-gray-100' : 'text-gray-500 hover:text-gray-300'}" on:click={() => (editMode = 'cron')}>Cron</button>
											</div>
										</div>
										{#if editMode === 'minutes'}
											<div class="mt-1 flex items-center gap-2">
												<span class="text-gray-400">Run every</span>
												<input type="number" min="1" step="1" bind:value={editMinutes} class="w-20 bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
												<span class="text-gray-400">minutes</span>
											</div>
											<div class="mt-1 text-[11px] text-gray-500 font-mono">cron: {editDraft.cron_expr || '--'}</div>
										{:else}
											<input type="text" bind:value={editDraft.cron_expr} placeholder="0 14 * * MON" class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200 font-mono" />
										{/if}
									</div>
								</div>
								<label class="text-xs block"><span class="text-gray-500 uppercase tracking-wider">Prompt</span>
									<textarea rows="3" bind:value={editDraft.prompt} class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200"></textarea>
								</label>
								<div class="grid sm:grid-cols-2 gap-3">
									<label class="text-xs"><span class="text-gray-500 uppercase tracking-wider">Context</span>
										<select bind:value={editDraft.tools_context} class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200">
											{#each VALID_CONTEXTS as ctx}<option value={ctx}>{ctx}</option>{/each}
										</select>
									</label>
									<label class="text-xs flex items-center gap-2 mt-5"><input type="checkbox" bind:checked={editDraft.enabled} /><span class="text-gray-300 uppercase tracking-wider">Enabled</span></label>
								</div>
								<label class="text-xs block"><span class="text-gray-500 uppercase tracking-wider">Skills (comma-separated)</span>
									<input type="text" bind:value={editSkillsInput} placeholder="postmortem-review, decay-watch" class="mt-1 w-full bg-black border border-[#222] px-2 py-1.5 text-gray-200" />
								</label>
								{#if editPreview.length > 0}
									<div class="text-[11px] text-gray-500">Upcoming fires (local): {editPreview.slice(0, 3).map((t) => fmtDate(t)).join(' · ')}</div>
								{/if}
								{#if editError}<div class="text-[11px] text-red-400">{editError}</div>{/if}
								<div class="flex gap-2">
									<button type="button" disabled={busyId === routine.id} class="border border-emerald-700 bg-emerald-900/20 text-emerald-300 px-3 py-1 rounded text-xs disabled:opacity-40" on:click={() => void saveEdit()}>{busyId === routine.id ? 'Saving...' : 'Save'}</button>
									<button type="button" class="border border-[#333] text-gray-300 px-3 py-1 rounded text-xs" on:click={() => (editingId = null)}>Cancel</button>
								</div>
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
