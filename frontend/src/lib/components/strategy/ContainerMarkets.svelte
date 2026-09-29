<script lang="ts">
	// The strategy page's Markets tab: the strategy's Gauntlet backtest on other
	// markets and timeframes, each in its own worker. Markets without local data
	// are reported and never requested (the loader would download them).
	import { createEventDispatcher, onDestroy } from 'svelte';
	import { strategyMarkets, type StrategyToolRequest } from '$lib/api';
	import { hasLocalData } from '$lib/utils/creatorGrids';
	import { runMarketRequests } from '$lib/utils/gridRuns';
	import MarketGridPanel, { marketKey, type MarketView } from './MarketGridPanel.svelte';

	export let request: StrategyToolRequest;
	export let availability: Map<string, Set<string>> | null = null;
	export let symbolOptions: string[] = [];
	export let currentSymbol = '';
	export let currentTimeframe = '';

	const dispatch = createEventDispatcher<{ pick: { symbol: string; timeframe: string } }>();
	let view: MarketView | null = null;
	let controller: AbortController | null = null;
	let context = '';
	onDestroy(() => controller?.abort());

	/** What a run covers apart from the market it varies; any other change makes it stale. */
	const contextOf = (req: StrategyToolRequest) => JSON.stringify({ ...req, symbol: null, timeframe: null });
	$: stale = !!view && context !== contextOf(request);
	$: if (stale && view?.status === 'running') controller?.abort();

	async function run({ symbols, timeframes }: { symbols: string[]; timeframes: string[] }) {
		controller?.abort();
		const own = new AbortController();
		controller = own;
		const base = { ...request };
		const cells = symbols.flatMap((s) => timeframes.map((t) => ({ symbol: s, timeframe: t })));
		view = { symbols, timeframes, rows: {}, done: 0, total: cells.length, status: 'running', warnings: [] };
		context = contextOf(base);
		const runnable = cells.filter((cell) => {
			if (!availability || hasLocalData(availability, cell.symbol, cell.timeframe)) return true;
			view!.rows[marketKey(cell.symbol, cell.timeframe)] = { ...cell, status: 'no_data',
				message: `No local ${cell.timeframe} data for ${cell.symbol}. Collect it on the Data page.` };
			view!.done += 1;
			return false;
		});
		view = view;
		await runMarketRequests(runnable, (group, signal) => strategyMarkets({ ...base, markets: group }, signal), {
			rows: (group, rows, warnings) => {
				if (!view) return;
				group.forEach((cell, i) => (view!.rows[marketKey(cell.symbol, cell.timeframe)] = rows[i]));
				view.done += group.length;
				view.warnings = [...new Set([...view.warnings, ...warnings])];
				view = view;
			},
			failed: (group, message) => {
				if (!view) return;
				for (const cell of group) view.rows[marketKey(cell.symbol, cell.timeframe)] = { ...cell, status: 'error', message };
				view.done += group.length;
				view = view;
			},
		}, own.signal);
		if (controller !== own || !view) return;
		view.status = own.signal.aborted ? 'cancelled' : 'done';
		view = view;
		controller = null;
	}
</script>

<MarketGridPanel {view} {availability} {symbolOptions} {currentSymbol} {currentTimeframe} {stale} canRun={!!request.strategy_id}
	note="Each market is this strategy's Gauntlet backtest (the parameters, window and execution settings on the Gauntlet tab) on that market, split in-sample / out-of-sample. Every market runs in its own worker, as optimizer runs do. A market the strategy cannot run on (a declared timeframe, a missing data feed) shows why. Markets without local data are not downloaded; collect them on the Data page. Nothing is saved: click a market to use it on the Gauntlet tab."
	on:run={(e) => run(e.detail)} on:cancel={() => controller?.abort()} on:pick={(e) => dispatch('pick', e.detail)} />
