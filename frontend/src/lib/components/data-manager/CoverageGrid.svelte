<script lang="ts">
	// Symbols × timeframes for one stream. Cell colour is the server's SLA state
	// (or history depth), the label is how much history is stored, and a mark
	// repeats the state for anyone who cannot rely on colour. Click opens the
	// series; drag or shift-click selects a rectangle; ctrl-click toggles.
	// Keyboard: arrows move, shift+arrows extend, Enter opens, Space toggles.
	import { createEventDispatcher, onDestroy } from 'svelte';
	import type { SlaState } from '$lib/api/dataManagerTypes';
	import { depthShade, rectangle, type CoverageCell, type CoverageModel } from './coverage';
	import { formatCount, historyLength, lagCaption, STATE_LABEL, TIER_HELP, TIER_LABEL } from './format';

	export let model: CoverageModel;
	export let mode: 'freshness' | 'depth' = 'freshness';
	export let selected: Set<string>;

	const dispatch = createEventDispatcher<{ open: CoverageCell; select: string[] }>();
	const CELL: Record<SlaState, string> = {
		fresh: 'bg-emerald-500/[0.16] text-emerald-200',
		late: 'bg-amber-400/[0.22] text-amber-100',
		breach: 'bg-red-500/[0.26] text-red-100',
		frozen: 'text-slate-400 bg-[repeating-linear-gradient(135deg,rgba(148,163,184,0.22)_0_2px,transparent_2px_5px)]',
		missing: 'border border-dashed border-[#555] text-[#888]',
	};
	const MARK: Partial<Record<SlaState, string>> = { late: '!', breach: '‼', frozen: '*', missing: '∅' };

	let root: HTMLDivElement | undefined;
	let active: [number, number] = [0, 0];
	let anchor: [number, number] | null = null;
	let dragFrom: [number, number] | null = null;
	let dragged = false;

	$: rowsFlat = model.flat;
	$: if (active[0] >= rowsFlat.length || active[1] >= (rowsFlat[0]?.length ?? 0)) active = [0, 0];
	$: rowIndex = new Map(model.groups.flatMap((g) => g.symbols).map((s, i) => [s.symbol, i]));
	$: activeKey = rowsFlat[active[0]]?.[active[1]]?.key ?? '';

	const cellId = (key: string) => `dm-cov-${key.replace(/[^A-Za-z0-9]/g, '_')}`;

	function label(cell: CoverageCell): string {
		if (cell.row) {
			const r = cell.row;
			const state = STATE_LABEL[r.sla.state];
			return `${cell.symbol} ${cell.timeframe}: ${state} (${lagCaption(r.sla)} behind / allowed), ${historyLength(r.first_ts, r.last_ts)} of history, ${formatCount(r.rows)} rows`;
		}
		return cell.planned ? `${cell.symbol} ${cell.timeframe}: planned for the research universe, not stored` : `${cell.symbol} ${cell.timeframe}: not stored`;
	}

	function focusCell(pos: [number, number]) {
		active = pos;
		const key = rowsFlat[pos[0]]?.[pos[1]]?.key;
		if (key) document.getElementById(cellId(key))?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
	}

	function onKey(event: KeyboardEvent) {
		const [r, c] = active;
		const rows = rowsFlat.length;
		const cols = rowsFlat[0]?.length ?? 0;
		const moves: Record<string, [number, number]> = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
		const move = moves[event.key];
		if (move) {
			event.preventDefault();
			const next: [number, number] = [Math.max(0, Math.min(rows - 1, r + move[0])), Math.max(0, Math.min(cols - 1, c + move[1]))];
			if (event.shiftKey) {
				anchor ??= [r, c];
				dispatch('select', rectangle(rowsFlat, anchor, next));
			} else anchor = null;
			focusCell(next);
			return;
		}
		const cell = rowsFlat[r]?.[c];
		if (!cell) return;
		if (event.key === 'Enter' && cell.row) {
			event.preventDefault();
			dispatch('open', cell);
		} else if (event.key === ' ') {
			event.preventDefault();
			anchor = [r, c];
			dispatch('select', selected.has(cell.key) ? [...selected].filter((k) => k !== cell.key) : [...selected, cell.key]);
		} else if (event.key === 'Escape' && selected.size) {
			event.preventDefault();
			dispatch('select', []);
		}
	}

	function down(event: MouseEvent, pos: [number, number]) {
		if (event.button !== 0 || event.shiftKey || event.ctrlKey || event.metaKey) return;
		dragFrom = pos;
		dragged = false;
	}
	function enter(pos: [number, number]) {
		if (!dragFrom) return;
		if (pos[0] !== dragFrom[0] || pos[1] !== dragFrom[1]) dragged = true;
		if (dragged) dispatch('select', rectangle(rowsFlat, dragFrom, pos));
	}
	function click(event: MouseEvent, cell: CoverageCell, pos: [number, number]) {
		active = pos;
		root?.focus({ preventScroll: true });
		if (dragged) {
			dragged = false;
			anchor = dragFrom;
			return;
		}
		if (event.shiftKey && anchor) dispatch('select', rectangle(rowsFlat, anchor, pos));
		else if (event.ctrlKey || event.metaKey) {
			anchor = pos;
			dispatch('select', selected.has(cell.key) ? [...selected].filter((k) => k !== cell.key) : [...selected, cell.key]);
		} else if (cell.row) dispatch('open', cell);
		else {
			anchor = pos;
			dispatch('select', selected.has(cell.key) ? [...selected].filter((k) => k !== cell.key) : [...selected, cell.key]);
		}
	}
	const up = () => (dragFrom = null);
	if (typeof window !== 'undefined') window.addEventListener('mouseup', up);
	onDestroy(() => {
		if (typeof window !== 'undefined') window.removeEventListener('mouseup', up);
	});

	function cellClass(cell: CoverageCell): string {
		if (cell.row && cell.row.rows > 0) {
			if (mode === 'depth') return 'text-white';
			return CELL[cell.row.sla.state] ?? '';
		}
		if (cell.row) return CELL.missing;
		if (cell.planned) return 'border border-dashed border-sky-800 text-sky-300/80';
		return 'text-[#2f2f2f]';
	}
	$: template = `minmax(128px, 168px) repeat(${model.timeframes.length}, minmax(50px, 60px))`;
