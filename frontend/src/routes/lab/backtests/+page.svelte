<script lang="ts">
	import { onMount } from 'svelte';
	import { getResults, type ResultSummary } from '$lib/api';
	import StrategyLink from '$lib/components/ui/StrategyLink.svelte';
	import SortableTh from '$lib/components/ui/SortableTh.svelte';

	type SortField =
		| 'created'
		| 'strategy'
		| 'symbol'
		| 'timeframe'
		| 'type'
		| 'return'
		| 'cagr'
		| 'sharpe'
		| 'drawdown'
		| 'win_rate'
		| 'trades'
		| 'profit_factor'
		| 'months';
	type SortDirection = 'asc' | 'desc';
	type ResultWithExtras = ResultSummary & { profit_factor_is_infinite?: boolean };

	const MAX_RESULTS = 5000;

	let loading = true;
	let error = '';
	let results: ResultWithExtras[] = [];
	let search = '';
	let symbolFilter = 'all';
	let typeFilter = 'all';
	let sortBy: SortField = 'created';
	let sortDirection: SortDirection = 'desc';

	onMount(() => {
		void loadResults();
	});

	async function loadResults(): Promise<void> {
		loading = true;
		error = '';
		try {
			results = (await getResults(undefined, undefined, MAX_RESULTS)) as ResultWithExtras[];
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load backtests';
		} finally {
			loading = false;
		}
	}

	function resultStrategyId(result: ResultSummary): string {
		return String(result.lifecycle_strategy_id || result.strategy_id || '').trim();
	}

	function resultType(result: ResultSummary): string {
		return String(result.result_type || 'backtest').trim().toLowerCase() || 'backtest';
	}

	function resultHaystack(result: ResultSummary): string {
		return [
			result.id,
			result.job_id,
			result.strategy_name,
			result.strategy_id,
			result.lifecycle_strategy_id,
			result.symbol,
			result.timeframe,
			result.result_type,
			result.verdict,
			result.description,
		]
			.map((value) => String(value ?? '').toLowerCase())
			.join(' ');
	}

	function formatDateTime(value: string | null | undefined): string {
		if (!value) return '-';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '-';
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function formatDate(value: string | null | undefined): string {
		if (!value) return '-';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '-';
		return date.toLocaleDateString();
	}

	function formatPercent(value: number | null | undefined, decimals = 2): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '-';
		return `${value.toFixed(decimals)}%`;
	}

	function formatNumber(value: number | null | undefined, decimals = 2): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '-';
		return value.toFixed(decimals);
	}

	function formatMonths(value: number | null | undefined): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return '-';
		if (value < 1) return value.toFixed(2);
		return value.toFixed(1);
	}

	function metricClass(kind: 'return' | 'sharpe' | 'drawdown' | 'win_rate' | 'profit_factor', value: number | null | undefined): string {
		if (value === null || value === undefined || !Number.isFinite(value)) return 'text-[#555]';
		switch (kind) {
			case 'return':
				return value >= 0 ? 'text-emerald-400' : 'text-red-400';
			case 'sharpe':
				if (value >= 1.0) return 'text-emerald-400';
				if (value >= 0.5) return 'text-emerald-300';
				if (value > 0) return 'text-yellow-300';
				return 'text-red-400';
			case 'drawdown':
				if (value <= 20) return 'text-emerald-400';
				if (value <= 35) return 'text-yellow-300';
				return 'text-red-400';
			case 'win_rate':
				if (value >= 55) return 'text-emerald-400';
				if (value >= 45) return 'text-yellow-300';
				return 'text-red-400';
			case 'profit_factor':
				if (value >= 1.5) return 'text-emerald-400';
				if (value >= 1.0) return 'text-yellow-300';
				return 'text-red-400';
		}
	}

	function verdictClass(verdict: string | undefined): string {
		const normalized = String(verdict ?? '').trim().toLowerCase();
		if (normalized.includes('promising') || normalized.includes('robust')) {
			return 'border-emerald-800 bg-emerald-950/20 text-emerald-300';
		}
		if (normalized.includes('marginal')) {
			return 'border-yellow-800 bg-yellow-950/20 text-yellow-300';
		}
		if (normalized.includes('weak') || normalized.includes('failed') || normalized.includes('not robust')) {
			return 'border-red-800 bg-red-950/20 text-red-300';
		}
		return 'border-[#333] bg-[#111] text-[#888]';
	}

	function toggleSort(field: SortField): void {
		if (sortBy === field) {
			sortDirection = sortDirection === 'desc' ? 'asc' : 'desc';
			return;
		}
		sortBy = field;
		sortDirection = field === 'drawdown' ? 'asc' : 'desc';
	}

	function numericSortValue(result: ResultWithExtras, field: SortField): number {
		switch (field) {
			case 'created': return Date.parse(result.created_at) || 0;
			case 'return': return result.total_return ?? Number.NEGATIVE_INFINITY;
			case 'cagr': return result.annualized_return_pct ?? Number.NEGATIVE_INFINITY;
			case 'sharpe': return result.sharpe_ratio ?? Number.NEGATIVE_INFINITY;
			case 'drawdown': return result.max_drawdown ?? Number.POSITIVE_INFINITY;
			case 'win_rate': return result.win_rate ?? Number.NEGATIVE_INFINITY;
			case 'trades': return result.total_trades ?? Number.NEGATIVE_INFINITY;
			case 'profit_factor':
				return result.profit_factor_is_infinite ? Number.POSITIVE_INFINITY : (result.profit_factor ?? Number.NEGATIVE_INFINITY);
			case 'months': return result.backtest_months ?? Number.NEGATIVE_INFINITY;
			default: return 0;
		}
	}

	function stringSortValue(result: ResultSummary, field: SortField): string {
		switch (field) {
			case 'strategy': return String(result.strategy_name || resultStrategyId(result)).toLowerCase();
			case 'symbol': return String(result.symbol || '').toLowerCase();
			case 'timeframe': return String(result.timeframe || '').toLowerCase();
			case 'type': return resultType(result);
			default: return '';
		}
	}

	function compareResults(
		a: ResultWithExtras,
		b: ResultWithExtras,
		field: SortField,
		direction: SortDirection
	): number {
		if (field === 'strategy' || field === 'symbol' || field === 'timeframe' || field === 'type') {
			const compared = stringSortValue(a, field).localeCompare(stringSortValue(b, field));
			return direction === 'asc' ? compared : -compared;
		}
		const av = numericSortValue(a, field);
		const bv = numericSortValue(b, field);
		if (av < bv) return direction === 'asc' ? -1 : 1;
		if (av > bv) return direction === 'asc' ? 1 : -1;
		return Date.parse(b.created_at) - Date.parse(a.created_at);
	}

	$: symbolOptions = ['all', ...Array.from(new Set(results.map((result) => String(result.symbol || '').trim()).filter(Boolean))).sort()];
	$: typeOptions = ['all', ...Array.from(new Set(results.map(resultType).filter(Boolean))).sort()];
	$: normalizedSearch = search.trim().toLowerCase();
	$: filteredResults = results
		.filter((result) => symbolFilter === 'all' || String(result.symbol || '') === symbolFilter)
		.filter((result) => typeFilter === 'all' || resultType(result) === typeFilter)
		.filter((result) => !normalizedSearch || resultHaystack(result).includes(normalizedSearch));
	// Reference sortBy/sortDirection directly so Svelte tracks them as dependencies
	// of this reactive sort — they are read inside compareResults, which the compiler
	// does not trace into, so the table would otherwise never re-sort on header click.
	$: sortedResults = [...filteredResults].sort((a, b) => compareResults(a, b, sortBy, sortDirection));
	$: loadedSummary = loading ? 'Loading...' : `${sortedResults.length} of ${results.length} loaded`;
