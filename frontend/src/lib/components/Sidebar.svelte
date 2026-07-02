<script lang="ts">
	import { page } from '$app/stores';
	import { markNavIndicatorSeen, navRouteMetrics, navEventPulses } from '$lib/stores/navMetrics';
	import NavBadge from '$lib/components/NavBadge.svelte';

	export let connectionStatus: 'checking' | 'connected' | 'disconnected' | string = 'checking';

	interface NavLink {
		label: string;
		href: string;
		icon: string;
		solidIcon?: string;
	}

	const primaryLinks: NavLink[] = [
		{
			label: 'Dashboard',
			href: '/',
			icon: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z'
		},
		{
			label: 'Data',
			href: '/data',
			icon: 'M12 2C6.48 2 2 4.69 2 8v8c0 3.31 4.48 6 10 6s10-2.69 10-6V8c0-3.31-4.48-6-10-6zm0 2c4.42 0 8 1.79 8 4s-3.58 4-8 4-8-1.79-8-4 3.58-4 8-4zM4 16v-2.47c1.82 1.5 4.74 2.47 8 2.47s6.18-.97 8-2.47V16c0 2.21-3.58 4-8 4s-8-1.79-8-4z'
		},
		{
			label: 'Strategy Creator',
			href: '/strategy-creator',
			icon: 'M19 9l1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25L19 9zm-7.5.5L9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5L17 12l-5.5-2.5zM19 15l-1.25 2.75L15 19l2.75 1.25L19 23l1.25-2.75L23 19l-2.75-1.25L19 15z'
		},
		{
			label: 'Crucibles',
			href: '/hypotheses',
			icon: 'M12 3l8 4v5c0 4.97-3.05 9.24-8 10.5C7.05 21.24 4 16.97 4 12V7l8-4zm0 3.1L6 8.6V12c0 3.82 2.18 7.14 6 8.32 3.82-1.18 6-4.5 6-8.32V8.6l-6-2.5zm-1 2.9h2v4h-2V9zm0 5h2v2h-2v-2z'
		},
		{
			label: 'Manual Backtest',
			href: '/backtest/new',
			icon: 'M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 14l-5-5 1.41-1.41L12 14.17l4.59-4.58L18 11l-6 6z'
		},
		{
			label: 'The Forge',
			href: '/lab',
			icon: 'M8 17l-5-5 5-5 1.41 1.41L5.83 12l3.58 3.59L8 17zm8 0l-1.41-1.41L18.17 12l-3.58-3.59L16 7l5 5-5 5zM10 19l4-14h2l-4 14h-2z'
		},
		{
			label: 'Risk',
			href: '/risk',
			icon: 'M12 2L4 5v5c0 4.63 3.2 8.94 8 10 4.8-1.06 8-5.37 8-10V5l-8-3zm-1 11l-2.5-2.5 1.41-1.41L11 10.17l3.09-3.08 1.41 1.41L11 13z'
		},
		{
			label: 'Trades',
			href: '/trading',
			icon: 'M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z'
		},
		{
			label: 'Bot Factory',
			href: '/bot-factory',
			icon: 'M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zM7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5zM16 17H8v-2h8v2zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13z'
		},
	];

	const managementLinks: NavLink[] = [
		{
			label: 'Agents',
			href: '/agents',
			icon: 'M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z'
		},
		{
			label: 'Brain',
			href: '/brain',
			icon: 'M13 3a4 4 0 014 4v.18A4 4 0 0121 11v2a4 4 0 01-3 3.87V18a3 3 0 01-3 3h-1a3 3 0 01-3-3v-1H8a4 4 0 01-4-4v-2a4 4 0 014-4 4 4 0 014-4h1zm-2 4H8a2 2 0 00-2 2v4a2 2 0 002 2h3v3a1 1 0 001 1h1a1 1 0 001-1v-2h1a2 2 0 002-2v-2.18A4 4 0 0014 7.18V7a2 2 0 00-2-2h-1z'
		},
		{
			label: 'Approvals',
			href: '/approval',
			icon: 'M9 12.75L5.5 9.25L6.5 8.25L9 10.75L14.5 5.25L15.5 6.25L9 12.75M12 20.5C6.49 20.5 2 16.01 2 10.5S6.49 0.5 12 0.5 22 4.99 22 10.5 17.51 20.5 12 20.5m0-1a8.5 8.5 0 110-17 8.5 8.5 0 010 17z',
			solidIcon: 'M12,2C6.48,2,2,6.48,2,12s4.48,10,10,10s10-4.48,10-10S17.52,2,12,2z M9.29,16.29L5.7,12.7l1.41-1.41l2.18,2.18l5.18-5.18 l1.41,1.41L9.29,16.29z'
		},
		{
			label: 'Diagnostics',
			href: '/diagnostics',
			icon: 'M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 12h-2v2h-2v-2h-2v-2h2v-2h2v2h2v2zm-1-6H8V7h8v2z'
		},
		{
			label: 'Pipeline',
			href: '/pipeline',
			icon: 'M22 11V3h-7v3H9V3H2v8h7V8h2v10h4v3h7v-8h-7v3h-2V8h2v3z'
		},
		{
			label: 'Routines',
			href: '/routines',
			icon: 'M12 2a10 10 0 100 20 10 10 0 000-20zm0 18a8 8 0 110-16 8 8 0 010 16zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z'
		},
		{
			label: 'Integrations',
			href: '/integrations',
			icon: 'M12 2L2 7v10l10 5 10-5V7l-10-5zm0 2.18L19.82 8 12 11.82 4.18 8 12 4.18zM4 9.5l7 3.5v7l-7-3.5v-7zm9 10.5v-7l7-3.5v7l-7 3.5z'
		}
	];

	const settingsLink: NavLink = {
		label: 'Settings',
		href: '/settings',
		icon: 'M19.14 12.94l1.57-1.22a1 1 0 00.26-1.31l-1.4-2.4a1 1 0 00-1.31-.4l-1.85.75a6.98 6.98 0 00-1.2-.7l-.33-1.97a1 1 0 00-.98-.83h-2.8a1 1 0 00-.97.83l-.34 1.97a7.17 7.17 0 00-1.2.7l-1.85-.75a1 1 0 00-1.31.4l-1.4 2.4a1 1 0 00.26 1.31l1.57 1.22a7.13 7.13 0 000 1.4l-1.57 1.22a1 1 0 00-.26 1.31l1.4 2.4a1 1 0 00.26.4 1 1 0 001.31.06l1.85-.75a7.1 7.1 0 001.2.7l.34 1.97a1 1 0 00.97.83h2.8a1 1 0 00.98-.83l.33-1.97a6.98 6.98 0 001.2-.7l1.85.75a1 1 0 001.31-.4l1.4-2.4a1 1 0 00-.26-1.31l-1.57-1.22a7.13 7.13 0 000-1.4zM12 15.5a3.5 3.5 0 110-7 3.5 3.5 0 010 7z'
	};

	const allNavHrefs = [...primaryLinks, ...managementLinks, settingsLink].map((link) => link.href);

	function isRouteActive(href: string, pathname: string): boolean {
		if (href === '/') return pathname === '/';
		if (pathname === href) return true;
		if (!pathname.startsWith(`${href}/`)) return false;
		return !allNavHrefs.some((other) =>
			other !== href
			&& other.length > href.length
			&& (pathname === other || pathname.startsWith(`${other}/`))
		);
	}

	// Depends on the metric/pulse stores too (not just navigation): while a
	// route is active its indicators are seen by definition, so a heartbeat
	// refresh must not resurrect a badge for the page the operator is already
	// reading. markNavIndicatorSeen no-ops when already seen/clear.
	function markActiveRouteSeen(currentPath: string, ..._deps: unknown[]): void {
		allNavHrefs.forEach((href) => {
			if (isRouteActive(href, currentPath)) {
				markNavIndicatorSeen(href);
			}
		});
	}

	$: markActiveRouteSeen($page.url.pathname, $navRouteMetrics, $navEventPulses);
