<script lang="ts">
	import { updateSettingsSection } from '$lib/api';
	import { listWallets, type WalletsSnapshot } from '$lib/api/wallets';
	import { openExternal } from '$lib/external-open';

	export let settings: Record<string, unknown> = {};
	export let mode: 'paper' | 'hyperliquid' = 'paper';
	export let onSettingsUpdated: (() => Promise<void> | void) | undefined = undefined;

	$: alreadyConfigured = Boolean(settings?.hyperliquid_has_key);
	$: savedWallet = String(settings?.hyperliquid_wallet ?? '').trim();
	$: savedApiAddress = String(settings?.hyperliquid_api_address ?? '').trim();
	$: savedTestnet = settings?.hyperliquid_testnet !== false;

	let walletAddress = '';
	let apiPrivateKey = '';
	let customApiAddress = '';
	let useTestnet = true;
	let showKey = false;
	let editingExisting = false;
	let initializedFromSettings = false;

	let saving = false;
	let testing = false;
	let formError: string | null = null;
	let verifyResult: { ok: boolean; title: string; detail: string } | null = null;

	$: if (!initializedFromSettings && settings && Object.keys(settings).length > 0) {
		walletAddress = savedWallet;
		customApiAddress = savedApiAddress;
		useTestnet = savedTestnet;
		initializedFromSettings = true;
	}

	$: apiPortalUrl = useTestnet
		? 'https://app.hyperliquid-testnet.xyz/API'
		: 'https://app.hyperliquid.xyz/API';

	function shortenAddress(addr: string): string {
		if (!addr || addr.length < 12) return addr || 'Not set';
		return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
	}

	function formatUsd(value: number | null | undefined): string | null {
		if (value === null || value === undefined || !Number.isFinite(value)) return null;
		return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);
	}

	function validateInputs(): string | null {
		const wallet = walletAddress.trim();
		const key = apiPrivateKey.trim().replace(/^0x/i, '');
		const apiAddr = customApiAddress.trim();

		if (!wallet) return 'Enter your Hyperliquid wallet address (starts with 0x).';
		if (!/^0x[0-9a-fA-F]{40}$/.test(wallet)) {
			return 'Wallet address should look like a 42-character Ethereum address starting with 0x.';
		}
		if (!alreadyConfigured && !key) {
			return 'Paste the API wallet private key generated on Hyperliquid.';
		}
		if (key && !/^[0-9a-fA-F]{64}$/.test(key)) {
			return 'API wallet private key should be 64 hex characters (with or without a 0x prefix).';
		}
		if (apiAddr && !/^0x[0-9a-fA-F]{40}$/.test(apiAddr)) {
			return 'Custom API wallet address should be a 42-character 0x address, or leave it blank to auto-derive.';
		}
		return null;
	}

	async function verifyConnection(): Promise<void> {
		testing = true;
		verifyResult = null;
		try {
			const snap: WalletsSnapshot = await listWallets(false);
			if (snap.discovery_error) {
				verifyResult = {
					ok: false,
					title: 'Credentials saved, but verification returned a warning',
					detail: snap.discovery_error,
				};
				return;
			}
			const perp = formatUsd(snap.master?.perp_usd);
			const spot = formatUsd(snap.master?.spot_usd);
			const balances = [perp ? `Perp: ${perp}` : null, spot ? `Spot: ${spot}` : null]
				.filter(Boolean)
				.join(' · ');
			const networkLabel = (editingExisting ? useTestnet : savedTestnet) ? 'Testnet' : 'Mainnet';
			verifyResult = {
				ok: true,
				title: `Connected to Hyperliquid (${networkLabel})`,
				detail: balances
					? `Master wallet ${shortenAddress(snap.master?.address || savedWallet || walletAddress)} reachable (${balances}).`
					: `Master wallet ${shortenAddress(snap.master?.address || savedWallet || walletAddress)} reachable.`,
			};
		} catch (err) {
			verifyResult = {
				ok: false,
				title: 'Could not verify with Hyperliquid right now',
				detail: err instanceof Error ? err.message : 'Check your internet connection and wallet details.',
			};
		} finally {
			testing = false;
		}
	}

	async function saveAndVerify(): Promise<void> {
		formError = null;
		verifyResult = null;
		const validationError = validateInputs();
		if (validationError) {
			formError = validationError;
			return;
		}

		saving = true;
		try {
			const payload: Record<string, unknown> = {
				actual_wallet_address: walletAddress.trim(),
				use_testnet: useTestnet,
			};
			if (apiPrivateKey.trim()) {
				payload.api_secret_key = apiPrivateKey.trim();
			}
			if (customApiAddress.trim()) {
				payload.api_address = customApiAddress.trim();
			}

			await updateSettingsSection('hyperliquid', payload);
			apiPrivateKey = '';
			editingExisting = false;
			await onSettingsUpdated?.();
			await verifyConnection();
		} catch (err) {
			formError = err instanceof Error ? err.message : 'Could not save Hyperliquid credentials.';
		} finally {
			saving = false;
		}
	}
