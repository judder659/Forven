import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import SourceHealth from '../lib/components/research/SourceHealth.svelte';
const api = vi.hoisted(() => ({ health: vi.fn() }));
vi.mock('$lib/api/data', () => ({ getCollectionHealth: api.health }));
let app: ReturnType<typeof mount> | null;
let target: HTMLDivElement;
const healthy = { score: 100, streams: [{stream:'ohlcv',status:'healthy',consecutive_failures:0,last_success:'2026-09-08T12:00:00Z',last_run:'2026-09-08T12:00:00Z',last_error:null,total_rows:100}] };
async function settle() { for(let i=0;i<8;i++){await Promise.resolve();await tick();} }
beforeEach(() => {vi.useFakeTimers();vi.spyOn(document,'hidden','get').mockReturnValue(false);api.health.mockReset().mockResolvedValue(healthy);target=document.createElement('div');document.body.appendChild(target);});
afterEach(async () => {if(app)await unmount(app);app=null;target.remove();vi.restoreAllMocks();vi.useRealTimers();});
it('refreshes background collection without an active manual download and stops on unmount', async () => {
 app=mount(SourceHealth,{target});await settle();expect(api.health).toHaveBeenCalledTimes(1);
 await vi.advanceTimersByTimeAsync(30_000);await settle();expect(api.health).toHaveBeenCalledTimes(2);
 await unmount(app);app=null;await vi.advanceTimersByTimeAsync(60_000);expect(api.health).toHaveBeenCalledTimes(2);
});
it('withdraws a prior success score on failure and recovers on the next poll', async () => {
 app=mount(SourceHealth,{target});await settle();expect(target.textContent).toContain('100/100');
 api.health.mockRejectedValue(new Error('Connection unavailable'));await vi.advanceTimersByTimeAsync(30_000);await settle();
 expect(target.textContent).not.toContain('100/100');expect(target.textContent).toContain('Collection status unavailable');expect(target.textContent).toContain('Last successful check');
 api.health.mockResolvedValue(healthy);await vi.advanceTimersByTimeAsync(30_000);await settle();expect(target.textContent).toContain('100/100');expect(target.textContent).not.toContain('Collection status unavailable');
});
it('does not overlap slow requests', async () => {
 let resolve!: (value:typeof healthy)=>void;api.health.mockImplementation(()=>new Promise(r=>resolve=r));
 app=mount(SourceHealth,{target});await settle();await vi.advanceTimersByTimeAsync(90_000);expect(api.health).toHaveBeenCalledTimes(1);
 resolve(healthy);await settle();expect(target.textContent).toContain('Collection reliability');
});
it('opens repeated failures and distinguishes unobserved streams from complete coverage', async () => {
 api.health.mockResolvedValue({score:50,streams:[{...healthy.streams[0],status:'down',consecutive_failures:5,last_error:'Rate limited'},{...healthy.streams[0],stream:'liquidations',status:'never_ran',last_success:null}]});
 app=mount(SourceHealth,{target});await settle();expect(target.querySelector('details')?.open).toBe(true);
 expect(target.textContent).toContain('1 streams reporting repeated failures');expect(target.textContent).toContain('1 not observed');expect(target.textContent).toContain('Rate limited');expect(target.textContent).toContain('does not establish complete or current data');
});
it('leaves healthy details collapsed and pauses polling in a hidden tab', async () => {
 app=mount(SourceHealth,{target});await settle();expect(target.querySelector('details')?.open).toBe(false);
 vi.spyOn(document,'hidden','get').mockReturnValue(true);document.dispatchEvent(new Event('visibilitychange'));
 await vi.advanceTimersByTimeAsync(60_000);expect(api.health).toHaveBeenCalledTimes(1);
});
