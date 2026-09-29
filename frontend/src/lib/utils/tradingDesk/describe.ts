/** Plain-language labels for strategy families, refusals, closes and regimes. */
import { cap1, fmtBps, fmtPx, num } from './format';

const ACRONYMS = new Set(['kc', 'iv', 'macd', 'atr', 'ema', 'sma', 'rsi', 'adx', 'bb', 'vwap', 'oi', 'dc', 'vt']);
const WORDS: Record<string, string> = {
	cont: 'continuation',
	volexpdownshort: 'vol-expansion down short',
};
const ASSET_TOKENS = new Set(['btc', 'eth', 'sol', 'bnb', 'xrp', 'doge', 'avax', 'link']);

/** "kc_pullback_thrust4h_s62778" → "KC pullback thrust 4h". */
export function humanFamily(type: string | null | undefined): string {
	let text = String(type ?? '').toLowerCase();
	text = text.replace(/^imported__dropzone_/, '').replace(/_[0-9a-f]{12}$/, '');
	text = text.replace(/_s\d{4,}$/, '').replace(/_v\d+$/, '');
	const words = text
		.split('_')
		.filter((word) => word && !ASSET_TOKENS.has(word) && !/^r\d+l\d+$/.test(word))
		// Family tags like "kc63202" keep their indicator ("KC") and drop the serial.
		.map((word) => word.replace(/^([a-z]{2,3})\d{4,}$/, '$1'))
		.map((word) => WORDS[word] ?? word.replace(/^([a-z]+)(\d+[mhd])$/, '$1 $2'))
		.map((word) => (ACRONYMS.has(word) ? word.toUpperCase() : word));
	return cap1(words.join(' ')) || String(type ?? '');
}

export const TRADE_MODE_LABEL: Record<string, string> = {
	both: 'long + short',
	long_only: 'long only',
	short_only: 'short only',
};

export function tradeModeLabel(mode: string | null | undefined): string {
	return TRADE_MODE_LABEL[String(mode ?? '')] ?? (mode ? String(mode).replace(/_/g, ' ') : 'long + short');
}

const REGIMES: Record<string, string> = {
	TREND_UP: 'trending up',
	TREND_DOWN: 'trending down',
	RANGE_BOUND: 'range-bound',
	HIGH_VOL: 'high volatility',
};

export function humanRegime(regime: string | null | undefined): string {
	const key = String(regime ?? '').toUpperCase();
	return REGIMES[key] ?? (regime ? String(regime).toLowerCase().replace(/_/g, ' ') : '—');
}

export interface RefusalText {
	short: string;
	raw: string;
}

/** A refusal reason as one short sentence; `raw` keeps the stored text. */
export function describeRefusal(reason: string | null | undefined): RefusalText {
	const raw = String(reason ?? '').trim();
	const text = raw.replace(/^BLOCKED\s+\w+\s+(live|paper)\s*[—–-]\s*/i, '').trim();
	if (/could not resolve strategy instance/i.test(text)) {
		return { short: 'The strategy failed to load for this check', raw };
	}
	if (/execution settings are unverified/i.test(text)) {
		return { short: 'Execution settings unverified; a fresh validation was required', raw };
	}
	const leverage = text.match(/leverage ([\d.]+) cannot be applied exactly/i);
	if (leverage) {
		return { short: `Leverage ${leverage[1]}× can't be set exactly on the exchange`, raw };
	}
	const budget = text.match(
		/the (\w+) wallet already holds \$([\d,.]+) of open notional; adding \$([\d,.]+) would exceed \d+% of its \$([\d,.]+) equity/i
	);
	if (budget) {
		return {
			short: `${cap1(budget[1])} wallet full: $${budget[2]} open + $${budget[3]} new is more than its $${budget[4]} equity`,
			raw,
		};
	}
	return { short: text.length > 110 ? `${text.slice(0, 108)}…` : text || 'No reason recorded', raw };
}

export interface CloseText {
	text: string;
	/** "stop" when the exit was a stop, for orange styling. */
	tone: '' | 'stop';
	/** Set when the label is inferred rather than recorded. */
	inferred?: string;
}

/**
 * The close reason in plain words. A live close found by reconcile whose exit
 * sits within 0.6% of the resting stop is labelled a stop fill, marked inferred.
 */
export function describeClose(trade: {
	status?: string | null;
	close_reason?: string | null;
	exit_price?: number | null;
	stop_price?: number | null;
}): CloseText {
	const status = String(trade.status ?? '').toUpperCase();
	if (status === 'FAILED') return { text: 'Order failed', tone: '' };
	if (status === 'OPEN') return { text: 'Open', tone: '' };
	const reason = String(trade.close_reason ?? '').toLowerCase();
	if (reason.includes('reconcile')) {
		const stop = num(trade.stop_price);
		const exit = num(trade.exit_price);
		if (stop !== null && exit !== null && Math.abs(exit - stop) / stop < 0.006) {
			return {
				text: 'Stop filled on exchange',
				tone: 'stop',
				inferred: `Recorded as ${trade.close_reason}; the exit ${fmtPx(exit)} is within 0.6% of the resting stop ${fmtPx(stop)}.`,
			};
		}
		if (reason.includes('pending_close')) return { text: 'Close confirmed by reconcile', tone: '' };
		return { text: 'Closed on exchange', tone: '' };
	}
	if (reason.startsWith('llm')) return { text: 'Bot exit', tone: '' };
	const map: Record<string, CloseText> = {
		signal: { text: 'Strategy exit', tone: '' },
		stop_loss: { text: 'Stop loss', tone: 'stop' },
		take_profit: { text: 'Take profit', tone: '' },
		trailing_stop: { text: 'Trailing stop', tone: 'stop' },
		manual_close: { text: 'Manual close', tone: '' },
		manual_partial_close: { text: 'Manual partial close', tone: '' },
		manual_flip_close: { text: 'Flipped', tone: '' },
		time_stop: { text: 'Time exit', tone: '' },
		max_hold: { text: 'Time exit', tone: '' },
	};
	return map[reason] ?? { text: reason ? cap1(reason.replace(/_/g, ' ')) : 'Closed', tone: '' };
}

/** Slippage against the signal price in words; positive bps = worse. */
export function slippageWords(bps: number | null | undefined): string {
	const n = num(bps);
	if (n === null) return 'no signal price recorded';
	if (Math.abs(n) < 0.05) return 'filled at the signal price';
	return `${fmtBps(n)} bps ${n > 0 ? 'worse' : 'better'} than the signal`;
}
