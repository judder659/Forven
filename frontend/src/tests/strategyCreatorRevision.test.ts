import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';
const api = vi.hoisted(() => Object.fromEntries([
 'getIndicators','previewStrategyChart','nlToSpec','listStrategyLibrary','createLibraryStrategy',
 'updateLibraryStrategy','deleteLibraryStrategy','duplicateLibraryStrategy','sendLibraryStrategyToForge',
 'getSystemStrategyDetail','getPrebuiltStrategies','getStrategies','submitBacktest','registerCustomStrategy','getResult','getSymbols',
].map(name => [name, vi.fn()])));
vi.mock('$lib/api', () => api);
const readiness = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/strategyCreator', () => ({ checkIdeaReadiness: readiness }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/components/chart/ChartWorkspace.svelte', async () => ({ default: (await import('./fixtures/Stub.svelte')).default }));
vi.mock('$lib/stores/processTracker', () => ({ addToast: vi.fn() }));
import Creator from '../routes/strategy-creator/+page.svelte';
let target: HTMLDivElement;
let app: ReturnType<typeof mount>;
let stored: any[];
async function settle() { for (let i=0;i<12;i++) { await Promise.resolve(); await tick(); } }
function button(text: string) { return [...target.querySelectorAll('button')].find(b => b.textContent?.trim() === text)!; }
beforeEach(async () => {
 vi.clearAllMocks(); stored=[];
 api.getIndicators.mockResolvedValue([]); api.getSymbols.mockResolvedValue([]);
 api.getPrebuiltStrategies.mockResolvedValue({strategies:[]});
 api.listStrategyLibrary.mockImplementation(async () => stored);
 api.createLibraryStrategy.mockImplementation(async (body) => { const row={...body,id:'lib_test',version:1,status:'draft'};stored=[row];return row; });
 api.registerCustomStrategy.mockResolvedValue({valid:true,registered:true,strategy_name:'example',default_params:{period:14}});
 target=document.createElement('div'); document.body.appendChild(target);
 app=mount(Creator,{target});await settle();await fireEvent.click(button('Code'));await settle();
});
afterEach(async () => { await unmount(app);target.remove(); });

it('checks inputs without generating and clears the report when the idea changes', async () => {
 await fireEvent.click(button('AI')); await settle();
 const input = target.querySelector('textarea')!;
 await fireEvent.input(input, {target:{value:'Use funding'}}); await settle();
 readiness.mockResolvedValue({can_generate:false,status:'blocked',required:['funding_rate'],present:[],issues:['Funding is missing'],warnings:[]});
 await fireEvent.click(button('Check data')); await settle();
 expect(target.textContent).toContain('Funding is missing');
 expect(api.nlToSpec).not.toHaveBeenCalled();
 await fireEvent.input(input, {target:{value:'Use RSI'}}); await settle();
 expect(target.textContent).not.toContain('Funding is missing');
});

it('does not apply generation returned after the user edits the idea', async () => {
 await fireEvent.click(button('AI')); await settle();
 const input = target.querySelector('textarea')!;
 await fireEvent.input(input, {target:{value:'Use RSI'}}); await settle();
 let finish!: (value: unknown) => void;
 api.nlToSpec.mockImplementation(() => new Promise(resolve => {finish=resolve;}));
 await fireEvent.click(button('Generate strategy')); await settle();
 await fireEvent.input(input, {target:{value:'Use EMA'}}); await settle();
 finish({valid:true,spec:{indicators:[],params:{},entry_long:null}}); await settle();
 expect(button('Generate strategy')).toBeTruthy();
 expect(input.value).toBe('Use EMA');
});
it('requires revalidation after code edits and does not submit the previous code',async () => {
 await fireEvent.click(button('Validate & load'));await settle();
 expect(target.textContent).toContain('Loaded');
 await fireEvent.input(target.querySelector('textarea')!,{target:{value:'changed code'}});await settle();
 await fireEvent.click(button('Run Backtest'));await settle();
 expect(api.submitBacktest).not.toHaveBeenCalled();
 expect(target.textContent).toContain('Validate & load your custom strategy first');
});
it('saves execution context and blocks sending unsaved edits',async () => {
 await fireEvent.click(button('Save to library'));await settle();
 const buttons=[...target.querySelectorAll('button')];
 const save=buttons.find(b=>b.textContent?.includes('Save as new'));
 expect(save).toBeTruthy(); await fireEvent.click(save!);await settle();
 expect(api.createLibraryStrategy).toHaveBeenCalled();
 const payload=api.createLibraryStrategy.mock.calls[0][0];
 expect(payload.params.execution_profile.sizing_mode).toBe('full');
 expect(payload.params._creator_context.fee_bps).toBe(10);
 expect(button('Send to Forge →').disabled).toBe(false);
 await fireEvent.input(target.querySelector('textarea')!,{target:{value:'new unsaved revision'}});await settle();
 expect(button('Send to Forge →').disabled).toBe(true);
 expect(target.textContent).toContain('Unsaved changes');
 expect(api.sendLibraryStrategyToForge).not.toHaveBeenCalled();
});
it('does not mark the saved draft tested when the run uses unsaved edits',async () => {
 await fireEvent.click(button('Validate & load'));await settle();
 await fireEvent.click(button('Save to library'));await settle();
 await fireEvent.click(button('Save as new'));await settle();
 await fireEvent.input(target.querySelector('textarea')!,{target:{value:'changed implementation'}});await settle();
 await fireEvent.click(button('Validate & load'));await settle();
 api.submitBacktest.mockResolvedValue({status:'succeeded',result_id:'result-new'});
 api.getResult.mockResolvedValue(null);
 await fireEvent.click(button('Run Backtest'));await settle();
 expect(api.submitBacktest).toHaveBeenCalled();
 expect(api.updateLibraryStrategy).not.toHaveBeenCalled();
});
it('restores saved execution settings when reopening a library strategy', async () => {
 const advanced=[...target.querySelectorAll('button')].find(b=>b.textContent?.includes('Execution Settings'))!;
 await fireEvent.click(advanced);await settle();
 const input=(name:string)=>[...target.querySelectorAll('label')].find(l=>l.textContent?.trim()===name)!.querySelector('input')!;
 await fireEvent.input(input('Leverage'),{target:{value:'3'}});
 await fireEvent.input(input('Fee (bps)'),{target:{value:'12'}});
 await fireEvent.input(input('Stop Loss %'),{target:{value:'2.5'}});await settle();
 await fireEvent.click(button('Save to library'));await settle();await fireEvent.click(button('Save as new'));await settle();
 await fireEvent.input(input('Leverage'),{target:{value:'8'}});await settle();
 await fireEvent.change(target.querySelector('select[title="Open any strategy in the system"]')!,{target:{value:'lib:lib_test'}});await settle();
 expect(input('Leverage').value).toBe('3');
 expect(input('Fee (bps)').value).toBe('12');
 expect(input('Stop Loss %').value).toBe('2.5');
 expect(button('Send to Forge →').disabled).toBe(false);
});
