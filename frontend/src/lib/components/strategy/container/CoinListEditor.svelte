<script lang="ts">
	// Edit the coins a strategy paper-trades (backend forven.multi_coin). The home
	// coin always trades; the breadth test's coins are suggested with their results.
	import { createEventDispatcher, onMount } from 'svelte';
	import { getStrategyBreadth, type BreadthCoin } from '$lib/api/breadth';
	import { setStrategyCoins, type CoinList } from '$lib/api/multiCoin';
	import { fmtNum, fmtPct, signClass } from '$lib/utils/strategyContainer/format';

	export let strategyId: string;
	export let list: CoinList;
	/** Coins with an open paper position; they can't be removed until flat. */
	export let openCoins: string[] = [];

	const dispatch = createEventDispatcher<{ saved: CoinList; cancel: void }>();

	let draft: string[] = [...list.coins];
	let input = '';
	let saving = false;
	let error = '';
	let breadth: BreadthCoin[] = [];

	$: extras = draft.slice(1);
	$: changed = draft.join(',') !== list.coins.join(',');
	$: full = draft.length >= list.max_coins;
	$: tested = new Set(list.tested_coins);
	$: suggestions = breadth
		.filter((row) => !row.error && row.asset && !draft.includes(row.asset.toUpperCase()))
		.sort((a, b) => (b.sharpe ?? -99) - (a.sharpe ?? -99));

	onMount(async () => {
		try {
			const state = await getStrategyBreadth(strategyId);
			breadth = (state.rows ?? []).map((row) => ({ ...row, asset: String(row.asset ?? '').toUpperCase() }));
		} catch {
			breadth = [];
		}
	});

	function clean(value: string): string {
		return value.trim().toUpperCase().split(/[/:\-_ ]/)[0].replace(/(USDT|USDC|PERP)$/, '') || value.trim().toUpperCase();
	}

	function add(raw: string) {
		error = '';
		const coin = clean(raw);
		if (!coin) return;
		if (!/^[A-Z0-9]{1,15}$/.test(coin)) {
			error = `${raw.trim()} is not a coin symbol.`;
			return;
		}
		if (draft.includes(coin)) return;
		if (full) {
			error = `At most ${list.max_coins} coins, home coin included.`;
			return;
		}
		draft = [...draft, coin];
		input = '';
	}

	function remove(coin: string) {
		error = '';
		draft = draft.filter((c) => c !== coin);
	}

	async function save() {
		saving = true;
		error = '';
		try {
			const saved = await setStrategyCoins(strategyId, extras);
			if (saved) dispatch('saved', saved);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Enter') {
			event.preventDefault();
			add(input);
		}
	}
</script>

<div class="grid gap-3" data-testid="coin-list-editor">
	<div class="flex flex-wrap items-center gap-1.5">
		{#each draft as coin (coin)}
			{@const isHome = coin === list.home}
			{@const isOpen = openCoins.includes(coin) && !isHome}
			<span
				class={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-plex-mono text-[12px] ${isHome ? 'border-sc-ink3 text-sc-ink' : 'border-sc-line2 text-sc-ink2'}`}
				data-coin={coin}
			>
				{coin}
				{#if isHome}
					<span class="font-plex-cond text-[10px] uppercase tracking-[0.06em] text-sc-ink4">home</span>
				{:else if tested.size && !tested.has(coin)}
					<span class="font-plex-cond text-[10px] uppercase tracking-[0.06em] text-[#e7b24a]" title="Not picked by the breadth test">by hand</span>
				{/if}
				{#if !isHome}
					<button
						type="button"
						class="ml-0.5 text-sc-ink3 hover:text-sc-ink disabled:opacity-40"
						aria-label={`Remove ${coin}`}
						title={isOpen ? `${coin} has an open position. Close it before removing the coin.` : `Remove ${coin}`}
						disabled={isOpen}
						on:click={() => remove(coin)}
					>×</button>
				{/if}
			</span>
		{/each}
		<input
			class="terminal-input !w-28 !py-0.5 !px-2 font-plex-mono text-[12px]"
			placeholder="Add coin"
			aria-label="Add a coin"
			bind:value={input}
			on:keydown={onKey}
			disabled={full}
		/>
		<button type="button" class="rounded-full border border-sc-line2 px-2.5 py-0.5 text-[11px] text-sc-ink2 hover:text-sc-ink disabled:opacity-40" disabled={!input.trim() || full} on:click={() => add(input)}>Add</button>
	</div>

	{#if suggestions.length}
		<div class="grid gap-1">
			<div class="text-[11px] text-sc-ink3">From the breadth test (same rule, research data), best Sharpe first:</div>
			<div class="flex flex-wrap gap-1.5">
				{#each suggestions as row (row.asset)}
					<button
						type="button"
						class="inline-flex items-center gap-1.5 rounded-full border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink disabled:opacity-40"
						disabled={full}
						title={`${row.trades ?? 0} trades in the breadth test`}
						on:click={() => add(row.asset)}
					>
						<span class="font-plex-mono">+ {row.asset}</span>
						<span class={`font-plex-mono ${signClass(row.return_pct)}`}>{fmtPct(row.return_pct, 0)}</span>
						<span class="text-sc-ink4">Sharpe {fmtNum(row.sharpe ?? null)}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<p class="m-0 text-[11px] leading-relaxed text-sc-ink3">
		Each coin trades the same rule on an equal share of the paper book. An added coin records forward trades from the moment you save; its past is never replayed. Coins you add by hand carry no breadth-test evidence, and a multi-coin strategy stays in paper until going live with several coins is reviewed.
	</p>

	{#if error}
		<div class="text-[11px] text-[#f2956f]" role="alert">{error}</div>
	{/if}

	<div class="flex items-center gap-2">
		<button
			type="button"
			class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40"
			disabled={!changed || saving}
			on:click={save}
		>{saving ? 'Saving…' : extras.length ? `Save ${draft.length} coins` : `Trade ${list.home} only`}</button>
		<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" on:click={() => dispatch('cancel')}>Cancel</button>
	</div>
</div>
