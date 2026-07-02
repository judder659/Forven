<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher<{ change: { spec: Record<string, unknown>; valid: boolean; errors: string[] } }>();

	export let disabled = false;

	// --- Indicator + operator catalogs (kept in sync with the backend rule_engine) ---
	type IndDef = { label: string; params: { key: string; def: number }[]; outputs: (id: string) => string[] };
	const INDICATOR_DEFS: Record<string, IndDef> = {
		rsi: { label: 'RSI', params: [{ key: 'length', def: 14 }], outputs: (id) => [id] },
		ema: { label: 'EMA', params: [{ key: 'length', def: 20 }], outputs: (id) => [id] },
		sma: { label: 'SMA', params: [{ key: 'length', def: 20 }], outputs: (id) => [id] },
		wma: { label: 'WMA', params: [{ key: 'length', def: 20 }], outputs: (id) => [id] },
		macd: { label: 'MACD', params: [{ key: 'fast', def: 12 }, { key: 'slow', def: 26 }, { key: 'signal', def: 9 }], outputs: (id) => [id, `${id}_signal`, `${id}_hist`] },
		atr: { label: 'ATR', params: [{ key: 'length', def: 14 }], outputs: (id) => [id] },
		bollinger: { label: 'Bollinger', params: [{ key: 'length', def: 20 }, { key: 'num_std', def: 2 }], outputs: (id) => [`${id}_upper`, `${id}_mid`, `${id}_lower`] },
		stochastic: { label: 'Stochastic', params: [{ key: 'k', def: 14 }, { key: 'd', def: 3 }, { key: 'smooth', def: 3 }], outputs: (id) => [`${id}_k`, `${id}_d`] },
		roc: { label: 'Rate of Change', params: [{ key: 'length', def: 10 }], outputs: (id) => [id] },
		momentum: { label: 'Momentum', params: [{ key: 'length', def: 10 }], outputs: (id) => [id] },
		vwap: { label: 'VWAP', params: [], outputs: (id) => [id] },
	};
	// OHLCV + crypto-native enrichment columns the engine exposes (order flow,
	// funding, liquidations). Enrichment fills 0.0 when a dataset lacks it.
	const RAW_COLUMNS = [
		'close', 'open', 'high', 'low', 'volume',
		'funding_rate', 'open_interest', 'taker_buy_sell_ratio',
		'ls_ratio', 'long_liq_usd', 'short_liq_usd', 'liq_imbalance',
	];
	const OPERATORS = ['<', '<=', '>', '>=', '==', '!=', 'crosses_above', 'crosses_below'];

	type Operand = { type: 'series' | 'param' | 'const'; value: string | number };
	// `_uid` is a stable key so #each blocks survive mid-list removal without
	// mis-associating bound values (index keys + bind:value are a Svelte footgun).
	type Condition = { left: Operand; op: string; right: Operand; _uid: number };
	type Group = { logic: 'and' | 'or'; conditions: Condition[] };

	let nextUid = 1;
	function mkCond(left: Operand, op: string, right: Operand): Condition {
		return { left, op, right, _uid: nextUid++ };
	}

	// `_prevId` / `_prev` track the prior id/name so a rename can be propagated
	// to the conditions that reference it (instead of orphaning them).
	let indicators: { id: string; kind: string; params: Record<string, number>; _prevId?: string; _uid: number }[] = [
		{ id: 'rsi', kind: 'rsi', params: { length: 14 }, _prevId: 'rsi', _uid: nextUid++ },
	];
	let params: { name: string; value: number; _prev?: string; _uid: number }[] = [
		{ name: 'oversold', value: 30, _prev: 'oversold', _uid: nextUid++ },
		{ name: 'overbought', value: 70, _prev: 'overbought', _uid: nextUid++ },
	];
	let entryLong: Group = { logic: 'and', conditions: [mkCond({ type: 'series', value: 'rsi' }, '<', { type: 'param', value: 'oversold' })] };
	let exitLong: Group = { logic: 'or', conditions: [mkCond({ type: 'series', value: 'rsi' }, '>', { type: 'param', value: 'overbought' })] };
	let entryShort: Group = { logic: 'and', conditions: [] };
	let exitShort: Group = { logic: 'or', conditions: [] };
	let showShort = false;

	let nextIndN = 2;

	function indicatorOutputs(ind: { id: string; kind: string }): string[] {
		const def = INDICATOR_DEFS[ind.kind];
		return def ? def.outputs(ind.id) : [ind.id];
	}

	$: availableSeries = [
		...RAW_COLUMNS,
		...indicators.flatMap((ind) => indicatorOutputs(ind)),
	];
	$: paramNames = params.map((p) => p.name).filter(Boolean);

	function addIndicator() {
		const id = `ind${nextIndN++}`;
		indicators = [...indicators, { id, kind: 'ema', params: { length: 20 }, _prevId: id, _uid: nextUid++ }];
	}
	function removeIndicator(i: number) {
		indicators = indicators.filter((_, idx) => idx !== i);
	}

	// Re-point operands when an indicator id or parameter name is renamed, so a
	// rename never orphans a condition. Series operands also remap multi-output
	// names (e.g. bb_upper -> band_upper when id bb -> band).
	function remapOperands(kind: 'series' | 'param', oldVal: string, newVal: string) {
		if (!oldVal || oldVal === newVal) return;
		for (const g of [entryLong, exitLong, entryShort, exitShort]) {
			for (const c of g.conditions) {
				for (const o of [c.left, c.right]) {
					if (o.type !== kind || typeof o.value !== 'string') continue;
					if (o.value === oldVal) o.value = newVal;
					else if (kind === 'series' && o.value.startsWith(oldVal + '_')) o.value = newVal + o.value.slice(oldVal.length);
				}
			}
		}
	}
	function onIndicatorRenamed(i: number) {
		const ind = indicators[i];
		remapOperands('series', ind._prevId ?? '', ind.id);
		ind._prevId = ind.id;
		bump();
	}
	function onParamRenamed(i: number) {
		const p = params[i];
		remapOperands('param', p._prev ?? '', p.name);
		p._prev = p.name;
		bump();
	}
	function onKindChange(i: number, kind: string) {
		const def = INDICATOR_DEFS[kind];
		const p: Record<string, number> = {};
		for (const sp of def?.params ?? []) p[sp.key] = sp.def;
		indicators[i] = { ...indicators[i], kind, params: p };
		indicators = [...indicators];
	}
	function addParam() {
		const nm = `param${params.length + 1}`;
		params = [...params, { name: nm, value: 0, _prev: nm, _uid: nextUid++ }];
	}
	function removeParam(i: number) {
		params = params.filter((_, idx) => idx !== i);
	}

	function addCondition(group: Group) {
		group.conditions = [...group.conditions, mkCond({ type: 'series', value: availableSeries[0] ?? 'close' }, '>', { type: 'const', value: 0 })];
		bump();
	}
	function removeCondition(group: Group, i: number) {
		group.conditions = group.conditions.filter((_, idx) => idx !== i);
		bump();
	}

	// When an operand's type changes, reset its value to a sensible default for
	// the new type (otherwise a stale 'close' string lingers in a Value input).
	function onOperandTypeChange(operand: Operand) {
		if (operand.type === 'const') operand.value = 0;
		else if (operand.type === 'series') operand.value = RAW_COLUMNS[0];
		else operand.value = paramNames[0] ?? '';
		bump();
	}

	function removeShortSide() {
		showShort = false;
		entryShort = { logic: 'and', conditions: [] };
		exitShort = { logic: 'or', conditions: [] };
		bump();
	}

	// Force the derived state (availableSeries / paramNames / spec) to recompute.
	// `bind:value` on a nested field (p.name, ind.params[k], cond.left.value …)
	// mutates in place and does not reliably invalidate the parent array, so any
	// edit inside the builder bubbles an input/change event up to bump().
	function bump() {
		indicators = indicators;
		params = params;
		entryLong = entryLong;
		exitLong = exitLong;
		entryShort = entryShort;
		exitShort = exitShort;
	}

	function operandToSpec(o: Operand): unknown {
		if (o.type === 'const') return Number(o.value) || 0;
		if (o.type === 'param') return { param: String(o.value) };
		return String(o.value); // series
	}
	function groupToSpec(g: Group): Record<string, unknown> | null {
		if (!g.conditions.length) return null;
		return {
			logic: g.logic,
			conditions: g.conditions.map((c) => ({ left: operandToSpec(c.left), op: c.op, right: operandToSpec(c.right) })),
		};
	}

	// Build spec + validate reactively, and emit to the parent.
	$: spec = {
		indicators: indicators.map((ind) => ({ id: ind.id, kind: ind.kind, params: { ...ind.params } })),
		params: Object.fromEntries(params.filter((p) => p.name).map((p) => [p.name, Number(p.value)])),
		entry_long: groupToSpec(entryLong),
		exit_long: groupToSpec(exitLong),
		entry_short: showShort ? groupToSpec(entryShort) : null,
		exit_short: showShort ? groupToSpec(exitShort) : null,
	};
	// Pass deps explicitly so Svelte re-runs after bump() reassigns them.
	$: errors = validateSpec(spec, indicators, params, availableSeries, paramNames, entryLong, exitLong, entryShort, exitShort, showShort);
	$: dispatch('change', { spec, valid: errors.length === 0, errors });

	function validateSpec(
		s: typeof spec, inds: typeof indicators, _p: typeof params, series: string[], pnames: string[],
		eL: Group, xL: Group, eS: Group, xS: Group, short: boolean,
	): string[] {
		const errs: string[] = [];
		const ids = new Set<string>();
		for (const ind of inds) {
			if (!ind.id.trim()) errs.push('Every indicator needs an id.');
			else if (RAW_COLUMNS.includes(ind.id)) errs.push(`Indicator id "${ind.id}" collides with a price/data column — choose a different id.`);
			else if (ids.has(ind.id)) errs.push(`Duplicate indicator id "${ind.id}".`);
			else ids.add(ind.id);
		}
		const hasEntry = (s.entry_long && (s.entry_long as Group).conditions?.length) || (s.entry_short && (s.entry_short as Group).conditions?.length);
		if (!hasEntry) errs.push('Add at least one entry condition (long or short).');

		// Flag operands that reference a renamed/removed parameter or series.
		const seriesSet = new Set(series);
		const paramSet = new Set(pnames);
		const groups: [string, Group][] = [['Entry Long', eL], ['Exit Long', xL]];
		if (short) groups.push(['Entry Short', eS], ['Exit Short', xS]);
		for (const [label, g] of groups) {
			for (const c of g.conditions) {
				for (const o of [c.left, c.right]) {
					if (o.type === 'series' && !seriesSet.has(String(o.value))) errs.push(`${label}: unknown series "${o.value}" — pick a valid one.`);
					if (o.type === 'param' && !paramSet.has(String(o.value))) errs.push(`${label}: unknown parameter "${o.value}" — pick a valid one.`);
				}
			}
		}
		return errs;
	}

	// Reactive list bound to the actual group objects. This MUST reference the
	// group `let`s directly (not via a helper) so Svelte re-renders the rows when
	// bump() reassigns them — otherwise adding/removing conditions won't show.
	$: groupList = [
		{ key: 'entryLong', label: 'Entry — Long', short: false, group: entryLong },
		{ key: 'exitLong', label: 'Exit — Long', short: false, group: exitLong },
		{ key: 'entryShort', label: 'Entry — Short', short: true, group: entryShort },
		{ key: 'exitShort', label: 'Exit — Short', short: true, group: exitShort },
	];

	const inputCls = 'border border-[#333] bg-[#050505] px-2 py-1 text-[12px] text-white outline-none transition-colors focus:border-white disabled:opacity-40';
