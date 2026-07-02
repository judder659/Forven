<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Scan } from '$lib/api';

	export let scan: Scan;

	const dispatch = createEventDispatcher();

	$: progress = scan?.progress_json;
	$: pct = progress?.pct_complete ?? 0;
	$: isRunning = scan?.status === 'running';
	$: isPaused = scan?.status === 'paused';
	$: isDone = scan?.status === 'completed' || scan?.status === 'cancelled' || scan?.status === 'failed';

	function formatTime(secs: number): string {
		if (secs < 60) return `${Math.round(secs)}s`;
		if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
		return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
	}
</script>

<div class="space-y-3">
	<!-- Status bar -->
	<div class="flex items-center gap-2">
		<div class="w-2 h-2 rounded-full {isRunning ? 'bg-emerald-400 animate-pulse' : isPaused ? 'bg-yellow-400' : isDone ? (scan.status === 'completed' ? 'bg-emerald-400' : 'bg-red-500') : 'bg-gray-700'}"></div>
		<span class="text-[10px] uppercase tracking-wider text-[#888]">{scan.status}</span>
		<span class="text-xs text-[#555] ml-auto">{scan.name}</span>
	</div>

	<!-- Progress bar -->
	<div class="w-full bg-[#222] h-2 overflow-hidden">
		<div class="h-full bg-white transition-all duration-300" style="width: {pct}%"></div>
	</div>

	<!-- Stats row -->
	<div class="grid grid-cols-4 gap-2 text-center">
		<div>
			<div class="text-lg font-mono">{progress?.completed_count?.toLocaleString() ?? 0}</div>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">TESTED</div>
		</div>
		<div>
			<div class="text-lg font-mono text-[#888]">{progress?.pruned_count?.toLocaleString() ?? 0}</div>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">PRUNED</div>
		</div>
		<div>
			<div class="text-lg font-mono text-yellow-400">{progress?.best_sharpe?.toFixed(2) ?? '0.00'}</div>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">BEST SHARPE</div>
		</div>
		<div>
			<div class="text-lg font-mono text-[#888]">{formatTime(progress?.elapsed_seconds ?? 0)}</div>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">ELAPSED</div>
		</div>
	</div>

	{#if progress?.best_strategy_name}
		<div class="bg-[#111] border border-[#222] px-3 py-2">
			<div class="text-[10px] uppercase tracking-wider text-[#666] mb-0.5">BEST SO FAR</div>
			<div class="text-xs font-mono truncate">{progress.best_strategy_name}</div>
		</div>
	{/if}

	{#if progress?.current_symbol && isRunning}
		<div class="text-xs text-[#666]">
			Testing: {progress.current_symbol} {progress.current_timeframe}
			{#if progress.avg_backtest_ms > 0}
				({progress.avg_backtest_ms.toFixed(0)}ms avg)
			{/if}
		</div>
	{/if}

	<!-- Controls -->
	{#if !isDone}
		<div class="flex gap-2 pt-1">
			{#if isRunning}
				<button class="px-3 py-1 text-xs uppercase tracking-wide border border-[#333] hover:border-yellow-500 hover:text-yellow-400 transition-colors" on:click={() => dispatch('pause')}>
					Pause
				</button>
			{:else if isPaused}
				<button class="px-3 py-1 text-xs uppercase tracking-wide border border-[#333] hover:border-emerald-500 hover:text-emerald-400 transition-colors" on:click={() => dispatch('resume')}>
					Resume
				</button>
			{/if}
			<button class="px-3 py-1 text-xs uppercase tracking-wide border border-[#333] hover:border-red-500 hover:text-red-400 transition-colors" on:click={() => dispatch('cancel')}>
				Cancel
			</button>
		</div>
	{/if}

	{#if scan.error}
		<div class="text-xs text-red-400 bg-red-500/5 border border-red-900 px-3 py-2">{scan.error}</div>
	{/if}
</div>
