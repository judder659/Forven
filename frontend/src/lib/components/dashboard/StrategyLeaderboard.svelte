<script lang="ts">
	import { onMount } from 'svelte';
	import { getDashboardLeaderboard } from '$lib/api';
	import type { LeaderboardEntry, WinnerEntry } from '$lib/api';
	import { openStrategyDetail } from '$lib/utils/strategyDetail';
	import TierBadge from '$lib/components/TierBadge.svelte';
	import Sparkline from '$lib/components/Sparkline.svelte';

	export let winners: WinnerEntry[] = [];

	let entries: LeaderboardEntry[] = [];
	let filteredEntries: LeaderboardEntry[] = [];
	let sortBy = 'sharpe_ratio';
	let tierFilter: 'all' | 'elite' | 'strong' | 'marginal' = 'all';
	const tierTabs: Array<'all' | 'elite' | 'strong' | 'marginal'> = ['all', 'elite', 'strong', 'marginal'];
	let loading = true;
	let loadError = '';

	function isWinnerRow(entry: LeaderboardEntry): boolean {
		if (entry.id && winnerKeys.has(entry.id)) return true;
		if (entry.strategy_name && winnerKeys.has(entry.strategy_name)) return true;
		return false;
	}

	async function load() {
		loading = true;
		try {
			entries = await getDashboardLeaderboard({
				sort_by: sortBy,
				limit: 30,
				tier: tierFilter !== 'all' ? tierFilter : undefined,
			});
			loadError = '';
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load leaderboard';
		}
		loading = false;
	}

	onMount(() => { load(); });

	function toggleSort(col: string) {
		sortBy = col;
		load();
	}

	function setTierFilter(nextTier: 'all' | 'elite' | 'strong' | 'marginal') {
		tierFilter = nextTier;
		load();
	}

	$: filteredEntries = tierFilter === 'all'
		? entries
		: entries.filter((entry) => entry.tier === tierFilter);

	$: winnerKeys = new Set(
		winners
			.flatMap((w) => [w.id, w.strategy_name])
			.filter((v): v is string => typeof v === 'string' && v.length > 0)
	);

	function fmtNum(n: number | undefined, dec = 2): string {
		return n?.toFixed(dec) ?? '\u2014';
	}

	function normalizedAnnualReturn(entry: LeaderboardEntry): number {
		if (typeof entry.annualized_return_pct === 'number' && Number.isFinite(entry.annualized_return_pct)) {
			return entry.annualized_return_pct;
		}
		return entry.total_return ?? 0;
	}

	function sortArrow(col: string): string {
		return sortBy === col ? ' \u25BC' : '';
	}

	function displayName(name: unknown): string {
		return String(name ?? 'Unnamed Strategy').replace(/\[Scan:[^\]]+\]\s*/, '');
	}

	function getStrategyName(entry: LeaderboardEntry): string {
		const raw = entry as unknown as Record<string, unknown>;
		return String(raw.strategy_name ?? raw.name ?? entry.id ?? 'Unnamed Strategy');
	}

	// What the operator sees: the friendly display name when set, else the canonical
	// name. Navigation still resolves via getStrategyName so links never break.
	function getDisplayLabel(entry: LeaderboardEntry): string {
		const friendly = String(entry.display_name ?? '').trim();
		return friendly || getStrategyName(entry);
	}

	function toLifecycleSource(source: string | undefined): 'manual' | 'scan' | 'autopilot' | 'code' | 'campaign' {
		if (source === 'scan' || source === 'autopilot' || source === 'manual' || source === 'code' || source === 'campaign') {
			return source;
		}
		return 'scan';
	}

	async function handleStrategyNameClick(e: MouseEvent, entry: LeaderboardEntry) {
		e.stopPropagation();
		const strategyName = getStrategyName(entry);
		await openStrategyDetail({
			name: strategyName,
			source: toLifecycleSource(entry.source),
			sourceRef: entry.scan_id,
			symbol: entry.symbol,
			timeframe: entry.timeframe,
			returnTo: '/',
		});
	}

	function handleStrategyNameKeydown(e: KeyboardEvent, entry: LeaderboardEntry) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			handleStrategyNameClick(e as unknown as MouseEvent, entry);
		}
	}

	function handleRowKeydown(e: KeyboardEvent, entry: LeaderboardEntry) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			const strategyName = getStrategyName(entry);
			void openStrategyDetail({
				name: strategyName,
				source: toLifecycleSource(entry.source),
				sourceRef: entry.scan_id,
				symbol: entry.symbol,
				timeframe: entry.timeframe,
				returnTo: '/',
			});
		}
	}
</script>

