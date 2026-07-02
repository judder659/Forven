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
	$: executionMode = (dashboard && (dashboard as any).execution_mode) as string | undefined;
	$: killSwitchActive = Boolean(
		dashboard && (dashboard as any).risk && (dashboard as any).risk.kill_switch_active,
	);
	$: tradingMode = (settings.trading_mode as string | undefined) ?? executionMode ?? 'paper';
	$: selfHealing = (settings.self_healing_enabled as boolean | undefined) ?? true;

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
					area: 'trading',
					id: 'hyperliquid.actual_wallet_address',
				});
			}
		}

		const anyNotifyOn = Boolean(
			settings.notify_on_entry ||
				settings.notify_on_exit ||
				settings.notify_daily_summary ||
				settings.notify_health_reports ||
				settings.notify_errors,
		);
		const webhook = (settings.discord_webhook_url as string | undefined) ?? '';
		const botToken = (settings.discord_bot_token as string | undefined) ?? '';
		if (anyNotifyOn && !webhook && !botToken) {
			issues.push({
				key: 'notify-no-transport',
				label: 'Notifications are enabled but no Discord transport is configured.',
				area: 'notifications',
				id: 'notifications.discord_webhook_url',
			});
		}

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
			class="terminal-button text-xs"
		>
			Open setup wizard
		</button>
	</div>

	<!-- Daily-control tiles -->
	<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-px border border-[#222] bg-[#222]">
		<button
			type="button"
			aria-label="Open system settings (status: {systemStatus ?? 'unknown'})"
			on:click={() => jumpTo('system')}
			class="bg-[#050505] px-4 py-3 text-left hover:bg-[#111] transition-colors"
		>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">System</div>
			<div class="mt-1 text-lg font-bold text-white">
				{systemStatus ?? (dashboard ? 'UNKNOWN' : 'Loading…')}
			</div>
			<div class="mt-1 text-xs text-[#666]">Overall runtime state.</div>
		</button>

		<button
			type="button"
			aria-label="Open trading mode settings (current: {tradingMode ?? 'unknown'})"
			on:click={() => jumpTo('trading', 'trading-mode.trading_mode')}
			class="bg-[#050505] px-4 py-3 text-left hover:bg-[#111] transition-colors"
		>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Mode</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={tradingMode === 'live'} class:text-white={tradingMode !== 'live'}>
				{tradingMode === 'live' ? 'Live' : 'Paper'}
			</div>
			<div class="mt-1 text-xs text-[#666]">Paper-trades vs. real orders.</div>
		</button>

		<button
			type="button"
			aria-label="Open system settings — kill switch ({killSwitchActive ? 'active' : 'inactive'})"
			on:click={() => jumpTo('system')}
			class="bg-[#050505] px-4 py-3 text-left hover:bg-[#111] transition-colors"
		>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Kill Switch</div>
			<div class="mt-1 text-lg font-bold" class:text-red-400={killSwitchActive} class:text-emerald-400={!killSwitchActive}>
				{killSwitchActive ? 'TRIPPED' : 'Armed'}
			</div>
			<div class="mt-1 text-xs text-[#666]">Emergency halt state.</div>
		</button>

		<button
			type="button"
			aria-label="Open self-healing settings (current: {selfHealing ? 'enabled' : 'disabled'})"
			on:click={() => jumpTo('system', 'bot-operations.self_healing_enabled')}
			class="bg-[#050505] px-4 py-3 text-left hover:bg-[#111] transition-colors"
		>
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Self-healing</div>
			<div class="mt-1 text-lg font-bold text-white">
				{selfHealing ? 'Enabled' : 'Disabled'}
			</div>
			<div class="mt-1 text-xs text-[#666]">Auto-recover from known errors.</div>
		</button>
	</div>

	<!-- Search -->
	<div class="terminal-card p-4">
		<div class="text-[10px] uppercase tracking-wider text-[#666] mb-2">Jump to a setting</div>
		<SettingsSearch />
	</div>

	<!-- Needs configuration -->
	<div class="terminal-card">
		<div class="flex items-baseline justify-between border-b border-[#1a1a1a] px-4 py-2">
			<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Needs configuration</h3>
			<span class="text-xs text-[#666]">{needsConfig.length} issue{needsConfig.length === 1 ? '' : 's'}</span>
		</div>
		<div class="p-4">
			{#if needsConfig.length === 0}
				<p class="text-xs text-[#666]">Nothing to configure. You're good to go.</p>
			{:else}
				<ul class="space-y-2">
					{#each needsConfig as issue (issue.key)}
						<li>
							<button
								type="button"
								on:click={() => jumpTo(issue.area, issue.id)}
								class="w-full text-left px-3 py-2 border border-yellow-900 bg-yellow-500/5 hover:border-yellow-700 text-xs text-yellow-400 transition-colors"
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
		<div class="flex items-baseline justify-between border-b border-[#1a1a1a] px-4 py-2">
			<h3 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Recently changed</h3>
			<span class="text-xs text-[#666]">last {auditLog.length} change{auditLog.length === 1 ? '' : 's'}</span>
		</div>
		<div class="p-4">
			{#if !auditLoaded}
				<p class="text-xs text-[#666]">Loading…</p>
			{:else if auditLog.length === 0}
				<p class="text-xs text-[#666]">No recent setting changes.</p>
			{:else}
				<ul class="divide-y divide-[#1a1a1a]">
					{#each auditLog as entry (entry.at + entry.id)}
						<li class="py-2 text-sm">
							<div class="flex flex-wrap items-baseline gap-x-2">
								<span class="text-white">{entry.id}</span>
								<span class="text-[#666] text-xs">{fmtWhen(entry.at)}</span>
								<span class="text-[#666] text-xs">by {entry.actor}</span>
							</div>
							<div class="mt-0.5 text-xs text-[#888]">
								<span class="text-[#666]">{str(entry.from)}</span>
								<span class="mx-1 text-[#555]">→</span>
								<span class="text-white">{str(entry.to)}</span>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>
</div>