</script>

<div bind:this={root} role="grid" tabindex="0" aria-label="Coverage: symbols by timeframe" aria-rowcount={rowsFlat.length + 1} aria-colcount={model.timeframes.length + 1}
	aria-activedescendant={activeKey ? cellId(activeKey) : undefined} on:keydown={onKey} data-testid="coverage-grid"
	class="relative min-h-0 flex-1 select-none overflow-auto border border-[#222] bg-[#050505] outline-none focus-visible:border-[#666]">
	<div class="grid w-max min-w-full" style="grid-template-columns: {template}">
		<div role="row" aria-rowindex={1} class="contents">
			<div role="columnheader" class="sticky left-0 top-0 z-30 border-b border-r border-[#1a1a1a] bg-[#0a0a0a] px-3 py-1.5 text-[9px] uppercase tracking-wider text-[#555]">Symbol</div>
			{#each model.timeframes as tf (tf)}
				<div role="columnheader" class="sticky top-0 z-20 border-b border-[#1a1a1a] bg-[#0a0a0a] py-1.5 text-center font-mono text-[10px] text-[#888]">{tf}</div>
			{/each}
		</div>
		{#each model.groups as group (group.tier)}
			<div role="row" class="contents">
				<div role="rowheader" title={TIER_HELP[group.tier]}
					class="sticky left-0 top-[27px] z-10 col-span-full border-y border-[#1a1a1a] bg-[#0c0c0c] px-3 py-1 text-[9px] font-bold uppercase tracking-wider text-[#aaa]" style="grid-column: 1 / -1">
					{TIER_LABEL[group.tier]} <span class="font-normal text-[#555]">· {group.symbols.length} symbol{group.symbols.length === 1 ? '' : 's'}</span>
				</div>
			</div>
			{#each group.symbols as sym (sym.symbol)}
				{@const r = rowIndex.get(sym.symbol) ?? 0}
				<div role="row" aria-rowindex={r + 2} class="contents">
					<div role="rowheader" class="sticky left-0 z-[5] truncate border-b border-r border-[#111] bg-[#050505] px-3 py-[3px] text-[11px] text-white" title={sym.symbol}>{sym.display}</div>
					{#each sym.cells as cell, c (cell.key)}
						{@const isSel = selected.has(cell.key)}
						{@const isActive = active[0] === r && active[1] === c}
						<!-- Keyboard: the grid handles arrows, Enter and Space for the active cell. -->
						<!-- svelte-ignore a11y-click-events-have-key-events -->
						<div id={cellId(cell.key)} role="gridcell" tabindex="-1" aria-selected={isSel} aria-label={label(cell)} title={label(cell)}
							on:mousedown={(e) => down(e, [r, c])} on:mouseenter={() => enter([r, c])} on:click={(e) => click(e, cell, [r, c])}
							class="relative m-[1px] flex h-[20px] cursor-pointer items-center justify-center font-mono text-[10px] tabular-nums {cellClass(cell)} {isSel ? 'outline outline-1 outline-white' : ''} {isActive ? 'ring-1 ring-inset ring-sky-400' : ''}"
							style={mode === 'depth' && cell.row && cell.row.rows > 0 ? `background: rgba(147, 197, 253, ${0.08 + 0.55 * depthShade(cell.row.first_ts, cell.row.last_ts)})` : ''}>
							{#if cell.row && cell.row.rows > 0}
								{historyLength(cell.row.first_ts, cell.row.last_ts)}{#if mode === 'freshness' && MARK[cell.row.sla.state]}<span class="ml-px text-[9px]" aria-hidden="true">{MARK[cell.row.sla.state]}</span>{/if}
							{:else if cell.row}
								none
							{:else if cell.planned}
								planned
							{:else}
								·
							{/if}
						</div>
					{/each}
				</div>
			{/each}
		{/each}
	</div>
</div>
