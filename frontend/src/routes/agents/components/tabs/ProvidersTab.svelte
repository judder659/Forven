<script lang="ts">
	/**
	 * Providers & Keys tab. Lifts the AI-providers card + full OAuth flow
	 * (device + authorization-code) from SettingsAgents.svelte verbatim, but:
	 *  - reads providers from the shared page-level store (agentsConfig), and
	 *  - surfaces the new `connected: boolean` so it's visually obvious which
	 *    providers authorize spend vs merely have an env credential.
	 *
	 * Saving a key (setForvenAuthProvider) connects; Disconnect
	 * (deleteForvenAuthProvider) revokes. After any mutation we reload the
	 * shared store so every other tab's pickers update.
	 */
	import {
		setForvenAuthProvider,
		deleteForvenAuthProvider,
		testForvenAuthProvider,
		startForvenAuthProviderOAuth,
		completeForvenAuthProviderOAuth,
		pollForvenAuthProviderOAuth,
		cancelForvenAuthProviderOAuth,
		type ForvenAuthProviderStatus,
		type ForvenAuthProviderOAuthStartResponse,
	} from '$lib/api';
	import { onDestroy } from 'svelte';
	import { openExternal } from '$lib/external-open';
	import OpenCodeGoReferralNote from '$lib/components/OpenCodeGoReferralNote.svelte';
	import { agentsConfig, isProviderConnected } from '../agentsConfigStore';

	$: providers = $agentsConfig.providers;
	$: authFile = $agentsConfig.authFile;
	$: authProvidersLoading = $agentsConfig.loading;
	$: authProvidersError = $agentsConfig.error;

	let providerActionBusy: Record<string, boolean> = {};
	let providerActionMessage: Record<string, string | null> = {};
	let providerActionError: Record<string, string | null> = {};
	let providerTokenInput: Record<string, string> = {};
	let providerBaseUrlInput: Record<string, string> = {};
	let providerOAuthState: Record<
		string,
		(ForvenAuthProviderOAuthStartResponse & { code: string }) | null
	> = {};
	let providerOAuthStatus: Record<string, string> = {};

	/**
	 * Where to sign up / get an API key per provider. Rendered as a plain "get a
	 * key" link beside the token input so connecting a new provider is self-serve.
	 * OpenCode GO is intentionally NOT here — it's a referral link, so it renders
	 * via <OpenCodeGoReferralNote> which labels it as such.
	 */
	const PROVIDER_SIGNUP_URLS: Record<string, string> = {
		'opencode-zen': 'https://opencode.ai/auth'
	};

	async function reload() {
		await agentsConfig.load();
	}

	function setProviderBusy(provider: string, busy: boolean) {
		providerActionBusy = { ...providerActionBusy, [provider]: busy };
	}
	function setProviderMessage(provider: string, msg: string | null) {
		providerActionMessage = { ...providerActionMessage, [provider]: msg };
		if (msg) setTimeout(() => setProviderMessage(provider, null), 3000);
	}
	function setProviderError(provider: string, err: string | null) {
		providerActionError = { ...providerActionError, [provider]: err };
	}

	async function saveProviderToken(provider: string) {
		const token = (providerTokenInput[provider] ?? '').trim();
		if (!token) {
			setProviderError(provider, 'Enter an API key or access token.');
			return;
		}
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			await setForvenAuthProvider(provider, { api_key: token });
			providerTokenInput = { ...providerTokenInput, [provider]: '' };
			setProviderMessage(provider, 'Connected');
			await reload();
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'Failed to save token');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	async function saveProviderBaseUrl(provider: string) {
		const url = (providerBaseUrlInput[provider] ?? '').trim();
		if (!url) {
			setProviderError(provider, 'Enter a base URL.');
			return;
		}
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			await setForvenAuthProvider(provider, { base_url: url });
			setProviderMessage(provider, 'Saved');
			await reload();
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'Failed to save base URL');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	async function testProvider(provider: string) {
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			const res = await testForvenAuthProvider(provider);
			setProviderMessage(
				provider,
				res.ok ? `Test passed (${res.status})` : `Test failed: ${res.message ?? res.status}`,
			);
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'Test failed');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	async function disconnectProvider(provider: string) {
		if (!window.confirm(`Disconnect ${provider}? This clears stored credentials and revokes spend authorization.`)) return;
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			await deleteForvenAuthProvider(provider);
			setProviderMessage(provider, 'Disconnected');
			await reload();
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'Failed to disconnect');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	const POLL_TIMERS: Record<string, ReturnType<typeof setTimeout> | null> = {};

	function stopPolling(provider: string) {
		const t = POLL_TIMERS[provider];
		if (t) clearTimeout(t);
		POLL_TIMERS[provider] = null;
	}

	// Cancel any in-flight OAuth polls when the tab unmounts (e.g. switching
	// tabs) so recurring setTimeout chains don't leak past the component.
	onDestroy(() => {
		for (const provider of Object.keys(POLL_TIMERS)) {
			stopPolling(provider);
		}
	});

	function setOAuthStatus(provider: string, status: string) {
		providerOAuthStatus = { ...providerOAuthStatus, [provider]: status };
	}

	function schedulePoll(provider: string, state: string, intervalSeconds: number) {
		stopPolling(provider);
		POLL_TIMERS[provider] = setTimeout(async () => {
			try {
				const status = await pollForvenAuthProviderOAuth(provider, state);
				const flow = providerOAuthState[provider];
				if (!flow) return; // cancelled mid-flight
				switch (status.status) {
					case 'complete':
						stopPolling(provider);
						setOAuthStatus(provider, 'complete');
						providerOAuthState = { ...providerOAuthState, [provider]: null };
						setProviderMessage(provider, 'Connected');
						await reload();
						return;
					case 'expired':
					case 'denied':
					case 'error': {
						const detail = ('error' in status && status.error) || status.status;
						stopPolling(provider);
						setOAuthStatus(provider, status.status);
						setProviderError(provider, `Sign-in ${status.status}: ${detail}`);
						return;
					}
					case 'slow_down':
						setOAuthStatus(provider, 'slow_down');
						schedulePoll(provider, state, status.interval);
						return;
					case 'awaiting_user':
					case 'code_received':
					default:
						setOAuthStatus(provider, status.status);
						schedulePoll(provider, state, intervalSeconds);
				}
			} catch {
				setOAuthStatus(provider, 'retrying');
				schedulePoll(provider, state, Math.min(intervalSeconds * 2, 10));
			}
		}, Math.max(1, intervalSeconds) * 1000);
	}

	async function startOAuth(provider: string) {
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			const res = await startForvenAuthProviderOAuth(provider);
			providerOAuthState = { ...providerOAuthState, [provider]: { ...res, code: '' } };
			if (res.authorize_url) {
				const ok = await openExternal(res.authorize_url);
				if (!ok) {
					setProviderError(
						provider,
						`Could not open browser. Copy this URL manually: ${res.authorize_url}`,
					);
				}
			} else if (res.verification_url) {
				const ok = await openExternal(res.verification_url);
				if (!ok) {
					setProviderError(
						provider,
						`Could not open browser. Copy this URL manually: ${res.verification_url}`,
					);
				}
			}

			const needsManualOnly = res.flow === 'authorization_code' && res.auto_callback === false;
			if (needsManualOnly) {
				setOAuthStatus(provider, 'manual_paste');
			} else {
				setOAuthStatus(provider, 'awaiting_user');
				schedulePoll(provider, res.state, res.interval ?? 2);
			}
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'Failed to start OAuth');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	async function completeOAuth(provider: string) {
		const flow = providerOAuthState[provider];
		if (!flow) return;
		const code = (flow.code ?? '').trim();
		if (!code && flow.flow === 'authorization_code') {
			setProviderError(provider, 'Paste the callback URL or authorization code from the browser.');
			return;
		}
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			await completeForvenAuthProviderOAuth(provider, {
				code: code || undefined,
				state: flow.state,
				code_verifier: flow.code_verifier,
			});
			stopPolling(provider);
			providerOAuthState = { ...providerOAuthState, [provider]: null };
			setProviderMessage(provider, 'Connected');
			await reload();
		} catch (e) {
			setProviderError(provider, e instanceof Error ? e.message : 'OAuth completion failed');
		} finally {
			setProviderBusy(provider, false);
		}
	}

	async function cancelOAuth(provider: string) {
		const flow = providerOAuthState[provider];
		stopPolling(provider);
		setOAuthStatus(provider, '');
		providerOAuthState = { ...providerOAuthState, [provider]: null };
		setProviderError(provider, null);
		if (flow?.state) {
			try {
				await cancelForvenAuthProviderOAuth(provider, flow.state);
			} catch {
				/* best-effort */
			}
		}
	}

	function connectedLabel(p: ForvenAuthProviderStatus): boolean {
		return isProviderConnected(p);
	}
