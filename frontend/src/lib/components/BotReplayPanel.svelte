<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getBotReplay,
		listBotReplays,
		startBotReplay,
		type BotConfig,
		type BotReplay
	} from '$lib/api';
	import EquityChart from '$lib/components/EquityChart.svelte';

	export let bot: BotConfig;

	const DAY_CHOICES = [7, 14, 30, 90, 180, 365];
	const INPUT_LABELS: Record<string, string> = {
		funding: 'funding',
		open_interest: 'open interest',
		strategy_signals: 'strategy signals'
	};

	$: usesAi = !bot.bot_type || bot.bot_type === 'ai';
	// AI replays cost a call per candle, so they start shorter.
	let days = !bot.bot_type || bot.bot_type === 'ai' ? 14 : 90;
	let budget = 2;
	let budgetApproved = false;
	let starting = false;
	let startError: string | null = null;
	let shiftedNote: string | null = null;
	let history: BotReplay[] = [];
	let current: BotReplay | null = null;
	let poll: ReturnType<typeof setTimeout> | null = null;


	function fmtPct(v: number | null | undefined): string {
		if (v == null) return '—';
		return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
	}
	function fmtUsd(v: number | null | undefined): string {
		if (v == null) return '—';
		return `${v < 0 ? '-' : ''}$${Math.abs(v).toFixed(2)}`;
	}
	function tone(v: number | null | undefined): string {
		if (v == null || v === 0) return 'text-sc-ink';
		return v > 0 ? 'text-emerald-400' : 'text-red-400';
	}
	function day(ts: string | null | undefined): string {
		return ts ? ts.slice(0, 10) : '—';
	}

	async function loadHistory() {
		try {
			history = await listBotReplays(bot.id, 10);
		} catch {
			history = [];
		}
	}

	async function open(id: string) {
		if (poll) clearTimeout(poll);
		try {
			current = await getBotReplay(id);
		} catch (e) {
			startError = e instanceof Error ? e.message : String(e);
			return;
		}
		if (current.status === 'running') {
			poll = setTimeout(() => open(id), 2000);
		} else {
			loadHistory();
		}
	}

	async function run() {
		startError = null;
		shiftedNote = null;
		if (usesAi && !budgetApproved) {
			startError = 'Tick the box to approve the AI budget for this replay.';
			return;
		}
		starting = true;
		try {
			const started = await startBotReplay(bot.id, {
				days,
				max_ai_usd: usesAi ? budget : null
			});
			if (started.shifted_for_holdout) {
				shiftedNote = `Your research holdout keeps recent candles back, so this replay runs ${day(started.window_start)} to ${day(started.window_end)}.`;
			}
			budgetApproved = false;
			await open(started.id);
		} catch (e) {
			startError = e instanceof Error ? e.message : String(e);
		} finally {
			starting = false;
		}
	}

	onMount(loadHistory);
	onDestroy(() => {
		if (poll) clearTimeout(poll);
	});
</script>

