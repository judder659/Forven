<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		listBots,
		listTemplates,
		startBot,
		stopBot,
		deleteBot,
		cloneBot,
		killAllBots,
		type BotConfig,
		type BotTemplate,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from '$lib/utils/realtime';

	let bots: BotConfig[] = [];
	let templates: BotTemplate[] = [];
	let loading = true;
	let error: string | null = null;
	let confirmDelete: string | null = null;
	let confirmStop: string | null = null;
	let confirmKillAll = false;
	let realtime: RealtimeRefreshController | null = null;

	// In-flight action guards: track which bots have a request in flight (by id)
	// and a global flag for Kill-All, so buttons can be disabled to prevent
	// double-click races.
	let busyBots = new Set<string>();
	let killAllBusy = false;

	function setBotBusy(id: string, busy: boolean) {
		// Reassign so Svelte tracks the change.
		const next = new Set(busyBots);
		if (busy) next.add(id);
		else next.delete(id);
		busyBots = next;
	}

	$: hasRunningBots = bots.some((b) => (b.runtime_status || b.status) === 'running');
	$: runningCount = bots.filter((b) => (b.runtime_status || b.status) === 'running').length;
	$: liveCount = bots.filter((b) => b.execution_mode === 'live').length;
	$: fleetPnl = bots.reduce((sum, b) => sum + (b.realized_pnl ?? 0), 0);
	$: openPositions = bots.reduce((sum, b) => sum + (b.open_positions ?? 0), 0);

	async function load() {
		try {
			[bots, templates] = await Promise.all([listBots(), listTemplates()]);
			error = null;
		} catch (e: any) {
			error = e.message || 'Failed to load bots';
		} finally {
			loading = false;
		}
	}

	async function handleStart(id: string) {
		if (busyBots.has(id)) return;
		setBotBusy(id, true);
		try {
			await startBot(id);
			addToast('Bot started', 'success');
			await load();
		} catch (e: any) {
			addToast(`Start failed: ${e.message}`, 'error');
		} finally {
			setBotBusy(id, false);
		}
	}

	async function handleStop(id: string) {
		if (busyBots.has(id)) return;
		confirmStop = null;
		setBotBusy(id, true);
		try {
			await stopBot(id);
			addToast('Bot stopped', 'success');
			await load();
		} catch (e: any) {
			addToast(`Stop failed: ${e.message}`, 'error');
		} finally {
			setBotBusy(id, false);
		}
	}

	async function handleDelete(id: string) {
		if (busyBots.has(id)) return;
		confirmDelete = null;
		setBotBusy(id, true);
		try {
			await deleteBot(id);
			addToast('Bot deleted', 'success');
			await load();
		} catch (e: any) {
			addToast(`Delete failed: ${e.message}`, 'error');
		} finally {
			setBotBusy(id, false);
		}
	}

	async function handleClone(id: string, name: string) {
		if (busyBots.has(id)) return;
		setBotBusy(id, true);
		try {
			await cloneBot(id, `${name} (copy)`);
			addToast('Bot cloned', 'success');
			await load();
		} catch (e: any) {
			addToast(`Clone failed: ${e.message}`, 'error');
		} finally {
			setBotBusy(id, false);
		}
	}

	async function handleKillAll() {
		if (killAllBusy) return;
		confirmKillAll = false;
		killAllBusy = true;
		try {
			const result = await killAllBots();
			addToast(`Stopped ${result.stopped} bot(s)`, 'success');
			await load();
		} catch (e: any) {
			addToast(`Kill all failed: ${e.message}`, 'error');
		} finally {
			killAllBusy = false;
		}
	}

	function createFromTemplate(templateId: string) {
		goto(`/bot-factory/editor?template=${templateId}`);
	}

	function statusOf(bot: BotConfig): string {
		return bot.runtime_status || bot.status || 'stopped';
	}

	function statusColor(s: string): string {
		if (s === 'running') return 'text-emerald-400';
		if (s === 'error') return 'text-red-500';
		if (s === 'paused') return 'text-yellow-400';
		return 'text-gray-600';
	}

	function statusDot(s: string): string {
		if (s === 'running') return 'bg-emerald-400 animate-pulse';
		if (s === 'error') return 'bg-red-500';
		if (s === 'paused') return 'bg-yellow-400';
		return 'bg-gray-700';
	}

	function fmtUsd(v: number): string {
		const sign = v < 0 ? '-' : '';
		return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	}

	onMount(() => {
		load();
		// WS-driven refresh (trade fills, task events) with polling only as an
		// offline fallback — same realtime pattern as the rest of the app.
		realtime = createRealtimeRefresh(load, {
			fallbackMs: 15_000,
			wsDebounceMs: 1000,
			wsEvents: ['trade', 'kill_switch_activated', 'kill_switch_cleared', 'risk_alert'],
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
	});
</script>

<svelte:head>
	<title>Bot Factory | Forven</title>
</svelte:head>

<div class="mx-auto max-w-7xl px-4 py-6">
	<!-- Header -->
	<div class="mb-4 flex items-end justify-between border-b border-[#222] pb-4">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">Bot Factory</h1>
			<p class="mt-1 text-xs text-[#666]">Autonomous LLM trading bots — paper by default, live behind GO LIVE</p>
		</div>
		<div class="flex gap-2">
			{#if hasRunningBots}
				{#if confirmKillAll}
					<button on:click={handleKillAll} disabled={killAllBusy} aria-busy={killAllBusy} class="terminal-button-danger text-xs">
						{killAllBusy ? 'Stopping…' : 'Confirm kill all'}
					</button>
					<button on:click={() => (confirmKillAll = false)} disabled={killAllBusy} class="terminal-button text-xs">Cancel</button>
				{:else}
					<button on:click={() => (confirmKillAll = true)} disabled={killAllBusy} class="terminal-button-danger text-xs">Kill All</button>
				{/if}
			{/if}
			<button on:click={() => goto('/bot-factory/editor')} class="terminal-button-primary text-xs">+ New Bot</button>
		</div>
	</div>

	<!-- Fleet summary strip -->
	{#if !loading && bots.length > 0}
		<div class="mb-6 grid grid-cols-2 gap-px border border-[#222] bg-[#222] sm:grid-cols-5">
			<div class="bg-[#050505] px-4 py-3">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Bots</div>
				<div class="mt-1 text-lg font-bold text-white">{bots.length}</div>
			</div>
			<div class="bg-[#050505] px-4 py-3">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Running</div>
				<div class="mt-1 text-lg font-bold {runningCount > 0 ? 'text-emerald-400' : 'text-white'}">{runningCount}</div>
			</div>
			<div class="bg-[#050505] px-4 py-3">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Live-armed</div>
				<div class="mt-1 text-lg font-bold {liveCount > 0 ? 'text-red-400' : 'text-white'}">{liveCount}</div>
			</div>
			<div class="bg-[#050505] px-4 py-3">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Open positions</div>
				<div class="mt-1 text-lg font-bold text-white">{openPositions}</div>
			</div>
			<div class="bg-[#050505] px-4 py-3">
				<div class="text-[10px] uppercase tracking-wider text-[#666]">Fleet P&L</div>
				<div class="mt-1 text-lg font-bold {fleetPnl > 0 ? 'text-emerald-400' : fleetPnl < 0 ? 'text-red-400' : 'text-white'}">
					{fleetPnl >= 0 ? '+' : ''}{fmtUsd(fleetPnl)}
				</div>
			</div>
		</div>
	{/if}

	<!-- Wallet management lives in Settings › HyperLiquid; live bots pick their
	     wallet in the GO LIVE drawer on the bot detail page. -->
	<div class="mb-6 flex items-center justify-between border border-[#222] bg-[#050505] px-4 py-2.5">
		<span class="text-[11px] text-[#666]">
			Live bots can route orders to an isolated Hyperliquid sub-account — create and fund wallets in Settings.
		</span>
		<a href="/settings#hyperliquid" class="terminal-button px-2.5 py-1 text-[10px]">Manage Wallets</a>
	</div>

	{#if loading}
		<div class="py-20 text-center text-xs uppercase tracking-widest text-[#555]">Loading…</div>
	{:else if error}
		<div class="py-20 text-center text-sm text-red-400">{error}</div>
	{:else if bots.length === 0}
		<!-- Empty state: template gallery -->
		<div class="py-10 text-center">
			<h2 class="text-sm font-bold uppercase tracking-widest text-white">Deploy your first bot</h2>
			<p class="mb-8 mt-1 text-xs text-[#666]">Start from a template or build from scratch</p>

			<div class="mx-auto grid max-w-4xl grid-cols-1 gap-3 text-left sm:grid-cols-2">
				{#each templates as template}
					<button on:click={() => createFromTemplate(template.id)} class="terminal-card group p-4 text-left transition-colors hover:border-[#555]">
						<div class="text-sm font-bold text-white group-hover:text-white">{template.name}</div>
						<div class="mt-1 text-xs leading-relaxed text-[#777]">{template.description}</div>
					</button>
				{/each}
				<button on:click={() => goto('/bot-factory/editor')} class="border border-dashed border-[#333] p-4 text-left transition-colors hover:border-[#666]">
					<div class="text-sm font-bold text-[#999]">Blank bot</div>
					<div class="mt-1 text-xs text-[#666]">Start from an empty configuration</div>
				</button>
			</div>
		</div>
	{:else}
		<!-- Bot roster -->
		<div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
			{#each bots as bot}
				{@const isBusy = busyBots.has(bot.id)}
				{@const status = statusOf(bot)}
				{@const pnl = bot.realized_pnl ?? 0}
				{@const isLive = bot.execution_mode === 'live'}
				{@const walletEquity = bot.live_wallet_equity ?? null}
				{@const equity = isLive ? (walletEquity ?? 0) : (bot.capital_allocation || 0) + pnl}
				<div class="terminal-card flex flex-col">
					<!-- Card header -->
					<div class="flex items-start justify-between border-b border-[#1a1a1a] px-4 py-3">
						<div class="min-w-0">
							<div class="flex items-center gap-2">
								<a href="/bot-factory/{bot.id}" class="truncate text-sm font-bold text-white hover:underline">{bot.name}</a>
								{#if bot.execution_mode === 'live'}
									<span class="border border-red-900 bg-red-500/10 px-1.5 py-px text-[9px] font-bold uppercase tracking-widest text-red-400">Live</span>
								{:else}
									<span class="border border-[#333] px-1.5 py-px text-[9px] font-bold uppercase tracking-widest text-[#888]">Paper</span>
								{/if}
							</div>
							<div class="mt-1 flex items-center gap-2 text-[11px] text-[#666]">
								<span class="truncate">{bot.model}</span>
								<span class="h-0.5 w-0.5 rounded-full bg-[#444]"></span>
								<span class="truncate">{bot.asset_mode === 'locked' ? (bot.locked_pairs || []).join(', ') : 'Free roam'}</span>
								{#if bot.live_wallet}
									<span class="h-0.5 w-0.5 rounded-full bg-[#444]"></span>
									<span class="truncate text-[#888]" title="Live orders route to this sub-account">⌂ {bot.live_wallet}</span>
								{/if}
							</div>
						</div>
						<div class="flex shrink-0 items-center gap-1.5 pl-3">
							<span class="inline-block h-1.5 w-1.5 rounded-full {statusDot(status)}"></span>
							<span class="text-[10px] uppercase tracking-wider {statusColor(status)}">{status}</span>
						</div>
					</div>

					<!-- Performance row -->
					<div class="grid grid-cols-3 gap-px bg-[#1a1a1a]">
						<div class="bg-[#050505] px-4 py-2.5">
							<div class="text-[9px] uppercase tracking-wider text-[#555]">{isLive ? 'Wallet' : 'Equity'}</div>
							{#if isLive && walletEquity == null}
								<div class="mt-0.5 text-sm font-bold text-[#666]" title="Live wallet balance unavailable — daemon snapshot pending or wallet unfunded">—</div>
							{:else}
								<div class="mt-0.5 text-sm font-bold text-white">{fmtUsd(equity)}</div>
							{/if}
						</div>
						<div class="bg-[#050505] px-4 py-2.5">
							<div class="text-[9px] uppercase tracking-wider text-[#555]">P&L</div>
							<div class="mt-0.5 text-sm font-bold {pnl > 0 ? 'text-emerald-400' : pnl < 0 ? 'text-red-400' : 'text-[#777]'}">
								{pnl >= 0 ? '+' : ''}{fmtUsd(pnl)}
							</div>
						</div>
						<div class="bg-[#050505] px-4 py-2.5">
							<div class="text-[9px] uppercase tracking-wider text-[#555]">Positions</div>
							<div class="mt-0.5 text-sm font-bold text-white">
								{bot.open_positions ?? 0}<span class="text-[#555]"> open</span>
							</div>
						</div>
					</div>

					<!-- Meta row -->
					<div class="flex items-center justify-between border-t border-[#1a1a1a] px-4 py-2 text-[10px] text-[#555]">
						<span>{bot.closed_trades ?? 0} closed trades</span>
						<span>LLM {bot.llm_calls_today ?? 0}/{bot.max_llm_calls_per_day}</span>
					</div>

					{#if bot.error_message && status !== 'running'}
						<div class="border-t border-red-900/40 bg-red-500/5 px-4 py-2 text-[11px] text-red-400">
							{bot.error_message}
						</div>
					{/if}

					<!-- Actions -->
					<div class="mt-auto flex gap-1.5 border-t border-[#1a1a1a] px-4 py-2.5 text-[10px]">
						{#if status === 'running'}
							{#if confirmStop === bot.id}
								<button on:click={() => handleStop(bot.id)} disabled={isBusy} aria-busy={isBusy} class="terminal-button-danger px-2 py-1 text-[10px]">{isBusy ? 'Stopping…' : 'Confirm'}</button>
								<button on:click={() => (confirmStop = null)} disabled={isBusy} class="terminal-button px-2 py-1 text-[10px]">Cancel</button>
							{:else}
								<button on:click={() => (confirmStop = bot.id)} disabled={isBusy} class="terminal-button-danger px-2 py-1 text-[10px]">Stop</button>
							{/if}
						{:else}
							<button on:click={() => handleStart(bot.id)} disabled={isBusy} aria-busy={isBusy} class="terminal-button px-2 py-1 text-[10px] hover:border-emerald-500 hover:bg-emerald-500 hover:text-black">{isBusy ? 'Starting…' : 'Start'}</button>
						{/if}
						<button on:click={() => goto(`/bot-factory/editor?id=${bot.id}`)} class="terminal-button px-2 py-1 text-[10px]">Edit</button>
						<button on:click={() => handleClone(bot.id, bot.name)} disabled={isBusy} aria-busy={isBusy} class="terminal-button px-2 py-1 text-[10px]">{isBusy ? '…' : 'Clone'}</button>
						<span class="ml-auto"></span>
						{#if confirmDelete === bot.id}
							<button on:click={() => handleDelete(bot.id)} disabled={isBusy} aria-busy={isBusy} class="terminal-button-danger px-2 py-1 text-[10px]">{isBusy ? 'Deleting…' : 'Confirm'}</button>
							<button on:click={() => (confirmDelete = null)} disabled={isBusy} class="terminal-button px-2 py-1 text-[10px]">Cancel</button>
						{:else}
							<button
								on:click={() => (confirmDelete = bot.id)}
								class="px-2 py-1 text-[10px] uppercase tracking-wide text-[#555] transition-colors hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-40"
								disabled={status === 'running' || isBusy}
							>Delete</button>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>
