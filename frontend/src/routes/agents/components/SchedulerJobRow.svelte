<script lang="ts">
	import type { ForvenSchedulerJob } from '$lib/api';
	import { msToMinutes, minutesToMs, formatIntervalMs } from '$lib/utils/schedule';

	export let job: ForvenSchedulerJob;
	export let onSave: (jobId: string | number, scheduleType: string, scheduleExpr: string, enabled: boolean) => Promise<void>;
	export let showErrors = false;

	let isEditing = false;
	let draftType: 'cron' | 'interval' = 'cron';
	let draftExpr = '';
	let saving = false;
	let errorMessage = '';
	let showError = false;
	let autoExpanded = false;

	const scheduleTypeOptions = [
		{ value: 'cron', label: 'cron' },
		{ value: 'interval', label: 'interval (min)' }
	] as const;

	$: normalizedType = job.schedule_type === 'interval' || job.schedule_type === 'cron' ? job.schedule_type : 'cron';
	$: normalizedExpr = (job.schedule_expr ?? '').trim();
	$: if (!isEditing) {
		draftType = normalizedType === 'interval' ? 'interval' : 'cron';
		draftExpr = toDisplayExpr(normalizedExpr, draftType);
	}

	// Interval schedules are stored as a millisecond string but shown/edited in
	// minutes; cron schedules pass through unchanged.
	function toDisplayExpr(expr: string, type: 'cron' | 'interval'): string {
		return type === 'interval' ? msToMinutes(expr) : expr;
	}

	function toStoredExpr(expr: string, type: 'cron' | 'interval'): string {
		return type === 'interval' ? minutesToMs(expr) : expr.trim();
	}

	function canSave(): boolean {
		if (saving) return false;
		return draftExpr.trim().length > 0;
	}

	function hasJobId(value: string | number | undefined | null): value is string | number {
		return value !== undefined && value !== null && String(value).trim() !== '';
	}

	function parseNextRun(iso?: string | null): string {
		if (!iso) return '--';
		const parsed = new Date(iso);
		if (Number.isNaN(parsed.getTime())) return '--';
		const diffMs = parsed.getTime() - Date.now();
		if (diffMs <= 0) return 'due now';
		const minutes = Math.max(1, Math.round(diffMs / 60000));
		if (minutes < 60) return `${minutes}m`;
		if (minutes < 24 * 60) return `${Math.floor(minutes / 60)}h`;
		return `${Math.floor(minutes / (24 * 60))}d`;
	}

	function displaySchedule(): string {
		if (!normalizedExpr) return '--';
		if (normalizedType === 'interval') return formatIntervalMs(normalizedExpr);
		return normalizedExpr;
	}

	function statusClass(status?: string | null): string {
		if (!status) return 'border-[#333] text-[#888]';
		const value = status.toLowerCase();
		if (value === 'pending') return 'border-[#333] text-[#888]';
		if (value === 'running') return 'border-yellow-500/50 text-yellow-400';
		if (value === 'done' || value === 'completed') return 'border-emerald-500/50 text-emerald-400';
		if (value === 'reviewed') return 'border-[#333] text-[#ccc]';
		if (value === 'error') return 'border-red-900 text-red-400';
		if (value === 'disabled') return 'border-[#333] text-[#555]';
		return 'border-[#333] text-[#888]';
	}

	async function handleSave() {
		errorMessage = '';
		const payload = toStoredExpr(draftExpr, draftType);
		if (!payload) {
			errorMessage =
				draftType === 'interval' ? 'Enter a positive number of minutes' : 'Schedule expression is required';
			return;
		}
		if (!hasJobId(job.id)) {
			errorMessage = 'Missing job id';
			return;
		}
		saving = true;
		try {
			await onSave(job.id, draftType, payload, Boolean(job.enabled));
			draftExpr = toDisplayExpr(payload, draftType);
			isEditing = false;
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to save job';
		} finally {
			saving = false;
		}
	}

	function handleCancel() {
		draftType = normalizedType === 'interval' ? 'interval' : 'cron';
		draftExpr = toDisplayExpr(normalizedExpr, draftType);
		errorMessage = '';
		isEditing = false;
	}

	function startEdit() {
		draftType = normalizedType === 'interval' ? 'interval' : 'cron';
		draftExpr = toDisplayExpr(normalizedExpr, draftType);
		errorMessage = '';
		isEditing = true;
	}

	async function handleEnabledToggle(event: Event) {
		if (!hasJobId(job.id)) {
			errorMessage = 'Missing job id';
			return;
		}
		const checked = (event.currentTarget as HTMLInputElement).checked;
		errorMessage = '';
		try {
			// Toggling enabled must not reinterpret the schedule, so send the
			// job's existing stored value as-is.
			await onSave(job.id, normalizedType, normalizedExpr, checked);
		} catch (err) {
			errorMessage = err instanceof Error ? err.message : 'Failed to toggle job';
		}
	}

	$: if (!showErrors) {
		showError = false;
		autoExpanded = false;
	}
	$: if (showErrors && job.last_error && !autoExpanded) {
		showError = true;
		autoExpanded = true;
	}
