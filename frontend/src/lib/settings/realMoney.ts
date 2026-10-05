// Settings changes that put real money at risk. Saving any of them asks for a
// typed confirmation with a summary of every pending change.
import { SETTINGS_MANIFEST } from './manifest';

interface RealMoneyRule {
  id: string;
  when: (value: unknown) => boolean;
  warning: string;
}

export const REAL_MONEY_RULES: RealMoneyRule[] = [
  {
    id: 'trading-mode.trading_mode',
    when: (value) => String(value).toLowerCase() === 'live',
    warning: 'Trading mode becomes Live, so orders are sent to the exchange.',
  },
  {
    id: 'hyperliquid.use_testnet',
    when: (value) => value === false,
    warning: 'Testnet turns off, so orders go to Hyperliquid mainnet with real funds.',
  },
  {
    id: 'bot-operations.allow_auto_live_promotion',
    when: (value) => value === true,
    warning: 'Strategies can go live without your typed GO LIVE.',
  },
];

export const REAL_MONEY_PHRASE = 'LIVE';

export function realMoneyWarnings(dirty: Iterable<string>, values: Record<string, unknown>): string[] {
  const ids = new Set(dirty);
  return REAL_MONEY_RULES.filter((rule) => ids.has(rule.id) && rule.when(values[rule.id])).map((rule) => rule.warning);
}

function formatValue(id: string, value: unknown): string {
  const entry = SETTINGS_MANIFEST.find((candidate) => candidate.id === id);
  if (entry?.type === 'secret') return '(hidden)';
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (Array.isArray(value)) return value.join(', ') || '—';
  const option = entry?.options?.find((candidate) => candidate.value === value);
  return option ? option.label : String(value);
}

/** One `[label, "old → new"]` row per pending change, for the confirmation. */
export function changeSummary(
  dirty: Iterable<string>,
  originals: Record<string, unknown>,
  values: Record<string, unknown>,
): Array<[string, string]> {
  return [...dirty].map((id) => {
    const entry = SETTINGS_MANIFEST.find((candidate) => candidate.id === id);
    return [entry?.label ?? id, `${formatValue(id, originals[id])} → ${formatValue(id, values[id])}`];
  });
}
