// Which model each agent, background task and the safety net runs on, and what
// each falls back to — loaded from four endpoints, edited as one draft, saved
// in one pass:
//  - an agent's model is PATCHed only when it changed;
//  - every set background-task model is sent, and one the operator cleared is
//    sent as {provider: null, model_id: null} so the backend drops the slot;
//  - the backup is written only when it changed, so a backup that follows its
//    provider's default model is not pinned by an unrelated save;
//  - one model-policy write carries every chain (`agent:<id>`, `aux:<kind>`)
//    and the Brain-derived primary, with the Brain's provider first in
//    provider_priority. A stored `backup` chain rides along untouched: nothing
//    at runtime reads it, so the page does not offer it.

import {
	getBrainAuxiliary,
	getForvenAgents,
	getSettings,
	updateBrainAuxiliary,
	updateForvenAgentModel,
	updateForvenModelPolicy,
	updateSettingsSection,
	type BrainAuxiliaryEntry,
	type BrainAuxiliaryTaskKind,
	type ForvenAgent,
	type ForvenModelPolicyFallbackEntry,
	type ForvenModelPolicyResponse,
	type ForvenModelPolicyUpdatePayload,
	type ForvenProvider,
} from '$lib/api';
import { backupKey } from './failover';

export const BRAIN_ID = 'brain';
export const AUX_KINDS: BrainAuxiliaryTaskKind[] = ['recall', 'skill_extraction', 'approval'];

export const AUX_META: Record<BrainAuxiliaryTaskKind, { label: string; help: string }> = {
	recall: { label: 'Memory recall', help: 'Re-ranks memory search hits and writes the recall summary.' },
	skill_extraction: { label: 'Skill extraction', help: 'Distils successful patterns into reusable skill cards.' },
	approval: { label: 'Approval classifier', help: 'Sorts pending approvals into auto-approve, escalate or hold.' },
};

/** One assignable slot: a "provider:model_id" key ('' = unset) and its ordered fallbacks. */
export interface Slot {
	key: string;
	fallbacks: string[];
}

export interface RoutingDraft {
	agents: Record<string, Slot>;
	aux: Record<BrainAuxiliaryTaskKind, Slot>;
	backup: Slot;
}

export interface RoutingAgent {
	id: string;
	name: string;
}

export interface RoutingSnapshot {
	agents: RoutingAgent[];
	draft: RoutingDraft;
	/** What failed to load, if anything; saving is refused while the policy is missing. */
	errors: string[];
}

export function toKey(provider: string | null | undefined, modelId: string | null | undefined): string {
	const p = String(provider ?? '').trim();
	const m = String(modelId ?? '').trim();
	return p && m ? `${p}:${m}` : '';
}

export function fromKey(key: string): ForvenModelPolicyFallbackEntry | null {
	const sep = key.indexOf(':');
	if (sep <= 0 || sep === key.length - 1) return null;
	return { provider: key.slice(0, sep), model_id: key.slice(sep + 1) };
}

export function providerOfKey(key: string): string {
	return fromKey(key)?.provider ?? '';
}

function chainToKeys(chain: ForvenModelPolicyFallbackEntry[] | undefined | null): string[] {
	return (chain ?? []).map((entry) => toKey(entry.provider, entry.model_id)).filter(Boolean);
}

function keysToChain(keys: string[]): ForvenModelPolicyFallbackEntry[] {
	return keys.map(fromKey).filter((entry): entry is ForvenModelPolicyFallbackEntry => entry !== null);
}

export function cloneDraft(draft: RoutingDraft): RoutingDraft {
	const copy = (slot: Slot): Slot => ({ key: slot.key, fallbacks: [...slot.fallbacks] });
	return {
		agents: Object.fromEntries(Object.entries(draft.agents).map(([id, slot]) => [id, copy(slot)])),
		aux: Object.fromEntries(AUX_KINDS.map((kind) => [kind, copy(draft.aux[kind])])) as Record<BrainAuxiliaryTaskKind, Slot>,
		backup: copy(draft.backup),
	};
}

