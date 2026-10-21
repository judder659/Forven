<script lang="ts">
	// Read-only answer to "can this install trade real money right now?". Real
	// money needs both a mainnet network and the mainnet switch on this computer
	// (FORVEN_ALLOW_MAINNET); the old Trading mode field decides neither alone.
	import { effectiveRiskReport } from '$lib/settings/effective';

	export let settings: Record<string, unknown>;

	$: report = effectiveRiskReport(settings);
	$: onMainnet =
		typeof report?.on_mainnet === 'boolean' ? report.on_mainnet : settings?.hyperliquid_testnet === false;
	$: armed = Boolean(settings?.mainnet_armed);
	$: lockedCount = Object.keys(report?.locked ?? {}).length;
	$: state = onMainnet ? (armed ? 'live' : 'refused') : 'testnet';
</script>

<div class="space-y-3 pb-3" data-testid="real-money-status">
	<div class="grid grid-cols-1 gap-px overflow-hidden rounded-md border border-sc-line bg-sc-line sm:grid-cols-3">
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Network</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={onMainnet} class:text-sc-ink={!onMainnet} data-testid="real-money-network">
				{onMainnet ? 'Mainnet' : 'Testnet'}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">Set by Use testnet under Exchange.</div>
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Mainnet switch</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={armed} class:text-sc-ink={!armed} data-testid="real-money-armed">
				{armed ? 'On' : 'Off'}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">The FORVEN_ALLOW_MAINNET variable on this computer.</div>
		</div>
		<a href="/risk" class="block bg-sc-panel px-4 py-3 hover:bg-sc-panel2">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Kill switch</div>
			<div class="mt-1 text-lg font-bold text-sc-ink">Risk page →</div>
			<div class="mt-1 text-xs text-sc-ink3">Halt or resume all live trading.</div>
		</a>
	</div>
	<p class="text-sm" class:text-red-300={state === 'live'} class:text-sc-ink2={state !== 'live'} data-testid="real-money-verdict">
		{#if state === 'live'}
			Live strategies trade real money on Hyperliquid mainnet.
		{:else if state === 'refused'}
			Orders resolve to mainnet, but every one is refused until the mainnet switch is on.
		{:else}
			Orders go to Hyperliquid testnet with play money. Paper strategies never place orders.
		{/if}
	</p>
	{#if onMainnet}
		<div class="rounded-md border border-red-800/70 bg-red-950/20 px-4 py-3 text-xs text-red-200" data-testid="mainnet-risk-banner">
			On mainnet the engine locks some limits tighter for real money{lockedCount > 0 ? `, and ${lockedCount} saved value${lockedCount === 1 ? ' is' : 's are'} replaced` : ''}. Fields below show the value the engine actually uses.
		</div>
	{/if}
</div>
