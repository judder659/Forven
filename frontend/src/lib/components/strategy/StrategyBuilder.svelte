<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { IndicatorMeta } from '$lib/api';
	import type { RuleSpec } from './templates';

	const dispatch = createEventDispatcher<{
		change: { spec: Record<string, unknown>; valid: boolean; errors: string[] };
	}>();

	export let indicators: IndicatorMeta[] = [];
	export let initialSpec: RuleSpec | null = null;
	export let disabled = false;

	// OHLCV + crypto-native enrichment columns the engine always exposes.
	const RAW_COLUMNS = [
		'close', 'open', 'high', 'low', 'volume',
		'funding_rate', 'open_interest', 'taker_buy_sell_ratio',
		'ls_ratio', 'long_liq_usd', 'short_liq_usd', 'liq_imbalance',
	];
	const OPERATORS = ['<', '<=', '>', '>=', '==', '!=', 'crosses_above', 'crosses_below'];
	const OP_LABELS: Record<string, string> = {
		'<': '<', '<=': '≤', '>': '>', '>=': '≥', '==': '=', '!=': '≠',
		crosses_above: 'crosses ↑', crosses_below: 'crosses ↓',
	};

	type OperandType = 'series' | 'param' | 'const';
	interface Operand { type: OperandType; value: string | number; }
	interface Cond { uid: number; left: Operand; op: string; right: Operand; }
	interface CondRow extends Cond { kind: 'cond'; }
	interface GroupRow { kind: 'group'; uid: number; logic: 'and' | 'or'; conds: Cond[]; }
	type Row = CondRow | GroupRow;
	interface Side { logic: 'and' | 'or'; rows: Row[]; }
	interface Instance { uid: number; id: string; kind: string; params: Record<string, number>; }
	interface Param { uid: number; name: string; value: number; }

	let uid = 1;
	const nextUid = () => uid++;

	let instances: Instance[] = [];
	let params: Param[] = [];
	let sides: Record<'entry_long' | 'exit_long' | 'entry_short' | 'exit_short', Side> = {
		entry_long: { logic: 'and', rows: [] },
		exit_long: { logic: 'or', rows: [] },
		entry_short: { logic: 'and', rows: [] },
		exit_short: { logic: 'or', rows: [] },
	};
	let showShort = false;

	$: metaByKind = Object.fromEntries(indicators.map((m) => [m.kind, m]));

	function outputNames(inst: Instance): string[] {
		const meta = metaByKind[inst.kind];
		if (!meta) return [inst.id];
		return meta.output_suffixes.map((s) => `${inst.id}${s}`);
	}

	$: availableSeries = [...RAW_COLUMNS, ...instances.flatMap(outputNames)];
	$: paramNames = params.map((p) => p.name).filter(Boolean);

	// ---- Palette --------------------------------------------------------------
	let paletteSearch = '';
	let paletteCat = 'All';
	$: categories = ['All', ...Array.from(new Set(indicators.map((m) => m.category)))];
	$: paletteResults = indicators.filter((m) => {
		if (paletteCat !== 'All' && m.category !== paletteCat) return false;
		const q = paletteSearch.trim().toLowerCase();
		if (!q) return true;
		return (
			m.label.toLowerCase().includes(q) ||
			m.kind.toLowerCase().includes(q) ||
			m.category.toLowerCase().includes(q) ||
			m.description.toLowerCase().includes(q)
		);
	});

	function uniqueId(base: string): string {
		const taken = new Set(instances.map((i) => i.id));
		if (!taken.has(base) && !RAW_COLUMNS.includes(base)) return base;
		let n = 2;
		while (taken.has(`${base}${n}`) || RAW_COLUMNS.includes(`${base}${n}`)) n++;
		return `${base}${n}`;
	}

	function addIndicator(meta: IndicatorMeta) {
		const params0: Record<string, number> = {};
		for (const p of meta.params) params0[p.key] = p.default;
		instances = [...instances, { uid: nextUid(), id: uniqueId(meta.kind), kind: meta.kind, params: params0 }];
	}
	function removeIndicator(i: number) {
		instances = instances.filter((_, idx) => idx !== i);
	}

	function addParam() {
		const nm = `param${params.length + 1}`;
		params = [...params, { uid: nextUid(), name: nm, value: 0 }];
	}
	function removeParam(i: number) {
		params = params.filter((_, idx) => idx !== i);
	}

	// ---- Conditions -----------------------------------------------------------
	function mkCond(): Cond {
		return { uid: nextUid(), left: { type: 'series', value: availableSeries[0] ?? 'close' }, op: '>', right: { type: 'const', value: 0 } };
	}
	function addCond(side: Side) {
		side.rows = [...side.rows, { kind: 'cond', ...mkCond() }];
		bump();
	}
	function addGroup(side: Side) {
		side.rows = [...side.rows, { kind: 'group', uid: nextUid(), logic: 'or', conds: [mkCond()] }];
		bump();
	}
	function removeRow(side: Side, i: number) {
		side.rows = side.rows.filter((_, idx) => idx !== i);
		bump();
	}
	function addGroupCond(group: GroupRow) {
		group.conds = [...group.conds, mkCond()];
		bump();
	}
	function removeGroupCond(group: GroupRow, i: number) {
		group.conds = group.conds.filter((_, idx) => idx !== i);
		bump();
	}
	function onOperandTypeChange(o: Operand) {
		if (o.type === 'const') o.value = 0;
		else if (o.type === 'series') o.value = RAW_COLUMNS[0];
		else o.value = paramNames[0] ?? '';
		bump();
	}

	function toggleShort() {
		showShort = !showShort;
		if (!showShort) {
			sides.entry_short = { logic: 'and', rows: [] };
			sides.exit_short = { logic: 'or', rows: [] };
		}
		bump();
	}

	// Force recompute of derived state after in-place nested mutation.
	function bump() {
		instances = instances;
		params = params;
		sides = sides;
	}

	// ---- Spec build -----------------------------------------------------------
	function operandToSpec(o: Operand): unknown {
		if (o.type === 'const') return Number(o.value) || 0;
		if (o.type === 'param') return { param: String(o.value) };
		return String(o.value);
	}
	function condToSpec(c: Cond) {
		return { left: operandToSpec(c.left), op: c.op, right: operandToSpec(c.right) };
	}
	function sideToSpec(side: Side): Record<string, unknown> | null {
		if (!side.rows.length) return null;
		const conditions: unknown[] = [];
		for (const row of side.rows) {
			if (row.kind === 'group') {
				if (row.conds.length) conditions.push({ logic: row.logic, conditions: row.conds.map(condToSpec) });
			} else {
				conditions.push(condToSpec(row));
			}
		}
		if (!conditions.length) return null;
		return { logic: side.logic, conditions };
	}

	$: spec = {
		indicators: instances.map((i) => ({ id: i.id, kind: i.kind, params: { ...i.params } })),
		params: Object.fromEntries(params.filter((p) => p.name).map((p) => [p.name, Number(p.value)])),
		entry_long: sideToSpec(sides.entry_long),
		exit_long: sideToSpec(sides.exit_long),
		entry_short: showShort ? sideToSpec(sides.entry_short) : null,
		exit_short: showShort ? sideToSpec(sides.exit_short) : null,
	};

	function validate(): string[] {
		const errs: string[] = [];
		const ids = new Set<string>();
		for (const inst of instances) {
			if (!inst.id.trim()) errs.push('Every indicator needs an id.');
			else if (RAW_COLUMNS.includes(inst.id)) errs.push(`Indicator id "${inst.id}" collides with a price/data column.`);
			else if (ids.has(inst.id)) errs.push(`Duplicate indicator id "${inst.id}".`);
			else ids.add(inst.id);
		}
		const hasEntry =
			!!(spec.entry_long as { conditions?: unknown[] } | null)?.conditions?.length ||
			!!(spec.entry_short as { conditions?: unknown[] } | null)?.conditions?.length;
		if (!hasEntry) errs.push('Add at least one entry condition (long or short).');

		const seriesSet = new Set(availableSeries);
		const paramSet = new Set(paramNames);
		const checkCond = (label: string, c: Cond) => {
			for (const o of [c.left, c.right]) {
				if (o.type === 'series' && !seriesSet.has(String(o.value))) errs.push(`${label}: unknown series "${o.value}".`);
				if (o.type === 'param' && !paramSet.has(String(o.value))) errs.push(`${label}: unknown parameter "${o.value}".`);
			}
		};
		const sideLabels: [keyof typeof sides, string][] = [
			['entry_long', 'Entry Long'], ['exit_long', 'Exit Long'],
			['entry_short', 'Entry Short'], ['exit_short', 'Exit Short'],
		];
		for (const [key, label] of sideLabels) {
			if (!showShort && (key === 'entry_short' || key === 'exit_short')) continue;
			for (const row of sides[key].rows) {
				if (row.kind === 'group') row.conds.forEach((c) => checkCond(label, c));
				else checkCond(label, row);
			}
		}
		return errs;
	}

	$: errors = validateDeps(spec, instances, params, availableSeries, paramNames, sides, showShort);
	function validateDeps(..._deps: unknown[]): string[] {
		return validate();
	}

	$: dispatch('change', { spec, valid: errors.length === 0, errors });

	// ---- Load an external spec (template / saved strategy) --------------------
	function parseOperand(o: unknown): Operand {
		if (typeof o === 'number') return { type: 'const', value: o };
		if (o && typeof o === 'object') {
			const obj = o as Record<string, unknown>;
			if ('param' in obj) return { type: 'param', value: String(obj.param) };
			if ('const' in obj) return { type: 'const', value: Number(obj.const) };
		}
		if (typeof o === 'string') {
			const n = Number(o);
			if (o.trim() !== '' && Number.isFinite(n) && !RAW_COLUMNS.includes(o)) return { type: 'const', value: n };
			return { type: 'series', value: o };
		}
		return { type: 'const', value: 0 };
	}
	function loadCond(c: Record<string, unknown>): Cond {
		return { uid: nextUid(), left: parseOperand(c.left), op: String(c.op ?? '>'), right: parseOperand(c.right) };
	}
	function loadSide(g: unknown): Side {
		if (!g || typeof g !== 'object') return { logic: 'and', rows: [] };
		const grp = g as { logic?: string; conditions?: unknown[] };
		const rows: Row[] = (grp.conditions ?? []).map((c) => {
			const cc = c as Record<string, unknown>;
			if (cc && Array.isArray(cc.conditions)) {
				return {
					kind: 'group', uid: nextUid(),
					logic: (cc.logic === 'or' ? 'or' : 'and'),
					conds: (cc.conditions as Record<string, unknown>[]).map(loadCond),
				};
			}
			return { kind: 'cond', ...loadCond(cc) };
		});
		return { logic: grp.logic === 'or' ? 'or' : 'and', rows };
	}
	function loadSpec(s: RuleSpec) {
		instances = (s.indicators ?? []).map((i) => ({ uid: nextUid(), id: i.id, kind: i.kind, params: { ...(i.params ?? {}) } }));
		params = Object.entries(s.params ?? {}).map(([name, value]) => ({ uid: nextUid(), name, value: Number(value) }));
		showShort = !!(s.entry_short || s.exit_short);
		sides = {
			entry_long: loadSide(s.entry_long),
			exit_long: loadSide(s.exit_long),
			entry_short: loadSide(s.entry_short),
			exit_short: loadSide(s.exit_short),
		};
	}
	let _lastLoaded: RuleSpec | null = null;
	$: if (initialSpec && initialSpec !== _lastLoaded) {
		_lastLoaded = initialSpec;
		loadSpec(initialSpec);
	}

	const inputCls =
		'border border-[#333] bg-black px-2 py-1 text-[12px] text-white outline-none transition-colors focus:border-white disabled:opacity-40';

	const SIDE_META: { key: keyof typeof sides; label: string; short: boolean }[] = [
		{ key: 'entry_long', label: 'Entry — Long', short: false },
		{ key: 'exit_long', label: 'Exit — Long', short: false },
		{ key: 'entry_short', label: 'Entry — Short', short: true },
		{ key: 'exit_short', label: 'Exit — Short', short: true },
	];
	$: visibleSides = SIDE_META.filter((s) => !s.short || showShort);
