<script lang="ts">
	/**
	 * What needs the operator, most severe first. Each item jumps to the strategy and tab.
	 * The +/− button folds the list away; the counts stay in view and the choice is kept per desk.
	 * Dismiss hides one item in this browser for a week, or until it turns more severe;
	 * an item whose cause is gone drops off the list by itself.
	 */
	import { createEventDispatcher, onMount } from 'svelte';
	import type { DeskMode } from '$lib/api/desk';
	import type { AttentionItem, SideTab } from '$lib/utils/tradingDesk/attention';

	export let mode: DeskMode;
	export let items: AttentionItem[] = [];

	const dispatch = createEventDispatcher<{ show: { strategyId: string; tab?: SideTab } }>();

	let collapsed = false;
	const storageKey = (): string => `forven.${mode}.attentionCollapsed`;
	const dismissKey = (): string => `forven.${mode}.attentionDismissed`;
	$: listId = `desk-attention-list-${mode}`;

	const DISMISS_MS = 7 * 86_400_000;
	const RANK: Record<AttentionItem['severity'], number> = { info: 0, caution: 1, fail: 2 };
	type Dismissal = { at: number; severity: AttentionItem['severity'] };
	let dismissed: Record<string, Dismissal> = {};

	onMount(() => {
		try {
			collapsed = window.localStorage.getItem(storageKey()) === '1';
		} catch {
			collapsed = false;
		}
		try {
			const raw = JSON.parse(window.localStorage.getItem(dismissKey()) || '{}');
			dismissed = raw && typeof raw === 'object' ? raw : {};
		} catch {
			dismissed = {};
		}
	});

	function saveDismissed(next: Record<string, Dismissal>): void {
		dismissed = next;
		try {
			if (Object.keys(next).length) window.localStorage.setItem(dismissKey(), JSON.stringify(next));
			else window.localStorage.removeItem(dismissKey());
		} catch {
			/* storage can be blocked */
		}
	}

	function dismiss(item: AttentionItem): void {
		saveDismissed({ ...dismissed, [item.id]: { at: Date.now(), severity: item.severity } });
	}

	function restoreAll(): void {
		saveDismissed({});
	}

	function isDismissed(item: AttentionItem, map: Record<string, Dismissal>): boolean {
		const entry = map[item.id];
		if (!entry || typeof entry.at !== 'number') return false;
		if (Date.now() - entry.at > DISMISS_MS) return false;
		return RANK[item.severity] <= (RANK[entry.severity] ?? 0);
	}

	$: visible = items.filter((item) => !isDismissed(item, dismissed));
	$: hiddenCount = items.length - visible.length;

	function toggle(): void {
		collapsed = !collapsed;
		try {
			if (collapsed) window.localStorage.setItem(storageKey(), '1');
			else window.localStorage.removeItem(storageKey());
		} catch {
			/* storage can be blocked */
		}
	}

	$: counts = visible.reduce(
		(acc, item) => ({ ...acc, [item.severity]: acc[item.severity] + 1 }),
		{ fail: 0, caution: 0, info: 0 } as Record<AttentionItem['severity'], number>
	);

	const BAR: Record<AttentionItem['severity'], string> = { fail: 'bg-[#e5574f]', caution: 'bg-[#e7b24a]', info: 'bg-sc-line2' };
	const TAG: Record<AttentionItem['severity'], string> = { fail: 'text-[#e5574f]', caution: 'text-[#e7b24a]', info: 'text-sc-ink3' };
	const WORD: Record<AttentionItem['severity'], string> = { fail: 'Critical', caution: 'Warning', info: 'Note' };
</script>

<section class="rounded-md border border-sc-line bg-sc-panel" aria-label="Needs attention" data-testid="desk-attention">
	{#if visible.length === 0}
		<div class="flex flex-wrap items-center gap-3 px-3 py-2">
			<h2 class="text-[13px] font-semibold text-sc-ink">Nothing needs you</h2>
			{#if hiddenCount > 0}
				<span class="text-[12px] text-sc-ink3">You dismissed {hiddenCount} item{hiddenCount === 1 ? '' : 's'}.</span>
				<button type="button" class="text-[12px] text-sc-ink3 underline underline-offset-2 hover:text-sc-ink" on:click={restoreAll} data-testid="desk-attention-restore">Show again</button>
			{:else}
				<span class="text-[12px] text-sc-ink3">Every strategy was checked recently and nothing was refused this week.</span>
			{/if}
		</div>
	{:else}
		<div class={`flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 ${collapsed ? '' : 'border-b border-sc-line'}`}>
			<h2 class="text-[13px] font-semibold text-sc-ink">
				<button
					type="button"
					class="group inline-flex items-center gap-2 text-left"
					aria-expanded={!collapsed}
					aria-controls={collapsed ? undefined : listId}
					title={collapsed ? 'Show the list' : 'Hide the list'}
					on:click={toggle}
					data-testid="desk-attention-toggle"
				>
					<span aria-hidden="true" class="grid h-[18px] w-[18px] place-items-center rounded-[3px] border border-sc-line2 font-plex-mono text-[13px] leading-none text-sc-ink2 group-hover:border-sc-ink4 group-hover:text-sc-ink">{collapsed ? '+' : '−'}</span>
					Needs attention
				</button>
			</h2>
			<span class="text-[12px] text-sc-ink3">
				{#if counts.fail}<span class="font-medium text-[#e5574f]">{`${counts.fail} critical`}</span>{' · '}{/if}{counts.caution} warning{counts.caution === 1 ? '' : 's'} · {counts.info} note{counts.info === 1 ? '' : 's'}
			</span>
			{#if hiddenCount > 0}
				<button type="button" class="text-[12px] text-sc-ink3 underline underline-offset-2 hover:text-sc-ink" on:click={restoreAll} data-testid="desk-attention-restore">{hiddenCount} dismissed · show again</button>
			{/if}
		</div>
		{#if !collapsed}
			<ul class="grid lg:grid-cols-2" id={listId}>
				{#each visible as item (item.id)}
					<li class="grid min-w-0 grid-cols-[4px_minmax(0,1fr)_auto_auto] items-start gap-2.5 border-b border-sc-line px-3 py-2 lg:[&:nth-child(odd)]:border-r">
						<span class={`self-stretch rounded-sm ${BAR[item.severity]}`}></span>
						<div class="min-w-0">
							<div class="text-[13px] font-semibold text-sc-ink">
								<span class={`mr-1.5 font-plex-cond text-[10.5px] font-semibold uppercase tracking-[0.08em] ${TAG[item.severity]}`}>{WORD[item.severity]}</span>{item.title}
							</div>
							<div class="text-[12px] text-sc-ink2">{item.body}</div>
						</div>
						{#if item.strategyId}
							<button type="button" class="whitespace-nowrap text-[12px] text-sc-ink3 underline underline-offset-2 hover:text-sc-ink" on:click={() => dispatch('show', { strategyId: item.strategyId ?? '', tab: item.tab })}>Show</button>
						{:else}
							<span></span>
						{/if}
						<button
							type="button"
							class="whitespace-nowrap text-[12px] text-sc-ink3 hover:text-sc-ink"
							title="Hide this for a week, or until it gets worse"
							aria-label={`Dismiss: ${item.title}`}
							on:click={() => dismiss(item)}
							data-testid="desk-attention-dismiss"
						>Dismiss</button>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</section>
