<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { forvenWsConnected } from '$lib/stores/forvenWebSocket';
	import {
		forvenDashboard,
		forvenRisk,
		forvenSentiment,
		forvenRegime,
	} from '$lib/stores/forven';
	import { simulationActive, simulationPhase, simulationTime } from '$lib/stores/simulation';
	import {
		getSystemStatus,
		setSystemMode,
		resetTradingHalt,
		setForvenExecutionMode,
		triggerForvenEmergencyHalt,
		type PausedManualCounts,
		type SystemMode,
	} from '$lib/api';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import LivePnlTicker from '$lib/components/forven/LivePnlTicker.svelte';
	import RegimeChip from '$lib/components/regime/RegimeChip.svelte';

	type ModalAction = 'mode-toggle' | 'emergency-halt' | 'trading-reset' | 'system-mode-change' | null;
	type ExecutionMode = 'paper' | 'live';

	let systemPaused = false;
	let generationPaused = false;
	let systemMode: SystemMode = 'manual';
	let systemModeTarget: SystemMode = 'manual';
	let wsConnected = false;
	let realtime: RealtimeRefreshController | null = null;
	let actionError = '';
	let actionBusy = false;
	let modalOpen = false;
	let modalAction: ModalAction = null;
	// FE-05: focus target when the confirm dialog opens.
	let cancelButton: HTMLButtonElement | null = null;
	let executionMode: ExecutionMode = 'paper';
	let modeSwitchTarget: ExecutionMode = 'live';
	let pausedManualCounts: PausedManualCounts = emptyPausedManualCounts();

	const SYSTEM_MODES: { value: SystemMode; label: string; short: string }[] = [
		{ value: 'manual', label: 'Manual', short: 'MANUAL' },
		{ value: 'semi_auto', label: 'Semi', short: 'SEMI' },
		{ value: 'auto', label: 'Auto', short: 'AUTO' },
	];

	function normalizeSystemMode(mode: unknown): SystemMode {
		if (mode === 'auto' || mode === 'semi_auto' || mode === 'manual') return mode;
		return 'manual';
	}

	function systemModeDescription(mode: SystemMode): string {
		if (mode === 'manual') {
			return 'Manual mode: all autonomous background work freezes. Scheduled jobs stop, queued autonomous tasks pause, and only direct operator actions can run until you leave manual mode.';
		}
		if (mode === 'semi_auto') {
			return 'Semi-automatic mode: agents will not write new ideas on their own. Ideas you submit are built into strategies and fully evaluated by the Gauntlet and lifecycle machinery. Trading stays active.';
		}
		return 'Fully automatic mode: agents write new ideas and build strategies from them within the daily budget, and the pipeline evaluates and promotes them. Live trading is active.';
	}

	function emptyPausedManualCounts(): PausedManualCounts {
		return { agent_tasks: 0, brain_tasks: 0, total: 0 };
	}

	function normalizePausedManualCounts(value: unknown): PausedManualCounts {
		if (!value || typeof value !== 'object') return emptyPausedManualCounts();
		const raw = value as Record<string, unknown>;
		const agentTasks = Number(raw.agent_tasks ?? 0);
		const brainTasks = Number(raw.brain_tasks ?? 0);
		const total = Number(raw.total ?? agentTasks + brainTasks);
		return {
			agent_tasks: Number.isFinite(agentTasks) ? agentTasks : 0,
			brain_tasks: Number.isFinite(brainTasks) ? brainTasks : 0,
			total: Number.isFinite(total) ? total : 0,
		};
	}

	function pausedManualBannerText(counts: PausedManualCounts): string {
		if (counts.total <= 0) {
			return 'Manual mode - all background work frozen. Only direct operator actions run.';
		}
		const pausedLabel = counts.total === 1 ? '1 queued task paused' : `${counts.total} queued tasks paused`;
		return `Manual mode - all background work frozen. ${pausedLabel}.`;
	}

	function normalizeMode(mode: unknown): ExecutionMode {
		return mode === 'live' ? 'live' : 'paper';
	}

	$: executionMode = normalizeMode($forvenDashboard?.execution_mode);
	// execution_mode is only a global label: strategies at the live stage and
	// live-armed bots are what send real orders, so count those instead.
	$: liveArmed = ($forvenDashboard?.live_strategy_count ?? 0) + ($forvenDashboard?.live_bot_count ?? 0);
	$: hlNetwork = (() => {
		if (!$forvenDashboard) return null;
		const raw = ($forvenDashboard.account?.network || '').toString().trim().toLowerCase();
		if (raw === 'mainnet' || raw === 'testnet') return raw;
		return executionMode === 'live' ? 'mainnet' : 'testnet';
	})();
	$: daemonStatus = $forvenDashboard ? ($forvenDashboard.daemon_running ? 'ACTIVE' : 'OFFLINE') : 'SYNCING';
	$: sentimentScore = typeof ($forvenSentiment as Record<string, unknown> | null)?.composite === 'number' ? ($forvenSentiment as Record<string, number>).composite : null;
	$: tradingAllowed = $forvenDashboard?.trading_allowed ?? true;
	$: tradingReason = ($forvenDashboard as Record<string, unknown> | null)?.trading_reason as string || 'OK';
	$: killSwitchActive = Boolean($forvenRisk?.kill_switch_active || ($forvenDashboard?.risk as Record<string, unknown> | undefined)?.kill_switch_active);
	$: modeSwitchTarget = executionMode === 'paper' ? 'live' : 'paper';
	$: wsConnected = $forvenWsConnected;
	$: simTimeFormatted = $simulationTime ? new Date($simulationTime).toLocaleString() : '--';
	$: simPhase = ($simulationPhase || 'idle').toUpperCase();

	$: modalTitle = getModalTitle(modalAction, modeSwitchTarget);
	$: modalMessage = getModalMessage(modalAction, executionMode, modeSwitchTarget);
	$: modalDanger =
		modalAction === 'emergency-halt'
		|| modalAction === 'trading-reset'
		|| (modalAction === 'mode-toggle' && modeSwitchTarget === 'live')
		|| (modalAction === 'system-mode-change' && systemModeTarget === 'auto');

	function getModalTitle(action: ModalAction, targetMode: 'paper' | 'live'): string {
		if (action === 'system-mode-change') {
			const target = SYSTEM_MODES.find((m) => m.value === systemModeTarget);
			return `Switch to ${target?.label ?? systemModeTarget} mode`;
		}
		if (action === 'emergency-halt') return 'Emergency Halt';
		if (action === 'trading-reset') return 'Reset Trading Halt';
		if (action === 'mode-toggle') return `Switch To ${targetMode.toUpperCase()}`;
		return '';
	}

	function getModalMessage(action: ModalAction, currentMode: 'paper' | 'live', targetMode: 'paper' | 'live'): string {
		if (action === 'system-mode-change') {
			return systemModeDescription(systemModeTarget);
		}
		if (action === 'emergency-halt') {
			return 'This will immediately close all open positions via market orders.';
		}
		if (action === 'trading-reset') {
			return 'This clears the current trading halt, resets risk halt flags, and resumes the runtime gate. Only continue if the trigger cause is resolved.';
		}
		if (action === 'mode-toggle') {
			if (targetMode === 'live') {
				return `Switch from ${currentMode.toUpperCase()} to LIVE mode? Live mode can send real orders.`;
			}
			return `Switch from ${currentMode.toUpperCase()} to PAPER mode?`;
		}
		return '';
	}

	async function loadSystemStatus() {
		try {
			const result = await getSystemStatus();
			systemPaused = result.paused;
			generationPaused = result.generation_paused ?? false;
			systemMode = normalizeSystemMode(result.system_mode);
			pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
		} catch {
			// System status unavailable - keep last known state
		}
	}

	function openModal(action: ModalAction) {
		actionError = '';
		modalAction = action;
		modalOpen = true;
		// FE-05: land focus on Cancel, not on whatever the page had focused —
		// otherwise a stray Enter after opening the Emergency Halt confirm
		// re-triggers the button behind the dialog.
		void tick().then(() => cancelButton?.focus());
	}

	// FE-05: Escape must cancel. The dialog previously had no keyboard exit at
	// all, and its scrim did not even take clicks.
	function handleModalKeydown(event: KeyboardEvent) {
		if (!modalOpen) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			closeModal();
		}
	}

	function requestSystemMode(target: SystemMode) {
		if (target === systemMode) return;
		systemModeTarget = target;
		openModal('system-mode-change');
	}

	function closeModal(force = false) {
		modalOpen = false;
		modalAction = null;
		if (force || !actionBusy) {
			actionError = '';
		}
	}

	async function confirmModal() {
		if (!modalAction) return;
		actionBusy = true;
		actionError = '';
		try {
			if (modalAction === 'system-mode-change') {
				const result = await setSystemMode(systemModeTarget);
				systemMode = normalizeSystemMode(result.system_mode);
				pausedManualCounts = normalizePausedManualCounts(result.paused_manual_counts);
				closeModal(true);
				void loadSystemStatus();
				return;
			} else if (modalAction === 'mode-toggle') {
				await setForvenExecutionMode(modeSwitchTarget);
			} else if (modalAction === 'emergency-halt') {
				await triggerForvenEmergencyHalt();
			} else if (modalAction === 'trading-reset') {
				await resetTradingHalt();
			}
			await loadSystemStatus();
			closeModal(true);
		} catch (err) {
			actionError = err instanceof Error ? err.message : 'Action failed';
		} finally {
			actionBusy = false;
		}
	}

	function getSentimentClass(score: number | null): string {
		if (score === null) return 'border-[#333] text-[#666]';
		if (score >= 60) return 'border-emerald-900 text-emerald-400';
		if (score >= 40) return 'border-yellow-900 text-yellow-400';
		return 'border-red-900 text-red-400';
	}

	onMount(() => {
		void loadSystemStatus();
		realtime = createRealtimeRefresh(loadSystemStatus, {
			fallbackMs: 30_000,
			wsDebounceMs: 1200,
			wsEvents: ['kill_switch_activated', 'kill_switch_cleared', 'strategy_transition', 'trade'],
		});
		realtime.start();
	});

	onDestroy(() => {
		realtime?.stop();
		realtime = null;
	});
