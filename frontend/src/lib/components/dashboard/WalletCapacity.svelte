<script lang="ts">
	/**
	 * How much room each live wallet and asset has left. A full wallet refuses new
	 * entries first-come-first-served, so this is where blocked entries come from.
	 */
	import type { ForvenRiskStatus } from '$lib/api';
	import { finite, formatUsd, meterTone } from '$lib/utils/liveDashboard';

	export let budget: ForvenRiskStatus['portfolio_budget_live'] | null = null;

	$: wallets = Object.entries(budget?.per_book ?? {}).map(([label, book]) => {
		const used = finite(book.gross_notional_usd) ?? 0;
		const limit = finite(book.limit_usd);
		return { label, used, limit, fraction: limit ? used / limit : null, positions: finite(book.positions) ?? 0 };
	});
	$: assets = Object.entries(budget?.per_asset ?? {}).map(([asset, exposure]) => {
		const net = finite(exposure.net_notional_usd) ?? 0;
		const limit = finite(exposure.limit_usd);
		return { asset, net, limit, fraction: limit ? Math.abs(net) / limit : null };
	});

	function width(fraction: number | null): string {
		return `${Math.min(100, Math.max(0, (fraction ?? 0) * 100)).toFixed(1)}%`;
	}
</script>

<div class="border border-[#222] bg-[#050505]" data-testid="wallet-capacity">
	<div class="flex items-center justify-between border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400">Wallet capacity &amp; exposure</h2>
		<a href="/risk" class="text-[10px] uppercase tracking-wider text-gray-500 hover:text-white">Risk →</a>
	</div>
	<div class="grid gap-x-6 gap-y-3 px-3 py-2 md:grid-cols-2">
		<div class="space-y-2">
			<div class="text-[10px] uppercase tracking-wider text-gray-500" title="Open notional against each wallet's own equity">Wallets</div>
			{#each wallets as wallet (wallet.label)}
				<div>
					<div class="flex justify-between text-[11px]">
						<span class="text-gray-300">{wallet.label}</span>
						<span class="font-mono text-gray-400">
							{formatUsd(wallet.used)} / {formatUsd(wallet.limit)}
							{#if wallet.fraction !== null}<span class="text-gray-500">({(wallet.fraction * 100).toFixed(0)}%)</span>{/if}
						</span>
					</div>
					<div class="mt-1 h-1 bg-[#1a1a1a]">
						<div class="h-1 {meterTone(wallet.fraction)}" style="width: {width(wallet.fraction)}"></div>
					</div>
				</div>
			{:else}
				<div class="text-xs text-gray-500">No live wallets reported.</div>
			{/each}
		</div>
		<div class="space-y-2">
			<div class="text-[10px] uppercase tracking-wider text-gray-500" title="Net notional per asset against the per-asset limit">Assets</div>
			{#each assets as exposure (exposure.asset)}
				<div>
					<div class="flex justify-between text-[11px]">
						<span class="text-gray-300">{exposure.asset} <span class="text-gray-500">{exposure.net >= 0 ? 'net long' : 'net short'}</span></span>
						<span class="font-mono text-gray-400">{formatUsd(Math.abs(exposure.net))} / {formatUsd(exposure.limit)}</span>
					</div>
					<div class="mt-1 h-1 bg-[#1a1a1a]">
						<div class="h-1 {meterTone(exposure.fraction)}" style="width: {width(exposure.fraction)}"></div>
					</div>
				</div>
			{:else}
				<div class="text-xs text-gray-500">No open live exposure.</div>
			{/each}
		</div>
	</div>
</div>
