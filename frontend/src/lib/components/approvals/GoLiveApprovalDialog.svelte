<script lang="ts">
	/**
	 * GO-LIVE-1 confirmation for approving a promotion into live_graduated.
	 *
	 * The backend refuses the approve without the typed phrase and a positive
	 * per-asset notional ceiling. This dialog collects both and shows what is
	 * about to trade real money (strategy, network, mainnet arming) first.
	 */
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import { getApprovalContext, getSettings, type ApprovalRecord, type ApprovalStrategyContext } from '$lib/api/forven';

	export let approval: ApprovalRecord;
	export let busy = false;
	/** The approve call's refusal, shown here because the page banner sits behind the dialog. */
	export let errorMessage: string | null = null;

	const GO_LIVE_PHRASE = 'GO LIVE';
	const dispatch = createEventDispatcher<{ cancel: void; confirm: { ceilingUsd: number } }>();

	let ceilingUsd: number | null = 1000;
	let typed = '';
	let strategyContext: ApprovalStrategyContext | null = null;
	let testnet: boolean | null = null;
	let mainnetArmed: boolean | null = null;
	let ceilingInput: HTMLInputElement;

	$: strategy = (strategyContext?.strategy ?? null) as Record<string, unknown> | null;
	$: strategyLabel = String(strategy?.display_name || strategy?.name || approval.target_id || 'this strategy');
	$: ceilingOk = ceilingUsd !== null && Number.isFinite(ceilingUsd) && ceilingUsd > 0;
	$: phraseOk = typed.trim().toUpperCase() === GO_LIVE_PHRASE;
	$: networkLabel = testnet === null ? 'Checking…' : testnet ? 'Testnet (no real money)' : 'Mainnet (real money)';
	$: armedLabel = mainnetArmed === null ? 'Checking…' : mainnetArmed ? 'Armed: orders will be placed' : 'Not armed: mainnet orders are refused';

	onMount(async () => {
		await tick();
		ceilingInput?.focus();
		const [contextResult, settingsResult] = await Promise.allSettled([
			getApprovalContext(approval.id),
			getSettings(),
		]);
		if (contextResult.status === 'fulfilled') strategyContext = contextResult.value.strategy_context ?? null;
		if (settingsResult.status === 'fulfilled') {
			const settings = settingsResult.value as unknown as Record<string, unknown>;
			// Prefer the network orders actually resolve to; fall back to the toggle.
			const report = settings.risk_effective as { on_mainnet?: boolean } | undefined;
			testnet = typeof report?.on_mainnet === 'boolean' ? !report.on_mainnet : Boolean(settings.hyperliquid_testnet);
			mainnetArmed = Boolean(settings.mainnet_armed);
		}
	});

	function fmt(value: unknown, digits = 2): string {
		const parsed = Number(value);
		return value === null || value === undefined || !Number.isFinite(parsed) ? '—' : parsed.toFixed(digits);
	}

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && !busy) dispatch('cancel');
	}

	function confirm() {
		if (busy || !ceilingOk || !phraseOk) return;
		dispatch('confirm', { ceilingUsd: Number(ceilingUsd) });
	}
</script>

<svelte:window on:keydown={onKey} />

<div
	class="fixed inset-0 z-[60] grid place-items-center bg-[#040507]/70 p-4"
	role="presentation"
	on:click={(event) => { if (event.target === event.currentTarget && !busy) dispatch('cancel'); }}
>
	<div class="grid w-full max-w-md gap-3 rounded-lg border border-[#e5574f]/50 bg-sc-panel p-4 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="go-live-title" data-testid="go-live-dialog">
		<h2 id="go-live-title" class="text-[15px] font-semibold text-sc-ink">Go live with {strategyLabel}?</h2>
		<div class="rounded border border-[#e5574f]/45 bg-[#e5574f]/10 px-2.5 py-2 text-[12px] text-[#f3b1ab]">
			Approving moves this strategy from paper to live trading. The ceiling below is the largest position it may hold per asset. It is enforced on every order and you can change it later.
		</div>
		<div class="grid gap-1 text-[12px]">
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Strategy</span><span class="text-right font-plex-mono text-sc-ink">{approval.target_id || '—'}</span></div>
			{#if strategy}
				<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Market</span><span class="text-right font-plex-mono text-sc-ink">{strategy.symbol || '—'} · {strategy.timeframe || '—'}</span></div>
				<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Current stage</span><span class="text-right font-plex-mono text-sc-ink">{strategy.stage || '—'}</span></div>
				<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Sharpe</span><span class="text-right font-plex-mono text-sc-ink">{fmt(strategy.sharpe)}</span></div>
			{/if}
			<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Network</span><span class="text-right font-plex-mono {testnet === false ? 'text-red-300' : 'text-sc-ink'}" data-testid="go-live-network">{networkLabel}</span></div>
			{#if testnet === false}
				<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3"><span class="text-sc-ink3">Mainnet switch</span><span class="text-right font-plex-mono text-sc-ink">{armedLabel}</span></div>
			{/if}
		</div>
		{#if approval.target_id}
			<a class="text-[12px] text-sc-ink2 underline hover:text-sc-ink" href={`/lab/strategy/${encodeURIComponent(approval.target_id)}`} target="_blank" rel="noopener">Open the strategy's full results in a new tab</a>
		{/if}
		<label class="grid gap-1 text-[12px] text-sc-ink3">
			<span>Per-asset notional ceiling (USD)</span>
			<input
				bind:this={ceilingInput}
				type="number"
				min="1"
				step="any"
				bind:value={ceilingUsd}
				class="terminal-input"
				data-testid="go-live-ceiling"
			/>
			{#if !ceilingOk}<span class="text-red-300">Enter a positive amount.</span>{/if}
		</label>
		<label class="grid gap-1 text-[12px] text-sc-ink3">
			<span>Type <span class="font-semibold text-red-400">{GO_LIVE_PHRASE}</span> to confirm</span>
			<input
				type="text"
				bind:value={typed}
				placeholder={GO_LIVE_PHRASE}
				class="terminal-input"
				data-testid="go-live-phrase"
				on:keydown={(event) => { if (event.key === 'Enter') confirm(); }}
			/>
		</label>
		{#if errorMessage}
			<div class="rounded border border-red-800 bg-red-900/20 px-2.5 py-2 text-[12px] text-red-300" data-testid="go-live-error">{errorMessage}</div>
		{/if}
		<div class="flex flex-wrap justify-end gap-2">
			<button type="button" class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12.5px] font-medium text-sc-ink hover:border-sc-ink4 disabled:opacity-50" disabled={busy} on:click={() => dispatch('cancel')}>Cancel</button>
			<button
				type="button"
				class="rounded-md border border-[#e5574f]/60 bg-[#e5574f]/15 px-3 py-1.5 text-[12.5px] font-medium text-[#f6b4ae] hover:bg-[#e5574f]/25 disabled:opacity-50"
				disabled={busy || !ceilingOk || !phraseOk}
				on:click={confirm}
				data-testid="go-live-confirm"
			>{busy ? 'Working…' : 'Approve and go live'}</button>
		</div>
	</div>
</div>
