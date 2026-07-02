<script lang="ts">
	import type { OHLCVBar } from '$lib/api';
	import { createEventDispatcher } from 'svelte';

	export let data: OHLCVBar[] = [];
	
	const dispatch = createEventDispatcher();

	// Filtering state
	let startDate = '';
	let endDate = '';
	
	// Column visibility
	let showColumns: Record<string, boolean> = {
		timestamp: true,
		open: true,
		high: true,
		low: true,
		close: true,
		volume: true
	};

	// Pagination/Virtualization (simple slicing for now)
	let page = 0;
	const pageSize = 100;

	// Computed
	$: filteredData = data.filter(bar => {
		if (startDate && new Date(bar.timestamp) < new Date(startDate)) return false;
		if (endDate && new Date(bar.timestamp) > new Date(endDate)) return false;
		return true;
	});

	$: displayData = filteredData.slice(page * pageSize, (page + 1) * pageSize);
	$: totalPages = Math.ceil(filteredData.length / pageSize);

	function formatDate(ts: string) {
		return new Date(ts).toLocaleString();
	}

	function formatPrice(val: number) {
		// Adapt decimals to magnitude so micro-cap prices (PEPE, SHIB, …) aren't shown as 0.00.
		const abs = Math.abs(val);
		if (abs === 0) return '0';
		if (abs >= 1) return val.toFixed(2);
		if (abs >= 0.01) return val.toFixed(4);
		return val.toPrecision(4);
	}

	function formatVolume(val: number) {
		return val.toLocaleString();
	}

	function nextPage() {
		if (page < totalPages - 1) page++;
	}

	function prevPage() {
		if (page > 0) page--;
	}

	function firstPage() {
		page = 0;
	}

	function lastPage() {
		page = Math.max(0, totalPages - 1);
	}

	function goToPage(e: Event) {
		const input = e.target as HTMLInputElement;
		let val = parseInt(input.value);
		if (isNaN(val)) val = 1;
		val = Math.max(1, Math.min(val, totalPages));
		page = val - 1;
		input.value = val.toString();
	}
</script>

