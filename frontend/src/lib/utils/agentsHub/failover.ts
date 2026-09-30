// Whether a run survives an outage of its model's provider, judged the way the
// runner builds a run's chain (forven/agents/runner.py): the model, then its own
// fallbacks on connected providers, then the backup model when the backup's
// provider is connected and differs from the model's. The Brain is no
// exception: its cycles run with agent_id "brain" (forven/runtime_worker.py).
// Nothing at runtime reads a fallback list for the backup itself.

type ChainEntry = { provider?: string | null; model_id?: string | null };

export interface FailoverContext {
	/** Stored fallback chains as "provider:model_id" keys, per slot (`agent:<id>`, ...). */
	chains: Record<string, string[]>;
	/** The backup model key, '' when the backup is off. */
	backup: string;
	/** Connected provider ids, lower-case. */
	connected: Set<string>;
}

export interface CoverStep {
	key: string;
	/** True when this is the global backup rather than one of the run's own fallbacks. */
	backup: boolean;
}

export interface OutageCover {
	/** Where runs go when the model's provider fails; null when nowhere. */
	via: CoverStep | null;
	/** Why there is nowhere to go. */
	gap: 'no-model' | 'none' | 'same-provider' | 'not-connected' | null;
	/** For 'not-connected': the fallback or backup that would cover it once its provider is connected. */
	blocked: CoverStep | null;
}

export function providerOf(key: string): string {
	const sep = key.indexOf(':');
	return sep > 0 ? key.slice(0, sep).trim().toLowerCase() : '';
}

export function modelOf(key: string): string {
	return key.slice(key.indexOf(':') + 1);
}

export function outageCover(key: string, fallbacks: string[], backup: string, connected: Set<string>): OutageCover {
	const provider = providerOf(key);
	if (!provider) return { via: null, gap: 'no-model', blocked: null };
	const steps: CoverStep[] = [...fallbacks.map((entry) => ({ key: entry, backup: false })), ...(backup ? [{ key: backup, backup: true }] : [])];
	const elsewhere = steps.filter((step) => providerOf(step.key) && providerOf(step.key) !== provider);
	const via = elsewhere.find((step) => connected.has(providerOf(step.key))) ?? null;
	if (via) return { via, gap: null, blocked: null };
	if (elsewhere.length > 0) return { via: null, gap: 'not-connected', blocked: elsewhere[0] };
	return { via: null, gap: steps.length === 0 ? 'none' : 'same-provider', blocked: null };
}

/**
 * The backup as a model key, '' when it is off. A backup with no pinned model
 * runs on its provider's default model, so it is on, not off.
 */
export function backupKey(
	settings: { backup_ai_provider?: unknown; backup_ai_model?: unknown } | null | undefined,
	defaultModels: Record<string, string> | null | undefined,
): string {
	const provider = String(settings?.backup_ai_provider ?? '').trim().toLowerCase();
	if (!provider || provider === 'none') return '';
	const model = String(settings?.backup_ai_model ?? '').trim() || String(defaultModels?.[provider] ?? '').trim();
	return model ? `${provider}:${model}` : '';
}

export function failoverContext(
	chains: Record<string, ChainEntry[] | null | undefined> | null | undefined,
	backup: string,
	connected: Iterable<string>,
): FailoverContext {
	const keys: Record<string, string[]> = {};
	for (const [slot, chain] of Object.entries(chains ?? {})) {
		keys[slot] = (chain ?? [])
			.map((entry) => {
				const provider = String(entry?.provider ?? '').trim().toLowerCase();
				const model = String(entry?.model_id ?? '').trim();
				return provider && model ? `${provider}:${model}` : '';
			})
			.filter(Boolean);
	}
	return { chains: keys, backup, connected: new Set([...connected].map((id) => String(id).toLowerCase())) };
}
