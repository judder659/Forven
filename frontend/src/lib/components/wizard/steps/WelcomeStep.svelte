<script lang="ts">
	const PIPELINE_STAGES = [
		{
			num: '01',
			title: 'Research',
			summary: 'AI agents brainstorm ideas and write Python strategy code for you.',
			badge: 'Safe',
		},
		{
			num: '02',
			title: 'Backtest & Gauntlet',
			summary: 'Historical stress tests check whether an idea has a real edge or just luck.',
			badge: 'Historical',
		},
		{
			num: '03',
			title: 'Paper Trading',
			summary: 'Passing strategies trade live markets in a $10,000 simulated sandbox.',
			badge: 'Play money',
		},
		{
			num: '04',
			title: 'Live (Opt-in)',
			summary: 'Real orders only happen if you connect Hyperliquid, arm Mainnet, and type GO LIVE.',
			badge: 'Locked by default',
		},
	];

	const CHECKLIST = [
		{
			title: 'Pick your trading mode',
			detail: 'Start risk-free with Paper Trading (no exchange keys needed), or connect a Hyperliquid API wallet.',
		},
		{
			title: 'Connect one AI provider',
			detail: 'Link OpenAI, Anthropic, Claude Code, Gemini, OpenRouter, or a local model so agents & chat can run.',
		},
		{
			title: 'Check starter market data',
			detail: 'Download free historical candles (BTC, ETH, SOL) in the background so backtests work right away.',
		},
	];
</script>

<div class="space-y-6" data-testid="wizard-step-welcome">
	<div class="rounded-lg border border-emerald-500/25 bg-emerald-500/[0.05] px-4 py-3.5 flex items-start gap-3">
		<span
			class="mt-0.5 w-5 h-5 rounded-full bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-xs flex-shrink-0"
			aria-hidden="true"
		>
			✓
		</span>
		<div class="space-y-1">
			<p class="text-[13px] font-medium text-sc-ink">
				You’re in a safe sandbox by default.
			</p>
			<p class="text-xs text-sc-ink2 leading-relaxed">
				Forven never touches real funds unless you connect an exchange key, switch off Testnet, and explicitly arm live trading. Take your time — this guide takes about 2 minutes.
			</p>
		</div>
	</div>

	<section aria-labelledby="welcome-pipeline-heading" class="space-y-2.5">
		<div class="flex items-baseline justify-between gap-2">
			<h3 id="welcome-pipeline-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
				How Forven takes an idea from scratch to trading
			</h3>
		</div>
		<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
			{#each PIPELINE_STAGES as stage (stage.num)}
				<div class="rounded-md border border-sc-line bg-sc-bg/80 p-3 flex flex-col justify-between gap-2">
					<div class="space-y-1">
						<div class="flex items-center justify-between gap-2">
							<span class="font-plex-mono text-[11px] text-sc-ink3">{stage.num}</span>
							<span class="font-plex-cond text-[10px] uppercase tracking-[0.08em] px-1.5 py-0.5 rounded border border-sc-line2 text-sc-ink3">
								{stage.badge}
							</span>
						</div>
						<p class="text-[13px] font-semibold text-sc-ink">{stage.title}</p>
						<p class="text-[11.5px] text-sc-ink2 leading-relaxed">{stage.summary}</p>
					</div>
				</div>
			{/each}
		</div>
	</section>

	<section aria-labelledby="welcome-plan-heading" class="rounded-md border border-sc-line bg-sc-panel2/60 p-4 space-y-3">
		<h3 id="welcome-plan-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
			What we’ll set up together
		</h3>
		<ol class="space-y-2.5">
			{#each CHECKLIST as item, idx (item.title)}
				<li class="flex items-start gap-3">
					<span class="w-5 h-5 rounded-full border border-sc-line2 bg-sc-bg text-[11px] font-plex-mono text-sc-ink2 flex items-center justify-center flex-shrink-0 mt-0.5">
						{idx + 1}
					</span>
					<div class="min-w-0">
						<p class="text-[12.5px] font-medium text-sc-ink">{item.title}</p>
						<p class="text-[11.5px] text-sc-ink3 leading-relaxed">{item.detail}</p>
					</div>
				</li>
			{/each}
		</ol>
	</section>

	<p class="text-xs text-sc-ink3">
		Everything here is reversible. You can tweak any setting later or reopen this guide anytime from
		<span class="text-sc-ink2">Settings → Overview</span>.
	</p>
</div>
