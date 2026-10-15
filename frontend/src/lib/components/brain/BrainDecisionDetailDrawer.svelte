<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		getBrainDecision,
		type BrainDecisionDetail,
		type BrainDecisionLinkedTask
	} from '$lib/api/brain';

	export let decisionId: number;
	export let onClose: () => void;

	let detail: BrainDecisionDetail | null = null;
	let loading = true;
	let error = '';
	let decisionExpanded = false;
	let copied = false;
	let copyTimer: ReturnType<typeof setTimeout> | null = null;

	function deepLink(id: number): string {
		// The drawer's open state round-trips through ?tab=decisions&d_id=<id>,
		// so a shareable link just pins those params onto the current URL.
		const url = new URL(window.location.href);
		url.searchParams.set('tab', 'decisions');
		url.searchParams.set('d_id', String(id));
		return url.toString();
	}

	async function copyLink() {
		const link = deepLink(decisionId);
		try {
			await navigator.clipboard.writeText(link);
		} catch {
			// Fallback for environments without async clipboard access.
			const ta = document.createElement('textarea');
			ta.value = link;
			ta.style.position = 'fixed';
			ta.style.opacity = '0';
			document.body.appendChild(ta);
			ta.select();
			try {
				document.execCommand('copy');
			} catch {
				/* best-effort */
			}
			document.body.removeChild(ta);
		}
		copied = true;
		if (copyTimer) clearTimeout(copyTimer);
		copyTimer = setTimeout(() => {
			copied = false;
		}, 2000);
	}

	async function load(id: number) {
		loading = true;
		error = '';
		try {
			detail = await getBrainDecision(id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			detail = null;
		} finally {
			loading = false;
		}
	}

	function formatTimestamp(value: string | null | undefined): string {
		if (!value) return '—';
		const dt = new Date(value);
		return Number.isNaN(dt.getTime()) ? value : dt.toLocaleString();
	}

	function taskLink(task: BrainDecisionLinkedTask): string {
		// The /tasks/[id] route resolves by display_id (LOWER(display_id)); a numeric id 404s.
		return `/tasks/${task.display_id ?? task.id}`;
	}

	function strategyLink(task: BrainDecisionLinkedTask): string | null {
		return task.strategy_id ? `/lab/strategy/${task.strategy_id}` : null;
	}

	const SECTION_TITLE = 'm-0 mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3';
	const BLOCK =
		'm-0 max-h-[360px] overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-sc-line bg-sc-panel p-2.5 font-plex-mono text-[12px] leading-relaxed text-sc-ink';
	const CHIP =
		'rounded border border-sc-line2 bg-sc-raise px-1.5 py-px font-plex-cond text-[10.5px] font-medium uppercase tracking-[0.06em] text-sc-ink2';
	const DT = 'font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3';

	function handleEsc(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	$: void load(decisionId);

	onMount(() => {
		document.addEventListener('keydown', handleEsc);
	});

	onDestroy(() => {
		document.removeEventListener('keydown', handleEsc);
		if (copyTimer) clearTimeout(copyTimer);
	});
</script>

<button type="button" class="fixed inset-0 z-[1000] cursor-pointer border-0 bg-black/70 p-0" on:click={onClose} aria-label="Close drawer"></button>

<div
	class="fixed right-0 top-0 z-[1001] flex h-screen w-full flex-col border-sc-line bg-sc-bg shadow-2xl sm:max-w-[640px] sm:border-l"
	role="dialog"
	aria-modal="true"
	aria-label="Decision detail"
	tabindex="-1"
>
	<header class="flex items-start justify-between gap-4 border-b border-sc-line bg-sc-panel px-5 py-4">
		<div class="min-w-0">
			<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Decision</span>
			<h2 class="m-0 font-plex-mono text-[18px] font-semibold text-sc-ink">#{decisionId}</h2>
		</div>
		<div class="flex shrink-0 items-center gap-1.5">
			<button
				type="button"
				class="whitespace-nowrap rounded-md border border-sc-line2 px-2.5 py-1 text-[12px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
				on:click={copyLink}
				title="Copy a shareable link to this decision"
			>
				{copied ? 'Copied' : 'Copy link'}
			</button>
			<button
				type="button"
				class="rounded-md border border-sc-line2 px-2 py-1 text-[12px] leading-none text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
				on:click={onClose}
				aria-label="Close">×</button
			>
		</div>
	</header>

	<div class="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-4">
		{#if loading}
			<div class="text-[13px] text-sc-ink3">Loading…</div>
		{:else if error}
			<div class="rounded-md border border-[#e5574f]/40 bg-[#e5574f]/10 px-3 py-2 text-[12.5px] text-[#f2956f]" role="alert">
				<strong class="font-medium">Failed to load decision:</strong>
				{error}
			</div>
		{:else if detail}
			<dl class="m-0 grid grid-cols-1 gap-x-4 gap-y-1.5 rounded-md border border-sc-line bg-sc-panel px-3.5 py-3 text-[13px] sm:grid-cols-[120px_minmax(0,1fr)]">
				<dt class={DT}>Cycle</dt>
				<dd class="m-0 break-all font-plex-mono text-[12.5px] text-sc-ink">{detail.cycle_id ?? '—'}</dd>
				<dt class={DT}>Created</dt>
				<dd class="m-0 text-sc-ink">{formatTimestamp(detail.created_at)}</dd>
				<dt class={DT}>Outcome</dt>
				<dd class="m-0 text-sc-ink">{detail.outcome_observed ?? 'pending'}{detail.outcome_at ? ` @ ${formatTimestamp(detail.outcome_at)}` : ''}</dd>
				<dt class={DT}>Prompt hash</dt>
				<dd class="m-0 break-all font-plex-mono text-[12px] text-sc-ink">{detail.prompt_hash ?? '—'}</dd>
			</dl>

			<section>
				<h3 class={SECTION_TITLE}>Situation</h3>
				<pre class={BLOCK}>{detail.situation_summary ?? ''}</pre>
			</section>

			<section>
				<h3 class={SECTION_TITLE}>
					<button
						type="button"
						class="cursor-pointer border-0 bg-transparent p-0 font-[inherit] uppercase tracking-[inherit] text-sc-ink3 transition-colors hover:text-sc-ink"
						on:click={() => (decisionExpanded = !decisionExpanded)}
					>
						Decision JSON {decisionExpanded ? '▾' : '▸'}
					</button>
				</h3>
				{#if decisionExpanded}
					<pre class={BLOCK}>{JSON.stringify(detail.decision, null, 2)}</pre>
				{/if}
			</section>

			{#if detail.action_taken}
				<section>
					<h3 class={SECTION_TITLE}>Action taken</h3>
					<pre class={BLOCK}>{detail.action_taken}</pre>
				</section>
			{/if}

			<section>
				<h3 class={SECTION_TITLE}>Linked tasks ({detail.linked_tasks.length})</h3>
				{#if detail.linked_tasks.length === 0}
					<p class="m-0 text-[12.5px] text-sc-ink3">No agent_tasks rows linked to this decision.</p>
				{:else}
					<ul class="m-0 flex list-none flex-col gap-2 p-0">
						{#each detail.linked_tasks as task (task.id)}
							<li class="rounded-md border border-sc-line bg-sc-panel px-3 py-2">
								<div class="mb-1.5 flex flex-wrap items-center gap-2">
									<a class="font-plex-mono text-[12px] text-sc-ink2 transition-colors hover:text-sc-ink hover:underline" href={taskLink(task)}>#{task.id}</a>
									<span class={CHIP}>{task.type ?? '—'}</span>
									<span class={CHIP}>{task.status ?? '—'}</span>
									{#if task.strategy_id}
										<a class="font-plex-mono text-[12px] text-sc-ink transition-colors hover:underline sm:ml-auto" href={strategyLink(task)}>
											{task.strategy_id}
										</a>
									{/if}
								</div>
								<div class="flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] text-sc-ink2">
									<span class="text-sc-ink">{task.title ?? ''}</span>
									{#if task.cost_usd != null}
										<span class="font-plex-mono">${task.cost_usd.toFixed(4)}</span>
									{/if}
									{#if task.provider}
										<span class="font-plex-mono">{task.provider}:{task.model_id ?? '—'}</span>
									{/if}
									<span class="text-sc-ink3">{formatTimestamp(task.created_at)}</span>
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/if}
	</div>
</div>
