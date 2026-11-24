<script lang="ts">
	import { providerLabel } from '$lib/utils/agentsHub/agents';

	export let tradingMode: 'paper' | 'hyperliquid' = 'paper';
	export let hyperliquidConfigured = false;
	export let hyperliquidTestnet = true;
	export let connectedProviderIds: string[] = [];
	export let datasetCount = 0;
	export let activeDownloadJobs = 0;
	export let starterDataStarted = false;
	export let finishing = false;
	export let onJumpToStep: (index: number) => void;
	export let onLaunchRoute: (href: string) => void;

	$: aiReady = connectedProviderIds.length > 0;
	$: dataReady = datasetCount > 0 || activeDownloadJobs > 0 || starterDataStarted;

	$: connectedNames = connectedProviderIds.map((id) => providerLabel(id)).join(', ');

	const LAUNCH_ACTIONS = [
		{
			href: '/lab',
			title: 'Open The Forge',
			badge: 'Recommended first stop',
			description: 'Submit a trading idea or let AI agents research, code, and test strategies for you.',
		},
		{
			href: '/backtest/new',
			title: 'Run a Manual Backtest',
			badge: 'Quick test',
			description: 'Pick a built-in strategy and run it against historical candles in seconds.',
		},
		{
			href: '/',
			title: 'Go to Dashboard',
			badge: 'Overview',
			description: 'See your workspace overview, system status, and paper trading portfolio.',
		},
	];
</script>

