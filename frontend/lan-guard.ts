import type { Server } from 'node:http';
import type { Socket } from 'node:net';
import type { Plugin } from 'vite';

// The launchers start Vite on `::` so both localhost (::1) and 127.0.0.1 work,
// but that also listens on every network interface. Vite proxies /api to a
// backend bound to 127.0.0.1, and the backend trusts loopback callers when no
// API key is set, so anyone on the same network could drive trading through
// the dashboard port. Refuse non-loopback connections unless the operator has
// opened the backend itself to the network, which the backend only allows with
// FORVEN_API_KEY and FORVEN_OPERATOR_KEY set (api_security.assert_safe_bind_host).

export function isLoopbackAddress(address: string | undefined | null): boolean {
	if (!address) return false;
	const value = address.trim().toLowerCase();
	if (value === '::1' || value === 'localhost') return true;
	const v4 = value.startsWith('::ffff:') ? value.slice('::ffff:'.length) : value;
	return v4.startsWith('127.');
}

// Mirrors api_security.resolved_bind_host: FORVEN_BIND_HOST, then FORVEN_HOST.
export function dashboardAllowsNetwork(env: Record<string, string | undefined>): boolean {
	for (const name of ['FORVEN_BIND_HOST', 'FORVEN_HOST']) {
		const value = (env[name] ?? '').trim();
		if (value) return !isLoopbackAddress(value);
	}
	return false;
}

export function guardServer(httpServer: Server, log: (message: string) => void = console.warn): void {
	const warned = new Set<string>();
	httpServer.prependListener('connection', (socket: Socket) => {
		const remote = socket.remoteAddress;
		if (isLoopbackAddress(remote)) return;
		const key = remote ?? 'unknown';
		if (!warned.has(key)) {
			warned.add(key);
			log(
				`[forven] Refused a dashboard connection from ${key}. The dashboard only accepts this ` +
					'computer. To use it from other devices, set FORVEN_BIND_HOST=0.0.0.0 together with ' +
					'FORVEN_API_KEY and FORVEN_OPERATOR_KEY in .env.'
			);
		}
		socket.destroy();
	});
}

export function lanGuard(env: Record<string, string | undefined> = process.env): Plugin {
	const apply = (httpServer: Server | null | undefined) => {
		if (httpServer && !dashboardAllowsNetwork(env)) guardServer(httpServer);
	};
	return {
		name: 'forven-lan-guard',
		configureServer(server) {
			apply(server.httpServer as Server | null);
		},
		configurePreviewServer(server) {
			apply(server.httpServer as Server | null);
		}
	};
}
