<script lang="ts">
	import { onMount, createEventDispatcher } from 'svelte';
	import { slide } from 'svelte/transition';
	import { getDatasets, deleteDataset, getActiveSymbols } from '$lib/api';
	import type { Dataset } from '$lib/api';
	import { selectedDataset } from '$lib/stores';

	const dispatch = createEventDispatcher<{
		select: { dataset: Dataset };
		refresh: void;
	}>();

	export let datasets: Dataset[] = [];
	export let loading = false;
	export let selectedSymbol: string | null = null;
	export let selectedTimeframe: string | null = null;

	let searchQuery = '';
	let assetClassFilter: 'all' | 'crypto' | 'stock' | 'etf' | 'forex' | 'index' = 'all';
	let expandedGroups: Record<string, boolean> = {};
	let activeSymbols: Set<string> = new Set();

	const ASSET_CLASS_BADGES: Record<string, { label: string; color: string; border: string }> = {
		crypto: { label: 'Crypto', color: 'text-[#888]', border: 'border-[#333]' },
		stock: { label: 'Stock', color: 'text-[#888]', border: 'border-[#333]' },
		etf: { label: 'ETF', color: 'text-[#888]', border: 'border-[#333]' },
		forex: { label: 'FX', color: 'text-[#888]', border: 'border-[#333]' },
		index: { label: 'Index', color: 'text-[#888]', border: 'border-[#333]' },
	};

	function inferAssetClass(dataset: Dataset): string {
		if (dataset.asset_class) return dataset.asset_class;
		// Infer from source
		if (dataset.source === 'polygon') {
			const sym = dataset.symbol.toUpperCase();
			if (sym.includes('/') || sym.includes('-USDT') || sym.includes('-USDC') || sym.includes('-USD')) return 'crypto';
			if (sym.length === 6 && /^[A-Z]+$/.test(sym)) return 'forex';
			return 'stock';
		}
		// Default: crypto for ccxt/binance sources
		if (['ccxt', 'binance', 'binance-vision'].includes(dataset.source)) return 'crypto';
		return 'stock';
	}

	onMount(async () => {
		try {
			const active = await getActiveSymbols();
			activeSymbols = new Set(active.map(a => a.symbol));
		} catch {
			// Non-blocking — LIVE badges are cosmetic
		}
	});

	function isLive(dataset: Dataset): boolean {
		const base = dataset.symbol.replace('/', '-').replace(':', '-');
		return activeSymbols.has(base) || activeSymbols.has(dataset.symbol);
	}

	$: filteredDatasets = datasets.filter(d => {
		if (assetClassFilter !== 'all' && inferAssetClass(d) !== assetClassFilter) return false;
		if (!searchQuery.trim()) return true;
		return d.symbol.toLowerCase().includes(searchQuery.toLowerCase()) ||
			d.source.toLowerCase().includes(searchQuery.toLowerCase());
	});
	$: groupedDatasets = filteredDatasets.reduce((acc, d) => {
		const base = d.symbol.split('/')[0] || d.symbol;
		if (!acc[base]) acc[base] = [];
		acc[base].push(d);
		return acc;
	}, {} as Record<string, Dataset[]>);
	$: groupedDatasetEntries = Object.entries(groupedDatasets).sort(([a], [b]) => a.localeCompare(b));
	$: {
		const next = { ...expandedGroups };
		let changed = false;
		for (const [base] of groupedDatasetEntries) {
			if (next[base] === undefined) {
				next[base] = false;
				changed = true;
			}
		}
		if (changed) expandedGroups = next;
	}

	function toggleGroup(base: string): void {
		const currently = searchQuery.trim() ? true : (expandedGroups[base] ?? false);
		expandedGroups = { ...expandedGroups, [base]: !currently };
	}

	function select(dataset: Dataset) {
		const base = dataset.symbol.split('/')[0] || dataset.symbol;
		if (expandedGroups[base] === false) {
			expandedGroups = { ...expandedGroups, [base]: true };
		}
		dispatch('select', { dataset });
	}

	async function remove(dataset: Dataset, e: MouseEvent) {
		e.stopPropagation();
		if (!confirm(`Delete ${dataset.symbol} ${dataset.timeframe}?`)) return;
		try {
			await deleteDataset(dataset.symbol, dataset.timeframe);
			dispatch('refresh');
		} catch (err) {
			alert(
				`Failed to delete ${dataset.symbol} ${dataset.timeframe}: ${err instanceof Error ? err.message : String(err)}`
			);
		}
	}
