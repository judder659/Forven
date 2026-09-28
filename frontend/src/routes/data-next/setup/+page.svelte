<script lang="ts">
	// Setup: start from a preset. Each shows what it includes and the server's
	// estimate (only bars not already stored count), and one click queues it.
	import { onMount } from 'svelte';
	import { estimateDownloads, startDownloads } from '$lib/api/dataManager';
	import type { DataStream, DownloadEstimateResponse, DownloadRequestItem } from '$lib/api/dataManagerTypes';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { runAction } from '$lib/components/data-manager/actions';
	import { formatBytes, formatCount, formatDuration, plural, streamLabel } from '$lib/components/data-manager/format';
	import { DM } from '$lib/components/data-manager/links';
	import { loading, settle, slaCensus, type Loadable } from '$lib/stores/dataManager';

	/** Binance USD-M perps in rough order of typical liquidity. */
	const TOP = [
		'BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'ADA', 'AVAX', 'LINK', 'SUI', 'TRX', 'LTC', 'BCH', 'DOT', 'NEAR', 'HYPE', 'UNI', 'AAVE', 'APT', 'ARB',
		'OP', 'ENA', 'WLD', '1000PEPE', 'INJ', 'TIA', 'SEI', 'FIL', 'ATOM', 'ETC', 'XLM', 'HBAR', 'ONDO', 'TAO', 'RUNE', 'STX', 'ICP', 'JUP', 'WIF', 'FET',
		'RENDER', 'POL', 'ALGO', '1000BONK', 'ORDI', 'PENDLE', 'CRV', 'LDO', 'TON', 'GALA',
	].map((base) => `${base}-USDT`);

	interface Preset {
		id: string;
		name: string;
		summary: string;
		lines: string[];
		items: DownloadRequestItem[];
	}

	function items(symbols: string[], timeframes: string[], streams: DataStream[] = []): DownloadRequestItem[] {
		return symbols.flatMap((symbol) =>
			timeframes.map((timeframe) => ({
				symbol,
				timeframe,
				venue: 'canonical',
				history: { mode: 'all' as const },
				...(streams.length && timeframe === '1h' ? { streams } : {}),
			})),
		);
	}

	const PRESETS: Preset[] = [
		{
			id: 'starter', name: 'Starter',
			summary: 'Enough to build and backtest your first strategies.',
			lines: ['BTC, ETH, SOL perps', '1h · 4h · 1d candles, all history', 'Funding and open interest'],
			items: items(TOP.slice(0, 3), ['1h', '4h', '1d'], ['funding', 'oi']),
		},
		{
			id: 'research', name: 'Research',
			summary: 'A research universe for scanning ideas across markets.',
			lines: ['Top 25 perps · 1h · 4h · 1d, all history', 'Top 10 also 15m and 5m', 'Funding and open interest'],
			items: [...items(TOP.slice(0, 25), ['1h', '4h', '1d'], ['funding', 'oi']), ...items(TOP.slice(0, 10), ['15m', '5m'])],
		},
		{
			id: 'pro', name: 'Pro',
			summary: 'Intraday depth for fast strategies. Large: check the disk estimate.',
			lines: ['Top 50 perps · 1h · 4h · 1d, all history', 'Top 20 also 15m and 5m, top 10 also 1m', 'Funding, open interest and basis'],
			items: [
				...items(TOP.slice(0, 50), ['1h', '4h', '1d'], ['funding', 'oi', 'basis']),
				...items(TOP.slice(0, 20), ['15m', '5m']),
				...items(TOP.slice(0, 10), ['1m']),
			],
		},
	];

	let estimates: Record<string, Loadable<DownloadEstimateResponse>> = Object.fromEntries(PRESETS.map((p) => [p.id, loading<DownloadEstimateResponse>()]));
	let starting = '';
	let started: Record<string, number> = {};

	async function estimate(preset: Preset) {
		estimates = { ...estimates, [preset.id]: loading() };
		const next = await settle(estimateDownloads(preset.items));
		estimates = { ...estimates, [preset.id]: next };
	}
	onMount(() => void Promise.all(PRESETS.map(estimate)));

	async function start(preset: Preset) {
		starting = preset.id;
		const e = estimates[preset.id]?.data;
		const runnable = e ? e.estimates.filter((x) => !x.blocked && x.new_bars_estimate > 0).map((x) => x.item) : preset.items;
		const result = await runAction(`Setting up ${preset.name}`, () => startDownloads(runnable), {
			success: (r) => `Queued ${plural(r.jobs.length, 'download')} for ${preset.name}. Progress is in Jobs; you can leave this page.`,
		});
		starting = '';
		if (result) started = { ...started, [preset.id]: result.jobs.length };
	}

	$: stored = $slaCensus.data?.total ?? 0;
	const streamsOf = (preset: Preset) => [...new Set(preset.items.flatMap((i) => i.streams ?? []))];
