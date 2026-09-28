// Text form of a rule side, e.g. `rsi < $oversold and (close > ema200 or macd crosses above macd_signal)`.
// Identifiers are series (price/data columns and indicator outputs, by id),
// `$name` reads a parameter and bare numbers are constants. `and` binds tighter
// than `or`; parentheses make a group. The builder shows one level of groups,
// so deeper nesting is refused rather than flattened into different logic.
import type { Condition, Group, Operand } from '$lib/components/strategy/templates';

type Logic = 'and' | 'or';

const OP_TEXT: Record<string, string> = {
	'<': '<', '<=': '<=', '>': '>', '>=': '>=', '==': '==', '!=': '!=',
	crosses_above: 'crosses above', crosses_below: 'crosses below',
};
const SYMBOL_OPS: Record<string, string> = {
	'<': '<', '<=': '<=', '>': '>', '>=': '>=', '==': '==', '=': '==', '!=': '!=',
	'≤': '<=', '≥': '>=', '≠': '!=',
};

function isGroup(item: unknown): item is Group {
	return !!item && typeof item === 'object' && Array.isArray((item as Group).conditions);
}

function formatNumber(value: number): string {
	return Number.isFinite(value) ? String(+value.toPrecision(12)) : '0';
}

function operandText(operand: Operand): string {
	if (typeof operand === 'number') return formatNumber(operand);
	if (typeof operand === 'string') return operand.trim();
	if (operand && typeof operand === 'object') {
		const obj = operand as Record<string, unknown>;
		if ('param' in obj) return `$${obj.param}`;
		if ('const' in obj) return formatNumber(Number(obj.const));
		const ref = obj.indicator ?? obj.series;
		if (ref != null) return String(ref).trim();
	}
	return '0';
}

function itemText(item: Condition | Group): string {
	if (isGroup(item)) {
		const inner = item.conditions.map(itemText).join(` ${item.logic === 'or' ? 'or' : 'and'} `);
		return item.conditions.length > 1 ? `(${inner})` : inner;
	}
	return `${operandText(item.left)} ${OP_TEXT[item.op] ?? item.op} ${operandText(item.right)}`;
}

/** The formula for one side of a spec ('' when the side has no conditions). */
export function sideToFormula(side: Group | null | undefined): string {
	if (!side || !side.conditions?.length) return '';
	return side.conditions.map(itemText).join(` ${side.logic === 'or' ? 'or' : 'and'} `);
}

// ---- Parsing --------------------------------------------------------------------

type Token =
	| { kind: 'num'; value: number; at: number }
	| { kind: 'param'; value: string; at: number }
	| { kind: 'ident'; value: string; at: number }
	| { kind: 'op'; value: string; at: number }
	| { kind: 'logic'; value: Logic; at: number }
	| { kind: 'paren'; value: '(' | ')'; at: number };

export class FormulaError extends Error {
	constructor(message: string, readonly at: number) {
		super(message);
	}
}

