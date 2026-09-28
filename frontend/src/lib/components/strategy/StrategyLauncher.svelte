<script lang="ts">
	// One place to start from: a blank canvas, a template, a saved strategy
	// (with its library actions), or a strategy already in the system.
	import { createEventDispatcher, onMount, tick } from 'svelte';
	import type { LibraryStrategy, Strategy } from '$lib/api';
	import type { StrategyTemplate } from './templates';

	export let library: LibraryStrategy[] = [];
	export let libraryLoading = false;
	export let templates: StrategyTemplate[] = [];
	export let prebuilt: Strategy[] = [];
	export let appStrategies: Strategy[] = [];
	export let includeAppGenerated = false;
	export let appLoading = false;
	export let currentLibraryId: string | null = null;
	export let forging = false;

	type Tab = 'library' | 'templates' | 'prebuilt' | 'app';
	export let tab: Tab = 'library';

	const dispatch = createEventDispatcher<{
		close: void;
		blank: void;
		template: string;
		openEntry: LibraryStrategy;
		duplicate: { entry: LibraryStrategy; event: Event };
		remove: { entry: LibraryStrategy; event: Event };
		forge: { entry: LibraryStrategy; event: Event };
		system: { source: 'pre' | 'app'; id: string };
		toggleApp: void;
		import: void;
	}>();

	let search = '';
	let searchInput: HTMLInputElement | undefined;
	onMount(async () => {
		await tick();
		searchInput?.focus();
	});

	$: q = search.trim().toLowerCase();
	const hit = (...parts: Array<string | null | undefined>) => !q || parts.some((part) => (part ?? '').toLowerCase().includes(q));
	$: libraryShown = library.filter((entry) => hit(entry.name, entry.description, entry.symbol, entry.status, entry.kind));
	$: templatesShown = templates.filter((t) => hit(t.name, t.category, t.description, t.symbol));
	$: prebuiltShown = prebuilt.filter((s) => hit(s.name, s.description, s.api_name));
	$: appShown = appStrategies.filter((s) => hit(s.name, s.description, s.api_name));

	$: tabs = [
		{ key: 'library' as Tab, label: `My Strategies (${library.length})` },
		{ key: 'templates' as Tab, label: `Templates (${templates.length})` },
		{ key: 'prebuilt' as Tab, label: `Prebuilt (${prebuilt.length})` },
		{ key: 'app' as Tab, label: 'App-generated' },
	];

	function onKey(event: KeyboardEvent) {
		if (event.key === 'Escape') dispatch('close');
	}
</script>

<svelte:window on:keydown={onKey} />

