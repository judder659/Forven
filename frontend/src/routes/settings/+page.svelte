<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { beforeNavigate, goto } from '$app/navigation';
	import { get } from 'svelte/store';

	import { getSettings, getForvenDashboard } from '$lib/api';
	import { SETTINGS_AREAS, type SettingsAreaId } from '$lib/settings/manifest';
	import { dirtyFields } from '$lib/settings/dirty';
	import { openWizard } from '$lib/stores/setupWizard';

	import SettingsSidebar from '$lib/components/settings/shell/SettingsSidebar.svelte';
	import SettingsSearch from '$lib/components/settings/shell/SettingsSearch.svelte';

	import SettingsHome from '$lib/components/settings/sections/SettingsHome.svelte';
	import SettingsData from '$lib/components/settings/sections/SettingsData.svelte';
	import SettingsLab from '$lib/components/settings/sections/SettingsLab.svelte';
	import SettingsTrading from '$lib/components/settings/sections/SettingsTrading.svelte';
	import SettingsHyperliquid from '$lib/components/settings/sections/SettingsHyperliquid.svelte';
	import SettingsNotifications from '$lib/components/settings/sections/SettingsNotifications.svelte';
	import SettingsSystem from '$lib/components/settings/sections/SettingsSystem.svelte';
	import SettingsDangerZone from '$lib/components/settings/sections/SettingsDangerZone.svelte';

	const VALID_AREAS: ReadonlySet<string> = new Set(SETTINGS_AREAS.map((a) => a.id));

	let activeArea: SettingsAreaId = 'home';
	let settings: Record<string, unknown> | null = null;
	let dashboard: Record<string, unknown> | null = null;
	let loadError: string | null = null;
	let loading = true;

	// In-app unsaved-changes guard (replaces native window.confirm).
	let leavePromptOpen = false;
	let pendingLeaveUrl: URL | null = null;
	let confirmedLeave = false;

	$: wizardIncomplete = settings != null && settings.setup_wizard_completed_at == null;

	function parseHash(hash: string): SettingsAreaId {
		// hash looks like "#area" or "#area/fieldId"; strip leading '#' and take
		// everything up to the first '/'.
		if (!hash || hash.length < 2) return 'home';
		const raw = hash.startsWith('#') ? hash.slice(1) : hash;
		const areaPart = raw.split('/')[0];
		if (VALID_AREAS.has(areaPart)) return areaPart as SettingsAreaId;
		return 'home';
	}

	function parseHashField(hash: string): string | null {
		if (!hash || hash.length < 2) return null;
		const raw = hash.startsWith('#') ? hash.slice(1) : hash;
		const slash = raw.indexOf('/');
		return slash > 0 ? raw.slice(slash + 1) : null;
	}

	// "#area/fieldId" deep links (search picks, risk-page "Edit caps/limits") must
	// LAND on the field, not just switch the area. The area section renders after
	// the settings blob loads, so retry briefly until the element exists, then
	// scroll it into view and focus it (the focus ring marks the field).
	function scrollToHashField(): void {
		if (typeof window === 'undefined') return;
		const fieldId = parseHashField(window.location.hash);
		if (!fieldId) return;
		let attempts = 0;
		const tryScroll = () => {
			const el = document.getElementById(fieldId);
			if (el) {
				el.scrollIntoView({ block: 'center', behavior: 'smooth' });
				(el as HTMLElement).focus?.({ preventScroll: true });
			} else if (attempts++ < 30) {
				setTimeout(tryScroll, 100);
			}
		};
		tryScroll();
	}

	function handleHashChange(): void {
		activeArea = parseHash(window.location.hash);
		scrollToHashField();
	}

	function setArea(id: string): void {
		const nextArea = VALID_AREAS.has(id) ? (id as SettingsAreaId) : 'home';
		activeArea = nextArea;
		if (typeof window !== 'undefined' && window.location.hash !== `#${nextArea}`) {
			window.location.hash = `#${nextArea}`;
		}
	}

	onMount(() => {
		activeArea = parseHash(window.location.hash);
		scrollToHashField();
		window.addEventListener('hashchange', handleHashChange);

		(async () => {
			const [settingsResult, dashboardResult] = await Promise.allSettled([
				getSettings(),
				getForvenDashboard(),
			]);
			if (settingsResult.status === 'fulfilled') {
				settings = settingsResult.value as unknown as Record<string, unknown>;
			} else {
				loadError =
					settingsResult.reason instanceof Error
						? settingsResult.reason.message
						: 'Failed to load settings.';
			}
			dashboard =
				dashboardResult.status === 'fulfilled'
					? (dashboardResult.value as Record<string, unknown>)
					: null;
			loading = false;
		})();
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('hashchange', handleHashChange);
		}
	});

	beforeNavigate((navigation) => {
		// Allow the navigation we re-triggered after the operator confirmed.
		if (confirmedLeave) {
			confirmedLeave = false;
			return;
		}
		if (get(dirtyFields).size === 0) return;
		// Cancel and surface a styled in-app prompt instead of a native dialog.
		navigation.cancel();
		pendingLeaveUrl = navigation.to?.url ?? null;
		leavePromptOpen = true;
	});

	function cancelLeave(): void {
		leavePromptOpen = false;
		pendingLeaveUrl = null;
	}

	function confirmLeave(): void {
		leavePromptOpen = false;
		const url = pendingLeaveUrl;
		pendingLeaveUrl = null;
		// pendingLeaveUrl is null for full-page unloads / external nav; nothing to
		// re-trigger in that case, so just drop the guard.
		if (!url) return;
		confirmedLeave = true;
		void goto(url);
	}
