import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import AgentOutcomes from '../lib/components/AgentOutcomes.svelte';
const api=vi.hoisted(()=>({get:vi.fn()}));
vi.mock('$lib/api/agentOutcomes',()=>({getAgentOutcomes:api.get}));
let app:ReturnType<typeof mount>;
let target:HTMLDivElement;
const report={days:7,candidates:{candidates:10,evaluating:3,paper:2,live:1,retired:4},tasks:{blocked:2,running:1,pending:3},usage:{calls:5,tokens:5000,priced_cost_usd:0,unpriced_calls:5,estimated_unpriced_usd:2},usage_scope:'Calls recorded since the usage ledger was enabled; not an invoice.'};
async function settle(){for(let i=0;i<10;i++){await Promise.resolve();await tick();}}
beforeEach(()=>{vi.useFakeTimers();api.get.mockReset().mockResolvedValue(report);target=document.createElement('div');document.body.appendChild(target);});
afterEach(async()=>{await unmount(app);target.remove();vi.useRealTimers();});
it('shows research stages and explicitly unpriced usage',async()=>{
 app=mount(AgentOutcomes,{target});await settle();
 expect(target.textContent).toContain('Candidates created');
 expect(target.textContent).toContain('5 calls unpriced');
 expect(target.textContent).toContain('budget estimate $2.00');
 expect(target.textContent).toContain('Needs attention (2)');
});
it('withdraws stale outcomes on refresh failure',async()=>{
 app=mount(AgentOutcomes,{target});await settle();
 api.get.mockRejectedValue(new Error('offline'));await vi.advanceTimersByTimeAsync(30_000);await settle();
 expect(target.textContent).toContain('reporting unavailable');
 expect(target.textContent).not.toContain('5 calls unpriced');
 expect(target.textContent).not.toContain('Candidates created');
});
it('does not represent an empty usage ledger as free usage',async()=>{
 api.get.mockResolvedValue({...report,usage:{...report.usage,calls:0}});
 app=mount(AgentOutcomes,{target});await settle();
 expect(target.textContent).toContain('No per-call cost records yet');
 expect(target.textContent).not.toContain('$0.00 priced');
});
