<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { createChart, CrosshairMode, type IChartApi, type ISeriesApi } from 'lightweight-charts';
	import { getDashboardEquityCurves } from '$lib/api';

	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let observer: ResizeObserver | null = null;
	let loading = true;
	let error = '';

	type OverlayMode = 'normalized' | 'absolute';
	type WindowMode = 'all' | 'recent';
	let overlayMode: OverlayMode = 'normalized';
	let windowMode: WindowMode = 'recent';

	interface CurveSeries {
		name: string;
		sharpe: number;
		values: number[];
	}
	let baseSeries: CurveSeries[] = [];
	let activeSeries: ISeriesApi<"Line">[] = [];

	function extractValues(curve: any[] | undefined): number[] {
		if (!Array.isArray(curve)) return [];
		const values: number[] = [];
		for (const point of curve) {
			const raw = typeof point === 'object' && point !== null
				? point.equity ?? point.value
				: point;
			const numeric = Number(raw);
			if (Number.isFinite(numeric)) values.push(numeric);
		}
		return values;
	}

	function toNormalized(values: number[]): number[] {
		if (values.length === 0) return [];
		const base = values[0];
		if (!Number.isFinite(base) || base === 0) return values.map(() => 0);
		return values.map((v) => ((v / base) - 1) * 100);
	}

	function maxDrawdownPct(values: number[]): number {
		if (values.length < 2) return 0;
		let peak = values[0];
		let worst = 0;
		for (const v of values) {
			if (v > peak) peak = v;
			if (peak > 0) {
				const dd = ((v - peak) / peak) * 100;
				if (dd < worst) worst = dd;
			}
		}
		return worst;
	}

	function formatSigned(val: number, digits = 2): string {
		const sign = val > 0 ? '+' : '';
		return `${sign}${val.toFixed(digits)}`;
	}

	function formatMoney(val: number): string {
		return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(val);
	}

	function clipSeries(values: number[]): number[] {
		if (windowMode === 'all') return values;
		const tail = 180;
		return values.length > tail ? values.slice(values.length - tail) : values;
	}

	function chartValues(values: number[]): number[] {
		const transformed = overlayMode === 'normalized' ? toNormalized(values) : values;
		return clipSeries(transformed);
	}

	$: summary = (() => {
		if (baseSeries.length === 0) {
			return { bestReturn: 0, avgReturn: 0, worstDd: 0 };
		}
		const returns = baseSeries.map((s) => {
			if (s.values.length < 2) return 0;
			const first = s.values[0];
			const last = s.values[s.values.length - 1];
			if (!first || !Number.isFinite(first)) return 0;
			return ((last / first) - 1) * 100;
		});
		const dds = baseSeries.map((s) => maxDrawdownPct(s.values));
		return {
			bestReturn: Math.max(...returns),
			avgReturn: returns.reduce((a, b) => a + b, 0) / returns.length,
			worstDd: Math.min(...dds),
		};
	})();

	function renderChart() {
		if (!chart) return;
		
		for (const series of activeSeries) {
			chart.removeSeries(series);
		}
		activeSeries = [];

		if (baseSeries.length === 0) return;

		const colors = ['#22d3ee', '#4ade80', '#60a5fa', '#facc15', '#f472b6'];

		// Apply scale formatting based on mode
		chart.applyOptions({
			rightPriceScale: {
				mode: 0,
				autoScale: true,
			},
			localization: {
				priceFormatter: (v: number) => overlayMode === 'normalized' ? `${v.toFixed(1)}%` : `$${formatMoney(v)}`,
			}
		});

		baseSeries.forEach((s, i) => {
			const color = colors[i % colors.length];
			const lineSeries = chart!.addLineSeries({
				color: color,
				lineWidth: 2,
				crosshairMarkerVisible: true,
				lastValueVisible: false,
				priceLineVisible: false,
			});

			const plottedData = chartValues(s.values);
			const lineData = plottedData.map((val, idx) => ({
				time: idx as unknown as string, // we use index as time for structural equity comparison
				value: val
			}));

			lineSeries.setData(lineData as any[]);
			activeSeries.push(lineSeries);
		});

		chart.timeScale().fitContent();
	}

	onMount(async () => {
		if (!container) return;

		chart = createChart(container, {
			layout: {
				background: { color: 'transparent' },
				textColor: '#666',
			},
			grid: {
				vertLines: { color: '#222' },
				horzLines: { color: '#222' },
			},
			crosshair: {
				mode: CrosshairMode.Magnet,
			},
			rightPriceScale: {
				borderColor: '#222',
				autoScale: true,
			},
			timeScale: {
				borderColor: '#222',
				timeVisible: false,
			},
		});

		try {
			const curves: any[] = await getDashboardEquityCurves(undefined, 5);
			if (Array.isArray(curves) && curves.length > 0) {
				baseSeries = curves.map((c: any, i: number) => {
					const raw = Array.isArray(c?.equity_curve)
						? c.equity_curve
						: (c?.equity_curve?.equity_curve ?? []);
					return {
						name: c?.strategy_name?.substring(0, 36) || `Strategy ${i + 1}`,
						sharpe: Number(c?.sharpe_ratio ?? 0),
						values: extractValues(raw),
					};
				}).filter((s) => s.values.length > 1);
			}
			renderChart();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load curves';
		} finally {
			loading = false;
		}

		observer = new ResizeObserver((entries) => {
			if (chart && entries.length > 0) {
				const { width, height } = entries[0].contentRect;
				chart.applyOptions({ width, height });
			}
		});
		observer.observe(container);
	});

	// Re-render when modes change
	$: if (chart && !loading && (overlayMode || windowMode)) {
		renderChart();
	}

	onDestroy(() => {
		observer?.disconnect();
		if (chart) {
			chart.remove();
			chart = null;
		}
	});