<div class="space-y-4 p-4">
	<div class="rounded-md border border-sc-line bg-sc-raise/40 p-4">
		<div class="mb-1 text-sm font-semibold text-sc-ink">Replay this bot on past candles</div>
		<p class="mb-3 text-[12px] text-sc-ink3">
			The bot decides at each candle close and fills at the next open, with fees and slippage. Nothing is
			traded. Replays stop where your research holdout begins, so they never see held-back data.
			{#if usesAi}Each candle is one AI call, so a replay costs money; answers are reused when you replay
				the same settings again.{:else}This bot decides in code, so a replay is free.{/if}
		</p>
		<div class="flex flex-wrap items-end gap-3">
			<label class="text-[12px] text-sc-ink2">
				<span class="mb-1 block font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Window</span>
				<select bind:value={days} class="terminal-input w-auto text-[12px]">
					{#each DAY_CHOICES as d}
						<option value={d}>Last {d} days</option>
					{/each}
				</select>
			</label>
			{#if usesAi}
				<label class="text-[12px] text-sc-ink2">
					<span class="mb-1 block font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">AI budget ($)</span>
					<input type="number" min="0.1" max="50" step="0.5" bind:value={budget} class="terminal-input w-24 text-[12px]" />
				</label>
				<label class="flex items-center gap-2 pb-1 text-[12px] text-sc-ink2">
					<input type="checkbox" bind:checked={budgetApproved} />
					I approve spending up to ${Number(budget || 0).toFixed(2)} on this replay
				</label>
			{/if}
			<button
				class="rounded bg-sc-ink px-3 py-1.5 text-[12px] font-semibold text-sc-panel disabled:opacity-50"
				on:click={run}
				disabled={starting || current?.status === 'running' || (usesAi && (!budgetApproved || !(budget > 0)))}
			>
				{starting ? 'Starting…' : current?.status === 'running' ? 'Replaying…' : 'Run replay'}
			</button>
		</div>
		{#if startError}<p class="mt-2 text-[12px] text-red-400">{startError}</p>{/if}
		{#if shiftedNote}<p class="mt-2 text-[12px] text-amber-400">{shiftedNote}</p>{/if}
	</div>

	{#if current}
		{#if current.status === 'running'}
			<div class="py-6 text-center text-sm text-sc-ink3">
				Replaying {day(current.window_start)} to {day(current.window_end)}…
			</div>
		{:else if current.status === 'failed'}
			<div class="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-[12px] text-red-300">
				The replay could not run: {current.error}
			</div>
		{:else if current.summary}
			{@const s = current.summary}
			{@const coin = s.benchmark_symbol.split('/')[0]}
			<div class="text-[12px] text-sc-ink3">
				{day(current.window_start)} to {day(current.window_end)} · {s.timeframe ?? bot.timeframe} candles ·
				{(s.pairs ?? []).join(', ')} · fees {s.fee_bps} bps, slippage {s.slippage_bps} bps a side
			</div>
			{#if s.ended_early}
				<div class="rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-[12px] text-amber-300">{s.ended_early}</div>
			{/if}
			{#if s.missing_pairs?.length}
				<div class="text-[12px] text-amber-400">No stored candles for {s.missing_pairs.join(', ')}, so they were left out.</div>
			{/if}
			{#if s.inputs_not_replayed?.length}
				<div class="text-[12px] text-sc-ink3">
					Not available in a replay: {s.inputs_not_replayed.map((i) => INPUT_LABELS[i] ?? i).join(', ')}.
				</div>
			{/if}
			{#if s.note}<div class="text-[12px] text-sc-ink3">{s.note}</div>{/if}
			<div class="grid grid-cols-2 gap-px rounded-md border border-sc-line bg-sc-raise sm:grid-cols-4 lg:grid-cols-7">
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Return</div>
					<div class="text-base font-bold {tone(s.return_pct)}">{fmtPct(s.return_pct)}</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">vs {coin} hold</div>
					<div class="text-base font-bold {tone(s.excess_return_pct)}">{fmtPct(s.excess_return_pct)}</div>
					<div class="text-[11px] text-sc-ink3">{coin} {fmtPct(s.benchmark_return_pct)}</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Max drawdown</div>
					<div class="text-base font-bold text-sc-ink">{s.max_drawdown_pct.toFixed(2)}%</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Trades</div>
					<div class="text-base font-bold text-sc-ink">{s.closed_trades}</div>
					<div class="text-[11px] text-sc-ink3">{s.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}% won` : 'none'}</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Profit factor</div>
					<div class="text-base font-bold text-sc-ink">{s.profit_factor != null ? s.profit_factor.toFixed(2) : '—'}</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">AI cost</div>
					<div class="text-base font-bold text-sc-ink">{fmtUsd(s.ai_cost_usd)}</div>
					<div class="text-[11px] text-sc-ink3">{s.ai_calls} calls{s.ai_cached ? `, ${s.ai_cached} reused` : ''}</div>
				</div>
				<div class="bg-sc-panel px-3 py-2">
					<div class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">P&L after AI</div>
					<div class="text-base font-bold {tone(s.pnl_after_ai_usd)}">{fmtUsd(s.pnl_after_ai_usd)}</div>
				</div>
			</div>
			{#if (current.curve?.length ?? 0) >= 2}
				<EquityChart data={current.curve ?? []} benchmarkData={current.benchmark ?? null} benchmarkTitle="{coin} hold" height={240} />
			{/if}
			{#if current.trades?.length}
				<div class="overflow-x-auto">
					<table class="w-full text-[12px]">
						<thead class="text-left font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">
							<tr><th class="py-1 pr-3">Opened</th><th class="pr-3">Pair</th><th class="pr-3">Side</th><th class="pr-3">Entry</th><th class="pr-3">Exit</th><th class="pr-3">Why it closed</th><th class="text-right">P&L</th></tr>
						</thead>
						<tbody>
							{#each current.trades.slice(-50).reverse() as t}
								<tr class="border-t border-sc-line">
									<td class="py-1 pr-3 text-sc-ink3">{t.opened_at.slice(0, 16).replace('T', ' ')}</td>
									<td class="pr-3">{t.ticker}</td>
									<td class="pr-3">{t.direction}</td>
									<td class="pr-3">{t.entry_price.toPrecision(6)}</td>
									<td class="pr-3">{t.exit_price.toPrecision(6)}</td>
									<td class="pr-3 text-sc-ink3">{t.exit_reason}</td>
									<td class="text-right {tone(t.pnl_usd)}">{fmtUsd(t.pnl_usd)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
					{#if current.trades.length > 50}<div class="mt-1 text-[11px] text-sc-ink3">Showing the last 50 of {current.trades.length} trades.</div>{/if}
				</div>
			{:else}
				<div class="text-[12px] text-sc-ink3">The bot made no trades in this window.</div>
			{/if}
		{/if}
	{/if}

	{#if history.length}
		<div>
			<div class="mb-1 font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Earlier replays</div>
			<div class="divide-y divide-sc-line rounded-md border border-sc-line">
				{#each history as h}
					<button class="flex w-full items-center justify-between px-3 py-2 text-left text-[12px] hover:bg-sc-raise/50" on:click={() => open(h.id)}>
						<span class="text-sc-ink2">{day(h.window_start)} to {day(h.window_end)}</span>
						<span class={h.status === 'done' ? tone(h.summary?.return_pct) : 'text-sc-ink3'}>
							{h.status === 'done' ? `${fmtPct(h.summary?.return_pct)} (${h.summary?.closed_trades ?? 0} trades)` : h.status}
						</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>