/** Build the draft from what the server holds. The Brain comes first. */
export function draftFromServer(
	agents: ForvenAgent[],
	auxiliary: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>> | null,
	settings: { backup_ai_provider?: unknown; backup_ai_model?: unknown } | null,
	policy: ForvenModelPolicyResponse | null,
): { agents: RoutingAgent[]; draft: RoutingDraft } {
	const chains = policy?.fallback_chains ?? {};
	const rows = agents
		.map((agent) => ({ id: String(agent.id ?? '').trim(), name: String(agent.name ?? '').trim() }))
		.filter((agent) => agent.id)
		.map((agent) => ({ ...agent, name: agent.name || agent.id }))
		.sort((a, b) => Number(b.id === BRAIN_ID) - Number(a.id === BRAIN_ID));
	const byId = new Map(agents.map((agent) => [String(agent.id ?? '').trim(), agent]));
	const draftAgents: Record<string, Slot> = {};
	for (const row of rows) {
		const agent = byId.get(row.id);
		draftAgents[row.id] = { key: toKey(agent?.model, agent?.model_id), fallbacks: chainToKeys(chains[`agent:${row.id}`]) };
	}
	const aux = {} as Record<BrainAuxiliaryTaskKind, Slot>;
	for (const kind of AUX_KINDS) {
		const entry = auxiliary?.[kind];
		aux[kind] = { key: toKey(entry?.provider, entry?.model_id), fallbacks: chainToKeys(chains[`aux:${kind}`]) };
	}
	const backup: Slot = { key: backupKey(settings, policy?.default_models), fallbacks: chainToKeys(chains.backup) };
	return { agents: rows, draft: { agents: draftAgents, aux, backup } };
}

/** Load every assignment. The caller waits for the shared config store first. */
export async function loadRouting(policy: ForvenModelPolicyResponse | null): Promise<RoutingSnapshot> {
	const [agentsRes, auxRes, settingsRes] = await Promise.allSettled([getForvenAgents(), getBrainAuxiliary(), getSettings()]);
	const errors: string[] = [];
	if (!policy) errors.push('the model policy');
	if (agentsRes.status === 'rejected') errors.push('the agents');
	if (auxRes.status === 'rejected') errors.push('the background-task models');
	if (settingsRes.status === 'rejected') errors.push('the backup model');
	const built = draftFromServer(
		agentsRes.status === 'fulfilled' ? agentsRes.value ?? [] : [],
		auxRes.status === 'fulfilled' ? (auxRes.value.auxiliary ?? null) : null,
		settingsRes.status === 'fulfilled' ? (settingsRes.value as { backup_ai_provider?: unknown; backup_ai_model?: unknown }) : null,
		policy,
	);
	return { ...built, errors };
}

/** Who uses each model key, as a primary or a fallback: "Brain", "Memory recall", "Backup". */
export function modelUsage(draft: RoutingDraft, agents: RoutingAgent[]): Map<string, string[]> {
	const usage = new Map<string, string[]>();
	const note = (key: string, who: string) => {
		if (!key) return;
		const list = usage.get(key) ?? [];
		if (!list.includes(who)) list.push(who);
		usage.set(key, list);
	};
	for (const agent of agents) {
		const slot = draft.agents[agent.id];
		if (!slot) continue;
		note(slot.key, agent.name);
		for (const key of slot.fallbacks) note(key, `${agent.name} (fallback)`);
	}
	for (const kind of AUX_KINDS) {
		note(draft.aux[kind].key, AUX_META[kind].label);
		for (const key of draft.aux[kind].fallbacks) note(key, `${AUX_META[kind].label} (fallback)`);
	}
	note(draft.backup.key, 'Backup');
	return usage;
}

/** The same usage, rolled up per provider. */
export function providerUsage(draft: RoutingDraft, agents: RoutingAgent[]): Map<string, string[]> {
	const byProvider = new Map<string, string[]>();
	for (const [key, users] of modelUsage(draft, agents)) {
		const provider = providerOfKey(key);
		const list = byProvider.get(provider) ?? [];
		for (const who of users) {
			const plain = who.replace(/ \(fallback\)$/, '');
			if (!list.includes(plain)) list.push(plain);
		}
		byProvider.set(provider, list);
	}
	return byProvider;
}

export interface RoutingChange {
	/** Slot id: `agent:<id>`, `aux:<kind>` or `backup`. */
	slot: string;
	/** Who or what the slot is, in words. */
	who: string;
	what: 'model' | 'fallbacks';
	before: string;
	after: string;
}

