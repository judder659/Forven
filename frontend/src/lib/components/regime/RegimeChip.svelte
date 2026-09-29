<script lang="ts">
	import { formatRegimeLabel, regimeBadgeClass } from '$lib/utils/labRegime';

	interface RegimeInputs {
		regime?: string | null;
		confidence?: number | null;
		adx?: number | null;
		ema_alignment?: string | null;
		atr_ratio?: number | null;
		rsi?: number | null;
		since?: number | null;
	}

	/** Regime label or a full snapshot with classifier inputs. */
	export let regime: RegimeInputs | string | null | undefined = null;
	/** Prefix shown before the label (e.g. the asset). */
	export let asset: string = '';
	/** mini = table-cell variant: smaller, no confidence, no popover. */
	export let mini = false;

	let open = false;
	let root: HTMLElement | null = null;

	$: snapshot = typeof regime === 'string' || regime == null ? null : regime;
	$: label = formatRegimeLabel(typeof regime === 'string' ? regime : (snapshot?.regime ?? null));
	$: badgeClass = regimeBadgeClass(typeof regime === 'string' ? regime : (snapshot?.regime ?? null));
	$: confidence = snapshot?.confidence;
	$: hasDetails = !mini && snapshot != null;
	$: arrow =
		label.startsWith('TREND UP') ? '▲' : label.startsWith('TREND DOWN') ? '▼' : label.startsWith('HIGH') ? '⚡' : '↔';

	function sinceText(since: number | null | undefined): string {
		if (!since || !Number.isFinite(since)) return '';
		const seconds = Math.max(0, Date.now() / 1000 - since);
		const days = Math.floor(seconds / 86400);
		const hours = Math.floor((seconds % 86400) / 3600);
		if (days > 0) return `for ${days}d ${hours}h`;
		const minutes = Math.floor((seconds % 3600) / 60);
		if (hours > 0) return `for ${hours}h ${minutes}m`;
		return `for ${minutes}m`;
	}

	function onWindowClick(event: MouseEvent) {
		if (open && root && !root.contains(event.target as Node)) open = false;
	}
</script>

<svelte:window on:click={onWindowClick} />

<span class="relative inline-flex" bind:this={root}>
	{#if hasDetails}
		<button
			type="button"
			class={`rounded-md inline-flex items-center gap-1.5 border px-2 py-0.5 text-[11px] ${badgeClass}`}
			aria-expanded={open}
			on:click={() => (open = !open)}
		>
			{#if asset}<span class="font-bold text-sc-ink">{asset}</span>{/if}
			<span>{arrow} {label}</span>
			{#if typeof confidence === 'number'}
				<span class="text-[10px] opacity-70">{Math.round(confidence * 100)}%</span>
			{/if}
		</button>
	{:else}
		<span
			class={`inline-flex items-center gap-1 border px-1.5 py-0.5 uppercase tracking-wider ${mini ? 'text-[10px]' : 'text-[11px]'} ${badgeClass}`}
		>
			{#if asset}<span class="font-bold text-sc-ink">{asset}</span>{/if}
			{#if label === '-'}
				<span class="text-sc-ink3 normal-case">—</span>
			{:else}
				<span>{arrow} {label}</span>
			{/if}
		</span>
	{/if}

	{#if open && snapshot}
		<div
			class="rounded-md absolute left-0 top-full z-40 mt-1 w-72 border border-sc-line2 bg-sc-panel p-3 text-left shadow-xl"
		>
			<div class="mb-2 flex items-baseline justify-between gap-2">
				<span class={`border px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] ${badgeClass}`}>
					{arrow} {label}
				</span>
				<span class="text-[10px] text-sc-ink3">{sinceText(snapshot.since)}</span>
			</div>
			{#if typeof confidence === 'number'}
				<div class="mb-2 flex items-center gap-2">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Confidence</span>
					<span class="relative h-1 flex-1 bg-sc-raise">
						<span
							class="absolute inset-y-0 left-0 bg-sc-ink/70"
							style={`width:${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}%`}
						></span>
					</span>
					<span class="text-[11px] text-sc-ink2">{Math.round(confidence * 100)}%</span>
				</div>
			{/if}
			<div class="rounded-md grid grid-cols-2 gap-px border border-sc-line bg-sc-raise text-[11px]">
				<div class="bg-sc-panel2 px-2 py-1.5">
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">ADX (14)</span>
					<span class="text-sc-ink">{snapshot.adx ?? '—'}</span>
				</div>
				<div class="bg-sc-panel2 px-2 py-1.5">
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">EMA 20/50/200</span>
					<span class="text-sc-ink">{snapshot.ema_alignment ?? '—'}</span>
				</div>
				<div class="bg-sc-panel2 px-2 py-1.5">
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">ATR ratio</span>
					<span class="text-sc-ink">{snapshot.atr_ratio ?? '—'}</span>
				</div>
				<div class="bg-sc-panel2 px-2 py-1.5">
					<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">RSI (14)</span>
					<span class="text-sc-ink">{snapshot.rsi ?? '—'}</span>
				</div>
			</div>
			<p class="mt-2 text-[10px] leading-snug text-sc-ink3">
				Causal classifier — same labels the backtest, trade stamps, and entry gate use.
			</p>
		</div>
	{/if}
</span>
