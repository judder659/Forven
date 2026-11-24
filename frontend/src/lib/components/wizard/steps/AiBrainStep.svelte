<script lang="ts">
	import {
		reconcileAgentProviders,
		testForvenAuthProvider,
		updateSettingsSection,
		type ForvenAuthProviderStatus,
	} from '$lib/api';
	import { openExternal } from '$lib/external-open';
	import { providerLabel } from '$lib/utils/agentsHub/agents';
	import ProviderConnect from '$lib/components/agents/setup/ProviderConnect.svelte';

	export let providers: ForvenAuthProviderStatus[] = [];
	export let loading = false;
	export let settings: Record<string, unknown> = {};
	export let onProvidersChanged: (() => Promise<void> | void) | undefined = undefined;
	export let onSettingsUpdated: (() => Promise<void> | void) | undefined = undefined;

	const POPULAR_ORDER = ['claude-code', 'openai', 'anthropic', 'gemini', 'openrouter', 'lmstudio'];

	const PROVIDER_HINTS: Record<string, { tagline: string; badge: string; signupUrl?: string; signupLabel?: string }> = {
		'claude-code': {
			tagline: 'Use your existing Claude Pro/Max subscription through Claude Code — no separate API billing.',
			badge: 'Subscription / CLI',
			signupUrl: 'https://docs.anthropic.com/en/docs/claude-code',
			signupLabel: 'About Claude Code',
		},
		openai: {
			tagline: 'GPT models — sign in with your OpenAI account or paste an API key.',
			badge: 'Sign in or API key',
			signupUrl: 'https://platform.openai.com/api-keys',
			signupLabel: 'OpenAI API keys',
		},
		anthropic: {
			tagline: 'Direct access to Claude models with an Anthropic API key.',
			badge: 'API key',
			signupUrl: 'https://console.anthropic.com/settings/keys',
			signupLabel: 'Anthropic Console',
		},
		gemini: {
			tagline: 'Google Gemini models — great starting point with a free tier on Google AI Studio.',
			badge: 'Free tier available',
			signupUrl: 'https://aistudio.google.com/apikey',
			signupLabel: 'Google AI Studio',
		},
		openrouter: {
			tagline: 'One API key unlocks 100+ models from every lab, including free community models.',
			badge: 'Multi-model',
			signupUrl: 'https://openrouter.ai/keys',
			signupLabel: 'OpenRouter keys',
		},
		lmstudio: {
			tagline: 'Run open-weight models locally on your own machine — 100% private and free.',
			badge: 'Local & free',
			signupUrl: 'https://lmstudio.ai',
			signupLabel: 'Get LM Studio',
		},
		deepseek: {
			tagline: 'Low-cost reasoning and coding models via DeepSeek API.',
			badge: 'API key',
			signupUrl: 'https://platform.deepseek.com/api_keys',
			signupLabel: 'DeepSeek Console',
		},
		groq: {
			tagline: 'Ultra-fast inference on open models with a generous free tier.',
			badge: 'Fast · Free tier',
			signupUrl: 'https://console.groq.com/keys',
			signupLabel: 'Groq Console',
		},
		xai: {
			tagline: 'Grok models via xAI API key.',
			badge: 'API key',
			signupUrl: 'https://console.x.ai',
			signupLabel: 'xAI Console',
		},
		mistral: {
			tagline: 'Mistral coding and reasoning models.',
			badge: 'API key',
			signupUrl: 'https://console.mistral.ai/api-keys',
			signupLabel: 'Mistral Console',
		},
	};

	const PACE_OPTIONS: Array<{
		id: 'conserve' | 'balanced' | 'max';
		label: string;
		tag: string;
		description: string;
	}> = [
		{
			id: 'conserve',
			label: 'Gentle / Free-tier',
			tag: '~12 ideas/day',
			description: 'Slower cadence and smaller batches — great for free tiers or keeping API spend minimal.',
		},
		{
			id: 'balanced',
			label: 'Balanced',
			tag: '~40 ideas/day',
			description: 'The default mix of research speed and cost for a standard paid provider or plan.',
		},
		{
			id: 'max',
			label: 'High throughput',
			tag: '~120 ideas/day',
			description: 'Fastest strategy discovery for high-limit keys or fast local models.',
		},
	];

	const BOT_OPS_KEYS = [
		'ideation_interval_minutes',
		'coding_interval_minutes',
		'testing_interval_minutes',
		'graduation_interval_minutes',
		'agent_task_claim_limit',
		'backtest_subprocess_budget',
		'gauntlet_drain_workers',
	];

	let selectedProviderKey: string | null = null;
	let showAllProviders = false;
	let testingKey: string | null = null;
	let testFeedback: Record<string, { ok: boolean; text: string }> = {};
	let paceSaving: string | null = null;
	let paceMessage: { ok: boolean; text: string } | null = null;
	let selectedPaceOverride: string | null = null;

	$: connectedProviders = providers.filter((p) => p?.status === 'active');
	$: availableProviders = providers.filter((p) => p?.status !== 'active');

	$: popularAvailable = POPULAR_ORDER
		.map((id) => availableProviders.find((p) => String(p.provider) === id))
		.filter((p): p is ForvenAuthProviderStatus => Boolean(p));

	$: extraAvailable = availableProviders
		.filter((p) => !POPULAR_ORDER.includes(String(p.provider)))
		.sort((a, b) => providerLabel(a.provider).localeCompare(providerLabel(b.provider)));

	$: throughputPresets = (settings?.throughput_presets as Record<string, Record<string, number>> | undefined) ?? null;

	$: currentPace = selectedPaceOverride ?? deriveCurrentPace(settings, throughputPresets);

	function deriveCurrentPace(
		s: Record<string, unknown>,
		presets: Record<string, Record<string, number>> | null,
	): string {
		const effective = String(s?.throughput_preset_effective ?? '').trim().toLowerCase();
		if (effective && ['trickle', 'conserve', 'balanced', 'max'].includes(effective)) {
			return effective === 'trickle' ? 'conserve' : effective;
		}
		if (!presets) return 'balanced';
		const researchSettings = (s?.research_settings as Record<string, unknown> | undefined) ?? {};
		const currentBudget = Number(researchSettings?.strategy_creation_daily_budget ?? s?.strategy_creation_daily_budget);
		for (const opt of PACE_OPTIONS) {
			const bundle = presets[opt.id];
			if (bundle && Number(bundle.strategy_creation_daily_budget) === currentBudget) {
				return opt.id;
			}
		}
		return 'balanced';
	}

	function hintFor(provider: ForvenAuthProviderStatus): { tagline: string; badge: string; signupUrl?: string; signupLabel?: string } {
		const key = String(provider.provider);
		if (PROVIDER_HINTS[key]) return PROVIDER_HINTS[key];
		if (provider.connect_mode === 'cli') {
			return { tagline: 'Uses your local CLI login on this computer.', badge: 'Local CLI' };
		}
		if (provider.requires_token === false && !provider.supports_oauth) {
			return { tagline: 'Connect to a local OpenAI-compatible server via its URL.', badge: 'Local server' };
		}
		return {
			tagline: provider.supports_oauth ? 'Sign in with OAuth or paste an API key.' : 'Connect with your API key.',
			badge: provider.supports_oauth ? 'OAuth or API key' : 'API key',
		};
	}

	async function handleConnected(key: string): Promise<void> {
		selectedProviderKey = null;
		await onProvidersChanged?.();
		try {
			await reconcileAgentProviders();
		} catch {
			// Best-effort — agents also reconcile on finish and fall back at runtime.
		}
		testFeedback = {
			...testFeedback,
			[key]: { ok: true, text: `${providerLabel(key)} connected and ready for your agents.` },
		};
	}

	async function runProviderTest(key: string): Promise<void> {
		testingKey = key;
		try {
			const res = await testForvenAuthProvider(key);
			testFeedback = {
				...testFeedback,
				[key]: {
					ok: res.ok,
					text: res.ok ? 'Connection verified — ready to run.' : res.message || `Test failed (${res.status})`,
				},
			};
		} catch (err) {
			testFeedback = {
				...testFeedback,
				[key]: {
					ok: false,
					text: err instanceof Error ? err.message : 'Test failed.',
				},
			};
		} finally {
			testingKey = null;
		}
	}

	async function applyPace(paceId: 'conserve' | 'balanced' | 'max'): Promise<void> {
		if (!throughputPresets || !throughputPresets[paceId]) {
			selectedPaceOverride = paceId;
			return;
		}
		const bundle = throughputPresets[paceId];
		paceSaving = paceId;
		paceMessage = null;
		try {
			const botOpsPayload: Record<string, number> = {};
			for (const k of BOT_OPS_KEYS) {
				if (bundle[k] !== undefined) botOpsPayload[k] = bundle[k];
			}
			if (Object.keys(botOpsPayload).length > 0) {
				await updateSettingsSection('bot-operations', botOpsPayload);
			}
			if (bundle.strategy_creation_daily_budget !== undefined) {
				await updateSettingsSection('research', {
					strategy_creation_daily_budget: bundle.strategy_creation_daily_budget,
				});
			}
			selectedPaceOverride = paceId;
			paceMessage = { ok: true, text: `AI pace saved (${PACE_OPTIONS.find((o) => o.id === paceId)?.label}).` };
			await onSettingsUpdated?.();
		} catch (err) {
			paceMessage = {
				ok: false,
				text: err instanceof Error ? err.message : 'Could not update AI pace.',
			};
		} finally {
			paceSaving = null;
		}
	}