function sameList(a: string[], b: string[]): boolean {
	return a.length === b.length && a.every((value, index) => value === b[index]);
}

/** Every difference between the saved and edited assignments, in page order. */
export function routingChanges(
	base: RoutingDraft,
	draft: RoutingDraft,
	agents: RoutingAgent[],
	label: (key: string) => string,
): RoutingChange[] {
	const changes: RoutingChange[] = [];
	const list = (keys: string[]) => (keys.length ? keys.map(label).join(' → ') : 'none');
	const compare = (slot: string, who: string, before: Slot | undefined, after: Slot | undefined, unset: string) => {
		if (!before || !after) return;
		if (before.key !== after.key) {
			changes.push({ slot, who, what: 'model', before: before.key ? label(before.key) : unset, after: after.key ? label(after.key) : unset });
		}
		if (!sameList(before.fallbacks, after.fallbacks)) {
			changes.push({ slot, who, what: 'fallbacks', before: list(before.fallbacks), after: list(after.fallbacks) });
		}
	};
	for (const agent of agents) compare(`agent:${agent.id}`, agent.name, base.agents[agent.id], draft.agents[agent.id], 'not set');
	for (const kind of AUX_KINDS) compare(`aux:${kind}`, AUX_META[kind].label, base.aux[kind], draft.aux[kind], 'built-in default');
	compare('backup', 'Backup model', base.backup, draft.backup, 'off');
	return changes;
}

function withDerivedPrimary(payload: ForvenModelPolicyUpdatePayload, brainKey: string, policy: ForvenModelPolicyResponse): ForvenModelPolicyUpdatePayload {
	const brain = fromKey(brainKey);
	if (!brain) return payload;
	const existing = policy.provider_priority ?? [];
	return {
		...payload,
		primary_provider: brain.provider,
		primary_model: brain.model_id,
		provider_priority: [brain.provider, ...existing.filter((provider) => provider !== brain.provider)],
	};
}

/** Persist the draft. Refuses without a loaded policy: saving then would wipe the stored chains. */
export async function saveRouting(
	base: RoutingDraft,
	draft: RoutingDraft,
	agents: RoutingAgent[],
	policy: ForvenModelPolicyResponse | null,
): Promise<ForvenModelPolicyResponse> {
	if (!policy) throw new Error('The model policy has not loaded, so saving would overwrite the stored fallback chains. Reload and try again.');

	for (const agent of agents) {
		const next = draft.agents[agent.id]?.key ?? '';
		if (next === (base.agents[agent.id]?.key ?? '')) continue;
		const entry = fromKey(next);
		if (!entry) continue;
		await updateForvenAgentModel(agent.id, { model: entry.provider as ForvenProvider, model_id: entry.model_id });
	}

	const auxPayload: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>> = {};
	for (const kind of AUX_KINDS) {
		const entry = fromKey(draft.aux[kind].key);
		if (entry) auxPayload[kind] = { provider: entry.provider, model_id: entry.model_id, base_url: null, api_key: null };
		else if (base.aux[kind].key) auxPayload[kind] = { provider: null, model_id: null, base_url: null, api_key: null };
	}
	if (Object.keys(auxPayload).length > 0) await updateBrainAuxiliary(auxPayload);

	if (draft.backup.key !== base.backup.key) {
		const backup = fromKey(draft.backup.key);
		await updateSettingsSection('agents', {
			backup_ai_provider: backup ? backup.provider : 'none',
			backup_ai_model: backup ? backup.model_id : '',
		});
	}

	const fallback_chains: Record<string, ForvenModelPolicyFallbackEntry[]> = { ...(policy.fallback_chains ?? {}) };
	for (const agent of agents) fallback_chains[`agent:${agent.id}`] = keysToChain(draft.agents[agent.id]?.fallbacks ?? []);
	for (const kind of AUX_KINDS) fallback_chains[`aux:${kind}`] = keysToChain(draft.aux[kind].fallbacks);
	return updateForvenModelPolicy(withDerivedPrimary({ fallback_chains }, draft.agents[BRAIN_ID]?.key ?? '', policy));
}
