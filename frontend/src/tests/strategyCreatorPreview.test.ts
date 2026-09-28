import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';

const api = vi.hoisted(() => Object.fromEntries([
	'getIndicators', 'previewStrategyChart', 'stressTestStrategy', 'nlToSpec', 'nlEditSpec', 'listStrategyLibrary', 'createLibraryStrategy',
	'updateLibraryStrategy', 'deleteLibraryStrategy', 'duplicateLibraryStrategy', 'sendLibraryStrategyToForge',
	'getSystemStrategyDetail', 'getPrebuiltStrategies', 'getStrategies', 'submitBacktest', 'registerCustomStrategy', 'getResult', 'getSymbols',
].map((name) => [name, vi.fn()])));
vi.mock('$lib/api', () => api);
vi.mock('$lib/api/strategyCreator', () => ({ checkIdeaReadiness: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
// lightweight-charts needs a canvas, which jsdom lacks.
vi.mock('$lib/components/strategy/StrategyChart.svelte', async () => ({ default: (await import('./fixtures/Stub.svelte')).default }));
const toast = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/processTracker', () => ({ addToast: toast }));
import Creator from '../routes/strategy-creator/+page.svelte';

let target: HTMLDivElement;
let app: ReturnType<typeof mount>;

async function settle() { for (let i = 0; i < 12; i++) { await Promise.resolve(); await tick(); } }
// Overlays (launcher, save prompt) render under <body>, outside the page.
function button(text: string) { return [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!; }
function text() { return target.textContent ?? ''; }
function preview(tradeCount: number, extra: Record<string, unknown> = {}) {
	return { bars: [], entry_markers: [], exit_markers: [], main_indicators: [], sub_indicators: [],
		strategy_params: {}, trade_count: tradeCount, exit_reasons: { signal: tradeCount }, signal_bars: { entry_long: 40 }, warnings: [], ...extra };
}
// Params exactly as the page saves them, so an unedited draft is not "dirty".
const savedParams = (profile: Record<string, unknown> = {}) => ({
	execution_profile: { sizing_mode: 'full', risk_per_trade: 0.02, fixed_size: 1000, atr_stop_multiplier: 2, kelly_multiplier: 0.5,
		kelly_lookback: 100, stop_loss_pct: null, take_profit_pct: null, trailing_stop_pct: null, time_stop_bars: null, ...profile },
	leverage: 1, trade_mode: 'long_only',
	_creator_context: { start: '2025-01-01', end: '2025-12-31', initial_capital: 10000, fee_bps: 10, slippage_bps: 5 },
});
const savedEntry = (overrides: Record<string, unknown> = {}) => ({
	id: 'lib_saved', owner: 'operator', name: 'Saved RSI', kind: 'visual', description: '', code: null,
	spec: { indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: {},
		entry_long: { logic: 'and', conditions: [{ left: 'rsi', op: '<', right: 30 }] }, exit_long: null, entry_short: null, exit_short: null },
	symbol: 'BTC/USDT', timeframe: '1h', params: savedParams(), tags: [], status: 'draft', version: 1, parent_library_id: null,
	forge_strategy_id: null, last_result_id: null, created_at: '', updated_at: '', ...overrides,
});
// The first template's knobs are oversold (30) and exit_level (55).
function knob(name: string) {
	const row = [...target.querySelectorAll('input[aria-label="parameter name"]')].find((i) => (i as HTMLInputElement).value === name)!;
	return row.parentElement!.querySelector('input[aria-label="parameter value"]') as HTMLInputElement;
}
async function runAndReadSpec() {
	api.submitBacktest.mockResolvedValue({ status: 'queued' });
	await fireEvent.click(button('Run Backtest')); await settle();
	return api.submitBacktest.mock.calls.at(-1)![0].params.spec;
}

beforeEach(async () => {
	vi.clearAllMocks();
	vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }); // the 500ms preview debounce fires only when advanced
	api.getIndicators.mockResolvedValue([]);
	api.getSymbols.mockResolvedValue([]);
	api.getPrebuiltStrategies.mockResolvedValue({ strategies: [] });
	api.listStrategyLibrary.mockResolvedValue([]);
	api.previewStrategyChart.mockResolvedValue(preview(3));
	target = document.createElement('div');
	document.body.appendChild(target);
	app = mount(Creator, { target });
	await settle();
});
afterEach(async () => { await unmount(app); target.remove(); vi.useRealTimers(); });

it('keeps the rules, edits included, when switching to Python and back', async () => {
	expect(text()).not.toContain('Add at least one entry condition');
	await fireEvent.input(knob('oversold'), { target: { value: '25' } }); await settle();
	await fireEvent.click(button('Python')); await settle();
	await fireEvent.click(button('Rules')); await settle();
	expect(text()).not.toContain('Add at least one entry condition');
	expect((await runAndReadSpec()).params.oversold).toBe(25);
});

it('shows only the newest preview when an older request answers last', async () => {
	const pending: Array<(value: unknown) => void> = [];
	api.previewStrategyChart.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
	await fireEvent.click(button('Refresh')); await settle();
	await fireEvent.click(button('Refresh')); await settle();
	pending[1](preview(7)); await settle();
	pending[0](preview(99)); await settle();
	expect(target.querySelector('[data-testid="preview-stats"]')?.textContent).toContain('Trades: 7');
});

it('previews with the execution settings a backtest would use', async () => {
	await vi.advanceTimersByTimeAsync(600); await settle();
	const request = api.previewStrategyChart.mock.calls.at(-1)![0];
	expect(request).toMatchObject({ sizing_mode: 'full', leverage: 1, fee_bps: 10, slippage_bps: 5, initial_capital: 10000 });
	expect(text()).toContain('Trades: 3');
});

it('charges the deflated Sharpe for each distinct version of the rules previewed', async () => {
	await vi.advanceTimersByTimeAsync(600); await settle();
	expect(api.previewStrategyChart.mock.calls.at(-1)![0].trials).toBe(1);
	await fireEvent.input(knob('oversold'), { target: { value: '25' } }); await settle();
	await vi.advanceTimersByTimeAsync(600); await settle();
	expect(api.previewStrategyChart.mock.calls.at(-1)![0].trials).toBe(2);
	await fireEvent.input(knob('oversold'), { target: { value: '30' } }); await settle();
	await vi.advanceTimersByTimeAsync(600); await settle();
	expect(api.previewStrategyChart.mock.calls.at(-1)![0].trials).toBe(2); // v1 again, not a new trial
	expect(text()).toContain('2 versions tried');
});

it('undoes and redoes rule edits, one step per number tuned', async () => {
	await fireEvent.input(knob('oversold'), { target: { value: '25' } }); await settle();
	await fireEvent.input(knob('oversold'), { target: { value: '20' } }); await settle();
	await fireEvent.click(target.querySelector('button[aria-label="Undo"]')!); await settle();
	expect(knob('oversold').value).toBe('30');
	expect((await runAndReadSpec()).params.oversold).toBe(30);
	await fireEvent.click(target.querySelector('button[aria-label="Redo"]')!); await settle();
	expect(knob('oversold').value).toBe('20');
});

it('keeps slow tuning of one number as one undo step, and another number as the next', async () => {
	const clock = vi.spyOn(Date, 'now');
	try {
		clock.mockReturnValue(1_000_000);
		await fireEvent.input(knob('oversold'), { target: { value: '25' } }); await settle();
		clock.mockReturnValue(1_010_000);
		await fireEvent.input(knob('oversold'), { target: { value: '20' } }); await settle();
		clock.mockReturnValue(1_020_000);
		await fireEvent.input(knob('exit_level'), { target: { value: '60' } }); await settle();
		const undo = target.querySelector('button[aria-label="Undo"]')!;
		await fireEvent.click(undo); await settle();
		expect([knob('oversold').value, knob('exit_level').value]).toEqual(['20', '55']);
		await fireEvent.click(undo); await settle();
		expect([knob('oversold').value, knob('exit_level').value]).toEqual(['30', '55']);
	} finally {
		clock.mockRestore();
	}
});

it('shows an AI change as a diff and applies it only when accepted', async () => {
	await fireEvent.click(button('AI assist')); await settle();
	await fireEvent.input(target.querySelector('textarea')!, { target: { value: 'buy deeper dips' } }); await settle();
	const before = await runAndReadSpec();
	api.nlEditSpec.mockResolvedValue({ valid: true, errors: [], warnings: [], provider: 'test',
		spec: { ...before, params: { ...before.params, oversold: 20 } } });
	await fireEvent.click(button('Change current rules')); await settle();
	expect(api.nlEditSpec).toHaveBeenCalledWith(expect.objectContaining({ description: 'buy deeper dips', spec: before }));
	const proposal = target.querySelector('[data-testid="ai-proposed-change"]')!;
	expect(proposal.textContent).toContain('oversold');
	expect(proposal.textContent).toContain('20');
	expect(knob('oversold').value).toBe('30'); // nothing applied yet
	await fireEvent.click(button('Apply change')); await settle();
	expect(knob('oversold').value).toBe('20');
	expect(target.querySelector('[data-testid="ai-proposed-change"]')).toBeNull();
});

it('stress-tests exactly what the preview runs', async () => {
	await vi.advanceTimersByTimeAsync(600); await settle();
	api.stressTestStrategy.mockResolvedValue({
		base: { trades: 10, net_return: 0.1, oos_trades: 4, oos_return: 0.05 },
		knobs: [{ target: 'param', name: 'oversold', label: 'oversold', value: 30, integer: true,
			variants: [-0.25, -0.1, 0.1, 0.25].map((step) => ({ step, value: 30 * (1 + step), trades: 9, net_return: 0.08, oos_trades: 4, oos_return: 0.04 })) }],
		verdict: { status: 'stable', text: 'Every ±10% nudge keeps at least half of the out-of-sample result.', fragile: [] },
		warnings: [],
	});
	await fireEvent.click(button('Stress test')); await settle();
	await fireEvent.click(button('Run stress test')); await settle();
	const { trials: _t, ...previewed } = api.previewStrategyChart.mock.calls.at(-1)![0];
	expect(api.stressTestStrategy.mock.calls[0][0]).toMatchObject(previewed);
	expect(target.querySelector('[data-testid="stress-verdict"]')?.textContent).toContain('stable');
});

it('explains why the selected trade opened', async () => {
	api.previewStrategyChart.mockResolvedValue(preview(1, {
		trades: [{ n: 1, direction: 'long', sample: 'out', entry_time: '2025-06-01T10:00:00+00:00', entry_price: 100, exit_time: '2025-06-02T10:00:00+00:00',
			exit_price: 104, exit_reason: 'signal', pnl_pct: 0.04, bars_held: 24, cost_pct: 0.002, funding_pct: 0, size_fraction: 1,
			entry_signal_time: '2025-06-01T09:00:00+00:00', exit_signal_time: '2025-06-02T09:00:00+00:00',
			entry_rule: { logic: 'and', result: true, items: [{ kind: 'cond', left: 'rsi', op: '<', right: { param: 'oversold' }, left_value: 27.5, right_value: 30, result: true }] },
			exit_rule: { logic: 'or', result: true, items: [{ kind: 'cond', left: 'rsi', op: '>', right: { param: 'exit_level' }, left_value: 56, right_value: 55, result: true }] } }],
	}));
	await vi.advanceTimersByTimeAsync(600); await settle();
	expect(text()).toContain('Trade #1');
	expect(text()).toContain('oversold (30)');
	expect(text()).toContain('27.5');
	expect(text()).toContain('+4.00%');
});

it('says what the engine runs when full equity has no exit', async () => {
	const summary = () => target.querySelector('[data-testid="execution-summary"]')?.textContent;
	expect(summary()).toContain('Default sizing: 1% risk, 2× ATR stop');
	await fireEvent.click([...target.querySelectorAll('button')].find((b) => b.textContent?.includes('Execution Settings'))!); await settle();
	const stop = [...target.querySelectorAll('label')].find((l) => l.textContent?.trim() === 'Stop Loss %')!.querySelector('input')!;
	await fireEvent.input(stop, { target: { value: '3' } }); await settle();
	expect(summary()).toContain('Full equity · stop 3%');
});

it('blocks a Kelly run, which the engine cannot size from a cold start', async () => {
	api.listStrategyLibrary.mockResolvedValue([savedEntry({ params: savedParams({ sizing_mode: 'kelly' }) })]);
	await fireEvent.click(button('Open…')); await settle();
	await fireEvent.click(button('Open')); await settle();
	await fireEvent.click(button('Run Backtest')); await settle();
	expect(api.submitBacktest).not.toHaveBeenCalled();
	expect(text()).toContain('Kelly sizing sizes each trade');
});

it('links to the Forge strategy instead of sending the same revision again', async () => {
	api.listStrategyLibrary.mockResolvedValue([savedEntry({ forge_strategy_id: 'S01234', status: 'in_forge' })]);
	await fireEvent.click(button('Open…')); await settle();
	await fireEvent.click(button('Open')); await settle();
	const link = [...target.querySelectorAll('a')].find((a) => a.textContent?.trim() === 'Open in Forge →');
	expect(link?.getAttribute('href')).toBe('/lab/strategy/S01234');
	expect(button('Send to Forge →')).toBeUndefined();
});

it('reports a revision the Forge already holds instead of claiming a new send', async () => {
	api.listStrategyLibrary.mockResolvedValue([savedEntry()]);
	api.sendLibraryStrategyToForge.mockResolvedValue({ ok: true, id: 'lib_saved', already_in_forge: true, strategy: savedEntry(),
		forge: { ok: true, strategy_id: 'S01234', display_id: 'S01234', stage: 'gauntlet', type: 'rule_engine' } });
	await fireEvent.click(button('Open…')); await settle();
	await fireEvent.click(button('Open')); await settle();
	await fireEvent.click(button('Send to Forge →')); await settle();
	expect(toast).toHaveBeenCalledWith(expect.stringContaining('already in the Forge as S01234'), 'info', '/lab/strategy/S01234');
});

it('refuses an AI draft the builder cannot show instead of rewriting it', async () => {
	const deep = { logic: 'or', conditions: [{ left: 'close', op: '>', right: 1 },
		{ logic: 'and', conditions: [{ left: 'open', op: '>', right: 1 }, { logic: 'or', conditions: [] }] }] };
	api.nlToSpec.mockResolvedValue({ valid: true, spec: { indicators: [], params: {}, entry_long: { logic: 'and', conditions: [deep] } }, errors: [], warnings: [] });
	await fireEvent.click(button('AI assist')); await settle();
	await fireEvent.input(target.querySelector('textarea')!, { target: { value: 'nested idea' } }); await settle();
	await fireEvent.click(button('Generate strategy')); await settle();
	expect(text()).toContain('more than one level deep');
	expect(button('Generate strategy')).toBeTruthy(); // the prompt stays, the draft is untouched
	expect(knob('oversold').value).toBe('30');
});
