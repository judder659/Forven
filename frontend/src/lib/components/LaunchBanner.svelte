<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getResumableTasks,
		resumeTask,
		type ResumableTask,
	} from '$lib/api/diagnostics';

	let tasks: ResumableTask[] = [];
	let dismissed = false;
	let loading = false;
	let error = '';
	let resumingAll = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const POLL_MS = 60_000;
	const SESSION_KEY = 'forven.launch_banner.dismissed';

	async function refresh() {
		if (dismissed) return;
		loading = true;
		error = '';
		try {
			const res = await getResumableTasks();
			tasks = res.tasks ?? [];
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to check resumable tasks.';
		} finally {
			loading = false;
		}
	}

	async function handleResumeAll() {
		if (resumingAll || tasks.length === 0) return;
		resumingAll = true;
		error = '';
		try {
			for (const t of tasks) {
				try {
					await resumeTask(t.id);
				} catch (err) {
					error = err instanceof Error ? err.message : 'Resume failed for one or more tasks.';
				}
			}
			await refresh();
		} finally {
			resumingAll = false;
		}
	}

	function handleDismiss() {
		dismissed = true;
		try {
			sessionStorage.setItem(SESSION_KEY, '1');
		} catch {
			// sessionStorage unavailable (e.g. in Tauri restricted context); just dismiss for the lifetime of this mount.
		}
	}

	function openDiagnostics() {
		goto('/diagnostics');
	}

	onMount(() => {
		try {
			dismissed = sessionStorage.getItem(SESSION_KEY) === '1';
		} catch {
			dismissed = false;
		}
		void refresh();
		pollTimer = setInterval(() => {
			void refresh();
		}, POLL_MS);
	});

	onDestroy(() => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	});

	$: visible = !dismissed && tasks.length > 0;
	$: countLabel = tasks.length === 1 ? '1 interrupted task' : `${tasks.length} interrupted tasks`;
</script>

{#if visible}
	<div
		class="border-b border-yellow-900 bg-yellow-500/5 text-yellow-400 px-4 py-2 flex items-center justify-between gap-3"
		role="status"
	>
		<div class="flex items-center gap-3 min-w-0">
			<svg class="w-4 h-4 text-yellow-400 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M12 2L1 21h22L12 2zm0 4.83L19.53 19H4.47L12 6.83zM11 10v4h2v-4h-2zm0 6v2h2v-2h-2z" />
			</svg>
			<div class="min-w-0">
				<div class="text-xs font-bold truncate">
					{countLabel} from a previous session
				</div>
				<div class="text-[11px] text-[#888] truncate">
					Tasks left running when the app closed are recoverable. Resume to re-queue them.
				</div>
			</div>
		</div>
		<div class="flex items-center gap-2 shrink-0">
			<button
				type="button"
				class="text-[11px] border border-yellow-900 text-yellow-400 px-2.5 py-1 hover:bg-yellow-500/10 transition-colors disabled:opacity-60"
				on:click={handleResumeAll}
				disabled={resumingAll || loading}
			>
				{resumingAll ? 'Resuming…' : 'Resume all'}
			</button>
			<button
				type="button"
				class="text-[11px] border border-[#333] text-[#888] px-2.5 py-1 hover:bg-[#111] hover:text-white transition-colors"
				on:click={openDiagnostics}
			>
				Review
			</button>
			<button
				type="button"
				class="text-[11px] text-[#666] hover:text-white px-2 transition-colors"
				on:click={handleDismiss}
				aria-label="Dismiss"
			>
				✕
			</button>
		</div>
	</div>
	{#if error}
		<div class="border-b border-red-900 bg-red-500/5 text-red-400 px-4 py-1.5 text-[11px]">
			{error}
		</div>
	{/if}
{/if}
