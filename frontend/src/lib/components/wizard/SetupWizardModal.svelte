<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { wizardOpen, wizardStep, closeWizard, clearWizardResume } from '$lib/stores/setupWizard';
	import { getForvenAuthProviders, reconcileAgentProviders, updateSettingsSection } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { pendingValues } from '$lib/settings/dirty';
	import SettingsTrading from '$lib/components/settings/sections/SettingsTrading.svelte';
	import SettingsHyperliquid from '$lib/components/settings/sections/SettingsHyperliquid.svelte';
	import SettingsAgents from '$lib/components/settings/sections/SettingsAgents.svelte';
	import SettingsNotifications from '$lib/components/settings/sections/SettingsNotifications.svelte';
	import WizardStepRail from './WizardStepRail.svelte';

	export let settings: Record<string, unknown>;

	type StepId = 'welcome' | 'trading' | 'ai' | 'notifications' | 'done';
	interface Step {
		id: StepId;
		label: string;
		critical: boolean;
		description: string;
	}

	const STEPS: Step[] = [
		{ id: 'welcome', label: 'Welcome', critical: false, description: 'Get Forven set up.' },
		{ id: 'trading', label: 'Trading basics', critical: true, description: 'Pick an exchange and paste API credentials.' },
		{ id: 'ai', label: 'AI providers', critical: true, description: 'Connect at least one provider so agents can run.' },
		{ id: 'notifications', label: 'Notifications', critical: false, description: 'Discord alerts (optional).' },
		{ id: 'done', label: 'Done', critical: false, description: 'Review and finish.' },
	];

	// The HL credentials subsection lives in the dedicated HyperLiquid settings
	// area now; the wizard's trading step renders it right after the exchange
	// picker via SettingsHyperliquid below.
	const TRADING_SUBS_WIZARD = ['trading-exchange'];
	const HL_SUBS_WIZARD = ['hl-credentials'];

	let providersActive = false;

	async function refreshProviders() {
		try {
			const response = await getForvenAuthProviders();
			const providers = response?.providers ?? [];
			providersActive = providers.some((p) => p?.status === 'active');
		} catch {
			providersActive = false;
		}
	}

	onMount(refreshProviders);

	// Re-poll providers when the user lands on the AI step, then poll every 3s
	// while they stay there — so an OAuth completion in another tab flips the
	// green checkmark without requiring manual step navigation.
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
		stopAiPoll();
		if (STEPS[$wizardStep]?.id === 'ai') {
			refreshProviders();
			aiPollTimer = setInterval(refreshProviders, 3000);
		}
	}

	onDestroy(stopAiPoll);

	$: step = STEPS[$wizardStep] ?? STEPS[0];
	$: tradingSatisfied = Boolean(
		settings?.hyperliquid_has_key && settings?.exchange === 'hyperliquid'
	);
	$: aiSatisfied = providersActive;
	// Notifications is optional, but once the user has entered either a bot
	// token or webhook URL (saved OR still-pending in the save bar), show a
	// green checkmark in the rail so progress is visible before they save.
	$: notificationsSatisfied = Boolean(
		settings?.discord_bot_token_configured ||
		settings?.discord_webhook_configured ||
		$pendingValues['notifications.discord_bot_token'] ||
		$pendingValues['notifications.discord_webhook_url']
	);
	$: stepsForRail = STEPS.map((s) => ({
		id: s.id,
		label: s.label,
		critical: s.critical,
		satisfied: s.id === 'trading' ? tradingSatisfied
			: s.id === 'ai' ? aiSatisfied
			: s.id === 'notifications' ? notificationsSatisfied
			: false,
	}));

	function isSatisfied(s: Step): boolean {
		if (s.id === 'trading') return tradingSatisfied;
		if (s.id === 'ai') return aiSatisfied;
		if (s.id === 'notifications') return notificationsSatisfied;
		return true;
	}

	function skipMessage(s: Step): string {
		if (s.id === 'trading') {
			return "Skip Trading basics? Forven won't be able to place paper or live orders until you connect an exchange in Settings.";
		}
		return "Skip AI providers? Forven won't be able to run research, propose strategies, or chat until you connect one in Settings.";
	}

	function goTo(i: number) {
		if (i < 0 || i >= STEPS.length) return;
		const current = STEPS[$wizardStep];
		if (current.critical && !isSatisfied(current) && i > $wizardStep) {
			if (!window.confirm(skipMessage(current))) return;
		}
		wizardStep.set(i);
	}

	async function finish() {
		const unsatisfied = STEPS.filter((s) => s.critical && !isSatisfied(s));
		if (unsatisfied.length > 0) {
			const labels = unsatisfied.map((s) => s.label).join(', ');
			const ok = window.confirm(
				`You haven't finished: ${labels}. Forven won't run correctly until these are set up. Finish anyway?`
			);
			if (!ok) return;
		}
		// Make the provider the operator just connected the agents' default, so a
		// fresh install isn't left with every agent pinned to the seed default
		// (openai) it has no key for. Best-effort: agents also fall back at runtime.
		try {
			await reconcileAgentProviders();
		} catch {
			// Non-fatal — don't block finishing setup.
		}
		try {
			await updateSettingsSection('ui', {
				setup_wizard_completed_at: new Date().toISOString(),
			});
		} catch (err) {
			addToast('Could not save wizard completion. Please try again.', 'error');
			return;
		}
		clearWizardResume();
		closeWizard();
	}

	async function skipAll() {
		const unsatisfied = STEPS.filter((s) => s.critical && !isSatisfied(s));
		if (unsatisfied.length > 0) {
			const labels = unsatisfied.map((s) => s.label).join(', ');
			const ok = window.confirm(
				`Skip setup? You haven't finished: ${labels}. Forven won't run correctly until these are set up.`
			);
			if (!ok) return;
		}
		try {
			await updateSettingsSection('ui', {
				setup_wizard_completed_at: new Date().toISOString(),
			});
		} catch (err) {
			addToast('Could not save wizard completion. Please try again.', 'error');
			return;
		}
		clearWizardResume();
		closeWizard();
	}
