<script lang="ts">
	import WalletsManager from '$lib/components/settings/WalletsManager.svelte';

	export let settings: Record<string, unknown>;

	// The network fund moves hit: the backend's resolved network when reported,
	// else the saved testnet toggle.
	$: onMainnet = (() => {
		const report = (settings?.risk_effective ?? null) as { on_mainnet?: boolean } | null;
		if (report && typeof report.on_mainnet === 'boolean') return report.on_mainnet;
		return settings?.hyperliquid_testnet === false;
	})();
</script>

<WalletsManager {onMainnet} />
