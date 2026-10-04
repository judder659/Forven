<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { getStrategyBreadth, startStrategyBreadth, type BreadthState } from '$lib/api/breadth';
	import { fmtNum, fmtPct, signClass } from '$lib/utils/strategyContainer/format';

	/** The strategy whose frozen rule is run across the coin list. */
	export let strategyId: string;

	let state: BreadthState | null = null;
	let error = '';
	let starting = false;
	let timer: ReturnType<typeof setTimeout> | null = null;

	const VERDICTS: Record<string, { text: string; cls: string; note: string }> = {
		general: {
			text: 'General',
			cls: 'border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]',
			note: 'Most other coins made money with the same rule.',
		},
		home_only: {
			text: 'Home coin only',
			cls: 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#e5574f]',
			note: 'The rule works on its home coin but not on most others — likely fitted to that coin.',
		},
		mixed: { text: 'Mixed', cls: 'border-sc-line2 text-sc-ink2', note: 'Neither clearly general nor home-only.' },
		too_few: { text: 'Too few coins', cls: 'border-sc-line2 text-sc-ink2', note: 'Fewer than 5 other coins produced trades.' },
	};

	$: verdict = state?.summary ? VERDICTS[state.summary.verdict] : null;
	$: rows = [...(state?.rows ?? [])].sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99));
	$: progress = state?.status === 'running' ? `${state.rows?.length ?? 0} of ${state.assets?.length ?? 15} coins` : '';

	onMount(() => void load());
	onDestroy(() => {
		if (timer) clearTimeout(timer);
	});

	async function load() {
		try {
			state = await getStrategyBreadth(strategyId);
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
		schedule();
	}

	function schedule() {
		if (timer) clearTimeout(timer);
		timer = state?.status === 'running' ? setTimeout(() => void load(), 5000) : null;
	}

	async function run(refresh: boolean) {
		starting = true;
		try {
			state = await startStrategyBreadth(strategyId, refresh);
			error = state.busy_with?.length ? `Another breadth test is running (${state.busy_with.join(', ')}); try again when it finishes.` : '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			starting = false;
		}
		schedule();
	}
</script>

<div class="grid gap-3" data-testid="breadth-card">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<div class="text-[11px] text-sc-ink3">
			{#if state?.status === 'running'}
				Running · {progress}
			{:else if state?.status === 'done' && state.summary}
				{verdict?.note}
				{#if state.stale}<span class="text-[#e0b04a]"> The rule or data changed since this run.</span>{/if}
			{:else if state?.status === 'interrupted'}
				The last run was interrupted by a restart.
			{:else if state?.status === 'error'}
				The last run failed: {state.error}
			{:else}
				Not run yet. Takes about one backtest per coin.
			{/if}
		</div>
		<div class="flex items-center gap-2">
			{#if verdict}
				<span class={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[12px] font-semibold ${verdict.cls}`} data-testid="breadth-verdict">{verdict.text}</span>
			{/if}
			<button
				type="button"
				class="rounded-full border border-sc-line2 px-2.5 py-0.5 text-[11px] text-sc-ink2 hover:text-sc-ink disabled:opacity-50"
				disabled={starting || state?.status === 'running'}
				on:click={() => run(state?.status === 'done' || state?.status === 'error' || state?.status === 'interrupted')}
			>
				{state?.status === 'done' ? 'Rerun' : 'Run breadth test'}
			</button>
		</div>
	</div>

	{#if error}
		<div class="text-[11px] text-[#f2956f]">{error}</div>
	{/if}

	{#if state?.summary}
		{@const s = state.summary}
		<div class="flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-sc-ink2">
			<span>Other coins positive <span class="font-plex-mono text-sc-ink">{s.other_positive} of {s.other_traded}</span></span>
			<span>Median Sharpe <span class={`font-plex-mono ${signClass(s.median_sharpe)}`}>{fmtNum(s.median_sharpe)}</span></span>
			<span title="Chance that this many coins come out positive if the rule has no edge. Coins move together, so this is optimistic.">
				Sign test p
				<span class="font-plex-mono text-sc-ink">{s.sign_test_p == null ? '—' : s.sign_test_p < 0.001 ? '<0.1%' : `${(s.sign_test_p * 100).toFixed(1)}%`}</span>
			</span>
			<span>Home ({s.home}) Sharpe rank <span class="font-plex-mono text-sc-ink">{s.home_sharpe_rank ?? '—'} of {s.ranked_coins}</span></span>
		</div>
	{/if}

	{#if rows.length}
		<div class="overflow-x-auto">
			<table class="w-full text-[12px]">
				<thead>
					<tr class="text-left font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">
						<th class="py-1 pr-2 font-medium">Coin</th>
						<th class="py-1 pr-2 text-right font-medium">Trades</th>
						<th class="py-1 pr-2 text-right font-medium">Return</th>
						<th class="py-1 pr-2 text-right font-medium">Sharpe</th>
						<th class="py-1 pr-2 text-right font-medium">Max DD</th>
						<th class="py-1 text-right font-medium">Buy &amp; hold</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.asset)}
						<tr class="border-t border-sc-line">
							<td class="py-1 pr-2 text-sc-ink">
								{row.asset}{#if row.asset === state?.home}<span class="ml-1 text-[10px] text-sc-ink3">home</span>{/if}
							</td>
							{#if row.error}
								<td class="py-1 text-right text-sc-ink3" colspan="5" title={row.error}>untestable — {row.error.slice(0, 80)}</td>
							{:else}
								<td class="py-1 pr-2 text-right font-plex-mono text-sc-ink2">{row.trades ?? 0}</td>
								<td class={`py-1 pr-2 text-right font-plex-mono ${signClass(row.return_pct)}`}>{fmtPct(row.return_pct)}</td>
								<td class={`py-1 pr-2 text-right font-plex-mono ${signClass(row.sharpe)}`}>{fmtNum(row.sharpe)}</td>
								<td class="py-1 pr-2 text-right font-plex-mono text-sc-ink2">{fmtPct(row.max_drawdown_pct, 1, false)}</td>
								<td class={`py-1 text-right font-plex-mono ${signClass(row.buy_hold_return_pct)}`}>{fmtPct(row.buy_hold_return_pct)}</td>
							{/if}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
