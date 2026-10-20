<script lang="ts">
	const RULE_LABELS: Record<string, string> = { grid: 'Grid bot', dca: 'DCA bot', signal: 'Signal follower' };
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
		getBotFleet,
		applyRecommendedBotSettings,
		type BotConfig,
		type BotTemplate,
		type BotFleetRow,
		type BotHealthLevel,
	} from '$lib/api';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import { addToast } from '$lib/stores/processTracker';
	import {
		createRealtimeRefresh,
		type RealtimeRefreshController,
	} from '$lib/utils/realtime';

	let bots: BotConfig[] = [];
	let templates: BotTemplate[] = [];
	let fleet: Record<string, BotFleetRow> = {};
	let benchmarkSymbol = 'BTC/USDT';

	type SortKey = 'name' | 'status' | 'health' | 'return' | 'excess' | 'win' | 'open' | 'ai' | 'last';
	let sortKey: SortKey = 'status';
	let sortDir: 1 | -1 = 1;
	let filterText = '';
	let filterStatus: 'all' | 'running' | 'stopped' | 'paused' | 'error' | 'live' | 'paper' | 'attention' = 'all';
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
			const [botList, templateList, fleetData] = await Promise.all([
				listBots(),
				listTemplates(),
				// The cockpit metrics are extra: the roster still renders without them.
				getBotFleet().catch(() => null),
			]);
			bots = botList;
			templates = templateList;
			if (fleetData) {
				fleet = Object.fromEntries(fleetData.bots.map((row) => [row.bot_id, row]));
				benchmarkSymbol = fleetData.benchmark_symbol;
			}
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
			const res = await stopBot(id);
			const flattened = res.flattened ?? [];
			const closed = flattened.filter((f) => f.state === 'closed').length;
			const pending = flattened.filter((f) => f.state === 'pending').length;
			const failed = flattened.filter((f) => f.state === 'failed').length;
			if (failed > 0) {
				addToast(`Bot stopped — ${failed} live close(s) FAILED, check positions`, 'error', undefined, 12_000);
			} else if (closed || pending) {
				addToast(`Bot stopped — ${closed + pending} live position(s) closed`, 'success', undefined, 8000);
			} else {
				addToast('Bot stopped', 'success');
			}
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

	async function handleApplyRecommended(id: string) {
		if (busyBots.has(id)) return;
		setBotBusy(id, true);
		try {
			await applyRecommendedBotSettings(id);
			addToast('Bot now decides once per new candle', 'success');
			await load();
		} catch (e: any) {
			addToast(`Change failed: ${e.message}`, 'error');
		} finally {
			setBotBusy(id, false);
		}
	}

	const STATUS_ORDER: Record<string, number> = { error: 0, paused: 1, running: 2, stopped: 3 };
	const HEALTH_ORDER: Record<BotHealthLevel, number> = { bad: 0, warn: 1, ok: 2, idle: 3 };

	function healthOf(bot: BotConfig): BotHealthLevel {
		return fleet[bot.id]?.health.level ?? (bot.model_check && !bot.model_check.ok ? 'bad' : 'idle');
	}

	function sortValue(bot: BotConfig, key: SortKey): number | string {
		const row = fleet[bot.id];
		switch (key) {
			case 'name': return bot.name.toLowerCase();
			case 'status': return STATUS_ORDER[statusOf(bot)] ?? 9;
			case 'health': return HEALTH_ORDER[healthOf(bot)];
			case 'return': return row?.return_pct ?? -Infinity;
			case 'excess': return row?.excess_return_pct ?? -Infinity;
			case 'win': return row?.win_rate ?? -Infinity;
			case 'open': return row?.open_positions ?? bot.open_positions ?? 0;
			case 'ai': return row?.ai_cost_today_usd ?? bot.llm_cost_today ?? 0;
			case 'last': return row?.health.last_decision_at ? Date.parse(row.health.last_decision_at) : 0;
		}
	}

	function setSort(key: SortKey) {
		if (sortKey === key) sortDir = sortDir === 1 ? -1 : 1;
		else {
			sortKey = key;
			// Text and status read best ascending; numbers best-first.
			sortDir = key === 'name' || key === 'status' || key === 'health' ? 1 : -1;
		}
	}

	function matchesFilter(bot: BotConfig, text: string, status: typeof filterStatus): boolean {
		const q = text.trim().toLowerCase();
		if (q && !`${bot.name} ${bot.model} ${(bot.locked_pairs || []).join(' ')}`.toLowerCase().includes(q)) return false;
		if (status === 'all') return true;
		if (status === 'live') return bot.execution_mode === 'live';
		if (status === 'paper') return bot.execution_mode !== 'live';
		if (status === 'attention') return ['bad', 'warn'].includes(healthOf(bot));
		return statusOf(bot) === status;
	}

	$: visibleBots = bots
		.filter((b) => matchesFilter(b, filterText, filterStatus))
		.sort((a, b) => {
			const va = sortValue(a, sortKey);
			const vb = sortValue(b, sortKey);
			if (va < vb) return -sortDir;
			if (va > vb) return sortDir;
			return a.name.localeCompare(b.name);
		});
	$: attentionCount = bots.filter((b) => ['bad', 'warn'].includes(healthOf(b))).length;

	function healthDot(level: BotHealthLevel): string {
		if (level === 'bad') return 'bg-red-500';
		if (level === 'warn') return 'bg-yellow-400';
		if (level === 'ok') return 'bg-emerald-400';
		return 'bg-sc-line2';
	}

	function fmtPct(v: number | null | undefined, digits = 1): string {
		if (v == null || !Number.isFinite(v)) return '—';
		return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
	}

	function pctTone(v: number | null | undefined): string {
		if (v == null) return 'text-sc-ink3';
		return v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-sc-ink2';
	}

	function fmtAgo(iso: string | null | undefined): string {
		if (!iso) return 'never';
		const secs = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
		if (secs < 90) return `${Math.round(secs)}s ago`;
		if (secs < 5400) return `${Math.round(secs / 60)}m ago`;
		if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
		return `${Math.round(secs / 86400)}d ago`;
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
		return 'text-sc-ink3';
	}

	function statusDot(s: string): string {
		if (s === 'running') return 'bg-emerald-400 animate-pulse';
		if (s === 'error') return 'bg-red-500';
		if (s === 'paused') return 'bg-yellow-400';
		return 'bg-sc-line2';
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
	<div class="mb-4 flex items-end justify-between border-b border-sc-line pb-4">
		<div>
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Bot Factory</h1>
			<p class="mt-1 text-xs text-sc-ink3">AI and rule-based trading bots — paper by default, live behind GO LIVE</p>
		</div>
		<div class="flex gap-2">
			{#if hasRunningBots}
				{#if confirmKillAll}
					<button on:click={handleKillAll} disabled={killAllBusy} aria-busy={killAllBusy} class="terminal-button-danger text-[12px]">
						{killAllBusy ? 'Stopping…' : 'Confirm kill all'}
					</button>
					<button on:click={() => (confirmKillAll = false)} disabled={killAllBusy} class="terminal-button text-[12px]">Cancel</button>
				{:else}
					<button on:click={() => (confirmKillAll = true)} disabled={killAllBusy} class="terminal-button-danger text-[12px]">Kill All</button>
				{/if}
			{/if}
			<button on:click={() => goto('/bot-factory/editor')} class="terminal-button-primary text-[12px]">+ New Bot</button>
		</div>
	</div>

	<!-- Fleet summary strip -->
	{#if !loading && bots.length > 0}
		<div class="rounded-md mb-6 grid grid-cols-2 gap-px border border-sc-line bg-sc-raise sm:grid-cols-5">
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Bots</div>
				<div class="mt-1 text-lg font-bold text-sc-ink">{bots.length}</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Running</div>
				<div class="mt-1 text-lg font-bold {runningCount > 0 ? 'text-emerald-400' : 'text-sc-ink'}">{runningCount}</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Live-armed</div>
				<div class="mt-1 text-lg font-bold {liveCount > 0 ? 'text-red-400' : 'text-sc-ink'}">{liveCount}</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Open positions</div>
				<div class="mt-1 text-lg font-bold text-sc-ink">{openPositions}</div>
			</div>
			<div class="bg-sc-panel px-4 py-3">
				<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Fleet P&L</div>
				<div class="mt-1 text-lg font-bold {fleetPnl > 0 ? 'text-emerald-400' : fleetPnl < 0 ? 'text-red-400' : 'text-sc-ink'}">
					{fleetPnl >= 0 ? '+' : ''}{fmtUsd(fleetPnl)}
				</div>
			</div>
		</div>
	{/if}

	<!-- Wallet management lives in Settings › HyperLiquid; live bots pick their
	     wallet in the GO LIVE drawer on the bot detail page. -->
	<div class="rounded-md mb-6 flex items-center justify-between border border-sc-line bg-sc-panel px-4 py-2.5">
		<span class="text-[11px] text-sc-ink3">
			Live bots can route orders to an isolated Hyperliquid sub-account — create and fund wallets in Settings.
		</span>
		<a href="/settings#hyperliquid" class="terminal-button px-2.5 py-1 text-[10px]">Manage Wallets</a>
	</div>

	{#if loading}
		<div class="py-20 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading…</div>
	{:else if error}
		<div class="py-20 text-center text-sm text-red-400">{error}</div>
	{:else if bots.length === 0}
		<!-- Empty state: template gallery -->
		<div class="py-10 text-center">
			<h2 class="text-[14px] font-semibold text-sc-ink">Deploy your first bot</h2>
			<p class="mb-8 mt-1 text-xs text-sc-ink3">Start from a template or build from scratch</p>

			<div class="mx-auto grid max-w-4xl grid-cols-1 gap-3 text-left sm:grid-cols-2">
				{#each templates as template}
					<button on:click={() => createFromTemplate(template.id)} class="terminal-card group p-4 text-left transition-colors hover:border-sc-line2">
						<div class="text-sm font-bold text-sc-ink group-hover:text-sc-ink">{template.name}</div>
						<div class="mt-1 text-xs leading-relaxed text-sc-ink3">{template.description}</div>
					</button>
				{/each}
				<button on:click={() => goto('/bot-factory/editor')} class="rounded-md border border-dashed border-sc-line2 p-4 text-left transition-colors hover:border-sc-line2">
					<div class="text-sm font-bold text-sc-ink2">Blank bot</div>
					<div class="mt-1 text-xs text-sc-ink3">Start from an empty configuration</div>
				</button>
			</div>
		</div>
	{:else}
		<!-- Fleet table -->
		<div class="mb-3 flex flex-wrap items-center gap-2">
			<input
				bind:value={filterText}
				placeholder="Filter by name, model or coin"
				class="terminal-input w-full max-w-xs text-[12px]"
				aria-label="Filter bots"
			/>
			<select bind:value={filterStatus} class="terminal-input w-auto text-[12px]" aria-label="Show">
				<option value="all">All bots</option>
				<option value="attention">Needs attention{attentionCount ? ` (${attentionCount})` : ''}</option>
				<option value="running">Running</option>
				<option value="paused">Paused</option>
				<option value="error">Error</option>
				<option value="stopped">Stopped</option>
				<option value="paper">Paper</option>
				<option value="live">Live</option>
			</select>
			<span class="ml-auto text-[11px] text-sc-ink3">Return is since each bot first ran; "vs {benchmarkSymbol.split('/')[0]}" is against holding {benchmarkSymbol.split('/')[0]} over the same time.</span>
		</div>

		<div class="terminal-card overflow-x-auto">
			<table class="w-full min-w-[960px] text-left text-[12px]">
				<thead class="border-b border-sc-line font-plex-cond text-[11px] uppercase tracking-[0.08em] text-sc-ink3">
					<tr>
						{#each [
							['health', ''],
							['name', 'Bot'],
							['status', 'Status'],
							['return', 'Equity'],
							['excess', `vs ${benchmarkSymbol.split('/')[0]}`],
							['win', 'Win rate'],
							['open', 'Open'],
							['ai', 'AI today'],
							['last', 'Last decision'],
						] as [key, label]}
							<th class="px-3 py-2 font-medium">
								<button class="hover:text-sc-ink" on:click={() => setSort(key as SortKey)} title="Sort">
									{label || 'Health'}{#if sortKey === key}<span class="ml-0.5">{sortDir === 1 ? '▲' : '▼'}</span>{/if}
								</button>
							</th>
						{/each}
						<th class="px-3 py-2 text-right font-medium">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each visibleBots as bot (bot.id)}
						{@const isBusy = busyBots.has(bot.id)}
						{@const status = statusOf(bot)}
						{@const row = fleet[bot.id]}
						{@const health = row?.health}
						{@const isLive = bot.execution_mode === 'live'}
						{@const walletEquity = bot.live_wallet_equity ?? null}
						<tr class="border-b border-sc-line align-top last:border-b-0 hover:bg-sc-raise/40">
							<td class="px-3 py-2.5">
								<span
									class="mt-1 inline-block h-2 w-2 rounded-full {healthDot(healthOf(bot))}"
									title={health?.reasons.length ? health.reasons.join('\n') : healthOf(bot) === 'ok' ? 'Healthy' : 'Not running'}
								></span>
							</td>
							<td class="max-w-[260px] px-3 py-2.5">
								<div class="flex items-center gap-2">
									<a href="/bot-factory/{bot.id}" class="truncate font-bold text-sc-ink hover:underline">{bot.name}</a>
									{#if isLive}
										<span class="border border-red-900 bg-red-500/10 px-1.5 py-px font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] text-red-400">Live</span>
									{:else}
										<span class="border border-sc-line2 px-1.5 py-px font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Paper</span>
									{/if}
								</div>
								<div class="mt-0.5 text-[11px] text-sc-ink3">
									{#if bot.bot_type && bot.bot_type !== 'ai'}
										{RULE_LABELS[bot.bot_type] ?? bot.bot_type} · decides in code
									{:else}
										<span class={bot.model_check && !bot.model_check.ok ? 'text-red-400' : ''} title={bot.model_check?.reason ?? ''}>{bot.model}</span>
										· {bot.asset_mode === 'locked' ? (bot.locked_pairs || []).join(', ') : 'Free roam'}
										· {bot.timeframe ?? '1h'} · {bot.decision_cadence === 'candle_close' ? 'per candle' : `every ${bot.cooldown_seconds ?? 60}s`}
									{/if}
								</div>
								{#if (bot.bot_type ?? 'ai') === 'ai' && bot.decision_cadence !== 'candle_close'}
									<button
										on:click={() => handleApplyRecommended(bot.id)}
										disabled={isBusy}
										class="mt-1 text-[11px] text-sky-400 hover:underline disabled:opacity-50"
										title="Decide once per closed candle instead of on a timer; stops, targets and drawdown are still checked every minute"
									>Decide once per candle instead</button>
								{/if}
								{#if health && health.reasons.length && status !== 'running' && healthOf(bot) === 'bad'}
									<div class="mt-1 text-[11px] text-red-400">{health.reasons[0]} <a href="/bot-factory/{bot.id}" class="underline">Fix it</a></div>
								{:else if bot.error_message && status !== 'running'}
									<div class="mt-1 truncate text-[11px] text-red-400" title={bot.error_message}>{bot.error_message}</div>
								{/if}
							</td>
							<td class="px-3 py-2.5">
								<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {statusColor(status)}">{status}</span>
								{#if health && health.reasons.length && status === 'running'}
									<div class="mt-0.5 max-w-[160px] text-[11px] {healthOf(bot) === 'bad' ? 'text-red-400' : 'text-yellow-400'}">{health.reasons[0]}</div>
								{/if}
							</td>
							<td class="px-3 py-2.5">
								<div class="flex items-center gap-2">
									<Sparkline data={row?.sparkline ?? []} width={72} height={22} />
									<div>
										{#if isLive}
											<div class="font-bold text-sc-ink" title="Live wallet balance">{walletEquity == null ? '—' : fmtUsd(walletEquity)}</div>
										{:else}
											<div class="font-bold text-sc-ink">{fmtUsd(row?.equity ?? (bot.capital_allocation || 0) + (bot.realized_pnl ?? 0))}</div>
										{/if}
										<div class="text-[11px] {pctTone(row?.return_pct)}">{fmtPct(row?.return_pct)}</div>
									</div>
								</div>
							</td>
							<td class="px-3 py-2.5">
								<div class={pctTone(row?.excess_return_pct)}>{fmtPct(row?.excess_return_pct)}</div>
								<div class="text-[11px] text-sc-ink3" title="{benchmarkSymbol} buy-and-hold over the same time">{benchmarkSymbol.split('/')[0]} {fmtPct(row?.benchmark_return_pct)}</div>
							</td>
							<td class="px-3 py-2.5">
								<div class="text-sc-ink">{row?.win_rate != null ? `${(row.win_rate * 100).toFixed(0)}%` : '—'}</div>
								<div class="text-[11px] text-sc-ink3">{row?.closed_trades ?? bot.closed_trades ?? 0} trades</div>
							</td>
							<td class="px-3 py-2.5 text-sc-ink">{row?.open_positions ?? bot.open_positions ?? 0}</td>
							<td class="px-3 py-2.5">
								<div class="text-sc-ink">${(row?.ai_cost_today_usd ?? bot.llm_cost_today ?? 0).toFixed(2)}</div>
								<div class="text-[11px] text-sc-ink3" title="AI calls today of the daily cap">{bot.llm_calls_today ?? 0}/{bot.max_llm_calls_per_day} calls</div>
							</td>
							<td class="px-3 py-2.5">
								<div class="text-sc-ink2">{fmtAgo(health?.last_decision_at)}</div>
								{#if health?.last_action_type}
									<div class="text-[11px] text-sc-ink3">{health.last_action_type}</div>
								{/if}
							</td>
							<td class="px-3 py-2.5">
								<div class="flex justify-end gap-1.5">
									{#if status === 'running'}
										{#if confirmStop === bot.id}
											{@const closesLive = isLive && (bot.open_positions ?? 0) > 0}
											<button on:click={() => handleStop(bot.id)} disabled={isBusy} aria-busy={isBusy} title={closesLive ? `Closes ${bot.open_positions} live position(s) on Hyperliquid` : ''} class="terminal-button-danger px-2 py-1 text-[12px]">{isBusy ? 'Stopping…' : closesLive ? `Confirm + Close ${bot.open_positions}` : 'Confirm'}</button>
											<button on:click={() => (confirmStop = null)} disabled={isBusy} class="terminal-button px-2 py-1 text-[12px]">Cancel</button>
										{:else}
											<button on:click={() => (confirmStop = bot.id)} disabled={isBusy} class="terminal-button-danger px-2 py-1 text-[12px]">Stop</button>
										{/if}
									{:else}
										<button on:click={() => handleStart(bot.id)} disabled={isBusy} aria-busy={isBusy} class="terminal-button px-2 py-1 text-[12px] hover:border-emerald-500 hover:bg-emerald-500 hover:text-black">{isBusy ? 'Starting…' : 'Start'}</button>
									{/if}
									<button on:click={() => goto(`/bot-factory/editor?id=${bot.id}`)} class="terminal-button px-2 py-1 text-[12px]">Edit</button>
									<button on:click={() => handleClone(bot.id, bot.name)} disabled={isBusy} aria-busy={isBusy} class="terminal-button px-2 py-1 text-[12px]">{isBusy ? '…' : 'Clone'}</button>
									{#if confirmDelete === bot.id}
										<button on:click={() => handleDelete(bot.id)} disabled={isBusy} aria-busy={isBusy} class="terminal-button-danger px-2 py-1 text-[12px]">{isBusy ? 'Deleting…' : 'Confirm'}</button>
										<button on:click={() => (confirmDelete = null)} disabled={isBusy} class="terminal-button px-2 py-1 text-[12px]">Cancel</button>
									{:else}
										<button
											on:click={() => (confirmDelete = bot.id)}
											class="px-2 py-1 text-[12px] text-sc-ink3 transition-colors hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-40"
											disabled={status === 'running' || isBusy}
										>Delete</button>
									{/if}
								</div>
							</td>
						</tr>
					{:else}
						<tr><td colspan="10" class="px-3 py-8 text-center text-sc-ink3">No bots match this filter.</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
