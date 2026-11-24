<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { getCatalog, listJobs, startDownloads } from '$lib/api/dataManager';
	import type { CatalogRow, DataJob, DownloadRequestItem } from '$lib/api/dataManagerTypes';

	export let onStatusChange:
		| ((status: { datasetCount: number; activeJobs: number; starterStarted: boolean }) => void)
		| undefined = undefined;

	interface StarterCoin {
		symbol: string;
		display: string;
		name: string;
		defaultSelected: boolean;
	}

	const STARTER_COINS: StarterCoin[] = [
		{ symbol: 'BTC-USDT', display: 'BTC/USDT', name: 'Bitcoin', defaultSelected: true },
		{ symbol: 'ETH-USDT', display: 'ETH/USDT', name: 'Ethereum', defaultSelected: true },
		{ symbol: 'SOL-USDT', display: 'SOL/USDT', name: 'Solana', defaultSelected: true },
		{ symbol: 'AVAX-USDT', display: 'AVAX/USDT', name: 'Avalanche', defaultSelected: false },
		{ symbol: 'LINK-USDT', display: 'LINK/USDT', name: 'Chainlink', defaultSelected: false },
		{ symbol: 'XRP-USDT', display: 'XRP/USDT', name: 'XRP', defaultSelected: false },
	];

	const STARTER_TIMEFRAMES = ['1h', '4h'];
	const STARTER_DAYS = 730; // 2 years — enough for walk-forward and gauntlet checks

	let loading = true;
	let loadError: string | null = null;
	let rows: CatalogRow[] = [];
	let totalSeries = 0;
	let jobs: DataJob[] = [];
	let selectedSymbols = new Set<string>(
		STARTER_COINS.filter((c) => c.defaultSelected).map((c) => c.symbol),
	);
	let starting = false;
	let startError: string | null = null;
	let starterStarted = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	$: activeJobs = jobs.filter((j) => j.status === 'running' || j.status === 'queued');
	$: trackedJobs = jobs.slice(0, 6);

	$: existingBySymbol = (() => {
		const map = new Map<string, string[]>();
		for (const row of rows) {
			const label = row.display_symbol || row.symbol.replace('-', '/');
			const list = map.get(label) ?? [];
			if (!list.includes(row.timeframe)) list.push(row.timeframe);
			map.set(label, list);
		}
		return Array.from(map.entries()).slice(0, 8);
	})();

	$: existingSymbolSet = new Set(rows.map((r) => r.symbol.toUpperCase()));
	$: hasAllCoreStarters = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT'].every((s) => existingSymbolSet.has(s));

	function emitStatus(): void {
		onStatusChange?.({
			datasetCount: totalSeries,
			activeJobs: activeJobs.length,
			starterStarted,
		});
	}

	function stopPoll(): void {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	function ensurePolling(): void {
		if (pollTimer !== null) return;
		pollTimer = setInterval(() => {
			void refreshData(true);
		}, 3000);
	}

	async function refreshData(silent = false): Promise<void> {
		if (!silent) {
			loading = true;
			loadError = null;
		}
		try {
			const [catalog, jobList] = await Promise.all([
				getCatalog({ stream: ['ohlcv'], limit: 100 }),
				listJobs({ kind: ['download'], limit: 12 }),
			]);
			rows = catalog?.rows ?? [];
			totalSeries = typeof catalog?.total === 'number' ? catalog.total : rows.length;
			jobs = jobList?.jobs ?? [];
			emitStatus();
			if (jobs.some((j) => j.status === 'running' || j.status === 'queued')) {
				ensurePolling();
			} else {
				stopPoll();
			}
		} catch (err) {
			if (!silent) {
				loadError = err instanceof Error ? err.message : 'Could not check local market data right now.';
			}
		} finally {
			loading = false;
		}
	}

	function toggleCoin(symbol: string): void {
		const next = new Set(selectedSymbols);
		if (next.has(symbol)) {
			if (next.size === 1) return; // keep at least one selected
			next.delete(symbol);
		} else {
			next.add(symbol);
		}
		selectedSymbols = next;
	}

	async function downloadStarterPack(): Promise<void> {
		if (selectedSymbols.size === 0 || starting) return;
		starting = true;
		startError = null;
		try {
			const items: DownloadRequestItem[] = [];
			for (const symbol of selectedSymbols) {
				for (const timeframe of STARTER_TIMEFRAMES) {
					items.push({
						symbol,
						timeframe,
						venue: 'canonical',
						history: { mode: 'days', days: STARTER_DAYS },
					});
				}
			}
			const res = await startDownloads(items);
			const created = res?.jobs ?? [];
			if (created.length > 0) {
				const existingIds = new Set(created.map((j) => j.id));
				jobs = [...created, ...jobs.filter((j) => !existingIds.has(j.id))];
			}
			starterStarted = true;
			emitStatus();
			ensurePolling();
		} catch (err) {
			startError =
				err instanceof Error
					? err.message
					: 'Could not start the starter download. You can also download data anytime from the Data page.';
		} finally {
			starting = false;
		}
	}

	function jobStatusLabel(job: DataJob): string {
		if (job.status === 'succeeded') return 'Done';
		if (job.status === 'running') {
			if (job.progress?.total && job.progress.total > 0) {
				const pct = Math.min(100, Math.round((job.progress.done / job.progress.total) * 100));
				return `Downloading ${pct}%`;
			}
			return 'Downloading…';
		}
		if (job.status === 'queued') return 'Queued';
		if (job.status === 'failed') return 'Failed';
		return job.status;
	}

	onMount(() => {
		void refreshData(false);
	});

	onDestroy(stopPoll);
</script>

<div class="space-y-5" data-testid="wizard-step-data">
	<p class="text-xs text-sc-ink2 leading-relaxed">
		Backtests and strategy validation run on historical price candles stored locally on your machine.
		Market data is <span class="text-sc-ink font-medium">free and public</span> — no exchange account or API keys required.
	</p>

	{#if loading}
		<div class="rounded-lg border border-sc-line bg-sc-bg/60 p-5 space-y-2 animate-pulse">
			<div class="h-4 w-48 rounded bg-sc-raise"></div>
			<div class="h-3 w-72 rounded bg-sc-raise"></div>
		</div>
	{:else if loadError}
		<div class="rounded-lg border border-amber-500/30 bg-amber-500/[0.05] p-4 space-y-2">
			<p class="text-xs font-medium text-amber-300">Couldn’t check your local data catalog</p>
			<p class="text-xs text-sc-ink2">{loadError}</p>
			<p class="text-[11px] text-sc-ink3">No worries — you can continue setup and download data anytime on the Data page.</p>
		</div>
	{:else}
		{#if totalSeries > 0}
			<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.05] p-4 space-y-3" data-testid="data-existing-summary">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div class="flex items-center gap-2">
						<span class="w-5 h-5 rounded-full bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-xs" aria-hidden="true">✓</span>
						<h3 class="text-[13.5px] font-semibold text-sc-ink">
							{totalSeries} price series ready locally
						</h3>
					</div>
					<span class="text-xs text-emerald-300/90">Ready for backtesting</span>
				</div>
				<div class="flex flex-wrap gap-1.5">
					{#each existingBySymbol as [sym, tfs] (sym)}
						<span class="rounded border border-sc-line2 bg-sc-panel px-2 py-0.5 text-[11px] font-plex-mono text-sc-ink2">
							{sym} <span class="text-sc-ink3">· {tfs.join(', ')}</span>
						</span>
					{/each}
					{#if totalSeries > rows.length}
						<span class="rounded border border-sc-line bg-sc-bg px-2 py-0.5 text-[11px] text-sc-ink3">
							+{totalSeries - rows.length} more
						</span>
					{/if}
				</div>
			</div>
		{/if}

		{#if totalSeries === 0 || !hasAllCoreStarters}
			<section class="rounded-lg border border-sc-line bg-sc-bg/70 p-4 space-y-4" data-testid="data-starter-pack">
				<div class="space-y-1">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<h3 class="text-[13.5px] font-semibold text-sc-ink">
							{totalSeries === 0 ? 'Download a starter market data pack' : 'Add popular starter pairs (optional)'}
						</h3>
						<span class="font-plex-cond text-[10.5px] uppercase tracking-[0.08em] px-2 py-0.5 rounded border border-sc-line2 text-sc-ink3">
							1h &amp; 4h · 2 years · Free
						</span>
					</div>
					<p class="text-xs text-sc-ink2 leading-relaxed">
						Pick the coins you want to start researching. We’ll fetch 2 years of hourly &amp; 4-hour candles in the background so you can run backtests right away.
					</p>
				</div>

				<div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
					{#each STARTER_COINS as coin (coin.symbol)}
						{@const checked = selectedSymbols.has(coin.symbol)}
						{@const alreadyHave = existingSymbolSet.has(coin.symbol)}
						<button
							type="button"
							aria-pressed={checked}
							class="rounded-md border px-3 py-2 text-left transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3
								{checked
									? 'border-emerald-500/60 bg-emerald-500/[0.08] text-sc-ink'
									: 'border-sc-line bg-sc-panel text-sc-ink2 hover:border-sc-line2'}"
							on:click={() => toggleCoin(coin.symbol)}
						>
							<div class="flex items-center justify-between gap-1">
								<span class="font-plex-mono text-xs font-medium">{coin.display}</span>
								<span class="text-[11px] {checked ? 'text-emerald-400' : 'text-sc-ink3'}" aria-hidden="true">
									{checked ? '✓' : '+'}
								</span>
							</div>
							<span class="block text-[11px] text-sc-ink3 mt-0.5">
								{coin.name}{alreadyHave ? ' · in library' : ''}
							</span>
						</button>
					{/each}
				</div>

				{#if startError}
					<p class="text-xs text-red-400" role="alert">{startError}</p>
				{/if}

				<div class="flex flex-wrap items-center justify-between gap-3 pt-1">
					<button
						type="button"
						class="terminal-button-primary text-[12px]"
						disabled={starting || selectedSymbols.size === 0}
						data-testid="download-starter-btn"
						on:click={downloadStarterPack}
					>
						{#if starting}
							Starting downloads…
						{:else}
							Download starter data ({selectedSymbols.size * STARTER_TIMEFRAMES.length} series)
						{/if}
					</button>
					<span class="text-[11px] text-sc-ink3">
						Runs in the background — you can click Next right away.
					</span>
				</div>
			</section>
		{/if}

		{#if trackedJobs.length > 0 && (starterStarted || activeJobs.length > 0)}
			<section class="rounded-lg border border-sc-line bg-sc-panel2/60 p-4 space-y-2.5" data-testid="data-jobs-list">
				<div class="flex items-center justify-between gap-2">
					<h3 class="text-xs font-semibold text-sc-ink">
						{activeJobs.length > 0
							? `Background downloads running (${activeJobs.length} active)`
							: 'Starter downloads finished'}
					</h3>
					{#if activeJobs.length > 0}
						<span class="text-[11px] text-emerald-400">Safe to continue — won’t interrupt downloads</span>
					{/if}
				</div>
				<ul class="space-y-1.5">
					{#each trackedJobs as job (job.id)}
						<li class="flex items-center justify-between gap-2 text-xs rounded border border-sc-line bg-sc-bg/70 px-2.5 py-1.5">
							<span class="text-sc-ink2 truncate">{job.title}</span>
							<span
								class="font-plex-mono text-[11px] flex-shrink-0
									{job.status === 'succeeded'
										? 'text-emerald-400'
										: job.status === 'failed'
											? 'text-red-400'
											: 'text-sc-ink3'}"
							>
								{jobStatusLabel(job)}
							</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}
	{/if}

	<p class="text-[11.5px] text-sc-ink3">
		You can download more coins, funding rates, open interest, or import your own CSV files anytime on the <span class="text-sc-ink2">Data</span> page.
	</p>
</div>