</script>

<div class="space-y-5" data-testid="wizard-step-ai">
	{#if connectedProviders.length > 0}
		<section class="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.06] p-4 space-y-3" data-testid="ai-connected-summary">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<div class="flex items-center gap-2">
					<span class="w-5 h-5 rounded-full bg-emerald-500/20 border border-emerald-500/40 text-emerald-400 flex items-center justify-center text-xs" aria-hidden="true">✓</span>
					<h3 class="text-[13.5px] font-semibold text-sc-ink">
						{connectedProviders.length === 1
							? `${providerLabel(connectedProviders[0].provider)} is connected`
							: `${connectedProviders.length} AI providers connected`}
					</h3>
				</div>
				<span class="text-xs text-emerald-300/90">Your agents &amp; chat assistant are ready to use this.</span>
			</div>

			<div class="grid gap-2 sm:grid-cols-2">
				{#each connectedProviders as provider (provider.provider)}
					{@const key = String(provider.provider)}
					<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2.5 flex items-center justify-between gap-2">
						<div class="min-w-0">
							<p class="text-[12.5px] font-medium text-sc-ink truncate">{providerLabel(key)}</p>
							{#if testFeedback[key]}
								<p class="text-[11px] truncate {testFeedback[key].ok ? 'text-emerald-400' : 'text-amber-300'}">
									{testFeedback[key].text}
								</p>
							{:else if provider.expires_in}
								<p class="text-[11px] text-sc-ink3 truncate">Active · renews in {provider.expires_in}</p>
							{:else}
								<p class="text-[11px] text-emerald-400/90">Active</p>
							{/if}
						</div>
						<button
							type="button"
							class="terminal-button text-[11px] py-1 px-2.5 flex-shrink-0"
							disabled={testingKey === key}
							on:click={() => runProviderTest(key)}
						>
							{testingKey === key ? 'Testing…' : 'Test'}
						</button>
					</div>
				{/each}
			</div>
		</section>
	{:else if !loading}
		<div class="rounded-lg border border-sc-line bg-sc-panel2/60 px-4 py-3 text-xs text-sc-ink2 leading-relaxed">
			Pick <span class="text-sc-ink font-medium">one AI provider</span> below to power strategy creation, research, and the in-app chat assistant.
			Already have a <span class="text-sc-ink">Claude</span> or <span class="text-sc-ink">ChatGPT</span> subscription, or want to start free with <span class="text-sc-ink">Google Gemini</span> / <span class="text-sc-ink">LM Studio</span>? You’re covered.
		</div>
	{/if}

	<section aria-labelledby="wizard-ai-picker-heading" class="space-y-3">
		<div class="flex items-baseline justify-between gap-2">
			<h3 id="wizard-ai-picker-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
				{connectedProviders.length > 0 ? 'Add another provider (optional)' : 'Popular AI providers'}
			</h3>
			<span class="text-[11px] text-sc-ink3">Credentials stay encrypted locally on your machine</span>
		</div>

		{#if loading && providers.length === 0}
			<div class="grid gap-2.5 sm:grid-cols-2">
				{#each [0, 1, 2, 3] as i (i)}
					<div class="h-24 rounded-md border border-sc-line bg-sc-panel2/40 animate-pulse"></div>
				{/each}
			</div>
		{:else if availableProviders.length === 0 && connectedProviders.length === 0}
			<p class="text-xs text-sc-ink3 rounded-md border border-sc-line bg-sc-bg p-4">
				Could not load the provider list yet. You can also connect providers anytime on the <span class="text-sc-ink2">Agents → Setup</span> page.
			</p>
		{:else}
			<div class="grid gap-2.5 sm:grid-cols-2">
				{#each popularAvailable as provider (provider.provider)}
					{@const key = String(provider.provider)}
					{@const hint = hintFor(provider)}
					{@const isExpanded = selectedProviderKey === key}
					<article
						class="rounded-lg border transition-colors p-3.5
							{isExpanded
								? 'border-sc-ink/50 bg-sc-panel2 sm:col-span-2'
								: 'border-sc-line bg-sc-bg/70 hover:border-sc-line2'}"
						data-testid="wizard-provider-card-{key}"
					>
						<div class="flex items-start justify-between gap-3">
							<div class="min-w-0 space-y-1">
								<div class="flex flex-wrap items-center gap-2">
									<h4 class="text-[13px] font-semibold text-sc-ink">{providerLabel(key)}</h4>
									<span class="font-plex-cond text-[10px] uppercase tracking-[0.08em] px-1.5 py-0.5 rounded border border-sc-line2 text-sc-ink3">
										{hint.badge}
									</span>
								</div>
								<p class="text-[11.5px] text-sc-ink2 leading-relaxed">{hint.tagline}</p>
							</div>
							{#if !isExpanded}
								<button
									type="button"
									class="terminal-button text-[11.5px] py-1 px-2.5 flex-shrink-0"
									on:click={() => (selectedProviderKey = key)}
								>
									Connect
								</button>
							{/if}
						</div>

						{#if isExpanded}
							<div class="mt-3 pt-3 border-t border-sc-line space-y-2.5">
								{#if hint.signupUrl}
									<p class="text-[11.5px] text-sc-ink3">
										Need credentials?
										<a
											href={hint.signupUrl}
											target="_blank"
											rel="noopener noreferrer"
											on:click|preventDefault={() => hint.signupUrl && openExternal(hint.signupUrl)}
											class="text-emerald-400 hover:text-emerald-300 underline ml-1"
										>
											{hint.signupLabel ?? 'Get a key'} ↗
										</a>
									</p>
								{/if}
								<ProviderConnect
									{provider}
									on:connected={() => handleConnected(key)}
									on:cancel={() => (selectedProviderKey = null)}
								/>
							</div>
						{/if}
					</article>
				{/each}
			</div>

			{#if extraAvailable.length > 0}
				<div class="pt-1">
					<button
						type="button"
						class="text-xs text-sc-ink3 hover:text-sc-ink flex items-center gap-1.5"
						on:click={() => (showAllProviders = !showAllProviders)}
					>
						<span>{showAllProviders ? '▾' : '▸'}</span>
						<span>{showAllProviders ? 'Hide other providers' : `Show ${extraAvailable.length} more ${extraAvailable.length === 1 ? 'provider' : 'providers'} (DeepSeek, Groq, xAI, Mistral…)`}</span>
					</button>

					{#if showAllProviders}
						<div class="mt-2.5 grid gap-2 sm:grid-cols-2">
							{#each extraAvailable as provider (provider.provider)}
								{@const key = String(provider.provider)}
								{@const hint = hintFor(provider)}
								{@const isExpanded = selectedProviderKey === key}
								<article
									class="rounded-md border p-3
										{isExpanded
											? 'border-sc-ink/50 bg-sc-panel2 sm:col-span-2'
											: 'border-sc-line bg-sc-bg/60 hover:border-sc-line2'}"
								>
									<div class="flex items-center justify-between gap-2">
										<div class="min-w-0">
											<h4 class="text-[12.5px] font-medium text-sc-ink">{providerLabel(key)}</h4>
											<p class="text-[11px] text-sc-ink3 truncate">{hint.tagline}</p>
										</div>
										{#if !isExpanded}
											<button
												type="button"
												class="terminal-button text-[11px] py-1 px-2 flex-shrink-0"
												on:click={() => (selectedProviderKey = key)}
											>
												Connect
											</button>
										{/if}
									</div>
									{#if isExpanded}
										<div class="mt-2.5 pt-2.5 border-t border-sc-line space-y-2">
											{#if hint.signupUrl}
												<p class="text-[11px] text-sc-ink3">
													Need a key?
													<a
														href={hint.signupUrl}
														target="_blank"
														rel="noopener noreferrer"
														on:click|preventDefault={() => hint.signupUrl && openExternal(hint.signupUrl)}
														class="text-emerald-400 hover:text-emerald-300 underline ml-1"
													>
														{hint.signupLabel ?? 'Open console'} ↗
													</a>
												</p>
											{/if}
											<ProviderConnect
												{provider}
												on:connected={() => handleConnected(key)}
												on:cancel={() => (selectedProviderKey = null)}
											/>
										</div>
									{/if}
								</article>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		{/if}
	</section>

	{#if throughputPresets}
		<section aria-labelledby="wizard-pace-heading" class="pt-3 border-t border-sc-line space-y-2.5">
			<div class="flex flex-wrap items-baseline justify-between gap-2">
				<h3 id="wizard-pace-heading" class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
					AI usage pace (optional — easy to change later)
				</h3>
				{#if paceMessage}
					<span class="text-[11.5px] {paceMessage.ok ? 'text-emerald-400' : 'text-red-400'}" role="status">
						{paceMessage.text}
					</span>
				{/if}
			</div>

			<div class="grid grid-cols-1 sm:grid-cols-3 gap-2" role="radiogroup" aria-label="AI usage pace">
				{#each PACE_OPTIONS as opt (opt.id)}
					{@const active = currentPace === opt.id}
					{@const busy = paceSaving === opt.id}
					<button
						type="button"
						role="radio"
						aria-checked={active}
						disabled={paceSaving !== null}
						class="rounded-md border p-3 text-left transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3
							{active
								? 'border-emerald-500/60 bg-emerald-500/[0.07]'
								: 'border-sc-line bg-sc-bg/60 hover:border-sc-line2'}"
						on:click={() => applyPace(opt.id)}
					>
						<div class="flex items-center justify-between gap-1.5">
							<span class="text-xs font-semibold text-sc-ink">{opt.label}</span>
							<span class="font-plex-mono text-[10.5px] text-sc-ink3">{busy ? 'Saving…' : opt.tag}</span>
						</div>
						<p class="text-[11px] text-sc-ink3 leading-relaxed mt-1">{opt.description}</p>
					</button>
				{/each}
			</div>
		</section>
	{/if}
</div>
