<script lang="ts">
	interface Fold {
		fold_index: number;
		train_start: string;
		train_end: string;
		test_start: string;
		test_end: string;
		train_metric: number;
		test_metric: number;
		best_params?: Record<string, unknown>;
	}

	export let folds: Fold[] = [];
	export let height: number = 180;

	function formatDate(dateStr: string): string {
		try {
			const d = new Date(dateStr);
			return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
		} catch {
			return dateStr;
		}
	}

	function formatMetric(val: number): string {
		return val.toFixed(3);
	}

	// Calculate degradation percentage
	function degradation(train: number, test: number): number {
		if (train === 0) return 0;
		return ((test - train) / Math.abs(train)) * 100;
	}

	// Get color based on degradation
	function getBarColor(train: number, test: number): string {
		const deg = degradation(train, test);
		if (deg >= 0) return 'rgba(34, 197, 94, 0.8)'; // green - no degradation
		if (deg > -20) return 'rgba(234, 179, 8, 0.8)'; // yellow - mild
		return 'rgba(239, 68, 68, 0.8)'; // red - significant
	}

	$: maxMetric = folds.length > 0 
		? Math.max(...folds.flatMap(f => [f.train_metric, f.test_metric]))
		: 1;
</script>

<div class="walk-forward-chart" style="height: {height}px">
	{#if folds.length === 0}
		<div class="empty-state">No fold data available</div>
	{:else}
		<div class="chart-area">
			<!-- Bar chart showing train vs test for each fold -->
			<div class="bars-container">
				{#each folds as fold, i}
					{@const trainHeight = (fold.train_metric / maxMetric) * 100}
					{@const testHeight = (fold.test_metric / maxMetric) * 100}
					{@const deg = degradation(fold.train_metric, fold.test_metric)}
					
					<div class="fold-group">
						<div class="bars">
							<div class="bar train" style="height: {trainHeight}%;" title="Train: {formatMetric(fold.train_metric)}">
								<span class="bar-value">{formatMetric(fold.train_metric)}</span>
							</div>
							<div class="bar test" style="height: {testHeight}%; background-color: {getBarColor(fold.train_metric, fold.test_metric)};" title="Test: {formatMetric(fold.test_metric)}">
								<span class="bar-value">{formatMetric(fold.test_metric)}</span>
							</div>
						</div>
						<div class="fold-label">Fold {fold.fold_index + 1}</div>
						<div class="degradation" class:positive={deg >= 0} class:negative={deg < 0}>
							{deg >= 0 ? '+' : ''}{deg.toFixed(0)}%
						</div>
					</div>
				{/each}
			</div>

			<!-- Timeline strip at bottom -->
			<div class="timeline-strip">
				{#each folds as fold, i}
					<div class="timeline-segment">
						<div class="train-period" title="{formatDate(fold.train_start)} - {formatDate(fold.train_end)}"></div>
						<div class="test-period" title="{formatDate(fold.test_start)} - {formatDate(fold.test_end)}"></div>
					</div>
				{/each}
			</div>
		</div>

		<div class="legend">
			<span class="legend-item"><span class="box train"></span> Train</span>
			<span class="legend-item"><span class="box test"></span> Test</span>
			<span class="legend-item text-[#555]">|</span>
			<span class="legend-item"><span class="timeline-box train"></span> Train Period</span>
			<span class="legend-item"><span class="timeline-box test"></span> Test Period</span>
		</div>
	{/if}
</div>

<style>
	.walk-forward-chart {
		width: 100%;
		display: flex;
		flex-direction: column;
	}
	.empty-state {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		color: #666666;
		font-size: 12px;
	}
	.chart-area {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.bars-container {
		flex: 1;
		display: flex;
		gap: 0.75rem;
		align-items: flex-end;
		padding: 0.5rem 0;
	}
	.fold-group {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.25rem;
	}
	.bars {
		flex: 1;
		width: 100%;
		display: flex;
		gap: 2px;
		align-items: flex-end;
		min-height: 60px;
	}
	.bar {
		flex: 1;
		min-height: 4px;
		position: relative;
		transition: height 0.3s ease;
	}
	.bar.train {
		background-color: rgba(255, 255, 255, 0.35);
	}
	.bar.test {
		/* Color set dynamically via inline style */
		transition: background-color 0.3s ease;
	}
	.bar-value {
		position: absolute;
		top: -16px;
		left: 50%;
		transform: translateX(-50%);
		font-size: 9px;
		color: #888888;
		white-space: nowrap;
	}
	.fold-label {
		font-size: 9px;
		color: #666666;
		text-transform: uppercase;
	}
	.degradation {
		font-size: 10px;
		font-weight: 600;
		font-family: monospace;
	}
	.degradation.positive { color: #22c55e; }
	.degradation.negative { color: #ef4444; }

	.timeline-strip {
		display: flex;
		height: 12px;
		overflow: hidden;
		background: #111111;
	}
	.timeline-segment {
		flex: 1;
		display: flex;
	}
	.train-period {
		flex: 7;
		background: rgba(255, 255, 255, 0.2);
	}
	.test-period {
		flex: 3;
		background: rgba(34, 197, 94, 0.4);
	}

	.legend {
		display: flex;
		justify-content: center;
		gap: 1rem;
		margin-top: 0.5rem;
		font-size: 10px;
		color: #888888;
	}
	.legend-item {
		display: flex;
		align-items: center;
		gap: 0.25rem;
	}
	.box {
		width: 10px;
		height: 10px;
	}
	.box.train { background: rgba(255, 255, 255, 0.35); }
	.box.test { background: rgba(34, 197, 94, 0.7); }
	.timeline-box {
		width: 16px;
		height: 6px;
	}
	.timeline-box.train { background: rgba(255, 255, 255, 0.2); }
	.timeline-box.test { background: rgba(34, 197, 94, 0.4); }
</style>
