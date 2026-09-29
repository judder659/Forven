<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { BacktestResult, StrategyContainerHistoryItem } from '$lib/api';
	import { fmtDateUtc, isNum, parseTimestamp, toNumber } from '$lib/utils/strategyContainer/format';

	/** The run a report describes: what ran, what was asked for, and the cost model. */
	export let result: BacktestResult | null = null;
	export let runs: StrategyContainerHistoryItem[] = [];
	export let selectedId = '';
	export let pinnedId = '';
	export let referenceId = '';

	const dispatch = createEventDispatcher<{ select: { resultId: string } }>();

	type Bag = Record<string, unknown>;
	$: config = (result?.config ?? {}) as Bag;
	$: ranStart = result?.start ?? null;
	$: ranEnd = result?.end ?? null;
	$: askedStart = typeof config.start === 'string' ? config.start : null;
	$: askedEnd = typeof config.end === 'string' ? config.end : null;
	// The stored start sits ~200 warm-up bars before the request; only flag a real move.
	$: moved = (() => {
		const re = parseTimestamp(ranEnd);
		const ae = parseTimestamp(askedEnd);
		const rs = parseTimestamp(ranStart);
		const as = parseTimestamp(askedStart);
		const endGap = re !== null && ae !== null ? Math.abs(re - ae) > 86_400_000 : false;
		const laterStart = rs !== null && as !== null ? rs - as > 86_400_000 : false;
		return endGap || laterStart;
	})();
	$: fee = toNumber(config.fee_bps);
	$: slip = toNumber(config.slippage_bps);
	$: leverage = toNumber(config.leverage);
	$: resultWarnings = ((result ?? {}) as unknown as Bag).warnings;
	$: warnings = [...(Array.isArray(config.warnings) ? config.warnings : []), ...(Array.isArray(resultWarnings) ? resultWarnings : [])]
		.map((warning) => String(warning ?? '').trim())
		.filter((warning, index, all) => warning && all.indexOf(warning) === index);
	$: tag = selectedId && selectedId === pinnedId ? 'pinned · drives paper and live' : selectedId && selectedId === referenceId ? 'newest · defaults drive execution' : 'not the driver';
</script>

<article class="grid gap-3 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" data-testid="run-facts">
	<div class="flex flex-wrap items-start justify-between gap-3">
		<div class="grid gap-1">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Run</div>
			<div class="flex flex-wrap items-center gap-2">
				<span class="font-plex-mono text-[12px] text-sc-ink" data-testid="run-facts-id">{selectedId || '—'}</span>
				<span class={`rounded-full border px-2 py-0.5 font-plex-cond text-[10.5px] uppercase tracking-[0.06em] ${selectedId && selectedId === pinnedId ? 'border-emerald-700/60 text-emerald-300' : 'border-sc-line2 text-sc-ink3'}`}>{tag}</span>
			</div>
		</div>
		{#if runs.length > 1}
			<label class="grid gap-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
				Switch run
				<select
					class="rounded-md border border-sc-line2 bg-sc-panel2 px-2 py-1 font-plex-mono text-[11px] normal-case tracking-normal text-sc-ink"
					value={selectedId}
					data-testid="run-facts-select"
					on:change={(event) => dispatch('select', { resultId: event.currentTarget.value })}
				>
					{#each runs as run (run.result_id)}
						<option value={run.result_id}>{run.result_id}{run.result_id === pinnedId ? ' (pinned)' : ''} · {run.symbol || '—'} {run.timeframe || ''} · {fmtDateUtc(run.created_at)}</option>
					{/each}
				</select>
			</label>
		{/if}
	</div>
	{#if result}
		<div class="flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-sc-ink3">
			<span><b class="font-medium text-sc-ink">{result.symbol || '—'} · {result.timeframe || '—'}</b></span>
			<span title="Bars the run actually used, including the warm-up before the requested start"><b class="font-medium text-sc-ink">{fmtDateUtc(ranStart)} – {fmtDateUtc(ranEnd)}</b> ran</span>
			{#if moved}
				<span class="text-yellow-400" data-testid="run-facts-moved" title="The research seal or data coverage moved the window">requested {fmtDateUtc(askedStart)} – {fmtDateUtc(askedEnd)}</span>
			{/if}
			<span><b class="font-medium text-sc-ink">{isNum(fee) ? fee : '—'} / {isNum(slip) ? slip : '—'} bps</b> fee / slippage a side</span>
			<span><b class="font-medium text-sc-ink">{isNum(leverage) ? `${leverage}×` : '—'}</b> {String(config.sizing_mode ?? '—')} sizing</span>
			<span><b class="font-medium text-sc-ink">{String(config.trade_mode ?? '—')}</b>{config.position_model ? ` · ${String(config.position_model)}` : ''}</span>
			{#if isNum(toNumber(config.engine_version))}<span>engine v{toNumber(config.engine_version)}</span>{/if}
		</div>
		{#each warnings as warning (warning)}
			<div class="border border-yellow-900 bg-yellow-500/5 px-2.5 py-1.5 text-[11px] text-yellow-400">{warning}</div>
		{/each}
	{/if}
</article>
