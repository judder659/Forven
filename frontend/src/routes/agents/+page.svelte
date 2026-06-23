<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { get } from 'svelte/store';
	import { createPoller, type Poller } from '$lib/utils/polling';
	import { createRealtimeRefresh, type RealtimeRefreshController } from '$lib/utils/realtime';
	import { createPersistedStore } from '$lib/stores';
	import {
		createForvenStrategyDeveloperAgent,
		deleteForvenAgent,
		dismissForvenAgentTask,
		getForvenAgents,
		getForvenAgentTasks,
		getForvenSchedulerJobs,
		getForvenLogs,
		getForvenAgentTerminal,
		getForvenAgentModelOptions,
		getForvenModelPolicy,
		updateForvenAgent,
		updateForvenAgentModel,
		updateForvenSchedulerJob
	} from '$lib/api';
	import {
		listMCPServers,
		listMCPGrants,
		grantMCPServer,
		revokeMCPServer,
		type MCPServer,
		type MCPGrant,
	} from '$lib/api/mcp';
	import { addToast } from '$lib/stores/processTracker';
	import type { ForvenAgent, ForvenAgentTask, ForvenAgentModelOption, ForvenAgentUpdatePayload, ForvenModelPolicyResponse, ForvenProvider, ForvenSchedulerJob } from '$lib/api';
	import AgentSettingsDrawer from './components/AgentSettingsDrawer.svelte';
	import type { AgentHubSettings } from './components/agentHubSettings';
	import SchedulerJobRow from './components/SchedulerJobRow.svelte';
	import TaskQueuePanel from './components/TaskQueuePanel.svelte';

	type AgentProvider = ForvenProvider;

	interface AgentCard {
		id: string;
		name: string;
		modelLabel: string;
		modelProvider: AgentProvider;
		modelId: string;
		modelKey: string;
		icon: string;
		visibility: 'visible' | 'internal';
		role: string;
	}

	// Core agent IDs that must not be deleted. Backend enforces this too
	// (api_core._PROTECTED_AGENT_IDS); keep the two lists in sync.
	const protectedAgentIds = new Set<string>([
		'brain',
		'quant-researcher',
		'simulation-agent',
		'risk-manager',
		'execution-trader',
		'full-stack-engineer',
		'strategy-developer'
	]);

	function isStrategyDeveloper(card: AgentCard): boolean {
		if (String(card.role ?? '').trim().toLowerCase() === 'strategy-developer') return true;
		if (card.id === 'strategy-developer') return true;
		// User-created agents (anything not in the core protected set) are strategy developers by design.
		return !protectedAgentIds.has(card.id);
	}

	interface AgentModelPreset {
		key: string;
		label: string;
		provider: AgentProvider;
		modelId: string;
		enabled: boolean;
	}

	interface AgentLogEntry {
		ts: string;
		level: string;
		source: string;
		meta?: Record<string, unknown>;
		created_at: string;
		message: string;
	}

	interface RawAgentLogEntry {
		level?: unknown;
		source?: unknown;
		created_at?: unknown;
		ts?: unknown;
		message?: unknown;
		msg?: unknown;
		data?: unknown;
	}

	const defaultAgentHubSettings: AgentHubSettings = {
		pollInterval: 5000,
		taskQueueCount: 15,
		compactCards: false,
		dateFormat: 'absolute',
		accent: 'cyan',
		soundOnComplete: false,
		showInternalWorkers: true,
		showSchedulerErrors: false
	};
	const alwaysVisibleInternalAgentIds = new Set<string>([]);

	const agentHubSettings = createPersistedStore<AgentHubSettings>('agentHub.settings', defaultAgentHubSettings);

	const iconMap: Record<string, string> = {
		'quant-researcher': 'M9 3v7.19l-4.78 4.78C3.11 16.08 3.89 18 5.41 18h13.18c1.52 0 2.3-1.92 1.19-3.03L15 10.19V3h1c.55 0 1-.45 1-1s-.45-1-1-1H8c-.55 0-1 .45-1 1s.45 1 1 1h1zm4 0v7.87l4.95 4.95c.22.22.07.61-.25.61H6.3c-.32 0-.47-.39-.25-.61L11 10.87V3h2z',
		'simulation-agent': 'M11.2 2L1 14h8.3l-1.5 8L18 10h-8.3l1.5-8z',
		'backtest-engineer': 'M11.2 2L1 14h8.3l-1.5 8L18 10h-8.3l1.5-8z',
		'risk-manager': 'M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z',
		'execution-trader': 'M3.5 18.49l6-6.01 4 4L22 6.92l-1.41-1.41-7.09 7.97-4-4L2 16.99z',
		'full-stack-engineer': 'M4 3h16a1 1 0 011 1v16a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1zm1 2v14h14V5H5zm2 2h10v2H7V7zm0 3h10v2H7v-2zm0 3h10v2H7v-2zm0 3h6v2H7v-2z',
		'strategy-developer': 'M4.8 3.6l-2.1 2.1 8.6 8.6 2.4-2.4L4.8 3.6zm7.2 5.4l9 1.3-8.5 2.5 10.5 4.1-14.8 4.4z'
	};

	const fallbackAgentDefBase: Omit<AgentCard, 'modelLabel' | 'modelProvider' | 'modelId' | 'modelKey'>[] = [
		{
			id: 'quant-researcher',
			name: 'Quant Researcher',
			icon: iconMap['quant-researcher'],
			visibility: 'visible',
			role: 'quant-researcher'
		},
		{
			id: 'simulation-agent',
			name: 'Simulation Agent',
			icon: iconMap['simulation-agent'],
			visibility: 'visible',
			role: 'simulation-agent'
		},
		{
			id: 'risk-manager',
			name: 'Risk Manager',
			icon: iconMap['risk-manager'],
			visibility: 'visible',
			role: 'risk-manager'
		},
		{
			id: 'execution-trader',
			name: 'Execution Trader',
			icon: iconMap['execution-trader'],
			visibility: 'visible',
			role: 'execution-trader'
		},
		{
			id: 'full-stack-engineer',
			name: 'Full Stack Engineer',
			icon: iconMap['full-stack-engineer'],
			visibility: 'visible',
			role: 'full-stack-engineer'
		},
		{
			id: 'strategy-developer',
			name: 'Strategy Developer',
			icon: iconMap['strategy-developer'],
			visibility: 'visible',
			role: 'strategy-developer'
		},
		{
			id: 'brain',
			name: 'Brain',
			icon: iconMap['quant-researcher'],
			visibility: 'visible',
			role: 'brain'
		}
	];

	const staticModelPresetFallbacks: AgentModelPreset[] = [
		{ key: 'openai:codex-5.3-ultra', label: 'OpenAI Codex-5.3-Ultra', provider: 'openai', modelId: 'codex-5.3-ultra', enabled: true },
		{ key: 'openai:codex-5.3-extra-high', label: 'OpenAI Codex-5.3-Extra-High', provider: 'openai', modelId: 'codex-5.3-extra-high', enabled: true },
		{ key: 'openai:codex-5.3', label: 'OpenAI Codex-5.3', provider: 'openai', modelId: 'codex-5.3', enabled: true },
		{ key: 'openai:o1-mini', label: 'OpenAI O1-Mini', provider: 'openai', modelId: 'o1-mini', enabled: true },
		{ key: 'openai:gpt-4o', label: 'OpenAI GPT-4o', provider: 'openai', modelId: 'gpt-4o', enabled: true }
	];
	const requiredCoreAgentIds = fallbackAgentDefBase
		.filter((agent) => agent.visibility === 'visible')
		.map((agent) => agent.id);
	let modelPolicy: ForvenModelPolicyResponse | null = null;
	let modelPresets: AgentModelPreset[] = [];
	let fallbackModelPresets: AgentModelPreset[] = [];
	let agentModelOptions: ForvenAgentModelOption[] = [];

	let agents: ForvenAgent[] = [];
	$: agentNamesById = agents.reduce<Record<string, string>>((acc, agent) => {
		const id = String(agent?.id ?? '').trim();
		const name = String(agent?.name ?? '').trim();
		if (id && name) acc[id] = name;
		return acc;
	}, {});
	let agentTasks: ForvenAgentTask[] = [];
	let schedulerJobs: ForvenSchedulerJob[] = [];
	let logs: AgentLogEntry[] = [];
	let agentLogs: AgentLogEntry[] = [];
	let terminalLogs: AgentLogEntry[] = [];
	let loading = true;
	let selectedAgent: string | null = null;
	let terminalMemory = '';
	let terminalLogsLoaded = false;
	let terminalTab: 'memory' | 'logs' | 'mcp' = 'memory';
	let loadingTerminal = false;
	let mcpAllServers: MCPServer[] = [];
	let mcpGrants: MCPGrant[] = [];
	let mcpLoading = false;
	let mcpError = '';
	let mcpBusyServer = '';
	let savingModelAgentId: string | null = null;
	let modelErrors: Record<string, string> = {};
	let modelSelections: Record<string, string> = {};
	let showSettings = false;
	let completionSoundPrimed = false;
	let priorCompletedTaskKeys = new Set<string>();
	let lastTaskCompletionTick = 0;
	let clearingTaskErrors = false;
	let kpiTasksCompletedToday = 0;
	let kpiTasksErroredToday = 0;
	let kpiActiveAgents = 0;
	let kpiJobsDueNextHour = 0;
	let agentDefs: AgentCard[] = [];
	let displayedAgentDefs: AgentCard[] = [];
	let coreAgentCards: AgentCard[] = [];
	let strategyDeveloperCards: AgentCard[] = [];

	// Strategy Developer add/rename/remove state
	let addingDeveloper = false;
	let newDeveloperName = '';
	let newDeveloperModelKey = '';
	let submittingDeveloper = false;
	let addDeveloperError = '';
	let removingAgentId: string | null = null;
	let renameDrafts: Record<string, string> = {};
	let renameSavingId: string | null = null;
	let renameErrors: Record<string, string> = {};
	let editingAgentId: string | null = null;
	let editDraftName = '';
	let editDraftModelKey = '';
	let editDraftInstructions = '';
	let editSavingId: string | null = null;
	let editErrors: Record<string, string> = {};

	let dataRealtime: RealtimeRefreshController | null = null;
	let terminalRealtime: RealtimeRefreshController | null = null;
	let modelOptionsPoller: Poller | null = null;
	let taskPollingInterval = defaultAgentHubSettings.pollInterval;
	let savingJobs = new Set<string | number>();
	let fallbackAgentDefs: AgentCard[] = [];

	function isAgentProvider(value: string): value is AgentProvider {
		return (
			value === 'minimax' ||
			value === 'openai' ||
			value === 'lmstudio' ||
			value === 'zai' ||
			value === 'openrouter' ||
			value === 'anthropic' ||
			value === 'deepseek' ||
			value === 'groq' ||
			value === 'gemini'
		);
	}

	function providerLabel(provider: AgentProvider): string {
		if (provider === 'openai') return 'OpenAI';
		if (provider === 'minimax') return 'MiniMax';
		if (provider === 'zai') return 'Z.AI';
		if (provider === 'openrouter') return 'OpenRouter';
		if (provider === 'anthropic') return 'Anthropic';
		if (provider === 'deepseek') return 'DeepSeek';
		if (provider === 'groq') return 'Groq';
		if (provider === 'gemini') return 'Google Gemini';
		return 'LM Studio';
	}

	function resolveDefaultModelId(provider: AgentProvider): string {
		const policyModel = modelPolicy?.default_models?.[provider];
		if (typeof policyModel === 'string' && policyModel.trim()) {
			return policyModel.trim();
		}
		const firstEnabled = agentModelOptions.find((entry) => entry.provider === provider && entry.enabled);
		if (firstEnabled?.model_id) return firstEnabled.model_id;
		const fallbackOption = fallbackModelPresets.find((entry) => entry.provider === provider);
		if (fallbackOption?.modelId) return fallbackOption.modelId;
		const discoveredOption = agentModelOptions.find((option) => option.provider === provider);
		if (discoveredOption?.model_id) return discoveredOption.model_id;
		const fallbackChainHead = modelPolicy?.fallback_chains?.[provider]?.[0];
		if (fallbackChainHead?.model_id) return fallbackChainHead.model_id;
		return '';
	}

	function resolveDefaultModelPreset(provider: AgentProvider): AgentModelPreset | null {
		const modelId = resolveDefaultModelId(provider);
		if (!modelId) {
			return null;
		}
		return {
			key: `${provider}:${modelId}`,
			label: `${providerLabel(provider)} ${modelId}`,
			provider,
			modelId,
			enabled: true
		};
	}

	function buildDefaultModelPresets() {
		const resolved = new Map<string, AgentModelPreset>();
		for (const provider of ['openai', 'minimax', 'lmstudio', 'zai', 'openrouter', 'anthropic', 'deepseek', 'groq', 'gemini'] as AgentProvider[]) {
			const defaultPreset = resolveDefaultModelPreset(provider);
			if (defaultPreset) {
				resolved.set(defaultPreset.key, defaultPreset);
			}
		}
		for (const preset of staticModelPresetFallbacks) {
			resolved.set(preset.key, preset);
		}
		return Array.from(resolved.values());
	}

	function resolveFallbackAgentModel(): { provider: AgentProvider; modelId: string } {
		const openaiDefault = resolveDefaultModelId('openai');
		if (openaiDefault) {
			return { provider: 'openai', modelId: openaiDefault };
		}
		const minimaxDefault = resolveDefaultModelId('minimax');
		if (minimaxDefault) {
			return { provider: 'minimax', modelId: minimaxDefault };
		}
		const lmstudioDefault = resolveDefaultModelId('lmstudio');
		if (lmstudioDefault) {
			return { provider: 'lmstudio', modelId: lmstudioDefault };
		}
		const zaiDefault = resolveDefaultModelId('zai');
		if (zaiDefault) {
			return { provider: 'zai', modelId: zaiDefault };
		}
		return { provider: 'openai', modelId: '' };
	}

	function buildFallbackAgentDefs(): AgentCard[] {
		const fallbackModel = resolveFallbackAgentModel();
		const modelProvider = fallbackModel.provider;
		const modelId = fallbackModel.modelId;
		const label = modelId ? `${providerLabel(modelProvider)} ${modelId}` : providerLabel(modelProvider);
		const modelKeyValue = `${modelProvider}:${modelId}`;
		return fallbackAgentDefBase.map((agent) => ({
			...agent,
			modelProvider,
			modelId,
			modelLabel: label,
			modelKey: modelKeyValue
		}));
	}

	function looksLikeOpenAIModel(modelId: string): boolean {
		const lowered = String(modelId || '').trim().toLowerCase();
		if (!lowered) return false;
		if (lowered.startsWith('codex-')) return true;
		return lowered.includes('gpt') || lowered.startsWith('o1');
	}

	function looksLikeMinimaxModel(modelId: string): boolean {
		const lowered = String(modelId || '').trim().toLowerCase();
		if (!lowered) return false;
		return lowered.startsWith('minimax') || lowered.includes('minimax');
	}

	function inferAgentModelProvider(rawProvider: string, modelId: string): AgentProvider {
		const provider = String(rawProvider || '').trim().toLowerCase();
		if (isAgentProvider(provider)) return provider;
		if (looksLikeOpenAIModel(modelId)) return 'openai';
		if (looksLikeMinimaxModel(modelId)) return 'minimax';
		return 'openai';
	}

	function modelKey(provider: AgentProvider, modelId: string): string {
	return `${provider}:${modelId}`;
	}

	function normalizeAgentVisibility(value: unknown): 'visible' | 'internal' {
		return String(value ?? '').trim().toLowerCase() === 'internal' ? 'internal' : 'visible';
	}

	$: fallbackModelPresets = buildDefaultModelPresets();
	$: fallbackAgentDefs = buildFallbackAgentDefs();

	function inferAgentId(raw: string | number | undefined | null): string {
		const text = String(raw ?? '').trim().toLowerCase();
		if (!text) return '';
		return text
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/-+/g, '-')
			.replace(/^-|-$/g, '');
	}

	function displayFromAgentId(agentId: string): string {
		if (!agentId) return 'Agent';
		return String(agentId)
			.split('-')
			.filter(Boolean)
			.map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
			.join(' ');
	}

	function parseModelSelection(value: string): { provider: AgentProvider; modelId: string } | null {
		const sep = value.indexOf(':');
		if (sep <= 0) return null;
		const providerRaw = value.slice(0, sep).trim().toLowerCase();
		const modelId = value.slice(sep + 1).trim();
		if (!isAgentProvider(providerRaw) || modelId.length === 0) return null;
		return { provider: providerRaw, modelId };
	}

	function resolveAgentModel(agent: ForvenAgent): { provider: AgentProvider; modelId: string; key: string } {
		const providerRaw = String(agent.model ?? '').trim().toLowerCase();
		const provider = inferAgentModelProvider(providerRaw, String(agent.model_id ?? ''));
		const fallback = resolveFallbackAgentModel();
		const storedModelId = String(agent.model_id ?? '').trim();
		const modelId = storedModelId || resolveDefaultModelId(provider) || fallback.modelId;
		const displayModelId = modelId || fallback.modelId || 'default';
		return {
			provider,
			modelId: displayModelId,
			key: modelKey(provider, displayModelId)
		};
	}

	function labelForModel(provider: AgentProvider, modelId: string): string {
		const found = modelPresets.find((preset) => preset.provider === provider && preset.modelId === modelId);
		if (found) return found.label;
		return `${provider}/${modelId}`;
	}

	function optionsForAgent(agent: AgentCard): AgentModelPreset[] {
		const byKey = new Map(modelPresets.map((preset) => [preset.key, preset]));
		if (byKey.has(agent.modelKey)) {
			return [...modelPresets];
		}
		return [
			...modelPresets,
			{
				key: agent.modelKey,
				label: agent.modelLabel,
				provider: agent.modelProvider,
				modelId: agent.modelId,
				enabled: true
			}
		];
	}

	function selectedModelValue(agent: AgentCard): string {
		return modelSelections[agent.id] ?? agent.modelKey;
	}

	function clearModelSelection(agentId: string): void {
		const next = { ...modelSelections };
		delete next[agentId];
		modelSelections = next;
	}

	function clearModelError(agentId: string): void {
		if (!agentId) return;
		const next = { ...modelErrors };
		delete next[agentId];
		modelErrors = next;
	}

	async function dismissTaskAlert(task: ForvenAgentTask, silent = false): Promise<void> {
		const taskId = task.id;
		if (taskId === undefined || taskId === null) return;
		const source = String(task.source || 'agent_tasks').trim().toLowerCase() === 'tasks' ? 'tasks' : 'agent_tasks';
		const taskIdKey = String(taskId);
		try {
			await dismissForvenAgentTask(taskId, source);
			agentTasks = agentTasks.filter((entry) => {
				const entrySource = String(entry.source || 'agent_tasks').trim().toLowerCase() === 'tasks' ? 'tasks' : 'agent_tasks';
				const entryId = entry.id === undefined || entry.id === null ? '' : String(entry.id);
				return !(entrySource === source && entryId === taskIdKey);
			});
			if (!silent) {
				addToast('Task alert cleared', 'success');
			}
		} catch (err) {
			if (!silent) {
				const message = err instanceof Error ? err.message : 'Failed to clear task alert';
				addToast(message, 'error');
			}
		}
	}

	async function clearAllTaskAlerts(): Promise<void> {
		if (clearingTaskErrors) return;
		clearingTaskErrors = true;
		try {
			const failedTasks = agentTasks.filter((task) => isErrorTask(task));
			await Promise.all(failedTasks.map((task) => dismissTaskAlert(task, true)));
			modelErrors = {};
			await fetchData();
			addToast(`Cleared ${failedTasks.length} error alert${failedTasks.length === 1 ? '' : 's'}.`, 'success');
		} finally {
			clearingTaskErrors = false;
		}
	}

	function normalizeTaskKey(task: ForvenAgentTask): string {
		const agent = String(task.agent_id ?? '').trim();
		if (task.id !== undefined && task.id !== null) return `${agent}:${task.id}`;
		return `${agent}:${task.created_at ?? ''}:${task.title ?? task.type ?? 'task'}`;
	}

	function isCompletedTask(task: ForvenAgentTask): boolean {
		const status = (task.status ?? '').toLowerCase();
		return status === 'done' || status === 'completed' || status === 'reviewed';
	}

	function isErrorTask(task: ForvenAgentTask): boolean {
		const status = (task.status ?? '').toLowerCase();
		return status === 'error' || status === 'failed';
	}

	function handleTaskCompletionAlert(nextTasks: ForvenAgentTask[]) {
		const settingsValue = get(agentHubSettings);
		if (!settingsValue.soundOnComplete) {
			completionSoundPrimed = false;
			priorCompletedTaskKeys = new Set();
			return;
		}

		const completedNow = nextTasks
			.filter((task) => isCompletedTask(task))
			.map(normalizeTaskKey);

		if (!completionSoundPrimed) {
			priorCompletedTaskKeys = new Set(completedNow);
			completionSoundPrimed = true;
			return;
		}

		const hasNewCompleted = completedNow.some((id) => !priorCompletedTaskKeys.has(id));
		if (hasNewCompleted) playCompletionSound();
		priorCompletedTaskKeys = new Set(completedNow);
	}

	function playCompletionSound() {
		if (typeof window === 'undefined') return;
		const now = Date.now();
		if (now - lastTaskCompletionTick < 1000) return;
		lastTaskCompletionTick = now;

		try {
			const AudioContextCtor = window.AudioContext || (window as { webkitAudioContext?: typeof window.AudioContext }).webkitAudioContext;
			if (!AudioContextCtor) return;
			const context = new AudioContextCtor();
			const gain = context.createGain();
			const osc = context.createOscillator();
			gain.gain.value = 0.18;
			osc.type = 'triangle';
			osc.frequency.setValueAtTime(760, context.currentTime);
			osc.frequency.exponentialRampToValueAtTime(1120, context.currentTime + 0.15);
			osc.connect(gain).connect(context.destination);
			osc.start();
			osc.stop(context.currentTime + 0.35);
			gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.35);
			setTimeout(() => {
				void context.close();
			}, 450);
		} catch {
			// Ignore media playback errors from browser autoplay restrictions.
		}
	}

	function formatDateTime(iso?: string | null): string {
		if (!iso) return '--';
		const parsed = new Date(iso);
		if (Number.isNaN(parsed.getTime())) return '--';
		return parsed
			.toLocaleString('en-US', {
				month: 'short',
				day: 'numeric',
				hour: '2-digit',
				minute: '2-digit',
				hour12: false
			})
			.replace(',', '');
	}

	function formatRelativeTime(iso?: string | null): string {
		if (!iso) return '--';
		const parsed = Date.parse(iso);
		if (Number.isNaN(parsed)) return '--';
		const diff = Date.now() - parsed;
		const abs = Math.abs(diff);
		const direction = diff >= 0 ? 'ago' : 'from now';
		const minute = 60_000;
		const hour = 60 * minute;
		const day = 24 * hour;

		if (abs < minute) {
			const seconds = Math.max(1, Math.round(abs / 1000));
			return `${seconds}s ${direction}`;
		}
		if (abs < hour) {
			const minutes = Math.round(abs / minute);
			return `${minutes}m ${direction}`;
		}
		if (abs < day) {
			const hours = Math.round(abs / hour);
			return `${hours}h ${direction}`;
		}
		const days = Math.round(abs / day);
		return `${days}d ${direction}`;
	}

	function parseTime(raw?: string | null): number {
		if (!raw) return 0;
		const parsed = Date.parse(raw);
		return Number.isNaN(parsed) ? 0 : parsed;
	}

	function formatTaskDate(iso?: string | null): string {
		if ($agentHubSettings.dateFormat === 'relative') return formatRelativeTime(iso);
		return formatDateTime(iso);
	}

	function statusColor(status?: string | null): string {
		if (!status) return 'border-gray-800 text-gray-500';
		const value = status.toLowerCase();
		if (value === 'pending') return 'border-gray-500 text-gray-400';
		if (value === 'running') return 'border-yellow-500 text-yellow-500';
		if (value === 'done' || value === 'completed' || value === 'reviewed') return 'border-green-500 text-green-500';
		if (value === 'brain_invoke') return 'border-purple-500 text-purple-500';
		if (value === 'error' || value === 'failed') return 'border-red-500 text-red-500';
		return 'border-gray-800 text-gray-500';
	}

	function parseAgentStatus(task: ForvenAgentTask | undefined | null): string {
		return (task?.status ?? 'pending').toLowerCase();
	}

	function isTaskToday(task: ForvenAgentTask, matcher: (entry: ForvenAgentTask) => boolean): boolean {
		if (!matcher(task)) return false;
		const ts = parseTime(task.completed_at);
		if (!ts) return false;
		const today = new Date();
		const target = new Date(ts);
		return target.toDateString() === today.toDateString();
	}

	function countTasksCompletedToday(): number {
		return agentTasks.filter((task) => isTaskToday(task, isCompletedTask)).length;
	}

	function countTasksErroredToday(): number {
		return agentTasks.filter((task) => isTaskToday(task, isErrorTask)).length;
	}

	function countActiveAgents(): number {
		const latestByAgent = new Map<string, ForvenAgentTask>();
		for (const task of [...agentTasks].sort((a, b) => parseTime(b.created_at) - parseTime(a.created_at))) {
			const agentId = String(task.agent_id ?? '').trim();
			if (!agentId || latestByAgent.has(agentId)) continue;
			latestByAgent.set(agentId, task);
		}
		let count = 0;
		for (const task of latestByAgent.values()) {
			if (parseAgentStatus(task) === 'running') count += 1;
		}
		return count;
	}

	function countJobsDueNextHour(): number {
		const now = Date.now();
		const oneHour = 60 * 60 * 1000;
		return schedulerJobs.filter((job) => {
			if (job.enabled === false) return false;
			if (!job.next_run_at) return false;
			const parsed = parseTime(job.next_run_at);
			if (!parsed) return false;
			const diff = parsed - now;
			return diff >= 0 && diff <= oneHour;
		}).length;
	}

	$: kpiTasksCompletedToday = countTasksCompletedToday();
	$: kpiTasksErroredToday = countTasksErroredToday();
	$: kpiActiveAgents = countActiveAgents();
	$: kpiJobsDueNextHour = countJobsDueNextHour();

	function accentBorderClass(accent: AgentHubSettings['accent']): string {
		if (accent === 'green') return 'focus:border-green-500';
		if (accent === 'amber') return 'focus:border-amber-500';
		if (accent === 'rose') return 'focus:border-rose-500';
		return 'focus:border-cyan-500';
	}

	function toAgentCard(agent: ForvenAgent): AgentCard | null {
		const rawId = String((agent as { id?: string; agent_id?: string }).id ?? (agent as { id?: string; agent_id?: string }).agent_id ?? '').trim();
		const id = rawId || inferAgentId(agent.name ?? '');
		if (!id) return null;
		const resolved = resolveAgentModel(agent);
		const role = String((agent as { role?: string }).role ?? '').trim();
		return {
			id,
			name: String(agent.name ?? (id || 'Unknown Agent')),
			modelLabel: labelForModel(resolved.provider, resolved.modelId),
			modelProvider: resolved.provider,
			modelId: resolved.modelId,
			modelKey: resolved.key,
			icon: iconMap[id] || iconMap['strategy-developer'],
			visibility: normalizeAgentVisibility(agent.visibility),
			role
		};
	}

	function makeSyntheticAgentCard(agentId: string): AgentCard {
		const fallbackDef = fallbackAgentDefBase.find((agent) => agent.id === agentId);
		const fallback = resolveFallbackAgentModel();
		const label = fallback.modelId ? `${providerLabel(fallback.provider)} ${fallback.modelId}` : 'Model pending';
		return {
			id: agentId,
			name: fallbackDef?.name || displayFromAgentId(agentId),
			modelLabel: label,
			modelProvider: fallback.provider,
			modelId: fallback.modelId,
			modelKey: modelKey(fallback.provider, fallback.modelId || 'default'),
			icon: fallbackDef?.icon || iconMap[agentId] || iconMap['quant-researcher'],
			visibility: fallbackDef?.visibility || 'visible',
			role: fallbackDef?.role || ''
		};
	}

	function discoverAgentIdsFromLogs(runtimeLogs: AgentLogEntry[]): string[] {
		return runtimeLogs
			.map((entry) => {
				const source = String(entry.source ?? '').trim();
				if (!source) return '';
				if (source === 'brain') return 'brain';
				if (source.startsWith('agent:')) return source.slice(6).trim();
				return '';
			})
			.filter((agentId) => agentId.length > 0);
	}

	function mergeAgentCards(runtimeAgents: ForvenAgent[], runtimeTasks: ForvenAgentTask[], runtimeLogs: AgentLogEntry[]): AgentCard[] {
		const discovered = runtimeAgents
			.map((agent) => toAgentCard(agent))
			.filter((agent): agent is AgentCard => agent !== null && agent.id.length > 0);
		const discoveredById = new Map<string, AgentCard>(discovered.map((agent) => [agent.id, agent]));
		const taskAgentIds = runtimeTasks
			.map((task) => String(task.agent_id ?? '').trim())
			.filter((agentId) => agentId.length > 0);
		const logAgentIds = discoverAgentIdsFromLogs(runtimeLogs);
		const sourceAgentIds = [...taskAgentIds, ...logAgentIds]
			.filter((agentId) => agentId.length > 0)
			.filter((agentId) => !discoveredById.has(agentId))
			.filter((agentId, idx, arr) => arr.indexOf(agentId) === idx);

		const merged: AgentCard[] = [];
		const emitted = new Set<string>();

		for (const fallback of fallbackAgentDefs) {
			const match = discoveredById.get(fallback.id);
			if (match) {
				merged.push(match);
			} else {
				merged.push(fallback);
			}
			emitted.add(fallback.id);
		}

	for (const discoveredItem of discovered) {
		if (!emitted.has(discoveredItem.id)) {
			merged.push(discoveredItem);
			emitted.add(discoveredItem.id);
		}
	}

		for (const taskAgentId of sourceAgentIds) {
			if (!emitted.has(taskAgentId)) {
				merged.push(makeSyntheticAgentCard(taskAgentId));
				emitted.add(taskAgentId);
			}
		}

		for (const requiredAgentId of requiredCoreAgentIds) {
			if (emitted.has(requiredAgentId)) continue;
			const requiredFallback = fallbackAgentDefs.find((agent) => agent.id === requiredAgentId);
			if (requiredFallback) {
				merged.push(requiredFallback);
			} else {
				merged.push(makeSyntheticAgentCard(requiredAgentId));
			}
			emitted.add(requiredAgentId);
		}

		return merged;
	}

	function toPresetOption(raw: unknown): AgentModelPreset | null {
		if (!raw || typeof raw !== 'object') return null;
		const record = raw as Record<string, unknown>;
		const providerRaw = String(record.provider ?? '').trim().toLowerCase();
		if (!isAgentProvider(providerRaw)) return null;
		const modelId = String(record.model_id ?? '').trim();
		if (!modelId) return null;
		const key = String(record.key ?? '').trim() || modelKey(providerRaw, modelId);
		const label = String(record.label ?? '').trim() || labelForModel(providerRaw, modelId);
		return {
			key,
			label,
			provider: providerRaw,
			modelId,
			enabled: Boolean(
				(record as { enabled?: boolean }).enabled === undefined
					? true
					: (record as { enabled?: boolean }).enabled
			)
		};
	}

	function getAgentLastTask(agentId: string): ForvenAgentTask | null {
		return agentTasks.find((task) => task.agent_id === agentId) || null;
	}

	function normalizeTerminalLog(raw: unknown): AgentLogEntry | null {
		if (!raw || typeof raw !== 'object') return null;
		const entry = raw as Record<string, unknown>;
		const level = String(entry.level ?? 'info');
		const source = String(entry.source ?? '').trim();
		const createdAt = String((entry.created_at as string | number | null | undefined) ?? (entry.ts as string | number | null | undefined) ?? '');
		const message = String(entry.message ?? entry.msg ?? '');
		const meta = typeof entry.data === 'object' && entry.data !== null ? (entry.data as Record<string, unknown>) : undefined;

		return {
			ts: createdAt,
			level,
			source,
			created_at: createdAt,
			message,
			meta
		};
	}

	function normalizeLogRow(raw: unknown): AgentLogEntry | null {
		if (!raw || typeof raw !== 'object') return null;
		return normalizeTerminalLog({
			level: (raw as RawAgentLogEntry).level,
			source: (raw as RawAgentLogEntry).source,
			created_at: (raw as RawAgentLogEntry).created_at,
			ts: (raw as RawAgentLogEntry).ts,
			message: (raw as RawAgentLogEntry).message ?? (raw as RawAgentLogEntry).msg,
			data: (raw as RawAgentLogEntry).data
		});
	}

	function isLogForAgent(entry: AgentLogEntry, agentId: string): boolean {
		const normalizedAgent = String(agentId ?? '').trim().toLowerCase();
		if (!normalizedAgent) return false;
		const normalizedSource = String(entry.source ?? '').trim().toLowerCase();
		if (!normalizedSource) return false;
		return (
			normalizedSource === normalizedAgent ||
			normalizedSource === `agent:${normalizedAgent}` ||
			normalizedSource.endsWith(`:${normalizedAgent}`)
		);
	}

	function getFallbackAgentLogs(agentId: string): AgentLogEntry[] {
		return logs.filter((entry) => isLogForAgent(entry, agentId)).slice(0, 50);
	}

	function closeSelectedAgent() {
		selectedAgent = null;
		terminalMemory = '';
		terminalLogs = [];
		terminalLogsLoaded = false;
		agentLogs = [];
		terminalTab = 'memory';
		mcpAllServers = [];
		mcpGrants = [];
		mcpError = '';
		mcpBusyServer = '';
	}

	function handleInteractiveKeydown(event: KeyboardEvent, action: () => void) {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			action();
		}
	}

	function handleOpenAgent(agentId: string) {
		if (!agentId) return;
		selectedAgent = agentId;
		terminalMemory = '';
		terminalLogs = [];
		terminalLogsLoaded = false;
		agentLogs = [];
		terminalTab = 'memory';
	}

	function modelSelectId(id: string): string {
		return `agent-model-${id}`;
	}

	function mergeModelPresets(base: AgentModelPreset[], incoming: AgentModelPreset[]): AgentModelPreset[] {
		const byKey = new Map<string, AgentModelPreset>();
		for (const preset of base) {
			byKey.set(preset.key, preset);
		}
		for (const preset of incoming) {
			byKey.set(preset.key, preset);
		}
		return Array.from(byKey.values());
	}

	async function fetchModelOptions() {
		try {
			const [policyResponse, optionsResponse] = await Promise.allSettled([
				getForvenModelPolicy(),
				getForvenAgentModelOptions()
			]);
			if (policyResponse.status === 'fulfilled') {
				modelPolicy = policyResponse.value;
			}
			const normalized = optionsResponse.status === 'fulfilled' && Array.isArray(optionsResponse.value.options)
				? optionsResponse.value.options.map((entry) => toPresetOption(entry)).filter((entry): entry is AgentModelPreset => entry !== null)
				: [];
			if (normalized.length > 0) {
				modelPresets = mergeModelPresets(fallbackModelPresets, normalized);
			} else {
				modelPresets = [...fallbackModelPresets];
			}
		} catch (err) {
			console.error('Failed to fetch agent model options', err);
			modelPresets = [...fallbackModelPresets];
		}
	}

	async function fetchData() {
		try {
			const [agentsRes, tasksRes, jobsRes, logsRes] = await Promise.allSettled([
				getForvenAgents(),
				getForvenAgentTasks(),
				getForvenSchedulerJobs(),
				getForvenLogs(50)
			]);

			if (agentsRes.status === 'fulfilled') agents = agentsRes.value;
			if (jobsRes.status === 'fulfilled') schedulerJobs = jobsRes.value;
			if (tasksRes.status === 'fulfilled') {
				agentTasks = tasksRes.value;
				handleTaskCompletionAlert(tasksRes.value);
			}
			if (logsRes.status === 'fulfilled') {
				logs = logsRes.value
					.map(normalizeLogRow)
					.filter((entry): entry is AgentLogEntry => entry !== null);
			}
		} catch (err) {
			console.error('Failed to fetch agent data', err);
		} finally {
			loading = false;
		}
	}

	async function fetchTerminal() {
		if (!selectedAgent) return;
		const agentId = selectedAgent;
		loadingTerminal = true;
		terminalLogsLoaded = false;
		terminalMemory = '';
		terminalLogs = [];
		try {
			const data = await getForvenAgentTerminal(agentId);
			if (selectedAgent !== agentId) return;
			terminalMemory = typeof data.memory === 'string' ? data.memory : '';
			const rawLogs = Array.isArray(data.logs) ? data.logs : [];
			const normalized = rawLogs
				.map(normalizeTerminalLog)
				.filter((entry): entry is AgentLogEntry => entry !== null)
				.filter((entry) => entry.created_at || entry.message)
				.slice(0, 50);
			terminalLogs = normalized.length > 0 ? normalized : getFallbackAgentLogs(agentId);
			terminalLogsLoaded = true;
		} catch (err) {
			if (selectedAgent !== agentId) return;
			console.error('Failed to fetch terminal memory', err);
			terminalLogs = getFallbackAgentLogs(agentId);
			terminalLogsLoaded = true;
		} finally {
			if (selectedAgent === agentId) loadingTerminal = false;
		}
	}

	async function loadMCPGrantsView() {
		if (!selectedAgent) return;
		const agentId = selectedAgent;
		mcpLoading = true;
		mcpError = '';
		try {
			const [serversRes, grantsRes] = await Promise.all([
				listMCPServers(),
				listMCPGrants(agentId),
			]);
			if (selectedAgent !== agentId) return;
			mcpAllServers = serversRes.servers || [];
			mcpGrants = grantsRes.grants || [];
		} catch (err) {
			if (selectedAgent !== agentId) return;
			mcpError = err instanceof Error ? err.message : String(err);
			mcpAllServers = [];
			mcpGrants = [];
		} finally {
			if (selectedAgent === agentId) mcpLoading = false;
		}
	}

	async function handleGrantMCP(serverName: string) {
		if (!selectedAgent || !serverName) return;
		mcpBusyServer = serverName;
		try {
			await grantMCPServer(selectedAgent, serverName);
			await loadMCPGrantsView();
		} catch (err) {
			mcpError = err instanceof Error ? err.message : String(err);
		} finally {
			mcpBusyServer = '';
		}
	}

	async function handleRevokeMCP(serverName: string) {
		if (!selectedAgent || !serverName) return;
		mcpBusyServer = serverName;
		try {
			await revokeMCPServer(selectedAgent, serverName);
			await loadMCPGrantsView();
		} catch (err) {
			mcpError = err instanceof Error ? err.message : String(err);
		} finally {
			mcpBusyServer = '';
		}
	}

	$: if (selectedAgent && terminalTab === 'mcp' && !mcpLoading && mcpAllServers.length === 0 && !mcpError) {
		void loadMCPGrantsView();
	}

	async function handleModelChange(agent: AgentCard, event: Event) {
		const target = event.currentTarget as HTMLSelectElement;
		const requested = parseModelSelection(target.value);
		if (!requested) {
			clearModelSelection(agent.id);
			return;
		}
		if (requested.provider === agent.modelProvider && requested.modelId === agent.modelId) {
			clearModelSelection(agent.id);
			return;
		}

		modelSelections = { ...modelSelections, [agent.id]: modelKey(requested.provider, requested.modelId) };
		modelErrors = { ...modelErrors, [agent.id]: '' };
		savingModelAgentId = agent.id;

		try {
			const updated = await updateForvenAgentModel(agent.id, {
				model: requested.provider,
				model_id: requested.modelId
			});
			agents = agents.map((item) => {
				const id = String(item.id ?? '').trim();
				return id === agent.id ? { ...item, ...updated } : item;
			});
			clearModelSelection(agent.id);
			addToast(`${agent.name} model updated`, 'success');
		} catch (err) {
			clearModelSelection(agent.id);
			const message = err instanceof Error ? err.message : 'Failed to update model';
			modelErrors = {
				...modelErrors,
				[agent.id]: message
			};
			addToast(message, 'error');
		} finally {
			if (savingModelAgentId === agent.id) savingModelAgentId = null;
		}
	}

	function getRenameDraft(card: AgentCard): string {
		return renameDrafts[card.id] ?? card.name;
	}

	function setRenameDraft(agentId: string, value: string) {
		renameDrafts = { ...renameDrafts, [agentId]: value };
	}

	function clearRenameDraft(agentId: string) {
		const next = { ...renameDrafts };
		delete next[agentId];
		renameDrafts = next;
	}

	async function handleRenameCommit(card: AgentCard) {
		const draft = (renameDrafts[card.id] ?? '').trim();
		if (!draft || draft === card.name) {
			clearRenameDraft(card.id);
			return;
		}
		renameSavingId = card.id;
		renameErrors = { ...renameErrors, [card.id]: '' };
		try {
			const updated = await updateForvenAgent(card.id, { name: draft });
			agents = agents.map((item) => {
				const id = String(item.id ?? '').trim();
				return id === card.id ? { ...item, ...updated } : item;
			});
			clearRenameDraft(card.id);
			addToast(`Renamed to "${draft}"`, 'success');
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Failed to rename agent';
			renameErrors = { ...renameErrors, [card.id]: message };
			addToast(message, 'error');
		} finally {
			if (renameSavingId === card.id) renameSavingId = null;
		}
	}

	function openAddDeveloperForm() {
		addingDeveloper = true;
		addDeveloperError = '';
		newDeveloperName = '';
		const defaultPreset = modelPresets.find((preset) => preset.enabled) ?? modelPresets[0];
		newDeveloperModelKey = defaultPreset?.key ?? '';
	}

	function cancelAddDeveloperForm() {
		addingDeveloper = false;
		newDeveloperName = '';
		newDeveloperModelKey = '';
		addDeveloperError = '';
	}

	async function handleAddDeveloper() {
		const name = newDeveloperName.trim();
		if (!name) {
			addDeveloperError = 'Give the developer a name first.';
			return;
		}
		const parsed = parseModelSelection(newDeveloperModelKey);
		if (!parsed) {
			addDeveloperError = 'Pick a model for this developer.';
			return;
		}
		submittingDeveloper = true;
		addDeveloperError = '';
		try {
			const created = await createForvenStrategyDeveloperAgent({
				name,
				model: parsed.provider,
				model_id: parsed.modelId
			});
			const createdId = String((created as { id?: string }).id ?? '').trim();
			if (createdId) {
				agents = [...agents.filter((item) => String(item.id ?? '').trim() !== createdId), created];
			} else {
				await fetchData();
			}
			addToast(`Added strategy developer "${name}"`, 'success');
			cancelAddDeveloperForm();
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Failed to add developer';
			addDeveloperError = message;
		} finally {
			submittingDeveloper = false;
		}
	}

	async function handleRemoveDeveloper(card: AgentCard) {
		if (protectedAgentIds.has(card.id)) return;
		const confirmed = typeof window === 'undefined'
			? true
			: window.confirm(`Remove strategy developer "${card.name}"? This cannot be undone.`);
		if (!confirmed) return;
		removingAgentId = card.id;
		try {
			await deleteForvenAgent(card.id);
			agents = agents.filter((item) => String(item.id ?? '').trim() !== card.id);
			addToast(`Removed "${card.name}"`, 'success');
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Failed to remove developer';
			addToast(message, 'error');
		} finally {
			if (removingAgentId === card.id) removingAgentId = null;
		}
	}

	function openEditForm(card: AgentCard) {
		const existing = agents.find((item) => String(item.id ?? '').trim() === card.id);
		editingAgentId = card.id;
		editDraftName = card.name;
		editDraftModelKey = card.modelKey;
		editDraftInstructions = String(existing?.instructions ?? '');
		editErrors = { ...editErrors, [card.id]: '' };
	}

	function cancelEditForm() {
		editingAgentId = null;
		editDraftName = '';
		editDraftModelKey = '';
		editDraftInstructions = '';
	}

	async function handleEditCommit(card: AgentCard) {
		const name = editDraftName.trim();
		if (!name) {
			editErrors = { ...editErrors, [card.id]: 'Name is required.' };
			return;
		}
		const parsed = parseModelSelection(editDraftModelKey);
		if (!parsed) {
			editErrors = { ...editErrors, [card.id]: 'Pick a model.' };
			return;
		}
		editSavingId = card.id;
		editErrors = { ...editErrors, [card.id]: '' };
		try {
			const payload: ForvenAgentUpdatePayload = {
				name,
				model: parsed.provider,
				model_id: parsed.modelId,
				instructions: editDraftInstructions
			};
			const updated = await updateForvenAgent(card.id, payload);
			agents = agents.map((item) => {
				const id = String(item.id ?? '').trim();
				return id === card.id ? { ...item, ...updated } : item;
			});
			clearRenameDraft(card.id);
			clearModelSelection(card.id);
			addToast(`Updated "${name}"`, 'success');
			cancelEditForm();
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Failed to update developer';
			editErrors = { ...editErrors, [card.id]: message };
			addToast(message, 'error');
		} finally {
			if (editSavingId === card.id) editSavingId = null;
		}
	}

	async function handleSchedulerSave(
		jobId: string | number,
		scheduleType: string,
		scheduleExpr: string,
		enabled: boolean
	) {
		if (savingJobs.has(jobId)) return;
		savingJobs = new Set([...savingJobs, jobId]);
		try {
			const payloadExpr = scheduleType === 'interval' ? String(scheduleExpr).trim() : scheduleExpr.trim();
			const result = await updateForvenSchedulerJob(jobId, scheduleType, payloadExpr, enabled);
			if (!result?.ok) {
				throw new Error(result?.error || 'Failed to save scheduler job');
			}
			await fetchData();
			addToast('Scheduler job updated', 'success');
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Failed to save scheduler job';
			addToast(message, 'error');
			throw err;
		} finally {
			const next = new Set(savingJobs);
			next.delete(jobId);
			savingJobs = next;
		}
	}

	function startDataRealtime(intervalMs: number) {
		dataRealtime?.stop();
		dataRealtime = createRealtimeRefresh(fetchData, {
			fallbackMs: intervalMs,
			wsDebounceMs: 1000,
			wsEvents: ['task_queued', 'task_completed', 'task_failed', 'strategy_transition', 'strategy_promoted'],
		});
		dataRealtime.start();
	}

	onMount(() => {
		void fetchData();
		void fetchModelOptions();
		taskPollingInterval = $agentHubSettings.pollInterval;
		startDataRealtime(taskPollingInterval);
		modelOptionsPoller = createPoller(fetchModelOptions, 120000);
		modelOptionsPoller.start();
	});

	onDestroy(() => {
		dataRealtime?.stop();
		dataRealtime = null;
		terminalRealtime?.stop();
		terminalRealtime = null;
		if (modelOptionsPoller) modelOptionsPoller.stop();
	});

	$: {
		if (selectedAgent) {
			void fetchTerminal();
			terminalRealtime?.stop();
			terminalRealtime = createRealtimeRefresh(fetchTerminal, {
				fallbackMs: 45_000,
				wsDebounceMs: 1000,
				wsEvents: ['task_queued', 'task_completed', 'task_failed'],
			});
			terminalRealtime.start();
		} else if (terminalRealtime) {
			terminalRealtime.stop();
			terminalRealtime = null;
		}
	}

	$: if (dataRealtime && taskPollingInterval !== $agentHubSettings.pollInterval) {
		taskPollingInterval = $agentHubSettings.pollInterval;
		startDataRealtime(taskPollingInterval);
	}

	$: agentLogs = selectedAgent
		? terminalLogsLoaded
			? terminalLogs
			: getFallbackAgentLogs(selectedAgent)
		: [];

	$: agentDefs = mergeAgentCards(agents, agentTasks, logs);
	$: displayedAgentDefs = agentDefs.filter(
		(agent) =>
			$agentHubSettings.showInternalWorkers ||
			agent.visibility !== 'internal' ||
			alwaysVisibleInternalAgentIds.has(agent.id)
	);
	$: coreAgentCards = displayedAgentDefs.filter((card) => !isStrategyDeveloper(card));
	$: strategyDeveloperCards = displayedAgentDefs.filter((card) => isStrategyDeveloper(card));
