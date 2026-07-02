<script lang="ts">
	/**
	 * Paper-trades header rollup: per-session realized PnL, win rate, and a
	 * close_reason breakdown (GET /api/paper/summary).
	 *
	 * The close_reason breakdown is the trust signal — it makes reconciler /
	 * stale-sweep closes visible next to genuine strategy exits, so the
	 * operator knows how much of the paper record to believe.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getPaperSummary, type PaperSummary } from '$lib/api/dashboard';

	const REFRESH_MS = 60_000;

	// Reasons that indicate the outcome was fabricated/forced rather than a
	// genuine strategy exit — rendered in warning color.
	const SYNTHETIC_REASON_TOKENS = ['reconcile', 'stale', 'sweep', 'unspecified', 'force'];

	let summary: PaperSummary | null = null;
	let loading = true;
	let error = '';
	let expanded = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	$: totals = summary?.totals ?? null;
	$: reasonRows = Object.entries(totals?.close_reasons ?? {}).sort(
		(a, b) => b[1] - a[1],
	);
	$: sessions = summary?.sessions ?? [];

	function isSyntheticReason(reason: string): boolean {
		const lower = reason.toLowerCase();
		return SYNTHETIC_REASON_TOKENS.some((token) => lower.includes(token));
	}

	function formatUsd(value: number): string {
		const sign = value < 0 ? '-' : value > 0 ? '+' : '';
		return `${sign}$${Math.abs(value).toFixed(2)}`;
	}

	function formatWinRate(value: number | null): string {
		return value === null ? '—' : `${value.toFixed(1)}%`;
	}

	function dominantReason(reasons: Record<string, number>): string {
		const entries = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
		return entries.length ? entries[0][0] : '—';
	}

	async function load(): Promise<void> {
		try {
			summary = await getPaperSummary(false);
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load paper summary.';
		} finally {
			loading = false;
		}
	}

	function tick() {
		if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
		void load();
	}

	onMount(() => {
		void load();
		timer = setInterval(tick, REFRESH_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<section class="summary" data-testid="paper-session-summary">
	{#if error}
		<div class="error" data-testid="paper-summary-error">{error}</div>
	{:else if loading && !summary}
		<div class="empty">Loading paper summary…</div>
	{:else if totals}
		<div class="strip">
			<span class="eyebrow">Paper rollup</span>
			<span class="stat">
				<span class="stat-label">Sessions</span>
				<span class="stat-value">{totals.session_count}</span>
			</span>
			<span class="stat">
				<span class="stat-label">Closed</span>
				<span class="stat-value" data-testid="paper-summary-closed">{totals.closed_count}</span>
			</span>
			<span class="stat">
				<span class="stat-label">Open</span>
				<span class="stat-value" data-testid="paper-summary-open">{totals.open_count}</span>
			</span>
			<span class="stat">
				<span class="stat-label">Realized</span>
				<span
					class="stat-value"
					class:pos={totals.realized_pnl_usd > 0}
					class:neg={totals.realized_pnl_usd < 0}
					data-testid="paper-summary-pnl"
				>
					{formatUsd(totals.realized_pnl_usd)}
				</span>
			</span>
			<span class="stat">
				<span class="stat-label">Win rate</span>
				<span class="stat-value" data-testid="paper-summary-winrate">
					{formatWinRate(totals.win_rate_pct)}
				</span>
			</span>
			{#if reasonRows.length > 0}
				<span class="reasons" data-testid="paper-summary-reasons">
					{#each reasonRows as [reason, count] (reason)}
						<span
							class="reason-chip"
							class:reason-chip--warn={isSyntheticReason(reason)}
							title={`${count} close${count === 1 ? '' : 's'} via ${reason}`}
						>
							{reason} <b>{count}</b>
						</span>
					{/each}
				</span>
			{/if}
			{#if sessions.length > 0}
				<button
					type="button"
					class="toggle"
					on:click={() => (expanded = !expanded)}
					aria-expanded={expanded}
					data-testid="paper-summary-toggle"
				>
					{expanded ? 'Hide sessions ▾' : 'Per session ▸'}
				</button>
			{/if}
		</div>

		{#if expanded && sessions.length > 0}
			<div class="table-wrap" data-testid="paper-summary-sessions">
				<table>
					<thead>
						<tr>
							<th>Strategy</th>
							<th>Symbol</th>
							<th class="num">Closed</th>
							<th class="num">Open</th>
							<th class="num">Realized</th>
							<th class="num">Win rate</th>
							<th>Top close reason</th>
						</tr>
					</thead>
					<tbody>
						{#each sessions as session (session.session_id || session.strategy_id)}
							<tr data-testid="paper-summary-session-row">
								<td class="mono" title={session.strategy_id}>{session.strategy_name || session.strategy_id}</td>
								<td>{session.symbol}</td>
								<td class="num">{session.closed_count}</td>
								<td class="num">{session.open_count}</td>
								<td
									class="num"
									class:pos={session.realized_pnl_usd > 0}
									class:neg={session.realized_pnl_usd < 0}
								>
									{formatUsd(session.realized_pnl_usd)}
								</td>
								<td class="num">{formatWinRate(session.win_rate_pct)}</td>
								<td
									class="reason-cell"
									class:warn={isSyntheticReason(dominantReason(session.close_reasons))}
								>
									{dominantReason(session.close_reasons)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</section>

<style>
	.summary {
		display: flex;
		flex-direction: column;
		border: 1px solid #222;
		background: #050505;
	}

	.strip {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.4rem 0.9rem;
		padding: 0.4rem 0.75rem;
	}

	.eyebrow {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.stat {
		display: inline-flex;
		align-items: baseline;
		gap: 0.3rem;
	}

	.stat-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: #777;
	}

	.stat-value {
		font-family: ui-monospace, monospace;
		font-size: 0.75rem;
		font-weight: 700;
		color: #fff;
		font-variant-numeric: tabular-nums;
	}

	.pos {
		color: #34d399;
	}

	.neg {
		color: #f87171;
	}

	.reasons {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 0.25rem;
		min-width: 0;
	}

	.reason-chip {
		font-size: 0.625rem;
		font-family: ui-monospace, monospace;
		padding: 0.1rem 0.4rem;
		border: 1px solid #333;
		background: #050505;
		color: #888;
		white-space: nowrap;
	}

	.reason-chip b {
		color: #fff;
	}

	.reason-chip--warn {
		border-color: #713f12;
		background: rgba(250, 204, 21, 0.08);
		color: #facc15;
	}

	.reason-chip--warn b {
		color: #fde68a;
	}

	.toggle {
		margin-left: auto;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: #888;
		background: none;
		border: none;
		cursor: pointer;
		padding: 0.1rem 0.25rem;
	}

	.toggle:hover {
		color: #fff;
	}

	.table-wrap {
		border-top: 1px solid #1a1a1a;
		max-height: 200px;
		overflow-y: auto;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.6875rem;
	}

	th {
		position: sticky;
		top: 0;
		background: #050505;
		text-align: left;
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: #666;
		font-weight: 600;
		padding: 0.3rem 0.5rem;
		border-bottom: 1px solid #1a1a1a;
	}

	td {
		padding: 0.25rem 0.5rem;
		border-bottom: 1px solid #141414;
		color: #888;
		white-space: nowrap;
	}

	.num {
		text-align: right;
		font-family: ui-monospace, monospace;
		font-variant-numeric: tabular-nums;
	}

	.mono {
		font-family: ui-monospace, monospace;
		max-width: 22ch;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.reason-cell {
		font-family: ui-monospace, monospace;
		color: #888;
		max-width: 28ch;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.reason-cell.warn {
		color: #facc15;
	}

	.empty {
		font-size: 0.6875rem;
		color: #666;
		padding: 0.4rem 0.75rem;
	}

	.error {
		font-size: 0.6875rem;
		color: #f87171;
		padding: 0.4rem 0.75rem;
	}
</style>
