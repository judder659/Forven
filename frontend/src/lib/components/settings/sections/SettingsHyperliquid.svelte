<script lang="ts">
	import { onMount } from 'svelte';
	import {
		SETTINGS_MANIFEST,
		SETTINGS_SUBSECTIONS,
		type SettingsEntry,
	} from '$lib/settings/manifest';
	import SettingsSubsection from '$lib/components/settings/primitives/SettingsSubsection.svelte';
	import SettingsFieldRow from '$lib/components/settings/primitives/SettingsFieldRow.svelte';
	import WalletsManager from '$lib/components/settings/WalletsManager.svelte';
	import { originalValues, pendingValues } from '$lib/settings/dirty';
	import { openExternal } from '$lib/external-open';

	export let settings: Record<string, unknown>;
	export let currentValues: Record<string, unknown> = {};
	export let variant: 'default' | 'wizard' = 'default';
	export let visibleSubsections: string[] | null = null;

	const AREA = 'hyperliquid' as const;

	// Hyperliquid referral CTA — rendered inside the credentials subsection, which
	// shows up both in the setup wizard's "Trading basics" step and on the full
	// Settings page. Every user needs a Hyperliquid account to trade, so we offer
	// them one — with a 4% fee discount for them — via our referral link.
	const HL_REFERRAL_URL = 'https://app.hyperliquid.xyz/join/FORVEN';
	let referralCopyFallback = false;
	async function openReferral(): Promise<void> {
		// Hand the URL to the system browser via the Tauri opener; window.open is a
		// silent no-op in the packaged shell. Reveal a copy-able fallback on failure.
		referralCopyFallback = !(await openExternal(HL_REFERRAL_URL));
	}

	const allSubs = SETTINGS_SUBSECTIONS.filter((s) => s.area === AREA);
	$: subs = variant === 'wizard' && visibleSubsections
		? allSubs.filter((s) => visibleSubsections!.includes(s.id))
		: allSubs;
	const areaEntries = SETTINGS_MANIFEST.filter((e) => e.area === AREA);
	$: entriesBySub = Object.fromEntries(
		subs.map((s) => [s.id, areaEntries.filter((e) => e.subsection === s.id)]),
	) as Record<string, SettingsEntry[]>;

	function readByPath(obj: unknown, path: string): unknown {
		return path
			.split('.')
			.reduce<any>((cursor, key) => (cursor == null ? undefined : cursor[key]), obj);
	}

	function initialValue(entry: SettingsEntry): unknown {
		// Settings blob is FLAT — backendSection is only a routing label, not a storage key.
		const v = readByPath(settings, entry.backendPath);
		return v === undefined ? entry.default : v;
	}

	// Seed original/current on mount so parent and dirty tracking have a baseline.
	onMount(() => {
		const origSeed: Record<string, unknown> = {};
		for (const e of areaEntries) origSeed[e.id] = initialValue(e);
		originalValues.update((o) => ({ ...o, ...origSeed }));
	});

	// Reactive derivation: currentValues = originals + pending (pending wins).
	$: {
		const originals: Record<string, unknown> = {};
		for (const e of areaEntries) originals[e.id] = initialValue(e);
		const pend = $pendingValues;
		const areaPending: Record<string, unknown> = {};
		for (const e of areaEntries) {
			if (e.id in pend) areaPending[e.id] = pend[e.id];
		}
		currentValues = { ...currentValues, ...originals, ...areaPending };
	}

	function displayValue(entry: SettingsEntry): unknown {
		const pend = $pendingValues;
		if (entry.id in pend) return pend[entry.id];
		return initialValue(entry);
	}
</script>

<div class="space-y-6">
	{#if variant !== 'wizard'}
		<!-- Wallets manager first — the operational heart of the page. Manifest
		     fields (credentials, books) follow below. -->
		<WalletsManager />
	{/if}

	{#each subs as sub (sub.id)}
		{@const entries = entriesBySub[sub.id] ?? []}
		{@const usedBy = [...new Set(entries.flatMap((e) => e.usedBy))]}
		<SettingsSubsection
			label={sub.label}
			description={sub.description ?? ''}
			deepLinkTo={sub.deepLinkTo}
			{usedBy}
		>
			{#each entries as entry (entry.id)}
				<SettingsFieldRow
					id={entry.id}
					label={entry.label}
					description={entry.description}
					unit={entry.unit}
					defaultValue={entry.default}
					value={displayValue(entry)}
					type={entry.type}
					options={entry.options ?? []}
					configured={entry.id === 'hyperliquid.api_secret_key' && Boolean(settings?.hyperliquid_has_key)}
				/>
			{/each}
			{#if sub.id === 'hl-credentials'}
				<div class="mt-4 border border-[#222] bg-[#050505] p-4">
					<p class="text-sm text-[#aaa]">
						No Hyperliquid account yet?
						<a
							href={HL_REFERRAL_URL}
							on:click|preventDefault={openReferral}
							class="font-semibold text-white hover:underline"
						>
							Create one and get 4% off trading fees →
						</a>
					</p>
					<p class="mt-1 text-xs text-[#555]">
						Referral link — you get the fee discount, we earn a small share of exchange
						fees. This helps with operating costs.
					</p>
					{#if referralCopyFallback}
						<p class="mt-2 text-xs text-yellow-400 break-all">
							Couldn't open your browser. Copy this link: {HL_REFERRAL_URL}
						</p>
					{/if}
				</div>
			{/if}
		</SettingsSubsection>
	{/each}
</div>