<div class="space-y-6" data-testid="wizard-step-ready">
	<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.06] p-4 flex items-start gap-3">
		<span
			class="mt-0.5 w-6 h-6 rounded-full bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-xs flex-shrink-0"
			aria-hidden="true"
		>
			✓
		</span>
		<div class="space-y-1">
			<h3 class="text-[14px] font-semibold text-sc-ink">
				{aiReady ? 'Your Forven workspace is ready!' : 'Your workspace is set up for exploring'}
			</h3>
			<p class="text-xs text-sc-ink2 leading-relaxed">
				{#if aiReady}
					Everything essential is in place. Your agents will automatically use {connectedNames}, and you’re safely protected from accidental real-money orders.
				{:else}
					You can browse, backtest built-in strategies, and paper-trade right away. Connect an AI provider whenever you want agents to brainstorm ideas or use the chat assistant.
				{/if}
			</p>
		</div>
	</div>

	<section aria-labelledby="ready-summary-heading" class="space-y-2">
		<h3 id="ready-summary-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
			Setup summary
		</h3>
		<div class="rounded-lg border border-sc-line divide-y divide-sc-line bg-sc-bg/70">
			<!-- Trading mode -->
			<div class="px-4 py-3 flex items-center justify-between gap-3">
				<div class="flex items-center gap-3 min-w-0">
					<span class="w-5 h-5 rounded-full bg-emerald-500/15 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-[11px] flex-shrink-0" aria-hidden="true">✓</span>
					<div class="min-w-0">
						<p class="text-[12.5px] font-medium text-sc-ink">Trading mode</p>
						<p class="text-[11.5px] text-sc-ink3 truncate">
							{#if hyperliquidConfigured && tradingMode === 'hyperliquid'}
								Hyperliquid connected ({hyperliquidTestnet ? 'Testnet · play money' : 'Mainnet'})
							{:else if hyperliquidConfigured}
								Paper Trading selected (Hyperliquid keys also saved)
							{:else}
								Paper Trading — simulated $10,000 sandboxes, zero real funds at risk
							{/if}
						</p>
					</div>
				</div>
				<button
					type="button"
					class="text-[11.5px] text-sc-ink3 hover:text-sc-ink flex-shrink-0"
					on:click={() => onJumpToStep(1)}
				>
					Edit
				</button>
			</div>

			<!-- AI Brain -->
			<div class="px-4 py-3 flex items-center justify-between gap-3">
				<div class="flex items-center gap-3 min-w-0">
					{#if aiReady}
						<span class="w-5 h-5 rounded-full bg-emerald-500/15 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-[11px] flex-shrink-0" aria-hidden="true">✓</span>
					{:else}
						<span class="w-5 h-5 rounded-full bg-amber-400/10 border border-amber-400/40 text-amber-300 flex items-center justify-center text-[11px] flex-shrink-0" aria-hidden="true">·</span>
					{/if}
					<div class="min-w-0">
						<p class="text-[12.5px] font-medium text-sc-ink">AI Brain</p>
						<p class="text-[11.5px] text-sc-ink3 truncate">
							{#if aiReady}
								Connected: {connectedNames}
							{:else}
								Not connected yet — needed for AI strategy creation &amp; chat
							{/if}
						</p>
					</div>
				</div>
				<button
					type="button"
					class="text-[11.5px] {aiReady ? 'text-sc-ink3 hover:text-sc-ink' : 'text-amber-300 hover:text-amber-200 font-medium'} flex-shrink-0"
					on:click={() => onJumpToStep(2)}
				>
					{aiReady ? 'Edit' : 'Connect now'}
				</button>
			</div>

			<!-- Market Data -->
			<div class="px-4 py-3 flex items-center justify-between gap-3">
				<div class="flex items-center gap-3 min-w-0">
					{#if dataReady}
						<span class="w-5 h-5 rounded-full bg-emerald-500/15 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-[11px] flex-shrink-0" aria-hidden="true">✓</span>
					{:else}
						<span class="w-5 h-5 rounded-full border border-sc-line2 text-sc-ink3 flex items-center justify-center text-[11px] flex-shrink-0" aria-hidden="true">○</span>
					{/if}
					<div class="min-w-0">
						<p class="text-[12.5px] font-medium text-sc-ink">Market data</p>
						<p class="text-[11.5px] text-sc-ink3 truncate">
							{#if activeDownloadJobs > 0}
								Starter download running in background ({activeDownloadJobs} active)
							{:else if datasetCount > 0}
								{datasetCount} local price series ready
							{:else if starterDataStarted}
								Starter download queued in background
							{:else}
								None downloaded yet — you can fetch free candles anytime on the Data page
							{/if}
						</p>
					</div>
				</div>
				<button
					type="button"
					class="text-[11.5px] text-sc-ink3 hover:text-sc-ink flex-shrink-0"
					on:click={() => onJumpToStep(3)}
				>
					{dataReady ? 'Review' : 'Download'}
				</button>
			</div>
		</div>
	</section>

	<section aria-labelledby="ready-launchpad-heading" class="space-y-2.5">
		<h3 id="ready-launchpad-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
			Where would you like to start?
		</h3>
		<div class="grid grid-cols-1 md:grid-cols-3 gap-2.5">
			{#each LAUNCH_ACTIONS as action (action.href)}
				<button
					type="button"
					disabled={finishing}
					data-testid="wizard-launch-{action.href === '/' ? 'dashboard' : action.href.slice(1).replace('/', '-')}"
					class="rounded-lg border border-sc-line bg-sc-bg/70 hover:border-sc-ink/50 hover:bg-sc-panel2 p-3.5 text-left transition-all flex flex-col justify-between gap-2 focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3 disabled:opacity-50"
					on:click={() => onLaunchRoute(action.href)}
				>
					<div class="space-y-1">
						<span class="inline-block font-plex-cond text-[10px] uppercase tracking-[0.08em] px-1.5 py-0.5 rounded border border-sc-line2 text-sc-ink3">
							{action.badge}
						</span>
						<p class="text-[13px] font-semibold text-sc-ink pt-0.5">{action.title} →</p>
						<p class="text-[11.5px] text-sc-ink2 leading-relaxed">{action.description}</p>
					</div>
				</button>
			{/each}
		</div>
	</section>

	<p class="text-[11.5px] text-sc-ink3">
		Want Discord notifications, custom risk limits, or assistant profile preferences later? You’ll find them anytime under <span class="text-sc-ink2">Settings</span>.
	</p>
</div>
