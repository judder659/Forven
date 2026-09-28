import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';

const api = vi.hoisted(() => Object.fromEntries([
	'getIndicators', 'previewStrategyChart', 'nlToSpec', 'listStrategyLibrary', 'createLibraryStrategy',
	'updateLibraryStrategy', 'deleteLibraryStrategy', 'duplicateLibraryStrategy', 'sendLibraryStrategyToForge',
	'getSystemStrategyDetail', 'getPrebuiltStrategies', 'getStrategies', 'submitBacktest', 'registerCustomStrategy', 'getResult', 'getSymbols',
].map((name) => [name, vi.fn()])));
vi.mock('$lib/api', () => api);
vi.mock('$lib/api/strategyCreator', () => ({ checkIdeaReadiness: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/components/chart/ChartWorkspace.svelte', async () => ({ default: (await import('./fixtures/Stub.svelte')).default }));
const toast = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/processTracker', () => ({ addToast: toast }));
import Creator from '../routes/strategy-creator/+page.svelte';

let target: HTMLDivElement;
let app: ReturnType<typeof mount>;

async function settle() { for (let i = 0; i < 12; i++) { await Promise.resolve(); await tick(); } }
function button(text: string) { return [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!; }
function text() { return target.textContent ?? ''; }
function preview(tradeCount: number) {
	return { bars: [], entry_markers: [], exit_markers: [], main_indicators: [], sub_indicators: [],
		strategy_params: {}, trade_count: tradeCount, exit_reasons: { signal: tradeCount }, signal_bars: { entry_long: 40 }, warnings: [] };
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

it('keeps the visual draft when returning from another tab', async () => {
	expect(text()).not.toContain('Add at least one entry condition');
	await fireEvent.click(button('AI')); await settle();
	await fireEvent.click(button('Visual')); await settle();
	expect(text()).not.toContain('Add at least one entry condition');
	await fireEvent.click(button('Run Backtest')); await settle();
	expect(api.submitBacktest).toHaveBeenCalled();
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
	await fireEvent.click(button('My Strategies (0)')); await settle();
	await fireEvent.click(button('Open')); await settle();
	await fireEvent.click(button('Run Backtest')); await settle();
	expect(api.submitBacktest).not.toHaveBeenCalled();
	expect(text()).toContain('Kelly sizing sizes each trade');
});

it('links to the Forge strategy instead of sending the same revision again', async () => {
	api.listStrategyLibrary.mockResolvedValue([savedEntry({ forge_strategy_id: 'S01234', status: 'in_forge' })]);
	await fireEvent.click(button('My Strategies (0)')); await settle();
	await fireEvent.click(button('Open')); await settle();
	const link = [...target.querySelectorAll('a')].find((a) => a.textContent?.trim() === 'Open in Forge →');
	expect(link?.getAttribute('href')).toBe('/lab/strategy/S01234');
	expect(button('Send to Forge →')).toBeUndefined();
});

it('reports a revision the Forge already holds instead of claiming a new send', async () => {
	api.listStrategyLibrary.mockResolvedValue([savedEntry()]);
	api.sendLibraryStrategyToForge.mockResolvedValue({ ok: true, id: 'lib_saved', already_in_forge: true, strategy: savedEntry(),
		forge: { ok: true, strategy_id: 'S01234', display_id: 'S01234', stage: 'gauntlet', type: 'rule_engine' } });
	await fireEvent.click(button('My Strategies (0)')); await settle();
	await fireEvent.click(button('Open')); await settle();
	await fireEvent.click(button('Send to Forge →')); await settle();
	expect(toast).toHaveBeenCalledWith(expect.stringContaining('already in the Forge as S01234'), 'info', '/lab/strategy/S01234');
});

it('refuses an AI draft the builder cannot show instead of rewriting it', async () => {
	const deep = { logic: 'or', conditions: [{ left: 'close', op: '>', right: 1 },
		{ logic: 'and', conditions: [{ left: 'open', op: '>', right: 1 }, { logic: 'or', conditions: [] }] }] };
	api.nlToSpec.mockResolvedValue({ valid: true, spec: { indicators: [], params: {}, entry_long: { logic: 'and', conditions: [deep] } }, errors: [], warnings: [] });
	await fireEvent.click(button('AI')); await settle();
	await fireEvent.input(target.querySelector('textarea')!, { target: { value: 'nested idea' } }); await settle();
	await fireEvent.click(button('Generate strategy')); await settle();
	expect(text()).toContain('more than one level deep');
	expect(button('Generate strategy')).toBeTruthy(); // still on the AI tab, draft untouched
});
