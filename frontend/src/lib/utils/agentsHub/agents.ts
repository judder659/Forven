// What each agent is for, what pausing it stops, and how its state reads in one
// line. The job descriptions follow forven/roster.py and the scheduler; the
// numbers always come from the fleet payload.

import type { AgentState, FleetAgent } from '$lib/api/agentsHub';
import type { Tone } from '$lib/utils/forge/status';
import { ago, elapsed } from '$lib/utils/forge/time';
import { modelOf, outageCover, providerOf, type FailoverContext, type OutageCover } from './failover';
import { plural } from './format';

export const BRAIN_ID = 'brain';

export interface AgentJob {
	/** Two-word job title for scanning the roster. */
	job: string;
	/** What stops when the operator pauses this agent. */
	pauseEffect: string;
}

const JOBS: Record<string, AgentJob> = {
	brain: {
		job: 'Orchestrator',
		pauseEffect: 'Brain cycles follow the autonomy mode (Manual · Semi · Auto) in the top bar, not this switch.',
	},
	'strategy-developer': {
		job: 'Strategy research',
		pauseEffect: 'New strategy creation stops. Creation runs keep queueing and wait until you resume.',
	},
	'simulation-agent': {
		job: 'Validation',
		pauseEffect: 'Walk-forward validation backtests stop, so strategies wait at the gauntlet until you resume.',
	},
	'risk-manager': {
		job: 'Risk oversight',
		pauseEffect: 'Scheduled risk audits stop. Trading does not: the scanner places and manages orders on its own.',
	},
	'quant-researcher': {
		job: 'Post-mortems',
		pauseEffect: 'Post-mortems of failed strategies and market research stop.',
	},
	'full-stack-engineer': {
		job: 'Diagnosis',
		pauseEffect: 'Read-only diagnoses of bugs, approvals and notifications stop.',
	},
};

export function agentJob(agentId: string): AgentJob {
	return JOBS[agentId] ?? { job: 'Custom agent', pauseEffect: 'This agent stops taking runs; anything queued waits.' };
}

/** The Brain's cycles are governed by the system autonomy mode, not the agent switch. */
export function canPause(agent: Pick<FleetAgent, 'id'>): boolean {
	return agent.id !== BRAIN_ID;
}

export interface StateMeta {
	label: string;
	tone: Tone;
}

export const STATE_META: Record<AgentState, StateMeta> = {
	running: { label: 'Working', tone: 'info' },
	queued: { label: 'Queued', tone: 'wait' },
	paused: { label: 'Paused', tone: 'caution' },
	idle: { label: 'Idle', tone: 'idle' },
};

const TYPE_LABELS: Record<string, [string, string]> = {
	generate_strategies: ['strategy creation', 'strategy creations'],
	develop_candidate: ['candidate build', 'candidate builds'],
	research: ['research run', 'research runs'],
	backtest: ['validation backtest', 'validation backtests'],
	risk_audit: ['risk audit', 'risk audits'],
	post_mortem: ['post-mortem', 'post-mortems'],
	analysis: ['analysis', 'analyses'],
	recall: ['memory recall', 'memory recalls'],
	brain_invoke: ['Brain cycle', 'Brain cycles'],
	execution: ['execution check', 'execution checks'],
	strategy_development: ['strategy build', 'strategy builds'],
};

export function typeLabel(type: string | null | undefined, count = 1): string {
	const key = String(type ?? '').trim().toLowerCase();
	const pair = TYPE_LABELS[key];
	if (pair) return count === 1 ? pair[0] : pair[1];
	return key ? key.replace(/_/g, ' ') : 'run';
}

/** "Working on “WFA: Validate S11287” · 3m", "2 queued · oldest 4m", "Idle · last run 12m ago". */
export function stateLine(agent: FleetAgent, now: number = Date.now()): string {
	if (agent.state === 'running' && agent.running.length > 0) {
		const run = agent.running[0];
		const extra = agent.running.length > 1 ? ` (+${agent.running.length - 1} more)` : '';
		const queued = agent.pending > 0 ? ` · ${agent.pending} queued` : '';
		return `Working on “${run.title}”${extra} · ${elapsed(run.started_at, now)}${queued}`;
	}
	if (agent.state === 'queued') {
		return `${plural(agent.pending, 'run')} queued${agent.oldest_pending_at ? ` · oldest ${ago(agent.oldest_pending_at, now)}` : ''}`;
	}
	if (agent.state === 'paused') {
		return agent.pending > 0 ? `Paused · ${plural(agent.pending, 'run')} waiting` : 'Paused';
	}
	if (!agent.last) return 'Idle · no runs yet';
	const noun = agent.id === BRAIN_ID ? 'cycle' : 'run';
	return `Idle · last ${noun} ${ago(agent.last.completed_at, now)}`;
}

