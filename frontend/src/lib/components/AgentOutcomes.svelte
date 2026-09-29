<script lang="ts">
    import { onMount } from 'svelte';
    import { createPoller } from '$lib/utils/polling';
    import { getAgentOutcomes, type AgentOutcomes } from '$lib/api/agentOutcomes';
    let data: AgentOutcomes | null = null;
    let error = '';
    let busy = false;
    let disposed = false;
    async function load() {
        if (busy) return;
        busy = true;
        try { const result = await getAgentOutcomes(); if (!disposed) { data = result; error = ''; } }
        catch { if (!disposed) { data = null; error = 'Research outcome reporting unavailable. Check the backend connection and version.'; } }
        finally { busy = false; }
    }
    onMount(() => { const poller = createPoller(load, 30_000); poller.start(); return () => { disposed = true; poller.stop(); }; });
</script>

<section class="rounded-md border border-sc-line bg-sc-panel p-4" aria-label="Agent research outcomes">
    <div class="flex items-center justify-between gap-3">
        <h2 class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink">Research output · last 7 days</h2>
        <a href="/agents?tab=tasks&status=blocked" class="text-xs text-amber-400">Needs attention {data ? `(${data.tasks.blocked || 0})` : ''}</a>
    </div>
    {#if error}<p class="mt-3 text-xs text-amber-400" role="status">{error}</p>
    {:else if data}
        <div class="mt-4 grid grid-cols-2 gap-4 md:grid-cols-5">
            {#each [['Candidates created', data.candidates.candidates], ['In evaluation', data.candidates.evaluating], ['In paper', data.candidates.paper], ['In live stage', data.candidates.live], ['Retired', data.candidates.retired]] as metric}
                <div><div class="text-xl font-mono text-sc-ink">{metric[1]}</div><div class="text-[11px] text-sc-ink2">{metric[0]}</div></div>
            {/each}
        </div>
        <p class="mt-3 text-[11px] text-sc-ink2">Current stages of agent-created candidates from this period; task completion alone is not evidence of a profitable strategy.</p>
        <p class="mt-3 text-xs text-sc-ink2">
            {data.tasks.running || 0} working · {data.tasks.pending || 0} queued ·
            {#if data.usage.calls}
                ${data.usage.priced_cost_usd.toFixed(2)} priced usage · {data.usage.tokens.toLocaleString()} tokens
                {#if data.usage.unpriced_calls} · {data.usage.unpriced_calls} calls unpriced (budget estimate ${data.usage.estimated_unpriced_usd.toFixed(2)}){/if}
            {:else}No per-call cost records yet{/if}
        </p>
        <p class="mt-1 text-[11px] text-sc-ink3">{data.usage_scope}</p>
    {:else}<p class="mt-3 text-xs text-sc-ink2">Loading research outcomes…</p>{/if}
</section>
