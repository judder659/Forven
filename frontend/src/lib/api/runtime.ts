import { fetchApi } from './core';

export interface RuntimeThreadSnapshot {
    pid: number;
    captured_at: string;
    threads: Array<{
        ident: number;
        name: string;
        native_id: number | null;
        stack: Array<{ file: string; function: string; line: number }>;
    }>;
}

export function getRuntimeThreads(): Promise<RuntimeThreadSnapshot> {
    return fetchApi<RuntimeThreadSnapshot>('/system/runtime/threads');
}

export function getRuntimeHealthProfile(): Promise<{
    pid: number;
    health: Record<string, unknown>;
    profile: Array<{
        file: string | null; function: string; line: number | null;
        calls: number; self_seconds: number; total_seconds: number;
    }>;
}> {
    return fetchApi('/system/runtime/health-profile');
}
