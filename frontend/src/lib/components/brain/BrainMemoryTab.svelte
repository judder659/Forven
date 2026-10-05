<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getBrainMemory,
		getBrainMemoryHistory,
		putBrainMemory,
		type BrainMemoryHistoryRow,
		type BrainMemoryState
	} from '$lib/api/brain';

	let state: BrainMemoryState | null = null;
	let history: BrainMemoryHistoryRow[] = [];
	let loading = true;
	let error = '';
	let saveError = '';
	let toast = '';
	let toastTimer: ReturnType<typeof setTimeout> | null = null;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	let editing = false;
	let draft = '';
	let saving = false;
	let lastSeenUpdatedAt: string | null = null;

	// In-page confirm dialog (replaces window.confirm so the prompt is themed
	// and testable rather than a native browser modal).
	let confirmMessage = '';
	let confirmResolve: ((ok: boolean) => void) | null = null;

	function askConfirm(message: string): Promise<boolean> {
		confirmMessage = message;
		return new Promise<boolean>((resolve) => {
			confirmResolve = resolve;
		});
	}

	function resolveConfirm(ok: boolean) {
		const resolve = confirmResolve;
		confirmResolve = null;
		confirmMessage = '';
		if (resolve) resolve(ok);
	}

	$: charCount = draft.length;
	$: cap = state?.cap ?? 2000;
	$: pct = cap === 0 ? 0 : Math.min(100, (charCount / cap) * 100);
	$: overCap = charCount > cap;
	$: barClass =
		pct >= 80
			? 'bg-[#e5574f]'
			: pct >= 60
				? 'bg-[#e7b24a]'
				: 'bg-[#3cc48f]';

	const PRIMARY =
		'rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black hover:bg-white disabled:cursor-not-allowed disabled:opacity-40';
	const BUTTON =
		'rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink disabled:cursor-not-allowed disabled:opacity-50';
	const BADGE = 'rounded border px-1.5 py-px font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.06em]';
	const EXCERPT = 'min-w-0 rounded border border-sc-line bg-sc-bg p-1.5';
	const EXCERPT_LABEL = 'mb-1 block font-plex-cond text-[10.5px] uppercase tracking-[0.08em] text-sc-ink3';
	const EXCERPT_PRE = 'm-0 whitespace-pre-wrap break-words font-plex-mono text-[11.5px] text-sc-ink';

	function mutationClass(type: string | null | undefined): string {
		if (type === 'add') return `${BADGE} border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]`;
		if (type === 'remove') return `${BADGE} border-[#e5574f]/40 bg-[#e5574f]/10 text-[#f2956f]`;
		return `${BADGE} border-sc-line2 bg-sc-raise text-sc-ink2`;
	}

	function showToast(message: string) {
		toast = message;
		if (toastTimer) clearTimeout(toastTimer);
		toastTimer = setTimeout(() => {
			toast = '';
		}, 5000);
	}

	async function refresh(silent = false) {
		try {
			const [next, hist] = await Promise.all([
				getBrainMemory(),
				getBrainMemoryHistory(20)
			]);
			if (
				editing &&
				lastSeenUpdatedAt &&
				next.updated_at &&
				next.updated_at !== lastSeenUpdatedAt
			) {
				showToast(
					'Memory was modified outside this view. Cancel to see the latest, or Save to overwrite.'
				);
			}
			state = next;
			history = hist.history;
			if (!editing) {
				lastSeenUpdatedAt = next.updated_at;
			}
			error = '';
		} catch (e) {
			if (!silent) {
				error = e instanceof Error ? e.message : String(e);
			}
		} finally {
			loading = false;
		}
	}

	function startEdit() {
		draft = state?.body ?? '';
		lastSeenUpdatedAt = state?.updated_at ?? null;
		editing = true;
		saveError = '';
	}

	function cancelEdit() {
		editing = false;
		draft = '';
		saveError = '';
	}

	async function save() {
		if (overCap) return;
		const body = draft;
		if (state && body === state.body) {
			editing = false;
			showToast('No changes to save.');
			return;
		}
		const confirmed = await askConfirm(
			`Replace the Brain memory body? (${body.length} of ${cap} chars)`
		);
		if (!confirmed) return;
		saving = true;
		saveError = '';
		try {
			const next = await putBrainMemory(body);
			state = next;
			lastSeenUpdatedAt = next.updated_at;
			editing = false;
			draft = '';
			showToast('Memory saved.');
			const hist = await getBrainMemoryHistory(20);
			history = hist.history;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			if (msg.includes('memory_cap_exceeded') || msg.includes('422')) {
				saveError = `Save rejected: body exceeds ${cap}-char cap.`;
			} else {
				saveError = `Save failed: ${msg}`;
			}
		} finally {
			saving = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	onMount(() => {
		void refresh(false);
		pollTimer = setInterval(() => {
			if (document.visibilityState === 'visible') {
				void refresh(true);
			}
		}, 30000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
		if (toastTimer) clearTimeout(toastTimer);
		// Reject any in-flight confirm so a pending save() promise can settle.
		if (confirmResolve) resolveConfirm(false);
	});
</script>

<div class="flex flex-col gap-4">
	{#if loading}
		<div class="text-[13px] text-sc-ink3">Loading memory…</div>
	{:else if error}
		<div class="rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12.5px] text-[#f2956f]" role="alert">
			<strong class="font-medium">Failed to load memory:</strong>
			{error}
			<button class="ml-2 underline transition-colors hover:text-sc-ink" type="button" on:click={() => refresh(false)}>retry</button>
		</div>
	{:else if state}
		<div class="flex flex-wrap gap-x-6 gap-y-1 text-[12.5px] text-sc-ink2">
			<span>Updated by <strong class="font-medium text-sc-ink">{state.updated_by ?? '—'}</strong></span>
			<span>at {formatTimestamp(state.updated_at)}</span>
			<span class="font-plex-mono text-[12px]">{state.char_count} / {state.cap} chars</span>
		</div>

		{#if editing}
			<div class="flex flex-col gap-2 rounded-md border border-sc-line bg-sc-panel p-3.5">
				<textarea
					class="w-full resize-y rounded-md border border-sc-line2 bg-sc-bg p-3 font-plex-mono text-[13px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4 disabled:opacity-60"
					bind:value={draft}
					rows="14"
					placeholder="Brain operational notes…"
					disabled={saving}
				></textarea>
				<div class="flex items-center gap-3">
					<div class="h-1.5 flex-1 overflow-hidden rounded-full bg-sc-raise">
						<div class="h-full transition-[width] duration-100 {barClass}" style="width: {Math.min(100, pct)}%"></div>
					</div>
					<span
						class="min-w-[80px] text-right font-plex-mono text-[12px] {overCap ? 'font-semibold text-[#f2956f]' : 'text-sc-ink2'}"
					>
						{charCount} / {cap}
					</span>
				</div>
				{#if saveError}
					<div class="rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-1.5 text-[12.5px] text-[#f2956f]" role="alert">{saveError}</div>
				{/if}
				<div class="mt-1 flex flex-wrap gap-2">
					<button
						type="button"
						class={PRIMARY}
						on:click={save}
						disabled={saving || overCap}
					>
						{saving ? 'Saving…' : 'Save'}
					</button>
					<button type="button" class={BUTTON} on:click={cancelEdit} disabled={saving}>Cancel</button>
				</div>
			</div>
		{:else}
			<div class="flex flex-col gap-2">
				{#if state.body}
					<pre class="m-0 whitespace-pre-wrap break-words rounded-md border border-sc-line bg-sc-panel p-3.5 font-plex-mono text-[13px] leading-relaxed text-sc-ink">{state.body}</pre>
				{:else}
					<p class="m-0 text-[13px] text-sc-ink3">Brain memory is empty.</p>
				{/if}
				<div class="mt-1 flex flex-wrap gap-2">
					<button type="button" class={PRIMARY} on:click={startEdit}>Edit</button>
					<button type="button" class={BUTTON} on:click={() => refresh(false)}>Refresh</button>
				</div>
			</div>
		{/if}

		<section>
			<h2 class="m-0 mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Recent changes</h2>
			{#if history.length === 0}
				<p class="m-0 text-[13px] text-sc-ink3">No mutations recorded yet.</p>
			{:else}
				<ul class="m-0 flex list-none flex-col gap-2 p-0">
					{#each history as row (row.id)}
						<li class="rounded-md border border-sc-line bg-sc-panel px-3 py-2.5">
							<div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-sc-ink2">
								<span class={mutationClass(row.mutation_type)}>
									{row.mutation_type}
								</span>
								<span class="text-sc-ink">{row.mutated_by ?? '—'}</span>
								<span class="text-sc-ink3">{formatTimestamp(row.mutated_at)}</span>
							</div>
							<div class="grid grid-cols-1 gap-2 md:grid-cols-2">
								<div class={EXCERPT}>
									<span class={EXCERPT_LABEL}>before</span>
									<pre class={EXCERPT_PRE}>{row.before_excerpt ?? ''}</pre>
								</div>
								<div class={EXCERPT}>
									<span class={EXCERPT_LABEL}>after</span>
									<pre class={EXCERPT_PRE}>{row.after_excerpt ?? ''}</pre>
								</div>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/if}

	{#if confirmMessage}
		<button
			type="button"
			class="fixed inset-0 z-[1001] cursor-pointer border-0 bg-black/70 p-0"
			aria-label="Cancel"
			on:click={() => resolveConfirm(false)}
		></button>
		<div
			class="fixed left-1/2 top-1/2 z-[1002] w-[calc(100%-2rem)] max-w-[420px] -translate-x-1/2 -translate-y-1/2 rounded-md border border-sc-line2 bg-sc-panel p-5 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-label="Confirm"
			tabindex="-1"
		>
			<p class="m-0 mb-4 text-[14px] leading-normal text-sc-ink">{confirmMessage}</p>
			<div class="flex justify-end gap-2">
				<button type="button" class={PRIMARY} on:click={() => resolveConfirm(true)}>
					Confirm
				</button>
				<button type="button" class={BUTTON} on:click={() => resolveConfirm(false)}>Cancel</button>
			</div>
		</div>
	{/if}

	{#if toast}
		<div class="fixed bottom-4 left-4 right-4 z-[1000] rounded-md border border-sc-line2 bg-sc-panel px-4 py-3 text-[13px] text-sc-ink shadow-2xl sm:bottom-6 sm:left-auto sm:right-6 sm:max-w-[380px]" role="status">{toast}</div>
	{/if}
</div>
