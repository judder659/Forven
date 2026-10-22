<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { beforeNavigate, goto } from '$app/navigation';
	import { get } from 'svelte/store';

	import { getSettings, getForvenDashboard } from '$lib/api';
	import { SETTINGS_AREAS, SETTINGS_MANIFEST, type SettingsAreaId } from '$lib/settings/manifest';
	import { clearDirty, dirtyFields } from '$lib/settings/dirty';
	import { openWizard } from '$lib/stores/setupWizard';

	import SettingsSidebar from '$lib/components/settings/shell/SettingsSidebar.svelte';
	import SettingsSearch from '$lib/components/settings/shell/SettingsSearch.svelte';

	import SettingsHome from '$lib/components/settings/sections/SettingsHome.svelte';
	import SettingsAreaView from '$lib/components/settings/SettingsAreaView.svelte';
	import ChangedSettingsView from '$lib/components/settings/ChangedSettingsView.svelte';

	const VALID_AREAS: ReadonlySet<string> = new Set(SETTINGS_AREAS.map((a) => a.id));

	// PORT-GATE-1: the Portfolio settings tab exists only while the layer's
	// master switch (System -> Experimental features) is on.

	let activeArea: SettingsAreaId = 'home';
	// "#changed": every field that differs from its default, across all areas.
	let showChanged = false;
	$: areaMeta = SETTINGS_AREAS.find((a) => a.id === activeArea);
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
		// A field link lands in the field's own area, so links written before a
		// field moved (e.g. #trading/<an automation field>) still find it.
		const fieldId = parseHashField(hash);
		const owner = fieldId ? SETTINGS_MANIFEST.find((e) => e.id === fieldId) : undefined;
		if (owner) return owner.area;
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
			const el = document.getElementById(fieldId) ?? document.getElementById(`group-${fieldId}`);
			if (el) {
				revealField(el);
				el.scrollIntoView({ block: 'center', behavior: 'smooth' });
				(el as HTMLElement).focus?.({ preventScroll: true });
			} else if (attempts++ < 30) {
				setTimeout(tryScroll, 100);
			}
		};
		tryScroll();
	}

	// Open whatever folds a deep-linked field: an "Advanced" fold inside its
	// group, or a whole group that starts collapsed.
	function revealField(el: HTMLElement): void {
		for (let node = el.parentElement; node; node = node.parentElement) {
			if (node instanceof HTMLDetailsElement) node.open = true;
			if (node.hasAttribute('hidden')) {
				const toggle = node.parentElement?.querySelector<HTMLButtonElement>('header button[aria-expanded="false"]');
				toggle?.click();
				node.removeAttribute('hidden');
			}
		}
	}

	function handleHashChange(): void {
		showChanged = window.location.hash === '#changed';
		activeArea = parseHash(window.location.hash);
		scrollToHashField();
	}

	function setArea(id: string): void {
		if (id === 'changed') {
			showChanged = true;
			if (typeof window !== 'undefined') window.location.hash = '#changed';
			return;
		}
		showChanged = false;
		const nextArea = VALID_AREAS.has(id) ? (id as SettingsAreaId) : 'home';
		activeArea = nextArea;
		if (typeof window !== 'undefined' && window.location.hash !== `#${nextArea}`) {
			window.location.hash = `#${nextArea}`;
		}
	}

	onMount(() => {
		showChanged = window.location.hash === '#changed';
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

	// A reload or closed tab can't use the in-app prompt; ask the browser to warn.
	function handleBeforeUnload(event: BeforeUnloadEvent): void {
		if (get(dirtyFields).size === 0) return;
		event.preventDefault();
		event.returnValue = '';
	}

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
		// "Discard & leave" must actually discard: the save bar lives in the
		// global layout, so kept edits would follow the operator to other pages
		// where "Save all" would still commit them.
		clearDirty();
		// pendingLeaveUrl is null for full-page unloads / external nav; nothing to
		// re-trigger in that case, so just drop the guard.
		if (!url) return;
		confirmedLeave = true;
		void goto(url);
	}
</script>

<svelte:window on:beforeunload={handleBeforeUnload} />

<div class="min-h-screen bg-sc-bg text-sc-ink p-6 space-y-6">
	<header class="flex items-baseline justify-between gap-4 border-b border-sc-line pb-4">
		<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Settings</h1>
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
				class="terminal-button text-[12px]"
			>
				Complete onboarding
			</button>
		</div>
	{/if}

	{#if loading}
		<p class="py-20 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading settings…</p>
	{:else if loadError && !settings}
		<div class="border border-red-900 bg-red-500/5 px-4 py-3">
			<p class="text-xs text-red-400">Failed to load settings: {loadError}</p>
		</div>
	{:else if settings}
		<div class="flex gap-6">
			<SettingsSidebar active={showChanged ? 'changed' : activeArea} onChange={setArea} />
			<div class="flex-1 min-w-0">
				{#if showChanged}
					<ChangedSettingsView {settings} onOpenArea={setArea} />
				{:else}
				{#if areaMeta && activeArea !== 'home'}
					<div class="mb-4 flex flex-wrap items-baseline justify-between gap-2">
						<div>
							<h2 class="text-[17px] font-semibold text-sc-ink">{areaMeta.label}</h2>
							<p class="text-xs text-sc-ink3">{areaMeta.description}</p>
						</div>
						{#if areaMeta.deepLinks.length}
							<div class="flex gap-3">
								{#each areaMeta.deepLinks as link (link.href)}
									<a href={link.href} class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 hover:text-sc-ink">{link.label} →</a>
								{/each}
							</div>
						{/if}
					</div>
				{/if}
				{#if activeArea === 'home'}
					<SettingsHome {settings} {dashboard} />
				{:else}
					{#key activeArea}
						<SettingsAreaView area={activeArea} {settings} />
					{/key}
				{/if}
				{/if}
			</div>
		</div>
	{/if}

	{#if leavePromptOpen}
		<div
			class="fixed inset-0 z-50 flex items-center justify-center bg-sc-bg/80 p-4"
			role="dialog"
			aria-modal="true"
			aria-labelledby="settings-leave-title"
		>
			<div class="rounded-md w-full max-w-md border border-sc-line bg-sc-panel p-5 space-y-4">
				<h2 id="settings-leave-title" class="text-[14px] font-semibold text-sc-ink">
					Discard unsaved changes?
				</h2>
				<p class="text-xs text-sc-ink2">
					You have unsaved settings changes. Leaving this page will discard them.
				</p>
				<div class="flex justify-end gap-2">
					<button
						type="button"
						on:click={cancelLeave}
						class="terminal-button text-[12px]"
					>
						Stay on page
					</button>
					<button
						type="button"
						on:click={confirmLeave}
						class="terminal-button-danger text-[12px]"
					>
						Discard &amp; leave
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>