<div class="fixed inset-0 z-50 flex items-start justify-center bg-black/70 px-4 pt-[8vh]" role="presentation"
	on:pointerdown={(e) => { if (e.target === e.currentTarget) dispatch('close'); }}>
	<div role="dialog" aria-modal="true" aria-label="Open a strategy"
		class="flex h-[76vh] w-full max-w-4xl flex-col border border-[#333] bg-[#060606] shadow-2xl shadow-black">
		<div class="flex items-center gap-2 border-b border-[#1c1c1c] p-3">
			<input bind:this={searchInput} bind:value={search} placeholder="Search strategies, templates, markets…" aria-label="search strategies"
				class="min-w-0 flex-1 border border-[#333] bg-black px-2 py-1.5 text-[13px] text-white outline-none focus:border-white" />
			<button type="button" on:click={() => dispatch('blank')} class="terminal-button text-[10px]">✦ Blank canvas</button>
			<button type="button" data-testid="creator-import-strategy" on:click={() => dispatch('import')}
				title="Import a strategy export as a new quick_screen container" class="terminal-button text-[10px]">Import…</button>
			<button type="button" on:click={() => dispatch('close')} aria-label="Close" class="px-2 text-[#555] hover:text-white">✕</button>
		</div>
		<div class="flex min-h-0 flex-1">
			<nav class="w-44 shrink-0 border-r border-[#1c1c1c] py-2" aria-label="sources">
				{#each tabs as t}
					<button type="button" on:click={() => (tab = t.key)} aria-pressed={tab === t.key}
						class="block w-full px-3 py-1.5 text-left text-[11px] {tab === t.key ? 'bg-white text-black' : 'text-[#888] hover:text-white'}">{t.label}</button>
				{/each}
			</nav>
			<div class="min-w-0 flex-1 overflow-y-auto p-3">
				{#if tab === 'library'}
					{#if libraryLoading && !library.length}
						<div class="text-[11px] uppercase tracking-widest text-[#555]">Loading…</div>
					{:else if !libraryShown.length}
						<div class="border border-dashed border-[#333] p-6 text-center text-[12px] text-[#555]">
							{library.length ? `Nothing matches “${search}”.` : 'No saved strategies yet. Build one and save it to your library.'}
						</div>
					{:else}
						<div class="space-y-2">
							{#each libraryShown as entry (entry.id)}
								<div class="border bg-[#050505] p-2.5 transition-colors hover:border-[#666] {currentLibraryId === entry.id ? 'border-white' : 'border-[#222]'}">
									<button type="button" on:click={() => dispatch('openEntry', entry)} class="block w-full text-left">
										<div class="flex items-center justify-between gap-2">
											<span class="truncate text-[13px] text-white">{entry.name}</span>
											<span class="shrink-0 border border-[#333] px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-[#888]">{entry.status}</span>
										</div>
										<div class="mt-0.5 truncate text-[11px] text-[#555]">
											{entry.kind === 'code' ? 'python' : 'rules'} · {entry.symbol} {entry.timeframe} · v{entry.version}{entry.description ? ` · ${entry.description}` : ''}
										</div>
									</button>
									<div class="mt-1.5 flex items-center gap-3 text-[11px]">
										<button type="button" class="text-white" data-testid={`library-open-${entry.id}`} on:click={() => dispatch('openEntry', entry)}>Open</button>
										<button type="button" class="text-[#666] hover:text-white" on:click={(event) => dispatch('duplicate', { entry, event })}>Duplicate</button>
										{#if entry.forge_strategy_id}
											<a class="text-emerald-400 hover:text-white" href={`/lab/strategy/${encodeURIComponent(entry.forge_strategy_id)}`}>In Forge →</a>
										{:else}
											<button type="button" class="text-[#888] hover:text-white disabled:opacity-40" disabled={forging}
												on:click={(event) => dispatch('forge', { entry, event })}>→ Forge</button>
										{/if}
										<button type="button" class="ml-auto text-[#555] hover:text-red-400" on:click={(event) => dispatch('remove', { entry, event })}>Delete</button>
									</div>
								</div>
							{/each}
						</div>
					{/if}
				{:else if tab === 'templates'}
					<div class="grid gap-2 sm:grid-cols-2">
						{#each templatesShown as t (t.id)}
							<button type="button" on:click={() => dispatch('template', t.id)}
								class="border border-[#222] bg-[#050505] p-2.5 text-left transition-colors hover:border-white">
								<div class="flex items-baseline justify-between gap-2">
									<span class="text-[13px] text-white">{t.name}</span>
									<span class="shrink-0 text-[9px] uppercase tracking-wider text-[#555]">{t.category}</span>
								</div>
								<div class="mt-1 text-[11px] leading-4 text-[#777]">{t.description}</div>
								<div class="mt-1.5 text-[10px] text-[#555]">{t.symbol} · {t.timeframe} · {t.trade_mode.replace('_', ' ')}</div>
							</button>
						{/each}
					</div>
				{:else if tab === 'prebuilt'}
					<p class="mb-2 text-[11px] text-[#666]">Strategies shipped with Forven. Rule-based ones open as an editable copy.</p>
					<div class="divide-y divide-[#111] border border-[#161616]">
						{#each prebuiltShown as s (s.api_name || s.name)}
							<button type="button" on:click={() => dispatch('system', { source: 'pre', id: s.api_name || s.name })}
								class="block w-full px-2.5 py-1.5 text-left hover:bg-[#101010]">
								<div class="text-[12px] text-white">{s.name}</div>
								{#if s.description}<div class="truncate text-[11px] text-[#555]">{s.description}</div>{/if}
							</button>
						{/each}
						{#if !prebuiltShown.length}<div class="px-2.5 py-4 text-center text-[12px] text-[#555]">No prebuilt strategies{q ? ' match' : ''}.</div>{/if}
					</div>
				{:else}
					<label class="mb-2 inline-flex items-center gap-1.5 text-[11px] text-[#888]">
						<input type="checkbox" checked={includeAppGenerated} on:change={() => dispatch('toggleApp')} class="accent-white" />
						Show strategies the app generated{#if appLoading}…{/if}
					</label>
					{#if includeAppGenerated}
						<div class="divide-y divide-[#111] border border-[#161616]">
							{#each appShown as s (s.api_name || s.name)}
								<button type="button" on:click={() => dispatch('system', { source: 'app', id: s.api_name || s.name })}
									class="block w-full px-2.5 py-1.5 text-left hover:bg-[#101010]">
									<div class="text-[12px] text-white">{s.name}</div>
									{#if s.description}<div class="truncate text-[11px] text-[#555]">{s.description}</div>{/if}
								</button>
							{/each}
							{#if !appShown.length && !appLoading}<div class="px-2.5 py-4 text-center text-[12px] text-[#555]">No app-generated strategies{q ? ' match' : ''}.</div>{/if}
						</div>
					{/if}
				{/if}
			</div>
		</div>
	</div>
</div>
