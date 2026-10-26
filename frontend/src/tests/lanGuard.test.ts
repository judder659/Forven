// @vitest-environment node
import { createServer, type Server } from 'node:http';
import { connect } from 'node:net';
import { networkInterfaces } from 'node:os';
import { afterEach, describe, expect, it } from 'vitest';
import { dashboardAllowsNetwork, guardServer, isLoopbackAddress } from '../../lan-guard';

describe('isLoopbackAddress', () => {
	it('accepts IPv4, IPv6 and mapped loopback', () => {
		for (const address of ['127.0.0.1', '127.1.2.3', '::1', '::ffff:127.0.0.1', 'localhost']) {
			expect(isLoopbackAddress(address)).toBe(true);
		}
	});

	it('rejects LAN, unspecified and missing addresses', () => {
		for (const address of ['192.168.1.20', '::ffff:10.0.0.5', 'fe80::1', '0.0.0.0', '::', '', undefined]) {
			expect(isLoopbackAddress(address)).toBe(false);
		}
	});
});

describe('dashboardAllowsNetwork', () => {
	it('stays local by default', () => {
		expect(dashboardAllowsNetwork({})).toBe(false);
		expect(dashboardAllowsNetwork({ FORVEN_BIND_HOST: '127.0.0.1' })).toBe(false);
	});

	it('opens only when the backend itself is bound beyond loopback', () => {
		expect(dashboardAllowsNetwork({ FORVEN_BIND_HOST: '0.0.0.0' })).toBe(true);
		expect(dashboardAllowsNetwork({ FORVEN_HOST: '192.168.1.5' })).toBe(true);
	});

	it('gives FORVEN_BIND_HOST precedence over FORVEN_HOST, like the backend', () => {
		expect(dashboardAllowsNetwork({ FORVEN_BIND_HOST: '127.0.0.1', FORVEN_HOST: '0.0.0.0' })).toBe(false);
	});
});

function lanAddress(): string | undefined {
	for (const entries of Object.values(networkInterfaces())) {
		for (const entry of entries ?? []) {
			if (entry.family === 'IPv4' && !entry.internal) return entry.address;
		}
	}
	return undefined;
}

function request(host: string, port: number): Promise<'served' | 'refused'> {
	return new Promise((resolve) => {
		const socket = connect({ host, port }, () => {
			socket.write(`GET / HTTP/1.1\r\nHost: ${host}\r\nConnection: close\r\n\r\n`);
		});
		let data = '';
		socket.on('data', (chunk) => (data += chunk.toString()));
		socket.on('error', () => resolve('refused'));
		socket.on('close', () => resolve(data.includes('200 OK') ? 'served' : 'refused'));
	});
}

describe('guardServer', () => {
	let server: Server | undefined;
	afterEach(() => new Promise<void>((done) => (server ? server.close(() => done()) : done())));

	async function start(): Promise<{ port: number; warnings: string[] }> {
		const warnings: string[] = [];
		server = createServer((_req, res) => res.end('ok'));
		guardServer(server, (message) => warnings.push(message));
		await new Promise<void>((done) => server!.listen(0, '0.0.0.0', () => done()));
		const address = server.address();
		return { port: typeof address === 'object' && address ? address.port : 0, warnings };
	}

	it('serves this computer', async () => {
		const { port } = await start();
		expect(await request('127.0.0.1', port)).toBe('served');
	});

	it('refuses another device and says how to allow it', async () => {
		const lan = lanAddress();
		if (!lan) return; // no non-loopback interface in this environment
		const { port, warnings } = await start();
		expect(await request(lan, port)).toBe('refused');
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain('FORVEN_BIND_HOST');
	});
});
