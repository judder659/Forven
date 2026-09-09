import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';
const api = vi.hoisted(() => ({createHypothesisManual:vi.fn(), previewHypothesisFromUrl:vi.fn(),createHypothesisFromUrl:vi.fn(),createHypothesisFromUrls:vi.fn()}));
vi.mock('$lib/api', () => api);
vi.mock('$lib/api/data', () => ({getSymbols:vi.fn().mockResolvedValue(['BTC/USDT'])}));
import Manual from '$lib/components/hypotheses/ManualIngestDialog.svelte';
import Url from '$lib/components/hypotheses/UrlIngestDialog.svelte';
import {dispatchNotice} from '$lib/utils/crucibleIntake';
let app: ReturnType<typeof mount>;
let target: HTMLDivElement;
async function settle() {for(let i=0;i<8;i++){await Promise.resolve();await tick();}}
function setup(component: typeof Manual | typeof Url) {target=document.createElement('div');document.body.append(target);app=mount(component,{target,props:{open:true}});}
function button(label: string) {return [...target.querySelectorAll('button')].find(b=>b.textContent?.trim()===label)!;}
afterEach(async()=>{if(app)await unmount(app);target?.remove();vi.clearAllMocks();});

it('reports saved/deferred/failed dispatch honestly',()=>{
 expect(dispatchNotice({task:{task_id:null,error:'offline'}}).failed).toBe(true);
 expect(dispatchNotice({research_deferred:true,task:null}).state).toBe('deferred');
 expect(dispatchNotice({task:{task_id:42}}).state).toBe('queued');
});

it('keeps an in-flight creation intact and reuses its request identity on retry',async()=>{
 setup(Manual);await settle();
 await fireEvent.input(target.querySelector('input')!,{target:{value:'RSI'}});
 const fields=target.querySelectorAll('textarea');
 await fireEvent.input(fields[0],{target:{value:'Mean reversion'}});
 await fireEvent.input(fields[1],{target:{value:'Buy low RSI'}});await settle();
 let reject!: (error: Error)=>void;
 api.createHypothesisManual.mockImplementationOnce(()=>new Promise((_,r)=>{reject=r;}));
 await fireEvent.click(button('Create crucible'));await settle();
 expect((target.querySelector('[aria-label="Close"]') as HTMLButtonElement).disabled).toBe(true);
 await fireEvent.keyDown(target.querySelector('[role="dialog"]')!,{key:'Escape'});await settle();
 expect((target.querySelector('input') as HTMLInputElement).value).toBe('RSI');
 reject(new Error('response lost'));await settle();
 api.createHypothesisManual.mockResolvedValueOnce({ok:true,hypothesis:{id:'H_TEST'},task:{task_id:42}});
 await fireEvent.click(button('Create crucible'));await settle();
 expect(api.createHypothesisManual.mock.calls[0][0].request_id).toBe(api.createHypothesisManual.mock.calls[1][0].request_id);
});

it('discards a URL preview when its input changed during fetch',async()=>{
 setup(Url);await settle();
 let resolve!: (result: unknown)=>void;
 api.previewHypothesisFromUrl.mockImplementationOnce(()=>new Promise(r=>{resolve=r;}));
 const input=target.querySelector('textarea')!;
 await fireEvent.input(input,{target:{value:'https://example.com/first'}});await settle();
 await fireEvent.click(button('Preview'));await settle();
 await fireEvent.input(input,{target:{value:'https://example.com/second'}});await settle();
 resolve({ok:true,title:'Old source',url:'https://example.com/first',content_bytes:10,content_preview:'old'});await settle();
 expect(target.textContent).not.toContain('Old source');
 expect(input.value).toBe('https://example.com/second');
 expect(button('Preview')).toBeTruthy();
});