</script>

{#if $wizardOpen}
	<div class="fixed inset-0 z-[100] flex items-stretch justify-center bg-black/80 p-6"
		on:click|self={closeWizard}
		role="presentation">
		<div class="flex w-full max-w-5xl h-full max-h-[90vh] bg-[#050505] border border-[#222] overflow-hidden">
			<WizardStepRail
				steps={stepsForRail}
				activeIndex={$wizardStep}
				onSelect={goTo}
				onSkipAll={skipAll}
			/>
			<section class="flex-1 min-w-0 flex flex-col">
				<header class="flex items-start justify-between px-6 py-4 border-b border-[#222]">
					<div>
						<h2 class="text-lg font-bold uppercase tracking-widest text-white">{step.label}</h2>
						<p class="text-xs text-[#666] mt-1">{step.description}</p>
					</div>
					<button type="button"
						class="text-[#666] hover:text-white focus:outline-none"
						on:click={closeWizard}
						aria-label="Close wizard">✕</button>
				</header>

				{#if step.critical && !isSatisfied(step)}
					<div class="px-6 py-3 bg-yellow-500/5 border-b border-yellow-900 text-sm text-yellow-400">
						{#if step.id === 'trading'}
							⚠ Without an exchange API connection, Forven can't place paper or live orders.
						{:else}
							⚠ Without an AI provider, agents can't research, propose strategies, or chat.
						{/if}
					</div>
				{/if}

				<div class="flex-1 min-h-0 overflow-y-auto px-6 py-4">
					{#if step.id === 'welcome'}
						<p class="text-sm text-[#888] leading-relaxed">
							This wizard walks you through the minimum setup to run Forven.
							You can skip anything and change it later in Settings.
						</p>
					{:else if step.id === 'trading'}
						<SettingsTrading
							{settings}
							variant="wizard"
							visibleSubsections={TRADING_SUBS_WIZARD}
						/>
						<div class="mt-4">
							<SettingsHyperliquid
								{settings}
								variant="wizard"
								visibleSubsections={HL_SUBS_WIZARD}
							/>
						</div>
					{:else if step.id === 'ai'}
						<div class="mb-4 border border-[#222] bg-[#111] px-4 py-3 text-sm text-[#888]">
							<p class="font-bold text-white">💡 On a budget? Recommended: Google Gemini → <span class="font-mono">gemini-2.5-flash-lite</span></p>
							<ul class="mt-2 space-y-1 text-xs text-[#888] list-disc pl-5">
								<li>Cheapest model that reliably runs Forven's agents (~$0.10 / $0.40 per 1M input/output tokens) and has a <strong>free tier</strong> to try.</li>
								<li>Supports the tool-calling and large context the agents need.</li>
								<li><strong>Caveat:</strong> it trades some reasoning depth for cost. If strategy quality looks weak, step up to <span class="font-mono">gemini-2.5-flash</span> (still far cheaper than the pro/3.x models).</li>
								<li><strong>Free tiers rate-limit hard</strong> (and can hit a project spend cap). For a continuous research loop, expect to add a small paid budget — see <a href="https://ai.studio/spend" target="_blank" rel="noopener" class="underline">ai.studio/spend</a>.</li>
							</ul>
						</div>
						<SettingsAgents {settings} variant="wizard" />
					{:else if step.id === 'notifications'}
						<SettingsNotifications {settings} />
					{:else if step.id === 'done'}
						<ul class="space-y-2 text-sm">
							{#each STEPS.slice(1, -1) as s (s.id)}
								<li class="flex items-center gap-2">
									{#if s.critical && !isSatisfied(s)}
										<span class="text-yellow-400" aria-hidden="true">△</span>
									{:else if isSatisfied(s)}
										<span class="text-emerald-400" aria-hidden="true">✓</span>
									{:else}
										<span class="text-[#666]" aria-hidden="true">○</span>
									{/if}
									<span>{s.label}</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>

				<footer class="flex items-center justify-between px-6 py-3 border-t border-[#222]">
					<button type="button"
						class="terminal-button text-xs"
						disabled={$wizardStep === 0}
						on:click={() => goTo($wizardStep - 1)}>
						Back
					</button>
					{#if step.id === 'done'}
						<button type="button"
							class="terminal-button-primary text-xs"
							on:click={finish}>
							Finish
						</button>
					{:else}
						<button type="button"
							class="terminal-button-primary text-xs"
							on:click={() => goTo($wizardStep + 1)}>
							Next
						</button>
					{/if}
				</footer>
			</section>
		</div>
	</div>
{/if}
