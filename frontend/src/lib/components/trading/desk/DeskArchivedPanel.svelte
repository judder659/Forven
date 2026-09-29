<script lang="ts">
	/** An archived paper strategy: why it left paper and its lifecycle timeline. */
	import type { LifecycleEvent, LifecycleStrategy } from '$lib/api';
	import { fmtDateTime } from '$lib/utils/tradingDesk/format';

	export let strategy: LifecycleStrategy;
	export let events: LifecycleEvent[] = [];
	export let loading = false;

	const PAPER = new Set(['paper', 'paper_trading', 'paper_challenger']);
	const ARCHIVED = new Set(['retired', 'archived', 'rejected', 'trash', 'killed']);
	const state = (value: string | null | undefined) => String(value ?? '').trim().toLowerCase();
	const pretty = (value: string | null | undefined) => state(value).replace(/_/g, ' ') || '—';

	$: demotion = (() => {
		for (let i = events.length - 1; i >= 0; i -= 1) {
			const event = events[i];
			if (PAPER.has(state(event.from_state)) && state(event.to_state) !== state(event.from_state)) return event;
		}
		for (let i = events.length - 1; i >= 0; i -= 1) {
			if (ARCHIVED.has(state(events[i].to_state))) return events[i];
		}
		return events.length ? events[events.length - 1] : null;
	})();
	$: reason = String(demotion?.reason || strategy.blocked_reason || 'No reason was recorded.').replace(/\s+/g, ' ').trim();
	$: leftPaper = demotion ? PAPER.has(state(demotion.from_state)) : false;
	$: timeline = [...events].reverse();
</script>

<div class="grid gap-3" data-testid="desk-archived">
	<div class="grid gap-1">
		<h3 class="text-[13px] font-semibold text-sc-ink">{strategy.display_id || strategy.name || strategy.id}</h3>
		<span class="text-[12px] text-sc-ink3">{strategy.symbol || '—'} · {pretty(strategy.state)} · updated {fmtDateTime(strategy.updated_at)}</span>
	</div>
	{#if loading}
		<p class="text-[12px] text-sc-ink3">Loading its history…</p>
	{:else}
		<div class="grid gap-1 rounded-md border border-sc-line bg-sc-panel2 p-3">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{leftPaper ? 'Why it left paper' : 'Why it was archived'}</span>
			<p class="text-[13px] leading-relaxed text-sc-ink">{reason}</p>
			{#if demotion}
				<span class="text-[12px] text-sc-ink3">{pretty(demotion.from_state)} → {pretty(demotion.to_state)} · {demotion.actor || 'system'} · {fmtDateTime(demotion.created_at)}</span>
			{/if}
		</div>
		<div class="grid gap-1">
			<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Lifecycle</span>
			{#if timeline.length === 0}
				<p class="text-[12px] text-sc-ink3">No lifecycle events recorded.</p>
			{:else}
				<ol class="grid">
					{#each timeline as event (event.id)}
						<li class="grid gap-0.5 border-b border-sc-line py-1.5 last:border-b-0">
							<div class="flex justify-between gap-2 text-[12px]"><span class="text-sc-ink">{pretty(event.from_state)} → {pretty(event.to_state)}</span><time class="font-plex-mono text-[11px] text-sc-ink3">{fmtDateTime(event.created_at)}</time></div>
							<span class="text-[11.5px] text-sc-ink3">{event.actor || 'system'}</span>
							{#if event.reason}<span class="text-[12px] text-sc-ink2">{String(event.reason).replace(/\s+/g, ' ').slice(0, 420)}</span>{/if}
						</li>
					{/each}
				</ol>
			{/if}
		</div>
		<a class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-center text-[12.5px] font-medium text-sc-ink no-underline hover:border-sc-ink4" href={`/lab/strategy/${encodeURIComponent(strategy.id)}`}>Open in the Forge</a>
	{/if}
</div>
