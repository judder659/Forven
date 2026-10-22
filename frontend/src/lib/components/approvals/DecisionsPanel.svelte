<script lang="ts">
	// "Waiting for your decision": every pending approval, with its details and
	// the decision buttons inline. Replaces the old Approvals page. Agents ask
	// for things here (demote a live strategy, update a skill);
	// paper strategies never appear, the paper pool's rules retire those.
	import { createEventDispatcher, onMount } from 'svelte';
	import { approveApproval, denyApproval, getApprovals, type ApprovalRecord } from '$lib/api/forven';
	import GoLiveApprovalDialog from './GoLiveApprovalDialog.svelte';
	import DenyReasonPicker from './DenyReasonPicker.svelte';
	import SkillUpdateProposalCard from './SkillUpdateProposalCard.svelte';
	import { friendlyTitle, isStrategyLifecycleType, payloadRenderer } from './renderers';
	import { stageLabel } from '$lib/utils/forge/status';
	import { ago } from '$lib/utils/forge/time';

	/** Approval to open and scroll to, e.g. from a notification link. */
	export let focusId: number | null = null;

	const dispatch = createEventDispatcher<{ count: number }>();

	let approvals: ApprovalRecord[] = [];
	let loaded = false;
	let error: string | null = null;
	let message: string | null = null;
	let busyId: number | null = null;
	let open = new Set<number>();
	let denyPickerId: number | null = null;
	let goLive: ApprovalRecord | null = null;
	let goLiveError: string | null = null;

	export async function refresh(): Promise<void> {
		try {
			approvals = await getApprovals({ status: 'pending_approval', limit: 100 });
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not load decisions.';
		} finally {
			loaded = true;
			dispatch('count', approvals.length);
		}
	}

	onMount(async () => {
		await refresh();
		if (focusId !== null && approvals.some((a) => a.id === focusId)) {
			open = new Set([focusId]);
			document.getElementById(`decision-${focusId}`)?.scrollIntoView({ block: 'center' });
		}
	});

	function isDethrone(a: ApprovalRecord): boolean {
		return a.approval_type === 'strategy_dethrone_recommendation';
	}

	// GO-LIVE-1: the backend flags approvals that need the typed GO LIVE.
	function needsGoLive(a: ApprovalRecord): boolean {
		if (typeof a.requires_go_live === 'boolean') return a.requires_go_live;
		return a.approval_type === 'strategy_promotion_approval' && String(a.requested_status || '').toLowerCase() === 'live_graduated';
	}

	function approveLabel(a: ApprovalRecord): string {
		if (isDethrone(a)) return `Move to ${stageLabel(a.requested_status).toLowerCase()}`;
		if (needsGoLive(a)) return 'Go live…';
		return 'Approve';
	}

	function denyLabel(a: ApprovalRecord): string {
		return isDethrone(a) ? 'Keep' : 'Deny';
	}

	function headline(a: ApprovalRecord): string {
		const target = a.target_id ? ` · ${a.target_id}` : '';
		const to = a.requested_status && a.target_type === 'strategy' ? ` → ${stageLabel(a.requested_status).toLowerCase()}` : '';
		return `${friendlyTitle(a.approval_type)}${target}${to}`;
	}

	function toggle(id: number): void {
		const next = new Set(open);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		open = next;
	}

	async function decide(
		a: ApprovalRecord,
		action: 'approve' | 'deny',
		reason?: string,
		extra: Record<string, unknown> = {},
	): Promise<boolean> {
		busyId = a.id;
		error = null;
		message = null;
		try {
			const body = { actor: 'operator', reason: reason || `Operator ${action === 'approve' ? 'approved' : 'denied'} from Needs you`, ...extra };
			if (action === 'approve') await approveApproval(a.id, body);
			else await denyApproval(a.id, body);
			message = `${friendlyTitle(a.approval_type)}${a.target_id ? ` for ${a.target_id}` : ''}: ${action === 'approve' ? approveLabel(a).replace('…', '').toLowerCase() : denyLabel(a).toLowerCase()}.`;
			await refresh();
			return true;
		} catch (e) {
			const text = e instanceof Error ? e.message : `Could not ${action} #${a.id}.`;
			if (goLive) goLiveError = text;
			else error = text;
			return false;
		} finally {
			busyId = null;
		}
	}

	function onApprove(a: ApprovalRecord): void {
		if (needsGoLive(a)) {
			goLiveError = null;
			goLive = a;
			return;
		}
		void decide(a, 'approve');
	}

	function onDeny(a: ApprovalRecord): void {
		// Denying a strategy move arms an escalating cooldown, so it takes a reason.
		if (isStrategyLifecycleType(a.approval_type)) {
			denyPickerId = denyPickerId === a.id ? null : a.id;
			return;
		}
		void decide(a, 'deny');
	}

	async function confirmGoLive(ceilingUsd: number): Promise<void> {
		if (!goLive) return;
		const ok = await decide(goLive, 'approve', undefined, { confirm: 'GO LIVE', live_notional_ceiling_usd: ceilingUsd });
		if (ok) goLive = null;
	}
