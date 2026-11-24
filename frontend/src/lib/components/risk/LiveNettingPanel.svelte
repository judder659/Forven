<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ApiError } from '$lib/api/core';
	import {
		NETTING_CONFIRM_PHRASE,
		checkLiveNetting,
		getLiveNetting,
		resolveLiveNetting,
		setLiveNetting,
		updateLiveNettingSettings,
		type NettingCoin,
		type NettingResolveAction,
		type NettingStatus
	} from '$lib/api/liveNetting';

	let status: NettingStatus | null = null;
	let loadError = '';
	let actionError = '';
	let busy = false;
	let confirmText = '';
	let asking = false;
	let slotsInput = '';
	let timer: ReturnType<typeof setInterval> | null = null;

	function message(err: unknown, fallback: string): string {
		if (err instanceof ApiError && err.message) return err.message;
		if (err instanceof Error && err.message) return err.message;
		return fallback;
	}

	async function load() {
		try {
			status = await getLiveNetting();
			loadError = '';
			if (!slotsInput) slotsInput = String(status.settings.live_netting_slots_override || '');
		} catch (err) {
			loadError = message(err, 'Could not read the netting engine status.');
		}
	}

	async function run(action: () => Promise<NettingStatus>, fallback: string) {
		if (busy) return;
		busy = true;
		actionError = '';
		try {
			status = await action();
		} catch (err) {
			actionError = message(err, fallback);
		} finally {
			busy = false;
		}
	}

	async function turnOn() {
		await run(() => setLiveNetting(true, confirmText), 'Could not turn the engine on.');
		if (!actionError) {
			asking = false;
			confirmText = '';
		}
	}

	const turnOff = () => run(() => setLiveNetting(false), 'Could not turn the engine off.');
	const checkNow = () => run(() => checkLiveNetting(), 'Could not check the exchange.');
	const resolve = (coin: string, action: NettingResolveAction) =>
		run(() => resolveLiveNetting(coin, action), 'Could not resolve the difference.');

	function saveSlots() {
		const value = slotsInput.trim() === '' ? 0 : Number(slotsInput);
		if (!Number.isInteger(value) || value < 0) {
			actionError = 'Slots must be a whole number, or empty to let Forven calculate it.';
			return;
		}
		return run(() => updateLiveNettingSettings({ slots_override: value }), 'Could not save the slot count.');
	}

	function num(value: number | null | undefined, digits = 4): string {
		if (value === null || value === undefined || Number.isNaN(value)) return '—';
		return Number(value.toFixed(digits)).toString();
	}

	function money(value: number | null | undefined): string {
		if (value === null || value === undefined || Number.isNaN(value)) return '—';
		return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	}

	function side(units: number): string {
		if (Math.abs(units) < 1e-12) return 'flat';
		return units > 0 ? 'long' : 'short';
	}

	function matches(coin: NettingCoin): boolean {
		return !coin.mismatch;
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), 20000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	$: slots = status?.slots ?? null;
	$: problems = (status?.coins ?? []).filter((coin) => coin.mismatch?.confirmed);
</script>