</script>

<div class="flex flex-col h-full bg-[#050505]">
	<!-- Header / Search -->
	<div class="p-2 border-b border-[#222] space-y-1.5">
		<input
			type="text"
			bind:value={searchQuery}
			placeholder="Search datasets..."
			class="terminal-input"
		/>
		<select
			bind:value={assetClassFilter}
			class="w-full bg-[#0b0b0b] border border-[#333] px-2 py-1 text-[10px] outline-none focus:border-[#555] text-[#888]"
		>
			<option value="all">All asset classes</option>
			<option value="crypto">Crypto</option>
			<option value="stock">Stocks</option>
			<option value="etf">ETFs</option>
			<option value="forex">Forex</option>
			<option value="index">Indices</option>
		</select>
	</div>

	<!-- List -->
	<div class="flex-1 overflow-y-auto">
		{#if loading && datasets.length === 0}
			<div class="p-4 text-xs text-[#666] text-center">Loading...</div>
		{:else if filteredDatasets.length === 0}
			<div class="p-4 text-xs text-[#666] text-center">No datasets found</div>
		{:else}
			{#each groupedDatasetEntries as [base, groupDatasets]}
				{@const expanded = !!searchQuery.trim() || expandedGroups[base] === true}
				<button
					type="button"
					class="sticky top-0 w-full bg-[#0a0a0a] px-3 py-1.5 border-b border-[#222] z-10 flex items-center gap-2 text-left hover:bg-[#111] transition-colors"
					on:click={() => toggleGroup(base)}
					aria-expanded={expanded}
					title={expanded ? 'Collapse' : 'Expand'}
				>
					<span class="text-[10px] text-[#666] font-mono transition-transform duration-150" class:rotate-90={expanded}>&#9654;</span>
					<span class="text-xs font-bold text-[#888] tracking-wider uppercase">{base}</span>
					<span class="text-[9px] px-1.5 py-0.5 bg-[#111] text-[#666]">{groupDatasets.length}</span>
				</button>
				{#if expanded}
					<div transition:slide={{ duration: 150 }}>
						{#each groupDatasets as dataset}
							{@const isSelected = selectedSymbol === dataset.symbol && selectedTimeframe === dataset.timeframe}
						{@const ac = inferAssetClass(dataset)}
							{@const badge = ASSET_CLASS_BADGES[ac]}
							<div
								class="terminal-list-item group {isSelected ? 'active' : ''}"
								on:click={() => select(dataset)}
								on:keydown={(e) => e.key === 'Enter' && select(dataset)}
								role="button"
								tabindex="0"
							>
								<div class="flex flex-col min-w-0">
									<span class="font-bold truncate" style={isSelected ? "color: #fff;" : ""}>{dataset.symbol}</span>
									<span class="text-[10px] text-[#666] flex gap-2">
										<span class="text-[#888] font-mono">{dataset.timeframe}</span>
										<span>&bull;</span>
										<span class="uppercase opacity-60">{dataset.source}</span>
									</span>
								</div>
								<div class="flex items-center gap-2">
									{#if badge && ac !== 'crypto'}
										<span class="text-[8px] font-bold tracking-widest {badge.color} border {badge.border} px-1 py-0.5">{badge.label}</span>
									{/if}
									{#if isLive(dataset)}
										<span class="text-[8px] font-bold tracking-widest text-emerald-400 border border-emerald-900 px-1 py-0.5">LIVE</span>
									{/if}
									<span class="text-[10px] text-[#555] font-mono">{(dataset.row_count || 0).toLocaleString()} <span class="opacity-50 text-[9px]">bars</span></span>
									<button
										class="hidden group-hover:block text-red-500 hover:text-red-400 p-1"
										on:click={(e) => remove(dataset, e)}
										title="Delete"
									>
										&times;
									</button>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			{/each}
		{/if}
	</div>
</div>
