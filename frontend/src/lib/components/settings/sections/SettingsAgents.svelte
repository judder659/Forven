<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getForvenAuthProviders,
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
	import { openExternal } from '$lib/external-open';
	import { copyTextToClipboard } from '$lib/utils/clipboard';

	let authProviders: ForvenAuthProviderStatus[] = [];
	let authProvidersLoading = true;
	let authProvidersError: string | null = null;
	let authFile: string | null = null;
	let providerActionBusy: Record<string, boolean> = {};
	let providerActionMessage: Record<string, string | null> = {};
	let providerActionError: Record<string, string | null> = {};
	let providerTokenInput: Record<string, string> = {};
	let providerBaseUrlInput: Record<string, string> = {};
	let providerOAuthState: Record<
		string,
		(ForvenAuthProviderOAuthStartResponse & { code: string; openFailed?: boolean }) | null
	> = {};
	let providerOAuthStatus: Record<string, string> = {};
	let providerLinkCopied: Record<string, boolean> = {};

	async function copySignInLink(provider: string) {
		const flow = providerOAuthState[provider];
		const url = flow?.authorize_url ?? flow?.verification_url;
		if (!url) return;
		const ok = await copyTextToClipboard(url);
		providerLinkCopied = { ...providerLinkCopied, [provider]: ok };
		if (ok) {
			setTimeout(() => {
				providerLinkCopied = { ...providerLinkCopied, [provider]: false };
			}, 2000);
		} else {
			setProviderError(provider, `Copy failed — select and copy the link manually: ${url}`);
		}
	}

	async function loadAuthProviders() {
		authProvidersLoading = true;
		authProvidersError = null;
		try {
			const res = await getForvenAuthProviders();
			authProviders = res.providers ?? [];
			authFile = res.auth_file ?? null;
		} catch (e) {
			authProvidersError = e instanceof Error ? e.message : 'Failed to load providers';
		} finally {
			authProvidersLoading = false;
		}
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
			setProviderMessage(provider, 'Saved');
			await loadAuthProviders();
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
			await loadAuthProviders();
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
		if (!window.confirm(`Disconnect ${provider}? This clears stored credentials.`)) return;
		setProviderBusy(provider, true);
		setProviderError(provider, null);
		try {
			await deleteForvenAuthProvider(provider);
			setProviderMessage(provider, 'Disconnected');
			await loadAuthProviders();
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
						setProviderError(provider, null);
						setProviderMessage(provider, 'Connected');
						await loadAuthProviders();
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
				// Network blip — back off but keep trying.
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

			const needsManualOnly = res.flow === 'authorization_code' && res.auto_callback === false;
			if (needsManualOnly) {
				setOAuthStatus(provider, 'manual_paste');
			} else {
				setOAuthStatus(provider, 'awaiting_user');
				schedulePoll(provider, res.state, res.interval ?? 2);
			}

			// Hand the authorize URL to the OS browser. In the packaged Tauri
			// shell, window.open(_blank) is a silent no-op — openExternal()
			// invokes the tauri-plugin-opener command; in a plain browser it
			// falls back to window.open. A failure here is recoverable from
			// inside the flow panel (open link + copy button), so flag it on
			// the flow state instead of aborting with an error.
			const target = res.authorize_url ?? res.verification_url;
			if (target) {
				const opened = await openExternal(target);
				const flow = providerOAuthState[provider];
				if (!opened && flow) {
					providerOAuthState = {
						...providerOAuthState,
						[provider]: { ...flow, openFailed: true },
					};
				}
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
			setProviderMessage(provider, 'Signed in');
			await loadAuthProviders();
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

	onMount(() => {
		void loadAuthProviders();
	});
</script>

<div class="space-y-6">
	<!-- Roster-driven: AI providers (read-only status, CLI-managed credentials) -->
	<section
		aria-labelledby="agents-providers-heading"
		class="terminal-card p-6 space-y-4"
	>
		<header class="border-b border-sc-line pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 id="agents-providers-heading" class="text-[14px] font-semibold text-sc-ink2">
					AI providers
				</h2>
				<p class="text-xs text-sc-ink3 mt-1">
					Provider credentials are stored in the auth file and managed via CLI.
					{#if authFile}<span class="font-mono">{authFile}</span>{/if}
				</p>
			</div>
			<button
				type="button"
				on:click={() => loadAuthProviders()}
				disabled={authProvidersLoading}
				class="terminal-button text-[12px]"
			>
				{authProvidersLoading ? 'Refreshing…' : 'Refresh'}
			</button>
		</header>

		{#if authProvidersError}
			<p class="text-xs text-red-400" role="alert">{authProvidersError}</p>
		{/if}

		{#if authProvidersLoading}
			<p class="text-sm text-sc-ink2">Loading providers…</p>
		{:else if authProviders.length === 0}
			<p class="text-sm text-sc-ink2">No providers registered.</p>
		{:else}
			<ul class="space-y-2">
				{#each authProviders as provider (provider.provider)}
					{@const key = provider.provider}
					{@const busy = Boolean(providerActionBusy[key])}
					{@const msg = providerActionMessage[key]}
					{@const err = providerActionError[key]}
					{@const oauth = providerOAuthState[key]}
					{@const isBaseUrlProvider = provider.requires_token === false && !provider.supports_oauth}
					{@const statusColor =
						provider.status === 'active'
							? 'text-emerald-400 border-emerald-900 bg-emerald-500/10'
							: provider.status === 'not_configured'
								? 'text-sc-ink2 border-sc-line2 bg-transparent'
								: provider.status === 'needs_reauth'
									? 'text-red-400 border-red-900 bg-red-500/10'
									: 'text-yellow-400 border-yellow-900 bg-yellow-500/10'}
					<li class="rounded-md border border-sc-line bg-sc-bg p-4 space-y-3">
						<div class="flex flex-wrap items-center justify-between gap-2">
							<div class="flex items-center gap-2">
								<span class="font-mono text-sm text-sc-ink uppercase">{key}</span>
								<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 border {statusColor}">
									{provider.status === 'needs_reauth' ? 're-authenticate' : provider.status}
								</span>
								{#if provider.supports_oauth}
									<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 border border-sc-line2 text-sc-ink2">
										oauth
									</span>
								{/if}
							</div>
							{#if provider.expires_in}
								<span class="text-xs text-sc-ink2">{provider.expires_in}</span>
							{/if}
						</div>

						{#if provider.expires_at}
							<p class="text-xs text-sc-ink3">Expires {provider.expires_at}</p>
						{/if}
						{#if provider.base_url}
							<p class="text-xs text-sc-ink2">
								Base URL: <span class="font-mono">{provider.base_url}</span>
							</p>
						{/if}
						{#if provider.last_refresh_error}
							<div class="border border-red-900 bg-red-500/5 px-2 py-1.5 text-xs text-red-400">
								<span class="font-semibold">Token refresh failed:</span>
								{provider.last_refresh_error}
								{#if provider.supports_oauth}
									<span class="text-red-200/80">— sign in again to recover.</span>
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
									? 'text-emerald-400 border-emerald-900 bg-emerald-500/10'
									: pollStatus === 'expired' ||
										  pollStatus === 'denied' ||
										  pollStatus === 'error'
										? 'text-red-400 border-red-900 bg-red-500/10'
										: pollStatus === 'slow_down' || pollStatus === 'retrying'
											? 'text-yellow-400 border-yellow-900 bg-yellow-500/10'
											: 'text-sc-ink2 border-sc-line2 bg-sc-panel2'}
							<div class="rounded-md bg-sc-panel border border-sc-line2 p-3 space-y-2">
								<p class="text-xs text-sc-ink2">
									{oauth.flow === 'device_code' ? 'Device code flow' : 'Authorization code flow'}
								</p>
								{#if oauth.openFailed}
									<p class="text-xs text-yellow-400">
										Your browser didn't open automatically — use the sign-in link below.
									</p>
								{/if}
								{#if oauth.verification_url && oauth.user_code}
									<p class="text-xs text-sc-ink2">
										Go to <a
											href={oauth.verification_url}
											target="_blank"
											rel="noopener noreferrer"
											on:click|preventDefault={() => openExternal(oauth.verification_url!)}
											class="text-sc-ink underline cursor-pointer">{oauth.verification_url}</a>
										and enter code <span class="font-mono text-sc-ink">{oauth.user_code}</span>
									</p>
								{:else if oauth.authorize_url}
									<p class="text-xs text-sc-ink2">
										{#if oauth.openFailed}
											<a
												href={oauth.authorize_url}
												target="_blank"
												rel="noopener noreferrer"
												class="text-sc-ink underline cursor-pointer">Open the sign-in page</a>
											or copy the link, then finish signing in there.
										{:else}
											A new tab opened to <a
												href={oauth.authorize_url}
												target="_blank"
												rel="noopener noreferrer"
												on:click|preventDefault={() => openExternal(oauth.authorize_url!)}
												class="text-sc-ink underline cursor-pointer">authorize</a>.
										{/if}
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
										class="terminal-input w-full font-mono"
									/>
								{/if}
								{#if oauth.bind_error}
									<p class="text-[11px] text-yellow-400">
										Couldn't bind loopback listener ({oauth.bind_error}); using manual paste.
									</p>
								{/if}
								<div class="flex items-center gap-2">
									<span
										class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-2 py-0.5 border {pillColor}"
									>
										{pillLabel}
									</span>
									{#if isAuthorizationCode}
										<button
											type="button"
											on:click={() => completeOAuth(key)}
											disabled={busy || !(oauth.code ?? '').trim()}
											class="terminal-button-primary text-[12px]"
										>
											{busy ? 'Completing…' : 'Use pasted code'}
										</button>
									{/if}
									{#if oauth.authorize_url || oauth.verification_url}
										<button
											type="button"
											on:click={() => copySignInLink(key)}
											class="terminal-button text-[12px]"
										>
											{providerLinkCopied[key] ? 'Copied ✓' : 'Copy sign-in link'}
										</button>
									{/if}
									<button
										type="button"
										on:click={() => cancelOAuth(key)}
										class="terminal-button text-[12px]"
									>
										Cancel
									</button>
								</div>
							</div>
						{:else}
							{@const isActive = provider.configured && provider.status === 'active'}
							{#if isActive}
								<p class="text-xs text-emerald-400">
									Connected{provider.expires_in ? ` · renews in ${provider.expires_in}` : ''}. No action needed.

								</p>
							{:else}
								<div class="flex flex-wrap items-end gap-2">
									{#if isBaseUrlProvider}
										<label class="flex-1 min-w-[14rem]">
											<span class="block text-xs text-sc-ink2 mb-1">Base URL</span>
											<input
												type="text"
												placeholder={provider.base_url ?? 'http://localhost:1234/v1'}
												bind:value={providerBaseUrlInput[key]}
												class="terminal-input w-full font-mono"
											/>
										</label>
										<button
											type="button"
											on:click={() => saveProviderBaseUrl(key)}
											disabled={busy}
											class="terminal-button-primary text-[12px]"
										>
											{busy ? 'Saving…' : 'Save'}
										</button>
									{:else}
										<label class="flex-1 min-w-[14rem]">
											<span class="block text-xs text-sc-ink2 mb-1">API key / access token</span>
											<input
												type="password"
												placeholder="Paste token and press Save"
												bind:value={providerTokenInput[key]}
												class="terminal-input w-full font-mono"
											/>
										</label>
										<button
											type="button"
											on:click={() => saveProviderToken(key)}
											disabled={busy}
											class="terminal-button-primary text-[12px]"
										>
											{busy ? 'Saving…' : 'Save'}
										</button>
										{#if provider.supports_oauth}
											<button
												type="button"
												on:click={() => startOAuth(key)}
												disabled={busy}
												class="terminal-button text-[12px]"
											>
												{provider.configured ? 'Re-authenticate' : 'Sign in with OAuth'}
											</button>
										{/if}
									{/if}
								</div>
							{/if}

							{#if provider.configured}
								<div class="flex gap-2">
									<button
										type="button"
										on:click={() => testProvider(key)}
										disabled={busy}
										class="terminal-button text-[12px]"
									>
										{busy ? 'Testing…' : 'Test connection'}
									</button>
									{#if isActive && provider.supports_oauth}
										<button
											type="button"
											on:click={() => startOAuth(key)}
											disabled={busy}
											class="terminal-button text-[12px]"
										>
											Re-authenticate
										</button>
									{/if}
									<button
										type="button"
										on:click={() => disconnectProvider(key)}
										disabled={busy}
										class="terminal-button-danger text-[12px]"
									>
										Disconnect
									</button>
								</div>
							{/if}
						{/if}

						<details class="text-xs text-sc-ink3">
							<summary class="cursor-pointer hover:text-sc-ink">CLI equivalent</summary>
							<div class="mt-1 space-y-1">
								{#if provider.login_command}
									<p><span class="text-sc-ink3">Login:</span> <span class="font-mono text-sc-ink2">{provider.login_command}</span></p>
								{/if}
								{#if provider.refresh_command && provider.configured}
									<p><span class="text-sc-ink3">Refresh:</span> <span class="font-mono text-sc-ink2">{provider.refresh_command}</span></p>
								{/if}
							</div>
						</details>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
