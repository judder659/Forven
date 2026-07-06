/**
 * Approval-type registry for the /approval queue.
 *
 * Friendly titles for every known approval_type plus the per-type payload
 * renderer map. Unknown/future types fall back to a prettified title and the
 * raw-JSON block in the page.
 */
import type { ComponentType, SvelteComponent } from 'svelte';
import type { ApprovalRecord } from '$lib/api/forven';

import StrategyApprovalCard from './StrategyApprovalCard.svelte';
import TaskApprovalCard from './TaskApprovalCard.svelte';
import CrucibleDethroneCard from './CrucibleDethroneCard.svelte';
import RegimeChampionCard from './RegimeChampionCard.svelte';
import RoutineCreateCard from './RoutineCreateCard.svelte';

export const APPROVAL_TYPE_TITLES: Record<string, string> = {
	strategy_dethrone_recommendation: 'Dethrone Recommendation',
	strategy_promotion_approval: 'Strategy Promotion',
	task_approval: 'Agent Task Approval',
	code_change: 'Code Change',
	skill_update_proposal: 'Skill Update Proposal',
	routine_create: 'New Routine Proposal',
	crucible_dethrone: 'Crucible Dethrone',
	regime_champion_promotion: 'Regime Champion Promotion',
};

export function friendlyTitle(approvalType: string | null | undefined): string {
	const key = String(approvalType || '').trim().toLowerCase();
	if (!key) return 'Approval';
	if (APPROVAL_TYPE_TITLES[key]) return APPROVAL_TYPE_TITLES[key];
	return key
		.split('_')
		.map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
		.join(' ');
}

type ApprovalCard = ComponentType<SvelteComponent<{ approval: ApprovalRecord }>>;

/** Per-type payload renderer. skill_update_proposal is dispatched separately
 *  in the page (its card takes `payload`, kept unchanged). */
const PAYLOAD_RENDERERS: Record<string, ApprovalCard> = {
	strategy_dethrone_recommendation: StrategyApprovalCard,
	strategy_promotion_approval: StrategyApprovalCard,
	task_approval: TaskApprovalCard,
	code_change: TaskApprovalCard,
	crucible_dethrone: CrucibleDethroneCard,
	regime_champion_promotion: RegimeChampionCard,
	routine_create: RoutineCreateCard,
};

export function payloadRenderer(approvalType: string | null | undefined): ApprovalCard | null {
	return PAYLOAD_RENDERERS[String(approvalType || '').trim().toLowerCase()] ?? null;
}

/** True for the strategy lifecycle types that support the deny-reason picker
 *  (deny arms the escalating dethrone cooldown for these). */
export function isStrategyLifecycleType(approvalType: string | null | undefined): boolean {
	const key = String(approvalType || '').trim().toLowerCase();
	return key === 'strategy_dethrone_recommendation' || key === 'strategy_promotion_approval';
}
