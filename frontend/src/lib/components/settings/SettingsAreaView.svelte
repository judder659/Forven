<script lang="ts">
	// Renders one Settings area straight from the manifest: its subsections in
	// manifest order, each with its custom block (customBlocks.ts) and field rows.
	// Every area uses this, so grouping, ordering, advanced folding, conditional
	// fields and the default/reset controls behave the same everywhere.
	import { onMount } from 'svelte';
	import {
		SETTINGS_MANIFEST,
		SETTINGS_SUBSECTIONS,
		type SettingsAreaId,
		type SettingsEntry,
	} from '$lib/settings/manifest';
	import SettingsSubsection from '$lib/components/settings/primitives/SettingsSubsection.svelte';
	import SettingsFieldRow from '$lib/components/settings/primitives/SettingsFieldRow.svelte';
	import { markField, originalValues, pendingValues } from '$lib/settings/dirty';
	import { effectiveValueFor } from '$lib/settings/effective';
	import { differsFromDefault, entryValue, isEntryVisible, seedOriginals } from '$lib/settings/values';
	import { CUSTOM_BLOCKS } from './customBlocks';

	export let area: SettingsAreaId;
	export let settings: Record<string, unknown>;
	export let variant: 'default' | 'wizard' = 'default';
	/** Wizard: only these subsections, and no custom blocks. */
	export let visibleSubsections: string[] | null = null;

	// Secrets never round-trip, so each maps to the sibling flag that says one is saved.
	const CONFIGURED_FLAGS: Record<string, string> = {
		'hyperliquid.api_secret_key': 'hyperliquid_has_key',
		'notifications.discord_bot_token': 'discord_bot_token_configured',
		'notifications.discord_webhook_url': 'discord_webhook_configured',
	};

	// The Propr mirror group only exists while the Propr integration is on (the
	// same gate as the sidebar's /propr link).
	let proprEnabled = false;
	onMount(async () => {
		seedOriginals(settings);
		if (!SETTINGS_SUBSECTIONS.some((s) => s.area === area && s.id === 'trading-propr-mirror')) return;
		try {
			const { getProprEnabled } = await import('$lib/api/propr');
			proprEnabled = await getProprEnabled();
		} catch {
			proprEnabled = false;
		}
	});

	$: values = { ...$originalValues, ...$pendingValues };

	function current(entry: SettingsEntry, vals: Record<string, unknown>): unknown {
		return entry.id in vals ? vals[entry.id] : entryValue(settings, entry);
	}

	function byOrder(a: SettingsEntry, b: SettingsEntry): number {
		return (a.order ?? Number.MAX_SAFE_INTEGER) - (b.order ?? Number.MAX_SAFE_INTEGER);
	}

	$: subs = SETTINGS_SUBSECTIONS.filter(
		(s) =>
			s.area === area &&
			(!visibleSubsections || visibleSubsections.includes(s.id)) &&
			(s.id !== 'trading-propr-mirror' || proprEnabled),
	);

	type Group = {
		sub: (typeof SETTINGS_SUBSECTIONS)[number];
		block: (typeof CUSTOM_BLOCKS)[string] | null;
		main: SettingsEntry[];
		advanced: SettingsEntry[];
		changed: SettingsEntry[];
	};

	$: groups = subs
		.map((sub): Group => {
			const block = variant === 'wizard' ? null : (CUSTOM_BLOCKS[sub.id] ?? null);
			// Array.sort is stable, so entries without an order keep manifest order.
			const entries = SETTINGS_MANIFEST.filter(
				(e) => e.area === area && e.subsection === sub.id && !e.hidden && isEntryVisible(e, settings, values),
			).sort(byOrder);
			return {
				sub,
				block,
				main: entries.filter((e) => !e.advanced),
				advanced: entries.filter((e) => e.advanced),
				changed: entries.filter((e) => differsFromDefault(e, current(e, values))),
			};
		})
		.filter((g) => g.block || g.main.length > 0 || g.advanced.length > 0);

	function resetGroup(group: Group): void {
		for (const entry of group.changed) markField(entry.id, entry.default);
	}
</script>

<div class="space-y-6">
	{#each groups as group (group.sub.id)}
		<SettingsSubsection
			id={group.sub.id}
			label={group.sub.label}
			description={group.sub.description ?? ''}
			deepLinkTo={group.sub.deepLinkTo}
			collapsed={Boolean(group.sub.advanced)}
			changedCount={group.changed.length}
			onReset={group.changed.length > 0 ? () => resetGroup(group) : undefined}
		>
			{#if group.block}
				<svelte:component this={group.block.component} {...group.block.props?.(settings) ?? {}} />
			{/if}
			{#each group.main as entry (entry.id)}
				<SettingsFieldRow
					id={entry.id}
					label={entry.label}
					description={entry.description}
					unit={entry.unit}
					valueHint={entry.valueHint}
					defaultValue={entry.default}
					value={current(entry, values)}
					type={entry.type}
					options={entry.options ?? []}
					realMoney={Boolean(entry.realMoney)}
					configured={Boolean(CONFIGURED_FLAGS[entry.id] && settings?.[CONFIGURED_FLAGS[entry.id]])}
					inForce={effectiveValueFor(settings, entry.backendPath)}
				/>
			{/each}
			{#if group.advanced.length > 0}
				<details class="group/adv mt-2 border-t border-sc-line pt-2" data-testid="advanced-{group.sub.id}">
					<summary class="cursor-pointer select-none py-1 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 hover:text-sc-ink">
						Advanced ({group.advanced.length})
					</summary>
					{#each group.advanced as entry (entry.id)}
						<SettingsFieldRow
							id={entry.id}
							label={entry.label}
							description={entry.description}
							unit={entry.unit}
							valueHint={entry.valueHint}
							defaultValue={entry.default}
							value={current(entry, values)}
							type={entry.type}
							options={entry.options ?? []}
							realMoney={Boolean(entry.realMoney)}
							configured={Boolean(CONFIGURED_FLAGS[entry.id] && settings?.[CONFIGURED_FLAGS[entry.id]])}
							inForce={effectiveValueFor(settings, entry.backendPath)}
						/>
					{/each}
				</details>
			{/if}
		</SettingsSubsection>
	{/each}
</div>