</script>

<!-- input/change bubble up from every control; bump() recomputes derived state -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="space-y-5" on:input={bump} on:change={bump}>
	<!-- Indicators -->
	<div>
		<div class="flex items-center justify-between">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Indicators</div>
			<button type="button" on:click={addIndicator} {disabled} class="border border-[#333] bg-[#111] px-2 py-1 text-[11px] text-[#888] transition-colors hover:border-[#555] hover:text-white disabled:opacity-40">+ Add indicator</button>
		</div>
		<div class="mt-2 space-y-2">
			{#each indicators as ind, i (ind._uid)}
				<div class="flex flex-wrap items-center gap-2 border border-[#1a1a1a] bg-[#050505] p-2">
					<input class={`${inputCls} w-24`} bind:value={ind.id} on:input={() => onIndicatorRenamed(i)} {disabled} placeholder="id" aria-label="indicator id" />
					<select class={inputCls} value={ind.kind} on:change={(e) => onKindChange(i, (e.currentTarget as HTMLSelectElement).value)} {disabled} aria-label="indicator kind">
						{#each Object.entries(INDICATOR_DEFS) as [k, def]}
							<option value={k}>{def.label}</option>
						{/each}
					</select>
					{#each INDICATOR_DEFS[ind.kind]?.params ?? [] as sp}
						<label class="flex items-center gap-1 text-[10px] text-[#666]">
							{sp.key}
							<input type="number" class={`${inputCls} w-16`} bind:value={ind.params[sp.key]} {disabled} step="any" />
						</label>
					{/each}
					<span class="ml-auto font-mono text-[10px] text-[#555]">→ {indicatorOutputs(ind).join(', ')}</span>
					<button type="button" on:click={() => removeIndicator(i)} {disabled} class="px-1.5 text-[12px] text-[#555] transition-colors hover:text-red-400" aria-label="remove indicator">✕</button>
				</div>
			{/each}
			{#if indicators.length === 0}
				<div class="border border-dashed border-[#333] px-3 py-2 text-[11px] text-[#666]">No indicators — you can still build conditions on raw price/volume.</div>
			{/if}
		</div>
	</div>

	<!-- Parameters -->
	<div>
		<div class="flex items-center justify-between">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Parameters <span class="normal-case tracking-normal text-[#555]">(editable knobs you can reference in conditions)</span></div>
			<button type="button" on:click={addParam} {disabled} class="border border-[#333] bg-[#111] px-2 py-1 text-[11px] text-[#888] transition-colors hover:border-[#555] hover:text-white disabled:opacity-40">+ Add parameter</button>
		</div>
		<div class="mt-2 flex flex-wrap gap-2">
			{#each params as p, i (p._uid)}
				<div class="flex items-center gap-1.5 border border-[#1a1a1a] bg-[#050505] p-1.5">
					<input class={`${inputCls} w-28`} bind:value={p.name} on:input={() => onParamRenamed(i)} {disabled} placeholder="name" aria-label="parameter name" />
					<span class="text-[#555]">=</span>
					<input type="number" class={`${inputCls} w-20`} bind:value={p.value} {disabled} step="any" aria-label="parameter value" />
					<button type="button" on:click={() => removeParam(i)} {disabled} class="px-1 text-[12px] text-[#555] transition-colors hover:text-red-400" aria-label="remove parameter">✕</button>
				</div>
			{/each}
		</div>
	</div>

	<!-- Condition groups -->
	{#if !showShort}
		<button type="button" on:click={() => (showShort = true)} {disabled} class="text-[11px] text-[#888] transition-colors hover:text-white">+ Add short side</button>
	{:else}
		<button type="button" on:click={removeShortSide} {disabled} class="text-[11px] text-[#666] transition-colors hover:text-red-400">− Remove short side</button>
	{/if}
	{#each groupList.filter((g) => !g.short || showShort) as gm (gm.key)}
		{@const group = gm.group}
		<div class="border border-[#1a1a1a] bg-[#050505] p-3">
			<div class="flex items-center justify-between">
				<div class="text-[10px] uppercase tracking-wider text-[#888]">{gm.label}</div>
				<div class="flex items-center gap-2">
					{#if group.conditions.length > 1}
						<select class={inputCls} bind:value={group.logic} {disabled} aria-label="combine logic">
							<option value="and">ALL (AND)</option>
							<option value="or">ANY (OR)</option>
						</select>
					{/if}
					<button type="button" on:click={() => addCondition(group)} {disabled} class="border border-[#333] bg-[#111] px-2 py-1 text-[11px] text-[#888] transition-colors hover:border-[#555] hover:text-white disabled:opacity-40">+ Condition</button>
				</div>
			</div>
			<div class="mt-2 space-y-2">
				{#each group.conditions as cond, ci (cond._uid)}
					<div class="flex flex-wrap items-center gap-2">
						<!-- LEFT operand -->
						<select class={inputCls} bind:value={cond.left.type} on:change={() => onOperandTypeChange(cond.left)} {disabled} aria-label="left operand type">
							<option value="series">Series</option>
							<option value="param">Param</option>
							<option value="const">Value</option>
						</select>
						{#if cond.left.type === 'series'}
							<select class={inputCls} bind:value={cond.left.value} {disabled}>
								{#if !availableSeries.includes(String(cond.left.value))}<option value={cond.left.value}>{cond.left.value} — missing</option>{/if}
								{#each availableSeries as s}<option value={s}>{s}</option>{/each}
							</select>
						{:else if cond.left.type === 'param'}
							<select class={inputCls} bind:value={cond.left.value} {disabled}>
								{#if !paramNames.includes(String(cond.left.value))}<option value={cond.left.value}>{cond.left.value} — missing</option>{/if}
								{#each paramNames as p}<option value={p}>{p}</option>{/each}
							</select>
						{:else}
							<input type="number" class={`${inputCls} w-24`} bind:value={cond.left.value} {disabled} step="any" />
						{/if}
						<!-- OPERATOR -->
						<select class={`${inputCls} font-mono`} bind:value={cond.op} {disabled} aria-label="operator">
							{#each OPERATORS as o}<option value={o}>{o}</option>{/each}
						</select>
						<!-- RIGHT operand -->
						<select class={inputCls} bind:value={cond.right.type} on:change={() => onOperandTypeChange(cond.right)} {disabled} aria-label="right operand type">
							<option value="series">Series</option>
							<option value="param">Param</option>
							<option value="const">Value</option>
						</select>
						{#if cond.right.type === 'series'}
							<select class={inputCls} bind:value={cond.right.value} {disabled}>
								{#if !availableSeries.includes(String(cond.right.value))}<option value={cond.right.value}>{cond.right.value} — missing</option>{/if}
								{#each availableSeries as s}<option value={s}>{s}</option>{/each}
							</select>
						{:else if cond.right.type === 'param'}
							<select class={inputCls} bind:value={cond.right.value} {disabled}>
								{#if !paramNames.includes(String(cond.right.value))}<option value={cond.right.value}>{cond.right.value} — missing</option>{/if}
								{#each paramNames as p}<option value={p}>{p}</option>{/each}
							</select>
						{:else}
							<input type="number" class={`${inputCls} w-24`} bind:value={cond.right.value} {disabled} step="any" />
						{/if}
						<button type="button" on:click={() => removeCondition(group, ci)} {disabled} class="ml-auto px-1 text-[12px] text-[#555] transition-colors hover:text-red-400" aria-label="remove condition">✕</button>
					</div>
				{/each}
				{#if group.conditions.length === 0}
					<div class="text-[11px] text-[#666]">No conditions{gm.short ? ' (short side optional)' : ''}.</div>
				{/if}
			</div>
		</div>
	{/each}

	{#if errors.length}
		<div class="space-y-1" role="alert">
			{#each errors as e}<div class="border border-yellow-900/50 bg-yellow-500/5 px-3 py-1.5 text-[11px] text-yellow-500">{e}</div>{/each}
		</div>
	{/if}
</div>
