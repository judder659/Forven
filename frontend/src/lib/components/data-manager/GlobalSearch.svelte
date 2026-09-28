<script lang="ts">
	// Header symbol search: any spelling (BTC, BTCUSDT, btc/usdt) resolves to
	// instruments with what is stored for them; Enter jumps to the best series,
	// or to Get data when nothing is stored yet.
	import { goto } from '$app/navigation';
	import { onDestroy } from 'svelte';
	import { dismissable } from '$lib/actions/dismissable';
	import { isRouteMissingError } from '$lib/api/core';
	import { resolveIdentity } from '$lib/api/dataManager';
	import type { SymbolCandidate } from '$lib/api/dataManagerTypes';
	import { createRequestGuard } from '$lib/stores/dataManager';
	import { streamLabel } from './format';
	import { candidateHref, catalogHref } from './links';

	/** Bound by the layout so "/" can focus it. */
	export let input: HTMLInputElement | undefined = undefined;

	type Option = { id: string; href: string; label: string; detail: string; tag?: string };
	const guard = createRequestGuard();
	let q = '';
	let open = false;
	let status: 'idle' | 'loading' | 'ready' | 'unavailable' | 'error' = 'idle';
	let candidates: SymbolCandidate[] = [];
	let activeIndex = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	onDestroy(() => {
		clearTimeout(timer);
		guard.cancel();
	});

	function storedText(c: SymbolCandidate): string {
		const candles = c.stored.filter((s) => s.stream === 'ohlcv' && s.rows > 0).map((s) => s.timeframe);
		const streams = [...new Set(c.stored.filter((s) => s.stream !== 'ohlcv' && s.rows > 0).map((s) => streamLabel(s.stream).toLowerCase()))];
		if (!candles.length && !streams.length) return 'not stored yet · get it';
		return [candles.length ? `candles ${[...new Set(candles)].join(' ')}` : '', streams.slice(0, 3).join(', ')].filter(Boolean).join(' · ');
	}

	$: options = [
		...candidates.map((c): Option => ({
			id: `dm-search-${c.symbol}`,
			href: candidateHref(c),
			label: c.display_symbol,
			detail: storedText(c),
			tag: c.delisted ? 'delisted' : c.asset_class !== 'crypto' ? c.asset_class : undefined,
		})),
		...(q.trim() ? [{ id: 'dm-search-catalog', href: catalogHref({ q: q.trim() }), label: `Search the catalog for “${q.trim()}”`, detail: '' }] : []),
	];
	$: if (activeIndex >= options.length) activeIndex = Math.max(0, options.length - 1);

	function onInput() {
		open = true;
		clearTimeout(timer);
		timer = setTimeout(search, 180);
	}

	async function search() {
		const query = q.trim();
		if (!query) {
			guard.cancel();
			candidates = [];
			status = 'idle';
			return;
		}
		const { signal, current } = guard.next();
		status = 'loading';
		try {
			const result = await resolveIdentity(query, signal);
			if (!current()) return;
			candidates = result.candidates.slice(0, 7);
			status = 'ready';
			activeIndex = 0;
		} catch (error) {
			if (!current()) return;
			candidates = [];
			status = isRouteMissingError(error) ? 'unavailable' : 'error';
		}
	}

	function choose(option: Option | undefined) {
		if (!option) return;
		open = false;
		q = '';
		candidates = [];
		input?.blur();
		void goto(option.href);
	}

	function onKey(event: KeyboardEvent) {
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			open = true;
			const delta = event.key === 'ArrowDown' ? 1 : -1;
			activeIndex = (activeIndex + delta + options.length) % Math.max(1, options.length);
		} else if (event.key === 'Enter') {
			event.preventDefault();
			choose(options[activeIndex]);
		} else if (event.key === 'Escape') {
			open = false;
			input?.blur();
		}
	}
</script>

<div class="relative" use:dismissable={() => (open = false)}>
	<input bind:this={input} bind:value={q} on:input={onInput} on:keydown={onKey} on:focus={() => (open = true)}
		type="search" placeholder="Find a market…" autocomplete="off" spellcheck="false"
		role="combobox" aria-label="Find a market" aria-expanded={open && !!q.trim()} aria-controls="dm-search-list"
		aria-autocomplete="list" aria-activedescendant={open && options[activeIndex] ? options[activeIndex].id : undefined}
		class="w-56 border border-[#2a2a2a] bg-black py-1 pl-2 pr-7 font-mono text-[12px] text-white outline-none placeholder:text-[#555] focus:border-white" />
	<kbd class="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 border border-[#333] px-1 text-[9px] text-[#666]" aria-hidden="true">/</kbd>
	{#if open && q.trim()}
		<ul id="dm-search-list" role="listbox" aria-label="Markets"
			class="absolute right-0 top-full z-50 mt-1 w-[380px] border border-[#333] bg-[#050505] py-1 shadow-[0_12px_32px_rgba(0,0,0,0.7)]">
			{#if status === 'loading' && !candidates.length}
				<li class="px-3 py-2 text-[11px] text-[#666]" role="presentation">Looking up “{q.trim()}”…</li>
			{:else if status === 'unavailable'}
				<li class="px-3 py-2 text-[11px] text-[#666]" role="presentation">Symbol lookup is not available on this backend yet. The catalog search below still works.</li>
			{:else if status === 'error'}
				<li class="px-3 py-2 text-[11px] text-red-400" role="presentation">Lookup failed. Try the catalog search.</li>
			{:else if status === 'ready' && !candidates.length}
				<li class="px-3 py-2 text-[11px] text-[#666]" role="presentation">No market matches “{q.trim()}”.</li>
			{/if}
			{#each options as option, i (option.id)}
				<li id={option.id} role="option" aria-selected={i === activeIndex}>
					<button type="button" tabindex="-1" on:mousedown|preventDefault={() => choose(option)} on:mouseenter={() => (activeIndex = i)}
						class="flex w-full items-baseline gap-2 px-3 py-1.5 text-left {i === activeIndex ? 'bg-[#161616]' : ''}">
						<span class="shrink-0 text-[12px] {option.detail ? 'font-bold text-white' : 'text-[#aaa]'}">{option.label}</span>
						{#if option.tag}<span class="shrink-0 border border-[#333] px-1 text-[9px] uppercase tracking-wider text-[#888]">{option.tag}</span>{/if}
						<span class="min-w-0 flex-1 truncate text-right text-[10px] text-[#666]">{option.detail}</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>