</script>

<tr class="hover:bg-[#111] transition-colors {job.enabled === false ? 'opacity-60' : ''}">
	<td class="px-4 py-2 text-[#ccc]">
		<div class="flex items-center gap-2">
			<span class="font-bold">{job.name || '(unnamed)'}</span>
			{#if !isEditing}
				<button
					type="button"
					class="text-[#666] hover:text-white transition-colors"
					aria-label={`Edit schedule for ${job.name || 'this job'}`}
					on:click={startEdit}
				>
					<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="currentColor">
						<path d="M3 17.25V21h3.75l11.05-11.05-3.75-3.75L3 17.25zm3.06 3.19h-2.06v-2.06l10.34-10.35 2.06 2.06L6.06 20.44zm13.94-14.19l-2.06 2.06 1.16 1.16 2.06-2.06-1.16-1.16z" />
					</svg>
				</button>
			{/if}
		</div>
		{#if errorMessage}
			<div class="text-[10px] text-red-400 mt-1">{errorMessage}</div>
		{/if}
	</td>
	<td class="px-4 py-2 text-[#888]">
		{#if isEditing}
			<div class="flex items-center gap-2">
				<select
					class="terminal-select !w-28 py-1"
					bind:value={draftType}
					on:change={() => (draftExpr = '')}
				>
					{#each scheduleTypeOptions as option}
						<option value={option.value}>{option.label}</option>
					{/each}
				</select>
				{#if draftType === 'interval'}
					<input
						class="terminal-input py-1 !w-24"
						type="number"
						min="1"
						step="1"
						placeholder="minutes"
						value={draftExpr}
						on:input={(e) => (draftExpr = e.currentTarget.value)}
					/>
				{:else}
					<input
						class="terminal-input py-1"
						type="text"
						placeholder="cron e.g. 0 9 * * *"
						bind:value={draftExpr}
					/>
				{/if}
				<div class="flex items-center gap-1">
					<button
						type="button"
						class="terminal-button text-xs px-2 py-1"
						disabled={!canSave()}
						on:click={handleSave}
					>
						{saving ? 'Saving...' : 'Save'}
					</button>
					<button
						type="button"
						class="terminal-button text-xs px-2 py-1"
						disabled={saving}
						on:click={handleCancel}
					>
						Cancel
					</button>
				</div>
			</div>
		{:else}
			<div>{displaySchedule()}</div>
		{/if}
	</td>
	<td class="px-4 py-2 text-[#888]">{parseNextRun(job.next_run_at ?? null)}</td>
	<td class="px-4 py-2">
		<span class="text-[10px] px-1.5 py-0.5 border {statusClass(job.last_status || 'pending')} uppercase font-bold tracking-wider">
			{job.last_status || 'pending'}
		</span>
	</td>
	<td class="px-4 py-2">
		<label class="relative inline-flex items-center cursor-pointer">
			<input
				type="checkbox"
				class="sr-only peer"
				checked={Boolean(job.enabled)}
				on:change={handleEnabledToggle}
			/>
			<div class="w-10 h-5 bg-[#333] rounded-full peer-checked:bg-emerald-500 transition-colors relative after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:w-4 after:h-4 after:bg-[#111] after:border after:border-[#333] after:rounded-full after:transition-transform peer-checked:after:translate-x-[20px]"></div>
		</label>
	</td>
</tr>
{#if showErrors && job.last_error}
	<tr class="bg-red-500/5 border-t border-red-900">
		<td class="px-4 py-2" colspan="5">
			<button
				type="button"
				class="text-[11px] uppercase tracking-wider text-red-400 hover:text-red-300"
				on:click={() => (showError = !showError)}
			>
				{showError ? 'Hide error' : 'Show error'}
			</button>
			{#if showError}
				<div class="mt-2 text-[11px] text-red-400 bg-red-500/5 border border-red-900 p-2 whitespace-pre-wrap">{job.last_error}</div>
			{/if}
		</td>
	</tr>
{/if}
