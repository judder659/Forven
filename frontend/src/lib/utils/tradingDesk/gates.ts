/** Account-level trading gates from the dashboard payload. */
import type { ForvenDashboardResponse } from '$lib/api';

export interface Gate {
	key: string;
	name: string;
	clear: boolean;
	value: string;
}

export function gatesOf(dashboard: ForvenDashboardResponse | null | undefined): Gate[] {
	const d = dashboard ?? {};
	const breakers = d.circuit_breakers ?? {};
	const breaker = (state: string | undefined) => String(state ?? 'closed').toLowerCase();
	return [
		{ key: 'system', name: 'System running', clear: !d.paused, value: d.paused ? 'paused' : 'running' },
		{ key: 'kill', name: 'Kill switch', clear: !d.risk?.kill_switch_active, value: d.risk?.kill_switch_active ? 'active' : 'off' },
		{ key: 'daily', name: 'Daily loss halt', clear: !d.risk?.daily_loss_halt, value: d.risk?.daily_loss_halt ? 'halted' : 'clear' },
		{ key: 'recovery', name: 'Recovery', clear: !d.recovery?.active, value: d.recovery?.active ? 'running' : d.recovery?.status || 'ok' },
		{ key: 'hl_price', name: 'Exchange prices API', clear: breaker(breakers.hl_price) === 'closed', value: breaker(breakers.hl_price) },
		{ key: 'hl_trade', name: 'Exchange trading API', clear: breaker(breakers.hl_trade) === 'closed', value: breaker(breakers.hl_trade) },
		{ key: 'hl_account', name: 'Exchange account API', clear: breaker(breakers.hl_account) === 'closed', value: breaker(breakers.hl_account) },
	];
}

export function blockingGates(dashboard: ForvenDashboardResponse | null | undefined): Gate[] {
	return gatesOf(dashboard).filter((gate) => !gate.clear);
}
