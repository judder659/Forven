<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { updateSettingsSection, type RegimeGateStatus } from '$lib/api/forven';
	import RegimeChip from '$lib/components/regime/RegimeChip.svelte';
	import { formatRegimeLabel } from '$lib/utils/labRegime';

	export let gate: RegimeGateStatus | null | undefined = null;
	/** Risk-page scope: filters the ledger + prove-it stats by execution type.
	 *  The gate itself (mode, rules, stances) is global — it guards both lanes. */
	export let scope: 'live' | 'paper' = 'live';

	const dispatch = createEventDispatcher<{ changed: void }>();
	const MODES = ['off', 'observe', 'enforce'] as const;

	let saving = false;
	let saveError = '';

	async function setMode(mode: (typeof MODES)[number]) {
		if (!gate || gate.mode === mode || saving) return;
		saving = true;
		saveError = '';
		try {
			await updateSettingsSection('risk', { regime_gate_mode: mode });
			dispatch('changed');
		} catch {
			saveError = 'Could not change gate mode — check the backend connection.';
		} finally {
			saving = false;
		}
	}

	function fmtTs(ts: string | null | undefined): string {
		if (!ts) return '—';
		const d = new Date(ts);
		return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
	}

	function mtmTone(value: number | null | undefined): string {
		if (value === null || value === undefined) return 'text-sc-ink3';
		// A NEGATIVE would-have-been return means the gate saved money.
		return value < 0 ? 'text-emerald-400' : 'text-red-400';
	}

	$: mode = gate?.mode ?? 'observe';
	$: scopedEvents = (gate?.events ?? []).filter((event) =>
		scope === 'paper'
			? (event.execution_type ?? 'paper') === 'paper'
			: (event.execution_type ?? 'paper') !== 'paper'
	);
	$: aggregates = (() => {
		const base = gate?.aggregates;
		if (!base) return base;
		const split = base.by_execution?.[scope];
		if (!split) return { ...base, events: 0, mtm_n: 0, mtm_avg_pct: null };
		return { ...base, ...split };
	})();
	$: verdict =
		aggregates && aggregates.mtm_n > 0 && aggregates.mtm_avg_pct !== null
			? aggregates.mtm_avg_pct < 0
				? 'earning its keep'
				: 'blocking winners — review rules'
			: 'collecting evidence';
	$: modeNote =
		mode === 'enforce'
			? `blocking ${gate?.block_long?.map((r) => formatRegimeLabel(r)).join(' / ') || '—'} longs · disarm any time`
			: mode === 'observe'
				? 'shadow mode — logging would-have-blocked signals, execution untouched'
				: 'gate disabled — no blocking, no logging';
</script>