const PROVIDER_LABELS: Record<string, string> = {
	openai: 'OpenAI',
	anthropic: 'Anthropic',
	minimax: 'MiniMax',
	zai: 'Z.AI',
	openrouter: 'OpenRouter',
	deepseek: 'DeepSeek',
	groq: 'Groq',
	gemini: 'Google Gemini',
	cerebras: 'Cerebras',
	mistral: 'Mistral',
	xai: 'xAI',
	together: 'Together AI',
	'opencode-zen': 'OpenCode Zen',
	'opencode-go': 'OpenCode GO',
	lmstudio: 'LM Studio',
	nvidia: 'NVIDIA',
};

export function providerLabel(provider: string | null | undefined): string {
	const key = String(provider ?? '').trim().toLowerCase();
	return PROVIDER_LABELS[key] ?? (key || 'No provider');
}

/** "MiniMax-M3" with its provider as a quieter second part. */
export function modelParts(agent: Pick<FleetAgent, 'model' | 'model_id'>): { model: string; provider: string } {
	return { model: agent.model_id || 'Default model', provider: providerLabel(agent.model) };
}

export interface FallbackNote {
	/** Two or three words for the roster. */
	short: string;
	text: string;
	/** Why it matters, for a tooltip. */
	help: string;
	tone: Tone;
	/** True when runs survive an outage of the model's provider. */
	covered: boolean;
}

/**
 * What happens to runs on `key` when its provider fails, in words. The Brain's
 * cycles have no fallbacks of their own, only the backup, so its wording names
 * the backup.
 */
export function coverNote(cover: OutageCover, key: string, brain: boolean): FallbackNote | null {
	const from = providerLabel(providerOf(key));
	const runs = brain ? 'Brain cycles' : 'runs';
	if (cover.via) {
		const to = providerLabel(providerOf(cover.via.key));
		const model = modelOf(cover.via.key);
		return cover.via.backup
			? { short: '→ backup', text: `falls back to the backup, ${model}`, help: `If ${from} fails, ${runs} move to the backup model on ${to}.`, tone: 'idle', covered: true }
			: { short: `→ ${model}`, text: `falls back to ${model}`, help: `If ${from} fails, ${runs} move to ${to}.`, tone: 'idle', covered: true };
	}
	const fix = 'Add a fallback or a backup model on another provider in Setup → Agent models.';
	const stops = brain ? 'Brain cycles stop' : 'this agent stops';
	switch (cover.gap) {
		case 'none':
			return brain
				? { short: 'no failover', text: 'no backup', help: `Brain cycles fall back only to the backup model, which is off, so if ${from} fails they stop. Set a backup on another provider in Setup → Agent models.`, tone: 'caution', covered: false }
				: { short: 'no failover', text: 'no fallback', help: `If ${from} fails, this agent’s runs fail. ${fix}`, tone: 'caution', covered: false };
		case 'same-provider':
			return brain
				? { short: 'no failover', text: `backup stays on ${from}`, help: `Brain cycles fall back only to the backup model, which is on ${from} too, so an outage there stops them. Set a backup on another provider in Setup → Agent models.`, tone: 'caution', covered: false }
				: { short: 'no failover', text: `fallback stays on ${from}`, help: `Everything it falls back to is on ${from} too, so an outage there stops this agent. ${fix}`, tone: 'caution', covered: false };
		case 'not-connected': {
			const blocked = cover.blocked!;
			const other = providerLabel(providerOf(blocked.key));
			return {
				short: 'no failover',
				text: `${other} not connected`,
				help: `${blocked.backup ? 'The backup model' : 'Its fallback'} is on ${other}, which is not connected, so if ${from} fails ${stops}. Connect ${other} under Setup → Providers.`,
				tone: 'caution',
				covered: false,
			};
		}
		default:
			return null;
	}
}

/**
 * What happens when this agent's provider fails: its `agent:<id>` fallbacks,
 * then the backup model. A fallback on the same provider, or on one that is not
 * connected, cannot carry a run through that outage, so it reads as a warning.
 * Brain cycles skip the agent chain and use only the backup. Null until the
 * policy, the backup and the connected providers have loaded.
 */
export function fallbackNote(agent: Pick<FleetAgent, 'id' | 'model' | 'model_id'>, context: FailoverContext | null | undefined): FallbackNote | null {
	if (!context) return null;
	const provider = String(agent.model ?? '').trim().toLowerCase();
	if (!provider) return null;
	const key = `${provider}:${agent.model_id ?? ''}`;
	const brain = agent.id === BRAIN_ID;
	const fallbacks = brain ? [] : context.chains[`agent:${agent.id}`] ?? [];
	return coverNote(outageCover(key, fallbacks, context.backup, context.connected), key, brain);
}
