<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getForvenAgents,
		getForvenAgentDocuments,
		updateForvenAgent,
		updateForvenAgentDocument,
		testForvenAgentDiscord,
		getForvenSchedulerJobs,
		updateForvenSchedulerJob,
		getForvenAgentModelOptions,
		updateSettingsSection,
		getForvenAuthProviders,
		setForvenAuthProvider,
		deleteForvenAuthProvider,
		testForvenAuthProvider,
		startForvenAuthProviderOAuth,
		completeForvenAuthProviderOAuth,
		pollForvenAuthProviderOAuth,
		cancelForvenAuthProviderOAuth,
		type ForvenAgent,
		type ForvenSchedulerJob,
		type ForvenAgentModelOption,
		type ForvenAuthProviderStatus,
		type ForvenAuthProviderOAuthStartResponse,
	} from '$lib/api';
	import { openExternal } from '$lib/external-open';
	import { msToMinutes, minutesToMs, formatIntervalMs } from '$lib/utils/schedule';

	export let settings: Record<string, unknown> = {};
	export let variant: 'default' | 'wizard' = 'default';

	let agents: ForvenAgent[] = [];
	let selectedAgentId: string | null = null;
	let agentDraft: {
		name: string;
		role: string;
		model: string;
		model_id: string;
		schedule_type: string;
		schedule_expr: string;
		enabled: boolean;
		instructions: string;
		has_discord_token: boolean;
		discord_token: string;
	} | null = null;
	type AgentDocKind = 'soul' | 'agents' | 'role';

	const agentDocKinds: AgentDocKind[] = ['soul', 'agents', 'role'];

	let agentDocs: Record<AgentDocKind, string> = { soul: '', agents: '', role: '' };
	let agentDocsLoading = false;
	let agentDocSaving: Record<AgentDocKind, boolean> = {
		soul: false,
		agents: false,
		role: false,
	};
	let agentSaving = false;
	let agentDiscordTesting = false;
	let agentMessage: string | null = null;
	let agentError: string | null = null;
	let agentsLoading = true;

	let schedulerJobs: ForvenSchedulerJob[] = [];
	let schedulerLoading = true;
	let schedulerJobSaving: Record<string, boolean> = {};
	let schedulerMessage: string | null = null;
	let schedulerError: string | null = null;

	let modelOptions: ForvenAgentModelOption[] = [];
	let modelOptionsLoading = true;
	let modelOptionsSaving = false;
	let modelOptionsError: string | null = null;
	let modelOptionsMessage: string | null = null;
	let enabledModelKeys: Set<string> = new Set();
	let modelOptionsRefreshing = false;

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
		(ForvenAuthProviderOAuthStartResponse & { code: string }) | null
	> = {};
	let providerOAuthStatus: Record<string, string> = {};

	function draftFromAgent(a: ForvenAgent) {
		return {
			name: (a.name ?? '') as string,
			role: (a.role ?? '') as string,
			model: (a.model ?? '') as string,
			model_id: (a.model_id ?? '') as string,
			schedule_type: (a.schedule_type ?? '') as string,
			schedule_expr: (a.schedule_expr ?? '') as string,
			enabled: Boolean(a.enabled ?? true),
			instructions: (a.instructions ?? '') as string,
			has_discord_token: Boolean(a.has_discord_token),
			discord_token: '',
		};
	}

	async function loadAgentRoster(preserveSelection = true) {
		agentsLoading = true;
		try {
			agents = await getForvenAgents();
			if (preserveSelection && selectedAgentId) {
				const match = agents.find((a) => a.id === selectedAgentId);
				if (match) {
					agentDraft = draftFromAgent(match);
					return;
				}
			}
			if (agents.length > 0) {
				await selectAgent(agents[0].id ?? null);
			}
		} catch (e) {
			agentError = e instanceof Error ? e.message : 'Failed to load agents';
		} finally {
			agentsLoading = false;
		}
	}

	async function selectAgent(id: string | null) {
		if (!id) return;
		const next = agents.find((a) => a.id === id);
		if (!next) return;
		selectedAgentId = id;
		agentDraft = draftFromAgent(next);
		agentDocsLoading = true;
		try {
			agentDocs = await getForvenAgentDocuments(id);
		} catch (e) {
			agentError = e instanceof Error ? e.message : 'Failed to load agent documents';
			agentDocs = { soul: '', agents: '', role: '' };
		} finally {
			agentDocsLoading = false;
		}
	}

	async function saveAgent() {
		if (!selectedAgentId || !agentDraft) return;
		agentSaving = true;
		agentError = null;
		try {
			const payload: Record<string, unknown> = {
				name: agentDraft.name.trim(),
				role: agentDraft.role.trim(),
				model: agentDraft.model.trim(),
				model_id: agentDraft.model_id.trim() || null,
				schedule_type: agentDraft.schedule_type.trim() || undefined,
				schedule_expr: agentDraft.schedule_expr.trim() || undefined,
				enabled: agentDraft.enabled,
				instructions: agentDraft.instructions.trimEnd(),
			};
			if (agentDraft.discord_token) payload.discord_token = agentDraft.discord_token;
			await updateForvenAgent(selectedAgentId, payload);
			agentMessage = 'Agent updated';
			await loadAgentRoster(true);
			setTimeout(() => (agentMessage = null), 3000);
		} catch (e) {
			agentError = e instanceof Error ? e.message : 'Failed to update agent';
		} finally {
			agentSaving = false;
		}
	}

	async function saveAgentDoc(doc: 'soul' | 'agents' | 'role') {
		if (!selectedAgentId) return;
		agentDocSaving = { ...agentDocSaving, [doc]: true };
		agentError = null;
		try {
			await updateForvenAgentDocument(selectedAgentId, doc, agentDocs[doc]);
			agentMessage = `${doc.toUpperCase()} saved`;
			setTimeout(() => (agentMessage = null), 3000);
		} catch (e) {
			agentError = e instanceof Error ? e.message : `Failed to save ${doc}`;
		} finally {
			agentDocSaving = { ...agentDocSaving, [doc]: false };
		}
	}

	async function testAgentDiscord() {
		if (!selectedAgentId || !agentDraft) return;
		agentDiscordTesting = true;
		agentError = null;
		try {
			const result = await testForvenAgentDiscord(
				selectedAgentId,
				agentDraft.discord_token || undefined,
			);
			agentMessage = `Test sent to #${result.channel} as ${result.agent_name ?? selectedAgentId}`;
			setTimeout(() => (agentMessage = null), 3000);
		} catch (e) {
			agentError = e instanceof Error ? e.message : 'Failed to send agent test message';
		} finally {
			agentDiscordTesting = false;
		}
	}

	async function loadSchedulerJobs() {
		schedulerLoading = true;
		try {
			schedulerJobs = await getForvenSchedulerJobs();
		} catch (e) {
			schedulerError = e instanceof Error ? e.message : 'Failed to load scheduler jobs';
		} finally {
			schedulerLoading = false;
		}
	}

	async function saveSchedulerJob(job: ForvenSchedulerJob) {
		if (job.id === undefined || job.id === null) return;
		const key = String(job.id);
		schedulerJobSaving = { ...schedulerJobSaving, [key]: true };
		schedulerError = null;
		try {
			await updateForvenSchedulerJob(
				job.id,
				job.schedule_type ?? 'cron',
				job.schedule_expr ?? '',
				job.enabled,
			);
			schedulerMessage = `Job ${job.name ?? job.id} updated`;
			setTimeout(() => (schedulerMessage = null), 3000);
			await loadSchedulerJobs();
		} catch (e) {
			schedulerError = e instanceof Error ? e.message : 'Failed to update scheduler job';
		} finally {
			schedulerJobSaving = { ...schedulerJobSaving, [key]: false };
		}
	}

	async function loadModelOptions(refresh = false) {
		if (refresh) modelOptionsRefreshing = true;
		else modelOptionsLoading = true;
		try {
			const res = await getForvenAgentModelOptions(refresh);
			modelOptions = res.options ?? [];
			enabledModelKeys = new Set(
				modelOptions.filter((o) => o.enabled).map((o) => o.key),
			);
		} catch (e) {
			modelOptionsError = e instanceof Error ? e.message : 'Failed to load model options';
		} finally {
			modelOptionsLoading = false;
			modelOptionsRefreshing = false;
		}
	}

	async function toggleModelKey(key: string, enabled: boolean) {
		const next = new Set(enabledModelKeys);
		if (enabled) next.add(key);
		else next.delete(key);
		enabledModelKeys = next;
		modelOptionsSaving = true;
		modelOptionsError = null;
		try {
			await updateSettingsSection('agent-model-keys', {
				agent_model_keys: [...next],
			});
			modelOptionsMessage = 'Model policy updated';
			setTimeout(() => (modelOptionsMessage = null), 2000);
		} catch (e) {
			modelOptionsError = e instanceof Error ? e.message : 'Failed to save model policy';
			const revert = new Set(enabledModelKeys);
			if (enabled) revert.delete(key);
			else revert.add(key);
			enabledModelKeys = revert;
		} finally {
			modelOptionsSaving = false;
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
			// Hand the authorize URL to the OS browser. In the packaged Tauri
			// shell, window.open(_blank) is a silent no-op — the OAuth tab
			// never loads and the user sees "Waiting for sign-in..." forever.
			// openExternal() invokes the tauri-plugin-opener command; in a
			// plain browser it falls back to window.open.
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
		void loadAgentRoster(false);
		void loadSchedulerJobs();
		void loadModelOptions(false);
		void loadAuthProviders();
	});

	function formatDate(iso: string | null | undefined): string {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	}