<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3" data-testid="regime-gate-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h2 class="text-[14px] font-semibold text-sc-ink2">
			Regime Gate
			<span class="ml-2 border px-1.5 py-0.5 text-[9px] font-normal tracking-wider {scope === 'live' ? 'border-red-900 text-red-400' : 'border-sc-line2 text-sc-ink2'}" title="Ledger and outcomes filtered to this scope; the gate itself guards both lanes">{scope.toUpperCase()} LEDGER</span>
		</h2>
		<div class="flex items-center gap-3">
			<div class="inline-flex border border-sc-line2" role="group" aria-label="regime gate mode">
				{#each MODES as candidate (candidate)}
					<button
						class="border-r border-sc-line2 px-3 py-1 text-[12px] last:border-r-0 {mode === candidate
							? 'bg-sc-ink font-medium text-black'
							: 'text-sc-ink2 hover:text-sc-ink'}"
						disabled={saving || !gate}
						on:click={() => setMode(candidate)}
					>
						{candidate}
					</button>
				{/each}
			</div>
		</div>
	</div>

	{#if !gate}
		<p class="text-[11px] text-sc-ink3">Gate status unavailable.</p>
	{:else}
		<p class="text-[11px] {mode === 'enforce' ? 'text-yellow-500' : 'text-sc-ink3'}">{modeNote}</p>
		{#if saveError}
			<p class="text-[11px] text-red-400">{saveError}</p>
		{/if}

		<div class="divide-y divide-sc-line border border-sc-line">
			{#each gate.stances as stance (stance.asset)}
				<div class="flex flex-wrap items-center gap-3 px-3 py-2">
					<span class="w-10 font-bold text-sc-ink">{stance.asset}</span>
					<RegimeChip regime={stance} />
					{#if mode === 'off'}
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">—</span>
					{:else if stance.restricted.length}
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {mode === 'enforce' ? 'text-yellow-500' : 'text-sc-ink2'}">
							{mode === 'enforce' ? '' : 'would '}block {stance.restricted.join(' + ')}s
						</span>
					{:else}
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">no restriction</span>
					{/if}
				</div>
			{/each}
		</div>

		{#if aggregates}
			<div class="rounded-md flex flex-wrap gap-6 border border-sc-line bg-sc-panel px-3 py-2">
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						{mode === 'enforce' ? 'Blocked' : 'Would block'} · {aggregates.window_days}d
					</span>
					<span class="text-sm font-bold text-sc-ink">{aggregates.events}</span>
				</div>
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						Their MTM +{gate.mtm_horizon_hours}h (avg)
					</span>
					<span class="text-sm font-bold {mtmTone(aggregates.mtm_avg_pct)}">
						{aggregates.mtm_avg_pct === null ? `— (${aggregates.mtm_n} scored)` : `${aggregates.mtm_avg_pct > 0 ? '+' : ''}${aggregates.mtm_avg_pct.toFixed(2)}%`}
					</span>
				</div>
				<div>
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Gate verdict</span>
					<span class="text-sm font-bold {verdict === 'earning its keep' ? 'text-emerald-400' : verdict === 'collecting evidence' ? 'text-sc-ink2' : 'text-red-400'}">
						{verdict}
					</span>
				</div>
			</div>
		{/if}

		{#if scopedEvents.length}
			<div class="overflow-x-auto">
				<table class="w-full text-[11px]">
					<thead>
						<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							<th class="px-2 py-1">Time</th>
							<th class="px-2 py-1">Strategy</th>
							<th class="px-2 py-1">Signal</th>
							<th class="px-2 py-1">Regime</th>
							<th class="px-2 py-1 text-right">Conf</th>
							<th class="px-2 py-1">Decision</th>
							<th class="px-2 py-1 text-right">MTM +{gate.mtm_horizon_hours}h</th>
						</tr>
					</thead>
					<tbody>
						{#each scopedEvents as event (event.id)}
							<tr class="border-b border-sc-line">
								<td class="whitespace-nowrap px-2 py-1 text-sc-ink2">{fmtTs(event.ts)}</td>
								<td class="px-2 py-1 font-mono text-sc-ink">{event.strategy_id}</td>
								<td class="px-2 py-1 {event.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}">
									{event.direction} {event.asset}
									{#if event.execution_type}<span class="ml-1 text-[9px] uppercase text-sc-ink3">{event.execution_type}</span>{/if}
								</td>
								<td class="px-2 py-1"><RegimeChip mini regime={event.regime} /></td>
								<td class="px-2 py-1 text-right text-sc-ink2">
									{event.confidence === null || event.confidence === undefined ? '—' : `${Math.round(event.confidence * 100)}%`}
								</td>
								<td class="px-2 py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {event.decision === 'blocked' ? 'text-yellow-500' : 'text-sc-ink2'}">
									{event.decision === 'blocked' ? 'blocked' : 'would block'}
								</td>
								<td class="px-2 py-1 text-right font-bold {mtmTone(event.mtm_pct)}">
									{event.mtm_pct === null || event.mtm_pct === undefined ? 'pending' : `${event.mtm_pct > 0 ? '+' : ''}${event.mtm_pct.toFixed(2)}%`}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="text-[11px] text-sc-ink3">
				No {scope} gate events yet{mode === 'off' ? ' — the gate is off' : ''}.
			</p>
		{/if}

		<p class="text-[10px] text-sc-ink3">
			Rules: block longs in {gate.block_long.map((r) => formatRegimeLabel(r)).join(', ') || 'none'};
			block shorts in {gate.block_short.map((r) => formatRegimeLabel(r)).join(', ') || 'none'};
			min confidence {Math.round(gate.min_confidence * 100)}%.
			A <span class="text-emerald-400">green MTM</span> means blocked entries would have LOST money — the gate helped.
			<a class="text-sc-ink2 underline hover:text-sc-ink" href="/settings#trading/risk.regime_gate_mode">Edit rules</a>
		</p>
	{/if}
</div>