</script>

<div class="h-full overflow-y-auto p-6 space-y-6">
	<div class="flex items-center gap-3 mb-2">
		<svg class="w-6 h-6 text-white" viewBox="0 0 24 24" fill="currentColor">
			<path d="M12 2a7 7 0 00-7 7v2H3v4h2v2a7 7 0 0014 0v-2h2v-4h-2V9a7 7 0 00-7-7zm-3 9V9a3 3 0 116 0v2H9zm3 8a3 3 0 01-3-3v-1h6v1a3 3 0 01-3 3z" />
		</svg>
		<h1 class="text-2xl font-bold tracking-tight">Agent Hub</h1>
		<span class="text-xs text-gray-500">({displayedAgentDefs.length} cards)</span>
		<a
			href="/settings#agents"
			class="px-2 py-1 border border-cyan-800 text-cyan-300 hover:bg-cyan-900/25 rounded whitespace-nowrap transition-colors text-xs uppercase"
			title="Open Main Settings"
			aria-label="Open main settings"
		>
			Main Settings
		</a>
		<div class="flex-1"></div>
		<button
			type="button"
			class="terminal-button-icon"
			on:click={() => (showSettings = true)}
			aria-label="Open agent hub settings"
			title="Settings"
		>
			<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<circle cx="12" cy="12" r="3"></circle>
				<path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 0010.4 19.4a1.65 1.65 0 00-1.51 1L8.5 20.47a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82A1.65 1.65 0 004.6 14.6V14A1.65 1.65 0 004.6 12.4a1.65 1.65 0 00-1.06-1.51L3.48 10.4A2 2 0 016.31 7.57l.06.06a1.65 1.65 0 001.82.33h.09A1.65 1.65 0 009.9 8.6V8a1.65 1.65 0 001.4-1l.06-.06a2 2 0 012.83 0l.06.06a1.65 1.65 0 001.4 1h.09a1.65 1.65 0 001.48 1.17h.19a1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V14.6A1.65 1.65 0 0019.4 15" />
			</svg>
		</button>
	</div>

	<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
		{#each coreAgentCards as agent}
			{@const lastTask = getAgentLastTask(agent.id)}
			<div
				class={`bg-[#111] border border-[#333] border-l-2 rounded-lg relative overflow-visible ${statusColor(lastTask?.status).split(' ')[0]}`}
			>
				<button
					type="button"
					class={`w-full hover:bg-[#1a1a1a] transition-colors group text-left ${$agentHubSettings.compactCards ? 'p-2' : 'p-4'}`}
					on:click={() => handleOpenAgent(agent.id)}
					on:keydown={(event) => handleInteractiveKeydown(event, () => handleOpenAgent(agent.id))}
				>
					<div class="flex items-center gap-2 font-bold text-sm text-gray-200 mb-1">
						<svg class="w-4 h-4 text-gray-400 group-hover:text-white transition-colors" viewBox="0 0 24 24" fill="currentColor">
							<path d={agent.icon} />
						</svg>
						{agent.name}
						{#if agent.visibility === 'internal'}
							<span class="ml-auto text-[9px] uppercase tracking-[0.2em] text-amber-400">Internal</span>
						{/if}
					</div>
					{#if !$agentHubSettings.compactCards}
						<div class="text-[10px] text-gray-500 uppercase tracking-widest mb-3">{agent.modelLabel}</div>
					{/if}
					{#if lastTask}
						<div class="text-xs text-gray-300 truncate mb-1" title={lastTask.title || lastTask.type}>
							{lastTask.title || lastTask.type}
						</div>
						<div class="flex items-center gap-2">
							<span class={`text-[10px] px-1.5 py-0.5 rounded border ${statusColor(lastTask.status ?? 'pending')} uppercase font-bold tracking-wider`}>
								{lastTask.status ?? 'pending'}
							</span>
							{#if lastTask.completed_at}
								<span class="text-[10px] text-gray-600">{formatTaskDate(lastTask.completed_at)}</span>
							{/if}
						</div>
					{:else}
						<div class="text-xs text-gray-600 uppercase tracking-widest font-bold">IDLE</div>
					{/if}
			</button>

				<div class="px-4 pb-4 border-t border-[#222]">
					<div class="pt-3">
						<label class="block text-[10px] text-gray-500 uppercase tracking-wider mb-1" for={modelSelectId(agent.id)}>
							Model
						</label>
						<select
							id={modelSelectId(agent.id)}
							class={`terminal-select ${accentBorderClass($agentHubSettings.accent)}`}
							value={selectedModelValue(agent)}
							on:change={(event) => handleModelChange(agent, event)}
							disabled={savingModelAgentId === agent.id}
						>
							{#each optionsForAgent(agent) as preset}
								<option value={preset.key} disabled={!preset.enabled}>
									{preset.label}{!preset.enabled ? ' (disabled)' : ''}
								</option>
							{/each}
						</select>
						{#if savingModelAgentId === agent.id}
							<div class="mt-1 text-[10px] text-cyan-400 uppercase tracking-wider">Saving...</div>
						{/if}
							{#if modelErrors[agent.id]}
								<div class="mt-1 text-[10px] text-red-400 flex items-center gap-2">
									<span>{modelErrors[agent.id]}</span>
									<button
										type="button"
										class="text-gray-500 hover:text-white"
										aria-label="Clear model error"
										on:click={() => clearModelError(agent.id)}
									>
										Clear
									</button>
								</div>
							{/if}
						</div>
					</div>
				</div>
			{/each}
		</div>

		<section class="space-y-3">
			<div class="flex items-center gap-3">
				<h2 class="text-sm font-bold tracking-widest uppercase text-gray-300">Strategy Developers</h2>
				<span class="text-xs text-gray-500">({strategyDeveloperCards.length})</span>
				<span class="text-[10px] text-gray-600 hidden md:inline">
					Each developer receives every research task — compare models side-by-side.
				</span>
			</div>
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
				{#each strategyDeveloperCards as agent (agent.id)}
					{@const lastTask = getAgentLastTask(agent.id)}
					{@const canRemove = !protectedAgentIds.has(agent.id)}
					{@const isEditing = editingAgentId === agent.id}
					<div
						class={`bg-[#111] border border-[#333] border-l-2 rounded-lg relative overflow-visible ${statusColor(lastTask?.status).split(' ')[0]}`}
					>
						<div class={`${$agentHubSettings.compactCards ? 'p-2' : 'p-4'}`}>
							<div class="flex items-center gap-2 text-sm text-gray-200 mb-2">
								<svg class="w-4 h-4 text-gray-400" viewBox="0 0 24 24" fill="currentColor">
									<path d={agent.icon} />
								</svg>
								<input
									class="flex-1 min-w-0 bg-transparent border-b border-transparent focus:border-cyan-600 focus:outline-none text-sm font-bold text-gray-100 px-1 py-0.5 disabled:opacity-60"
									type="text"
									value={getRenameDraft(agent)}
									aria-label={`Rename ${agent.name}`}
									on:input={(event) => setRenameDraft(agent.id, (event.currentTarget as HTMLInputElement).value)}
									on:blur={() => handleRenameCommit(agent)}
									on:keydown={(event) => { if (event.key === 'Enter') (event.currentTarget as HTMLInputElement).blur(); }}
									disabled={renameSavingId === agent.id || isEditing}
								/>
								<button
									type="button"
									class="text-gray-500 hover:text-cyan-300 px-1 disabled:opacity-40"
									aria-label={`Edit ${agent.name}`}
									title={isEditing ? 'Close editor' : 'Edit developer'}
									on:click={() => (isEditing ? cancelEditForm() : openEditForm(agent))}
									disabled={editSavingId === agent.id}
								>
									<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
										<path d="M12 20h9" />
										<path d="M16.5 3.5a2.121 2.121 0 113 3L7 19l-4 1 1-4 12.5-12.5z" />
									</svg>
								</button>
								{#if canRemove}
									<button
										type="button"
										class="text-gray-500 hover:text-red-400 px-1 disabled:opacity-40"
										aria-label={`Remove ${agent.name}`}
										title="Remove developer"
										on:click={() => handleRemoveDeveloper(agent)}
										disabled={removingAgentId === agent.id}
									>
										{#if removingAgentId === agent.id}
											<span class="text-[10px] uppercase">...</span>
										{:else}
											<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
												<path d="M3 6h18" />
												<path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2" />
												<path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
											</svg>
										{/if}
									</button>
								{:else}
									<span class="text-[9px] uppercase tracking-widest text-gray-600" title="Built-in developer">Core</span>
								{/if}
							</div>
							{#if renameErrors[agent.id]}
								<div class="text-[10px] text-red-400 mb-2">{renameErrors[agent.id]}</div>
							{/if}
							{#if isEditing}
								<form
									class="space-y-2 mb-3 border border-cyan-900/60 rounded p-2 bg-[#0d0d0d]"
									on:submit|preventDefault={() => handleEditCommit(agent)}
								>
									<label class="block text-[10px] text-gray-500 uppercase tracking-wider">
										Name
										<input
											type="text"
											class="terminal-input mt-1 w-full"
											bind:value={editDraftName}
											disabled={editSavingId === agent.id}
											maxlength="60"
										/>
									</label>
									<label class="block text-[10px] text-gray-500 uppercase tracking-wider">
										Model
										<select
											class={`terminal-select mt-1 ${accentBorderClass($agentHubSettings.accent)}`}
											bind:value={editDraftModelKey}
											disabled={editSavingId === agent.id}
										>
											{#each optionsForAgent(agent) as preset}
												<option value={preset.key} disabled={!preset.enabled}>
													{preset.label}{!preset.enabled ? ' (disabled)' : ''}
												</option>
											{/each}
										</select>
									</label>
									<label class="block text-[10px] text-gray-500 uppercase tracking-wider">
										Instructions
										<textarea
											class="terminal-input mt-1 w-full text-xs font-mono"
											rows="3"
											bind:value={editDraftInstructions}
											disabled={editSavingId === agent.id}
											placeholder="Optional system-prompt guidance"
										></textarea>
									</label>
									{#if editErrors[agent.id]}
										<div class="text-[10px] text-red-400">{editErrors[agent.id]}</div>
									{/if}
									<div class="flex gap-2">
										<button
											type="submit"
											class="px-2 py-0.5 text-[10px] uppercase tracking-wider border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30 disabled:opacity-50"
											disabled={editSavingId === agent.id}
										>
											{editSavingId === agent.id ? 'Saving...' : 'Save'}
										</button>
										<button
											type="button"
											class="px-2 py-0.5 text-[10px] uppercase tracking-wider border border-[#333] text-gray-400 hover:text-white"
											on:click={cancelEditForm}
											disabled={editSavingId === agent.id}
										>
											Cancel
										</button>
									</div>
								</form>
							{/if}
							<button
								type="button"
								class="w-full text-left text-[10px] uppercase tracking-widest text-gray-500 hover:text-gray-300"
								on:click={() => handleOpenAgent(agent.id)}
							>
								Open terminal
							</button>
							{#if lastTask}
								<div class="text-xs text-gray-300 truncate mt-2" title={lastTask.title || lastTask.type}>
									{lastTask.title || lastTask.type}
								</div>
								<div class="flex items-center gap-2 mt-1">
									<span class={`text-[10px] px-1.5 py-0.5 rounded border ${statusColor(lastTask.status ?? 'pending')} uppercase font-bold tracking-wider`}>
										{lastTask.status ?? 'pending'}
									</span>
									{#if lastTask.completed_at}
										<span class="text-[10px] text-gray-600">{formatTaskDate(lastTask.completed_at)}</span>
									{/if}
								</div>
							{:else}
								<div class="text-xs text-gray-600 uppercase tracking-widest font-bold mt-2">IDLE</div>
							{/if}
						</div>
						<div class="px-4 pb-4 border-t border-[#222]">
							<div class="pt-3">
								<label class="block text-[10px] text-gray-500 uppercase tracking-wider mb-1" for={modelSelectId(agent.id)}>
									Model
								</label>
								<select
									id={modelSelectId(agent.id)}
									class={`terminal-select ${accentBorderClass($agentHubSettings.accent)}`}
									value={selectedModelValue(agent)}
									on:change={(event) => handleModelChange(agent, event)}
									disabled={savingModelAgentId === agent.id}
								>
									{#each optionsForAgent(agent) as preset}
										<option value={preset.key} disabled={!preset.enabled}>
											{preset.label}{!preset.enabled ? ' (disabled)' : ''}
										</option>
									{/each}
								</select>
								{#if savingModelAgentId === agent.id}
									<div class="mt-1 text-[10px] text-cyan-400 uppercase tracking-wider">Saving...</div>
								{/if}
								{#if modelErrors[agent.id]}
									<div class="mt-1 text-[10px] text-red-400 flex items-center gap-2">
										<span>{modelErrors[agent.id]}</span>
										<button
											type="button"
											class="text-gray-500 hover:text-white"
											aria-label="Clear model error"
											on:click={() => clearModelError(agent.id)}
										>
											Clear
										</button>
									</div>
								{/if}
							</div>
						</div>
					</div>
				{/each}

				{#if addingDeveloper}
					<form
						class="bg-[#0d0d0d] border border-dashed border-cyan-800 rounded-lg p-4 flex flex-col gap-3"
						on:submit|preventDefault={handleAddDeveloper}
					>
						<div class="text-[10px] uppercase tracking-widest text-cyan-300">New Developer</div>
						<label class="block text-[10px] text-gray-500 uppercase tracking-wider" for="new-dev-name">
							Name
							<input
								id="new-dev-name"
								type="text"
								class="terminal-input mt-1 w-full"
								bind:value={newDeveloperName}
								placeholder="e.g. Two"
								disabled={submittingDeveloper}
								maxlength="60"
								autofocus
							/>
						</label>
						<label class="block text-[10px] text-gray-500 uppercase tracking-wider" for="new-dev-model">
							Model
							<select
								id="new-dev-model"
								class={`terminal-select mt-1 ${accentBorderClass($agentHubSettings.accent)}`}
								bind:value={newDeveloperModelKey}
								disabled={submittingDeveloper}
							>
								{#each modelPresets as preset}
									<option value={preset.key} disabled={!preset.enabled}>
										{preset.label}{!preset.enabled ? ' (disabled)' : ''}
									</option>
								{/each}
							</select>
						</label>
						{#if addDeveloperError}
							<div class="text-[10px] text-red-400">{addDeveloperError}</div>
						{/if}
						<div class="flex gap-2">
							<button
								type="submit"
								class="px-3 py-1 text-xs uppercase tracking-wider border border-cyan-700 text-cyan-200 hover:bg-cyan-900/30 disabled:opacity-50"
								disabled={submittingDeveloper}
							>
								{submittingDeveloper ? 'Adding...' : 'Add'}
							</button>
							<button
								type="button"
								class="px-3 py-1 text-xs uppercase tracking-wider border border-[#333] text-gray-400 hover:text-white"
								on:click={cancelAddDeveloperForm}
								disabled={submittingDeveloper}
							>
								Cancel
							</button>
						</div>
					</form>
				{:else}
					<button
						type="button"
						class="bg-[#0d0d0d] border border-dashed border-[#333] hover:border-cyan-700 hover:text-cyan-200 text-gray-500 rounded-lg p-4 flex flex-col items-center justify-center min-h-[160px] transition-colors"
						on:click={openAddDeveloperForm}
					>
						<svg class="w-6 h-6 mb-2" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
							<line x1="12" y1="5" x2="12" y2="19" />
							<line x1="5" y1="12" x2="19" y2="12" />
						</svg>
						<span class="text-xs uppercase tracking-widest">New Strategy Developer</span>
					</button>
				{/if}
			</div>
		</section>

		<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
		<div class="bg-[#111] border border-[#333] rounded-lg p-4">
			<div class="text-xs text-gray-500 uppercase tracking-wider mb-1">Tasks Today</div>
			<div class="text-2xl font-bold text-green-400">{kpiTasksCompletedToday}</div>
		</div>
			<div class="bg-[#111] border border-[#333] rounded-lg p-4">
				<div class="text-xs text-gray-500 uppercase tracking-wider mb-1 flex items-center justify-between gap-2">
					<span>Errors</span>
					<button
						type="button"
						class="text-[10px] text-red-300 hover:text-red-200 disabled:opacity-50"
						on:click={clearAllTaskAlerts}
						disabled={clearingTaskErrors}
					>
						{clearingTaskErrors ? 'Clearing...' : 'Clear Errors'}
					</button>
				</div>
				<div class="text-2xl font-bold text-red-500">{kpiTasksErroredToday}</div>
			</div>
		<div class="bg-[#111] border border-[#333] rounded-lg p-4">
			<div class="text-xs text-gray-500 uppercase tracking-wider mb-1">Active Agents</div>
			<div class="text-2xl font-bold text-cyan-400">{kpiActiveAgents}</div>
		</div>
		<div class="bg-[#111] border border-[#333] rounded-lg p-4">
			<div class="text-xs text-gray-500 uppercase tracking-wider mb-1">Jobs Due 1h</div>
			<div class="text-2xl font-bold text-amber-400">{kpiJobsDueNextHour}</div>
		</div>
	</div>

	<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
		<TaskQueuePanel
			tasks={agentTasks}
			visibleCount={$agentHubSettings.taskQueueCount}
			dateFormat={$agentHubSettings.dateFormat}
			accentColor={$agentHubSettings.accent}
			agentNamesById={agentNamesById}
			onAgentClick={handleOpenAgent}
			onDismissTask={dismissTaskAlert}
		/>

		<div class="bg-[#111] border border-[#333] rounded-lg flex flex-col">
			<div class="px-4 py-3 border-b border-[#333]">
				<h3 class="font-bold text-sm text-gray-300 uppercase tracking-wider">Scheduler Jobs</h3>
			</div>
			<div class="overflow-x-auto">
				<table class="w-full text-left text-xs">
					<thead>
						<tr class="border-b border-[#222] text-gray-500 uppercase tracking-wider">
							<th class="px-4 py-2 font-medium">Name</th>
							<th class="px-4 py-2 font-medium">Schedule</th>
							<th class="px-4 py-2 font-medium">Next Run</th>
							<th class="px-4 py-2 font-medium">Status</th>
							<th class="px-4 py-2 font-medium">Enabled</th>
						</tr>
					</thead>
					<tbody class="divide-y divide-[#222]">
						{#each schedulerJobs as job}
							<SchedulerJobRow
								job={job}
								onSave={handleSchedulerSave}
								showErrors={$agentHubSettings.showSchedulerErrors}
							/>
						{:else}
							<tr><td colspan="5" class="px-4 py-4 text-center text-gray-500">No jobs configured</td></tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	</div>
</div>

{#if selectedAgent}
	<div
		class="fixed inset-0 bg-black/80 backdrop-blur-sm z-[100] flex items-center justify-center p-4 md:p-8"
		role="button"
		tabindex="0"
		aria-label="Close agent terminal"
		on:click={closeSelectedAgent}
		on:keydown={(event) => handleInteractiveKeydown(event, closeSelectedAgent)}
	>
		<!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
		<!-- svelte-ignore a11y-click-events-have-key-events -->
		<div
			class="bg-[#111] border border-[#333] rounded-lg shadow-2xl w-full max-w-4xl max-h-full flex flex-col overflow-hidden"
			role="dialog"
			aria-modal="true"
			tabindex="-1"
			on:click|stopPropagation
		>
			<div class="px-4 py-3 border-b border-[#333] flex items-center justify-between bg-[#1a1a1a]">
				<div class="flex items-center gap-2 text-white font-bold">
					<svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
						<path d={iconMap[selectedAgent] || iconMap['quant-researcher']} />
					</svg>
					{agentDefs.find((agent) => agent.id === selectedAgent)?.name || selectedAgent} Terminal
				</div>
				<button
					class="text-gray-500 hover:text-white p-1 transition-colors"
					aria-label="Close terminal"
					title="Close terminal"
					on:click={closeSelectedAgent}
				>
					<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<line x1="18" y1="6" x2="6" y2="18"></line>
						<line x1="6" y1="6" x2="18" y2="18"></line>
					</svg>
				</button>
			</div>

			<div class="flex border-b border-[#333] bg-[#0a0a0a]">
				<button
					class={`px-6 py-2 text-sm font-bold tracking-wider uppercase transition-colors ${terminalTab === 'memory' ? 'text-green-400 border-b-2 border-green-400 bg-[#111]' : 'text-gray-500 hover:text-gray-300'}`}
					on:click={() => (terminalTab = 'memory')}
				>
					Memory
				</button>
				<button
					class={`px-6 py-2 text-sm font-bold tracking-wider uppercase transition-colors ${terminalTab === 'logs' ? 'text-green-400 border-b-2 border-green-400 bg-[#111]' : 'text-gray-500 hover:text-gray-300'}`}
					on:click={() => (terminalTab = 'logs')}
				>
					Logs
				</button>
				<button
					class={`px-6 py-2 text-sm font-bold tracking-wider uppercase transition-colors ${terminalTab === 'mcp' ? 'text-green-400 border-b-2 border-green-400 bg-[#111]' : 'text-gray-500 hover:text-gray-300'}`}
					on:click={() => (terminalTab = 'mcp')}
				>
					MCP
				</button>
			</div>

			<div class="p-4 overflow-y-auto flex-1 min-h-[300px] max-h-[600px] font-mono text-xs leading-relaxed text-gray-300 whitespace-pre-wrap break-words">
				{#if terminalTab === 'mcp'}
					{@const grantedNames = new Set(mcpGrants.map((g) => g.server_name))}
					{@const ungranted = mcpAllServers.filter((s) => !grantedNames.has(s.name))}
					<div class="space-y-4 whitespace-normal">
						<div class="flex items-center justify-between">
							<h3 class="text-xs uppercase tracking-wider text-gray-400">
								Granted MCP servers ({mcpGrants.length})
							</h3>
							<button
								type="button"
								class="text-[11px] px-2 py-1 rounded border border-[#333] text-gray-300 hover:text-white hover:border-[#555] disabled:opacity-50"
								on:click={() => void loadMCPGrantsView()}
								disabled={mcpLoading}
							>
								{mcpLoading ? 'Loading…' : 'Refresh'}
							</button>
						</div>
						{#if mcpError}
							<p class="text-xs text-red-400">{mcpError}</p>
						{/if}
						{#if mcpLoading && mcpGrants.length === 0 && mcpAllServers.length === 0}
							<p class="text-xs text-gray-500">Loading MCP servers…</p>
						{:else if mcpGrants.length === 0}
							<p class="text-xs text-gray-500">
								No MCP servers granted. The agent can only call tools from servers explicitly granted below.
							</p>
						{:else}
							<ul class="space-y-1.5">
								{#each mcpGrants as grant (grant.server_name)}
									<li class="flex items-center justify-between bg-[#0d0d0d] border border-[#222] rounded px-3 py-2">
										<div>
											<div class="font-mono text-gray-200">{grant.server_name}</div>
											{#if grant.granted_at}
												<div class="text-[10px] text-gray-500">
													granted {grant.granted_at}{grant.granted_by ? ` by ${grant.granted_by}` : ''}
												</div>
											{/if}
										</div>
										<button
											type="button"
											class="text-[11px] px-2 py-1 rounded border border-red-900 text-red-300 hover:text-red-200 hover:bg-red-950/40 disabled:opacity-50"
											on:click={() => void handleRevokeMCP(grant.server_name)}
											disabled={mcpBusyServer === grant.server_name}
										>
											{mcpBusyServer === grant.server_name ? '…' : 'Revoke'}
										</button>
									</li>
								{/each}
							</ul>
						{/if}

						<div class="border-t border-[#222] pt-3">
							<h3 class="text-xs uppercase tracking-wider text-gray-400 mb-2">
								Available servers ({ungranted.length})
							</h3>
							{#if mcpAllServers.length === 0 && !mcpLoading}
								<p class="text-xs text-gray-500">
									No MCP servers configured. Add one at <a href="/integrations/mcp" class="text-blue-400 hover:underline">Integrations → MCP</a>.
								</p>
							{:else if ungranted.length === 0}
								<p class="text-xs text-gray-500">
									All configured servers are already granted to this agent.
								</p>
							{:else}
								<ul class="space-y-1.5">
									{#each ungranted as srv (srv.name)}
										<li class="flex items-center justify-between bg-[#0d0d0d] border border-[#222] rounded px-3 py-2">
											<div>
												<div class="font-mono text-gray-200">{srv.name}</div>
												<div class="text-[10px] text-gray-500">
													{srv.transport} · {srv.enabled ? 'enabled' : 'disabled'}
													{#if !srv.enabled}<span class="text-amber-400"> · grant will work but no tools register until enabled</span>{/if}
												</div>
											</div>
											<button
												type="button"
												class="text-[11px] px-2 py-1 rounded border border-blue-900 text-blue-300 hover:text-blue-200 hover:bg-blue-950/40 disabled:opacity-50"
												on:click={() => void handleGrantMCP(srv.name)}
												disabled={mcpBusyServer === srv.name}
											>
												{mcpBusyServer === srv.name ? '…' : 'Grant'}
											</button>
										</li>
									{/each}
								</ul>
							{/if}
						</div>
					</div>
				{:else if loadingTerminal && !terminalMemory && agentLogs.length === 0}
					<div class="text-gray-500 animate-pulse">Loading...</div>
				{:else if terminalTab === 'memory'}
					{#if terminalMemory}
						{terminalMemory}
					{:else}
						<div class="text-gray-500">No memory found for today.</div>
					{/if}
				{:else}
					{#each agentLogs as log}
						<div class="mb-1 hover:bg-[#222] -mx-2 px-2 py-0.5 rounded transition-colors">
							<span class="text-gray-500">[{formatDateTime(log.created_at)}]</span>
							<span class={log.level === 'error' ? 'text-red-500 font-bold' : ''}> {log.message}</span>
						</div>
					{:else}
						<div class="text-gray-500">No recent logs.</div>
					{/each}
				{/if}
			</div>
		</div>
	</div>
{/if}

{#if showSettings}
	<AgentSettingsDrawer settings={agentHubSettings} on:close={() => (showSettings = false)} />
{/if}
