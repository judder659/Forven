import { fetchApi } from './core';

export interface AgentOutcomes {
    days: number;
    candidates: { candidates: number; evaluating: number; paper: number; live: number; retired: number };
    tasks: Record<string, number>;
    usage: { calls: number; tokens: number; priced_cost_usd: number; unpriced_calls: number; estimated_unpriced_usd: number };
    usage_scope: string;
}
export function getAgentOutcomes(): Promise<AgentOutcomes> { return fetchApi('/agents/outcomes'); }
export function resumeAgentCheckpoint(taskId: number): Promise<{ ok: boolean }> {
    return fetchApi(`/agent-tasks/${taskId}/resume`, { method: 'POST' });
}