</script>

<!-- FE-05: Escape cancels the confirm dialog. Must live at the top level —
     `<svelte:window>` cannot be nested inside a block. -->
<svelte:window on:keydown={handleModalKeydown} />

{#if systemMode === 'manual'}
	<div class="bg-yellow-500/5 border-b border-yellow-900 px-4 py-1 text-[11px] uppercase tracking-wider text-yellow-400 font-bold flex flex-wrap items-center justify-between gap-2">
		<span>{pausedManualBannerText(pausedManualCounts)}</span>
		<button
			class="px-2 py-0.5 border border-yellow-900 text-[10px] hover:bg-yellow-500/10 transition-colors"
			on:click={() => requestSystemMode('semi_auto')}
		>
			Switch to Semi
		</button>
	</div>
{:else if systemMode === 'semi_auto'}
	<div class="bg-white/5 border-b border-[#333] px-4 py-1 text-[11px] uppercase tracking-wider text-[#999] font-bold flex flex-wrap items-center justify-between gap-2">
		<span>Semi mode - autonomous generation off; ideas you submit still run through the pipeline.</span>
		<button
			class="px-2 py-0.5 border border-[#555] text-[10px] hover:bg-white hover:text-black transition-colors"
			on:click={() => requestSystemMode('auto')}
		>
			Switch to Auto
		</button>
	</div>
{/if}
{#if !wsConnected}
	<div class="bg-red-500/5 border-b border-red-900 px-4 py-1 text-[11px] uppercase tracking-wider text-red-400 font-bold">
		Connection lost. Reconnecting to Forven websocket...
	</div>
{/if}
{#if $simulationActive}
	<div class="bg-white/5 border-b border-[#333] px-4 py-1 text-[11px] uppercase tracking-wider text-white font-bold flex flex-wrap items-center justify-between gap-2">
		<span class="flex items-center gap-2">
			<span class="w-2 h-2 bg-white rounded-full animate-pulse"></span>
			Simulation Active &mdash; Virtual Time: {simTimeFormatted} &mdash; {simPhase}
		</span>
		<a href="/lab" class="px-2 py-0.5 border border-[#555] text-[10px] hover:bg-white hover:text-black transition-colors">
			Open Strategies
		</a>
	</div>
{/if}
<header class="border-b border-[#222] bg-[#050505] px-4 py-2 flex flex-wrap items-center gap-3 text-[11px] uppercase tracking-wider">
	<div class="flex min-w-0 flex-wrap items-center gap-3 lg:gap-4">
		<div class="flex items-center gap-2 whitespace-nowrap">
			<span class={`w-2 h-2 rounded-full ${daemonStatus === 'OFFLINE' ? 'bg-red-500' : daemonStatus === 'SYNCING' ? 'bg-yellow-400' : 'bg-emerald-400'}`}></span>
			<span class="text-[#888]">Daemon {daemonStatus}</span>
		</div>
		<div class="flex items-center gap-2 whitespace-nowrap">
			<span class={`w-2 h-2 rounded-full ${wsConnected ? 'bg-emerald-400' : 'bg-red-500'}`}></span>
			<span class={wsConnected ? 'text-emerald-400' : 'text-red-400'}>{wsConnected ? 'WS Live' : 'WS Offline'}</span>
		</div>
	</div>

	<div class="flex min-w-0 flex-1 flex-wrap items-center justify-center gap-2">
		<LivePnlTicker />

		{#each ['BTC', 'ETH', 'SOL'] as regimeAsset (regimeAsset)}
			{#if ($forvenRegime as Record<string, Record<string, unknown>> | null)?.[regimeAsset]}
				<RegimeChip
					asset={regimeAsset}
					regime={($forvenRegime as Record<string, never>)[regimeAsset]}
				/>
			{/if}
		{/each}

		<!-- Display-only, with no in-app switch to live. Real orders come from
		     strategies promoted to the live stage and live-armed bots, so the badge
		     counts those rather than reading the global execution_mode label. -->
		{#if liveArmed > 0}
			<a
				href="/live-trades"
				class="px-2 py-1 border whitespace-nowrap font-bold border-red-900 text-red-400 bg-red-500/10"
				title={`${liveArmed} live ${liveArmed === 1 ? 'strategy/bot sends' : 'strategies/bots send'} real orders`}
			>
				Live ×{liveArmed}
			</a>
		{:else}
			<span
				class={`px-2 py-1 border whitespace-nowrap ${executionMode === 'live' ? 'border-red-900 text-red-400 bg-red-500/10' : 'border-[#333] text-[#888]'}`}
				title={`Execution mode: ${executionMode.toUpperCase()} — no strategies or bots are trading real money`}
			>
				Mode: {executionMode.toUpperCase()}
			</span>
		{/if}

		{#if hlNetwork}
			<span
				class={`px-2 py-1 border whitespace-nowrap font-bold ${hlNetwork === 'mainnet' ? 'border-red-900 text-red-400 bg-red-500/10' : 'border-emerald-900 text-emerald-400 bg-emerald-500/10'}`}
				title={hlNetwork === 'mainnet' ? 'HyperLiquid MAINNET — orders use real funds' : 'HyperLiquid testnet — no real funds at risk'}
			>
				{hlNetwork === 'mainnet' ? 'MAINNET' : 'TESTNET'}
			</span>
		{/if}

		<span class={`px-2 py-1 border whitespace-nowrap ${getSentimentClass(sentimentScore)}`}>
			F&G: {sentimentScore !== null ? Math.round(sentimentScore) : '--'}
		</span>

		<a href="/risk" class={`px-2 py-1 border whitespace-nowrap transition-colors ${tradingAllowed ? 'border-emerald-900 text-emerald-400' : 'border-red-900 text-red-400'}`}>
			{tradingAllowed ? 'Trading Allowed' : 'Trading Halted'}
		</a>

		{#if killSwitchActive}
			<span class="px-2 py-1 border border-red-900 bg-red-500/10 text-red-400 whitespace-nowrap">Kill Switch Active</span>
		{/if}
	</div>

	<div class="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
		<div
			class="inline-flex items-stretch border border-[#333]"
			role="group"
			aria-label="System mode"
			title="System mode - controls whether the system runs autonomously"
		>
			{#each SYSTEM_MODES as option (option.value)}
				<button
					class={`px-2.5 py-1 text-[11px] font-bold transition-colors border-r border-[#333] last:border-r-0 ${
						systemMode === option.value
							? option.value === 'auto'
								? 'bg-red-500/10 text-red-400'
								: option.value === 'semi_auto'
									? 'bg-white/10 text-white'
									: 'bg-yellow-500/10 text-yellow-400'
							: 'text-[#888] hover:bg-[#111] hover:text-white'
					}`}
					on:click={() => requestSystemMode(option.value)}
					aria-pressed={systemMode === option.value}
				>
					{option.short}
				</button>
			{/each}
		</div>
		{#if !tradingAllowed}
			<button class="px-2 py-1 border border-[#333] text-[#888] hover:bg-[#111] hover:text-white whitespace-nowrap transition-colors" on:click={() => openModal('trading-reset')}>
				Reset Halt
			</button>
		{/if}
		<button
			class="px-2 py-1 border border-red-900 text-red-400 hover:bg-red-500 hover:border-red-500 hover:text-white whitespace-nowrap transition-colors"
			on:click={() => openModal('emergency-halt')}
		>
			Emergency Halt
		</button>
	</div>
</header>

{#if !tradingAllowed}
	<div class="border-b border-red-900 bg-red-500/5 px-4 py-1 text-[10px] uppercase tracking-wider text-red-400">
		Trading halted: {tradingReason}
	</div>
{/if}

<!-- FE-05: this confirm gates Emergency Halt (closes every open position at
     market) and the paper->live mode switch, but its scrim carried
     `pointer-events-none` — so it was a picture of a modal, not a modal. Clicks
     landed on the controls BEHIND it, including the very buttons it was asking
     about, and it trapped neither focus nor Escape. Scrim now takes pointer
     events, the dialog is announced as modal, Escape cancels, and focus starts on
     Cancel so a stray Enter can never confirm a destructive action. -->
{#if modalOpen && modalAction}
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<div class="fixed inset-0 z-[10010] bg-black/80 flex items-center justify-center p-4" on:click={() => closeModal()}>
		<div
			class="w-full max-w-md border border-[#222] bg-[#050505] p-4 space-y-3"
			role="dialog"
			aria-modal="true"
			aria-labelledby="global-control-modal-title"
			tabindex="-1"
			on:click|stopPropagation
		>
			<h3 id="global-control-modal-title" class={`text-sm font-bold uppercase tracking-wider ${modalDanger ? 'text-red-400' : 'text-white'}`}>{modalTitle}</h3>
			<p class="text-xs text-[#888] leading-relaxed">{modalMessage}</p>
			{#if actionError}
				<div class="text-xs border border-red-900 bg-red-500/5 text-red-400 px-2 py-1">{actionError}</div>
			{/if}
			<div class="flex justify-end gap-2 pt-1">
				<button
					bind:this={cancelButton}
					class="px-3 py-1.5 text-xs border border-[#333] text-[#888] hover:text-white hover:border-[#555] transition-colors"
					on:click={() => closeModal()}
				>
					Cancel
				</button>
				<button
					class={`px-3 py-1.5 text-xs border transition-colors ${modalDanger ? 'border-red-900 text-red-400 hover:bg-red-500 hover:border-red-500 hover:text-white' : 'border-white text-white hover:bg-white hover:text-black'}`}
					on:click={confirmModal}
					disabled={actionBusy}
				>
					{actionBusy ? 'Working...' : 'Confirm'}
				</button>
			</div>
		</div>
	</div>
{/if}