</script>

<svelte:head><title>Data · Setup | Forven</title></svelte:head>

<div class="mx-auto max-w-5xl space-y-3 p-4 pb-24">
	<div>
		<h1 class="text-[11px] font-bold uppercase tracking-[0.2em] text-white">Set up data</h1>
		<p class="mt-1 max-w-2xl text-[12px] leading-relaxed text-[#888]">
			Backtests, the gauntlet and paper trading read stored market data. Pick a preset: it downloads in the background, you can keep working,
			and the collector keeps it current afterwards. {#if stored}You already have {formatCount(stored)} series; estimates count only bars that are not stored yet.{/if}
		</p>
	</div>

	<div class="grid gap-2 lg:grid-cols-3">
		{#each PRESETS as preset (preset.id)}
			{@const est = estimates[preset.id]}
			<section class="flex flex-col border border-[#222] bg-[#050505]" aria-labelledby="dm-preset-{preset.id}" data-testid="preset-{preset.id}">
				<header class="border-b border-[#141414] px-3 py-2">
					<h2 id="dm-preset-{preset.id}" class="text-[13px] font-bold text-white">{preset.name}</h2>
					<p class="text-[11px] text-[#888]">{preset.summary}</p>
				</header>
				<ul class="space-y-0.5 px-3 py-2 text-[11px] text-[#ccc]">
					{#each preset.lines as line}<li>· {line}</li>{/each}
				</ul>
				<p class="px-3 text-[10px] text-[#666]">{plural(preset.items.length, 'series', 'series')} of candles{#if streamsOf(preset).length}, plus {streamsOf(preset).map((s) => streamLabel(s).toLowerCase()).join(', ')}{/if}</p>
				<div class="mt-auto border-t border-[#141414] px-3 py-2">
					{#if est.status === 'ready' && est.data}
						{@const e = est.data}
						{@const blocked = e.estimates.filter((x) => x.blocked).length}
						{@const nothing = e.estimates.every((x) => x.blocked || x.new_bars_estimate === 0)}
						<div class="flex items-baseline justify-between text-[11px]">
							<span class="text-[#666]">Download</span>
							<span class="font-mono tabular-nums text-white">{formatBytes(e.total_bytes)} · about {formatDuration(e.total_seconds)}</span>
						</div>
						<div class="flex items-baseline justify-between text-[10px]">
							<span class="text-[#666]">Disk free</span>
							<span class="font-mono tabular-nums {e.total_bytes > e.disk_free_bytes * 0.5 ? 'text-amber-400' : 'text-[#888]'}">{formatBytes(e.disk_free_bytes)}</span>
						</div>
						{#if blocked}<p class="mt-1 text-[10px] text-[#888]">{plural(blocked, 'series', 'series')} cannot be downloaded (not listed) and will be skipped.</p>{/if}
						{#each [...new Set([...e.warnings, ...e.estimates.flatMap((x) => x.warnings)])].slice(0, 3) as warning}<p class="mt-1 text-[10px] text-amber-400">{warning}</p>{/each}
						{#if started[preset.id] != null}
							<p class="mt-2 text-[11px] text-emerald-400">Queued {plural(started[preset.id], 'download')}. <a href="{DM}/jobs" class="underline">See progress</a></p>
						{:else if nothing}
							<p class="mt-2 text-[11px] text-emerald-400">Everything in this preset is already stored.</p>
						{:else}
							<button type="button" class="{preset.id === 'starter' ? 'terminal-button-primary' : 'terminal-button'} mt-2 w-full text-[10px]" disabled={!!starting} on:click={() => start(preset)}>
								{starting === preset.id ? 'Queueing…' : `Set up ${preset.name}`}</button>
						{/if}
					{:else if est.status === 'loading'}
						<p class="text-[11px] text-[#666]" aria-busy="true">Estimating size and time…</p>
					{:else}
						<SectionState state={est} what="The estimate" endpoint="POST /api/data/acquire/estimate" rows={1} on:retry={() => estimate(preset)} />
					{/if}
				</div>
			</section>
		{/each}
	</div>

	<p class="text-[11px] text-[#666]">Want something specific? <a href="{DM}/get" class="text-white underline">Get exactly the markets you need</a>, or <a href="{DM}/import" class="text-white underline">import a file</a>.</p>
</div>
