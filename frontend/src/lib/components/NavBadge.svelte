<script lang="ts">
	import type { NavBadgeView } from '$lib/stores/navMetrics';

	/** What the sidebar link shows (navMetrics.navBadges); nothing when null. */
	export let badge: NavBadgeView | null | undefined = undefined;

	// Bright while the badge holds something you have not seen on that page yet;
	// dimmed (outline only) once you have. The number itself never changes on a
	// visit — it is the real count until the items are resolved.
	const FRESH: Record<string, string> = {
		danger: 'border-red-900 bg-red-500/15 text-red-300',
		warn: 'border-yellow-900 bg-yellow-500/15 text-yellow-300',
		success: 'border-emerald-900 bg-emerald-500/15 text-emerald-300',
		info: 'border-sc-line2 bg-sc-raise text-sc-ink',
		neutral: 'border-sc-line2 bg-sc-raise text-sc-ink2',
	};

	const SEEN: Record<string, string> = {
		danger: 'border-red-900/70 text-red-400/80',
		warn: 'border-yellow-900/70 text-yellow-500/80',
		success: 'border-emerald-900/70 text-emerald-500/80',
		info: 'border-sc-line2 text-sc-ink3',
		neutral: 'border-sc-line2 text-sc-ink3',
	};

	function countLabel(count: number): string {
		return count > 99 ? '99+' : String(count);
	}

	// A status pill (e.g. STALE live data) is a standing hazard: always bright.
	$: palette = badge?.fresh || badge?.kind === 'status' ? FRESH : SEEN;
	$: tone = palette[badge?.severity ?? 'neutral'] ?? palette.neutral;
	$: tooltip = badge ? `${badge.summary}${badge.fresh && badge.kind === 'count' ? ' · new since you last looked' : ''}` : '';
</script>

{#if badge && badge.kind === 'status' && badge.label}
	<span
		class="shrink-0 border px-1.5 py-0.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {tone}"
		title={tooltip}
		data-fresh={badge.fresh}
	>
		{badge.label}
	</span>
{:else if badge && badge.kind === 'count' && badge.count > 0}
	<span
		class="shrink-0 min-w-[18px] h-[18px] px-1 border text-[10px] font-bold tabular-nums flex items-center justify-center {tone}"
		title={tooltip}
		aria-label={tooltip}
		data-fresh={badge.fresh}
	>
		{countLabel(badge.count)}
	</span>
{/if}