</script>

<svelte:head>
	<title>All Backtests | Forven</title>
	<meta name="description" content="Sortable backtest result inventory for strategy containers." />
</svelte:head>

<div class="h-full flex flex-col overflow-hidden">
	<div class="px-4 py-3 bg-[#050505] border-b border-[#222] flex-shrink-0">
		<div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
			<div>
				<div class="mb-1">
					<a href="/lab" class="text-xs text-[#555] uppercase tracking-wider transition-colors hover:text-white">The Forge</a>
				</div>
				<h1 class="text-xl font-bold uppercase tracking-widest text-white">All Backtests</h1>
				<p class="mt-1 text-xs text-[#666]">{loadedSummary}</p>
			</div>
			<div class="flex items-center gap-2 self-start md:self-auto">
				<a
					href="/lab"
					class="text-xs border border-[#333] px-3 py-1.5 text-[#888] transition-colors hover:border-white hover:text-white"
				>
					Back to Forge
				</a>
				<button
					type="button"
					on:click={loadResults}
					disabled={loading}
					class="text-xs border border-[#333] px-3 py-1.5 text-[#888] transition-colors hover:border-white hover:text-white disabled:opacity-40"
				>
					{loading ? 'Loading...' : 'Refresh'}
				</button>
			</div>
		</div>
	</div>

	<section class="flex-1 flex flex-col overflow-hidden min-h-[500px]">
		<div class="border-b border-[#222] px-4 py-2 flex items-center gap-2 flex-wrap bg-[#050505]">
			<input
				type="text"
				bind:value={search}
				placeholder="Search strategy, result id, symbol..."
				class="bg-black border border-[#333] px-3 py-1.5 text-xs w-full focus:outline-none focus:border-white sm:w-80"
			/>
			<select
				aria-label="Filter by symbol"
				bind:value={symbolFilter}
				class="terminal-input !w-full !py-1 !px-2 text-xs sm:!w-44"
			>
				{#each symbolOptions as symbol}
					<option value={symbol}>{symbol === 'all' ? 'All symbols' : symbol}</option>
				{/each}
			</select>
			<select
				aria-label="Filter by result type"
				bind:value={typeFilter}
				class="terminal-input !w-full !py-1 !px-2 text-xs sm:!w-44"
			>
				{#each typeOptions as type}
					<option value={type}>{type === 'all' ? 'All types' : type}</option>
				{/each}
			</select>
			<span class="ml-auto text-[11px] text-[#555]">{sortedResults.length} rows</span>
		</div>

		{#if error}
			<div class="border-b border-red-900 bg-red-500/5 px-4 py-2 text-xs text-red-400">{error}</div>
		{/if}

		<div class="flex-1 overflow-auto bg-black">
			<table class="w-full min-w-[1280px] text-xs">
				<thead class="sticky top-0 z-10 bg-[#0d0d0d]">
					<tr class="border-b border-[#222] text-[#666]">
						<th class="py-2 px-2 text-left">Backtest</th>
						<SortableTh field="strategy" label="Strategy" active={sortBy === 'strategy'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="symbol" label="Pair" active={sortBy === 'symbol'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="timeframe" label="TF" active={sortBy === 'timeframe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="type" label="Type" active={sortBy === 'type'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="return" label="Return" active={sortBy === 'return'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Total return over the tested window." />
						<SortableTh field="cagr" label="CAGR" active={sortBy === 'cagr'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} title="Annualized return when available." />
						<SortableTh field="sharpe" label="Sharpe" active={sortBy === 'sharpe'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="drawdown" label="Max DD" active={sortBy === 'drawdown'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="win_rate" label="Win%" active={sortBy === 'win_rate'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="trades" label="Trades" active={sortBy === 'trades'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="profit_factor" label="PF" active={sortBy === 'profit_factor'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<SortableTh field="months" label="Months" active={sortBy === 'months'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
						<th class="py-2 px-2 text-left">Window</th>
						<th class="py-2 px-2 text-left">Verdict</th>
						<SortableTh field="created" label="Created" active={sortBy === 'created'} direction={sortDirection} on:sort={(e) => toggleSort(e.detail as SortField)} />
					</tr>
				</thead>
				<tbody>
					{#if loading}
						<tr><td colspan="16" class="py-8 text-center text-[#555]">Loading backtests...</td></tr>
					{:else if sortedResults.length === 0}
						<tr><td colspan="16" class="py-8 text-center text-[#555]">No backtests match this view.</td></tr>
					{:else}
						{#each sortedResults as result (result.id)}
							{@const strategyId = resultStrategyId(result)}
							<tr class="border-t border-[#181818] hover:bg-[#0f0f0f]">
								<td class="py-2 px-2 font-mono text-[#888]">
									<div class="text-white">{result.id}</div>
									<div class="mt-0.5 text-[10px] text-[#555]">{result.job_id}</div>
								</td>
								<td class="py-2 px-2 max-w-[360px]">
									<StrategyLink
										strategyId={strategyId}
										label={result.strategy_name || strategyId || 'Unknown Strategy'}
										returnTo="/lab/backtests"
										className="max-w-full truncate bg-transparent border-0 px-0 py-0 text-left text-white hover:text-emerald-400"
										titlePrefix="Open strategy container"
									/>
									{#if strategyId}
										<div class="mt-0.5 font-mono text-[10px] text-[#555]">{strategyId}</div>
									{/if}
								</td>
								<td class="py-2 px-2 font-mono text-[#888]">{result.symbol || '-'}</td>
								<td class="py-2 px-2 font-mono text-[#888]">{result.timeframe || '-'}</td>
								<td class="py-2 px-2">
									<span class="border border-[#333] bg-[#111] px-1.5 py-0.5 text-[10px] uppercase tracking-[0.14em] text-[#888]">{resultType(result)}</span>
								</td>
								<td class={`py-2 px-2 font-mono ${metricClass('return', result.total_return)}`}>{formatPercent(result.total_return)}</td>
								<td class={`py-2 px-2 font-mono ${metricClass('return', result.annualized_return_pct)}`}>{formatPercent(result.annualized_return_pct)}</td>
								<td class={`py-2 px-2 font-mono ${metricClass('sharpe', result.sharpe_ratio)}`}>{formatNumber(result.sharpe_ratio)}</td>
								<td class={`py-2 px-2 font-mono ${metricClass('drawdown', result.max_drawdown)}`}>{formatPercent(result.max_drawdown)}</td>
								<td class={`py-2 px-2 font-mono ${metricClass('win_rate', result.win_rate)}`}>{formatPercent(result.win_rate, 1)}</td>
								<td class="py-2 px-2 font-mono text-[#888]">{formatNumber(result.total_trades, 0)}</td>
								<td class={`py-2 px-2 font-mono ${result.profit_factor_is_infinite ? 'text-emerald-400' : metricClass('profit_factor', result.profit_factor)}`}>
									{result.profit_factor_is_infinite ? 'Inf' : formatNumber(result.profit_factor)}
								</td>
								<td class="py-2 px-2 font-mono text-[#888]">{formatMonths(result.backtest_months)}</td>
								<td class="py-2 px-2 text-[#666]">{formatDate(result.start)} to {formatDate(result.end)}</td>
								<td class="py-2 px-2">
									<span class={`border px-1.5 py-0.5 text-[10px] uppercase tracking-[0.14em] ${verdictClass(result.verdict)}`}>
										{result.verdict || '-'}
									</span>
								</td>
								<td class="py-2 px-2 text-[#666]">{formatDateTime(result.created_at)}</td>
							</tr>
						{/each}
					{/if}
				</tbody>
			</table>
		</div>
	</section>
</div>
