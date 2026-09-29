<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		cancelProprOrder,
		clearProprApiKey,
		closeProprPosition,
		getProprMirror,
		getProprOverview,
		getProprStatus,
		runProprConnectionTest,
		setProprApiKey,
		tickProprMirror,
		updateProprMirror,
		type ProprConnectionCheck,
		type ProprMirror,
		type ProprOverview,
		type ProprStatus,
	} from '$lib/api/propr';
	import ErrorBanner from '$lib/components/ErrorBanner.svelte';
	import LoadingState from '$lib/components/LoadingState.svelte';

	let overview: ProprOverview | null = null;
	let status: ProprStatus | null = null;
	let loading = true;
	let error = '';
	let actionMessage = '';
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	// PROPR-1: while the hidden backend flag is off, every route except status
	// 404s and status reports { enabled: false } — render the neutral off state
	// (deliberately without naming the flag; this page must not document its
	// own activation).
	let enabled: boolean | null = null;

	let apiKeyInput = '';
	let keyBusy = false;
	let testBusy = false;
	let testChecks: ProprConnectionCheck[] | null = null;
	let closingPosition: string | null = null;
	let confirmingClose: string | null = null;
	let cancellingOrder: string | null = null;

	// --- strategy mirror ----------------------------------------------------
	let mirror: ProprMirror | null = null;
	let rosterDraft: Set<string> = new Set();
	let rosterDirty = false;
	let mirrorBusy = false;
	let mirrorTickBusy = false;
	let strategyFilter = '';

	type Row = Record<string, unknown>;
	// Propr response field names are matched permissively — the docs don't pin
	// every key, so each cell reads the first present spelling.
	const pick = (row: Row, ...keys: string[]): unknown => {
		for (const key of keys) {
			const value = row?.[key];
			if (value !== undefined && value !== null && value !== '') return value;
		}
		return null;
	};
	const pickStr = (row: Row, ...keys: string[]): string => {
		const value = pick(row, ...keys);
		return value === null ? '—' : String(value);
	};
	const pickNum = (row: Row, ...keys: string[]): number | null => {
		const value = pick(row, ...keys);
		if (value === null) return null;
		const num = Number(value);
		return Number.isNaN(num) ? null : num;
	};
	const fmtNum = (v: number | null | undefined, digits = 2) =>
		v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(digits);
	const fmtUsd = (v: number | null | undefined) => {
		if (v === null || v === undefined || Number.isNaN(v)) return '—';
		return `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
	};
	const fmtWhen = (value: unknown) => {
		if (!value) return '—';
		try {
			return new Date(String(value)).toLocaleString();
		} catch {
			return String(value);
		}
	};

	async function load() {
		try {
			status = await getProprStatus(false);
			enabled = Boolean(status?.enabled);
			if (!enabled) {
				loading = false;
				return;
			}
			const [ov, mi] = await Promise.all([
				getProprOverview(),
				getProprMirror().catch(() => null),
			]);
			overview = ov;
			status = ov.status;
			mirror = mi;
			if (mi && !rosterDirty) rosterDraft = new Set(Object.keys(mi.strategies));
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		refreshTimer = setInterval(load, 30_000);
	});
	onDestroy(() => {
		if (refreshTimer) clearInterval(refreshTimer);
	});

	async function saveKey() {
		keyBusy = true;
		actionMessage = '';
		try {
			await setProprApiKey(apiKeyInput.trim());
			apiKeyInput = '';
			actionMessage = 'API key stored (encrypted). Run the connection test to verify it.';
			await load();
		} catch (e) {
			actionMessage = `Could not store the key: ${e instanceof Error ? e.message : e}`;
		} finally {
			keyBusy = false;
		}
	}

	async function clearKey() {
		keyBusy = true;
		actionMessage = '';
		try {
			await clearProprApiKey();
			actionMessage = 'API key removed.';
			testChecks = null;
			await load();
		} catch (e) {
			actionMessage = `Could not remove the key: ${e instanceof Error ? e.message : e}`;
		} finally {
			keyBusy = false;
		}
	}

	async function runTest() {
		testBusy = true;
		actionMessage = '';
		testChecks = null;
		try {
			const res = await runProprConnectionTest();
			testChecks = res.checks;
			actionMessage = res.ok
				? 'Connection test passed — key, account and positions all read cleanly.'
				: 'Connection test finished with failures — see the checks below.';
		} catch (e) {
			actionMessage = `Connection test failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			testBusy = false;
		}
	}

	function toggleRosterStrategy(id: string) {
		if (rosterDraft.has(id)) rosterDraft.delete(id);
		else rosterDraft.add(id);
		rosterDraft = new Set(rosterDraft);
		rosterDirty = true;
	}

	async function saveRoster() {
		mirrorBusy = true;
		actionMessage = '';
		try {
			await updateProprMirror({ strategies: [...rosterDraft] });
			rosterDirty = false;
			actionMessage = `Mirror roster saved (${rosterDraft.size} strateg${rosterDraft.size === 1 ? 'y' : 'ies'}). Only trades opened from now on are mirrored.`;
			await load();
		} catch (e) {
			actionMessage = `Roster save failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorBusy = false;
		}
	}

	async function toggleMirror() {
		mirrorBusy = true;
		actionMessage = '';
		try {
			const next = !mirror?.enabled;
			await updateProprMirror({ enabled: next });
			actionMessage = next
				? 'Mirror enabled — the observer copies new roster trades to Propr every 60s.'
				: 'Mirror disabled — no new Propr orders; existing mirrored positions keep their brackets.';
			await load();
		} catch (e) {
			actionMessage = `Mirror toggle failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorBusy = false;
		}
	}

	async function forceMirrorTick() {
		mirrorTickBusy = true;
		actionMessage = '';
		try {
			const res = await tickProprMirror();
			actionMessage = `Mirror tick: ${JSON.stringify(res.result)}`;
			await load();
		} catch (e) {
			actionMessage = `Mirror tick failed: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorTickBusy = false;
		}
	}

	async function closePos(row: Row) {
		const asset = pickStr(row, 'asset', 'coin');
		const side = pickStr(row, 'positionSide', 'side').toLowerCase() === 'short' ? 'short' : 'long';
		const qty = Math.abs(pickNum(row, 'quantity', 'size', 'szi') ?? 0);
		const key = `${asset}:${side}`;
		if (confirmingClose !== key) {
			confirmingClose = key;
			return;
		}
		confirmingClose = null;
		closingPosition = key;
		actionMessage = '';
		try {
			await closeProprPosition(asset, side, qty);
			actionMessage = `Close sent for ${asset} ${side} (${qty}).`;
			await load();
		} catch (e) {
			actionMessage = `Close refused: ${e instanceof Error ? e.message : e}`;
		} finally {
			closingPosition = null;
		}
	}

	async function cancelOrder(row: Row) {
		const orderId = pickStr(row, 'orderId', 'order_id', 'id');
		cancellingOrder = orderId;
		actionMessage = '';
		try {
			await cancelProprOrder(orderId);
			actionMessage = `Cancel sent for order ${orderId}.`;
			await load();
		} catch (e) {
			actionMessage = `Cancel refused: ${e instanceof Error ? e.message : e}`;
		} finally {
			cancellingOrder = null;
		}
	}

	$: candidates = (mirror?.candidates ?? []).filter(
		(c) =>
			!strategyFilter.trim() ||
			`${c.id} ${c.name}`.toLowerCase().includes(strategyFilter.trim().toLowerCase())
	);
	// Skips/failures carry only recorded_at — without the fallback they sink to
	// the bottom regardless of recency.
	const stateWhen = (st: { opened_at?: string; closed_at?: string; recorded_at?: string }) =>
		st.closed_at ?? st.opened_at ?? st.recorded_at ?? '';
	$: mirrorRows = Object.entries(mirror?.state ?? {}).sort((a, b) =>
		stateWhen(b[1]).localeCompare(stateWhen(a[1]))
	);
	$: accountIsPaper = status?.account_type === 'paper';
	$: halt = mirror?.halt ?? null;
	// SLICE-1: how the challenge account is divided across the roster.
	$: capitalSlice = mirror?.capital_slice ?? null;

	// Realized PnL of one CLOSED ledger entry (price move × qty, before fees).
	const statePnl = (st: {
		status?: string;
		direction?: string;
		quantity?: number;
		entry_price?: number | null;
		exit_price?: number | null;
	}): number | null => {
		if (st.status !== 'closed') return null;
		const entry = Number(st.entry_price);
		const exit = Number(st.exit_price);
		const qty = Number(st.quantity);
		if (!Number.isFinite(entry) || !Number.isFinite(exit) || !Number.isFinite(qty) || qty <= 0)
			return null;
		return (exit - entry) * qty * (st.direction === 'short' ? -1 : 1);
	};

	// Mirror activity summary over the ledger (the backend prunes terminal
	// entries after 7 days, so these figures cover the recent window).
	$: mirrorSummary = (() => {
		const counts: Record<string, number> = {};
		let realized = 0;
		let hasRealized = false;
		for (const [, st] of mirrorRows) {
			counts[st.status] = (counts[st.status] ?? 0) + 1;
			const pnl = statePnl(st);
			if (pnl !== null) {
				realized += pnl;
				hasRealized = true;
			}
		}
		return { counts, realized: hasRealized ? realized : null };
	})();

	// Challenge P&L tiles: since-start and today, plus open unrealized.
	$: todayPnl =
		halt?.equity !== undefined && halt?.day_start_equity !== undefined
			? (halt.equity ?? 0) - (halt.day_start_equity ?? 0)
			: null;
	$: netPnlPct =
		halt?.profit_progress_usd !== undefined && halt?.starting_balance
			? ((halt.profit_progress_usd ?? 0) / halt.starting_balance) * 100
			: null;
	$: unrealizedTotal = (() => {
		let total = 0;
		let seen = false;
		for (const row of positions) {
			const v = pickNum(row as Row, 'unrealizedPnl', 'unrealized_pnl');
			if (v !== null) {
				total += v;
				seen = true;
			}
		}
		return seen ? total : null;
	})();
	// Days into the active attempt, from the attempts list the venue serves.
	$: attemptDaysIn = (() => {
		const active = attempts.find((a) => pickStr(a, 'status').toLowerCase() === 'active');
		const started = pick(active ?? {}, 'createdAt', 'created_at', 'startedAt');
		if (!started) return null;
		const ms = Date.now() - new Date(String(started)).getTime();
		return Number.isFinite(ms) && ms >= 0 ? Math.floor(ms / 86_400_000) : null;
	})();
	const signedUsd = (v: number | null | undefined) =>
		v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${fmtUsd(v)}`;

	// Gauge helpers for the challenge-rules panel: fill % of the venue limit,
	// clamped, with color stepping at 50% and at the 80% halt line.
	const gaugePct = (used: number | null | undefined, limit: number | null | undefined) => {
		if (!limit || limit <= 0) return 0;
		return Math.max(0, Math.min(100, ((used ?? 0) / limit) * 100));
	};
	const gaugeColor = (pctUsed: number) =>
		pctUsed >= 80 ? '#f87171' : pctUsed >= 50 ? '#eab308' : '#34d399';
	$: positions = (overview?.positions ?? []) as Row[];
	$: openOrders = ((overview?.orders ?? []) as Row[]).filter((o) =>
		['pending', 'open', 'partially_filled'].includes(pickStr(o, 'status').toLowerCase())
	);
	$: trades = ((overview?.trades ?? []) as Row[]).slice(0, 25);
	$: attempts = (overview?.attempts ?? []) as Row[];
	$: sectionErrors = overview?.errors ?? {};
</script>

<svelte:head>
	<title>Propr — Forven</title>
</svelte:head>

<div class="space-y-4 p-4">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-bold uppercase tracking-wider text-sc-ink">Propr</h1>
			<p class="text-[11px] text-sc-ink3">
				On-chain prop firm on Hyperliquid: a purchased challenge account trades real HL markets
				through Propr's API. This page manages the connection, the challenge account, and manual
				position control. Live dispatch only reaches Propr when explicitly armed on the backend.
			</p>
		</div>
		{#if status?.enabled}
			<div class="flex items-center gap-2 shrink-0">
				<span
					class={`text-xs px-2 py-1 border ${status.connected ? 'text-emerald-400 border-emerald-800' : 'text-yellow-400 border-yellow-800'}`}
				>
					{status.connected ? 'Connected' : status.api_key_configured ? 'Not connected' : 'No API key'}
				</span>
				{#if status.account_type}
					<span
						class={`text-xs px-2 py-1 border ${accountIsPaper ? 'text-[#c9a227] border-[#8a6d1a]' : 'text-red-400 border-red-800'}`}
						title={accountIsPaper
							? 'Propr reports this account as a paper/trial account — mirrored orders spend no real money.'
							: 'Propr reports this account as REAL — NEW positions require the backend live opt-in. Closes, protective legs and cancels are unaffected.'}
					>
						{accountIsPaper ? 'PAPER ACCOUNT' : `REAL (${status.account_type})`}
					</span>
				{/if}
				<span
					class={`text-xs px-2 py-1 border ${mirror?.enabled ? 'text-emerald-400 border-emerald-800' : 'text-sc-ink3 border-sc-line2'}`}
				>
					{mirror?.enabled ? 'Mirror ON' : 'Mirror off'}
				</span>
			</div>
		{/if}
	</div>

	{#if error}
		<ErrorBanner message={error} />
	{/if}
	{#if actionMessage}
		<div class="rounded-md border border-sc-line2 bg-sc-panel px-3 py-2 text-xs text-sc-ink2">{actionMessage}</div>
	{/if}

	{#if loading}
		<LoadingState message="Loading Propr…" />
	{:else if enabled === false}
		<div class="rounded-md border border-sc-line bg-sc-panel p-6 text-center space-y-2">
			<div class="text-sm font-bold uppercase tracking-wider text-sc-ink2">
				Propr integration is not enabled
			</div>
			<p class="text-xs text-sc-ink3">
				This integration is switched off on the backend and has no Settings-page control.
			</p>
		</div>
	{:else}
		<!-- ─────────────────────── connection ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Connection</h2>
				<span class="text-[10px] text-sc-ink3">{status?.base_url}</span>
			</div>
			{#if status?.connected && status?.account_error}
				<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">
					Propr answered, but your ACCOUNT could not be resolved ({status.account_error}).
					Nothing can be placed until that clears — opens, closes, protective legs and cancels
					all resolve the same account and will fail with this error. This is a venue or
					connection problem, not the permission gate.
				</div>
			{:else if status?.connected && !status?.orders_allowed}
				<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-500">
					NEW POSITIONS are DISARMED: the account is not verifiably a paper/trial account
					(type: {status?.account_type ?? 'unknown'}) and the backend live opt-in is not set.
					This is the designed behavior for when the evaluation ends and the account becomes
					real. You are NOT locked in: closing a position, arming a stop or take-profit, and
					cancelling an order all still work (PROPR-PERM-2) — only opens need the opt-in.
				</div>
			{:else if accountIsPaper && !status?.allow_live}
				<div class="border border-[#1a2438] bg-[#050a12] px-3 py-2 text-[11px] text-[#9ab]">
					Orders are allowed WITHOUT the backend live opt-in because Propr reports this account
					as paper — re-verified against the exchange on every single open, not from a cache
					(PROPR-PERM-1). The moment the evaluation ends and the account type changes, new
					positions fail closed automatically; exits keep working.
				</div>
			{/if}
			<div class="grid grid-cols-1 md:grid-cols-3 gap-2 items-end text-[11px]">
				<label class="space-y-1 md:col-span-2">
					<span class="text-sc-ink3 uppercase text-[10px] tracking-wider">
						API key {status?.api_key_configured ? '(configured — paste to replace)' : '(from app.propr.xyz → Settings)'}
					</span>
					<input
						type="password"
						bind:value={apiKeyInput}
						placeholder="pk_live_…"
						autocomplete="off"
						class="rounded-md w-full border border-sc-line2 bg-sc-panel px-2 py-1.5 text-sc-ink outline-none"
					/>
				</label>
				<div class="flex items-center gap-2">
					<button
						class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
						on:click={saveKey}
						disabled={keyBusy || !apiKeyInput.trim()}
					>
						{keyBusy ? 'Saving…' : 'Save key'}
					</button>
					{#if status?.api_key_configured}
						<button
							class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
							on:click={clearKey}
							disabled={keyBusy}
						>
							Remove
						</button>
					{/if}
					<button
						class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
						on:click={runTest}
						disabled={testBusy || !status?.api_key_configured}
					>
						{testBusy ? 'Testing…' : 'Connection test'}
					</button>
				</div>
			</div>
			{#if testChecks}
				<div class="space-y-1">
					{#each testChecks as check (check.name)}
						<div class="flex items-center gap-2 text-[11px]">
							<span class={check.ok ? 'text-emerald-400' : 'text-red-400'}>{check.ok ? '✓' : '✗'}</span>
							<span class="uppercase text-[10px] tracking-wider text-sc-ink2 w-20">{check.name}</span>
							<span class="text-sc-ink3 truncate font-mono text-[10px]">
								{check.ok ? 'ok' : String(check.detail)}
							</span>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── challenge account ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
			<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Challenge account</h2>
			{#if sectionErrors.attempts}
				<div class="text-[11px] text-yellow-500">{sectionErrors.attempts}</div>
			{/if}
			<div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-center">
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Account value</div>
					<div class="text-base font-bold text-sc-ink">{fmtUsd(status?.account_value)}</div>
					{#if halt?.starting_balance}
						<div class="text-[10px] text-sc-ink3">started {fmtUsd(halt.starting_balance)}</div>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">P&L since start</div>
					<div
						class={`text-base font-bold ${(halt?.profit_progress_usd ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
					>
						{signedUsd(halt?.profit_progress_usd)}
					</div>
					{#if netPnlPct !== null}
						<div class="text-[10px] text-sc-ink3">{netPnlPct >= 0 ? '+' : ''}{netPnlPct.toFixed(2)}%</div>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Today</div>
					<div class={`text-base font-bold ${(todayPnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
						{signedUsd(todayPnl)}
					</div>
					<div class="text-[10px] text-sc-ink3">resets each UTC day</div>
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Unrealized</div>
					<div
						class={`text-base font-bold ${unrealizedTotal === null ? 'text-sc-ink2' : unrealizedTotal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}
					>
						{unrealizedTotal === null ? '—' : signedUsd(unrealizedTotal)}
					</div>
					<div class="text-[10px] text-sc-ink3">
						{positions.length} open position{positions.length === 1 ? '' : 's'}
					</div>
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Attempt status</div>
					<div class="text-base font-bold text-sc-ink2">{status?.attempt_status ?? '—'}</div>
					{#if attemptDaysIn !== null}
						<div class="text-[10px] text-sc-ink3">day {attemptDaysIn + 1}</div>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">To target</div>
					{#if halt?.profit_target_usd}
						<div class="text-base font-bold text-sc-ink">
							{fmtUsd(Math.max(0, (halt.profit_target_usd ?? 0) - (halt.profit_progress_usd ?? 0)))}
						</div>
						<div class="text-[10px] text-sc-ink3">of {fmtUsd(halt.profit_target_usd)} target</div>
					{:else}
						<div class="text-base font-bold text-sc-ink2">—</div>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Sizing</div>
					{#if capitalSlice?.risk_pct}
						<div class="text-base font-bold text-sc-ink">
							{capitalSlice.risk_pct}% <span class="text-sc-ink3 text-xs">/ trade</span>
						</div>
						<div class="text-[10px] text-sc-ink3">
							≈ {fmtUsd(capitalSlice.risk_usd_per_trade)} risk on a {fmtUsd(capitalSlice.slice_usd)} slice
						</div>
					{:else}
						<div class="text-base font-bold text-sc-ink2">—</div>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-2">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Account</div>
					<div class="text-[10px] font-mono text-sc-ink2 pt-1.5 truncate" title={status?.account_id}>
						{status?.account_id ?? '—'}
					</div>
				</div>
			</div>
			{#if attempts.length > 0}
				<details class="text-[11px]">
					<summary class="cursor-pointer text-sc-ink3 hover:text-sc-ink2 uppercase text-[10px] tracking-wider">
						Challenge attempts ({attempts.length})
					</summary>
					<div class="mt-1 space-y-0.5 max-h-40 overflow-y-auto">
						{#each attempts as attempt}
							<div class="flex items-center gap-3 border-b border-sc-line py-1 text-sc-ink2">
								<span class="font-mono text-[10px] truncate">{pickStr(attempt, 'id', 'attemptId')}</span>
								<span class="uppercase text-[10px]">{pickStr(attempt, 'status')}</span>
								<span class="text-sc-ink3 text-[10px]">{fmtWhen(pick(attempt, 'createdAt', 'created_at'))}</span>
							</div>
						{/each}
					</div>
				</details>
			{/if}
		</div>

		<!-- ─────────────────────── challenge rules ─────────────────────── -->
		{#if halt?.checked_at}
			{@const dailyPct = gaugePct(halt.daily_loss, halt.daily_loss_limit_usd)}
			{@const ddPct = gaugePct(halt.drawdown_used, halt.drawdown_allowance_usd)}
			{@const targetPct = gaugePct(halt.profit_progress_usd, halt.profit_target_usd)}
			<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-4">
				<div class="flex items-center justify-between">
					<div>
						<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Challenge rules</h2>
						<p class="text-[11px] text-sc-ink3">
							The venue's kill conditions, read from the challenge itself
							({halt.rules_source === 'challenge' ? "Propr's own phase rules" : 'conservative fallback defaults'}),
							and how close the account is to each. The white tick on each bar is the mirror's
							halt line — new opens stop there, 20% before Propr would fail the attempt.
						</p>
					</div>
					<span class="text-[10px] text-sc-ink3 shrink-0">
						checked {fmtWhen(halt.checked_at)}
					</span>
				</div>

				{#if halt.daily_rule_fully_enforced === false}
					<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-[11px] text-yellow-500">
						Today's opening balance was never observed (cold start), so the daily-loss gauge is
						anchored on the first reading of the day and only PARTIALLY enforces the venue's rule
						today. Tomorrow's anchor carries over normally.
					</div>
				{/if}

				<!-- daily loss gauge -->
				<div class="space-y-1">
					<div class="flex items-baseline justify-between text-[11px]">
						<span class="uppercase tracking-wider text-[10px] text-sc-ink2">
							Max daily loss
							<span class="text-sc-ink3 normal-case">— resets each UTC day</span>
						</span>
						<span class={dailyPct >= 80 ? 'text-red-400 font-bold' : 'text-sc-ink2'}>
							{fmtUsd(halt.daily_loss)} of {fmtUsd(halt.daily_loss_limit_usd)}
							<span class="text-sc-ink3">· halts at {fmtUsd(halt.daily_halt_at_usd)}</span>
						</span>
					</div>
					<div class="rounded-md relative h-3 border border-sc-line bg-sc-panel">
						<div
							class="absolute inset-y-0 left-0"
							style={`width:${dailyPct}%; background:${gaugeColor(dailyPct)}; opacity:.85`}
						></div>
						<div class="absolute inset-y-0 w-px bg-sc-ink" style="left:80%" title="Mirror halt line (80% of the venue cap)"></div>
					</div>
				{#if capitalSlice?.slice_usd}
					<div class="rounded-md border border-sc-line bg-sc-panel px-3 py-2 text-[11px]">
						<div class="flex items-center justify-between">
							<span class="text-sc-ink2">Position sizing</span>
							<span class="text-sc-ink2">
								account split {capitalSlice.roster_size} ways ·
								<span class="text-sc-ink">{fmtUsd(capitalSlice.slice_usd)}</span> each
								{#if capitalSlice.risk_pct}
									· risking <span class="text-sc-ink">{capitalSlice.risk_pct}%</span>
									(≈ {fmtUsd(capitalSlice.risk_usd_per_trade)}) per trade
								{/if}
							</span>
						</div>
						<div class="mt-1 text-[10px] text-sc-ink3">
							Each roster strategy risks {capitalSlice.risk_pct ?? 2}% of ITS OWN share at the
							trade's stop distance, so the whole roster stopping out on the same day risks
							~{capitalSlice.risk_pct ?? 2}% of the account — not {capitalSlice.roster_size}× it.
							The percentage is tunable in Settings → Trading → Propr challenge mirror.
						</div>
					</div>
				{/if}
					<div class="text-[10px] text-sc-ink3">
						Guarded by: open-halt at 80% · loss-at-stop of open positions counts against the
						remaining budget, so simultaneous stop-outs can't stack past the cap.
					</div>
				</div>

				<!-- drawdown gauge -->
				<div class="space-y-1">
					<div class="flex items-baseline justify-between text-[11px]">
						<span class="uppercase tracking-wider text-[10px] text-sc-ink2">
							Max drawdown
							<span class="text-sc-ink3 normal-case">
								— {halt.drawdown_type === 'trailing' ? 'trailing, from the high-water mark' : `static, from the ${fmtUsd(halt.starting_balance)} starting balance`}
							</span>
						</span>
						<span class={ddPct >= 80 ? 'text-red-400 font-bold' : 'text-sc-ink2'}>
							{fmtUsd(halt.drawdown_used)} of {fmtUsd(halt.drawdown_allowance_usd)}
						</span>
					</div>
					<div class="rounded-md relative h-3 border border-sc-line bg-sc-panel">
						<div
							class="absolute inset-y-0 left-0"
							style={`width:${ddPct}%; background:${gaugeColor(ddPct)}; opacity:.85`}
						></div>
						<div class="absolute inset-y-0 w-px bg-sc-ink" style="left:80%" title="Mirror halt line (80% of the allowance)"></div>
					</div>
					<div class="text-[10px] text-sc-ink3">
						Guarded by: open-halt at 80% · every mirrored entry carries an exchange-side stop
						bracket · sizing risks {capitalSlice?.risk_pct ?? 2}% of each strategy's slice per
						trade (tunable in Settings → Trading → Propr challenge mirror) · closes are never
						halted, so risk can always come down.
					</div>
				</div>

				<!-- profit target gauge -->
				{#if halt.profit_target_usd}
					<div class="space-y-1">
						<div class="flex items-baseline justify-between text-[11px]">
							<span class="uppercase tracking-wider text-[10px] text-sc-ink2">
								Profit target
								<span class="text-sc-ink3 normal-case">— passes the phase</span>
							</span>
							<span class={(halt.profit_progress_usd ?? 0) < 0 ? 'text-red-400' : 'text-emerald-400'}>
								{fmtUsd(halt.profit_progress_usd)} of {fmtUsd(halt.profit_target_usd)}
							</span>
						</div>
						<div class="rounded-md relative h-3 border border-sc-line bg-sc-panel">
							<div
								class="absolute inset-y-0 left-0 bg-emerald-500"
								style={`width:${targetPct}%; opacity:.85`}
							></div>
						</div>
					</div>
				{/if}

				<div class="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-sc-ink3 border-t border-sc-line pt-2">
					<span>✓ no unprotected entries (source trades without a stop are never mirrored)</span>
					<span>✓ day-start equity carried from the previous day's last reading, so overnight losses count</span>
					<span>✓ auto-disarm if the account stops reporting as paper</span>
					<span>✓ unreadable rules fall back to the strictest tier (3% / 6%)</span>
				</div>
			</div>
		{/if}

		<!-- ─────────────────────── strategy mirror ─────────────────────── -->
		<div
			class={`border p-4 space-y-3 ${mirror?.enabled ? 'border-emerald-900 bg-sc-panel' : 'border-sc-line bg-sc-panel'}`}
		>
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Strategy mirror</h2>
					<p class="text-[11px] text-sc-ink3">
						Pick the strategies whose trades get copied onto the Propr account. Live and paper
						trading are completely untouched — the mirror only observes their trades and places
						its own independently-sized orders here ({capitalSlice?.risk_pct ?? 2}% of the
						strategy's equal slice at each trade's stop, entry + stop + take-profit as one
						bracket, reduce-only closes). Only trades opened AFTER a strategy joins the roster
						are mirrored; a failed open retries as long as the entry signal stays live.
					</p>
				</div>
				<div class="flex items-center gap-2 shrink-0">
					<button
						class={`rounded-md border px-3 py-1.5 text-[12px] disabled:opacity-50 ${
							mirror?.enabled
								? 'border-emerald-800 text-emerald-400 bg-[#081008] hover:bg-[#0a140a]'
								: 'border-sc-line2 bg-sc-panel2 text-sc-ink2 hover:bg-sc-raise'
						}`}
						on:click={toggleMirror}
						disabled={mirrorBusy || !status?.api_key_configured}
					>
						{mirror?.enabled ? 'Mirror is ON — click to disable' : 'Enable mirror'}
					</button>
					<button
						class="rounded-md border border-sc-line2 bg-sc-panel2 px-3 py-1.5 text-[12px] text-sc-ink2 hover:bg-sc-raise disabled:opacity-50"
						on:click={forceMirrorTick}
						disabled={mirrorTickBusy || !mirror?.enabled}
					>
						{mirrorTickBusy ? 'Ticking…' : 'Tick now'}
					</button>
				</div>
			</div>

			{#if halt?.halted}
				<div class="border border-red-900 bg-red-500/5 px-3 py-2 text-[11px] text-red-400">
					<span class="font-bold uppercase tracking-wider">Halted — new opens blocked:</span>
					{(halt.reasons ?? []).join('; ')}. Closes still execute. The daily-loss halt clears at
					the next UTC day; the drawdown halt clears if equity recovers.
				</div>
			{/if}
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							Strategies ({rosterDraft.size} selected)
						</span>
						<input
							type="text"
							bind:value={strategyFilter}
							placeholder="filter…"
							class="rounded-md border border-sc-line2 bg-sc-panel px-2 py-1 text-[11px] text-sc-ink outline-none w-32"
						/>
					</div>
					<div class="max-h-64 overflow-y-auto border border-sc-line divide-y divide-sc-line">
						{#each candidates as c (c.id)}
							<label class="flex items-center gap-2 px-2 py-1.5 text-[11px] cursor-pointer hover:bg-sc-panel2">
								<input
									type="checkbox"
									checked={rosterDraft.has(c.id)}
									on:change={() => toggleRosterStrategy(c.id)}
								/>
								<span class="font-bold text-sc-ink2">{c.id}</span>
								<span class="text-sc-ink3 truncate flex-1">{c.name}</span>
								<span
									class={`font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-1 py-0.5 border ${
										c.stage === 'live_graduated'
											? 'border-red-900 text-red-400'
											: c.stage === 'paper'
												? 'border-[#1c2b1c] text-emerald-500'
												: 'border-sc-line2 text-sc-ink3'
									}`}
								>
									{c.stage}
								</span>
								{#if c.timeframe}<span class="text-sc-ink3 text-[10px]">{c.timeframe}</span>{/if}
							</label>
						{:else}
							<div class="px-2 py-3 text-[11px] text-sc-ink3">No strategies match the filter.</div>
						{/each}
					</div>
					{#if rosterDirty}
						<button
							class="rounded-md border border-emerald-900 bg-[#081008] px-3 py-1.5 text-[12px] text-emerald-400 hover:bg-[#0a140a] disabled:opacity-50"
							on:click={saveRoster}
							disabled={mirrorBusy}
						>
							{mirrorBusy ? 'Saving…' : `Save roster (${rosterDraft.size})`}
						</button>
					{/if}
				</div>

				<div class="space-y-1">
					<div class="flex items-center justify-between">
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							Mirrored trades <span class="normal-case text-sc-ink3">(last 7 days)</span>
						</span>
						{#if mirrorRows.length > 0}
							<span class="text-[10px] text-sc-ink3">
								{#each ['open', 'closed', 'pending', 'error', 'failed', 'skipped'] as s}
									{#if mirrorSummary.counts[s]}
										<span class="mr-2">{mirrorSummary.counts[s]} {s}</span>
									{/if}
								{/each}
								{#if mirrorSummary.realized !== null}
									· realized
									<span class={mirrorSummary.realized >= 0 ? 'text-emerald-400' : 'text-red-400'}>
										{signedUsd(mirrorSummary.realized)}
									</span>
								{/if}
							</span>
						{/if}
					</div>
					{#if mirrorRows.length === 0}
						<div class="text-[11px] text-sc-ink3">
							Nothing mirrored yet — trades appear here within a minute of a roster strategy
							opening one.
						</div>
					{:else}
						<div class="max-h-64 overflow-y-auto space-y-0.5">
							{#each mirrorRows as [tradeId, st] (tradeId)}
								{@const pnl = statePnl(st)}
								<div class="rounded-md border border-sc-line bg-sc-panel px-2 py-1 text-[11px]">
									<div class="flex items-center gap-2">
										<span
											class={`font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-1 py-0.5 border shrink-0 ${
												st.status === 'open'
													? 'border-emerald-800 text-emerald-400'
													: st.status === 'closed'
														? 'border-sc-line2 text-sc-ink2'
														: st.status === 'skipped'
															? 'border-sc-line2 text-sc-ink3'
															: st.status === 'pending'
																? 'border-yellow-900 text-yellow-500'
																: 'border-red-900 text-red-400'
											}`}
										>
											{st.status}
										</span>
										<span class="font-bold text-sc-ink2 shrink-0">{st.asset}</span>
										<span class={st.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}>
											{st.direction}
										</span>
										{#if st.retry_signal_gated}
											<span
												class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] px-1 py-0.5 border border-[#3a2a10] text-[#c9a227] shrink-0"
												title="Re-armed after a terminal open failure because the strategy's entry signal was still live (RETRY-1)."
											>
												re-armed
											</span>
										{/if}
										<span class="text-sc-ink3 truncate flex-1" title={st.reason ?? ''}>
											{st.strategy}
											<span class="text-sc-ink3">({st.source_execution_type ?? '?'})</span>
										</span>
										{#if pnl !== null}
											<span class={`shrink-0 ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
												{signedUsd(pnl)}
											</span>
										{/if}
										<span class="text-sc-ink3 text-[10px] shrink-0">{fmtWhen(stateWhen(st))}</span>
									</div>
									<div class="mt-0.5 flex items-center gap-3 text-[10px] text-sc-ink3 pl-1">
										{#if st.quantity}
											<span>{fmtNum(st.quantity, 5)} {st.asset}</span>
										{/if}
										{#if st.entry_price}
											<span>
												in {fmtNum(st.entry_price)}{st.exit_price ? ` → out ${fmtNum(st.exit_price)}` : ''}
											</span>
										{/if}
										{#if st.risk_usd}
											<span>risk {fmtUsd(st.risk_usd)}</span>
										{/if}
										{#if st.reason}
											<span class="truncate text-sc-ink3" title={st.reason}>{st.reason}</span>
										{/if}
									</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			</div>
		</div>

		<!-- ─────────────────────── positions ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Positions</h2>
			{#if sectionErrors.positions}
				<div class="text-[11px] text-yellow-500">{sectionErrors.positions}</div>
			{:else if positions.length === 0}
				<div class="text-[11px] text-sc-ink3">No open positions.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 border-b border-sc-line">
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Size</th>
								<th class="py-1 pr-2 text-right">Entry</th>
								<th class="py-1 pr-2 text-right">Mark</th>
								<th class="py-1 pr-2 text-right">Liq.</th>
								<th class="py-1 pr-2 text-right">uPnL</th>
								<th class="py-1 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#each positions as row}
								{@const key = `${pickStr(row, 'asset', 'coin')}:${pickStr(row, 'positionSide', 'side').toLowerCase()}`}
								{@const upnl = pickNum(row, 'unrealizedPnl', 'unrealized_pnl')}
								<tr class="border-b border-sc-line text-sc-ink2">
									<td class="py-1 pr-2 font-bold text-sc-ink2">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">
										<span class={pickStr(row, 'positionSide', 'side').toLowerCase() === 'short' ? 'text-red-400' : 'text-emerald-400'}>
											{pickStr(row, 'positionSide', 'side')}
										</span>
									</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity', 'size', 'szi'), 5)}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'entryPrice', 'entry_price'))}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'markPrice', 'mark_price'))}</td>
									<td class="py-1 pr-2 text-right text-sc-ink3">{fmtNum(pickNum(row, 'liquidationPrice', 'liquidation_price'))}</td>
									<td class={`py-1 pr-2 text-right ${(upnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
										{fmtUsd(upnl)}
									</td>
									<td class="py-1 text-right">
										<button
											class={`rounded-md border px-2 py-0.5 text-[12px] disabled:opacity-50 ${
												confirmingClose === key
													? 'border-red-700 bg-red-950 text-red-300'
													: 'border-sc-line2 text-sc-ink2 hover:text-sc-ink'
											}`}
											on:click={() => closePos(row)}
											disabled={closingPosition === key}
										>
											{closingPosition === key ? 'Closing…' : confirmingClose === key ? 'Confirm close' : 'Close'}
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── open orders ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Open orders</h2>
			{#if sectionErrors.orders}
				<div class="text-[11px] text-yellow-500">{sectionErrors.orders}</div>
			{:else if openOrders.length === 0}
				<div class="text-[11px] text-sc-ink3">No open orders.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 border-b border-sc-line">
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Type</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Qty</th>
								<th class="py-1 pr-2 text-right">Price / trigger</th>
								<th class="py-1 pr-2">Status</th>
								<th class="py-1 text-right">Actions</th>
							</tr>
						</thead>
						<tbody>
							{#each openOrders as row}
								{@const orderId = pickStr(row, 'orderId', 'order_id', 'id')}
								<tr class="border-b border-sc-line text-sc-ink2">
									<td class="py-1 pr-2 font-bold text-sc-ink2">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'type')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'side')}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity'), 5)}</td>
									<td class="py-1 pr-2 text-right">
										{fmtNum(pickNum(row, 'price') ?? pickNum(row, 'triggerPrice', 'trigger_price'))}
									</td>
									<td class="py-1 pr-2 text-sc-ink3">{pickStr(row, 'status')}</td>
									<td class="py-1 text-right">
										<button
											class="rounded-md border border-sc-line2 px-2 py-0.5 text-[12px] text-sc-ink2 hover:text-sc-ink disabled:opacity-50"
											on:click={() => cancelOrder(row)}
											disabled={cancellingOrder === orderId}
										>
											{cancellingOrder === orderId ? 'Cancelling…' : 'Cancel'}
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── recent trades ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-2">
			<h2 class="text-sm font-bold uppercase tracking-wider text-sc-ink">Recent trades</h2>
			{#if sectionErrors.trades}
				<div class="text-[11px] text-yellow-500">{sectionErrors.trades}</div>
			{:else if trades.length === 0}
				<div class="text-[11px] text-sc-ink3">No trades yet.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 border-b border-sc-line">
								<th class="py-1 pr-2">When</th>
								<th class="py-1 pr-2">Asset</th>
								<th class="py-1 pr-2">Type</th>
								<th class="py-1 pr-2">Side</th>
								<th class="py-1 pr-2 text-right">Qty</th>
								<th class="py-1 pr-2 text-right">Price</th>
								<th class="py-1 pr-2 text-right">Fee</th>
								<th class="py-1 text-right">Realized PnL</th>
							</tr>
						</thead>
						<tbody>
							{#each trades as row}
								{@const rpnl = pickNum(row, 'realizedPnl', 'realized_pnl')}
								<tr class="border-b border-sc-line text-sc-ink2">
									<td class="py-1 pr-2 text-sc-ink3">{fmtWhen(pick(row, 'createdAt', 'created_at', 'timestamp'))}</td>
									<td class="py-1 pr-2 font-bold text-sc-ink2">{pickStr(row, 'asset', 'coin')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'type')}</td>
									<td class="py-1 pr-2">{pickStr(row, 'side')}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'quantity'), 5)}</td>
									<td class="py-1 pr-2 text-right">{fmtNum(pickNum(row, 'price'))}</td>
									<td class="py-1 pr-2 text-right text-sc-ink3">{fmtNum(pickNum(row, 'fee'), 4)}</td>
									<td class={`py-1 text-right ${(rpnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
										{fmtUsd(rpnl)}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
	{/if}
</div>
