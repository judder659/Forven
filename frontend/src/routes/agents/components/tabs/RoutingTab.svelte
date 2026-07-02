<script lang="ts">
	/**
	 * Routing & Fallbacks tab — the SINGLE place every model is selected.
	 *
	 * What lives here:
	 *  - Agents — EVERY agent (core + strategy-developers, including the Brain):
	 *    its model + ordered fallback chain. This is the only place an agent's
	 *    model is set; the Roster shows it read-only.
	 *  - 5 auxiliary task kinds: compression, recall, skill_extraction,
	 *    post_mortem, approval — lightweight Brain sub-task models.
	 *  - Backup provider — the global safety net when a slot's primary fails.
	 *  - Per-slot fallback chains + provider priority.
	 *
	 * The model-policy's `primary_provider`/`primary_model` (the default model for
	 * any slot with no explicit selection) is DERIVED from the Brain agent's
	 * current selection in the Agents section here — there is no duplicate primary
	 * picker. Whenever this tab writes the model-policy it carries the
	 * Brain-derived primary forward so the policy stays in sync.
	 *
	 * Every picker is constrained to CONNECTED providers + ENABLED models only
	 * (the page-wide safety invariant). An empty fallback list = "no fallback
	 * (fail closed)".
	 *
	 * Persistence (all in ONE save via the shared dirty-bar):
	 *  - Each changed agent's model → PATCH /api/agents/{id}/model
	 *    (updateForvenAgentModel).
	 *  - Each agent's fallback chain → model-policy fallback_chains under the slot
	 *    key `agent:<id>`.
	 *  - provider_priority + aux/backup fallback_chains (+ derived primary) → PUT
	 *    /api/model-policy (updateForvenModelPolicy) — one call.
	 *  - 5 auxiliary kinds → PUT /api/brain/auxiliary (updateBrainAuxiliary). The
	 *    backend stores a single {provider, model_id} per aux kind; the optional
	 *    per-slot fallback ordering is persisted alongside in model-policy's
	 *    fallback_chains under a slot-scoped key (`aux:<kind>` / `backup`) so the
	 *    intent round-trips even though the aux endpoint itself takes one model.
	 *  - Backup provider/model → updateSettingsSection('agents', …).
	 *
	 * Where a backend contract is uncertain we degrade gracefully (optional
	 * chaining + defaults) so the page never crashes.
	 */
	import { onMount } from 'svelte';
	import {
		updateForvenModelPolicy,
		updateForvenAgentModel,
		updateSettingsSection,
		getSettings,
		getForvenAgents,
		getBrainAuxiliary,
		updateBrainAuxiliary,
		type ForvenAgent,
		type ForvenProvider,
		type ForvenModelPolicyFallbackEntry,
		type ForvenModelPolicyUpdatePayload,
		type BrainAuxiliaryTaskKind,
		type BrainAuxiliaryEntry,
	} from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import { agentsConfig, selectableModelOptions } from '../agentsConfigStore';
	import ModelSlotPicker from './ModelSlotPicker.svelte';
	import DirtyBar from '../DirtyBar.svelte';

	// Notify the host page when this tab gains/loses unsaved edits, so it can
	// guard tab switches / navigation against silently discarding them (this tab
	// is destroyed on tab switch).
	export let onDirtyChange: ((dirty: boolean) => void) | undefined = undefined;

	$: policy = $agentsConfig.policy;
	$: selectable = $selectableModelOptions;

	// ---- key <-> {provider, model_id} helpers ----------------------------- //
	function toKey(provider: string | null | undefined, modelId: string | null | undefined): string {
		const p = String(provider ?? '').trim();
		const m = String(modelId ?? '').trim();
		return p && m ? `${p}:${m}` : '';
	}
	function fromKey(key: string): ForvenModelPolicyFallbackEntry | null {
		const sep = key.indexOf(':');
		if (sep <= 0) return null;
		return { provider: key.slice(0, sep), model_id: key.slice(sep + 1) };
	}
	function chainToKeys(chain: ForvenModelPolicyFallbackEntry[] | undefined): string[] {
		return (chain ?? []).map((e) => toKey(e.provider, e.model_id)).filter(Boolean);
	}
	function keysToChain(keys: string[]): ForvenModelPolicyFallbackEntry[] {
		return keys.map(fromKey).filter((e): e is ForvenModelPolicyFallbackEntry => e !== null);
	}

	function labelForOptionKey(key: string): string {
		const found = selectable.find((o) => o.key === key);
		if (found) return found.label;
		const sep = key.indexOf(':');
		return sep > 0 ? `${key.slice(0, sep)} / ${key.slice(sep + 1)}` : key;
	}

	// ---- Agents ------------------------------------------------------------ //
	// EVERY agent (core + strategy-developers, including the Brain) picks its
	// model + fallback chain here. The agent's model is persisted on the agent
	// row (PATCH /api/agents/{id}/model); its fallback chain rides in the
	// model-policy under the slot key `agent:<id>`.
	const BRAIN_AGENT_ID = 'brain';

	function agentSlotKey(id: string): string {
		return `agent:${id}`;
	}

	interface AgentRow {
		id: string;
		name: string;
		role: string;
	}

	let agentRows: AgentRow[] = [];
	let agentKey: Record<string, string> = {};
	let agentFallbacks: Record<string, string[]> = {};
	let agentDirty: Record<string, boolean> = {};
	// The model each agent had on load — used to decide whether to PATCH the row.
	let agentBaseKey: Record<string, string> = {};

	// ---- Brain-derived primary --------------------------------------------- //
	// The model-policy primary (default for any slot without an explicit model)
	// is DERIVED from the Brain agent's selection in the Agents section above.
	// We never expose a separate picker for it; we read the Brain's current
	// selection and carry it forward on every model-policy write.
	$: brainEntry = fromKey(agentKey[BRAIN_AGENT_ID] ?? '');
	$: brainProvider = brainEntry?.provider ?? '';
	$: brainModel = brainEntry?.model_id ?? '';
	$: brainKey = toKey(brainProvider, brainModel);
	$: brainModelLabel = brainKey ? labelForOptionKey(brainKey) : '';

	// ---- Auxiliary --------------------------------------------------------- //
	const AUX_KINDS: BrainAuxiliaryTaskKind[] = [
		'compression',
		'recall',
		'skill_extraction',
		'post_mortem',
		// `approval` is requested by the spec; the BrainAuxiliaryTaskKind union
		// in the client may not list it yet, so cast at the boundary.
		'approval' as BrainAuxiliaryTaskKind,
	];
	const AUX_LABELS: Record<string, string> = {
		compression: 'Compression',
		recall: 'Recall',
		skill_extraction: 'Skill extraction',
		post_mortem: 'Post-mortem',
		approval: 'Approval classifier',
	};
	const AUX_DESCRIPTIONS: Record<string, string> = {
		compression: 'Summarizes long context before it hits the primary reasoning model.',
		recall: 'Re-ranks FTS5 hits and writes the recall summary on /brain/recall.',
		skill_extraction: 'Distills successful trade patterns into reusable skill cards.',
		post_mortem: 'Writes the after-action analysis for completed decisions.',
		approval: 'Classifies pending approvals (auto-approve / escalate / hold).',
	};

	let auxKey: Record<string, string> = {};
	let auxFallbacks: Record<string, string[]> = {};
	let auxDirty: Record<string, boolean> = {};

	// ---- Backup ------------------------------------------------------------ //
	let backupKey = '';
	let backupFallbacks: string[] = [];
	let backupDirty = false;

	// One save for the whole tab — make many changes, then save once.
	let saving = false;
	$: anyDirty =
		agentRows.some((a) => agentDirty[a.id]) || AUX_KINDS.some((k) => auxDirty[k]) || backupDirty;
	// Surface the dirty state to the host so tab-switch / navigation can guard it.
	$: onDirtyChange?.(anyDirty);

	let loading = true;
	let loadError: string | null = null;

	function slotChainKey(kind: string): string {
		return `aux:${kind}`;
	}

	async function load() {
		loading = true;
		loadError = null;
		try {
			// Ensure the shared store is populated (providers/models/policy).
			if (!$agentsConfig.policy && !$agentsConfig.loading) {
				await agentsConfig.load();
			}
			const p = $agentsConfig.policy;

			const [auxRes, live, agentsRes] = await Promise.allSettled([
				getBrainAuxiliary(),
				getSettings(),
				getForvenAgents(),
			]);

			// Build the Agents section from EVERY agent (core + strategy
			// developers, including the Brain). Each agent's model comes from its
			// row; its fallback chain from policy.fallback_chains['agent:<id>'].
			// The Brain's selection here also derives the model-policy primary.
			if (agentsRes.status === 'fulfilled') {
				const list = (agentsRes.value ?? []).filter(
					(a): a is ForvenAgent => Boolean(String(a?.id ?? '').trim())
				);
				const rows: AgentRow[] = [];
				const nextKey: Record<string, string> = {};
				const nextBase: Record<string, string> = {};
				const nextFallbacks: Record<string, string[]> = {};
				for (const a of list) {
					const id = String(a.id ?? '').trim();
					if (!id) continue;
					const key = toKey(a.model, a.model_id);
					rows.push({
						id,
						name: String(a.name ?? id).trim() || id,
						role: String(a.role ?? '').trim(),
					});
					nextKey[id] = key;
					nextBase[id] = key;
					nextFallbacks[id] = chainToKeys(p?.fallback_chains?.[agentSlotKey(id)]);
				}
				// Brain first, then the rest in their returned order.
				rows.sort((x, y) => {
					const bx = x.id.toLowerCase() === BRAIN_AGENT_ID ? 0 : 1;
					const by = y.id.toLowerCase() === BRAIN_AGENT_ID ? 0 : 1;
					return bx - by;
				});
				agentRows = rows;
				agentKey = nextKey;
				agentBaseKey = nextBase;
				agentFallbacks = nextFallbacks;
			} else {
				agentRows = [];
				agentKey = {};
				agentBaseKey = {};
				agentFallbacks = {};
					loadError =
						agentsRes.reason instanceof Error ? agentsRes.reason.message : 'Failed to load agents';
			}

			if (auxRes.status === 'fulfilled') {
				const aux = auxRes.value.auxiliary ?? ({} as Record<string, BrainAuxiliaryEntry>);
				for (const kind of AUX_KINDS) {
					const entry = (aux as Record<string, BrainAuxiliaryEntry | undefined>)[kind];
					auxKey[kind] = toKey(entry?.provider, entry?.model_id);
					auxFallbacks[kind] = chainToKeys(p?.fallback_chains?.[slotChainKey(kind)]);
				}
				auxKey = { ...auxKey };
				auxFallbacks = { ...auxFallbacks };
			} else {
				// Aux fetch failed — reset to server-truth empties (mirror the agent
				// block) and surface the failure so a partial discard/load doesn't
				// leave stale local edits as the new baseline.
				const nextAuxKey: Record<string, string> = {};
				const nextAuxFallbacks: Record<string, string[]> = {};
				for (const kind of AUX_KINDS) {
					nextAuxKey[kind] = '';
					nextAuxFallbacks[kind] = [];
				}
				auxKey = nextAuxKey;
				auxFallbacks = nextAuxFallbacks;
				loadError = auxRes.reason instanceof Error ? auxRes.reason.message : 'Failed to load auxiliary models';
			}

			if (live.status === 'fulfilled') {
				const provider = String(live.value.backup_ai_provider ?? '').trim();
				const model = String(live.value.backup_ai_model ?? '').trim();
				backupKey = provider && provider !== 'none' ? toKey(provider, model) : '';
				backupFallbacks = chainToKeys(p?.fallback_chains?.['backup']);
			} else {
				// Backup fetch failed — reset to server-truth empties and surface it.
				backupKey = '';
				backupFallbacks = [];
				loadError = live.reason instanceof Error ? live.reason.message : 'Failed to load backup provider';
			}
			// Loading fresh state clears the dirty flags (also used by Discard).
			agentDirty = {};
			auxDirty = {};
			backupDirty = false;
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Failed to load routing';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	// ---- Save handlers ----------------------------------------------------- //
	/**
	 * Every model-policy write from this tab carries the Brain-derived primary
	 * forward (so the policy's primary stays in sync with the Roster) and keeps
	 * the Brain's provider at the head of provider_priority.
	 */
	function withDerivedPrimary(
		payload: ForvenModelPolicyUpdatePayload
	): ForvenModelPolicyUpdatePayload {
		const next: ForvenModelPolicyUpdatePayload = { ...payload };
		if (brainProvider && brainModel) {
			next.primary_provider = brainProvider;
			next.primary_model = brainModel;
			const existing = policy?.provider_priority ?? [];
			next.provider_priority = [brainProvider, ...existing.filter((p) => p !== brainProvider)];
		}
		return next;
	}

	/**
	 * Persist EVERY pending change on this tab in one go: each changed agent's
	 * model (one PATCH per agent), all auxiliary models (one /brain/auxiliary
	 * call), the backup provider/model, and all per-slot fallback chains —
	 * including every agent's chain under `agent:<id>` — in one /model-policy
	 * write. Make as many edits as you like, then save once.
	 */
	async function saveAll() {
		if (!anyDirty || saving) return;
		// Guard: if the shared model-policy never loaded, every per-slot fallback
		// chain was seeded empty (chainToKeys(undefined) === []). Persisting those
		// would clobber the server's real fallback_chains with empties as if they
		// were the loaded baseline. Bail with a clear error instead.
		if (!policy) {
			const why = $agentsConfig.error ? ` (${$agentsConfig.error})` : '';
			addToast(`Cannot save routing — the model policy failed to load${why}. Refresh and try again.`, 'error');
			return;
		}
		saving = true;
		try {
			// 1) Each changed agent's model → PATCH /api/agents/{id}/model. Only
			//    PATCH when the model actually changed from what we loaded and the
			//    new selection is a valid provider:model_id pair.
			for (const row of agentRows) {
				if (!agentDirty[row.id]) continue;
				const cur = agentKey[row.id] ?? '';
				if (cur === (agentBaseKey[row.id] ?? '')) continue;
				const entry = fromKey(cur);
				if (!entry) continue;
				await updateForvenAgentModel(row.id, {
					model: entry.provider as ForvenProvider,
					model_id: entry.model_id,
				});
			}

			// 2) All auxiliary models in a single call. A valid selection is sent as
			//    {provider, model_id}; a kind the operator CLEARED (auxKey === '')
			//    and changed is sent explicitly as {provider: null, model_id: null}
			//    so the backend can DELETE the slot (per the aux contract: a null/
			//    empty provider or model_id means "remove this slot"). Without this
			//    a cleared slot is omitted and can never be unset.
			const auxPayload: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>> = {};
			for (const kind of AUX_KINDS) {
				const entry = fromKey(auxKey[kind] ?? '');
				if (entry) {
					auxPayload[kind] = {
						provider: entry.provider,
						model_id: entry.model_id,
						base_url: null,
						api_key: null,
					};
				} else if (auxDirty[kind] && (auxKey[kind] ?? '') === '') {
					auxPayload[kind] = {
						provider: null,
						model_id: null,
						base_url: null,
						api_key: null,
					};
				}
			}
			if (Object.keys(auxPayload).length > 0) {
				await updateBrainAuxiliary(auxPayload);
			}

			// 3) Backup provider/model.
			const backupEntry = fromKey(backupKey);
			await updateSettingsSection('agents', {
				backup_ai_provider: backupEntry ? backupEntry.provider : 'none',
				backup_ai_model: backupEntry ? backupEntry.model_id : '',
			});

			// 4) All fallback chains (every agent slot + every aux slot + backup)
			//    in one policy write. The Brain-derived primary rides along.
			const fallback_chains: Record<string, ForvenModelPolicyFallbackEntry[]> = {
				...(policy?.fallback_chains ?? {}),
			};
			for (const row of agentRows) {
				fallback_chains[agentSlotKey(row.id)] = keysToChain(agentFallbacks[row.id] ?? []);
			}
			for (const kind of AUX_KINDS) {
				fallback_chains[slotChainKey(kind)] = keysToChain(auxFallbacks[kind] ?? []);
			}
			fallback_chains['backup'] = keysToChain(backupFallbacks);
			const updated = await updateForvenModelPolicy(withDerivedPrimary({ fallback_chains }));
			agentsConfig.setPolicy(updated);

			// Reflect the saved models as the new baseline and refresh the shared
			// store so the Roster shows the new models without a manual reload.
			agentBaseKey = { ...agentKey };
			agentDirty = {};
			auxDirty = {};
			backupDirty = false;
			void agentsConfig.load();
			addToast('Routing & fallbacks saved', 'success');
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to save routing', 'error');
		} finally {
			saving = false;
		}
	}

	async function discardAll() {
		// Re-load server state, dropping all pending edits.
		await load();
	}

	function onAgentChange(id: string, e: CustomEvent<{ value: string; fallbacks: string[] }>) {
		agentKey = { ...agentKey, [id]: e.detail.value };
		agentFallbacks = { ...agentFallbacks, [id]: e.detail.fallbacks };
		agentDirty = { ...agentDirty, [id]: true };
	}
	function onAuxChange(kind: string, e: CustomEvent<{ value: string; fallbacks: string[] }>) {
		auxKey = { ...auxKey, [kind]: e.detail.value };
		auxFallbacks = { ...auxFallbacks, [kind]: e.detail.fallbacks };
		auxDirty = { ...auxDirty, [kind]: true };
	}
	function onBackupChange(e: CustomEvent<{ value: string; fallbacks: string[] }>) {
		backupKey = e.detail.value;
		backupFallbacks = e.detail.fallbacks;
		backupDirty = true;
	}

	$: noneSelectable = selectable.length === 0;
</script>

<div class="space-y-6">
	<section class="terminal-card p-6 space-y-4">
		<header class="border-b border-[#1a1a1a] pb-2 flex items-start justify-between gap-3">
			<div>
				<h2 class="text-sm font-bold uppercase tracking-widest text-white">Routing &amp; Fallbacks</h2>
				<p class="text-xs text-[#666] mt-1">
					<span class="text-[#888]">Every agent's model — including the Brain's — is set
					<span class="text-white">here</span>, not on the Roster (the Roster shows it
					read-only).</span>
					Set each agent's model and fallback chain below, plus the auxiliary Brain sub-task
					models, the global backup, and per-slot fallback chains.
					Every picker is limited to <span class="text-emerald-400">connected providers</span> and
					<span class="text-emerald-400">enabled models</span>. An empty fallback list means
					<span class="text-yellow-400">no fallback (fail closed)</span>.
				</p>
			</div>
			<button
				type="button"
				on:click={load}
				disabled={loading}
				class="terminal-button text-xs px-2 py-1 disabled:opacity-60"
			>
				{loading ? 'Refreshing…' : 'Refresh'}
			</button>
		</header>

		{#if loadError}
			<p class="text-xs text-red-400" role="alert">{loadError}</p>
		{/if}

		{#if noneSelectable}
			<div class="border border-yellow-900 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-400">
				No connected provider has an enabled model yet. Connect a provider (Providers &amp; Keys)
				and enable at least one of its models (Models) before configuring routing.
			</div>
		{/if}
	</section>

	<!-- Agents — the single place every agent's model + fallback chain is set. -->
	<section class="terminal-card p-6 space-y-3">
		<h3 class="text-sm font-bold tracking-widest uppercase text-[#888]">Agents</h3>
		<p class="text-xs text-[#666]">
			Pick each agent's model and ordered fallback chain. This is the
			<span class="text-white">single place</span> an agent's model is set — the Roster shows
			it read-only. The <span class="text-[#ccc]">Brain's</span> selection also becomes the
			default model for any routing slot below with no explicit choice.
		</p>
		{#if agentRows.length === 0}
			<p class="text-xs text-[#555]">No agents found.</p>
		{:else}
			<ul class="space-y-3">
				{#each agentRows as agent (agent.id)}
					<ModelSlotPicker
						label={agent.id.toLowerCase() === BRAIN_AGENT_ID ? `${agent.name} (Brain · default model)` : agent.name}
						description=""
						value={agentKey[agent.id] ?? ''}
						fallbacks={agentFallbacks[agent.id] ?? []}
						{selectable}
						allowUnset
						dirty={Boolean(agentDirty[agent.id])}
						on:change={(e) => onAgentChange(agent.id, e)}
					/>
				{/each}
			</ul>
		{/if}
	</section>

	<!-- Default model (derived from the Brain's selection in Agents above) -->
	<section class="terminal-card p-6 space-y-2">
		<h3 class="text-sm font-bold tracking-widest uppercase text-[#888]">Default model</h3>
		<p class="text-xs text-[#666]">
			The fallback model for any slot below with no explicit selection. This is
			<span class="text-[#ccc]">not a separate setting</span> — it is derived from the
			<span class="text-white">Brain's model in the Agents section above</span> and saved
			automatically whenever you save here.
		</p>
		<div class="border border-[#1a1a1a] bg-[#050505] px-3 py-2 text-sm font-mono">
			{#if brainModelLabel}
				<span class="text-[#ccc]">{brainModelLabel}</span>
			{:else}
				<span class="text-yellow-400">Brain model not set — pick it in the Agents section above.</span>
			{/if}
		</div>
	</section>

	<!-- Auxiliary -->
	<section class="terminal-card p-6 space-y-3">
		<h3 class="text-sm font-bold tracking-widest uppercase text-[#888]">Auxiliary task models</h3>
		<p class="text-xs text-[#666]">
			Lightweight models for specific Brain sub-tasks. Each is independent of the default model.
		</p>
		<ul class="space-y-3">
			{#each AUX_KINDS as kind (kind)}
				<div class="space-y-2">
					<ModelSlotPicker
						label={AUX_LABELS[kind] ?? kind}
						description={AUX_DESCRIPTIONS[kind] ?? ''}
						value={auxKey[kind] ?? ''}
						fallbacks={auxFallbacks[kind] ?? []}
						{selectable}
						allowUnset
						dirty={Boolean(auxDirty[kind])}
						on:change={(e) => onAuxChange(kind, e)}
					/>
				</div>
			{/each}
		</ul>
	</section>

	<!-- Backup -->
	<section class="terminal-card p-6 space-y-3">
		<h3 class="text-sm font-bold tracking-widest uppercase text-[#888]">Backup provider</h3>
		<p class="text-xs text-[#666]">
			When a slot's primary credentials become unusable, calls fall back to this model instead of
			failing. Leave unset to disable backup — a credential problem then pauses the routine and alerts you.
		</p>
		<ul class="space-y-3">
			<ModelSlotPicker
				label="Backup model"
				description="Used when a primary provider goes down."
				value={backupKey}
				fallbacks={backupFallbacks}
				{selectable}
				allowUnset
				unsetLabel="None (disabled · fail closed)"
				dirty={backupDirty}
				on:change={onBackupChange}
			/>
		</ul>
	</section>

	<!-- One save for the whole tab — appears only when there are unsaved changes. -->
	<DirtyBar
		dirty={anyDirty}
		{saving}
		message="You have unsaved routing &amp; fallback changes."
		onSave={saveAll}
		onDiscard={discardAll}
	/>
</div>
