<script lang="ts">
	/**
	 * Phase 3 / P3-T15 — Dashboard widget that surfaces quant skills whose
	 * confidence has trended down over the last N days (default 14).
	 *
	 * Data source: GET /api/skills/declining (see ``forven/routers/skills.py``).
	 * The endpoint aggregates `skill_outcome_events.confidence_delta` server-
	 * side, so this widget never has to walk per-skill outcomes itself.
	 *
	 * Click on a row opens the existing `SkillDetailDrawer` so operators can
	 * inspect history / outcomes / evidence without leaving the dashboard.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { listDecliningSkills, type DecliningSkillRow } from '$lib/api/skills';
	import SkillDetailDrawer from '$lib/components/SkillDetailDrawer.svelte';

	const REFRESH_MS = 60_000;
	const LOOKBACK_DAYS = 14;
	const LIMIT = 5;

	let rows: DecliningSkillRow[] = [];
	let loading = true;
	let error = '';
	let drawerSkillName: string | null = null;
	let timer: ReturnType<typeof setInterval> | null = null;

	async function load(): Promise<void> {
		try {
			const res = await listDecliningSkills({ days: LOOKBACK_DAYS, limit: LIMIT });
			rows = res.items;
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load declining skills.';
		} finally {
			loading = false;
		}
	}

	function formatRelative(iso: string | null): string {
		if (!iso) return '—';
		const ts = Date.parse(iso);
		if (!Number.isFinite(ts)) return '—';
		const ageMin = Math.max(0, (Date.now() - ts) / 60000);
		if (ageMin < 60) return `${Math.round(ageMin)}m ago`;
		const ageH = ageMin / 60;
		if (ageH < 24) return `${Math.round(ageH)}h ago`;
		return `${Math.round(ageH / 24)}d ago`;
	}

	function severity(delta: number): 'mild' | 'warn' | 'critical' {
		if (delta <= -0.2) return 'critical';
		if (delta <= -0.1) return 'warn';
		return 'mild';
	}

	function openDrawer(name: string): void {
		drawerSkillName = name;
	}

	function closeDrawer(): void {
		drawerSkillName = null;
		void load();
	}

	onMount(() => {
		void load();
		timer = setInterval(() => void load(), REFRESH_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<section class="declining-widget">
	<header class="widget-head">
		<div class="title-block">
			<div class="eyebrow">Declining Skills</div>
			<div class="subtitle">Confidence drops over last {LOOKBACK_DAYS}d</div>
		</div>
		<div class="count-pill" class:count-pill--clean={!loading && rows.length === 0}>
			{loading ? '…' : rows.length}
		</div>
	</header>

	{#if error}
		<div class="error">{error}</div>
	{:else if loading && rows.length === 0}
		<div class="empty">Loading…</div>
	{:else if rows.length === 0}
		<div class="empty empty--clean">All skills holding steady.</div>
	{:else}
		<ul class="rows">
			{#each rows as row (row.skill_name)}
				{@const sev = severity(row.total_delta)}
				<li>
					<button type="button" class="row" on:click={() => openDrawer(row.skill_name)}>
						<div class="row-main">
							<div class="row-name" title={row.skill_name}>{row.skill_name}</div>
							<div class="row-meta">
								v{row.version} · {Math.round(row.confidence * 100)}% conf · {formatRelative(row.last_event_at)}
							</div>
						</div>
						<div class="row-stats">
							<div class="delta delta--{sev}">{row.total_delta.toFixed(2)}</div>
							<div class="event-count">{row.event_count} ev</div>
						</div>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</section>

{#if drawerSkillName}
	<SkillDetailDrawer name={drawerSkillName} on:close={closeDrawer} />
{/if}

<style>
	.declining-widget {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.75rem;
		border: 1px solid #222;
		background: #050505;
		height: 100%;
		min-height: 0;
	}

	.widget-head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 0.5rem;
	}

	.title-block {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.eyebrow {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.18em;
		color: #888;
		font-weight: 600;
	}

	.subtitle {
		font-size: 0.6875rem;
		color: #666;
	}

	.count-pill {
		font-size: 0.625rem;
		font-weight: 700;
		padding: 0.2rem 0.5rem;
		border: 1px solid #7f1d1d;
		background: rgba(239, 68, 68, 0.1);
		color: #f87171;
		min-width: 1.75rem;
		text-align: center;
	}

	.count-pill--clean {
		border-color: #064e3b;
		background: rgba(52, 211, 153, 0.08);
		color: #34d399;
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		overflow-y: auto;
	}

	.row {
		width: 100%;
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.5rem;
		padding: 0.4rem 0.5rem;
		background: #050505;
		border: 1px solid #1a1a1a;
		color: inherit;
		text-align: left;
		cursor: pointer;
		transition: border-color 120ms ease, background 120ms ease;
	}

	.row:hover {
		border-color: #555;
		background: #111;
	}

	.row-main {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.row-name {
		font-family: ui-monospace, monospace;
		font-size: 0.75rem;
		color: #fff;
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.row-meta {
		font-size: 0.625rem;
		color: #666;
	}

	.row-stats {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 0.125rem;
		flex-shrink: 0;
	}

	.delta {
		font-family: ui-monospace, monospace;
		font-size: 0.75rem;
		font-weight: 700;
	}

	.delta--mild {
		color: #888;
	}

	.delta--warn {
		color: #facc15;
	}

	.delta--critical {
		color: #f87171;
	}

	.event-count {
		font-size: 0.5625rem;
		color: #666;
		text-transform: uppercase;
		letter-spacing: 0.08em;
	}

	.empty {
		font-size: 0.75rem;
		color: #666;
		padding: 0.5rem;
		text-align: center;
	}

	.empty--clean {
		color: #34d399;
	}

	.error {
		font-size: 0.6875rem;
		color: #f87171;
		padding: 0.4rem 0.5rem;
		border: 1px solid #7f1d1d;
		background: rgba(239, 68, 68, 0.05);
	}
</style>
