import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
    window.localStorage.clear();
});

describe('backend identity', () => {
    it('honors a configured relative proxy', async () => {
        vi.stubEnv('VITE_API_BASE', '/api');
        vi.resetModules();
        const { API_BASE } = await import('../lib/api/core');
        expect(API_BASE).toBe('/api');
        const { getForvenLiveWebSocketUrls } = await import('../lib/api/forven');
        expect(getForvenLiveWebSocketUrls()).toEqual([`ws://${window.location.host}/api/ws/live`]);
    });

    it('never redirects requests or credentials to a second installation after failure', async () => {
        vi.stubEnv('VITE_API_BASE', 'http://127.0.0.1:8017/api');
        vi.resetModules();
        window.localStorage.setItem('forven_operator_key', 'test-key');
        const fetch = vi.fn().mockRejectedValue(new TypeError('connection refused'));
        vi.stubGlobal('fetch', fetch);
        const { fetchApi, ACTIVE_API_BASE } = await import('../lib/api/core');
        await expect(fetchApi('/strategies')).rejects.toThrow();
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch.mock.calls[0][0]).toBe('http://127.0.0.1:8017/api/strategies');
        expect(ACTIVE_API_BASE).toBe('http://127.0.0.1:8017/api');
        const { getForvenLiveWebSocketUrls } = await import('../lib/api/forven');
        expect(getForvenLiveWebSocketUrls()).toEqual(['ws://127.0.0.1:8017/api/ws/live']);
    });
});