</script>

<!-- input/change bubble up; bump() recomputes derived spec -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="space-y-4" on:input={bump} on:change={bump}>
	<!-- Indicator palette + active instances -->
	<div class="border border-[#222] bg-[#050505] p-3">
		<div class="text-[10px] uppercase tracking-wider text-[#666]">Indicators</div>
		<div class="mt-2 flex flex-wrap items-center gap-2">
			<input
				bind:value={paletteSearch}
				{disabled}
				placeholder="Search 40+ indicators…"
				class={`${inputCls} w-48`}
				aria-label="search indicators"
			/>
			<div class="flex flex-wrap gap-1">
				{#each categories as cat}
					<button type="button" on:click={() => (paletteCat = cat)} {disabled}
						class="border px-2 py-1 text-[10px] uppercase tracking-wide transition-colors {paletteCat === cat ? 'border-white bg-white text-black' : 'border-[#333] bg-transparent text-[#666] hover:border-[#555] hover:text-white'}">
						{cat}
					</button>
				{/each}
			</div>
		</div>
		<div class="mt-2 grid max-h-44 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">
			{#each paletteResults as meta (meta.kind)}
				<button type="button" on:click={() => addIndicator(meta)} {disabled}
					class="group flex items-start gap-2 border border-[#222] bg-black px-2 py-1.5 text-left transition-colors hover:border-white disabled:opacity-40">
					<span class="mt-0.5 text-[#666] group-hover:text-white">＋</span>
					<span class="min-w-0">
						<span class="block truncate text-[12px] text-[#aaa] group-hover:text-white">{meta.label}</span>
						<span class="block truncate text-[10px] text-[#555]">{meta.category}</span>
					</span>
				</button>
			{/each}
			{#if paletteResults.length === 0}
				<div class="col-span-full px-2 py-3 text-[11px] text-[#555]">No indicators match “{paletteSearch}”.</div>
			{/if}
		</div>

		<!-- Active indicator instances -->
		<div class="mt-3 space-y-2">
			{#each instances as inst, i (inst.uid)}
				{@const meta = metaByKind[inst.kind]}
				<div class="flex flex-wrap items-center gap-2 border border-[#222] bg-black p-2">
					<span class="border border-[#333] px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-[#888]">{meta?.label ?? inst.kind}</span>
					<input class={`${inputCls} w-24`} bind:value={inst.id} {disabled} placeholder="id" aria-label="indicator id" />
					{#each meta?.params ?? [] as p}
						<label class="flex items-center gap-1 text-[10px] text-[#666]">
							{p.key}
							<input type="number" class={`${inputCls} w-16`} bind:value={inst.params[p.key]}
								min={p.min} max={p.max} step={p.step} {disabled} />
						</label>
					{/each}
					<span class="ml-auto font-mono text-[10px] text-[#555]">→ {outputNames(inst).join(', ')}</span>
					<button type="button" on:click={() => removeIndicator(i)} {disabled}
						class="px-1.5 text-[12px] text-[#555] hover:text-red-400" aria-label="remove indicator">✕</button>
				</div>
			{/each}
			{#if instances.length === 0}
				<div class="border border-dashed border-[#333] px-3 py-2 text-[11px] text-[#555]">
					No indicators yet — click one above, or build conditions on raw price/volume.
				</div>
			{/if}
		</div>
	</div>

	<!-- Parameters -->
	<div>
		<div class="flex items-center justify-between">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">
				Parameters <span class="normal-case tracking-normal text-[#555]">(tunable knobs you can reference in conditions)</span>
			</div>
			<button type="button" on:click={addParam} {disabled}
				class="terminal-button px-2 py-1 text-[10px]">+ Parameter</button>
		</div>
		<div class="mt-2 flex flex-wrap gap-2">
			{#each params as p, i (p.uid)}
				<div class="flex items-center gap-1.5 border border-[#222] bg-[#050505] p-1.5">
					<input class={`${inputCls} w-28`} bind:value={p.name} {disabled} placeholder="name" aria-label="parameter name" />
					<span class="text-[#555]">=</span>
					<input type="number" class={`${inputCls} w-20`} bind:value={p.value} {disabled} step="any" aria-label="parameter value" />
					<button type="button" on:click={() => removeParam(i)} {disabled}
						class="px-1 text-[12px] text-[#555] hover:text-red-400" aria-label="remove parameter">✕</button>
				</div>
			{/each}
			{#if params.length === 0}
				<span class="text-[11px] text-[#555]">No parameters.</span>
			{/if}
		</div>
	</div>

	<!-- Short side toggle -->
	{#if !showShort}
		<button type="button" on:click={toggleShort} {disabled} class="text-[11px] text-[#888] hover:text-white">+ Add short side</button>
	{:else}
		<button type="button" on:click={toggleShort} {disabled} class="text-[11px] text-[#666] hover:text-red-400">− Remove short side</button>
	{/if}

	<!-- Condition sides -->
	{#each visibleSides as sm (sm.key)}
		{@const side = sides[sm.key]}
		<div class="border border-[#222] bg-[#050505] p-3">
			<div class="flex items-center justify-between">
				<div class="text-[10px] uppercase tracking-wider text-[#888]">{sm.label}</div>
				<div class="flex items-center gap-2">
					{#if side.rows.length > 1}
						<select class={inputCls} bind:value={side.logic} {disabled} aria-label="combine logic">
							<option value="and">ALL (AND)</option>
							<option value="or">ANY (OR)</option>
						</select>
					{/if}
					<button type="button" on:click={() => addCond(side)} {disabled}
						class="terminal-button px-2 py-1 text-[10px]">+ Condition</button>
					<button type="button" on:click={() => addGroup(side)} {disabled}
						class="terminal-button px-2 py-1 text-[10px]">+ Group</button>
				</div>
			</div>
			<div class="mt-2 space-y-2">
				{#each side.rows as row, ri (row.uid)}
					{#if row.kind === 'group'}
						<div class="border border-[#333] bg-black p-2">
							<div class="mb-1.5 flex items-center justify-between">
								<select class={inputCls} bind:value={row.logic} {disabled} aria-label="group logic">
									<option value="or">ANY (OR)</option>
									<option value="and">ALL (AND)</option>
								</select>
								<button type="button" on:click={() => removeRow(side, ri)} {disabled}
									class="px-1 text-[12px] text-[#555] hover:text-red-400" aria-label="remove group">✕ group</button>
							</div>
							<div class="space-y-2">
								{#each row.conds as cond (cond.uid)}
									<div class="flex flex-wrap items-center gap-2">
										<!-- left -->
										<select class={inputCls} bind:value={cond.left.type} on:change={() => onOperandTypeChange(cond.left)} {disabled} aria-label="left type">
											<option value="series">Series</option><option value="param">Param</option><option value="const">Value</option>
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
										<select class={`${inputCls} font-mono`} bind:value={cond.op} {disabled} aria-label="operator">
											{#each OPERATORS as o}<option value={o}>{OP_LABELS[o]}</option>{/each}
										</select>
										<!-- right -->
										<select class={inputCls} bind:value={cond.right.type} on:change={() => onOperandTypeChange(cond.right)} {disabled} aria-label="right type">
											<option value="series">Series</option><option value="param">Param</option><option value="const">Value</option>
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
										<button type="button" on:click={() => removeGroupCond(row, row.conds.indexOf(cond))} {disabled}
											class="ml-auto px-1 text-[12px] text-[#555] hover:text-red-400" aria-label="remove condition">✕</button>
									</div>
								{/each}
								<button type="button" on:click={() => addGroupCond(row)} {disabled}
									class="text-[11px] text-[#888] hover:text-white">+ condition in group</button>
							</div>
						</div>
					{:else}
						<div class="flex flex-wrap items-center gap-2">
							<select class={inputCls} bind:value={row.left.type} on:change={() => onOperandTypeChange(row.left)} {disabled} aria-label="left type">
								<option value="series">Series</option><option value="param">Param</option><option value="const">Value</option>
							</select>
							{#if row.left.type === 'series'}
								<select class={inputCls} bind:value={row.left.value} {disabled}>
									{#if !availableSeries.includes(String(row.left.value))}<option value={row.left.value}>{row.left.value} — missing</option>{/if}
									{#each availableSeries as s}<option value={s}>{s}</option>{/each}
								</select>
							{:else if row.left.type === 'param'}
								<select class={inputCls} bind:value={row.left.value} {disabled}>
									{#if !paramNames.includes(String(row.left.value))}<option value={row.left.value}>{row.left.value} — missing</option>{/if}
									{#each paramNames as p}<option value={p}>{p}</option>{/each}
								</select>
							{:else}
								<input type="number" class={`${inputCls} w-24`} bind:value={row.left.value} {disabled} step="any" />
							{/if}
							<select class={`${inputCls} font-mono`} bind:value={row.op} {disabled} aria-label="operator">
								{#each OPERATORS as o}<option value={o}>{OP_LABELS[o]}</option>{/each}
							</select>
							<select class={inputCls} bind:value={row.right.type} on:change={() => onOperandTypeChange(row.right)} {disabled} aria-label="right type">
								<option value="series">Series</option><option value="param">Param</option><option value="const">Value</option>
							</select>
							{#if row.right.type === 'series'}
								<select class={inputCls} bind:value={row.right.value} {disabled}>
									{#if !availableSeries.includes(String(row.right.value))}<option value={row.right.value}>{row.right.value} — missing</option>{/if}
									{#each availableSeries as s}<option value={s}>{s}</option>{/each}
								</select>
							{:else if row.right.type === 'param'}
								<select class={inputCls} bind:value={row.right.value} {disabled}>
									{#if !paramNames.includes(String(row.right.value))}<option value={row.right.value}>{row.right.value} — missing</option>{/if}
									{#each paramNames as p}<option value={p}>{p}</option>{/each}
								</select>
							{:else}
								<input type="number" class={`${inputCls} w-24`} bind:value={row.right.value} {disabled} step="any" />
							{/if}
							<button type="button" on:click={() => removeRow(side, ri)} {disabled}
								class="ml-auto px-1 text-[12px] text-[#555] hover:text-red-400" aria-label="remove condition">✕</button>
						</div>
					{/if}
				{/each}
				{#if side.rows.length === 0}
					<div class="text-[11px] text-[#555]">No conditions{sm.short ? ' (short side optional)' : ''}.</div>
				{/if}
			</div>
		</div>
	{/each}

	{#if errors.length}
		<div class="space-y-1" role="alert">
			{#each errors as e}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{e}</div>{/each}
		</div>
	{/if}
</div>