function tokenize(text: string): Token[] {
	const tokens: Token[] = [];
	const pattern = /\s*(?:(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\$([A-Za-z_]\w*)|([A-Za-z_]\w*)|(<=|>=|==|!=|[<>=≤≥≠])|([()]))/y;
	let pos = 0;
	while (pos < text.length) {
		if (/^\s*$/.test(text.slice(pos))) break;
		pattern.lastIndex = pos;
		const match = pattern.exec(text);
		if (!match) {
			const at = pos + (text.slice(pos).length - text.slice(pos).trimStart().length);
			throw new FormulaError(`Unexpected "${text[at]}"`, at);
		}
		const at = match.index + (match[0].length - match[0].trimStart().length);
		const [, num, param, ident, sym, paren] = match;
		if (num !== undefined) tokens.push({ kind: 'num', value: Number(num), at });
		else if (param !== undefined) tokens.push({ kind: 'param', value: param, at });
		else if (sym !== undefined) tokens.push({ kind: 'op', value: SYMBOL_OPS[sym], at });
		else if (paren !== undefined) tokens.push({ kind: 'paren', value: paren as '(' | ')', at });
		else if (ident !== undefined) {
			const word = ident.toLowerCase();
			if (word === 'and' || word === 'or') tokens.push({ kind: 'logic', value: word, at });
			else if (word === 'crosses_above' || word === 'crosses_below') tokens.push({ kind: 'op', value: word, at });
			else tokens.push({ kind: 'ident', value: ident, at });
		}
		pos = pattern.lastIndex;
	}
	return tokens;
}

type Node = { kind: 'cond'; cond: Condition } | { kind: 'group'; logic: Logic; items: Node[] };

class Parser {
	private i = 0;
	constructor(private readonly tokens: Token[], private readonly length: number) {}

	private peek(): Token | undefined {
		return this.tokens[this.i];
	}
	private fail(message: string): never {
		throw new FormulaError(message, this.peek()?.at ?? this.length);
	}

	parse(): Node {
		const node = this.orExpr();
		if (this.peek()) this.fail(`Unexpected "${String(this.peek()!.value)}"`);
		return node;
	}
	private orExpr(): Node {
		return this.chain('or', () => this.andExpr());
	}
	private andExpr(): Node {
		return this.chain('and', () => this.atom());
	}
	private chain(logic: Logic, next: () => Node): Node {
		const items = [next()];
		while (this.peek()?.kind === 'logic' && this.peek()!.value === logic) {
			this.i++;
			items.push(next());
		}
		return items.length === 1 ? items[0] : { kind: 'group', logic, items };
	}
	private atom(): Node {
		const token = this.peek();
		if (token?.kind === 'paren' && token.value === '(') {
			this.i++;
			const node = this.orExpr();
			const close = this.peek();
			if (close?.kind !== 'paren' || close.value !== ')') this.fail('Missing ")"');
			this.i++;
			return node;
		}
		const left = this.operand();
		const op = this.operator();
		const right = this.operand();
		return { kind: 'cond', cond: { left, op, right } };
	}
	private operand(): Operand {
		const token = this.peek();
		if (!token) this.fail('A value is missing');
		this.i++;
		if (token.kind === 'num') return token.value;
		if (token.kind === 'param') return { param: token.value };
		if (token.kind === 'ident') {
			const word = token.value.toLowerCase();
			if (word === 'crosses' || word === 'above' || word === 'below') {
				this.i--;
				this.fail('A value is missing before the comparison');
			}
			return token.value;
		}
		this.i--;
		this.fail(`Expected a series, $parameter or number, found "${String(token.value)}"`);
	}
	private operator(): string {
		const token = this.peek();
		if (token?.kind === 'op') {
			this.i++;
			return token.value;
		}
		if (token?.kind === 'ident' && token.value.toLowerCase() === 'crosses') {
			const direction = this.tokens[this.i + 1];
			const word = direction?.kind === 'ident' ? direction.value.toLowerCase() : '';
			if (word === 'above' || word === 'below') {
				this.i += 2;
				return `crosses_${word}`;
			}
			this.i++;
			this.fail('Write "crosses above" or "crosses below"');
		}
		this.fail('Expected a comparison such as <, >= or "crosses above"');
	}
}

export interface ParsedSide {
	side: Group | null;
	error: string | null;
	at: number | null;
}

/** Parse a formula into a spec side. An empty formula is an empty side. */
export function formulaToSide(text: string, defaultLogic: Logic = 'and'): ParsedSide {
	if (!text.trim()) return { side: null, error: null, at: null };
	try {
		const root = new Parser(tokenize(text), text.length).parse();
		const items = root.kind === 'group' ? root.items : [root];
		const logic = root.kind === 'group' ? root.logic : defaultLogic;
		const conditions = items.map((item): Condition | Group => {
			if (item.kind === 'cond') return item.cond;
			if (item.items.some((inner) => inner.kind === 'group')) {
				throw new FormulaError('Groups can nest one level deep in the visual builder.', 0);
			}
			return { logic: item.logic, conditions: item.items.map((inner) => (inner as { cond: Condition }).cond) };
		});
		return { side: { logic, conditions }, error: null, at: null };
	} catch (err) {
		if (err instanceof FormulaError) return { side: null, error: err.message, at: err.at };
		throw err;
	}
}