<div class="flex flex-col h-full">
	<div class="text-[10px] text-gray-600 uppercase tracking-wider px-2 mb-1.5">Strategy Leaderboard</div>
	<div class="flex items-center gap-1 px-2 mb-1.5">
		{#each tierTabs as tier}
			<button
				type="button"
				class="border px-2 py-0.5 text-[10px] uppercase tracking-wide transition-colors {tierFilter === tier ? 'border-white bg-white text-black' : 'border-[#333] text-gray-500 hover:border-[#555] hover:text-white'}"
				on:click={() => setTierFilter(tier)}
			>
				{tier === 'all' ? 'All' : tier.charAt(0).toUpperCase() + tier.slice(1)}
			</button>
		{/each}
	</div>

	<div class="flex-1 overflow-auto border border-[#222]">
		<table class="w-full text-xs">
			<thead class="bg-[#0a0a0a] sticky top-0">
				<tr class="text-gray-600 text-left">
					<th class="px-2 py-1.5 cursor-pointer hover:text-white" on:click={() => toggleSort('strategy_name')}>Strategy</th>
					<th class="px-1 py-1 w-7"></th>
					<th class="px-1 py-1 w-[70px]"></th>
					<th class="px-2 py-1 text-center">Pair</th>
					<th class="px-2 py-1 text-right cursor-pointer hover:text-white" on:click={() => toggleSort('sharpe_ratio')}>Sharpe{sortArrow('sharpe_ratio')}</th>
					<th class="px-2 py-1 text-right cursor-pointer hover:text-white" on:click={() => toggleSort('annualized_return_pct')}>Ann%{sortArrow('annualized_return_pct')}</th>
					<th class="px-2 py-1 text-right cursor-pointer hover:text-white" on:click={() => toggleSort('win_rate')}>WR%{sortArrow('win_rate')}</th>
					<th class="px-2 py-1 text-right">Trades</th>
					<th class="px-2 py-1 text-center">Open</th>
				</tr>
			</thead>
			<tbody>
				{#each filteredEntries as e, idx (`${e.strategy_name}:${e.symbol}:${e.timeframe}:${e.scan_id ?? ''}:${idx}`)}
					<tr
						class="border-t border-[#111] hover:bg-[#0a0a0a] transition-colors cursor-pointer {tierFilter === 'all' && e.tier === 'weak' ? 'opacity-40' : ''}"
						role="button"
						tabindex="0"
						on:click={() => {
							const strategyName = getStrategyName(e);
							openStrategyDetail({
								name: strategyName,
								source: toLifecycleSource(e.source),
								sourceRef: e.scan_id,
								symbol: e.symbol,
								timeframe: e.timeframe,
								returnTo: '/',
							});
						}}
						on:keydown={(ev) => handleRowKeydown(ev, e)}
					>
						<td class="px-2 py-1 truncate max-w-[160px] font-mono" title={getDisplayLabel(e)}>
							{#if isWinnerRow(e)}
								<span
									data-testid={`winner-badge-${e.id || e.strategy_name}`}
									class="text-amber-400 text-xs mr-1"
									role="img"
									aria-label="Recent winner"
									title="Recent winner"
								>★</span>
							{/if}
							<span
								class="cursor-pointer text-white hover:underline"
								role="button"
								tabindex="0"
								on:click={(ev) => handleStrategyNameClick(ev, e)}
								on:keydown={(ev) => handleStrategyNameKeydown(ev, e)}
							>{displayName(getDisplayLabel(e))}</span>
						</td>
						<td class="px-1 py-1 text-center"><TierBadge tier={e.tier ?? 'weak'} /></td>
						<td class="px-1 py-1"><Sparkline data={e.mini_equity ?? []} width={60} height={18} /></td>
						<td class="px-2 py-1 text-center text-gray-500">{e.symbol}<br/><span class="text-[10px]">{e.timeframe}</span></td>
						<td class="px-2 py-1 text-right font-mono {e.sharpe_ratio > 1 ? 'text-green-400' : e.sharpe_ratio > 0 ? 'text-gray-300' : 'text-red-400'}">{fmtNum(e.sharpe_ratio)}</td>
						<td class="px-2 py-1 text-right font-mono {normalizedAnnualReturn(e) > 0 ? 'text-green-400' : 'text-red-400'}">{fmtNum(normalizedAnnualReturn(e), 2)}</td>
						<td class="px-2 py-1 text-right font-mono">{fmtNum(e.win_rate, 1)}</td>
						<td class="px-2 py-1 text-right font-mono text-gray-500">{e.total_trades}</td>
						<td class="px-2 py-1 text-center">
							<button
								type="button"
								class="text-[#888] transition-colors hover:text-white"
								title="Open strategy detail"
								aria-label="Open strategy detail"
								on:click|stopPropagation={(ev) => handleStrategyNameClick(ev, e)}
							>
								<svg class="w-4 h-4 inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<path d="M7 17L17 7M8 7h9v9" />
								</svg>
							</button>
						</td>
					</tr>
				{:else}
					<tr><td colspan="9" class="px-4 py-6 text-center text-gray-600">{loading ? 'Loading...' : (loadError || 'No strategies tested yet')}</td></tr>
				{/each}
			</tbody>
		</table>
	</div>
</div>