</script>

<div class="min-h-screen bg-black text-white p-6 space-y-6">
	<header class="flex items-baseline justify-between gap-4 border-b border-[#222] pb-4">
		<h1 class="text-lg font-bold uppercase tracking-widest text-white">Settings</h1>
		<div class="w-full max-w-md"><SettingsSearch /></div>
	</header>

	{#if wizardIncomplete}
		<div
			class="flex items-center justify-between gap-4 border border-yellow-900 bg-yellow-500/5 text-yellow-400 px-4 py-3"
			role="alert"
		>
			<span class="text-xs">Wizard incomplete — complete the onboarding wizard to finish setting up Forven.</span>
			<button
				type="button"
				on:click={openWizard}
				class="terminal-button text-xs"
			>
				Complete onboarding
			</button>
		</div>
	{/if}

	{#if loading}
		<p class="py-20 text-center text-xs uppercase tracking-widest text-[#555]">Loading settings…</p>
	{:else if loadError && !settings}
		<div class="border border-red-900 bg-red-500/5 px-4 py-3">
			<p class="text-xs text-red-400">Failed to load settings: {loadError}</p>
		</div>
	{:else if settings}
		<div class="flex gap-6">
			<SettingsSidebar active={activeArea} onChange={setArea} />
			<div class="flex-1 min-w-0">
				{#if activeArea === 'home'}
					<SettingsHome {settings} {dashboard} />
				{:else if activeArea === 'data'}
					<SettingsData {settings} />
				{:else if activeArea === 'lab'}
					<SettingsLab {settings} />
				{:else if activeArea === 'trading'}
					<SettingsTrading {settings} />
				{:else if activeArea === 'hyperliquid'}
					<SettingsHyperliquid {settings} />
				{:else if activeArea === 'notifications'}
					<SettingsNotifications {settings} />
				{:else if activeArea === 'system'}
					<SettingsSystem {settings} />
				{:else if activeArea === 'danger'}
					<SettingsDangerZone {settings} />
				{/if}
			</div>
		</div>
	{/if}

	{#if leavePromptOpen}
		<div
			class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
			role="dialog"
			aria-modal="true"
			aria-labelledby="settings-leave-title"
		>
			<div class="w-full max-w-md border border-[#222] bg-[#050505] p-5 space-y-4">
				<h2 id="settings-leave-title" class="text-sm font-bold uppercase tracking-widest text-white">
					Discard unsaved changes?
				</h2>
				<p class="text-xs text-[#888]">
					You have unsaved settings changes. Leaving this page will discard them.
				</p>
				<div class="flex justify-end gap-2">
					<button
						type="button"
						on:click={cancelLeave}
						class="terminal-button text-xs"
					>
						Stay on page
					</button>
					<button
						type="button"
						on:click={confirmLeave}
						class="terminal-button-danger text-xs"
					>
						Discard &amp; leave
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>
