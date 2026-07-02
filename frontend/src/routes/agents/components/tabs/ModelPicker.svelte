<script lang="ts">
	/**
	 * The ONE standard provider+model picker used on every surface of the Agents
	 * page — Roster cards, the add/edit developer forms, and (wrapped in a
	 * fallback-chain) the Routing & Fallbacks slots.
	 *
	 * Hard constraint (the page-wide safety invariant): the only options ever
	 * offered are CONNECTED providers and ENABLED models. The caller passes the
	 * already-filtered `selectable` options (from `selectableModelOptions`). If
	 * the current value points at a model that is no longer selectable (its
	 * provider was disconnected or the model disabled), it is still SHOWN but
	 * clearly flagged "(unavailable)" so the operator notices and can reselect.
	 */
	import type { ForvenAgentModelOption } from '$lib/api';
	import { createEventDispatcher } from 'svelte';

	/** Current choice as a "provider:model_id" key, or '' for unset. */
	export let value: string;
	/** Connected + enabled options only. */
	export let selectable: ForvenAgentModelOption[] = [];
	export let allowUnset = false;
	export let unsetLabel = '— select —';
	export let disabled = false;
	/** Optional <label> text rendered above the select. */
	export let label = '';
	/** Optional id so an external <label for> can target the select. */
	export let id: string | undefined = undefined;
	/** Show the inline "(unavailable)" warning line below the select. */
	export let showStaleWarning = true;

	const dispatch = createEventDispatcher<{ change: { value: string } }>();

	function onChange(next: string) {
		value = next;
		dispatch('change', { value });
	}

	function labelForKey(key: string): string {
		const found = selectable.find((o) => o.key === key);
		if (found) return found.label;
		const sep = key.indexOf(':');
		return sep > 0 ? `${key.slice(0, sep)} / ${key.slice(sep + 1)}` : key;
	}

	function providerOf(key: string): string {
		const sep = key.indexOf(':');
		return sep > 0 ? key.slice(0, sep) : '';
	}

	// The current value is in the dropdown already?
	$: inSelectable = Boolean(value) && selectable.some((o) => o.key === value);

	// "Stale" ONLY when the value's PROVIDER is not connected (genuinely
	// unusable). A connected provider's model is fine even if it isn't in the
	// enable-list — so we no longer falsely flag working models as disabled.
	$: stale = Boolean(value) && !selectable.some((o) => String(o.provider) === providerOf(value));
</script>

{#if label}
	<span class="block text-[10px] text-[#666] uppercase tracking-wider mb-1">{label}</span>
{/if}
<select
	{id}
	{disabled}
	value={value}
	on:change={(e) => onChange((e.target as HTMLSelectElement).value)}
	class="terminal-select w-full text-sm font-mono disabled:opacity-60"
>
	{#if allowUnset}
		<option value="">{unsetLabel}</option>
	{/if}
	{#each selectable as opt (opt.key)}
		<option value={opt.key}>{opt.label}</option>
	{/each}
	{#if value && !inSelectable}
		<option value={value}>{labelForKey(value)}{stale ? ' (provider not connected)' : ''}</option>
	{/if}
</select>
{#if showStaleWarning && stale}
	<p class="mt-1 text-[11px] text-yellow-400">
		This model's provider isn't connected — connect it under Providers &amp; Keys, or pick a connected model.
	</p>
{/if}
