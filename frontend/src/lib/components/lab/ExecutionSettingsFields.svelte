<script lang="ts">
	import type { ExecutionProfileDraft } from '$lib/types/executionProfile';

	// The shared execution-settings form. Bound to the parent's executionDraft — the
	// one draft that `saveParameterDraft` persists (rendered in the Gauntlet Parameters
	// panel on the Gauntlet tab).
	export let draft: ExecutionProfileDraft;
	export let error = '';
	export let disabled = false;
	// Sizing-mode defaults live in the parent (they reuse parent constants/helpers), so
	// the parent passes the handler in. Called after the bound mode value has updated.
	export let onSizingModeChange: () => void = () => {};
</script>

<div class="mb-4 grid gap-3 lg:grid-cols-3">
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Initial Capital
		<input type="number" bind:value={draft.initial_capital} {disabled} min="0" step="1000" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Fee (bps)
		<input type="number" bind:value={draft.fee_bps} {disabled} min="0" step="0.1" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Slippage (bps)
		<input type="number" bind:value={draft.slippage_bps} {disabled} min="0" step="0.1" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Leverage
		<input type="number" bind:value={draft.leverage} {disabled} min="0" max="125" step="0.1" placeholder="Engine default" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Sizing Mode
		<select bind:value={draft.sizing_mode} {disabled} on:change={() => onSizingModeChange()} class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50">
			<option value="full">Full equity</option>
			<option value="fraction">Fraction risk</option>
			<option value="fixed">Fixed notional</option>
			<option value="atr">ATR risk</option>
			<option value="kelly">Kelly</option>
		</select>
	</label>
	{#if draft.sizing_mode === 'fraction' || draft.sizing_mode === 'atr'}
		<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			Risk Per Trade
			<input type="number" bind:value={draft.risk_per_trade} {disabled} min="0" max="1" step="0.005" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
		</label>
	{/if}
	{#if draft.sizing_mode === 'fixed'}
		<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			Fixed Notional
			<input type="number" bind:value={draft.fixed_size} {disabled} min="0" step="100" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
		</label>
	{/if}
	{#if draft.sizing_mode === 'atr'}
		<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			ATR Stop Multiplier
			<input type="number" bind:value={draft.atr_stop_multiplier} {disabled} min="0" step="0.1" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
		</label>
	{/if}
	{#if draft.sizing_mode === 'kelly'}
		<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			Kelly Multiplier
			<input type="number" bind:value={draft.kelly_multiplier} {disabled} min="0" max="5" step="0.05" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
		</label>
		<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
			Kelly Lookback
			<input type="number" bind:value={draft.kelly_lookback} {disabled} min="1" step="1" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
		</label>
	{/if}
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Stop Loss %
		<input type="number" bind:value={draft.stop_loss_pct} {disabled} min="0" max="100" step="0.1" placeholder="None" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Take Profit %
		<input type="number" bind:value={draft.take_profit_pct} {disabled} min="0" step="0.1" placeholder="None" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Trailing Stop %
		<input type="number" bind:value={draft.trailing_stop_pct} {disabled} min="0" max="100" step="0.1" placeholder="None" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
	<label class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
		Time Stop (bars)
		<input type="number" bind:value={draft.time_stop_bars} {disabled} min="1" step="1" placeholder="None" class="rounded-md mt-1 w-full border border-sc-line2 bg-sc-bg px-3 py-2 text-xs text-sc-ink outline-none transition-colors focus:border-sc-ink3 disabled:opacity-50" />
	</label>
</div>
{#if error}
	<div class="mb-3 border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">{error}</div>
{/if}
