<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		getBotArena,
		listBotArenas,
		listBots,
		startBotArena,
		type BotArena,
		type BotArenaListItem,
		type BotConfig
	} from '$lib/api';

	const DAY_CHOICES = [7, 14, 30, 90, 180];
	const TYPE_LABELS: Record<string, string> = { ai: 'AI', grid: 'Grid', dca: 'DCA', signal: 'Signal' };

	let bots: BotConfig[] = [];
	let picked: Record<string, boolean> = {};
	let days = 30;
	let starting = false;
	let error: string | null = null;
	let shiftedNote: string | null = null;
	let arenas: BotArenaListItem[] = [];
	let arena: BotArena | null = null;
	let poll: ReturnType<typeof setTimeout> | null = null;

	$: chosen = bots.filter((b) => picked[b.id]);

	function fmtPct(v: number | null | undefined): string {
		if (v == null) return '—';
		return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
	}
	function tone(v: number | null | undefined): string {
		if (v == null || v === 0) return 'text-sc-ink';
		return v > 0 ? 'text-emerald-400' : 'text-red-400';
	}
	function day(ts: string | null | undefined): string {
		return ts ? ts.slice(0, 10) : '—';
	}

	async function open(id: string) {
		if (poll) clearTimeout(poll);
		try {
			arena = await getBotArena(id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			return;
		}
		if (arena.running > 0) poll = setTimeout(() => open(id), 2500);
		else arenas = await listBotArenas().catch(() => arenas);
	}

	async function run() {
		error = null;
		shiftedNote = null;
		if (chosen.length < 2) {
			error = 'Pick at least two bots.';
			return;
		}
		starting = true;
		try {
			const started = await startBotArena({ bot_ids: chosen.map((b) => b.id), days });
			if (started.shifted_for_holdout) {
				shiftedNote = `Your research holdout keeps recent candles back, so every bot runs ${day(started.window_start)} to ${day(started.window_end)}.`;
			}
			await open(started.id);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			starting = false;
		}
	}

	onMount(async () => {
		try {
			[bots, arenas] = await Promise.all([listBots(), listBotArenas()]);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
		if (arenas.length) open(arenas[0].id);
	});
	onDestroy(() => {
		if (poll) clearTimeout(poll);
	});
</script>

<svelte:head>
	<title>Bot Arena | Forven</title>
</svelte:head>

<div class="mx-auto max-w-7xl space-y-5 px-4 py-6">
	<div class="border-b border-sc-line pb-4">
		<a href="/bot-factory" class="text-[12px] text-sc-ink3 hover:text-sc-ink">&larr; Bot Factory</a>
		<h1 class="mt-1 text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Arena</h1>
		<p class="mt-1 text-xs text-sc-ink3">
			Replays the bots you pick over the same dates, each on its own coins, and ranks them by return. Nothing is traded. Replays stop where your research holdout begins.
		</p>
	</div>

	<div class="rounded-md border border-sc-line bg-sc-panel p-4">
		<div class="mb-2 font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Bots</div>
		{#if bots.length === 0}
			<div class="text-[12px] text-sc-ink3">No bots yet.</div>
		{:else}
			<div class="grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
				{#each bots as b}
					<label class="flex items-center gap-2 rounded px-2 py-1 text-[12px] text-sc-ink2 hover:bg-sc-raise/50">
						<input type="checkbox" bind:checked={picked[b.id]} />
						<span class="text-sc-ink">{b.name}</span>
						<span class="text-sc-ink3">{TYPE_LABELS[b.bot_type || 'ai'] ?? b.bot_type} · {b.timeframe}</span>
					</label>
				{/each}
			</div>
		{/if}
		<div class="mt-3 flex flex-wrap items-end gap-3">
			<label class="text-[12px] text-sc-ink2">
				<span class="mb-1 block font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Window</span>
				<select bind:value={days} class="terminal-input w-auto text-[12px]">
					{#each DAY_CHOICES as d}<option value={d}>Last {d} days</option>{/each}
				</select>
			</label>
			<button
				class="rounded bg-sc-ink px-3 py-1.5 text-[12px] font-semibold text-sc-panel disabled:opacity-50"
				on:click={run}
				disabled={starting || chosen.length < 2}
			>
				{starting ? 'Starting…' : `Run arena (${chosen.length} bot${chosen.length === 1 ? "" : "s"})`}
			</button>
		</div>
		{#if error}<p class="mt-2 text-[12px] text-red-400">{error}</p>{/if}
		{#if shiftedNote}<p class="mt-2 text-[12px] text-amber-400">{shiftedNote}</p>{/if}
	</div>

	{#if arena}
		<div class="rounded-md border border-sc-line bg-sc-panel">
			<div class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-4 py-2 text-[12px] text-sc-ink3">
				<span>{day(arena.window_start)} to {day(arena.window_end)}</span>
				{#if arena.running}<span>{arena.running} still replaying…</span>{/if}
			</div>
			<div class="overflow-x-auto">
				<table class="w-full min-w-[760px] text-left text-[12px]">
					<thead class="font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">
						<tr>
							<th class="px-4 py-2">#</th><th>Bot</th><th>Return</th><th>vs BTC hold</th><th>BTC hold</th>
							<th>Max drawdown</th><th>Trades</th><th>Coins</th>
						</tr>
					</thead>
					<tbody>
						{#each arena.rows as r}
							<tr class="border-t border-sc-line align-top">
								<td class="px-4 py-2 text-sc-ink3">{r.rank ?? ''}</td>
								<td class="py-2">
									{#if r.bot_id}<a class="text-sc-ink hover:underline" href="/bot-factory/{r.bot_id}">{r.name}</a>{:else}{r.name}{/if}
									{#if r.status === 'failed'}<div class="text-[11px] text-red-400">{r.error}</div>{/if}
									{#if r.ended_early}<div class="text-[11px] text-amber-400">{r.ended_early}</div>{/if}
								</td>
								{#if r.status === 'running'}
									<td colspan="6" class="text-sc-ink3">Replaying…</td>
								{:else}
									<td class="font-bold {tone(r.return_pct)}">{fmtPct(r.return_pct)}</td>
									<td class={tone(r.excess_return_pct)}>{fmtPct(r.excess_return_pct)}</td>
									<td class="text-sc-ink3">{fmtPct(r.benchmark_return_pct)}</td>
									<td>{r.max_drawdown_pct != null ? `${r.max_drawdown_pct.toFixed(2)}%` : '—'}</td>
									<td>{r.closed_trades ?? '—'}{r.win_rate != null ? ` · ${(r.win_rate * 100).toFixed(0)}% won` : ''}</td>
									<td class="text-sc-ink3">{(r.pairs ?? []).map((p) => p.split('/')[0]).join(', ')}</td>
								{/if}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}

	{#if arenas.length > 1}
		<div>
			<div class="mb-1 font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">Earlier arenas</div>
			<div class="divide-y divide-sc-line rounded-md border border-sc-line">
				{#each arenas as a}
					<button class="flex w-full justify-between px-3 py-2 text-left text-[12px] hover:bg-sc-raise/50" on:click={() => open(a.id)}>
						<span class="text-sc-ink2">{day(a.window_start)} to {day(a.window_end)}</span>
						<span class="text-sc-ink3">{a.bots} bots · run {day(a.created_at)}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>
