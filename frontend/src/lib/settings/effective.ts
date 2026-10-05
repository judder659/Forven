// RISK-BOUND-1: the backend reports (GET /api/settings -> risk_effective) every
// saved risk value the enforcement path replaces, e.g. caps locked on mainnet.
// Fields show the value in force next to the saved one.

export interface EffectiveRiskValue {
  effective: unknown;
  reason: string;
}

export interface EffectiveRiskReport {
  on_mainnet: boolean;
  leverage_cap: number | null;
  locked: Record<string, EffectiveRiskValue>;
}

export function effectiveRiskReport(settings: unknown): EffectiveRiskReport | null {
  const report = (settings as { risk_effective?: unknown } | null)?.risk_effective;
  if (!report || typeof report !== 'object') return null;
  return report as EffectiveRiskReport;
}

export function effectiveValueFor(settings: unknown, backendPath: string): EffectiveRiskValue | null {
  return effectiveRiskReport(settings)?.locked?.[backendPath] ?? null;
}

export function formatEffective(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));
  return String(value ?? '—');
}