</script>

<div class="space-y-6">
	<!-- Roster-driven: AI providers (read-only status, CLI-managed credentials) -->
	<section
		aria-labelledby="agents-providers-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-4"
	>
		<header class="border-b border-gray-800 pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 id="agents-providers-heading" class="text-lg font-semibold text-white">
					AI providers
				</h2>
				<p class="text-xs text-gray-500 mt-1">
					Provider credentials are stored in the auth file and managed via CLI.
					{#if authFile}<span class="font-mono">{authFile}</span>{/if}
				</p>
			</div>
			<button
				type="button"
				on:click={() => loadAuthProviders()}
				disabled={authProvidersLoading}
				class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 disabled:opacity-60"
			>
				{authProvidersLoading ? 'Refreshing…' : 'Refresh'}
			</button>
		</header>

		{#if authProvidersError}
			<p class="text-xs text-red-400" role="alert">{authProvidersError}</p>
		{/if}

		{#if authProvidersLoading}
			<p class="text-sm text-gray-400">Loading providers…</p>
		{:else if authProviders.length === 0}
			<p class="text-sm text-gray-400">No providers registered.</p>
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
							? 'text-green-300 border-green-800 bg-green-950/40'
							: provider.status === 'not_configured'
								? 'text-gray-400 border-gray-700 bg-gray-900/40'
								: provider.status === 'needs_reauth'
									? 'text-red-300 border-red-800 bg-red-950/40'
									: 'text-amber-300 border-amber-800 bg-amber-950/40'}
					<li class="bg-black border border-gray-800 rounded p-4 space-y-3">
						<div class="flex flex-wrap items-center justify-between gap-2">
							<div class="flex items-center gap-2">
								<span class="font-mono text-sm text-white uppercase">{key}</span>
								<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border {statusColor}">
									{provider.status === 'needs_reauth' ? 're-authenticate' : provider.status}
								</span>
								{#if provider.supports_oauth}
									<span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-gray-700 text-gray-400">
										oauth
									</span>
								{/if}
							</div>
							{#if provider.expires_in}
								<span class="text-xs text-gray-400">{provider.expires_in}</span>
							{/if}
						</div>

						{#if provider.expires_at}
							<p class="text-xs text-gray-500">Expires {provider.expires_at}</p>
						{/if}
						{#if provider.base_url}
							<p class="text-xs text-gray-400">
								Base URL: <span class="font-mono">{provider.base_url}</span>
							</p>
						{/if}
						{#if provider.last_refresh_error}
							<div class="rounded border border-red-900 bg-red-950/30 px-2 py-1.5 text-xs text-red-300">
								<span class="font-semibold">Token refresh failed:</span>
								{provider.last_refresh_error}
								{#if provider.supports_oauth}
									<span class="text-red-200/80">— sign in again to recover.</span>
								{/if}
							</div>
						{/if}

						{#if msg}
							<p class="text-xs text-green-400" role="status">{msg}</p>
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
									? 'text-green-300 border-green-800 bg-green-950/40'
									: pollStatus === 'expired' ||
										  pollStatus === 'denied' ||
										  pollStatus === 'error'
										? 'text-red-300 border-red-800 bg-red-950/40'
										: pollStatus === 'slow_down' || pollStatus === 'retrying'
											? 'text-amber-300 border-amber-800 bg-amber-950/40'
											: 'text-blue-300 border-blue-800 bg-blue-950/40'}
							<div class="bg-gray-950 border border-gray-700 rounded p-3 space-y-2">
								<p class="text-xs text-gray-300">
									{oauth.flow === 'device_code' ? 'Device code flow' : 'Authorization code flow'}
								</p>
								{#if oauth.verification_url && oauth.user_code}
									<p class="text-xs text-gray-400">
										Go to <a
											href={oauth.verification_url}
											on:click|preventDefault={() => openExternal(oauth.verification_url!)}
											class="text-blue-400 underline cursor-pointer">{oauth.verification_url}</a>
										and enter code <span class="font-mono text-white">{oauth.user_code}</span>
									</p>
								{:else if oauth.authorize_url}
									<p class="text-xs text-gray-400">
										A new tab opened to <a
											href={oauth.authorize_url}
											on:click|preventDefault={() => openExternal(oauth.authorize_url!)}
											class="text-blue-400 underline cursor-pointer">authorize</a>.
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
										class="w-full bg-black border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									/>
								{/if}
								{#if oauth.bind_error}
									<p class="text-[11px] text-amber-300">
										Couldn't bind loopback listener ({oauth.bind_error}); using manual paste.
									</p>
								{/if}
								<div class="flex items-center gap-2">
									<span
										class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border {pillColor}"
									>
										{pillLabel}
									</span>
									{#if isAuthorizationCode}
										<button
											type="button"
											on:click={() => completeOAuth(key)}
											disabled={busy || !(oauth.code ?? '').trim()}
											class="text-xs px-3 py-1 rounded bg-blue-700 text-white hover:bg-blue-600 disabled:opacity-60"
										>
											{busy ? 'Completing…' : 'Use pasted code'}
										</button>
									{/if}
									<button
										type="button"
										on:click={() => cancelOAuth(key)}
										class="text-xs px-3 py-1 rounded border border-gray-700 text-gray-300 hover:text-white"
									>
										Cancel
									</button>
								</div>
							</div>
						{:else}
							{@const isActive = provider.configured && provider.status === 'active'}
							{#if isActive}
								<p class="text-xs text-green-400">
									Connected{provider.expires_in ? ` · renews in ${provider.expires_in}` : ''}. No action needed.
								</p>
							{:else}
								<div class="flex flex-wrap items-end gap-2">
									{#if isBaseUrlProvider}
										<label class="flex-1 min-w-[14rem]">
											<span class="block text-xs text-gray-400 mb-1">Base URL</span>
											<input
												type="text"
												placeholder={provider.base_url ?? 'http://localhost:1234/v1'}
												bind:value={providerBaseUrlInput[key]}
												class="w-full bg-black border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
											/>
										</label>
										<button
											type="button"
											on:click={() => saveProviderBaseUrl(key)}
											disabled={busy}
											class="text-xs px-3 py-1.5 rounded bg-blue-700 text-white hover:bg-blue-600 disabled:opacity-60"
										>
											{busy ? 'Saving…' : 'Save'}
										</button>
									{:else}
										<label class="flex-1 min-w-[14rem]">
											<span class="block text-xs text-gray-400 mb-1">API key / access token</span>
											<input
												type="password"
												placeholder="Paste token and press Save"
												bind:value={providerTokenInput[key]}
												class="w-full bg-black border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
											/>
										</label>
										<button
											type="button"
											on:click={() => saveProviderToken(key)}
											disabled={busy}
											class="text-xs px-3 py-1.5 rounded bg-blue-700 text-white hover:bg-blue-600 disabled:opacity-60"
										>
											{busy ? 'Saving…' : 'Save'}
										</button>
										{#if provider.supports_oauth}
											<button
												type="button"
												on:click={() => startOAuth(key)}
												disabled={busy}
												class="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-200 hover:text-white hover:border-gray-500 disabled:opacity-60"
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
										class="text-xs px-3 py-1 rounded border border-gray-700 text-gray-300 hover:text-white disabled:opacity-60"
									>
										{busy ? 'Testing…' : 'Test connection'}
									</button>
									{#if isActive && provider.supports_oauth}
										<button
											type="button"
											on:click={() => startOAuth(key)}
											disabled={busy}
											class="text-xs px-3 py-1 rounded border border-gray-700 text-gray-300 hover:text-white disabled:opacity-60"
										>
											Re-authenticate
										</button>
									{/if}
									<button
										type="button"
										on:click={() => disconnectProvider(key)}
										disabled={busy}
										class="text-xs px-3 py-1 rounded border border-red-900 text-red-300 hover:text-red-200 hover:border-red-700 disabled:opacity-60"
									>
										Disconnect
									</button>
								</div>
							{/if}
						{/if}

						<details class="text-xs text-gray-500">
							<summary class="cursor-pointer hover:text-gray-300">CLI equivalent</summary>
							<div class="mt-1 space-y-1">
								{#if provider.login_command}
									<p><span class="text-gray-600">Login:</span> <span class="font-mono text-gray-400">{provider.login_command}</span></p>
								{/if}
								{#if provider.refresh_command && provider.configured}
									<p><span class="text-gray-600">Refresh:</span> <span class="font-mono text-gray-400">{provider.refresh_command}</span></p>
								{/if}
							</div>
						</details>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	{#if variant !== 'wizard'}
	<!-- Roster-driven: Model policy (enabled model checkboxes grouped by provider) -->
	<section
		aria-labelledby="agents-model-policy-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-4"
	>
		<header class="border-b border-gray-800 pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 id="agents-model-policy-heading" class="text-lg font-semibold text-white">
					Model policy
				</h2>
				<p class="text-xs text-gray-500 mt-1">
					Check the model options that should appear in the agent model picker.
				</p>
			</div>
			<button
				type="button"
				on:click={() => loadModelOptions(true)}
				disabled={modelOptionsRefreshing || modelOptionsLoading}
				class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 disabled:opacity-60"
			>
				{modelOptionsRefreshing ? 'Refreshing…' : 'Refresh from providers'}
			</button>
		</header>

		{#if modelOptionsError}
			<p class="text-xs text-red-400" role="alert">{modelOptionsError}</p>
		{/if}
		{#if modelOptionsMessage}
			<p class="text-xs text-green-400" role="status">{modelOptionsMessage}</p>
		{/if}

		{#if modelOptionsLoading}
			<p class="text-sm text-gray-400">Loading available models…</p>
		{:else if modelOptions.length === 0}
			<p class="text-sm text-gray-400">
				No models discovered. Configure a provider under AI providers above.
			</p>
		{:else}
			{@const grouped = modelOptions.reduce<Record<string, ForvenAgentModelOption[]>>(
				(acc, opt) => {
					(acc[opt.provider] ??= []).push(opt);
					return acc;
				},
				{},
			)}
			<div class="space-y-4">
				{#each Object.entries(grouped) as [provider, opts] (provider)}
					<div>
						<h3 class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
							{provider} <span class="text-gray-600 font-normal">({opts.length})</span>
						</h3>
						<div class="grid gap-1 md:grid-cols-2 lg:grid-cols-3">
							{#each opts as opt (opt.key)}
								<label
									class="flex items-center gap-2 px-2 py-1.5 rounded text-sm text-gray-200 hover:bg-gray-900 cursor-pointer"
								>
									<input
										type="checkbox"
										checked={enabledModelKeys.has(opt.key)}
										disabled={modelOptionsSaving}
										on:change={(e) =>
											toggleModelKey(opt.key, (e.target as HTMLInputElement).checked)}
										class="rounded"
									/>
									<span class="font-mono text-xs">{opt.label}</span>
								</label>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</section>

	{/if}

	{#if variant !== 'wizard'}
	<!-- Roster-driven: Agent personas + per-agent documents -->
	<section
		aria-labelledby="agents-personas-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-4"
	>
		<header class="border-b border-gray-800 pb-2">
			<h2 id="agents-personas-heading" class="text-lg font-semibold text-white">
				Agent personas
			</h2>
			<p class="text-xs text-gray-500 mt-1">
				Per-agent role, model, schedule, instructions, and SOUL.md / AGENTS.md / ROLE.md.
			</p>
		</header>

		{#if agentError}
			<p class="text-xs text-red-400" role="alert">{agentError}</p>
		{/if}
		{#if agentMessage}
			<p class="text-xs text-green-400" role="status">{agentMessage}</p>
		{/if}

		{#if agentsLoading}
			<p class="text-sm text-gray-400">Loading agents…</p>
		{:else if agents.length === 0}
			<p class="text-sm text-gray-400">No agents registered.</p>
		{:else}
			<div class="grid gap-6 md:grid-cols-[240px_1fr]">
				<!-- Roster -->
				<nav aria-label="Agent roster">
					<ul class="space-y-1" role="listbox" aria-label="Agent roster">
						{#each agents as agent (agent.id)}
							<li>
								<button
									type="button"
									role="option"
									aria-selected={agent.id === selectedAgentId}
									on:click={() => selectAgent(agent.id ?? null)}
									class="w-full text-left px-3 py-2 rounded text-sm border border-gray-800 hover:border-gray-600 {agent.id ===
									selectedAgentId
										? 'bg-gray-800 text-white border-gray-600'
										: 'bg-gray-900 text-gray-300'}"
								>
									<span class="block font-medium truncate">{agent.name ?? agent.id}</span>
								</button>
							</li>
						{/each}
					</ul>
				</nav>

				<!-- Selected agent detail -->
				<div class="space-y-6">
					{#if agentDraft && selectedAgentId}
						<div class="space-y-3">
							<div class="grid gap-3 md:grid-cols-2">
								<label class="block text-xs text-gray-400">
									Name
									<input
										type="text"
										bind:value={agentDraft.name}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
									/>
								</label>
								<label class="block text-xs text-gray-400">
									Role
									<input
										type="text"
										bind:value={agentDraft.role}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
									/>
								</label>
								<label class="block text-xs text-gray-400">
									Model provider
									<input
										type="text"
										bind:value={agentDraft.model}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
									/>
								</label>
								<label class="block text-xs text-gray-400">
									Model ID
									<input
										type="text"
										bind:value={agentDraft.model_id}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
									/>
								</label>
								<label class="block text-xs text-gray-400">
									Schedule type
									<select
										bind:value={agentDraft.schedule_type}
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
									>
										<option value="">Unspecified</option>
										<option value="cron">Cron</option>
										<option value="interval">Interval</option>
									</select>
								</label>
								<label class="block text-xs text-gray-400">
									Schedule expression
									<input
										type="text"
										bind:value={agentDraft.schedule_expr}
										placeholder="e.g. 0 9 * * *"
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									/>
								</label>
							</div>

							<label class="flex items-center gap-2 text-sm text-gray-300">
								<input type="checkbox" bind:checked={agentDraft.enabled} class="rounded" />
								Enabled
							</label>

							<label class="block text-xs text-gray-400">
								Instructions
								<textarea
									rows="6"
									bind:value={agentDraft.instructions}
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-xs font-mono"
								></textarea>
							</label>

							<label class="block text-xs text-gray-400">
								Discord bot token
								{#if agentDraft.has_discord_token}
									<span class="text-gray-500">(saved — enter a new value to overwrite)</span>
								{/if}
								<input
									type="password"
									bind:value={agentDraft.discord_token}
									placeholder={agentDraft.has_discord_token ? '•••••••• (saved)' : ''}
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
								/>
							</label>

							<div class="flex gap-2">
								<button
									type="button"
									on:click={saveAgent}
									disabled={agentSaving}
									class="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white px-3 py-1.5 rounded text-sm"
								>
									{agentSaving ? 'Saving…' : 'Update agent'}
								</button>
								<button
									type="button"
									on:click={testAgentDiscord}
									disabled={agentDiscordTesting || !agentDraft.has_discord_token}
									class="border border-gray-700 hover:border-gray-500 text-gray-200 disabled:bg-gray-800 disabled:text-gray-500 px-3 py-1.5 rounded text-sm"
								>
									{agentDiscordTesting ? 'Sending…' : 'Send Discord test'}
								</button>
							</div>
						</div>

						<!-- Per-agent documents -->
						<div class="bg-black border border-gray-800 rounded p-4 space-y-4">
							<h3 class="text-sm font-medium text-white">Agent docs</h3>
							<p class="text-xs text-gray-500">
								SOUL.md, AGENTS.md, and ROLE.md are saved per-agent. Restart background
								services if behavior updates need to propagate.
							</p>

							{#each agentDocKinds as doc}
								<div class="space-y-2">
									<div class="flex items-center justify-between">
										<span class="block text-xs text-gray-400">{doc.toUpperCase()}.md</span>
										<button
											type="button"
											on:click={() => saveAgentDoc(doc)}
											disabled={agentDocSaving[doc] || agentDocsLoading}
											class="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 disabled:opacity-60"
										>
											{agentDocSaving[doc] ? 'Saving…' : 'Save'}
										</button>
									</div>
									{#if agentDocsLoading}
										<p class="text-xs text-gray-500">Loading {doc.toUpperCase()}.md…</p>
									{:else}
										<textarea
											rows="8"
											bind:value={agentDocs[doc]}
											aria-label={`${doc.toUpperCase()}.md content`}
											class="w-full bg-gray-950 border border-gray-700 text-white px-3 py-2 rounded text-xs font-mono resize-y"
										></textarea>
									{/if}
								</div>
							{/each}
						</div>
					{:else}
						<p class="text-sm text-gray-400">Select an agent to edit its settings and docs.</p>
					{/if}
				</div>
			</div>
		{/if}
	</section>

	{/if}

	{#if variant !== 'wizard'}
	<!-- Roster-driven: Scheduler jobs -->
	<section
		aria-labelledby="agents-scheduler-heading"
		class="border border-gray-800 rounded-lg bg-black p-6 space-y-4"
	>
		<header class="border-b border-gray-800 pb-2">
			<h2 id="agents-scheduler-heading" class="text-lg font-semibold text-white">
				Scheduler jobs
			</h2>
			<p class="text-xs text-gray-500 mt-1">
				Schedules for continuous learning and trading processes. Each job has its own
				cron/interval.
			</p>
		</header>

		{#if schedulerError}
			<p class="text-xs text-red-400" role="alert">{schedulerError}</p>
		{/if}
		{#if schedulerMessage}
			<p class="text-xs text-green-400" role="status">{schedulerMessage}</p>
		{/if}

		{#if schedulerLoading}
			<p class="text-sm text-gray-400">Loading scheduler jobs…</p>
		{:else if schedulerJobs.length === 0}
			<p class="text-sm text-gray-400">No scheduler jobs found.</p>
		{:else}
			<div class="space-y-3">
				{#each schedulerJobs as job (job.id)}
					<div class="bg-black border border-gray-800 rounded p-4 space-y-3">
						<div class="flex items-start justify-between gap-3">
							<div>
								<h3 class="font-medium text-white">{job.name ?? job.id}</h3>
								<p class="text-xs text-gray-500 font-mono">ID: {job.id}</p>
							</div>
							<label class="flex items-center gap-2 text-sm text-gray-300">
								<input type="checkbox" bind:checked={job.enabled} class="rounded" />
								Enabled
							</label>
						</div>

						<div class="grid gap-3 md:grid-cols-[160px_1fr_auto] items-end">
							<label class="block text-xs text-gray-400">
								Type
								<select
									bind:value={job.schedule_type}
									on:change={() => (job.schedule_expr = '')}
									class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm"
								>
									<option value="cron">Cron</option>
									<option value="interval">Interval (minutes)</option>
								</select>
							</label>
							{#if job.schedule_type === 'interval'}
								<label class="block text-xs text-gray-400">
									Run every (minutes)
									<input
										type="number"
										min="1"
										step="1"
										value={msToMinutes(job.schedule_expr)}
										on:input={(e) =>
											(job.schedule_expr = minutesToMs(e.currentTarget.value))}
										placeholder="e.g. 60"
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									/>
								</label>
							{:else}
								<label class="block text-xs text-gray-400">
									Expression (cron)
									<input
										type="text"
										bind:value={job.schedule_expr}
										placeholder="e.g. 0 9 * * *"
										class="mt-1 w-full bg-gray-950 border border-gray-700 text-white px-2 py-1.5 rounded text-sm font-mono"
									/>
								</label>
							{/if}
							<button
								type="button"
								on:click={() => saveSchedulerJob(job)}
								disabled={schedulerJobSaving[String(job.id ?? '')]}
								class="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white px-3 py-1.5 rounded text-sm"
							>
								{schedulerJobSaving[String(job.id ?? '')] ? 'Saving…' : 'Update job'}
							</button>
						</div>

						<div class="flex gap-4 text-xs text-gray-400">
							{#if job.schedule_type === 'interval' && job.schedule_expr}
								<span>Schedule: <span class="text-gray-300">{formatIntervalMs(job.schedule_expr)}</span></span>
							{/if}
							{#if job.next_run_at}
								<span>Next run: <span class="text-gray-300">{formatDate(job.next_run_at)}</span></span>
							{/if}
							{#if job.last_status}
								<span
									>Last status:
									<span class={job.last_status === 'ok' ? 'text-green-400' : 'text-red-400'}
										>{job.last_status}</span
									></span
								>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</section>
	{/if}
</div>