<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3" data-testid="live-netting-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h2 class="text-[14px] font-semibold text-sc-ink2">
			Shared coins (live netting)
			<span
				class="ml-2 border px-1.5 py-0.5 text-[9px] font-normal tracking-wider {status?.active
					? 'border-emerald-900 text-emerald-400'
					: 'border-sc-line2 text-sc-ink3'}"
			>
				{status?.active ? 'ON' : 'OFF'}
			</span>
		</h2>
		{#if status}
			<div class="flex items-center gap-2">
				{#if status.enabled}
					<button class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" disabled={busy} on:click={checkNow}>
						Check now
					</button>
					<button class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" disabled={busy} on:click={turnOff}>
						Turn off
					</button>
				{:else if !asking}
					<button class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" disabled={busy} on:click={() => (asking = true)}>
						Turn on
					</button>
				{/if}
			</div>
		{/if}
	</div>

	{#if loadError}
		<p class="text-[11px] text-red-400">{loadError}</p>
	{:else if !status}
		<p class="text-[11px] text-sc-ink3">Loading…</p>
	{:else}
		<p class="text-[11px] text-sc-ink3">
			{#if status.active}
				Any number of live strategies can trade the same coin. The exchange holds one position per coin, the sum of
				their trades. Forven runs each strategy's own stop and target, and one disaster stop per coin rests on the
				exchange in case Forven is not running.
			{:else}
				Off: one live strategy can hold a coin at a time and every other entry on that coin is refused. Turn this on
				to let live strategies share coins. It can only be switched while no live position is open.
			{/if}
		</p>
		{#if status.books_on}
			<p class="text-[11px] text-yellow-500">
				Direction books (sub-accounts) are on. The engine runs on the main wallet only and stays off until they are
				turned off.
			</p>
		{/if}

		{#if asking && !status.enabled}
			<div class="space-y-2 border border-yellow-900 p-3">
				<p class="text-[11px] text-yellow-500">
					With this on, a live trade no longer has its own stop order on the exchange. Forven closes it when its
					stop or target is touched, so Forven must be running. The exchange keeps one disaster stop per coin,
					{num(status.settings.live_netting_disaster_gap_pct, 2)}% beyond the furthest strategy stop. Type
					{NETTING_CONFIRM_PHRASE} to turn it on.
				</p>
				<div class="flex flex-wrap items-center gap-2">
					<input
						class="w-40 border border-sc-line2 bg-transparent px-2 py-1 text-[12px] text-sc-ink"
						placeholder={NETTING_CONFIRM_PHRASE}
						bind:value={confirmText}
					/>
					<button
						class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink disabled:opacity-40"
						disabled={busy || confirmText.trim().toUpperCase() !== NETTING_CONFIRM_PHRASE}
						on:click={turnOn}
					>
						Turn on
					</button>
					<button class="px-2 py-1 text-[12px] text-sc-ink3 hover:text-sc-ink" on:click={() => ((asking = false), (confirmText = ''))}>
						Cancel
					</button>
				</div>
			</div>
		{/if}

		{#if actionError}
			<p class="text-[11px] text-red-400">{actionError}</p>
		{/if}

		{#if status.enabled}
			{#if problems.length}
				<div class="space-y-2 border border-red-900 p-3">
					{#each problems as coin (coin.coin)}
						<div class="space-y-2">
							<p class="text-[12px] text-red-400">
								{coin.coin}: live trades add up to {num(coin.mismatch?.book_units)} but the exchange holds
								{num(coin.mismatch?.exchange_units)}. New entries on {coin.coin} are paused.
							</p>
							<div class="flex flex-wrap gap-2">
								<button
									class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink"
									disabled={busy}
									title="Send the order that makes the exchange hold what the live trades add up to"
									on:click={() => resolve(coin.coin, 'trade_to_books')}
								>
									Trade the exchange to match
								</button>
								<button
									class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink"
									disabled={busy}
									title="Only when the exchange holds none of this coin: close its live trades in Forven at the current price, with no order"
									on:click={() => resolve(coin.coin, 'close_books')}
								>
									Close these trades in Forven
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<div class="rounded-md flex flex-wrap items-end gap-6 border border-sc-line bg-sc-panel px-3 py-2">
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Slots in use</span>
					<span class="text-sm font-bold text-sc-ink">{slots ? `${num(slots.in_use, 2)} of ${slots.slots}` : '—'}</span>
				</div>
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">One slot</span>
					<span class="text-sm font-bold text-sc-ink">{money(slots?.slot_usd)}</span>
				</div>
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">History asks for</span>
					<span class="text-sm font-bold text-sc-ink">{slots?.slots_wanted ?? '—'}</span>
				</div>
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Balance can carry</span>
					<span class="text-sm font-bold {slots?.capital_bound ? 'text-yellow-500' : 'text-sc-ink'}">{slots?.slots_capital_cap ?? '—'}</span>
				</div>
				<div class="flex items-end gap-2">
					<label class="block">
						<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Set slots yourself</span>
						<input
							class="w-24 border border-sc-line2 bg-transparent px-2 py-1 text-[12px] text-sc-ink"
							placeholder="auto"
							inputmode="numeric"
							bind:value={slotsInput}
						/>
					</label>
					<button class="border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" disabled={busy} on:click={saveSlots}>
						Save
					</button>
				</div>
			</div>
			<p class="text-[10px] text-sc-ink3">
				A slot is one full-size position, and a probation trade takes its share of one. The count covers
				{num(status.settings.live_netting_slot_coverage_pct, 1)}% of the time these strategies have been in a trade
				together, plus one spare, and never more than this account's balance can carry at the exchange's $10 minimum
				order. When every slot is in use, a new entry waits.
			</p>

			{#if status.coins.length}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
								<th class="px-2 py-1">Coin</th>
								<th class="px-2 py-1 text-right">Trades</th>
								<th class="px-2 py-1 text-right">Long</th>
								<th class="px-2 py-1 text-right">Short</th>
								<th class="px-2 py-1 text-right">Net (Forven)</th>
								<th class="px-2 py-1 text-right">Exchange</th>
								<th class="px-2 py-1 text-right">Disaster stop</th>
								<th class="px-2 py-1">State</th>
							</tr>
						</thead>
						<tbody>
							{#each status.coins as coin (coin.coin)}
								<tr class="border-b border-sc-line">
									<td class="px-2 py-1 font-bold text-sc-ink">{coin.coin}</td>
									<td class="px-2 py-1 text-right text-sc-ink2">{coin.trades.length}</td>
									<td class="px-2 py-1 text-right text-emerald-400">{num(coin.long_units)}</td>
									<td class="px-2 py-1 text-right text-red-400">{num(coin.short_units)}</td>
									<td class="px-2 py-1 text-right text-sc-ink">{num(coin.book_units)} {side(coin.book_units)}</td>
									<td class="px-2 py-1 text-right text-sc-ink2">{num(coin.exchange_units)}</td>
									<td class="px-2 py-1 text-right {coin.disaster_stop?.error ? 'text-yellow-500' : 'text-sc-ink2'}" title={coin.disaster_stop?.error ?? ''}>
										{coin.disaster_stop ? num(coin.disaster_stop.price, 6) : '—'}
									</td>
									<td class="px-2 py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {matches(coin) ? 'text-emerald-400' : coin.mismatch?.confirmed ? 'text-red-400' : 'text-yellow-500'}">
										{matches(coin) ? 'matches' : coin.mismatch?.confirmed ? 'differs' : 'checking'}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{:else}
				<p class="text-[11px] text-sc-ink3">No live positions open.</p>
			{/if}
			{#if status.last_sweep_at}
				<p class="text-[10px] text-sc-ink3">Last checked against the exchange {new Date(status.last_sweep_at).toLocaleString()}.</p>
			{/if}
		{/if}
	{/if}
</div>
