<script lang="ts">
	import { onMount } from 'svelte';
	import SettingsSearch from '$lib/components/settings/shell/SettingsSearch.svelte';
	import { getSettingsAuditLog, getForvenDashboard, type SettingsAuditEntry } from '$lib/api';
	import { openWizard } from '$lib/stores/setupWizard';

	export let settings: Record<string, unknown> = {};
	// Optional: parent shell may already have fetched dashboard; accept it as a prop.
	// If absent, we'll fetch on mount.
	export let dashboard: Record<string, unknown> | null = null;

	let auditLog: SettingsAuditEntry[] = [];
	let auditLoaded = false;

	onMount(async () => {
		try {
			auditLog = await getSettingsAuditLog(5);
		} catch {
			auditLog = [];
		} finally {
			auditLoaded = true;
		}

		if (!dashboard) {
			try {
				dashboard = (await getForvenDashboard()) as Record<string, unknown>;
			} catch {
				dashboard = null;
			}
		}
	});

	function jumpTo(area: string, id?: string): void {
		if (typeof window === 'undefined') return;
		window.location.hash = id ? `#${area}/${id}` : `#${area}`;
	}

	function str(v: unknown): string {
		if (v === null || v === undefined) return '—';
		if (typeof v === 'string') return v;
		if (typeof v === 'number' || typeof v === 'boolean') return String(v);
		try {
			return JSON.stringify(v);
		} catch {
			return String(v);
		}
	}

	function fmtWhen(iso: string): string {
		try {
			const d = new Date(iso);
			if (Number.isNaN(d.getTime())) return iso;
			return d.toLocaleString();
		} catch {
			return iso;
		}
	}

	// ---- Daily-control tiles ----
	$: systemStatus = (dashboard && (dashboard as any).status) as string | undefined;
	$: killSwitchActive = Boolean(
		dashboard && (dashboard as any).risk && (dashboard as any).risk.kill_switch_active,
	);
	$: onTestnet = settings.hyperliquid_testnet !== false;
	$: mainnetArmed = Boolean(settings.mainnet_armed);
	$: realMoney = !onTestnet && mainnetArmed;
	$: liveStrategies = (dashboard && typeof (dashboard as any).live_strategy_count === 'number'
		? (dashboard as any).live_strategy_count
		: null) as number | null;
	$: systemMode = (dashboard && (dashboard as any).system_mode) as string | undefined;
	const SYSTEM_MODE_LABELS: Record<string, string> = { manual: 'Manual', semi_auto: 'Semi-auto', auto: 'Auto' };

	// ---- Needs-config rules ----
	type Issue = { key: string; label: string; area: string; id?: string };

	$: needsConfig = ((): Issue[] => {
		const issues: Issue[] = [];

		const exchange = (settings.exchange as string | undefined) ?? '';
		if (exchange === 'hyperliquid') {
			const wallet = settings.hyperliquid_wallet as string | undefined;
			const apiAddr = settings.hyperliquid_api_address as string | undefined;
			const hasKey = settings.hyperliquid_has_key as boolean | undefined;
			if (!wallet && !apiAddr && !hasKey) {
				issues.push({
					key: 'hl-creds',
					label: 'Hyperliquid is selected but no credentials are configured.',
					area: 'hyperliquid',
					id: 'hyperliquid.actual_wallet_address',
				});
			}
		}

		// No "Discord not configured" rule: notifications reach the app without
		// Discord (pop-ups, badges, the Diagnostics inbox), so an unconnected
		// Discord is a normal state, not a setup gap. The Notifications section
		// says so where the Discord switches are.

		// Note: "unconfigured OAuth provider" rule intentionally omitted. The
		// settings manifest doesn't expose a stable "enabled but not connected"
		// signal we can read off the flat settings blob. Task 20's shell surfaces
		// this via the auth-providers API once that wires up.

		return issues;
	})();
</script>

