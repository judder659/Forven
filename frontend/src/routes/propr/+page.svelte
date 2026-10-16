<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		cancelProprOrder,
		clearProprApiKey,
		closeProprPosition,
		getProprMirror,
		getProprOverview,
		getProprScorecard,
		getProprStatus,
		runProprConnectionTest,
		setProprApiKey,
		tickProprMirror,
		updateProprMirror,
		type ProprConnectionCheck,
		type ProprMirror,
		type ProprOverview,
		type ProprScorecard,
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

	// --- scorecard (PROPR-SCORE-1) -------------------------------------------
	let scorecard: ProprScorecard | null = null;
	let scorecardError = '';
	let showAllTrades = false;

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
			const [ov, mi, sc] = await Promise.all([
				getProprOverview(),
				getProprMirror().catch(() => null),
				getProprScorecard().catch((e) => {
					scorecardError = e instanceof Error ? e.message : String(e);
					return null;
				}),
			]);
			overview = ov;
			status = ov.status;
			mirror = mi;
			if (sc) {
				scorecard = sc;
				scorecardError = '';
			}
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

	async function removeInactive() {
		const inactive = new Set(scorecard?.inactive_roster ?? []);
		if (!inactive.size || !mirror) return;
		mirrorBusy = true;
		actionMessage = '';
		try {
			const keep = Object.keys(mirror.strategies).filter((id) => !inactive.has(id));
			await updateProprMirror({ strategies: keep });
			rosterDraft = new Set(keep);
			rosterDirty = false;
			actionMessage = `Removed ${inactive.size} inactive strateg${inactive.size === 1 ? 'y' : 'ies'}; each remaining member's capital slice grows to match.`;
			await load();
		} catch (e) {
			actionMessage = `Could not update the roster: ${e instanceof Error ? e.message : e}`;
		} finally {
			mirrorBusy = false;
		}
	}

	async function setRosterMode(mode: 'manual' | 'track_record') {
		if (mirror?.mode === mode) return;
		mirrorBusy = true;
		actionMessage = '';
		try {
			const res = await updateProprMirror({ mode });
			rosterDraft = new Set(Object.keys(res.strategies));
			rosterDirty = false;
			const n = Object.keys(res.strategies).length;
			actionMessage =
				mode === 'track_record'
					? `Track record mode: the mirror now follows ${n} strateg${n === 1 ? 'y' : 'ies'} with a winning forward record${n === 0 ? ' — none qualify yet, so the account stays flat' : ''}.`
					: 'Manual mode: the roster is yours to pick again.';
			await load();
		} catch (e) {
			actionMessage = `Could not change the roster mode: ${e instanceof Error ? e.message : e}`;
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

	// Challenge P&L: since-start and today.
	$: todayPnl =
		halt?.equity !== undefined && halt?.day_start_equity !== undefined
			? (halt.equity ?? 0) - (halt.day_start_equity ?? 0)
			: null;
	$: netPnlPct =
		halt?.profit_progress_usd !== undefined && halt?.starting_balance
			? ((halt.profit_progress_usd ?? 0) / halt.starting_balance) * 100
			: null;
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
	// --- scorecard derived --------------------------------------------------
	const fmtR = (v: number | null | undefined, digits = 2) =>
		v === null || v === undefined || Number.isNaN(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}R`;
	const toneOf = (v: number | null | undefined) =>
		v === null || v === undefined || v === 0 ? 'text-sc-ink2' : v > 0 ? 'text-emerald-400' : 'text-red-400';
	const healthClass: Record<string, string> = {
		ok: 'border-emerald-800 text-emerald-400',
		info: 'border-sc-line2 text-sc-ink3',
		warn: 'border-yellow-900 text-yellow-500',
		bad: 'border-red-900 text-red-400',
	};
	const healthRank: Record<string, number> = { bad: 0, warn: 1, info: 2, ok: 3 };
	$: scoreRows = [...(scorecard?.strategies ?? [])].sort(
		(a, b) =>
			Number(b.on_roster) - Number(a.on_roster) ||
			(healthRank[a.health.level] ?? 9) - (healthRank[b.health.level] ?? 9) ||
			a.strategy_id.localeCompare(b.strategy_id)
	);
	$: inactiveRoster = mirror?.mode === 'track_record' ? [] : (scorecard?.inactive_roster ?? []);
	$: trackMode = mirror?.mode === 'track_record';
	$: trackRows = scorecard?.track_record?.strategies ?? [];
	const verdictClass: Record<string, string> = {
		qualified: 'border-emerald-800 text-emerald-400',
		probation: 'border-sc-line2 text-sc-ink3',
		benched: 'border-red-900 text-red-400',
	};
	$: scoreTrades = [...(scorecard?.trades ?? [])].reverse();
	$: visibleTrades = showAllTrades ? scoreTrades : scoreTrades.slice(0, 12);
	$: closedTrades = (scorecard?.trades ?? []).filter((t) => !t.venue_open);
	// What removing the inactive members does to everyone else's size: the
	// slice is equity / roster size, so risk per trade scales with it.
	$: riskAfterPrune = (() => {
		const n = capitalSlice?.roster_size ?? 0;
		const keep = n - inactiveRoster.length;
		if (!riskPerTrade || !n || keep <= 0) return null;
		return (riskPerTrade * n) / keep;
	})();
	$: winRate = closedTrades.length
		? closedTrades.filter((t) => t.pnl_usd > 0).length / closedTrades.length
		: null;
	$: totalR = closedTrades.reduce((sum, t) => sum + (t.r ?? 0), 0);
	// Challenge track: drawdown floor ... start ... target, with equity marked.
	$: track = (() => {
		const start = halt?.starting_balance;
		const allowance = halt?.drawdown_allowance_usd;
		const target = halt?.profit_target_usd;
		const equity = halt?.equity ?? status?.account_value ?? null;
		if (!start || !allowance || !target || equity === null || equity === undefined) return null;
		// Works for static and trailing rules alike: the floor sits wherever the
		// remaining drawdown allowance runs out from here.
		const floor = equity - (allowance - (halt?.drawdown_used ?? 0));
		const top = start + target;
		const span = top - floor;
		const at = (v: number) => Math.max(0, Math.min(100, ((v - floor) / span) * 100));
		return {
			floor,
			top,
			equity,
			startPct: at(start),
			equityPct: at(equity),
			haltPct: at(floor + allowance * 0.2),
			toTarget: top - equity,
			toFloor: equity - floor,
		};
	})();
	$: riskPerTrade = capitalSlice?.risk_usd_per_trade ?? null;
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
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Propr</h1>
			<p class="text-[11px] text-sc-ink3">
				Your Propr challenge account, traded by mirroring the strategies you pick. Who is making
				and losing money is below, rebuilt from the venue's own fills.
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
		<!-- ─────────────────────── roster health alert ─────────────────────── -->
		{#if inactiveRoster.length > 0}
			<div class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-red-900 bg-red-500/5 px-3 py-2 text-[12px] text-red-400">
				<span>
					{inactiveRoster.length} of {Object.keys(mirror?.strategies ?? {}).length} roster strategies
					can no longer trade ({inactiveRoster.join(', ')}) but still hold an equal share of the
					account.
					{#if riskAfterPrune}
						Removing them raises each remaining member's risk per trade from {fmtUsd(riskPerTrade)}
						to about {fmtUsd(riskAfterPrune)}; check the scorecard below before you do.
					{/if}
				</span>
				<button
					class="rounded-md border border-red-800 px-3 py-1 text-[12px] text-red-300 hover:bg-red-950 disabled:opacity-50"
					on:click={removeInactive}
					disabled={mirrorBusy}
				>
					{mirrorBusy ? 'Updating…' : `Remove ${inactiveRoster.length} inactive`}
				</button>
			</div>
		{/if}

		<!-- ─────────────────────── challenge hero ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-4">
			<div class="flex flex-wrap items-end justify-between gap-4">
				<div>
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						Challenge equity
					</div>
					<div class="text-[28px] font-semibold leading-tight text-sc-ink">
						{fmtUsd(halt?.equity ?? status?.account_value)}
					</div>
					<div class={`text-[13px] ${toneOf(halt?.profit_progress_usd)}`}>
						{signedUsd(halt?.profit_progress_usd)}
						{#if netPnlPct !== null}({netPnlPct >= 0 ? '+' : ''}{netPnlPct.toFixed(2)}%){/if}
						<span class="text-sc-ink3">since the {fmtUsd(halt?.starting_balance)} start</span>
					</div>
				</div>
				<div class="grid grid-cols-2 gap-x-6 gap-y-1 text-[12px] sm:grid-cols-4">
					<div>
						<div class="text-[10px] uppercase tracking-wider text-sc-ink3">Today</div>
						<div class={toneOf(todayPnl)}>{signedUsd(todayPnl)}</div>
					</div>
					<div>
						<div class="text-[10px] uppercase tracking-wider text-sc-ink3">Mirrored trades</div>
						<div class="text-sc-ink2">
							{closedTrades.length}
							{#if winRate !== null}<span class="text-sc-ink3">· {(winRate * 100).toFixed(0)}% won</span>{/if}
						</div>
					</div>
					<div>
						<div class="text-[10px] uppercase tracking-wider text-sc-ink3">Net result</div>
						<div class={toneOf(totalR)}>{closedTrades.length ? fmtR(totalR, 1) : '—'}</div>
					</div>
					<div>
						<div class="text-[10px] uppercase tracking-wider text-sc-ink3">Risk per trade</div>
						<div class="text-sc-ink2">{fmtUsd(riskPerTrade)}</div>
					</div>
				</div>
			</div>

			{#if track}
				<div class="space-y-1">
					<div class="relative h-2.5 rounded-full bg-sc-panel2">
						<div
							class="absolute inset-y-0 rounded-full bg-red-500/30"
							style={`left:0; width:${track.haltPct}%`}
							title="Inside the mirror's drawdown halt zone"
						></div>
						<div class="absolute inset-y-[-3px] w-px bg-sc-ink3" style={`left:${track.startPct}%`} title="Starting balance"></div>
						<div
							class={`absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-sc-panel ${track.equity >= (halt?.starting_balance ?? 0) ? 'bg-emerald-400' : 'bg-red-400'}`}
							style={`left:${track.equityPct}%`}
							title={`Equity ${fmtUsd(track.equity)}`}
						></div>
					</div>
					<div class="flex justify-between text-[11px] text-sc-ink3">
						<span>Fail at {fmtUsd(track.floor)} · {fmtUsd(track.toFloor)} of room</span>
						<span>Pass at {fmtUsd(track.top)} · {fmtUsd(track.toTarget)} to go</span>
					</div>
					{#if riskPerTrade && track.toTarget > 0}
						<div class="text-[11px] text-sc-ink3">
							At {fmtUsd(riskPerTrade)} risked per trade, passing needs about
							{Math.ceil(track.toTarget / riskPerTrade)}R of net wins; failing takes about
							{Math.ceil(track.toFloor / riskPerTrade)}R of net losses.
						</div>
					{/if}
				</div>
			{/if}

			{#if scorecard?.account?.change_usd !== null && scorecard?.account?.change_usd !== undefined}
				{@const acct = scorecard.account}
				<div class="flex flex-wrap gap-x-5 gap-y-1 border-t border-sc-line pt-2 text-[11px] text-sc-ink3">
					<span>Where it went:</span>
					<span>trading after fees <span class={toneOf(acct.trading_net_usd)}>{signedUsd(acct.trading_net_usd)}</span></span>
					<span
						title="Balance changes that are not fills — mostly funding payments. Derived as account change minus trading."
					>
						funding and other <span class={toneOf(acct.other_usd)}>{signedUsd(acct.other_usd)}</span>
					</span>
					{#if scorecard.fidelity.trades > 0}
						<span
							title="Each mirrored trade's R against the same trade in the source strategy. Close numbers mean the mirror is copying faithfully."
						>
							copy fidelity: mirror {fmtR(scorecard.fidelity.mirror_sum_r, 1)} vs source {fmtR(scorecard.fidelity.source_sum_r, 1)}
							over {scorecard.fidelity.trades} trades
						</span>
					{/if}
				</div>
			{/if}
		</div>

		<!-- ─────────────────────── scorecard ─────────────────────── -->
		<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
			<div class="flex flex-wrap items-baseline justify-between gap-2">
				<div>
					<h2 class="text-[14px] font-semibold text-sc-ink">Who is making money</h2>
					<p class="text-[11px] text-sc-ink3">
						The mirror can only be as good as what it copies. "Strategy" is each strategy's own
						record since it joined, mirrored or not; "On Propr" is what the copies actually made.
						R is profit in units of the trade's risk at its stop.
					</p>
				</div>
				{#if scorecard?.ledger_refreshed_at}
					<span class="text-[10px] text-sc-ink3">from venue fills · {fmtWhen(scorecard.ledger_refreshed_at)}</span>
				{/if}
			</div>
			{#if scorecardError && !scorecard}
				<div class="text-[11px] text-yellow-500">Scorecard unavailable: {scorecardError}</div>
			{:else if scoreRows.length === 0}
				<div class="text-[11px] text-sc-ink3">No roster strategies or mirrored trades yet.</div>
			{:else}
				<div class="overflow-x-auto">
					<table class="w-full text-[12px]">
						<thead>
							<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
								<th class="py-1.5 pr-3">Strategy</th>
								<th class="py-1.5 pr-3">Status</th>
								<th class="py-1.5 pr-3 text-right">Strategy trades</th>
								<th class="py-1.5 pr-3 text-right">Strategy result</th>
								<th class="py-1.5 pr-3 text-right">On Propr</th>
								<th class="py-1.5 pr-3 text-right">Propr P&L</th>
								<th class="py-1.5 pr-3 text-right">Missed</th>
								<th class="py-1.5 text-right" title="Average entry price gap vs the source trade; positive is worse">Slip</th>
							</tr>
						</thead>
						<tbody>
							{#each scoreRows as row (row.strategy_id)}
								{@const reasons = Object.entries(row.forward.missed_reasons)
									.map(([why, n]) => `${n}× ${why}`)
									.join('\n')}
								<tr class={`border-b border-sc-line ${row.on_roster ? '' : 'opacity-60'}`}>
									<td class="py-1.5 pr-3">
										<div class="font-semibold text-sc-ink">{row.strategy_id}</div>
										<div class="max-w-[220px] truncate text-[10px] text-sc-ink3" title={row.name ?? ''}>
											{row.on_roster ? '' : 'left roster · '}{row.stage ?? 'missing'}{row.timeframe ? ` · ${row.timeframe}` : ''}
										</div>
									</td>
									<td class="py-1.5 pr-3">
										<span
											class={`whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px] ${healthClass[row.health.level] ?? ''}`}
											title={row.health.detail}
										>
											{row.health.label}
										</span>
									</td>
									<td class="py-1.5 pr-3 text-right text-sc-ink2">
										{row.forward.trades}
										{#if row.forward.with_r}<span class="text-sc-ink3">· {row.forward.wins}W</span>{/if}
									</td>
									<td class={`py-1.5 pr-3 text-right ${toneOf(row.forward.sum_r)}`}>
										{fmtR(row.forward.sum_r, 1)}
										{#if row.forward.mean_r !== null}
											<div class="text-[10px] text-sc-ink3">{fmtR(row.forward.mean_r)} avg</div>
										{/if}
									</td>
									<td class="py-1.5 pr-3 text-right text-sc-ink2">
										{row.mirror.closed}
										{#if row.mirror.closed}<span class="text-sc-ink3">· {row.mirror.wins}W</span>{/if}
									</td>
									<td class={`py-1.5 pr-3 text-right ${toneOf(row.mirror.pnl_usd)}`}>
										{row.mirror.closed ? signedUsd(row.mirror.pnl_usd) : '—'}
										{#if row.mirror.sum_r !== null}
											<div class="text-[10px] text-sc-ink3">{fmtR(row.mirror.sum_r, 1)}</div>
										{/if}
									</td>
									<td class="py-1.5 pr-3 text-right">
										{#if row.forward.missed}
											<span class="cursor-help text-yellow-500 underline decoration-dotted" title={reasons}>
												{row.forward.missed}
											</span>
										{:else}
											<span class="text-sc-ink3">0</span>
										{/if}
									</td>
									<td class="py-1.5 text-right text-sc-ink3">
										{row.mirror.avg_entry_slippage_bps === null ? '—' : `${row.mirror.avg_entry_slippage_bps.toFixed(1)} bps`}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				<p class="text-[10px] text-sc-ink3">
					Hover a status or a missed count for details. Fewer than 8 trades is noise, not a verdict.
				</p>
			{/if}
		</div>

		<!-- ─────────────────────── mirrored trade history ─────────────────────── -->
		{#if scoreTrades.length > 0}
			<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-2">
				<div class="flex items-baseline justify-between">
					<h2 class="text-[14px] font-semibold text-sc-ink">Mirrored trades</h2>
					<span class="text-[10px] text-sc-ink3">all time, from venue fills</span>
				</div>
				<div class="overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="border-b border-sc-line text-left font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
								<th class="py-1 pr-2">Opened</th>
								<th class="py-1 pr-2">Strategy</th>
								<th class="py-1 pr-2">Trade</th>
								<th class="py-1 pr-2 text-right">Entry → exit</th>
								<th class="py-1 pr-2 text-right">P&L</th>
								<th class="py-1 pr-2 text-right">Propr R</th>
								<th class="py-1 text-right">Source R</th>
							</tr>
						</thead>
						<tbody>
							{#each visibleTrades as t (t.trade_id)}
								<tr class="border-b border-sc-line text-sc-ink2">
									<td class="py-1 pr-2 text-sc-ink3">{fmtWhen(t.opened_at)}</td>
									<td class="py-1 pr-2">{t.strategy_id} <span class="text-sc-ink3">({t.execution_type})</span></td>
									<td class="py-1 pr-2">
										<span class="font-semibold">{t.asset}</span>
										<span class={t.direction === 'short' ? 'text-red-400' : 'text-emerald-400'}>{t.direction}</span>
										{#if t.shared_position}
											<span class="text-sc-ink3" title="Shared a netted venue position with another mirrored trade; P&L split by quantity">· shared</span>
										{/if}
									</td>
									<td class="py-1 pr-2 text-right">
										{fmtNum(t.entry_fill)} → {t.venue_open ? 'open' : fmtNum(t.exit_fill)}
									</td>
									<td class={`py-1 pr-2 text-right ${t.venue_open ? 'text-sc-ink3' : toneOf(t.pnl_usd)}`}>
										{t.venue_open ? '—' : signedUsd(t.pnl_usd)}
									</td>
									<td class={`py-1 pr-2 text-right ${toneOf(t.r)}`}>{fmtR(t.r)}</td>
									<td class={`py-1 text-right ${toneOf(t.source_r)}`}>{fmtR(t.source_r)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				{#if scoreTrades.length > 12}
					<button class="text-[11px] text-sc-ink3 hover:text-sc-ink2" on:click={() => (showAllTrades = !showAllTrades)}>
						{showAllTrades ? 'Show fewer' : `Show all ${scoreTrades.length}`}
					</button>
				{/if}
			</div>
		{/if}

		<!-- ─────────────────────── challenge rules ─────────────────────── -->
		{#if halt?.checked_at}
			{@const dailyPct = gaugePct(halt.daily_loss, halt.daily_loss_limit_usd)}
			{@const ddPct = gaugePct(halt.drawdown_used, halt.drawdown_allowance_usd)}
			{@const targetPct = gaugePct(halt.profit_progress_usd, halt.profit_target_usd)}
			<details class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-4" open={halt.halted || dailyPct >= 50 || ddPct >= 50}>
				<summary class="flex cursor-pointer items-center justify-between">
					<div>
						<h2 class="inline text-[14px] font-semibold text-sc-ink">Challenge rules and safeguards</h2>
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
				</summary>

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
			</details>
		{/if}

		<!-- ─────────────────────── strategy mirror ─────────────────────── -->
		<div
			class={`border p-4 space-y-3 ${mirror?.enabled ? 'border-emerald-900 bg-sc-panel' : 'border-sc-line bg-sc-panel'}`}
		>
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-[14px] font-semibold text-sc-ink">Strategy mirror</h2>
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
			<div class="flex flex-wrap items-center gap-2 text-[12px]">
				<span class="text-sc-ink3">Who to follow:</span>
				{#each [['track_record', 'Track record'], ['manual', 'Manual picks']] as [m, label] (m)}
					<button
						class={`rounded-md border px-3 py-1 disabled:opacity-50 ${
							(mirror?.mode ?? 'manual') === m
								? 'border-emerald-800 bg-[#081008] text-emerald-400'
								: 'border-sc-line2 bg-sc-panel2 text-sc-ink2 hover:bg-sc-raise'
						}`}
						on:click={() => setRosterMode(m as 'manual' | 'track_record')}
						disabled={mirrorBusy}
					>
						{label}
					</button>
				{/each}
				<span class="text-[11px] text-sc-ink3">
					{trackMode
						? `Follows every paper or live strategy with ${scorecard?.track_record?.min_trades ?? 8}+ forward trades and a positive average R; benches the rest. Re-checked every 15 minutes. Open copies always close normally.`
						: 'You choose the roster below.'}
				</span>
			</div>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
				{#if trackMode}
				<div class="space-y-2">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
						Forward records ({trackRows.filter((r) => r.verdict === 'qualified').length} qualify)
					</span>
					<div class="max-h-64 overflow-y-auto border border-sc-line divide-y divide-sc-line">
						{#each trackRows as r (r.strategy_id)}
							<div class="flex items-center gap-2 px-2 py-1.5 text-[11px]">
								<span class={`rounded border px-1.5 py-0.5 text-[10px] ${verdictClass[r.verdict] ?? ''}`}>{r.verdict}</span>
								<span class="font-bold text-sc-ink2">{r.strategy_id}</span>
								<span class="text-sc-ink3">{r.stage}</span>
								<span class="flex-1"></span>
								<span class="text-sc-ink3">{r.trades} trades · {r.wins}W</span>
								<span class={`w-20 text-right ${toneOf(r.mean_r)}`}>{fmtR(r.mean_r)} avg</span>
							</div>
						{:else}
							<div class="px-2 py-3 text-[11px] text-sc-ink3">No paper or live strategies yet.</div>
						{/each}
					</div>
				</div>
				{:else}
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
				{/if}

				<div class="space-y-1">
					<div class="flex items-center justify-between">
						<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
							Recent activity <span class="normal-case text-sc-ink3">(last 7 days, incl. skips and why)</span>
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
			<h2 class="text-[14px] font-semibold text-sc-ink">Positions</h2>
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
			<h2 class="text-[14px] font-semibold text-sc-ink">Open orders</h2>
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
			<h2 class="text-[14px] font-semibold text-sc-ink">Recent trades</h2>
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
		<!-- ─────────────────────── connection ─────────────────────── -->
		<details class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3" open={!status?.connected}>
			<summary class="flex cursor-pointer items-center justify-between">
				<h2 class="inline text-[14px] font-semibold text-sc-ink">Connection</h2>
				<span class="text-[10px] text-sc-ink3">{status?.base_url}</span>
			</summary>
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
		</details>

	{/if}
</div>
