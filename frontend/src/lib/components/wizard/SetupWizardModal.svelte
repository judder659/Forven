<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getForvenAuthProviders,
		reconcileAgentProviders,
		updateSettingsSection,
		type ForvenAuthProviderStatus,
	} from '$lib/api';
	import { getCatalog } from '$lib/api/dataManager';
	import { addToast } from '$lib/stores/processTracker';
	import { wizardOpen, wizardStep, closeWizard, clearWizardResume } from '$lib/stores/setupWizard';
	import WizardStepRail, { type WizardRailStep } from './WizardStepRail.svelte';
	import WelcomeStep from './steps/WelcomeStep.svelte';
	import TradingModeStep from './steps/TradingModeStep.svelte';
	import AiBrainStep from './steps/AiBrainStep.svelte';
	import MarketDataStep from './steps/MarketDataStep.svelte';
	import ReadyStep from './steps/ReadyStep.svelte';

	export let settings: Record<string, unknown> = {};
	export let onSettingsUpdated: (() => Promise<void> | void) | undefined = undefined;

	type StepId = 'welcome' | 'trading' | 'ai' | 'data' | 'ready';

	interface StepMeta {
		id: StepId;
		label: string;
		subtitle: string;
		heading: string;
		description: string;
	}

	const STEPS: StepMeta[] = [
		{
			id: 'welcome',
			label: 'Welcome',
			subtitle: 'How Forven works',
			heading: 'Welcome to Forven',
			description: 'A quick, guided walkthrough to get your workspace ready safely.',
		},
		{
			id: 'trading',
			label: 'Trading Mode',
			subtitle: 'Paper or Hyperliquid',
			heading: 'Choose how you want to start',
			description: 'Start risk-free with Paper Trading, or connect a Hyperliquid API wallet now.',
		},
		{
			id: 'ai',
			label: 'AI Brain',
			subtitle: 'Connect a model & pace',
			heading: 'Connect an AI provider',
			description: 'Powers strategy research, code generation, and the in-app assistant.',
		},
		{
			id: 'data',
			label: 'Market Data',
			subtitle: 'Starter price history',
			heading: 'Starter market data',
			description: 'Make sure historical price candles are ready so backtests work right away.',
		},
		{
			id: 'ready',
			label: 'Ready to Go',
			subtitle: 'Summary & first steps',
			heading: 'Review & launch',
			description: 'Check your setup summary and pick where you’d like to start.',
		},
	];

	let tradingMode: 'paper' | 'hyperliquid' = Boolean(settings?.hyperliquid_has_key)
		? 'hyperliquid'
		: 'paper';
	let providers: ForvenAuthProviderStatus[] = [];
	let providersLoading = true;
	let datasetCount = 0;
	let activeDownloadJobs = 0;
	let starterDataStarted = false;
	let finishing = false;
	let skipConfirmOpen = false;
	let maxStepVisited = 0;

	$: if ($wizardStep > maxStepVisited) {
		maxStepVisited = $wizardStep;
	}

	async function refreshProviders(): Promise<void> {
		try {
			const response = await getForvenAuthProviders();
			const list = Array.isArray(response)
				? (response as unknown as ForvenAuthProviderStatus[])
				: (response?.providers ?? []);
			providers = list;
		} catch {
			// Keep prior snapshot on transient error.
		} finally {
			providersLoading = false;
		}
	}

	async function checkInitialData(): Promise<void> {
		try {
			const catalog = await getCatalog({ stream: ['ohlcv'], limit: 1 });
			if (typeof catalog?.total === 'number') {
				datasetCount = catalog.total;
			}
		} catch {
			// Non-fatal — MarketDataStep handles full check and error state.
		}
	}

	onMount(() => {
		void refreshProviders();
		void checkInitialData();
	});

	// Poll providers while the user is on the AI step so an OAuth completion in
	// another tab updates the UI automatically without manual refresh.
	let lastObservedStep = -1;
	let aiPollTimer: ReturnType<typeof setInterval> | null = null;

	function stopAiPoll(): void {
		if (aiPollTimer !== null) {
			clearInterval(aiPollTimer);
			aiPollTimer = null;
		}
	}

	$: if ($wizardStep !== lastObservedStep) {
		lastObservedStep = $wizardStep;
		skipConfirmOpen = false;
		stopAiPoll();
		if (STEPS[$wizardStep]?.id === 'ai') {
			void refreshProviders();
			aiPollTimer = setInterval(() => {
				void refreshProviders();
			}, 3000);
		}
	}

	onDestroy(stopAiPoll);

	$: step = STEPS[$wizardStep] ?? STEPS[0];
	$: hyperliquidConfigured = Boolean(settings?.hyperliquid_has_key);
	$: hyperliquidTestnet = settings?.hyperliquid_testnet !== false;
	$: connectedProviderIds = providers
		.filter((p) => p?.status === 'active')
		.map((p) => String(p.provider));
	$: aiSatisfied = connectedProviderIds.length > 0;
	$: tradingSatisfied = tradingMode === 'paper' || hyperliquidConfigured;
	$: dataSatisfied = datasetCount > 0 || activeDownloadJobs > 0 || starterDataStarted;

	$: stepsForRail = STEPS.map((s, idx): WizardRailStep => {
		let status: WizardRailStep['status'] = 'pending';
		if (s.id === 'welcome') {
			status = maxStepVisited > 0 ? 'complete' : 'pending';
		} else if (s.id === 'trading') {
			status = idx <= maxStepVisited && tradingSatisfied ? 'complete' : 'pending';
		} else if (s.id === 'ai') {
			status = aiSatisfied ? 'complete' : 'recommended';
		} else if (s.id === 'data') {
			status = dataSatisfied ? 'complete' : 'pending';
		} else if (s.id === 'ready') {
			status = 'pending';
		}
		return {
			id: s.id,
			label: s.label,
			subtitle: s.subtitle,
			status,
		};
	});

	$: nextButtonLabel = (() => {
		if (step.id === 'welcome') return 'Get started →';
		if (step.id === 'trading') {
			if (tradingMode === 'paper') return 'Continue with Paper Trading →';
			return hyperliquidConfigured ? 'Continue →' : 'Continue anyway →';
		}
		if (step.id === 'ai') {
			return aiSatisfied ? 'Continue →' : 'Skip for now →';
		}
		if (step.id === 'data') return 'Continue →';
		return 'Finish & open workspace';
	})();

	function goTo(index: number): void {
		if (index < 0 || index >= STEPS.length) return;
		skipConfirmOpen = false;
		wizardStep.set(index);
	}

	function handleDataStatusChange(status: {
		datasetCount: number;
		activeJobs: number;
		starterStarted: boolean;
	}): void {
		datasetCount = status.datasetCount;
		activeDownloadJobs = status.activeJobs;
		starterDataStarted = status.starterStarted;
	}

	async function completeWizard(targetRoute?: string): Promise<void> {
		if (finishing) return;
		finishing = true;
		try {
			if (aiSatisfied) {
				try {
					await reconcileAgentProviders();
				} catch {
					// Best-effort — agents also fall back at runtime.
				}
			}
			await updateSettingsSection('ui', {
				setup_wizard_completed_at: new Date().toISOString(),
			});
			await onSettingsUpdated?.();
		} catch {
			addToast('Could not save setup progress. Please try again.', 'error');
			finishing = false;
			return;
		}
		clearWizardResume();
		closeWizard();
		finishing = false;
		if (targetRoute) {
			try {
				await goto(targetRoute);
			} catch {
				// Non-fatal in test environments without a live router.
			}
		}
	}

	function requestSkipAll(): void {
		if (aiSatisfied) {
			void completeWizard();
			return;
		}
		skipConfirmOpen = true;
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && $wizardOpen && !finishing) {
			closeWizard();
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

{#if $wizardOpen}
	<div
		class="fixed inset-0 z-[100] flex items-center justify-center bg-sc-bg/85 backdrop-blur-[2px] p-3 sm:p-6"
		on:click|self={closeWizard}
		role="presentation"
	>
		<div
			role="dialog"
			aria-modal="true"
			aria-labelledby="wizard-step-heading"
			class="rounded-lg flex flex-col md:flex-row w-full max-w-5xl h-full max-h-[88vh] bg-sc-panel border border-sc-line shadow-2xl overflow-hidden"
		>
			<div class="hidden md:flex">
				<WizardStepRail
					steps={stepsForRail}
					activeIndex={$wizardStep}
					onSelect={goTo}
					onSkipAll={requestSkipAll}
				/>
			</div>

			<!-- Compact mobile step header -->
			<div class="md:hidden px-4 py-2.5 border-b border-sc-line bg-sc-panel2 flex items-center justify-between gap-3">
				<span class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.1em] text-sc-ink3">
					Step {$wizardStep + 1} of {STEPS.length} · {step.label}
				</span>
				<button
					type="button"
					class="text-[11px] text-sc-ink3 hover:text-sc-ink"
					on:click={requestSkipAll}
				>
					Skip setup
				</button>
			</div>

			<section class="flex-1 min-w-0 flex flex-col bg-sc-panel">
				<header class="flex items-start justify-between gap-4 px-6 py-4 border-b border-sc-line">
					<div class="min-w-0">
						<div class="flex items-center gap-2">
							<span class="font-plex-mono text-[11px] text-sc-ink3">
								Step {$wizardStep + 1} of {STEPS.length}
							</span>
						</div>
						<h2 id="wizard-step-heading" class="text-[17px] font-semibold text-sc-ink tracking-[-0.01em] mt-0.5">
							{step.heading}
						</h2>
						<p class="text-xs text-sc-ink2 mt-0.5">{step.description}</p>
					</div>
					<button
						type="button"
						class="w-7 h-7 rounded-md flex items-center justify-center text-sc-ink3 hover:text-sc-ink hover:bg-sc-panel2 transition-colors focus:outline-none"
						on:click={closeWizard}
						aria-label="Close setup guide"
						title="Close for now (resumes where you left off)"
					>
						✕
					</button>
				</header>

				<div class="flex-1 min-h-0 overflow-y-auto px-6 py-5">
					{#if step.id === 'welcome'}
						<WelcomeStep />
					{:else if step.id === 'trading'}
						<TradingModeStep
							{settings}
							bind:mode={tradingMode}
							{onSettingsUpdated}
						/>
					{:else if step.id === 'ai'}
						<AiBrainStep
							{providers}
							loading={providersLoading}
							{settings}
							onProvidersChanged={refreshProviders}
							{onSettingsUpdated}
						/>
					{:else if step.id === 'data'}
						<MarketDataStep onStatusChange={handleDataStatusChange} />
					{:else if step.id === 'ready'}
						<ReadyStep
							{tradingMode}
							{hyperliquidConfigured}
							{hyperliquidTestnet}
							{connectedProviderIds}
							{datasetCount}
							{activeDownloadJobs}
							{starterDataStarted}
							{finishing}
							onJumpToStep={goTo}
							onLaunchRoute={(href) => void completeWizard(href)}
						/>
					{/if}
				</div>

				{#if skipConfirmOpen}
					<div
						class="px-6 py-3 border-t border-amber-500/30 bg-amber-500/[0.06] flex flex-wrap items-center justify-between gap-3"
						role="region"
						aria-label="Confirm skipping setup"
						data-testid="wizard-skip-confirm"
					>
						<p class="text-xs text-amber-200/90">
							Skip without connecting an AI provider? You can still explore and run manual backtests, and connect an AI provider anytime in <span class="font-medium text-sc-ink">Agents → Setup</span>.
						</p>
						<div class="flex items-center gap-2 flex-shrink-0">
							<button
								type="button"
								class="terminal-button text-[12px]"
								on:click={() => {
									skipConfirmOpen = false;
									goTo(2);
								}}
							>
								Connect AI first
							</button>
							<button
								type="button"
								class="terminal-button-primary text-[12px]"
								disabled={finishing}
								data-testid="wizard-skip-confirm-yes"
								on:click={() => void completeWizard()}
							>
								{finishing ? 'Closing…' : 'Yes, skip for now'}
							</button>
						</div>
					</div>
				{/if}

				<footer class="flex items-center justify-between gap-3 px-6 py-3.5 border-t border-sc-line bg-sc-panel">
					<button
						type="button"
						class="terminal-button text-[12px]"
						disabled={$wizardStep === 0 || finishing}
						on:click={() => goTo($wizardStep - 1)}
					>
						← Back
					</button>

					<div class="flex items-center gap-2">
						{#if step.id === 'ready'}
							<button
								type="button"
								class="terminal-button-primary text-[12px]"
								disabled={finishing}
								data-testid="wizard-finish-btn"
								on:click={() => void completeWizard()}
							>
								{finishing ? 'Finishing…' : nextButtonLabel}
							</button>
						{:else}
							<button
								type="button"
								class="terminal-button-primary text-[12px]"
								disabled={finishing}
								data-testid="wizard-next-btn"
								on:click={() => goTo($wizardStep + 1)}
							>
								{nextButtonLabel}
							</button>
						{/if}
					</div>
				</footer>
			</section>
		</div>
	</div>
{/if}
