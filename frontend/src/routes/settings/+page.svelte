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
	import SettingsAgents from '$lib/components/settings/sections/SettingsAgents.svelte';
	import SettingsModels from '$lib/components/settings/sections/SettingsModels.svelte';
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

	function handleHashChange(): void {
		activeArea = parseHash(window.location.hash);
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
	<header class="flex items-baseline justify-between gap-4">
		<h1 class="text-2xl font-semibold text-white">Settings</h1>
		<div class="w-full max-w-md"><SettingsSearch /></div>
	</header>

	{#if wizardIncomplete}
		<div
			class="flex items-center justify-between gap-4 border border-amber-700 bg-amber-900/30 text-amber-100 rounded px-4 py-3"
			role="alert"
		>
			<span class="text-sm">Wizard incomplete — complete the onboarding wizard to finish setting up Forven.</span>
			<button
				type="button"
				on:click={openWizard}
				class="px-3 py-1.5 rounded bg-amber-600 hover:bg-amber-500 text-white text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-300"
			>
				Complete onboarding
			</button>
		</div>
	{/if}

	{#if loading}
		<p class="text-sm text-gray-400">Loading settings…</p>
	{:else if loadError && !settings}
		<div class="border border-red-900 bg-red-950/40 rounded p-4">
			<p class="text-sm text-red-200">Failed to load settings: {loadError}</p>
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
				{:else if activeArea === 'agents'}
					<SettingsAgents {settings} />
				{:else if activeArea === 'models'}
					<SettingsModels {settings} />
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
			class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
			role="dialog"
			aria-modal="true"
			aria-labelledby="settings-leave-title"
		>
			<div class="w-full max-w-md rounded border border-[#333] bg-[#0a0a0a] p-5 space-y-4 shadow-xl">
				<h2 id="settings-leave-title" class="text-base font-semibold text-white">
					Discard unsaved changes?
				</h2>
				<p class="text-sm text-gray-400">
					You have unsaved settings changes. Leaving this page will discard them.
				</p>
				<div class="flex justify-end gap-2">
					<button
						type="button"
						on:click={cancelLeave}
						class="px-3 py-1.5 rounded border border-[#333] text-sm text-gray-300 hover:bg-[#161616] focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-500"
					>
						Stay on page
					</button>
					<button
						type="button"
						on:click={confirmLeave}
						class="px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400"
					>
						Discard &amp; leave
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>