</script>

{#if loaded && (approvals.length > 0 || error || message)}
	<article id="decisions" class="overflow-hidden rounded-md border border-[#e7b24a]/40 bg-sc-panel" data-testid="decisions-panel">
		<header class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
			<h2 class="flex items-center gap-2 text-[13px] font-semibold text-sc-ink">
				Waiting for your decision
				{#if approvals.length > 0}
					<span class="rounded-full bg-[#e7b24a]/15 px-1.5 font-plex-mono text-[11px] font-medium text-[#e7b24a]">{approvals.length}</span>
				{/if}
			</h2>
			<span class="text-[11px] text-sc-ink3">Requests from agents that only you can answer</span>
		</header>
		{#if error}
			<p class="m-0 border-b border-sc-line px-3.5 py-2 text-[12px] text-[#f2956f]" data-testid="decisions-error">{error}</p>
		{/if}
		{#if message}
			<p class="m-0 border-b border-sc-line px-3.5 py-2 text-[12px] text-sc-ink2" data-testid="decisions-message">{message}</p>
		{/if}
		{#if approvals.length === 0}
			<p class="m-0 px-3.5 py-3 text-[12px] text-sc-ink3">Nothing else is waiting.</p>
		{:else}
			<ul class="m-0 list-none divide-y divide-sc-line p-0">
				{#each approvals as a (a.id)}
					<li id="decision-{a.id}" class="px-3.5 py-2.5" data-testid="decision-item">
						<div class="flex flex-wrap items-start justify-between gap-x-3 gap-y-1.5">
							<div class="min-w-0 flex-1">
								<span class="block text-[12.5px] font-medium text-sc-ink">{headline(a)}</span>
								{#if a.reason}
									<p class="m-0 mt-0.5 line-clamp-2 text-[11.5px] text-sc-ink2" title={a.reason}>{a.reason}</p>
								{/if}
								<span class="block text-[11px] text-sc-ink3">
									{a.actor ? `Asked by ${a.actor}` : 'Asked'} {ago(a.created_at, Date.now())}{a.expires_at ? ` · expires ${new Date(a.expires_at).toLocaleDateString()}` : ''}
								</span>
							</div>
							<div class="flex flex-wrap gap-1.5">
								<button
									type="button"
									class="rounded border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink"
									aria-expanded={open.has(a.id)}
									on:click={() => toggle(a.id)}
								>{open.has(a.id) ? 'Hide details' : 'Details'}</button>
								<button
									type="button"
									class="rounded border border-[#7fb2ff]/45 px-2 py-0.5 text-[11px] text-[#a9cbff] hover:border-[#7fb2ff] hover:bg-[#7fb2ff]/10 disabled:opacity-50"
									disabled={busyId !== null}
									data-testid="decision-approve"
									on:click={() => onApprove(a)}
								>{busyId === a.id ? 'Working…' : approveLabel(a)}</button>
								<button
									type="button"
									class="rounded border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 hover:border-sc-ink hover:text-sc-ink disabled:opacity-50"
									disabled={busyId !== null}
									data-testid="decision-deny"
									on:click={() => onDeny(a)}
								>{denyLabel(a)}</button>
							</div>
						</div>
						{#if denyPickerId === a.id}
							<div class="mt-2">
								<DenyReasonPicker
									busy={busyId === a.id}
									on:confirm={(event) => { denyPickerId = null; void decide(a, 'deny', event.detail.reason); }}
									on:cancel={() => (denyPickerId = null)}
								/>
							</div>
						{/if}
						{#if open.has(a.id)}
							<div class="mt-2 text-xs">
								{#if a.approval_type === 'skill_update_proposal' && a.payload && typeof a.payload === 'object'}
									<SkillUpdateProposalCard payload={a.payload} />
								{:else if payloadRenderer(a.approval_type) && a.payload && typeof a.payload === 'object'}
									<svelte:component this={payloadRenderer(a.approval_type)} approval={a} />
								{:else}
									<pre class="max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-sc-line bg-sc-bg p-2 text-[11px] text-sc-ink2">{JSON.stringify(a.payload ?? {}, null, 2)}</pre>
								{/if}
								{#if a.target_type === 'strategy' && a.target_id}
									<a class="mt-1.5 inline-block text-[11px] text-sc-ink3 hover:text-sc-ink hover:underline" href={`/lab/strategy/${encodeURIComponent(a.target_id)}`}>Open strategy →</a>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</article>
{/if}

{#if goLive}
	<GoLiveApprovalDialog
		approval={goLive}
		busy={busyId === goLive.id}
		errorMessage={goLiveError}
		on:cancel={() => (goLive = null)}
		on:confirm={(event) => void confirmGoLive(event.detail.ceilingUsd)}
	/>
{/if}