</script>

<section
	aria-labelledby="agents-providers-heading"
	class="terminal-card p-6 space-y-4"
>
	<header class="border-b border-[#1a1a1a] pb-2 flex items-start justify-between gap-3">
		<div>
			<h2 id="agents-providers-heading" class="text-sm font-bold uppercase tracking-widest text-white">
				Providers &amp; Keys
			</h2>
			<p class="text-xs text-[#666] mt-1">
				Connect a provider to authorize spend against it. Only <span class="text-emerald-400">connected</span>
				providers and enabled models are ever selectable for agents or routing.
				{#if authFile}<span class="font-mono">{authFile}</span>{/if}
			</p>
		</div>
		<button
			type="button"
			on:click={reload}
			disabled={authProvidersLoading}
			class="terminal-button text-xs px-2 py-1 disabled:opacity-60"
		>
			{authProvidersLoading ? 'Refreshing…' : 'Refresh'}
		</button>
	</header>

	{#if authProvidersError}
		<p class="text-xs text-red-400" role="alert">{authProvidersError}</p>
	{/if}

	{#if authProvidersLoading && providers.length === 0}
		<p class="text-sm text-[#666]">Loading providers…</p>
	{:else if providers.length === 0}
		<p class="text-sm text-[#666]">No providers registered.</p>
	{:else}
		<ul class="space-y-2">
			{#each providers as provider (provider.provider)}
				{@const key = provider.provider}
				{@const busy = Boolean(providerActionBusy[key])}
				{@const msg = providerActionMessage[key]}
				{@const err = providerActionError[key]}
				{@const oauth = providerOAuthState[key]}
				{@const connected = connectedLabel(provider)}
				{@const isBaseUrlProvider = provider.requires_token === false && !provider.supports_oauth}
				{@const statusColor =
					provider.status === 'active'
						? 'text-emerald-400 border-emerald-900'
						: provider.status === 'not_configured'
							? 'text-[#888] border-[#333]'
							: provider.status === 'needs_reauth'
								? 'text-red-400 border-red-900'
								: 'text-yellow-400 border-yellow-900'}
				<li class="terminal-card p-4 space-y-3 {connected ? 'border-emerald-900' : 'border-[#222]'}">
					<div class="flex flex-wrap items-center justify-between gap-2">
						<div class="flex items-center gap-2 flex-wrap">
							<span class="font-mono text-sm text-white uppercase">{key}</span>
							{#if connected}
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border text-emerald-400 border-emerald-900 bg-emerald-500/10 flex items-center gap-1">
									<svg class="w-3 h-3" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
									Connected
								</span>
							{:else}
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border border-[#333] text-[#888]">
									Not connected
								</span>
							{/if}
							<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border {statusColor}">
								{provider.status === 'needs_reauth' ? 're-authenticate' : provider.status}
							</span>
							{#if provider.supports_oauth}
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border border-[#333] text-[#888]">
									oauth
								</span>
							{/if}
							{#if provider.configured && !connected}
								<span class="text-[10px] text-yellow-400" title="A credential exists (e.g. env var) but the operator has not connected this provider.">env key only</span>
							{/if}
						</div>
						{#if provider.expires_in}
							<span class="text-xs text-[#888]">{provider.expires_in}</span>
						{/if}
					</div>

					{#if provider.expires_at}
						<p class="text-xs text-[#666]">Expires {provider.expires_at}</p>
					{/if}
					{#if provider.base_url}
						<p class="text-xs text-[#888]">
							Base URL: <span class="font-mono">{provider.base_url}</span>
						</p>
					{/if}
					{#if provider.last_refresh_error}
						<div class="border border-red-900 bg-red-500/5 px-2 py-1.5 text-xs text-red-400">
							<span class="font-semibold">Token refresh failed:</span>
							{provider.last_refresh_error}
							{#if provider.supports_oauth}
								<span class="text-red-400/80">— sign in again to recover.</span>
							{/if}
						</div>
					{/if}

					{#if msg}
						<p class="text-xs text-emerald-400" role="status">{msg}</p>
					{/if}
					{#if err}
						<p class="text-xs text-red-400" role="alert">{err}</p>
					{/if}

					{#if oauth}
						{@const pollStatus = providerOAuthStatus[key] ?? ''}
						{@const isAuthorizationCode = oauth.flow === 'authorization_code'}
						{@const isManualPaste = isAuthorizationCode && oauth.auto_callback === false}
						{@const pillLabel =
							pollStatus === 'awaiting_user'
								? 'Waiting for sign-in…'
								: pollStatus === 'code_received'
									? 'Exchanging code…'
									: pollStatus === 'slow_down'
										? 'Slow down — backing off…'
										: pollStatus === 'retrying'
											? 'Network blip — retrying…'
											: pollStatus === 'complete'
												? 'Connected'
												: pollStatus === 'expired'
													? 'Sign-in expired'
													: pollStatus === 'denied'
														? 'Sign-in denied'
														: pollStatus === 'error'
															? 'Sign-in failed'
															: pollStatus === 'manual_paste'
																? 'Paste code below'
																: 'Starting…'}
						{@const pillColor =
							pollStatus === 'complete'
								? 'text-emerald-400 border-emerald-900'
								: pollStatus === 'expired' || pollStatus === 'denied' || pollStatus === 'error'
									? 'text-red-400 border-red-900'
									: pollStatus === 'slow_down' || pollStatus === 'retrying'
										? 'text-yellow-400 border-yellow-900'
										: 'text-[#888] border-[#333]'}
						<div class="border border-[#333] bg-[#050505] p-3 space-y-2">
							<p class="text-xs text-[#888]">
								{oauth.flow === 'device_code' ? 'Device code flow' : 'Authorization code flow'}
							</p>
							{#if oauth.verification_url && oauth.user_code}
								<p class="text-xs text-[#888]">
									Go to <a
										href={oauth.verification_url}
										on:click|preventDefault={() => openExternal(oauth.verification_url!)}
										class="text-white underline cursor-pointer">{oauth.verification_url}</a>
									and enter code <span class="font-mono text-white">{oauth.user_code}</span>
								</p>
							{:else if oauth.authorize_url}
								<p class="text-xs text-[#888]">
									A new tab opened to <a
										href={oauth.authorize_url}
										on:click|preventDefault={() => openExternal(oauth.authorize_url!)}
										class="text-white underline cursor-pointer">authorize</a>.
									{#if isManualPaste}
										Paste the code returned by the provider:
									{:else if isAuthorizationCode}
										If it does not finish automatically, paste the callback URL from the browser:
									{:else}
										You'll be returned here automatically.
									{/if}
								</p>
							{/if}
							{#if isAuthorizationCode}
								<input
									type="text"
									placeholder="Paste callback URL or authorization code"
									bind:value={oauth.code}
									class="terminal-input w-full text-sm font-mono"
								/>
							{/if}
							{#if oauth.bind_error}
								<p class="text-[11px] text-yellow-400">
									Couldn't bind loopback listener ({oauth.bind_error}); using manual paste.
								</p>
							{/if}
							<div class="flex items-center gap-2">
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 border {pillColor}">
									{pillLabel}
								</span>
								{#if isAuthorizationCode}
									<button
										type="button"
										on:click={() => completeOAuth(key)}
										disabled={busy || !(oauth.code ?? '').trim()}
										class="terminal-button-primary text-xs px-3 py-1 disabled:opacity-60"
									>
										{busy ? 'Completing…' : 'Use pasted code'}
									</button>
								{/if}
								<button
									type="button"
									on:click={() => cancelOAuth(key)}
									class="terminal-button text-xs px-3 py-1"
								>
									Cancel
								</button>
							</div>
						</div>
					{:else}
						{#if connected}
							<p class="text-xs text-emerald-400">
								Connected{provider.expires_in ? ` · renews in ${provider.expires_in}` : ''}. This provider is authorized to spend.
							</p>
						{:else}
							<div class="flex flex-wrap items-end gap-2">
								{#if isBaseUrlProvider}
									<label class="flex-1 min-w-[14rem]">
										<span class="block text-[10px] uppercase tracking-wider text-[#666] mb-1">Base URL</span>
										<input
											type="text"
											placeholder={provider.base_url ?? 'http://localhost:1234/v1'}
											bind:value={providerBaseUrlInput[key]}
											class="terminal-input w-full text-sm font-mono"
										/>
									</label>
									<button
										type="button"
										on:click={() => saveProviderBaseUrl(key)}
										disabled={busy}
										class="terminal-button-primary text-xs px-3 py-1.5 disabled:opacity-60"
									>
										{busy ? 'Saving…' : 'Connect'}
									</button>
								{:else}
									<label class="flex-1 min-w-[14rem]">
										<span class="block text-[10px] uppercase tracking-wider text-[#666] mb-1">API key / access token</span>
										<input
											type="password"
											placeholder="Paste token and press Connect"
											bind:value={providerTokenInput[key]}
											class="terminal-input w-full text-sm font-mono"
										/>
									</label>
									<button
										type="button"
										on:click={() => saveProviderToken(key)}
										disabled={busy}
										class="terminal-button-primary text-xs px-3 py-1.5 disabled:opacity-60"
									>
										{busy ? 'Saving…' : 'Connect'}
									</button>
									{#if provider.supports_oauth}
										<button
											type="button"
											on:click={() => startOAuth(key)}
											disabled={busy}
											class="terminal-button text-xs px-3 py-1.5 disabled:opacity-60"
										>
											{provider.configured ? 'Re-authenticate' : 'Sign in with OAuth'}
										</button>
									{/if}
								{/if}
							</div>
							{#if key === 'opencode-go'}
								<OpenCodeGoReferralNote />
							{:else if PROVIDER_SIGNUP_URLS[key]}
								<p class="text-xs text-[#666]">
									Need access? <a
										href={PROVIDER_SIGNUP_URLS[key]}
										on:click|preventDefault={() => openExternal(PROVIDER_SIGNUP_URLS[key]!)}
										class="text-white underline cursor-pointer">Get a key ↗</a>
								</p>
							{/if}
						{/if}

						{#if provider.configured}
							<div class="flex gap-2 flex-wrap">
								<button
									type="button"
									on:click={() => testProvider(key)}
									disabled={busy}
									class="terminal-button text-xs px-3 py-1 disabled:opacity-60"
								>
									{busy ? 'Testing…' : 'Test connection'}
								</button>
								{#if connected && provider.supports_oauth}
									<button
										type="button"
										on:click={() => startOAuth(key)}
										disabled={busy}
										class="terminal-button text-xs px-3 py-1 disabled:opacity-60"
									>
										Re-authenticate
									</button>
								{/if}
								<button
									type="button"
									on:click={() => disconnectProvider(key)}
									disabled={busy}
									class="terminal-button-danger text-xs px-3 py-1 disabled:opacity-60"
								>
									Disconnect
								</button>
							</div>
						{/if}
					{/if}

					<details class="text-xs text-[#666]">
						<summary class="cursor-pointer hover:text-white">CLI equivalent</summary>
						<div class="mt-1 space-y-1">
							{#if provider.login_command}
								<p><span class="text-[#555]">Login:</span> <span class="font-mono text-[#888]">{provider.login_command}</span></p>
							{/if}
							{#if provider.refresh_command && provider.configured}
								<p><span class="text-[#555]">Refresh:</span> <span class="font-mono text-[#888]">{provider.refresh_command}</span></p>
							{/if}
						</div>
					</details>
				</li>
			{/each}
		</ul>
	{/if}
</section>
