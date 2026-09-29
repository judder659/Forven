<script lang="ts">
	/** One line: can it trade, the gates, the exchange reconcile and the scanner's timing. */
	import type { ForvenDashboardResponse } from '$lib/api';
	import type { SchedulerJobSummary } from '$lib/api/dashboard';
	import type { DeskMode } from '$lib/api/desk';
	import { ago, dur, parseTs } from '$lib/utils/tradingDesk/format';
	import { gatesOf } from '$lib/utils/tradingDesk/gates';

	export let mode: DeskMode;
	export let dashboard: ForvenDashboardResponse | null = null;
	export let jobs: SchedulerJobSummary[] = [];
	export let wsConnected = false;
	export let now = Date.now();

	let gatesOpen = false;

	$: gates = gatesOf(dashboard);
	$: blocking = gates.filter((gate) => !gate.clear);
	$: allowed = dashboard ? dashboard.trading_allowed !== false && blocking.length === 0 : null;
	$: recovery = dashboard?.recovery ?? null;
	$: scanner = jobs.find((job) => job.id === 'forven-scanner-hourly') ?? jobs.find((job) => job.id === 'forven-scanner-signal') ?? null;
	$: nextScan = parseTs(scanner?.nextRunAt);
	$: network = dashboard?.account?.network ?? recovery?.network ?? 'mainnet';

	const dot = (tone: 'ok' | 'caution' | 'fail' | 'idle') =>
		`inline-block h-2 w-2 flex-none rounded-full ${tone === 'ok' ? 'bg-[#3cc48f] shadow-[0_0_0_3px_rgba(60,196,143,0.15)]' : tone === 'caution' ? 'bg-[#e7b24a] shadow-[0_0_0_3px_rgba(231,178,74,0.15)]' : tone === 'fail' ? 'bg-[#e5574f] shadow-[0_0_0_3px_rgba(229,87,79,0.18)]' : 'bg-sc-ink4'}`;
</script>

<svelte:window on:click={() => (gatesOpen = false)} />

<section class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-md border border-sc-line bg-sc-panel px-3 py-2 text-[12px] text-sc-ink2" aria-label="Trading status" data-testid="desk-status">
	{#if mode === 'live'}
		<span class="inline-flex items-center gap-2">
			<i class={dot(allowed === null ? 'idle' : allowed ? 'ok' : 'fail')}></i>
			<b class="font-medium text-sc-ink">{allowed === null ? 'Status loading' : allowed ? 'Trading allowed' : 'Trading blocked'}</b>
			{#if allowed === false}<span>{dashboard?.trading_reason || blocking.map((gate) => gate.name).join(', ')}</span>{/if}
		</span>
		<span class="relative">
			<button type="button" class="rounded-full border border-sc-line2 px-2.5 py-px text-[12px] text-sc-ink2 hover:border-sc-ink4 hover:text-sc-ink" aria-expanded={gatesOpen} on:click|stopPropagation={() => (gatesOpen = !gatesOpen)}>
				{gates.length - blocking.length} of {gates.length} gates clear ▾
			</button>
			{#if gatesOpen}
				<div class="absolute left-0 top-full z-30 mt-2 grid min-w-[260px] gap-1.5 rounded-md border border-sc-line2 bg-[#0b0d11] p-3 shadow-2xl" role="presentation" on:click|stopPropagation>
					{#each gates as gate}
						<div class="grid grid-cols-[10px_minmax(0,1fr)_auto] items-center gap-2">
							<i class={dot(gate.clear ? 'ok' : 'fail')}></i>
							<span>{gate.name}</span>
							<span class="font-plex-mono text-[11px] text-sc-ink3">{gate.value}</span>
						</div>
					{/each}
				</div>
			{/if}
		</span>
		<span class="rounded-full border border-[#8fb0ff]/45 px-2.5 font-plex-cond text-[11px] font-semibold uppercase tracking-[0.08em] text-[#8fb0ff]">Hyperliquid {network}</span>
		{#if recovery}
			<span class="inline-flex items-center gap-2">
				<i class={dot(recovery.requires_operator ? 'fail' : recovery.discrepancy_count ? 'caution' : 'ok')}></i>
				Reconciled <b class="font-medium text-sc-ink">{ago(recovery.last_checked_at, now)}</b>
				<span class="text-sc-ink3">· {recovery.discrepancy_count ? `${recovery.discrepancy_count} discrepancies` : 'positions and orders match'}</span>
			</span>
		{/if}
	{:else}
		<span class="inline-flex items-center gap-2">
			<i class={dot('ok')}></i>
			<b class="font-medium text-sc-ink">Paper books</b>
			<span class="text-sc-ink3">simulated fills at the live mid · no real orders · account halts don't apply to paper</span>
		</span>
	{/if}
	{#if scanner}
		<span class="inline-flex items-center gap-2">
			<i class={dot(scanner.lastStatus === 'ok' || !scanner.lastStatus ? 'ok' : 'caution')}></i>
			Scanner ran <b class="font-medium text-sc-ink">{ago(scanner.lastRunAt, now)}</b>
			{#if nextScan !== null}
				<span class="text-sc-ink3">· next {nextScan > now ? `in ${dur(nextScan - now)}` : 'due now'}</span>
			{/if}
		</span>
	{/if}
	<span class="inline-flex items-center gap-2">
		<i class={dot(wsConnected ? 'ok' : 'caution')}></i>
		Prices <span class="text-sc-ink3">{wsConnected ? 'streaming' : 'polling (stream reconnecting)'}</span>
	</span>
</section>
