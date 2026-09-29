<script lang="ts">
	/** The decision journal: fills, closes, refused signals (collapsed) and regime flags, newest first. */
	import type { DeskMode, JournalEvent, JournalKind } from '$lib/api/desk';
	import { cap1, fmtDateTime, fmtPct, fmtPx, fmtQty, fmtTime, fmtUsd, toneClass } from '$lib/utils/tradingDesk/format';
	import { describeClose, describeRefusal, humanRegime, slippageWords } from '$lib/utils/tradingDesk/describe';

	export let mode: DeskMode;
	export let events: JournalEvent[] = [];
	export let windowDays = 30;

	type Filter = 'all' | 'fills' | 'closes' | 'refused' | 'regime';
	let filter: Filter = 'all';

	const KIND_GROUP: Record<JournalKind, Filter> = {
		opened: 'fills',
		failed: 'fills',
		closed: 'closes',
		entry_refused: 'refused',
		exit_refused: 'refused',
		regime_flag: 'regime',
	};
	$: counts = events.reduce((acc, event) => ({ ...acc, [KIND_GROUP[event.kind]]: (acc[KIND_GROUP[event.kind]] ?? 0) + 1 }), {} as Partial<Record<Filter, number>>);
	$: shown = events.filter((event) => filter === 'all' || KIND_GROUP[event.kind] === filter).slice(0, 200);
	$: filters = [['all', 'All', events.length], ['fills', 'Fills', counts.fills ?? 0], ['closes', 'Closes', counts.closes ?? 0], ['refused', 'Refused', counts.refused ?? 0], ['regime', 'Regime', counts.regime ?? 0]] as Array<[Filter, string, number]>;

	function label(event: JournalEvent): { text: string; tone: string } {
		switch (event.kind) {
			case 'opened': return { text: 'Opened', tone: 'text-[#8fb0ff]' };
			case 'closed': return { text: 'Closed', tone: 'text-sc-ink2' };
			case 'failed': return { text: 'Order failed', tone: 'text-[#e5574f]' };
			case 'entry_refused': return { text: `Entry refused${(event.count ?? 1) > 1 ? ` ×${event.count}` : ''}`, tone: 'text-[#e7b24a]' };
			case 'exit_refused': return { text: `Exit refused${(event.count ?? 1) > 1 ? ` ×${event.count}` : ''}`, tone: event.positioned ? 'text-[#e5574f]' : 'text-[#e7b24a]' };
			default: return { text: 'Regime flag', tone: 'text-sc-ink3' };
		}
	}

	function when(event: JournalEvent): string {
		if (event.first_at && event.first_at !== event.at) return `${fmtDateTime(event.first_at)} → ${fmtTime(event.at)}`;
		return fmtDateTime(event.at);
	}

	const who = (sid: string) => (sid.startsWith('bot:') ? 'bot' : sid);
</script>

<div class="flex flex-wrap gap-1.5 border-b border-sc-line px-3 py-2" role="group" aria-label="Filter decisions">
	{#each filters as [key, text, count]}
		<button type="button" class={`rounded-full border px-2.5 py-px text-[12px] ${filter === key ? 'border-sc-ink4 bg-sc-raise text-sc-ink' : 'border-sc-line2 text-sc-ink3 hover:text-sc-ink2'}`} aria-pressed={filter === key} on:click={() => (filter = key)}>
			{text}<span class="ml-1 font-plex-mono text-[11px] text-sc-ink4">{count}</span>
		</button>
	{/each}
	<span class="ml-auto self-center text-[11.5px] text-sc-ink3">Last {windowDays} days · repeats of one refusal within 6 hours are collapsed</span>
</div>
{#if shown.length === 0}
	<div class="px-4 py-7 text-center text-[12.5px] text-sc-ink3">No {mode} decisions of this kind in the window.</div>
{:else}
	<ol class="grid max-h-[460px] overflow-y-auto px-3" data-testid="desk-journal">
		{#each shown as event, index (`${event.kind}-${event.trade_id ?? ''}-${event.strategy_id}-${event.at}-${index}`)}
			{@const tag = label(event)}
			<li class="grid gap-x-3 gap-y-0.5 border-b border-sc-line py-2 sm:grid-cols-[170px_72px_minmax(0,1fr)]">
				<time class="font-plex-mono text-[11.5px] text-sc-ink3">{when(event)}</time>
				<span class="font-plex-mono text-[12px] text-sc-ink2">{who(event.strategy_id)}</span>
				<div class="grid min-w-0 gap-0.5">
					<span class={`font-plex-cond text-[10.5px] font-semibold uppercase tracking-[0.08em] ${tag.tone}`}>{tag.text}</span>
					<span class="text-[12.5px] text-sc-ink [overflow-wrap:anywhere]">
						{#if event.kind === 'opened'}
							{cap1(event.direction)} <span class="font-plex-mono">{fmtQty(event.size)} {event.asset}</span> at <span class="font-plex-mono">{fmtPx(event.price)}</span>{#if event.signal_price !== null && event.signal_price !== undefined}, {slippageWords(event.slippage_bps)}{/if}{#if event.stop_price} · stop <span class="font-plex-mono">{fmtPx(event.stop_price)}</span>{/if}{#if event.risk_usd} · risking <span class="font-plex-mono">{fmtUsd(event.risk_usd)}</span>{/if}{#if event.book} · {event.book} wallet{/if}{#if event.source === 'manual'} · opened by hand{/if}
						{:else if event.kind === 'closed'}
							{@const why = describeClose({ status: 'CLOSED', close_reason: event.close_reason, exit_price: event.price, stop_price: event.stop_price })}
							{cap1(event.direction)} closed at <span class="font-plex-mono">{fmtPx(event.price)}</span> · <span class={why.tone === 'stop' ? 'text-[#f2956f]' : ''}>{why.text}</span> · net <span class={`font-plex-mono ${toneClass(event.net_pnl_usd)}`}>{fmtUsd(event.net_pnl_usd, { signed: true })}</span>
							{#if why.inferred || (event.close_reason && why.text !== 'Strategy exit')}
								<details class="text-[11.5px] text-sc-ink3"><summary class="w-fit cursor-pointer">Recorded reason</summary><p class="mt-1 font-plex-mono text-[11px]">{why.inferred ?? `close_reason: ${event.close_reason}`}</p></details>
							{/if}
						{:else if event.kind === 'failed'}
							{cap1(event.direction)} {event.asset} entry order failed{event.failure_reason ? `: ${event.failure_reason}` : ''}
						{:else if event.kind === 'entry_refused' || event.kind === 'exit_refused'}
							{@const refusal = describeRefusal(event.reason)}
							{refusal.short}{#if event.kind === 'exit_refused'} · {#if event.positioned}<b class="font-medium text-[#e5574f]">position open</b>{:else}while flat{/if}{/if}
							{#if refusal.raw && refusal.raw !== refusal.short}
								<details class="text-[11.5px] text-sc-ink3"><summary class="w-fit cursor-pointer">Recorded reason</summary><p class="mt-1 font-plex-mono text-[11px]">{refusal.raw}</p></details>
							{/if}
						{:else}
							Would have blocked a {event.direction} in {humanRegime(event.regime)} ({event.gate_mode} mode{event.gate_mode === 'observe' ? ', so it went ahead' : ''}).{#if event.mtm_pct !== null && event.mtm_pct !== undefined} 48 h later the entry was <span class={`font-plex-mono ${toneClass(event.mtm_pct)}`}>{fmtPct(event.mtm_pct)}</span>.{/if}
						{/if}
					</span>
				</div>
			</li>
		{/each}
	</ol>
{/if}
