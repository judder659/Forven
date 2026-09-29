<script lang="ts">
	import { createEventDispatcher, tick } from 'svelte';
	import type { IndicatorMeta, RuleSideKey } from '$lib/api';
	import { RESERVED_PARAM_NAMES } from '$lib/utils/ruleSpec';
	import { formatValue, RAW_COLUMN_LABELS, seriesLabel } from '$lib/utils/ruleLabels';
	import { formulaToSide, sideToFormula } from '$lib/utils/ruleFormula';
	import OperandChip from './OperandChip.svelte';
	import OperatorChip from './OperatorChip.svelte';
	import type { Condition, Group, RuleSpec } from './templates';

	const dispatch = createEventDispatcher<{
		change: { spec: Record<string, unknown>; valid: boolean; errors: string[] };
	}>();

	export let indicators: IndicatorMeta[] = [];
	export let initialSpec: RuleSpec | null = null;
	export let disabled = false;
	/** Bars on which each side's rule held in the latest preview, out of barCount. */
	export let signalBars: Partial<Record<RuleSideKey, number>> = {};
	export let barCount = 0;

	// OHLCV + crypto-native enrichment columns the engine always exposes.
	const PRICE_COLUMNS = ['close', 'open', 'high', 'low', 'volume'];
	const DATA_COLUMNS = [
		'funding_rate', 'open_interest', 'taker_buy_sell_ratio',
		'ls_ratio', 'long_liq_usd', 'short_liq_usd', 'liq_imbalance',
	];
	const RAW_COLUMNS = [...PRICE_COLUMNS, ...DATA_COLUMNS];

	type OperandType = 'series' | 'param' | 'const';
	interface Operand { type: OperandType; value: string | number; }
	interface Cond { uid: number; left: Operand; op: string; right: Operand; }
	interface CondRow extends Cond { kind: 'cond'; }
	interface GroupRow { kind: 'group'; uid: number; logic: 'and' | 'or'; conds: Cond[]; }
	type Row = CondRow | GroupRow;
	interface Side { logic: 'and' | 'or'; rows: Row[]; }
	// prevId / prevName: the name conditions currently use, so a rename re-points them.
	interface Instance { uid: number; id: string; prevId: string; kind: string; params: Record<string, number>; }
	// lo/hi/step: the slider's range. It is set when a value is loaded or typed,
	// not while dragging, so the range never moves under the pointer.
	interface Param { uid: number; name: string; prevName: string; value: number; lo: number; hi: number; step: number; }

	let uid = 1;
	const nextUid = () => uid++;

	let instances: Instance[] = [];
	let params: Param[] = [];
	let sides: Record<RuleSideKey, Side> = {
		entry_long: { logic: 'and', rows: [] },
		exit_long: { logic: 'or', rows: [] },
		entry_short: { logic: 'and', rows: [] },
		exit_short: { logic: 'or', rows: [] },
	};
	let showShort = false;

	$: metaByKind = Object.fromEntries(indicators.map((m) => [m.kind, m]));

	function outputNames(inst: Instance): string[] {
		const id = inst.id.trim();
		const meta = metaByKind[inst.kind];
		if (!meta) return [id];
		return meta.output_suffixes.map((s) => `${id}${s}`);
	}

	$: availableSeries = [...RAW_COLUMNS, ...instances.flatMap(outputNames)];
	$: paramNames = params.map((p) => p.name.trim()).filter(Boolean);

	// ---- Indicator palette ------------------------------------------------------
	let paletteOpen = false;
	let paletteSearch = '';
	let paletteCat = 'All';
	let paletteIndex = 0;
	let paletteInput: HTMLInputElement | undefined;
	// The operand whose "Add an indicator…" opened the palette; it reads the new indicator.
	let pendingOperand: Operand | null = null;
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
	$: if (paletteIndex >= paletteResults.length) paletteIndex = Math.max(0, paletteResults.length - 1);

	async function openPalette(target: Operand | null = null) {
		if (disabled) return;
		pendingOperand = target;
		paletteSearch = '';
		paletteIndex = 0;
		paletteOpen = true;
		await tick();
		paletteInput?.focus();
	}
	function closePalette() {
		paletteOpen = false;
		pendingOperand = null;
	}
	function onPaletteKey(event: KeyboardEvent) {
		if (event.key === 'ArrowDown') paletteIndex = Math.min(paletteResults.length - 1, paletteIndex + 1);
		else if (event.key === 'ArrowUp') paletteIndex = Math.max(0, paletteIndex - 1);
		else if (event.key === 'Enter' && paletteResults[paletteIndex]) addIndicator(paletteResults[paletteIndex]);
		else if (event.key === 'Escape') closePalette();
		else return;
		event.preventDefault();
	}

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
		const id = uniqueId(meta.kind);
		const inst: Instance = { uid: nextUid(), id, prevId: id, kind: meta.kind, params: params0 };
		instances = [...instances, inst];
		if (pendingOperand) {
			pendingOperand.type = 'series';
			pendingOperand.value = outputNames(inst)[0];
		}
		closePalette();
		bump();
	}
	function removeIndicator(i: number) {
		instances = instances.filter((_, idx) => idx !== i);
	}

	// ---- Knobs (named parameters) -------------------------------------------------
	function sliderRange(value: number): { lo: number; hi: number; step: number } {
		const v = Number.isFinite(value) ? value : 0;
		if (v === 0) return { lo: -1, hi: 1, step: 0.01 };
		const span = Math.abs(v);
		const step = Number.isInteger(v) && span >= 5 ? 1 : 10 ** (Math.floor(Math.log10(span)) - 2);
		return { lo: v > 0 ? 0 : v - span, hi: v > 0 ? v + span : 0, step };
	}
	function mkParam(name: string, value: number): Param {
		return { uid: nextUid(), name, prevName: name, value, ...sliderRange(value) };
	}
	function uniqueParamName(base: string): string {
		const clean = base.replace(/\W/g, '_').replace(/^(\d)/, '_$1') || 'knob';
		const taken = new Set(params.map((p) => p.name.trim()));
		const free = (name: string) => !taken.has(name) && !RESERVED_PARAM_NAMES.includes(name);
		if (free(clean)) return clean;
		let n = 2;
		while (!free(`${clean}${n}`)) n++;
		return `${clean}${n}`;
	}
	function addParam() {
		params = [...params, mkParam(uniqueParamName('knob'), 0)];
	}
	function removeParam(i: number) {
		params = params.filter((_, idx) => idx !== i);
	}
	function commitParam(p: Param) {
		// Re-centre the slider when a typed value leaves its range.
		if (typeof p.value === 'number' && Number.isFinite(p.value) && (p.value < p.lo || p.value > p.hi)) {
			Object.assign(p, sliderRange(p.value));
		}
		bump();
	}
	/** Point an operand at a new knob holding `value`, named after what it is compared with. */
	function makeKnob(target: Operand, other: Operand, value: number) {
		const name = uniqueParamName(other.type === 'series' ? `${other.value}_level` : 'level');
		params = [...params, mkParam(name, value)];
		target.type = 'param';
		target.value = name;
		bump();
	}

	// ---- Renames: re-point the conditions that use the old name ----------------
	function eachOperand(visit: (o: Operand) => void) {
		for (const side of Object.values(sides)) {
			for (const row of side.rows) {
				for (const c of row.kind === 'group' ? row.conds : [row]) {
					visit(c.left);
					visit(c.right);
				}
			}
		}
	}
	function renameIndicator(inst: Instance) {
		const from = inst.prevId;
		const to = inst.id.trim();
		// Wait for a free id: re-pointing onto a name in use would merge references.
		if (!to || to === from || RAW_COLUMNS.includes(to) || instances.some((o) => o !== inst && o.id.trim() === to)) return;
		const suffixes = metaByKind[inst.kind]?.output_suffixes ?? [''];
		const renamed = new Map(suffixes.map((s) => [`${from}${s}`, `${to}${s}`]));
		eachOperand((o) => {
			const next = o.type === 'series' ? renamed.get(String(o.value)) : undefined;
			if (next) o.value = next;
		});
		inst.prevId = to;
		bump();
	}
	function renameParam(p: Param) {
		const from = p.prevName;
		const to = p.name.trim();
		if (!to || to === from || params.some((o) => o !== p && o.name.trim() === to)) return;
		eachOperand((o) => {
			if (o.type === 'param' && o.value === from) o.value = to;
		});
		p.prevName = to;
		bump();
	}

	// ---- Conditions -----------------------------------------------------------
	function mkCond(): Cond {
		const left = instances.length ? outputNames(instances[0])[0] : 'close';
		return { uid: nextUid(), left: { type: 'series', value: left }, op: '>', right: { type: 'const', value: 0 } };
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
	function setOperand(o: Operand, next: { type: OperandType; value: string | number }) {
		o.type = next.type;
		o.value = next.value;
		bump();
	}
	function flipLogic(target: { logic: 'and' | 'or' }) {
		target.logic = target.logic === 'and' ? 'or' : 'and';
		bump();
	}

	function toggleShort() {
		showShort = !showShort;
		if (!showShort) {
			sides.entry_short = { logic: 'and', rows: [] };
			sides.exit_short = { logic: 'or', rows: [] };
			formulaOpen = { ...formulaOpen, entry_short: false, exit_short: false };
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
		indicators: instances.map((i) => ({ id: i.id.trim(), kind: i.kind, params: { ...i.params } })),
		params: Object.fromEntries(params.filter((p) => p.name.trim()).map((p) => [p.name.trim(), Number(p.value)])),
		entry_long: sideToSpec(sides.entry_long),
		exit_long: sideToSpec(sides.exit_long),
		entry_short: showShort ? sideToSpec(sides.entry_short) : null,
		exit_short: showShort ? sideToSpec(sides.exit_short) : null,
	};

	function validate(): string[] {
		const errs: string[] = [];
		const ids = new Set<string>();
		for (const inst of instances) {
			const id = inst.id.trim();
			if (!id) errs.push('Every indicator needs an id.');
			else if (RAW_COLUMNS.includes(id)) errs.push(`Indicator id "${id}" collides with a price/data column.`);
			else if (ids.has(id)) errs.push(`Duplicate indicator id "${id}".`);
			else ids.add(id);
			// The engine silently swaps an unusable setting for its default.
			const meta = metaByKind[inst.kind];
			for (const p of meta?.params ?? []) {
				const value = inst.params[p.key];
				const label = `${meta?.label ?? inst.kind} ${p.key}`;
				if (typeof value !== 'number' || !Number.isFinite(value)) errs.push(`${label} needs a number.`);
				else if (value < p.min) errs.push(`${label} must be at least ${p.min}.`);
				else if (p.step >= 1 && !Number.isInteger(value)) errs.push(`${label} must be a whole number.`);
			}
		}
		const names = new Set<string>();
		for (const p of params) {
			const name = p.name.trim();
			if (!name) errs.push('Every parameter needs a name.');
			else if (RESERVED_PARAM_NAMES.includes(name)) errs.push(`Parameter name "${name}" is reserved for a strategy setting. Choose another name.`);
			else if (names.has(name)) errs.push(`Duplicate parameter "${name}".`);
			else names.add(name);
			if (typeof p.value !== 'number' || !Number.isFinite(p.value)) errs.push(`Parameter "${name}" needs a number.`);
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
		for (const { key, label } of SIDE_META) {
			if (!showShort && (key === 'entry_short' || key === 'exit_short')) continue;
			for (const row of sides[key].rows) {
				if (row.kind === 'group') row.conds.forEach((c) => checkCond(label, c));
				else checkCond(label, row);
			}
		}
		return errs;
	}

	$: errors = validateDeps(spec, instances, params, availableSeries, paramNames, sides, showShort, metaByKind);
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
			// The engine also reads a series written as {series: name} or {indicator: name}.
			const ref = obj.indicator ?? obj.series;
			if (ref != null) return { type: 'series', value: String(ref).trim() };
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
	function parseSpec(s: RuleSpec) {
		return {
			instances: (s.indicators ?? []).map((i): Instance => ({ uid: nextUid(), id: i.id, prevId: i.id, kind: i.kind, params: { ...(i.params ?? {}) } })),
			params: Object.entries(s.params ?? {}).map(([name, value]): Param => mkParam(name, Number(value))),
			showShort: !!(s.entry_short || s.exit_short),
			sides: {
				entry_long: loadSide(s.entry_long),
				exit_long: loadSide(s.exit_long),
				entry_short: loadSide(s.entry_short),
				exit_short: loadSide(s.exit_short),
			},
		};
	}
	// Assign the loaded state right here, not inside a helper: the compiler orders
	// `$:` blocks by the assignments it can see, and the derived spec below must
	// run after this block. (Mounted with a spec already set, e.g. on returning to
	// the Visual tab, the builder otherwise reported an empty spec.)
	let _lastLoaded: RuleSpec | null = null;
	$: if (initialSpec && initialSpec !== _lastLoaded) {
		_lastLoaded = initialSpec;
		({ instances, params, showShort, sides } = parseSpec(initialSpec));
		formulaOpen = {};
	}

	// ---- Formula view: a side as text, applied on demand ------------------------
	let formulaOpen: Partial<Record<RuleSideKey, boolean>> = {};
	let formulaText: Partial<Record<RuleSideKey, string>> = {};
	let formulaError: Partial<Record<RuleSideKey, string>> = {};

	function openFormula(key: RuleSideKey) {
		formulaText[key] = sideToFormula(sideToSpec(sides[key]) as Group | null);
		formulaError[key] = '';
		formulaOpen[key] = true;
	}
	function closeFormula(key: RuleSideKey) {
		formulaOpen[key] = false;
	}
	function unknownReference(side: Group | null): string | null {
		const series = new Set(availableSeries);
		const knobs = new Set(paramNames);
		const walk = (items: Array<Condition | Group>): string | null => {
			for (const item of items) {
				if ('conditions' in item) {
					const inner = walk(item.conditions);
					if (inner) return inner;
					continue;
				}
				for (const o of [item.left, item.right]) {
					if (typeof o === 'string' && !series.has(o)) {
						return `Unknown series "${o}". Use a price column or an indicator output: ${[...series].filter((s) => !DATA_COLUMNS.includes(s)).join(', ')}.`;
					}
					if (o && typeof o === 'object' && 'param' in o && !knobs.has(String(o.param))) {
						return `Unknown knob "$${o.param}". Add it under Knobs first.`;
					}
				}
			}
			return null;
		};
		return side ? walk(side.conditions) : null;
	}
	function applyFormula(key: RuleSideKey) {
		const parsed = formulaToSide(formulaText[key] ?? '', sides[key].logic);
		if (parsed.error) {
			formulaError[key] = parsed.at ? `${parsed.error} (at character ${parsed.at + 1}).` : `${parsed.error}.`;
			return;
		}
		const unknown = unknownReference(parsed.side);
		if (unknown) {
			formulaError[key] = unknown;
			return;
		}
		const next = loadSide(parsed.side);
		// An emptied side keeps its usual combine logic.
		if (!parsed.side) next.logic = sides[key].logic;
		sides[key] = next;
		formulaOpen[key] = false;
		bump();
	}
	function onFormulaKey(event: KeyboardEvent, key: RuleSideKey) {
		if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
			event.preventDefault();
			event.stopPropagation();
			applyFormula(key);
		} else if (event.key === 'Escape') {
			event.stopPropagation();
			closeFormula(key);
		}
	}

	// ---- Display ----------------------------------------------------------------
	$: seriesGroups = [
		{ label: 'Price', items: PRICE_COLUMNS.map((c) => ({ value: c, label: RAW_COLUMN_LABELS[c] ?? c })) },
		...(instances.length
			? [{ label: 'Your indicators', items: instances.flatMap((inst) => outputNames(inst).map((name) => ({ value: name, label: seriesLabel(name, instances, metaByKind) }))) }]
			: []),
		{ label: 'Market data', items: DATA_COLUMNS.map((c) => ({ value: c, label: RAW_COLUMN_LABELS[c] ?? c })) },
	];
	$: knobList = params.filter((p) => p.name.trim()).map((p) => ({ name: p.name.trim(), value: p.value }));

	function operandLabel(o: Operand, ..._deps: unknown[]): string {
		if (o.type === 'const') return formatValue(Number(o.value));
		if (o.type === 'param') {
			const knob = params.find((p) => p.name.trim() === String(o.value));
			return knob ? `${o.value} · ${formatValue(knob.value)}` : String(o.value);
		}
		return seriesLabel(String(o.value), instances, metaByKind);
	}
	function operandMissing(o: Operand, ..._deps: unknown[]): boolean {
		if (o.type === 'series') return !availableSeries.includes(String(o.value));
		if (o.type === 'param') return !paramNames.includes(String(o.value));
		return false;
	}

	// How many conditions read each series / knob, to flag unused ones.
	$: usage = countUsage(sides, showShort);
	function countUsage(..._deps: unknown[]): Map<string, number> {
		const counts = new Map<string, number>();
		for (const { key } of SIDE_META) {
			if (!showShort && (key === 'entry_short' || key === 'exit_short')) continue;
			for (const row of sides[key].rows) {
				for (const c of row.kind === 'group' ? row.conds : [row]) {
					for (const o of [c.left, c.right]) {
						const k = `${o.type}:${String(o.value).trim()}`;
						counts.set(k, (counts.get(k) ?? 0) + 1);
					}
				}
			}
		}
		return counts;
	}
	function instanceUses(inst: Instance, ..._deps: unknown[]): number {
		return outputNames(inst).reduce((n, name) => n + (usage.get(`series:${name}`) ?? 0), 0);
	}
	function paramUses(p: Param, ..._deps: unknown[]): number {
		return usage.get(`param:${p.name.trim()}`) ?? 0;
	}

	function signalText(key: RuleSideKey, ..._deps: unknown[]): string {
		const n = signalBars[key];
		if (n == null) return '';
		const pct = barCount > 0 ? ` · ${((n / barCount) * 100).toFixed(n / barCount < 0.01 ? 2 : 1)}%` : '';
		return `true on ${n.toLocaleString()} bars${pct}`;
	}

	const inputCls =
		'border border-sc-line2 bg-sc-bg px-1.5 py-0.5 text-[12px] text-sc-ink outline-none transition-colors focus:border-sc-ink disabled:opacity-40';

	const SIDE_META: { key: RuleSideKey; label: string; title: string; short: boolean; dot: string; empty: string }[] = [
		{ key: 'entry_long', label: 'Entry Long', title: 'Enter long', short: false, dot: 'bg-emerald-500',
			empty: 'No long entry. Add a condition, or trade the short side only.' },
		{ key: 'exit_long', label: 'Exit Long', title: 'Exit long', short: false, dot: 'bg-emerald-900',
			empty: 'No exit rule. Longs close on your stops and targets (Risk).' },
		{ key: 'entry_short', label: 'Entry Short', title: 'Enter short', short: true, dot: 'bg-orange-500',
			empty: 'No short entry.' },
		{ key: 'exit_short', label: 'Exit Short', title: 'Exit short', short: true, dot: 'bg-orange-900',
			empty: 'No exit rule. Shorts close on your stops and targets (Risk).' },
	];
	$: visibleSides = SIDE_META.filter((s) => !s.short || showShort);
</script>

{#snippet condition(cond: Cond, connector: string, onRemove: () => void)}
	<div class="group/cond flex min-h-[26px] flex-wrap items-center gap-1.5">
		<span class="w-7 shrink-0 text-right font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{connector}</span>
		<OperandChip type={cond.left.type} value={cond.left.value} label={operandLabel(cond.left, instances, params)}
			missing={operandMissing(cond.left, availableSeries, paramNames)} {seriesGroups} knobs={knobList} {disabled} ariaLabel="left operand"
			on:change={(e) => setOperand(cond.left, e.detail)}
			on:addIndicator={() => openPalette(cond.left)}
			on:newKnob={(e) => makeKnob(cond.left, cond.right, e.detail)} />
		<OperatorChip op={cond.op} {disabled} on:change={(e) => { cond.op = e.detail; bump(); }} />
		<OperandChip type={cond.right.type} value={cond.right.value} label={operandLabel(cond.right, instances, params)}
			missing={operandMissing(cond.right, availableSeries, paramNames)} {seriesGroups} knobs={knobList} {disabled} ariaLabel="right operand"
			on:change={(e) => setOperand(cond.right, e.detail)}
			on:addIndicator={() => openPalette(cond.right)}
			on:newKnob={(e) => makeKnob(cond.right, cond.left, e.detail)} />
		<button type="button" on:click={onRemove} {disabled} aria-label="remove condition"
			class="ml-auto px-1 text-[11px] text-sc-ink4 opacity-0 transition-opacity hover:text-red-400 focus:opacity-100 group-hover/cond:opacity-100">✕</button>
	</div>
{/snippet}

<!-- input/change bubble up; bump() recomputes derived spec -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="space-y-3" on:input={bump} on:change={bump}>
	<!-- Rules, one card per side -->
	{#each visibleSides as sm (sm.key)}
		{@const side = sides[sm.key]}
		<section class="rounded-md border border-sc-line bg-sc-panel" aria-label={sm.title}>
			<header class="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-sc-line px-3 py-1.5">
				<span class="h-2 w-2 shrink-0 {sm.dot}"></span>
				<h3 class="text-[13px] font-semibold text-sc-ink">{sm.title}</h3>
				<span class="text-[11px] text-sc-ink3">
					when{#if side.rows.length > 1}
						<button type="button" on:click={() => flipLogic(side)} {disabled} aria-label="combine logic"
							title="Switch between all and any"
							class="rounded-md mx-1 border border-sc-line2 px-1 text-[12px] font-medium text-sc-ink hover:border-sc-ink">{side.logic === 'and' ? 'ALL' : 'ANY'}</button>of these hold{/if}
				</span>
				<span class="ml-auto text-[10px] text-sc-ink3" data-testid={`signal-${sm.key}`}>{signalText(sm.key, signalBars, barCount)}</span>
				<button type="button" on:click={() => (formulaOpen[sm.key] ? closeFormula(sm.key) : openFormula(sm.key))} {disabled}
					aria-label="edit as formula" aria-pressed={!!formulaOpen[sm.key]} title="Edit this rule as a formula"
					class="rounded-md border px-1.5 font-mono text-[11px] italic transition-colors {formulaOpen[sm.key] ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink'}">ƒx</button>
			</header>

			<div class="px-2 py-2">
				{#if formulaOpen[sm.key]}
					<textarea bind:value={formulaText[sm.key]} rows="3" spellcheck="false" aria-label={`${sm.title} formula`}
						on:keydown={(e) => onFormulaKey(e, sm.key)} on:input|stopPropagation
						placeholder="rsi < $oversold and (close > ema200 or macd crosses above macd_signal)"
						class="rounded-md w-full resize-y border border-sc-line2 bg-sc-bg px-2 py-1.5 font-mono text-[12px] leading-5 text-sc-ink outline-none focus:border-sc-ink"></textarea>
					{#if formulaError[sm.key]}
						<div class="mt-1 border border-amber-900 bg-amber-500/5 px-2 py-1 text-[11px] text-amber-400" role="alert">{formulaError[sm.key]}</div>
					{/if}
					<div class="mt-1.5 flex flex-wrap items-center gap-2">
						<button type="button" on:click={() => applyFormula(sm.key)} class="terminal-button-primary px-2 py-0.5 text-[12px]">Apply</button>
						<button type="button" on:click={() => closeFormula(sm.key)} class="terminal-button px-2 py-0.5 text-[12px]">Cancel</button>
						<span class="text-[10px] text-sc-ink3">Series by id, knobs as <span class="font-mono text-sc-ink2">$name</span>, <span class="font-mono text-sc-ink2">&lt; &gt;= crosses above</span>, <span class="font-mono text-sc-ink2">and / or</span>, parentheses. Ctrl+Enter applies.</span>
					</div>
				{:else}
					<div class="space-y-1">
						{#each side.rows as row, ri (row.uid)}
							{@const connector = ri === 0 ? 'if' : side.logic}
							{#if row.kind === 'group'}
								<div class="flex items-start gap-1.5">
									<span class="w-7 shrink-0 pt-1 text-right font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">{connector}</span>
									<div class="min-w-0 flex-1 border-l border-sc-line2 py-0.5 pl-1">
										<div class="flex items-center gap-1.5 pb-0.5 text-[10px] text-sc-ink3">
											<button type="button" on:click={() => flipLogic(row)} {disabled} aria-label="group logic"
												class="rounded-md border border-sc-line2 px-1 font-medium text-sc-ink hover:border-sc-ink">{row.logic === 'and' ? 'ALL' : 'ANY'}</button>
											of
											<button type="button" on:click={() => removeRow(side, ri)} {disabled} aria-label="remove group"
												class="ml-auto px-1 text-sc-ink4 hover:text-red-400">✕ group</button>
										</div>
										{#each row.conds as cond, ci (cond.uid)}
											{@render condition(cond, ci === 0 ? '' : row.logic, () => removeGroupCond(row, ci))}
										{/each}
										<button type="button" on:click={() => addGroupCond(row)} {disabled}
											class="ml-8 mt-0.5 text-[12px] text-sc-ink3 hover:text-sc-ink">＋ condition in group</button>
									</div>
								</div>
							{:else}
								{@render condition(row, connector, () => removeRow(side, ri))}
							{/if}
						{/each}
						{#if side.rows.length === 0}
							<div class="px-1 py-1 text-[11px] text-sc-ink3">{sm.empty}</div>
						{/if}
					</div>
					<div class="mt-1.5 flex items-center gap-3 pl-8">
						<button type="button" on:click={() => addCond(side)} {disabled} class="text-[12px] text-sc-ink3 hover:text-sc-ink">＋ Condition</button>
						<button type="button" on:click={() => addGroup(side)} {disabled} class="text-[12px] text-sc-ink3 hover:text-sc-ink">＋ Group</button>
					</div>
				{/if}
			</div>
		</section>
	{/each}

	<div class="flex items-center gap-3 px-1">
		{#if !showShort}
			<button type="button" on:click={toggleShort} {disabled} class="text-[11px] text-sc-ink2 hover:text-sc-ink">+ Add short side</button>
		{:else}
			<button type="button" on:click={toggleShort} {disabled} class="text-[11px] text-sc-ink3 hover:text-red-400">− Remove short side</button>
		{/if}
	</div>

	<!-- Indicators -->
	<section class="rounded-md border border-sc-line bg-sc-panel" aria-label="Indicators">
		<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
			<h3 class="text-[13px] font-semibold text-sc-ink">Indicators</h3>
			<span class="text-[10px] text-sc-ink3">{instances.length}</span>
			<button type="button" on:click={() => openPalette()} {disabled}
				class="rounded-md ml-auto border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40">＋ Indicator</button>
		</header>
		<div class="divide-y divide-sc-line">
			{#each instances as inst, i (inst.uid)}
				{@const meta = metaByKind[inst.kind]}
				{@const uses = instanceUses(inst, usage)}
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5">
					<span class="w-32 truncate text-[12px] text-sc-ink" title={meta?.description ?? inst.kind}>{meta?.label ?? inst.kind}</span>
					<input class={`${inputCls} w-24 font-mono`} bind:value={inst.id} {disabled} placeholder="id" aria-label="indicator id"
						on:input={(e) => { inst.id = e.currentTarget.value; renameIndicator(inst); }} />
					{#each meta?.params ?? [] as p}
						<label class="flex items-center gap-1 text-[10px] text-sc-ink3">
							{p.key}
							<input type="number" class={`${inputCls} w-14`} bind:value={inst.params[p.key]}
								min={p.min} max={p.max} step={p.step} {disabled} />
						</label>
					{/each}
					<span class="ml-auto text-[10px] {uses ? 'text-sc-ink3' : 'text-amber-500/80'}"
						title={uses ? `${uses} condition${uses === 1 ? '' : 's'} read this indicator` : 'No condition reads this indicator'}>{uses ? `used ×${uses}` : 'unused'}</span>
					<button type="button" on:click={() => removeIndicator(i)} {disabled}
						class="px-1 text-[11px] text-sc-ink4 hover:text-red-400" aria-label="remove indicator">✕</button>
				</div>
			{/each}
			{#if instances.length === 0}
				<div class="px-3 py-2 text-[11px] text-sc-ink3">No indicators yet. Add one, or write rules on price and market data.</div>
			{/if}
		</div>
	</section>

	<!-- Knobs: named numbers the rules read -->
	<section class="rounded-md border border-sc-line bg-sc-panel" aria-label="Knobs">
		<header class="flex items-center gap-2 border-b border-sc-line px-3 py-1.5">
			<h3 class="text-[13px] font-semibold text-sc-ink">Knobs</h3>
			<span class="truncate text-[10px] text-sc-ink3">named numbers your rules read · drag to tune, stress-test to check</span>
			<button type="button" on:click={addParam} {disabled}
				class="rounded-md ml-auto shrink-0 border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40">＋ Knob</button>
		</header>
		<div class="divide-y divide-sc-line">
			{#each params as p, i (p.uid)}
				{@const uses = paramUses(p, usage)}
				<div class="flex items-center gap-2 px-3 py-1.5">
					<input class={`${inputCls} w-32 font-mono text-sky-300`} bind:value={p.name} {disabled} placeholder="name" aria-label="parameter name"
						on:input={(e) => { p.name = e.currentTarget.value; renameParam(p); }} />
					<input type="number" class={`${inputCls} w-20 font-mono`} bind:value={p.value} {disabled} step="any" aria-label="parameter value"
						on:change={() => commitParam(p)} />
					<input type="range" min={p.lo} max={p.hi} step={p.step} value={p.value} {disabled} aria-label={`tune ${p.name}`}
						on:input={(e) => { p.value = Number(e.currentTarget.value); }}
						class="min-w-0 flex-1 accent-sky-400" />
					<span class="w-14 shrink-0 text-right text-[10px] {uses ? 'text-sc-ink3' : 'text-amber-500/80'}">{uses ? `used ×${uses}` : 'unused'}</span>
					<button type="button" on:click={() => removeParam(i)} {disabled}
						class="px-1 text-[11px] text-sc-ink4 hover:text-red-400" aria-label="remove parameter">✕</button>
				</div>
			{/each}
			{#if params.length === 0}
				<div class="px-3 py-2 text-[11px] text-sc-ink3">No knobs. Click a number in a rule and choose “Turn it into a knob” to tune it here.</div>
			{/if}
		</div>
	</section>

	{#if errors.length}
		<div class="space-y-1" role="alert">
			{#each errors as e}<div class="border border-amber-900 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-400">{e}</div>{/each}
		</div>
	{/if}
</div>

{#if paletteOpen}
	<div class="fixed inset-0 z-50 flex items-start justify-center bg-sc-bg/70 px-4 pt-[12vh]" role="presentation"
		on:pointerdown={(e) => { if (e.target === e.currentTarget) closePalette(); }}>
		<div role="dialog" aria-modal="true" aria-label="Add an indicator"
			class="rounded-md flex max-h-[70vh] w-full max-w-2xl flex-col border border-sc-line2 bg-sc-panel shadow-2xl shadow-black">
			<div class="flex items-center gap-2 border-b border-sc-line p-2">
				<input bind:this={paletteInput} bind:value={paletteSearch} on:keydown={onPaletteKey}
					placeholder={`Search ${indicators.length || ''} indicators…`} aria-label="search indicators"
					class="rounded-md min-w-0 flex-1 border border-sc-line2 bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink outline-none focus:border-sc-ink" />
				<button type="button" on:click={closePalette} class="px-2 text-[12px] text-sc-ink3 hover:text-sc-ink">Esc</button>
			</div>
			<div class="flex flex-wrap gap-1 border-b border-sc-line px-2 py-1.5">
				{#each categories as cat}
					<button type="button" on:click={() => { paletteCat = cat; paletteIndex = 0; paletteInput?.focus(); }}
						class="rounded-md border px-2 py-0.5 text-[12px] transition-colors {paletteCat === cat ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink3 hover:border-sc-line2 hover:text-sc-ink'}">{cat}</button>
				{/each}
			</div>
			<div class="min-h-0 flex-1 overflow-y-auto py-1" role="listbox" aria-label="indicators">
				{#each paletteResults as meta, i (meta.kind)}
					<button type="button" role="option" aria-selected={i === paletteIndex} on:click={() => addIndicator(meta)}
						on:mousemove={() => (paletteIndex = i)}
						class="flex w-full items-baseline gap-3 px-3 py-1.5 text-left {i === paletteIndex ? 'bg-sc-raise' : ''}">
						<span class="w-44 shrink-0 truncate text-[12px] text-sc-ink">{meta.label}</span>
						<span class="min-w-0 flex-1 truncate text-[11px] text-sc-ink3">{meta.description}</span>
						<span class="shrink-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink4">{meta.category}</span>
					</button>
				{/each}
				{#if paletteResults.length === 0}
					<div class="px-3 py-3 text-[11px] text-sc-ink3">No indicators match “{paletteSearch}”.</div>
				{/if}
			</div>
			<div class="border-t border-sc-line px-3 py-1.5 text-[10px] text-sc-ink3">
				↑↓ to move · Enter to add{#if pendingOperand} · the condition you came from will read the new indicator{/if}
			</div>
		</div>
	</div>
{/if}
