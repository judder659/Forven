<script lang="ts">
	import TradingDesk from '$lib/components/trading/desk/TradingDesk.svelte';
	import type { ForvenDashboardResponse } from '$lib/api';

	export let data: { dashboard: ForvenDashboardResponse | null };
</script>

<svelte:head>
	<title>Paper Trades | Forven</title>
	<meta name="description" content="Paper trading desk: simulated positions, fills, funding and open interest, refusals and results against the backtest. Simulated fills only." />
</svelte:head>

<TradingDesk mode="paper" dashboard={data.dashboard} />
