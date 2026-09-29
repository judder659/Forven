<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Strategy } from '$lib/api';

	export let strategies: Strategy[] = [];
	export let symbols: string[] = [];
	export let timeframes: string[] = [];
	export let selectedSymbol: string = '';
	export let selectedTimeframe: string = '';
	export let selectedStrategy: Strategy | null = null;
	export let showSignals: boolean = true;
	export let isLoadingSignals: boolean = false;
	export let entryCount: number = 0;
	export let exitCount: number = 0;

	const dispatch = createEventDispatcher<{
		symbolChange: string;
		timeframeChange: string;
		strategyChange: Strategy | null;
		toggleSignals: boolean;
		refreshSignals: void;
	}>();

	function handleSymbolChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		dispatch('symbolChange', select.value);
	}

	function handleTimeframeChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		dispatch('timeframeChange', select.value);
	}

	function handleStrategyChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		const strategy = strategies.find((s) => s.name === select.value) || null;
		dispatch('strategyChange', strategy);
	}

	function handleToggleSignals() {
		dispatch('toggleSignals', !showSignals);
	}

	function handleRefresh() {
		dispatch('refreshSignals');
	}
</script>

<div class="chart-toolbar">
	<!-- Strategy selector -->
	<div class="toolbar-group">
		<label class="toolbar-label" for="chart-toolbar-strategy">Strategy</label>
		<select
			id="chart-toolbar-strategy"
			class="toolbar-select strategy-select"
			value={selectedStrategy?.name || ''}
			on:change={handleStrategyChange}
		>
			<option value="">-- None --</option>
			{#each strategies as strategy}
				<option value={strategy.name}>{strategy.name}</option>
			{/each}
		</select>
	</div>

	<!-- Symbol selector -->
	<div class="toolbar-group">
		<label class="toolbar-label" for="chart-toolbar-symbol">Symbol</label>
		<select
			id="chart-toolbar-symbol"
			class="toolbar-select"
			value={selectedSymbol}
			on:change={handleSymbolChange}
			disabled={symbols.length === 0}
		>
			{#if symbols.length === 0}
				<option value="">No data</option>
			{:else}
				{#each symbols as symbol}
					<option value={symbol}>{symbol}</option>
				{/each}
			{/if}
		</select>
	</div>

	<!-- Timeframe selector -->
	<div class="toolbar-group">
		<label class="toolbar-label" for="chart-toolbar-timeframe">Timeframe</label>
		<select
			id="chart-toolbar-timeframe"
			class="toolbar-select"
			value={selectedTimeframe}
			on:change={handleTimeframeChange}
			disabled={timeframes.length === 0}
		>
			{#if timeframes.length === 0}
				<option value="">--</option>
			{:else}
				{#each timeframes as tf}
					<option value={tf}>{tf}</option>
				{/each}
			{/if}
		</select>
	</div>

	<!-- Spacer -->
	<div class="toolbar-spacer"></div>

	<!-- Signal controls -->
	{#if selectedStrategy}
		<div class="toolbar-group signal-controls">
			<button
				class="toolbar-btn"
				class:active={showSignals}
				on:click={handleToggleSignals}
				title={showSignals ? 'Hide signals' : 'Show signals'}
			>
				<svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					{#if showSignals}
						<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
						<circle cx="12" cy="12" r="3" />
					{:else}
						<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
						<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
						<line x1="1" y1="1" x2="23" y2="23" />
					{/if}
				</svg>
				Signals
			</button>

			<!-- Signal count badge -->
			{#if !isLoadingSignals && (entryCount > 0 || exitCount > 0)}
				<span class="signal-count">
					<span class="entry-count" title="Entry signals">{entryCount}</span>
					/
					<span class="exit-count" title="Exit signals">{exitCount}</span>
				</span>
			{/if}

			<button
				class="toolbar-btn refresh-btn"
				on:click={handleRefresh}
				disabled={isLoadingSignals}
				title="Refresh signals"
			>
				<svg
					class="btn-icon"
					class:spinning={isLoadingSignals}
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
				>
					<polyline points="23 4 23 10 17 10" />
					<polyline points="1 20 1 14 7 14" />
					<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
				</svg>
			</button>
		</div>
	{/if}
</div>

<style>
	.chart-toolbar {
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0.75rem 1rem;
		background: #0c0e11;
		border-bottom: 1px solid #1c2026;
	}

	.toolbar-group {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.toolbar-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: #747c88;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.toolbar-select {
		background: #11141a;
		border: 1px solid #2a2f38;
		color: #eef1f5;
		padding: 0.375rem 0.75rem;
		font-size: 0.875rem;
		font-family: inherit;
		cursor: pointer;
		min-width: 100px;
	}

	.toolbar-select:hover:not(:disabled) {
		border-color: #2a2f38;
	}

	.toolbar-select:focus {
		outline: none;
		border-color: #2a2f38;
	}

	.toolbar-select:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.strategy-select {
		min-width: 150px;
	}

	.toolbar-spacer {
		flex: 1;
	}

	.signal-controls {
		gap: 0.5rem;
	}

	.toolbar-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.375rem 0.75rem;
		background: #11141a;
		border: 1px solid #2a2f38;
		color: #aab1bc;
		font-size: 0.75rem;
		font-family: inherit;
		cursor: pointer;
		transition: background-color 0.15s, border-color 0.15s, color 0.15s;
	}

	.toolbar-btn:hover:not(:disabled) {
		background: #181c23;
		color: #eef1f5;
	}

	.toolbar-btn.active {
		background: #11141a;
		border-color: #eef1f5;
		color: #eef1f5;
	}

	.toolbar-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.refresh-btn {
		padding: 0.375rem;
	}

	.btn-icon {
		width: 1rem;
		height: 1rem;
	}

	.btn-icon.spinning {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.signal-count {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.25rem 0.5rem;
		background: #11141a;
		border: 1px solid #2a2f38;
		font-size: 0.75rem;
		font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
		color: #aab1bc;
	}

	.entry-count {
		color: #34d399;
	}

	.exit-count {
		color: #ef4444;
	}
</style>
