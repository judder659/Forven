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
			? 'bar-red'
			: pct >= 60
				? 'bar-amber'
				: 'bar-green';

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

<div class="memory-tab">
	{#if loading}
		<div class="loading">Loading memory…</div>
	{:else if error}
		<div class="error-banner">
			<strong>Failed to load memory:</strong>
			{error}
			<button class="link-btn" type="button" on:click={() => refresh(false)}>retry</button>
		</div>
	{:else if state}
		<div class="meta">
			<span>Updated by <strong>{state.updated_by ?? '—'}</strong></span>
			<span>at {formatTimestamp(state.updated_at)}</span>
			<span>{state.char_count} / {state.cap} chars</span>
		</div>

		{#if editing}
			<div class="editor">
				<textarea
					bind:value={draft}
					rows="14"
					placeholder="Brain operational notes…"
					disabled={saving}
				></textarea>
				<div class="bar-row">
					<div class="bar-track">
						<div class="bar-fill {barClass}" style="width: {Math.min(100, pct)}%"></div>
					</div>
					<span class="counter" class:over={overCap}>
						{charCount} / {cap}
					</span>
				</div>
				{#if saveError}
					<div class="error-banner small">{saveError}</div>
				{/if}
				<div class="actions">
					<button
						type="button"
						class="primary"
						on:click={save}
						disabled={saving || overCap}
					>
						{saving ? 'Saving…' : 'Save'}
					</button>
					<button type="button" on:click={cancelEdit} disabled={saving}>Cancel</button>
				</div>
			</div>
		{:else}
			<div class="viewer">
				{#if state.body}
					<pre>{state.body}</pre>
				{:else}
					<p class="empty">Brain memory is empty.</p>
				{/if}
				<div class="actions">
					<button type="button" class="primary" on:click={startEdit}>Edit</button>
					<button type="button" on:click={() => refresh(false)}>Refresh</button>
				</div>
			</div>
		{/if}

		<section class="history">
			<h2>Recent changes</h2>
			{#if history.length === 0}
				<p class="empty">No mutations recorded yet.</p>
			{:else}
				<ul>
					{#each history as row (row.id)}
						<li>
							<div class="history-head">
								<span class="badge mutation-{row.mutation_type}">
									{row.mutation_type}
								</span>
								<span class="who">{row.mutated_by ?? '—'}</span>
								<span class="when">{formatTimestamp(row.mutated_at)}</span>
							</div>
							<div class="diff">
								<div class="excerpt before">
									<span class="label">before</span>
									<pre>{row.before_excerpt ?? ''}</pre>
								</div>
								<div class="excerpt after">
									<span class="label">after</span>
									<pre>{row.after_excerpt ?? ''}</pre>
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
			class="confirm-overlay"
			aria-label="Cancel"
			on:click={() => resolveConfirm(false)}
		></button>
		<div
			class="confirm-dialog"
			role="dialog"
			aria-modal="true"
			aria-label="Confirm"
			tabindex="-1"
		>
			<p>{confirmMessage}</p>
			<div class="confirm-actions">
				<button type="button" class="primary" on:click={() => resolveConfirm(true)}>
					Confirm
				</button>
				<button type="button" on:click={() => resolveConfirm(false)}>Cancel</button>
			</div>
		</div>
	{/if}

	{#if toast}
		<div class="toast">{toast}</div>
	{/if}
</div>

<style>
	.memory-tab {
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
	}

	.loading,
	.empty {
		color: #888;
		font-size: 0.875rem;
	}

	.error-banner {
		background: #2a1010;
		border: 1px solid #5a2020;
		color: #f8c0c0;
		padding: 0.625rem 0.875rem;
		border-radius: 0;
		font-size: 0.875rem;
	}

	.error-banner.small {
		padding: 0.5rem 0.75rem;
	}

	.link-btn {
		background: transparent;
		color: #f8c0c0;
		border: none;
		text-decoration: underline;
		cursor: pointer;
		margin-left: 0.5rem;
		padding: 0;
	}

	.meta {
		display: flex;
		gap: 1.5rem;
		flex-wrap: wrap;
		color: #888;
		font-size: 0.8125rem;
	}

	.meta strong {
		color: #ddd;
	}

	textarea {
		width: 100%;
		background: #050505;
		border: 1px solid #333;
		border-radius: 0;
		color: #e5e5e5;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		font-size: 0.875rem;
		padding: 0.75rem;
		resize: vertical;
	}

	textarea:focus {
		outline: none;
		border-color: #fff;
	}

	.bar-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.bar-track {
		flex: 1;
		height: 6px;
		background: #1a1a1a;
		border-radius: 0;
		overflow: hidden;
	}

	.bar-fill {
		height: 100%;
		transition: width 120ms ease;
	}

	.bar-green {
		background: #4ade80;
	}

	.bar-amber {
		background: #f59e0b;
	}

	.bar-red {
		background: #ef4444;
	}

	.counter {
		font-size: 0.8125rem;
		color: #888;
		min-width: 80px;
		text-align: right;
	}

	.counter.over {
		color: #ef4444;
		font-weight: 600;
	}

	.actions {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.5rem;
	}

	.actions button {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ddd;
		padding: 0.5rem 1rem;
		border-radius: 0;
		cursor: pointer;
		font-size: 0.875rem;
	}

	.actions button:hover:not(:disabled) {
		background: #222;
	}

	.actions button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.actions .primary {
		background: #fff;
		border-color: #fff;
		color: #000;
	}

	.actions .primary:hover:not(:disabled) {
		background: #ddd;
	}

	.viewer pre {
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.875rem;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		font-size: 0.875rem;
		white-space: pre-wrap;
		word-wrap: break-word;
		color: #e5e5e5;
	}

	.history h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 0.5rem;
		color: #ddd;
	}

	.history ul {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.history li {
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 0.625rem 0.75rem;
	}

	.history-head {
		display: flex;
		gap: 0.75rem;
		align-items: center;
		font-size: 0.8125rem;
		color: #888;
		margin-bottom: 0.5rem;
	}

	.badge {
		padding: 0.125rem 0.5rem;
		border-radius: 0;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
	}

	.mutation-replace {
		background: #1a1a1a;
		color: #888;
	}

	.mutation-add {
		background: #14532d;
		color: #86efac;
	}

	.mutation-remove {
		background: #5b1e1e;
		color: #fca5a5;
	}

	.who {
		color: #ccc;
	}

	.diff {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.5rem;
	}

	.excerpt {
		background: #050505;
		border: 1px solid #1a1a1a;
		border-radius: 0;
		padding: 0.375rem;
	}

	.excerpt .label {
		display: block;
		font-size: 0.6875rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #666;
		margin-bottom: 0.25rem;
	}

	.excerpt pre {
		margin: 0;
		font-family: 'JetBrains Mono', 'Consolas', monospace;
		font-size: 0.75rem;
		color: #ccc;
		white-space: pre-wrap;
		word-wrap: break-word;
	}

	.confirm-overlay {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.8);
		border: none;
		padding: 0;
		cursor: pointer;
		z-index: 1001;
	}

	.confirm-dialog {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		background: #050505;
		border: 1px solid #222;
		border-radius: 0;
		padding: 1.25rem;
		max-width: 420px;
		width: calc(100% - 2rem);
		box-shadow: none;
		z-index: 1002;
	}

	.confirm-dialog p {
		margin: 0 0 1rem;
		color: #e5e5e5;
		font-size: 0.9375rem;
		line-height: 1.5;
	}

	.confirm-actions {
		display: flex;
		gap: 0.5rem;
		justify-content: flex-end;
	}

	.confirm-actions button {
		background: #1a1a1a;
		border: 1px solid #333;
		color: #ddd;
		padding: 0.5rem 1rem;
		border-radius: 0;
		cursor: pointer;
		font-size: 0.875rem;
	}

	.confirm-actions button:hover {
		background: #222;
	}

	.confirm-actions .primary {
		background: #fff;
		border-color: #fff;
		color: #000;
	}

	.confirm-actions .primary:hover {
		background: #ddd;
	}

	.toast {
		position: fixed;
		bottom: 1.5rem;
		right: 1.5rem;
		background: #050505;
		border: 1px solid #222;
		color: #e5e5e5;
		padding: 0.75rem 1rem;
		border-radius: 0;
		font-size: 0.875rem;
		box-shadow: none;
		max-width: 380px;
		z-index: 1000;
	}
</style>