</script>

<div class="flex flex-col h-full">
	<div class="flex items-center justify-between gap-2 px-2 mb-1.5">
		<div class="text-[10px] text-gray-600 uppercase tracking-wider">Equity Overlay (Top 5)</div>
		<div class="flex items-center gap-1">
			<button
				type="button"
				class="border px-2 py-0.5 text-[10px] uppercase tracking-wide {overlayMode === 'normalized' ? 'border-white bg-white text-black' : 'border-[#333] text-gray-500 hover:border-[#555] hover:text-white'}"
				on:click={() => overlayMode = 'normalized'}
			>Return %</button>
			<button
				type="button"
				class="border px-2 py-0.5 text-[10px] uppercase tracking-wide {overlayMode === 'absolute' ? 'border-white bg-white text-black' : 'border-[#333] text-gray-500 hover:border-[#555] hover:text-white'}"
				on:click={() => overlayMode = 'absolute'}
			>Equity $</button>
			<button
				type="button"
				class="border px-2 py-0.5 text-[10px] uppercase tracking-wide {windowMode === 'recent' ? 'border-white bg-white text-black' : 'border-[#333] text-gray-500 hover:border-[#555] hover:text-white'}"
				on:click={() => windowMode = 'recent'}
			>Recent</button>
			<button
				type="button"
				class="border px-2 py-0.5 text-[10px] uppercase tracking-wide {windowMode === 'all' ? 'border-white bg-white text-black' : 'border-[#333] text-gray-500 hover:border-[#555] hover:text-white'}"
				on:click={() => windowMode = 'all'}
			>All</button>
		</div>
	</div>

	<div class="px-2 mb-1.5 flex items-center gap-3 text-[10px]">
		<span class="text-gray-500">Best <span class="text-emerald-400 font-bold">{formatSigned(summary.bestReturn)}%</span></span>
		<span class="text-gray-500">Avg <span class="font-bold text-white">{formatSigned(summary.avgReturn)}%</span></span>
		<span class="text-gray-500">Worst DD <span class="text-red-400 font-bold">{summary.worstDd.toFixed(2)}%</span></span>
	</div>

	<div class="relative min-h-[200px] flex-1 overflow-hidden border border-[#222]" bind:this={container}>
		{#if loading}
			<div class="absolute inset-0 flex items-center justify-center text-xs text-gray-500 bg-[#0a0a0a]/70 z-10 pointer-events-none">Loading equity curves...</div>
		{:else if !loading && baseSeries.length === 0}
			<div class="absolute inset-0 flex items-center justify-center text-xs text-gray-500 bg-[#0a0a0a]/70 z-10 pointer-events-none">No persisted equity curves yet</div>
		{:else if error}
			<div class="absolute inset-0 flex items-center justify-center text-xs text-red-400 bg-[#0a0a0a]/70 z-10 pointer-events-none">{error}</div>
		{/if}
	</div>
</div>
