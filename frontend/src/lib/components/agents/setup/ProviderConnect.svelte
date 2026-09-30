<script lang="ts">
	/**
	 * Connect (or re-authenticate) one provider: paste a key, set a local base
	 * URL, or sign in with OAuth (device code or authorization code, with the
	 * loopback callback or a pasted code). The flow is the one the Providers tab
	 * shipped with, scoped to a single provider.
	 */
	import { createEventDispatcher, onDestroy } from 'svelte';
	import {
		cancelForvenAuthProviderOAuth,
		completeForvenAuthProviderOAuth,
		pollForvenAuthProviderOAuth,
		setForvenAuthProvider,
		startForvenAuthProviderOAuth,
		type ForvenAuthProviderOAuthStartResponse,
		type ForvenAuthProviderStatus,
	} from '$lib/api';
	import { openExternal } from '$lib/external-open';
	import { copyTextToClipboard } from '$lib/utils/clipboard';
	import { providerLabel } from '$lib/utils/agentsHub/agents';

	export let provider: ForvenAuthProviderStatus;
	/** Start straight into OAuth (a re-authenticate click). */
	export let startWithOAuth = false;

	const dispatch = createEventDispatcher<{ connected: void; cancel: void }>();

	/** Where to get a key, when the provider needs one. */
	const SIGNUP: Record<string, string> = {
		'opencode-zen': 'https://opencode.ai/auth',
	};

	type Flow = ForvenAuthProviderOAuthStartResponse & { code: string; openFailed?: boolean };

	$: key = String(provider.provider);
	$: name = providerLabel(key);
	$: usesBaseUrl = provider.requires_token === false && !provider.supports_oauth;

	let token = '';
	let baseUrl = '';
	let busy = false;
	let error: string | null = null;
	let flow: Flow | null = null;
	let flowStatus = '';
	let copied = false;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	onDestroy(() => stopPolling());

	function stopPolling() {
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = null;
	}

	async function saveToken() {
		if (!token.trim()) {
			error = 'Paste an API key or access token first.';
			return;
		}
		busy = true;
		error = null;
		try {
			await setForvenAuthProvider(key, { api_key: token.trim() });
			token = '';
			dispatch('connected');
		} catch (err) {
			error = err instanceof Error ? err.message : 'The key was not accepted.';
		} finally {
			busy = false;
		}
	}

	async function saveBaseUrl() {
		if (!baseUrl.trim()) {
			error = 'Enter the server’s base URL first.';
			return;
		}
		busy = true;
		error = null;
		try {
			await setForvenAuthProvider(key, { base_url: baseUrl.trim() });
			dispatch('connected');
		} catch (err) {
			error = err instanceof Error ? err.message : 'The base URL was not accepted.';
		} finally {
			busy = false;
		}
	}

	function schedulePoll(state: string, seconds: number) {
		stopPolling();
		pollTimer = setTimeout(async () => {
			try {
				const status = await pollForvenAuthProviderOAuth(key, state);
				if (!flow) return; // cancelled while the request was out
				switch (status.status) {
					case 'complete':
						stopPolling();
						flow = null;
						flowStatus = '';
						dispatch('connected');
						return;
					case 'expired':
					case 'denied':
					case 'error': {
						stopPolling();
						flowStatus = status.status;
						error = `Sign-in ${status.status}: ${('error' in status && status.error) || status.status}`;
						return;
					}
					case 'slow_down':
						flowStatus = 'slow_down';
						schedulePoll(state, status.interval);
						return;
					default:
						flowStatus = status.status;
						schedulePoll(state, seconds);
				}
			} catch {
				flowStatus = 'retrying';
				schedulePoll(state, Math.min(seconds * 2, 10));
			}
		}, Math.max(1, seconds) * 1000);
	}

	async function startOAuth() {
		busy = true;
		error = null;
		try {
			const res = await startForvenAuthProviderOAuth(key);
			flow = { ...res, code: '' };
			if (res.flow === 'authorization_code' && res.auto_callback === false) {
				flowStatus = 'manual_paste';
			} else {
				flowStatus = 'awaiting_user';
				schedulePoll(res.state, res.interval ?? 2);
			}
			const target = res.authorize_url ?? res.verification_url;
			if (target) {
				const opened = await openExternal(target);
				if (!opened && flow) flow = { ...flow, openFailed: true };
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not start the sign-in.';
		} finally {
			busy = false;
		}
	}

	async function completeOAuth() {
		if (!flow) return;
		const code = flow.code.trim();
		if (!code && flow.flow === 'authorization_code') {
			error = 'Paste the callback URL or the code from the browser.';
			return;
		}
		busy = true;
		error = null;
		try {
			await completeForvenAuthProviderOAuth(key, { code: code || undefined, state: flow.state, code_verifier: flow.code_verifier });
			stopPolling();
			flow = null;
			dispatch('connected');
		} catch (err) {
			error = err instanceof Error ? err.message : 'The sign-in could not be completed.';
		} finally {
			busy = false;
		}
	}

	async function cancelOAuth() {
		const current = flow;
		stopPolling();
		flow = null;
		flowStatus = '';
		error = null;
		if (current?.state) {
			try {
				await cancelForvenAuthProviderOAuth(key, current.state);
			} catch {
				// Best effort: the server expires abandoned sign-ins on its own.
			}
		}
	}

	async function copyLink() {
		const url = flow?.authorize_url ?? flow?.verification_url;
		if (!url) return;
		copied = await copyTextToClipboard(url);
		if (copied) setTimeout(() => (copied = false), 2000);
		else error = `Copy failed. Select and copy the link: ${url}`;
	}

	const STATUS_TEXT: Record<string, string> = {
		awaiting_user: 'Waiting for you to finish signing in…',
		code_received: 'Exchanging the code…',
		slow_down: 'The provider asked us to slow down…',
		retrying: 'Network blip, retrying…',
		manual_paste: 'Paste the code below',
		expired: 'Sign-in expired',
		denied: 'Sign-in denied',
		error: 'Sign-in failed',
	};

	let started = false;
	$: if (startWithOAuth && !started && provider.supports_oauth) {
		started = true;
		void startOAuth();
	}
</script>

<div class="grid gap-2.5" data-testid="provider-connect">
	{#if flow}
		<div class="grid gap-2 rounded-md border border-sc-line2 bg-sc-bg px-3 py-2.5 text-[12px]">
			{#if flow.openFailed}
				<p class="m-0 text-[#e7b24a]">Your browser did not open. Use the link below.</p>
			{/if}
			{#if flow.verification_url && flow.user_code}
				<p class="m-0 text-sc-ink2">
					Go to <a href={flow.verification_url} target="_blank" rel="noopener noreferrer" class="text-sc-ink underline" on:click|preventDefault={() => flow?.verification_url && openExternal(flow.verification_url)}>{flow.verification_url}</a>
					and enter <span class="font-plex-mono text-sc-ink">{flow.user_code}</span>.
				</p>
			{:else if flow.authorize_url}
				<p class="m-0 text-sc-ink2">
					Finish signing in to {name} in your browser
					(<a href={flow.authorize_url} target="_blank" rel="noopener noreferrer" class="text-sc-ink underline" on:click|preventDefault={() => flow?.authorize_url && openExternal(flow.authorize_url)}>open it again</a>).
					{#if flow.flow === 'authorization_code'}
						{flow.auto_callback === false ? 'Then paste the code it shows:' : 'If it does not come back on its own, paste the callback URL:'}
					{/if}
				</p>
			{/if}
			{#if flow.flow === 'authorization_code'}
				<input class="w-full rounded-md border border-sc-line2 bg-sc-panel px-2.5 py-1.5 font-plex-mono text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder="Callback URL or code" bind:value={flow.code} aria-label="Callback URL or code" />
			{/if}
			{#if flow.bind_error}
				<p class="m-0 text-[11px] text-[#e7b24a]">The local callback listener could not start ({flow.bind_error}); paste the code instead.</p>
			{/if}
			<div class="flex flex-wrap items-center gap-2">
				<span class="text-[11.5px] text-sc-ink3">{STATUS_TEXT[flowStatus] ?? 'Starting…'}</span>
				<span class="flex-1"></span>
				{#if flow.flow === 'authorization_code'}
					<button type="button" class="rounded-md bg-sc-ink px-3 py-1 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={busy || !flow.code.trim()} on:click={completeOAuth}>{busy ? 'Finishing…' : 'Use this code'}</button>
				{/if}
				{#if flow.authorize_url || flow.verification_url}
					<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" on:click={copyLink}>{copied ? 'Copied' : 'Copy link'}</button>
				{/if}
				<button type="button" class="rounded-md border border-sc-line2 px-3 py-1 text-[12px] text-sc-ink2 hover:text-sc-ink" on:click={cancelOAuth}>Cancel</button>
			</div>
		</div>
	{:else if usesBaseUrl}
		<form class="flex flex-wrap items-end gap-2" on:submit|preventDefault={saveBaseUrl}>
			<label class="grid min-w-[16rem] flex-1 gap-1 text-[11.5px] text-sc-ink3">
				Server base URL
				<input class="w-full rounded-md border border-sc-line2 bg-sc-bg px-2.5 py-1.5 font-plex-mono text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder={provider.base_url ?? 'http://localhost:1234/v1'} bind:value={baseUrl} />
			</label>
			<button type="submit" class="rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={busy}>{busy ? 'Saving…' : 'Connect'}</button>
			<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:text-sc-ink" on:click={() => dispatch('cancel')}>Cancel</button>
		</form>
	{:else}
		<form class="flex flex-wrap items-end gap-2" on:submit|preventDefault={saveToken}>
			<label class="grid min-w-[16rem] flex-1 gap-1 text-[11.5px] text-sc-ink3">
				API key
				<input type="password" autocomplete="off" class="w-full rounded-md border border-sc-line2 bg-sc-bg px-2.5 py-1.5 font-plex-mono text-[12px] text-sc-ink outline-none placeholder:text-sc-ink4 focus:border-sc-ink4" placeholder="Paste the key" bind:value={token} />
			</label>
			<button type="submit" class="rounded-md bg-sc-ink px-3 py-1.5 text-[12px] font-medium text-black hover:bg-white disabled:opacity-40" disabled={busy}>{busy ? 'Checking…' : 'Connect'}</button>
			{#if provider.supports_oauth}
				<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-40" disabled={busy} on:click={startOAuth}>Sign in instead</button>
			{/if}
			<button type="button" class="rounded-md border border-sc-line2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:text-sc-ink" on:click={() => dispatch('cancel')}>Cancel</button>
		</form>
		<p class="m-0 text-[11px] text-sc-ink3">
			The key is checked with {name} before it is saved.
			{#if SIGNUP[key]}<a class="text-sc-ink2 underline" href={SIGNUP[key]} on:click|preventDefault={() => openExternal(SIGNUP[key])}>Get a key ↗</a>{/if}
			{#if provider.login_command}<span> Or from a terminal: <span class="font-plex-mono text-sc-ink2">{provider.login_command}</span></span>{/if}
		</p>
	{/if}
	{#if error}
		<p class="m-0 text-[12px] text-[#f2956f]" role="alert">{error}</p>
	{/if}
</div>