</script>

<aside class="relative z-40 w-60 flex-shrink-0 border-r border-[#222] bg-black flex flex-col">
	<div class="px-3 py-4 border-b border-[#222] flex items-center justify-center gap-2">
		<div class="w-2 h-2 bg-white shrink-0" title="forven"></div>
		<div class="text-sm font-mono lowercase tracking-wide text-white">forven</div>
	</div>

	<nav aria-label="Primary navigation" class="flex-1 overflow-y-auto px-2 py-4 flex flex-col gap-4">
		<div class="space-y-1">
			{#each primaryLinks as link}
				{@const isActive = isRouteActive(link.href, $page.url.pathname)}
				<a
					href={link.href}
					on:click={() => markNavIndicatorSeen(link.href)}
					data-sveltekit-preload-data="hover"
					aria-label={link.label}
					aria-current={isActive ? 'page' : undefined}
					title={link.label}
					class="group flex min-h-[48px] w-full items-center justify-start gap-3 border-l-2 px-3 py-2 transition-colors {isActive ? 'border-l-white text-white bg-[#111]' : 'border-l-transparent text-[#888] hover:text-white hover:bg-[#111]'}"
				>
					<svg class="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
						<path d={link.icon} />
					</svg>
					<div class="min-w-0 flex-1">
						<div class="truncate text-[11px] font-medium tracking-wide">{link.label}</div>
					</div>
					<NavBadge metric={$navRouteMetrics[link.href]} pulse={$navEventPulses[link.href]} active={isActive} />
				</a>
			{/each}
		</div>

		<section class="mt-auto border-t border-[#222] pt-3">
			<div class="px-2 pb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-[#666]">Management</div>
			<div class="space-y-1">
				{#each managementLinks as link}
					{@const isActive = isRouteActive(link.href, $page.url.pathname)}
					<a
						href={link.href}
						on:click={() => markNavIndicatorSeen(link.href)}
						data-sveltekit-preload-data="hover"
						aria-label={link.label}
						aria-current={isActive ? 'page' : undefined}
						title={link.label}
						class="group flex min-h-[48px] w-full items-center justify-start gap-3 border-l-2 px-3 py-2 transition-colors {isActive ? 'border-l-white text-white bg-[#111]' : 'border-l-transparent text-[#888] hover:text-white hover:bg-[#111]'}"
					>
						<svg class="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<path d={link.icon} />
						</svg>
						<div class="min-w-0 flex-1">
							<div class="truncate text-[11px] font-medium tracking-wide">{link.label}</div>
						</div>
						<NavBadge metric={$navRouteMetrics[link.href]} pulse={$navEventPulses[link.href]} active={isActive} />
					</a>
				{/each}
			</div>
		</section>
	</nav>

	<div class="px-2 pb-3 border-t border-[#222]">
		<a
			href={settingsLink.href}
			on:click={() => markNavIndicatorSeen(settingsLink.href)}
			data-sveltekit-preload-data="hover"
			aria-label={settingsLink.label}
			aria-current={isRouteActive(settingsLink.href, $page.url.pathname) ? 'page' : undefined}
			title={settingsLink.label}
			class="group my-2 flex min-h-[48px] w-full items-center justify-start gap-3 border-l-2 px-3 py-2 transition-colors {isRouteActive(settingsLink.href, $page.url.pathname) ? 'border-l-white text-white bg-[#111]' : 'border-l-transparent text-[#888] hover:text-white hover:bg-[#111]'}"
		>
			<svg class="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d={settingsLink.icon} />
			</svg>
			<div class="min-w-0 flex-1">
				<div class="truncate text-[11px] font-medium tracking-wide">{settingsLink.label}</div>
			</div>
			<NavBadge
				metric={$navRouteMetrics[settingsLink.href]}
				pulse={$navEventPulses[settingsLink.href]}
				active={isRouteActive(settingsLink.href, $page.url.pathname)}
			/>
		</a>
	</div>

	<div class="px-3 pb-2 text-center">
		<a
			href="https://github.com/judder659/Forven"
			target="_blank"
			rel="noopener noreferrer"
			class="text-[10px] text-[#555] hover:text-[#888] transition-colors"
			title="Forven source code (AGPL-3.0)"
		>Source · AGPL-3.0</a>
	</div>

	<div class="px-2 py-3 border-t border-[#222] flex items-center justify-center">
		<div class="flex items-center">
			<div
				class="w-2 h-2 rounded-full"
				title={`Status: ${connectionStatus}`}
				class:bg-emerald-400={connectionStatus === 'connected'}
				class:bg-red-500={connectionStatus === 'disconnected'}
				class:bg-yellow-400={connectionStatus === 'checking'}
			></div>
		</div>
	</div>
</aside>
