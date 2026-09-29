<script lang="ts">
	import { onMount } from 'svelte';
	import {
		listWallets,
		createSubaccount,
		registerWallet,
		removeWallet,
		transferWallet,
		classTransfer,
		type WalletsSnapshot,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';

	let snapshot: WalletsSnapshot | null = null;
	let loading = true;

	// Create sub-account
	let createName = '';
	let creating = false;

	// Manual register
	let regLabel = '';
	let regAddress = '';
	let registering = false;

	// Master ↔ sub-account USD transfer (per registered wallet)
	let transferLabel: string | null = null;
	let transferAmount: number | null = null;
	let transferDirection: 'deposit' | 'withdraw' = 'deposit';
	let transferring = false;

	// Spot ↔ perp class transfer ('master' or a wallet label)
	let classLabel: string | null = null;
	let classAmount: number | null = null;
	let classToPerp = true;
	let classBusy = false;

	// Full refusal text rendered inline under the open form — toasts truncate,
	// and Hyperliquid's fund-movement refusals carry the explanation the
	// operator actually needs (agent wallets can't move funds by protocol).
	let transferError: string | null = null;
	let classError: string | null = null;

	// Remove confirm
	let confirmRemove: string | null = null;
	let removing = false;

	async function load() {
		loading = true;
		try {
			snapshot = await listWallets();
		} catch (e: any) {
			addToast(`Wallets load failed: ${e.message}`, 'error');
		} finally {
			loading = false;
		}
	}

	async function handleCreate() {
		if (creating || !createName.trim()) return;
		creating = true;
		try {
			const result = await createSubaccount(createName.trim());
			addToast(`Sub-account '${result.label}' created and registered`, 'success');
			createName = '';
			await load();
		} catch (e: any) {
			addToast(`Create failed: ${e.message}`, 'error', undefined, 10_000);
		} finally {
			creating = false;
		}
	}

	async function handleRegister(label: string, address: string) {
		if (registering) return;
		registering = true;
		try {
			const result = await registerWallet(label, address);
			addToast(`Wallet '${result.label}' registered`, 'success');
			regLabel = '';
			regAddress = '';
			await load();
		} catch (e: any) {
			addToast(`Register failed: ${e.message}`, 'error');
		} finally {
			registering = false;
		}
	}

	async function handleRemove(label: string) {
		if (removing) return;
		confirmRemove = null;
		removing = true;
		try {
			await removeWallet(label);
			addToast(`Wallet '${label}' removed`, 'success');
			await load();
		} catch (e: any) {
			addToast(`Remove failed: ${e.message}`, 'error', undefined, 8000);
		} finally {
			removing = false;
		}
	}

	async function handleTransfer(label: string) {
		if (transferring || !transferAmount || transferAmount <= 0) return;
		transferring = true;
		transferError = null;
		try {
			const result = await transferWallet(label, transferAmount, transferDirection);
			addToast(
				`$${result.amount_usd.toLocaleString()} ${result.deposit ? 'deposited to' : 'withdrawn from'} '${label}'`,
				'success'
			);
			transferLabel = null;
			transferAmount = null;
			await load();
		} catch (e: any) {
			transferError = String(e.message || e);
		} finally {
			transferring = false;
		}
	}

	async function handleClassTransfer(wallet: string) {
		if (classBusy || !classAmount || classAmount <= 0) return;
		classBusy = true;
		classError = null;
		try {
			await classTransfer(wallet === 'master' ? null : wallet, classAmount, classToPerp);
			addToast(
				`$${classAmount.toLocaleString()} moved to ${classToPerp ? 'perp' : 'spot'} on '${wallet}'`,
				'success'
			);
			classLabel = null;
			classAmount = null;
			await load();
		} catch (e: any) {
			classError = String(e.message || e);
		} finally {
			classBusy = false;
		}
	}

	function toggleClassForm(wallet: string) {
		classLabel = classLabel === wallet ? null : wallet;
		classAmount = null;
		classError = null;
		transferLabel = null;
	}

	function toggleTransferForm(label: string) {
		transferLabel = transferLabel === label ? null : label;
		transferAmount = null;
		transferError = null;
		classLabel = null;
	}

	function shortAddr(addr: string): string {
		return addr.length > 12 ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : addr;
	}

	function fmtUsd(v: number | null): string {
		if (v == null) return '—';
		return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	}

	onMount(load);
</script>

<section class="rounded-md border border-sc-line bg-sc-panel">
	<header class="flex items-center justify-between border-b border-sc-line px-4 py-3">
		<div>
			<h3 class="text-[13px] font-semibold text-sc-ink2">Wallets &amp; sub-accounts</h3>
			<p class="mt-0.5 text-xs text-sc-ink3">
				Master balances, named sub-account wallets (Bot Factory isolation), funding, and spot ⇄ perp moves.
			</p>
		</div>
		<button on:click={load} disabled={loading} class="terminal-button text-[12px]">
			{loading ? 'Refreshing…' : 'Refresh'}
		</button>
	</header>

	{#if loading && !snapshot}
		<div class="px-4 py-8 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Loading wallets…</div>
	{:else if snapshot}
		<!-- Master wallet -->
		<div class="border-b border-sc-line px-4 py-3">
			<div class="flex flex-wrap items-center gap-x-6 gap-y-2">
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Master wallet</div>
					{#if snapshot.master}
						<div class="font-mono text-sm text-sc-ink" title={snapshot.master.address}>{shortAddr(snapshot.master.address)}</div>
					{:else}
						<div class="text-sm text-yellow-500">Not configured — set the wallet address in Credentials below.</div>
					{/if}
				</div>
				{#if snapshot.master}
					<div>
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Perp balance</div>
						<div class="text-sm font-bold text-sc-ink">{fmtUsd(snapshot.master.perp_usd)}</div>
					</div>
					<div>
						<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Spot USDC</div>
						<div class="text-sm font-bold text-sc-ink">{fmtUsd(snapshot.master.spot_usd)}</div>
					</div>
					{#if snapshot.can_transfer}
						<button on:click={() => toggleClassForm('master')} class="terminal-button text-[12px]">
							Spot ⇄ Perp
						</button>
					{/if}
				{/if}
			</div>
			{#if snapshot.master && !snapshot.can_transfer}
				<p class="mt-2 text-[11px] text-sc-ink3">
					Balance moves are disabled here: your API/agent key can't move funds (by Hyperliquid protocol —
					that's what makes it withdrawal-safe). Fund wallets and shift spot ⇄ perp in the Hyperliquid app.
				</p>
			{/if}
			{#if classLabel === 'master'}
				<div class="rounded-md mt-3 flex flex-wrap items-end gap-3 border border-sc-line bg-sc-bg px-3 py-3">
					<div class="flex flex-col gap-1">
						<label for="class-amount-master" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Amount (USDC)</label>
						<input id="class-amount-master" type="number" min="1" step="any" bind:value={classAmount} class="terminal-input w-36" />
					</div>
					<div class="flex gap-1.5">
						<button on:click={() => (classToPerp = true)} class="rounded-md border px-2.5 py-1.5 text-[12px] {classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Spot → Perp</button>
						<button on:click={() => (classToPerp = false)} class="rounded-md border px-2.5 py-1.5 text-[12px] {!classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Perp → Spot</button>
					</div>
					<button on:click={() => handleClassTransfer('master')} disabled={classBusy || !classAmount || classAmount <= 0} class="terminal-button-primary text-[12px]">
						{classBusy ? 'Sending…' : 'Send'}
					</button>
				</div>
				{#if classError}
					<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-2 text-xs leading-relaxed text-red-400">{classError}</div>
				{/if}
			{/if}
		</div>

		<!-- Registered named wallets + direction-book sub-accounts -->
		{#if snapshot.registered.length > 0 || snapshot.book_wallets.length > 0}
			<div class="overflow-x-auto">
				<table class="w-full text-sm">
					<thead>
						<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							<th class="px-4 py-2">Wallet</th>
							<th class="px-4 py-2">Address</th>
							<th class="px-4 py-2">Perp</th>
							<th class="px-4 py-2">Spot</th>
							<th class="px-4 py-2">Open trades</th>
							<th class="px-4 py-2">Bots</th>
							<th class="px-4 py-2"></th>
						</tr>
					</thead>
					<tbody>
						{#each snapshot.book_wallets as book}
							<tr class="border-b border-sc-line hover:bg-sc-panel2 transition-colors">
								<td class="px-4 py-2.5">
									<span class="font-bold text-sc-ink">{book.label}</span>
									<span class="ml-1.5 border border-sc-line2 px-1 py-px font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">direction book</span>
								</td>
								<td class="px-4 py-2.5 font-mono text-xs text-sc-ink2" title={book.address}>{shortAddr(book.address)}</td>
								<td class="px-4 py-2.5 text-sc-ink">{fmtUsd(book.perp_usd)}</td>
								<td class="px-4 py-2.5 text-sc-ink">{fmtUsd(book.spot_usd)}</td>
								<td class="px-4 py-2.5 text-sc-ink3">—</td>
								<td class="px-4 py-2.5 text-xs text-sc-ink3">pipeline {book.label}s</td>
								<td class="px-4 py-2.5 text-right whitespace-nowrap text-xs">
									{#if snapshot.can_transfer}
										<button on:click={() => toggleTransferForm(book.label)} class="terminal-button px-2 py-1 text-[12px]">Fund</button>
										<button on:click={() => toggleClassForm(book.label)} class="ml-1 terminal-button px-2 py-1 text-[12px]">Spot⇄Perp</button>
									{/if}
								</td>
							</tr>
							{#if transferLabel === book.label}
								<tr class="border-b border-sc-line bg-sc-bg">
									<td colspan="7" class="px-4 py-3">
										<div class="flex flex-wrap items-end gap-3">
											<div class="flex flex-col gap-1">
												<label for="tx-amount-{book.label}" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Amount (USD)</label>
												<input id="tx-amount-{book.label}" type="number" min="1" step="any" bind:value={transferAmount} class="terminal-input w-36" />
											</div>
											<div class="flex gap-1.5">
												<button on:click={() => (transferDirection = 'deposit')} class="rounded-md border px-2.5 py-1.5 text-[12px] {transferDirection === 'deposit' ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Master → {book.label}</button>
												<button on:click={() => (transferDirection = 'withdraw')} class="rounded-md border px-2.5 py-1.5 text-[12px] {transferDirection === 'withdraw' ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">{book.label} → Master</button>
											</div>
											<button on:click={() => handleTransfer(book.label)} disabled={transferring || !transferAmount || transferAmount <= 0} class="terminal-button-primary text-[12px]">
												{transferring ? 'Sending…' : 'Send'}
											</button>
										</div>
										{#if transferError}
											<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-2 text-xs leading-relaxed text-red-400">{transferError}</div>
										{/if}
									</td>
								</tr>
							{/if}
							{#if classLabel === book.label}
								<tr class="border-b border-sc-line bg-sc-bg">
									<td colspan="7" class="px-4 py-3">
										<div class="flex flex-wrap items-end gap-3">
											<div class="flex flex-col gap-1">
												<label for="class-amount-{book.label}" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Amount (USDC)</label>
												<input id="class-amount-{book.label}" type="number" min="1" step="any" bind:value={classAmount} class="terminal-input w-36" />
											</div>
											<div class="flex gap-1.5">
												<button on:click={() => (classToPerp = true)} class="rounded-md border px-2.5 py-1.5 text-[12px] {classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Spot → Perp</button>
												<button on:click={() => (classToPerp = false)} class="rounded-md border px-2.5 py-1.5 text-[12px] {!classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Perp → Spot</button>
											</div>
											<button on:click={() => handleClassTransfer(book.label)} disabled={classBusy || !classAmount || classAmount <= 0} class="terminal-button-primary text-[12px]">
												{classBusy ? 'Sending…' : 'Send'}
											</button>
										</div>
										{#if classError}
											<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-2 text-xs leading-relaxed text-red-400">{classError}</div>
										{/if}
									</td>
								</tr>
							{/if}
						{/each}
						{#each snapshot.registered as wallet}
							<tr class="border-b border-sc-line hover:bg-sc-panel2 transition-colors">
								<td class="px-4 py-2.5 font-bold text-sc-ink">{wallet.label}</td>
								<td class="px-4 py-2.5 font-mono text-xs text-sc-ink2" title={wallet.address}>{shortAddr(wallet.address)}</td>
								<td class="px-4 py-2.5 text-sc-ink">{fmtUsd(wallet.perp_usd ?? wallet.equity_usd)}</td>
								<td class="px-4 py-2.5 text-sc-ink">{fmtUsd(wallet.spot_usd)}</td>
								<td class="px-4 py-2.5 {wallet.open_trades > 0 ? 'text-emerald-400' : 'text-sc-ink3'}">{wallet.open_trades}</td>
								<td class="px-4 py-2.5 text-xs text-sc-ink2">{wallet.bots.length ? wallet.bots.join(', ') : '—'}</td>
								<td class="px-4 py-2.5 text-right whitespace-nowrap text-xs">
									{#if snapshot.can_transfer}
										<button on:click={() => toggleTransferForm(wallet.label)} class="terminal-button px-2 py-1 text-[12px]">Fund</button>
										<button on:click={() => toggleClassForm(wallet.label)} class="ml-1 terminal-button px-2 py-1 text-[12px]">Spot⇄Perp</button>
									{/if}
									{#if confirmRemove === wallet.label}
										<button on:click={() => handleRemove(wallet.label)} disabled={removing} class="ml-1 terminal-button-danger px-2 py-1 text-[12px]">{removing ? '…' : 'Confirm'}</button>
										<button on:click={() => (confirmRemove = null)} class="ml-1 terminal-button px-2 py-1 text-[12px]">×</button>
									{:else}
										<button
											on:click={() => (confirmRemove = wallet.label)}
											disabled={wallet.open_trades > 0 || wallet.bots.length > 0}
											title={wallet.open_trades > 0 || wallet.bots.length > 0 ? 'In use — close trades / detach bots first' : 'Remove registration'}
											class="ml-1 px-2 py-1 text-[12px] text-sc-ink3 transition-colors hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-40"
										>Remove</button>
									{/if}
								</td>
							</tr>
							{#if transferLabel === wallet.label}
								<tr class="border-b border-sc-line bg-sc-bg">
									<td colspan="7" class="px-4 py-3">
										<div class="flex flex-wrap items-end gap-3">
											<div class="flex flex-col gap-1">
												<label for="tx-amount-{wallet.label}" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Amount (USD)</label>
												<input id="tx-amount-{wallet.label}" type="number" min="1" step="any" bind:value={transferAmount} class="terminal-input w-36" />
											</div>
											<div class="flex gap-1.5">
												<button on:click={() => (transferDirection = 'deposit')} class="rounded-md border px-2.5 py-1.5 text-[12px] {transferDirection === 'deposit' ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Master → {wallet.label}</button>
												<button on:click={() => (transferDirection = 'withdraw')} class="rounded-md border px-2.5 py-1.5 text-[12px] {transferDirection === 'withdraw' ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">{wallet.label} → Master</button>
											</div>
											<button on:click={() => handleTransfer(wallet.label)} disabled={transferring || !transferAmount || transferAmount <= 0} class="terminal-button-primary text-[12px]">
												{transferring ? 'Sending…' : 'Send'}
											</button>
											<span class="text-xs text-sc-ink3">Moves PERP USD between master and the sub-account.</span>
										</div>
										{#if transferError}
											<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-2 text-xs leading-relaxed text-red-400">{transferError}</div>
										{/if}
									</td>
								</tr>
							{/if}
							{#if classLabel === wallet.label}
								<tr class="border-b border-sc-line bg-sc-bg">
									<td colspan="7" class="px-4 py-3">
										<div class="flex flex-wrap items-end gap-3">
											<div class="flex flex-col gap-1">
												<label for="class-amount-{wallet.label}" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Amount (USDC)</label>
												<input id="class-amount-{wallet.label}" type="number" min="1" step="any" bind:value={classAmount} class="terminal-input w-36" />
											</div>
											<div class="flex gap-1.5">
												<button on:click={() => (classToPerp = true)} class="rounded-md border px-2.5 py-1.5 text-[12px] {classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Spot → Perp</button>
												<button on:click={() => (classToPerp = false)} class="rounded-md border px-2.5 py-1.5 text-[12px] {!classToPerp ? 'border-sc-ink bg-sc-ink text-black' : 'border-sc-line2 text-sc-ink2 hover:border-sc-line2'}">Perp → Spot</button>
											</div>
											<button on:click={() => handleClassTransfer(wallet.label)} disabled={classBusy || !classAmount || classAmount <= 0} class="terminal-button-primary text-[12px]">
												{classBusy ? 'Sending…' : 'Send'}
											</button>
											<span class="text-xs text-sc-ink3">Bots trade PERP — keep their capital on the perp side.</span>
										</div>
										{#if classError}
											<div class="mt-2 border border-red-900 bg-red-500/5 px-3 py-2 text-xs leading-relaxed text-red-400">{classError}</div>
										{/if}
									</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="border-b border-sc-line px-4 py-3 text-sm text-sc-ink3">
				No named wallets yet. A named wallet is a Hyperliquid sub-account you register under a label —
				live Bot Factory bots can then route ALL their orders to it, fully isolated from the pipeline's wallet.
			</div>
		{/if}

		<!-- Discovered (exchange-side, unregistered) -->
		{#if snapshot.discovered.length > 0}
			<div class="border-b border-sc-line px-4 py-3">
				<div class="mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">On the exchange, not registered</div>
				<div class="flex flex-wrap gap-2">
					{#each snapshot.discovered as sub}
						<div class="rounded-md flex items-center gap-2 border border-sc-line bg-sc-bg px-2.5 py-1.5 text-xs">
							<span class="font-bold text-sc-ink">{sub.name || '(unnamed)'}</span>
							<span class="font-mono text-sc-ink3" title={sub.address}>{shortAddr(sub.address)}</span>
							<span class="text-sc-ink2">{fmtUsd(sub.equity_usd)}</span>
							<button
								on:click={() => handleRegister(sub.name || sub.address.slice(2, 10).toLowerCase(), sub.address)}
								disabled={registering}
								class="terminal-button px-2 py-0.5 text-[12px]"
							>Register</button>
						</div>
					{/each}
				</div>
			</div>
		{:else if snapshot.discovery_error}
			<div class="border-b border-sc-line px-4 py-2 text-xs text-sc-ink3" title={snapshot.discovery_error}>
				Exchange discovery unavailable ({snapshot.discovery_error.slice(0, 100)}).
			</div>
		{/if}

		<!-- Create + register forms -->
		<div class="grid grid-cols-1 gap-4 px-4 py-3 lg:grid-cols-2">
			<div>
				<div class="mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Create sub-account</div>
				<div class="flex items-end gap-2">
					<div class="flex min-w-0 flex-1 flex-col gap-1">
						<label for="sub-name" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Name (becomes the wallet label)</label>
						<input id="sub-name" bind:value={createName} placeholder="botfund" class="terminal-input" />
					</div>
					<button on:click={handleCreate} disabled={creating || !createName.trim()} class="terminal-button-primary text-[12px]">
						{creating ? 'Creating…' : 'Create'}
					</button>
				</div>
				<p class="mt-1.5 text-xs leading-relaxed text-sc-ink3">
					Created on Hyperliquid under your master wallet. Mainnet requires ~${snapshot.books.subaccount_volume_requirement_usd.toLocaleString()} cumulative volume; refusals surface here.
				</p>
			</div>
			<div>
				<div class="mb-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">Register existing</div>
				<div class="flex items-end gap-2">
					<div class="flex w-32 flex-col gap-1">
						<label for="reg-label" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Label</label>
						<input id="reg-label" bind:value={regLabel} placeholder="botfund" class="terminal-input" />
					</div>
					<div class="flex min-w-0 flex-1 flex-col gap-1">
						<label for="reg-addr" class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Address</label>
						<input id="reg-addr" bind:value={regAddress} placeholder="0x…" class="terminal-input font-mono" />
					</div>
					<button
						on:click={() => handleRegister(regLabel, regAddress)}
						disabled={registering || !regLabel.trim() || !regAddress.trim()}
						class="terminal-button text-[12px]"
					>{registering ? '…' : 'Register'}</button>
				</div>
			</div>
		</div>
	{/if}
</section>