<div class="space-y-6">
	<div class="flex">
		<button
			type="button"
			on:click={openWizard}
			class="terminal-button text-[12px]"
		>
			Open setup wizard
		</button>
	</div>

	<!-- Daily-control tiles -->
	<div class="rounded-md grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-px border border-sc-line bg-sc-raise">
		<button
			type="button"
			aria-label="Open system settings (status: {systemStatus ?? 'unknown'})"
			on:click={() => jumpTo('system')}
			class="bg-sc-panel px-4 py-3 text-left hover:bg-sc-panel2 transition-colors"
		>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">System</div>
			<div class="mt-1 text-lg font-bold text-sc-ink">
				{systemStatus ?? (dashboard ? 'UNKNOWN' : 'Loading…')}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">Overall runtime state.</div>
		</button>

		<button
			type="button"
			aria-label="Open real-money status (network: {onTestnet ? 'testnet' : 'mainnet'})"
			on:click={() => jumpTo('trading')}
			class="bg-sc-panel px-4 py-3 text-left hover:bg-sc-panel2 transition-colors"
		>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Real money</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={realMoney} class:text-sc-ink={!realMoney} data-testid="home-network">
				{onTestnet ? 'Testnet' : 'Mainnet'}{realMoney && liveStrategies != null ? ` · ${liveStrategies} live` : ''}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">
				{#if onTestnet}Orders go to Hyperliquid testnet.{:else}Mainnet orders {mainnetArmed ? 'are armed (real money)' : 'are refused until armed'}.{/if}
			</div>
		</button>

		<a
			href="/risk"
			aria-label="Open the Risk page — kill switch ({killSwitchActive ? 'active' : 'inactive'})"
			class="block bg-sc-panel px-4 py-3 text-left hover:bg-sc-panel2 transition-colors"
		>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Kill Switch</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={killSwitchActive} class:text-emerald-400={!killSwitchActive}>
				{killSwitchActive ? 'TRIPPED' : 'Armed'}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">Emergency halt state.</div>
		</a>

		<button
			type="button"
			aria-label="Open automation settings (system mode: {systemMode ?? 'unknown'})"
			on:click={() => jumpTo('automation')}
			class="bg-sc-panel px-4 py-3 text-left hover:bg-sc-panel2 transition-colors"
		>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Automation</div>
			<div class="mt-1 text-lg font-bold text-sc-ink">
				{systemMode ? SYSTEM_MODE_LABELS[systemMode] ?? systemMode : dashboard ? 'Unknown' : 'Loading…'}
			</div>
			<div class="mt-1 text-xs text-sc-ink3">How much runs on its own.</div>
		</button>
	</div>

	<!-- Search -->
	<div class="terminal-card p-4">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 mb-2">Jump to a setting</div>
		<SettingsSearch />
	</div>

	<!-- Needs configuration -->
	<div class="terminal-card">
		<div class="flex items-baseline justify-between border-b border-sc-line px-4 py-2">
			<h3 class="text-[13px] font-semibold text-sc-ink2">Needs configuration</h3>
			<span class="text-xs text-sc-ink3">{needsConfig.length} issue{needsConfig.length === 1 ? '' : 's'}</span>
		</div>
		<div class="p-4">
			{#if needsConfig.length === 0}
				<p class="text-xs text-sc-ink3">Nothing to configure. You're good to go.</p>
			{:else}
				<ul class="space-y-2">
					{#each needsConfig as issue (issue.key)}
						<li>
							<button
								type="button"
								on:click={() => jumpTo(issue.area, issue.id)}
								class="rounded-md w-full text-left px-3 py-2 border border-yellow-900 bg-yellow-500/5 hover:border-yellow-700 text-[12px] text-yellow-400 transition-colors"
							>
								{issue.label}
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>

	<!-- Recently changed -->
	<div class="terminal-card">
		<div class="flex items-baseline justify-between border-b border-sc-line px-4 py-2">
			<h3 class="text-[13px] font-semibold text-sc-ink2">Recently changed</h3>
			<span class="text-xs text-sc-ink3">last {auditLog.length} change{auditLog.length === 1 ? '' : 's'}</span>
		</div>
		<div class="p-4">
			{#if !auditLoaded}
				<p class="text-xs text-sc-ink3">Loading…</p>
			{:else if auditLog.length === 0}
				<p class="text-xs text-sc-ink3">No recent setting changes.</p>
			{:else}
				<ul class="divide-y divide-sc-line">
					{#each auditLog as entry (entry.at + entry.id)}
						<li class="py-2 text-sm">
							<div class="flex flex-wrap items-baseline gap-x-2">
								<span class="text-sc-ink">{entry.id}</span>
								<span class="text-sc-ink3 text-xs">{fmtWhen(entry.at)}</span>
								<span class="text-sc-ink3 text-xs">by {entry.actor}</span>
							</div>
							<div class="mt-0.5 text-xs text-sc-ink2">
								<span class="text-sc-ink3">{str(entry.from)}</span>
								<span class="mx-1 text-sc-ink3">→</span>
								<span class="text-sc-ink">{str(entry.to)}</span>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>
</div>
