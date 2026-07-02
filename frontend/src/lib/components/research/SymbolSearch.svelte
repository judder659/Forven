<script lang="ts">
	// Debounced symbol typeahead for the Data Manager fetch form. Replaces the
	// blind "type the exact ticker" input: queries the selected source/exchange's
	// live market list and lets the user pick the canonical symbol. Free typing
	// still works (value stays bound to the raw text) so power users can paste a
	// symbol and fetch without selecting from the list.
	import { onDestroy } from 'svelte';
	import { getSourceSymbols, type SourceSymbol } from '$lib/api';

	export let value = '';
	export let source: string; // 'ccxt' | 'binance'
	export let exchange: string | undefined = undefined;
	export let placeholder = 'Search symbol…';
	export let disabled = false;
	export let inputId: string | undefined = undefined;

	let results: SourceSymbol[] = [];
	let open = false;
	let loading = false;
	let activeIndex = -1;
	let debounceTimer: ReturnType<typeof setTimeout> | null = null;
	let blurTimer: ReturnType<typeof setTimeout> | null = null;
	let reqToken = 0;

	const idBase = inputId || 'symbol-search';
	$: listboxId = `${idBase}-listbox`;
	const optionId = (i: number) => `${idBase}-opt-${i}`;

	// Re-search (or clear stale results) when the selected exchange changes, so a
	// symbol from the *previous* exchange can't be left selectable. Guarded so it
	// never fires on initial mount.
	let lastExchange = exchange;
	$: if (exchange !== lastExchange) {
		lastExchange = exchange;
		reqToken++; // drop any in-flight response from the old exchange
		results = [];
		if (open && value.trim()) scheduleSearch(value);
	}

	onDestroy(() => {
		if (debounceTimer) clearTimeout(debounceTimer);
		if (blurTimer) clearTimeout(blurTimer);
	});

	function clearBlurTimer() {
		if (blurTimer) {
			clearTimeout(blurTimer);
			blurTimer = null;
		}
	}

	function scheduleSearch(raw: string) {
		if (debounceTimer) clearTimeout(debounceTimer);
		const term = raw.trim();
		if (term.length < 1) {
			reqToken++; // invalidate any in-flight response so it can't re-open the list
			results = [];
			open = false;
			loading = false;
			return;
		}
		loading = true;
		open = true;
		debounceTimer = setTimeout(() => runSearch(term), 200);
	}

	async function runSearch(term: string) {
		const token = ++reqToken;
		try {
			const r = await getSourceSymbols(source, term, exchange);
			if (token !== reqToken) return; // a newer keystroke / pick / exchange-change superseded this
			results = r;
			activeIndex = r.length ? 0 : -1;
			open = true;
		} catch {
			if (token !== reqToken) return;
			results = [];
		} finally {
			if (token === reqToken) loading = false;
		}
	}

	function onInput(e: Event) {
		clearBlurTimer();
		value = (e.target as HTMLInputElement).value;
		scheduleSearch(value);
	}

	function pick(s: SourceSymbol) {
		reqToken++; // invalidate any in-flight response so it can't re-open the list after we close it
		value = s.symbol;
		open = false;
		results = [];
		activeIndex = -1;
		loading = false;
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown' && (!open || !results.length)) {
			e.preventDefault();
			if (results.length) open = true; // reopen cached results without a redundant search
			else if (value.trim()) scheduleSearch(value);
			return;
		}
		if (!open || !results.length) return;
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			activeIndex = (activeIndex + 1) % results.length;
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			activeIndex = (activeIndex - 1 + results.length) % results.length;
		} else if (e.key === 'Enter') {
			if (activeIndex >= 0) {
				e.preventDefault();
				pick(results[activeIndex]);
			}
		} else if (e.key === 'Escape') {
			open = false;
		}
	}

	function onFocus() {
		clearBlurTimer();
		if (results.length) open = true;
		else if (value.trim()) scheduleSearch(value);
	}

	function onBlur() {
		// Delay so a mousedown on a result still registers before we close.
		blurTimer = setTimeout(() => (open = false), 120);
	}
</script>

<div class="relative">
	<!-- magnifier affordance: signals the field is searchable even when pre-filled -->
	<svg
		class="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#666]"
		viewBox="0 0 20 20"
		fill="none"
		stroke="currentColor"
		stroke-width="1.6"
		aria-hidden="true"
	>
		<circle cx="9" cy="9" r="6" />
		<path d="M14 14l4 4" stroke-linecap="round" />
	</svg>
	<input
		id={inputId}
		class="terminal-input"
		style="padding-left: 1.75rem"
		{placeholder}
		{disabled}
		{value}
		autocomplete="off"
		role="combobox"
		aria-expanded={open}
		aria-autocomplete="list"
		aria-controls={listboxId}
		aria-activedescendant={open && activeIndex >= 0 ? optionId(activeIndex) : undefined}
		on:input={onInput}
		on:keydown={onKeydown}
		on:focus={onFocus}
		on:blur={onBlur}
	/>
	{#if loading}
		<span class="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-[#666]">…</span>
	{/if}

	{#if open && results.length}
		<ul
			id={listboxId}
			role="listbox"
			class="absolute z-30 mt-1 max-h-64 w-full overflow-auto border border-[#222] bg-[#050505]"
		>
			{#each results as s, i (s.symbol)}
				<li id={optionId(i)} role="option" aria-selected={i === activeIndex}>
					<button
						type="button"
						class="flex w-full items-center justify-between gap-2 px-2 py-1 text-left text-xs {i ===
						activeIndex
							? 'bg-[#161616]'
							: 'hover:bg-[#111]'}"
						on:mousedown|preventDefault={() => pick(s)}
						on:mouseenter={() => (activeIndex = i)}
					>
						<span class="font-mono text-white">{s.symbol}</span>
						<span class="flex items-center gap-1.5 text-[10px] text-[#666]">
							{#if s.base && s.quote}<span>{s.base}/{s.quote}</span>{/if}
							{#if s.active === false}<span class="text-red-400">delisted</span>{/if}
						</span>
					</button>
				</li>
			{/each}
		</ul>
	{:else if open && !loading && value.trim()}
		<div
			class="absolute z-30 mt-1 w-full border border-[#222] bg-[#050505] px-2 py-1.5 text-[11px] text-[#666]"
		>
			No matches on {exchange || source}.
		</div>
	{/if}
</div>