</script>

<div class="space-y-5" data-testid="wizard-step-trading">
	<div class="grid grid-cols-1 md:grid-cols-2 gap-3" role="radiogroup" aria-label="Choose trading mode">
		<button
			type="button"
			role="radio"
			aria-checked={mode === 'paper'}
			data-testid="trading-mode-paper"
			class="rounded-lg border p-4 text-left transition-all focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3
				{mode === 'paper'
					? 'border-emerald-500/60 bg-emerald-500/[0.07]'
					: 'border-sc-line bg-sc-bg/70 hover:border-sc-line2 hover:bg-sc-panel2/60'}"
			on:click={() => {
				mode = 'paper';
				formError = null;
			}}
		>
			<div class="flex items-center justify-between gap-2 mb-1.5">
				<span class="text-[13.5px] font-semibold text-sc-ink">Start with Paper Trading</span>
				<span class="font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 rounded border border-emerald-500/40 bg-emerald-500/15 text-emerald-300">
					Recommended
				</span>
			</div>
			<p class="text-xs text-sc-ink2 leading-relaxed">
				Zero keys, zero risk. Every strategy gets a simulated <span class="text-sc-ink font-medium">$10,000</span> paper balance on live market prices.
			</p>
			<p class="mt-2.5 text-[11px] text-sc-ink3">
				Best if you’re exploring Forven or testing ideas first.
			</p>
		</button>

		<button
			type="button"
			role="radio"
			aria-checked={mode === 'hyperliquid'}
			data-testid="trading-mode-hyperliquid"
			class="rounded-lg border p-4 text-left transition-all focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3
				{mode === 'hyperliquid'
					? 'border-sc-ink/60 bg-sc-panel2'
					: 'border-sc-line bg-sc-bg/70 hover:border-sc-line2 hover:bg-sc-panel2/60'}"
			on:click={() => {
				mode = 'hyperliquid';
				formError = null;
			}}
		>
			<div class="flex items-center justify-between gap-2 mb-1.5">
				<span class="text-[13.5px] font-semibold text-sc-ink">Connect Hyperliquid</span>
				{#if alreadyConfigured}
					<span class="font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 rounded border border-emerald-500/40 bg-emerald-500/15 text-emerald-300">
						Connected ✓
					</span>
				{:else}
					<span class="font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 rounded border border-sc-line2 text-sc-ink3">
						Testnet or Live
					</span>
				{/if}
			</div>
			<p class="text-xs text-sc-ink2 leading-relaxed">
				Link a Hyperliquid API wallet to trade on Testnet (play money) or eventually graduate strategies to real funds.
			</p>
			<p class="mt-2.5 text-[11px] text-sc-ink3">
				Optional — you can also add this later in Settings → Exchange.
			</p>
		</button>
	</div>

	{#if mode === 'paper'}
		<div class="rounded-lg border border-emerald-500/25 bg-emerald-500/[0.05] p-4 space-y-2" data-testid="paper-mode-confirmation">
			<div class="flex items-center gap-2 text-emerald-400 text-[13px] font-medium">
				<span aria-hidden="true">✓</span>
				<span>Paper Trading is ready — no exchange keys required</span>
			</div>
			<p class="text-xs text-sc-ink2 leading-relaxed">
				You can run backtests, let AI agents research &amp; build strategies, and paper-trade live markets right away.
				Whenever you’re ready for Hyperliquid Testnet or Mainnet later, head to <span class="text-sc-ink">Settings → Exchange</span>.
			</p>
		</div>
	{:else}
		{#if alreadyConfigured && !editingExisting}
			<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.05] p-4 space-y-3" data-testid="hyperliquid-connected-card">
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div>
						<div class="flex items-center gap-2">
							<span class="text-emerald-400 text-sm" aria-hidden="true">✓</span>
							<h3 class="text-[13.5px] font-semibold text-sc-ink">Hyperliquid is connected</h3>
							<span class="font-plex-cond text-[10.5px] uppercase tracking-[0.08em] px-2 py-0.5 rounded border {savedTestnet ? 'border-emerald-500/40 text-emerald-300 bg-emerald-500/10' : 'border-amber-500/40 text-amber-300 bg-amber-500/10'}">
								{savedTestnet ? 'Testnet (play money)' : 'Mainnet'}
							</span>
						</div>
						<p class="text-xs text-sc-ink2 mt-1">
							Wallet <span class="font-plex-mono text-sc-ink">{shortenAddress(savedWallet)}</span>
							{#if savedApiAddress}
								· API signer <span class="font-plex-mono text-sc-ink">{shortenAddress(savedApiAddress)}</span>
							{/if}
						</p>
					</div>
					<div class="flex items-center gap-2">
						<button
							type="button"
							class="terminal-button text-[12px]"
							disabled={testing}
							on:click={verifyConnection}
						>
							{testing ? 'Checking…' : 'Test connection'}
						</button>
						<button
							type="button"
							class="terminal-button text-[12px]"
							on:click={() => {
								walletAddress = savedWallet;
								customApiAddress = savedApiAddress;
								useTestnet = savedTestnet;
								editingExisting = true;
								verifyResult = null;
							}}
						>
							Update keys
						</button>
					</div>
				</div>

				{#if verifyResult}
					<div
						class="rounded-md border px-3 py-2 text-xs {verifyResult.ok
							? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
							: 'border-amber-500/40 bg-amber-500/10 text-amber-300'}"
						role="status"
					>
						<p class="font-medium">{verifyResult.title}</p>
						<p class="mt-0.5 opacity-90">{verifyResult.detail}</p>
					</div>
				{/if}
			</div>
		{:else}
			<div class="rounded-lg border border-sc-line bg-sc-bg/70 p-4 space-y-4" data-testid="hyperliquid-guided-form">
				<div class="rounded-md border border-sc-line bg-sc-panel2/80 p-3.5 space-y-2.5">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<h3 class="text-[12.5px] font-semibold text-sc-ink">
							How to create a safe Hyperliquid API wallet (takes 1 minute)
						</h3>
						<a
							href={apiPortalUrl}
							target="_blank"
							rel="noopener noreferrer"
							on:click|preventDefault={() => openExternal(apiPortalUrl)}
							class="text-xs text-emerald-400 hover:text-emerald-300 underline"
						>
							Open Hyperliquid API page ↗
						</a>
					</div>
					<ol class="space-y-1 text-xs text-sc-ink2 list-decimal list-inside">
						<li>Open the Hyperliquid API page and connect your wallet.</li>
						<li>Enter a name like <span class="font-plex-mono text-sc-ink">Forven</span>, click <span class="text-sc-ink font-medium">Generate</span>, then click <span class="text-sc-ink font-medium">Authorize API Wallet</span>.</li>
						<li>Paste your <span class="text-sc-ink font-medium">main wallet address</span> and the new <span class="text-sc-ink font-medium">API private key</span> below.</li>
					</ol>
					<p class="text-[11px] text-sc-ink3 border-t border-sc-line pt-2">
						<span class="text-emerald-400 font-medium">Security note:</span> A Hyperliquid API wallet can only place and cancel orders. It can <span class="text-sc-ink2 font-medium">never withdraw funds</span> from your account.
					</p>
				</div>

				<div class="space-y-1.5">
					<span class="block text-xs font-medium text-sc-ink2">Which network?</span>
					<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
						<button
							type="button"
							class="rounded-md border px-3 py-2 text-left transition-colors
								{useTestnet
									? 'border-emerald-500/60 bg-emerald-500/10 text-sc-ink'
									: 'border-sc-line bg-sc-panel text-sc-ink2 hover:border-sc-line2'}"
							on:click={() => (useTestnet = true)}
						>
							<span class="block text-xs font-semibold">Testnet (Recommended)</span>
							<span class="block text-[11px] text-sc-ink3 mt-0.5">Uses Hyperliquid play money — safe for testing live wiring.</span>
						</button>
						<button
							type="button"
							class="rounded-md border px-3 py-2 text-left transition-colors
								{!useTestnet
									? 'border-amber-500/60 bg-amber-500/10 text-sc-ink'
									: 'border-sc-line bg-sc-panel text-sc-ink2 hover:border-sc-line2'}"
							on:click={() => (useTestnet = false)}
						>
							<span class="block text-xs font-semibold">Mainnet</span>
							<span class="block text-[11px] text-sc-ink3 mt-0.5">Real account. Live orders stay locked until you also arm Mainnet in Settings.</span>
						</button>
					</div>
				</div>

				<form class="space-y-3" on:submit|preventDefault={saveAndVerify}>
					<label class="block space-y-1">
						<span class="block text-xs font-medium text-sc-ink2">Your main Hyperliquid wallet address</span>
						<input
							type="text"
							autocomplete="off"
							placeholder="0x1234… (the wallet that holds your balance)"
							bind:value={walletAddress}
							class="terminal-input font-plex-mono text-xs"
						/>
					</label>

					<div class="space-y-1">
						<div class="flex items-center justify-between">
							<label for="wizard-hl-key" class="text-xs font-medium text-sc-ink2">
								API wallet private key
							</label>
							<button
								type="button"
								class="text-[11px] text-sc-ink3 hover:text-sc-ink"
								on:click={() => (showKey = !showKey)}
							>
								{showKey ? 'Hide key' : 'Show key'}
							</button>
						</div>
						{#if showKey}
							<input
								id="wizard-hl-key"
								type="text"
								autocomplete="off"
								placeholder={alreadyConfigured ? 'Saved (leave blank to keep existing key)' : '0x… (64-character hex key from Hyperliquid)'}
								bind:value={apiPrivateKey}
								class="terminal-input font-plex-mono text-xs"
							/>
						{:else}
							<input
								id="wizard-hl-key"
								type="password"
								autocomplete="off"
								placeholder={alreadyConfigured ? 'Saved (leave blank to keep existing key)' : '0x… (64-character hex key from Hyperliquid)'}
								bind:value={apiPrivateKey}
								class="terminal-input font-plex-mono text-xs"
							/>
						{/if}
						<p class="text-[11px] text-sc-ink3">
							Stored locally in your Forven vault. Your API wallet address is derived automatically from this key.
						</p>
					</div>

					<details class="text-xs text-sc-ink3">
						<summary class="cursor-pointer hover:text-sc-ink2 select-none">
							Advanced: enter API wallet address manually (optional)
						</summary>
						<label class="block mt-2 space-y-1">
							<span class="block text-[11px] text-sc-ink3">API wallet address (only needed if different from derived key address)</span>
							<input
								type="text"
								autocomplete="off"
								placeholder="Leave blank to derive automatically"
								bind:value={customApiAddress}
								class="terminal-input font-plex-mono text-xs"
							/>
						</label>
					</details>

					{#if formError}
						<p class="text-xs text-red-400" role="alert">{formError}</p>
					{/if}

					{#if verifyResult}
						<div
							class="rounded-md border px-3 py-2 text-xs {verifyResult.ok
								? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
								: 'border-amber-500/40 bg-amber-500/10 text-amber-300'}"
							role="status"
						>
							<p class="font-medium">{verifyResult.title}</p>
							<p class="mt-0.5 opacity-90">{verifyResult.detail}</p>
						</div>
					{/if}

					<div class="flex flex-wrap items-center gap-2 pt-1">
						<button
							type="submit"
							class="terminal-button-primary text-[12px]"
							disabled={saving || testing}
						>
							{saving ? 'Saving…' : testing ? 'Verifying…' : 'Save & verify connection'}
						</button>
						{#if editingExisting}
							<button
								type="button"
								class="terminal-button text-[12px]"
								on:click={() => {
									editingExisting = false;
									formError = null;
								}}
							>
								Cancel
							</button>
						{:else}
							<button
								type="button"
								class="text-xs text-sc-ink3 hover:text-sc-ink px-2 py-1"
								on:click={() => {
									mode = 'paper';
									formError = null;
								}}
							>
								Actually, I’ll use Paper Trading for now
							</button>
						{/if}
					</div>
				</form>
			</div>
		{/if}
	{/if}
</div>
