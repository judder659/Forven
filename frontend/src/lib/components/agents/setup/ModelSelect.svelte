<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { ForvenAgentModelOption } from '$lib/api';
	import { providerLabel } from '$lib/utils/agentsHub/agents';
	import { providerOfKey } from '$lib/utils/agentsHub/routing';

	/** Current choice as "provider:model_id", or '' for unset. */
	export let value = '';
	/** Every model of a connected provider. */
	export let options: ForvenAgentModelOption[] = [];
	/** Keys shown first, as the "Shortlist" group. */
	export let shortlist: Set<string> = new Set();
	/** Label of the empty choice; null means the slot cannot be left empty. */
	export let unsetLabel: string | null = null;
	export let ariaLabel = 'Model';
	export let disabled = false;
	export let compact = false;

	const dispatch = createEventDispatcher<{ change: { value: string } }>();

	function modelName(option: ForvenAgentModelOption): string {
		const label = String(option.label ?? '').trim();
		const prefix = providerLabel(option.provider);
		if (label.toLowerCase().startsWith(`${prefix.toLowerCase()} `)) return label.slice(prefix.length + 1);
		return option.model_id || label || option.key;
	}

	function fullName(option: ForvenAgentModelOption): string {
		return `${modelName(option)} · ${providerLabel(option.provider)}`;
	}

	$: known = new Map(options.map((option) => [option.key, option]));
	$: listed = options.filter((option) => shortlist.has(option.key));
	$: groups = [...new Set(options.map((option) => String(option.provider)))]
		.map((provider) => ({
			provider,
			label: providerLabel(provider),
			items: options.filter((option) => String(option.provider) === provider && !shortlist.has(option.key)),
		}))
		.filter((group) => group.items.length > 0)
		.sort((a, b) => a.label.localeCompare(b.label));
	// An assignment no picker offers stays visible, flagged: its provider is not
	// connected, or the provider's model list does not include it.
	$: orphan = value && !known.has(value) ? value : '';
	$: orphanListed = orphan ? options.some((option) => String(option.provider) === providerOfKey(orphan)) : false;
	$: orphanText = orphan
		? `${orphan.slice(orphan.indexOf(':') + 1)} · ${providerLabel(providerOfKey(orphan))} (${orphanListed ? 'not in its model list' : 'not connected'})`
		: '';
</script>

<select
	class={`w-full min-w-0 rounded-md border bg-sc-bg font-plex-mono text-sc-ink outline-none transition-colors focus:border-sc-ink4 disabled:opacity-60 ${compact ? 'px-2 py-1 text-[11.5px]' : 'px-2.5 py-1.5 text-[12px]'} ${orphan ? 'border-[#e7b24a]/60' : 'border-sc-line2'}`}
	aria-label={ariaLabel}
	{disabled}
	{value}
	on:change={(event) => dispatch('change', { value: event.currentTarget.value })}
>
	{#if unsetLabel !== null}
		<option value="">{unsetLabel}</option>
	{/if}
	{#if orphan}
		<option value={orphan}>{orphanText}</option>
	{/if}
	{#if listed.length > 0}
		<optgroup label="Shortlist">
			{#each listed as option (option.key)}
				<option value={option.key}>{fullName(option)}</option>
			{/each}
		</optgroup>
	{/if}
	{#each groups as group (group.provider)}
		<optgroup label={group.label}>
			{#each group.items as option (option.key)}
				<option value={option.key}>{modelName(option)}</option>
			{/each}
		</optgroup>
	{/each}
</select>