<div class="flex flex-col h-full bg-[#050505] text-xs font-mono">
	<!-- Toolbar -->
	<div class="flex items-center gap-4 p-2 border-b border-[#222] bg-[#0a0a0a]">
		<!-- Date Range -->
		<div class="flex items-center gap-2">
			<div class="flex flex-col">
				<label for="start" class="text-[9px] text-[#666] uppercase font-bold">Start</label>
				<input 
					type="datetime-local" 
					id="start" 
					bind:value={startDate}
					class="bg-[#111] border border-[#333] text-white px-2 py-1 rounded-none text-xs w-36"
				/>
			</div>
			<div class="flex flex-col">
				<label for="end" class="text-[9px] text-[#666] uppercase font-bold">End</label>
				<input 
					type="datetime-local" 
					id="end" 
					bind:value={endDate}
					class="bg-[#111] border border-[#333] text-white px-2 py-1 rounded-none text-xs w-36"
				/>
			</div>
		</div>

		<div class="w-px h-8 bg-[#222] mx-2"></div>

		<!-- Column Toggles -->
		<div class="flex items-center gap-3">
			<span class="text-[9px] text-[#666] uppercase font-bold">Columns:</span>
			{#each Object.keys(showColumns) as col}
				<label class="flex items-center gap-1.5 cursor-pointer select-none hover:text-white text-[#888]">
					<input 
						type="checkbox" 
						bind:checked={showColumns[col]} 
						class="bg-[#111] border-[#333] w-3 h-3 text-white focus:ring-0"
					/>
					<span class="uppercase text-xs">{col}</span>
				</label>
			{/each}
		</div>

		<div class="flex-1"></div>

		<!-- Stats -->
		<div class="text-xs text-[#666]">
			Showing <span class="text-white">{filteredData.length}</span> rows
		</div>
	</div>

	<!-- Table Container -->
	<div class="flex-1 overflow-auto">
		<table class="w-full text-left border-collapse">
			<thead class="sticky top-0 bg-[#111] z-10">
				<tr class="border-b border-[#333]">
					{#if showColumns.timestamp}
						<th class="p-2 font-bold text-[#666] uppercase w-48">Timestamp</th>
					{/if}
					{#if showColumns.open}
						<th class="p-2 font-bold text-[#666] uppercase text-right">Open</th>
					{/if}
					{#if showColumns.high}
						<th class="p-2 font-bold text-[#666] uppercase text-right">High</th>
					{/if}
					{#if showColumns.low}
						<th class="p-2 font-bold text-[#666] uppercase text-right">Low</th>
					{/if}
					{#if showColumns.close}
						<th class="p-2 font-bold text-[#666] uppercase text-right">Close</th>
					{/if}
					{#if showColumns.volume}
						<th class="p-2 font-bold text-[#666] uppercase text-right">Volume</th>
					{/if}
				</tr>
			</thead>
			<tbody class="divide-y divide-[#111]">
				{#each displayData as bar}
					<tr class="hover:bg-[#111] transition-colors">
						{#if showColumns.timestamp}
							<td class="p-2 text-[#888] whitespace-nowrap">{formatDate(bar.timestamp)}</td>
						{/if}
						{#if showColumns.open}
							<td class="p-2 text-[#888] text-right font-mono">{formatPrice(bar.open)}</td>
						{/if}
						{#if showColumns.high}
							<td class="p-2 text-green-400 text-right font-mono">{formatPrice(bar.high)}</td>
						{/if}
						{#if showColumns.low}
							<td class="p-2 text-red-400 text-right font-mono">{formatPrice(bar.low)}</td>
						{/if}
						{#if showColumns.close}
							<td class="p-2 text-white text-right font-bold font-mono">{formatPrice(bar.close)}</td>
						{/if}
						{#if showColumns.volume}
							<td class="p-2 text-[#666] text-right font-mono">{formatVolume(bar.volume)}</td>
						{/if}
					</tr>
				{/each}
				{#if displayData.length === 0}
					<tr>
						<td colspan={Object.values(showColumns).filter(Boolean).length} class="p-8 text-center text-[#555]">
							No data matches the current filter
						</td>
					</tr>
				{/if}
			</tbody>
		</table>
	</div>

	<!-- Pagination -->
	<div class="p-2 border-t border-[#222] bg-[#0a0a0a] flex justify-between items-center">
		<div class="flex items-center gap-2">
			<button 
				on:click={firstPage}
				disabled={page === 0}
				class="terminal-button px-2 py-1 disabled:opacity-30"
				title="First Page"
			>
				&laquo;
			</button>
			<button 
				on:click={prevPage} 
				disabled={page === 0}
				class="terminal-button px-3 py-1 disabled:opacity-30"
				title="Previous Page"
			>
				&lsaquo; Prev
			</button>
		</div>

		<div class="flex items-center gap-2 text-[#666]">
			<span>Page</span>
			<input 
				type="number" 
				min="1" 
				max={totalPages} 
				value={page + 1} 
				on:change={goToPage}
				class="bg-[#111] border border-[#333] text-white px-2 py-0.5 rounded-none text-xs w-16 text-center"
			/>
			<span>of <span class="text-white">{Math.max(1, totalPages)}</span></span>
		</div>

		<div class="flex items-center gap-2">
			<button 
				on:click={nextPage} 
				disabled={page >= totalPages - 1}
				class="terminal-button px-3 py-1 disabled:opacity-30"
				title="Next Page"
			>
				Next &rsaquo;
			</button>
			<button 
				on:click={lastPage}
				disabled={page >= totalPages - 1}
				class="terminal-button px-2 py-1 disabled:opacity-30"
				title="Last Page"
			>
				&raquo;
			</button>
		</div>
	</div>
</div>
