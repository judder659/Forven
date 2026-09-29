<script lang="ts">
	import TradingDesk from '$lib/components/trading/desk/TradingDesk.svelte';
	import type { ForvenDashboardResponse } from '$lib/api';

	export let data: { dashboard: ForvenDashboardResponse | null };
</script>

<svelte:head>
	<title>Live Trades | Forven</title>
	<meta name="description" content="Live trading desk: real positions, orders, funding and open interest, refusals and results against the backtest. Actions here place REAL exchange orders." />
</svelte:head>

<TradingDesk mode="live" dashboard={data.dashboard} />
