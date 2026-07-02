import { fetchApi } from './core';

// ── Types ──────────────────────────────────────────────────────────

export interface RegisteredWallet {
	label: string;
	address: string;
	equity_usd: number | null;
	perp_usd: number | null;
	spot_usd: number | null;
	open_trades: number;
	bots: string[];
}

export interface DiscoveredSubaccount {
	name: string;
	address: string;
	equity_usd: number | null;
}

export interface MasterWalletBalances {
	address: string;
	perp_usd: number | null;
	spot_usd: number | null;
}

export interface BookWallet {
	label: 'long' | 'short';
	address: string;
	perp_usd: number | null;
	spot_usd: number | null;
}

export interface WalletsSnapshot {
	master: MasterWalletBalances | null;
	registered: RegisteredWallet[];
	book_wallets: BookWallet[];
	discovered: DiscoveredSubaccount[];
	discovery_error: string | null;
	books: {
		enabled: boolean;
		long_only: boolean;
		long_book_configured: boolean;
		short_book_configured: boolean;
		named_wallets: string[];
		subaccount_volume_requirement_usd: number;
		note: string | null;
	};
	master_wallet: string | null;
	can_transfer: boolean;
}

export interface WalletTransferResult {
	label: string;
	address: string;
	amount_usd: number;
	deposit: boolean;
	status: string;
}

// ── Calls ──────────────────────────────────────────────────────────

/** light=true skips balances + exchange discovery — instant label list for
 * pickers (e.g. the GO LIVE wallet select). */
export async function listWallets(light = false): Promise<WalletsSnapshot> {
	return fetchApi(light ? '/wallets?light=true' : '/wallets');
}

/** Create a Hyperliquid sub-account named `name` and register it as a wallet. */
export async function createSubaccount(name: string): Promise<{ label: string; address: string }> {
	return fetchApi('/wallets/subaccounts', {
		method: 'POST',
		body: JSON.stringify({ name }),
	});
}

/** Register an existing sub-account address as a named wallet. */
export async function registerWallet(label: string, address: string): Promise<{ label: string; address: string }> {
	return fetchApi('/wallets/register', {
		method: 'POST',
		body: JSON.stringify({ label, address }),
	});
}

export async function removeWallet(label: string): Promise<{ label: string; removed: boolean }> {
	return fetchApi(`/wallets/${label}`, { method: 'DELETE' });
}

/** Move USD master ↔ sub-account. Refused by protocol on agent-wallet setups. */
export async function transferWallet(
	label: string,
	amountUsd: number,
	direction: 'deposit' | 'withdraw'
): Promise<WalletTransferResult> {
	return fetchApi(`/wallets/${label}/transfer`, {
		method: 'POST',
		body: JSON.stringify({ amount_usd: amountUsd, direction }),
	});
}

/** Move USDC between a wallet's SPOT and PERP balances.
 * wallet null/"master" = master wallet; else a named-wallet label. */
export async function classTransfer(
	wallet: string | null,
	amountUsd: number,
	toPerp: boolean
): Promise<{ wallet: string; amount_usd: number; to_perp: boolean; status: string }> {
	return fetchApi('/wallets/class-transfer', {
		method: 'POST',
		body: JSON.stringify({ wallet, amount_usd: amountUsd, to_perp: toPerp }),
	});
}
