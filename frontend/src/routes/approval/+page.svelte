<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		approveApproval,
		bulkApproveApprovals,
		classifyApproval,
		denyApproval,
		getApprovalContext,
		getApprovalModes,
		getApprovals,
		getSettings,
		reviseApproval,
		troubleshootApproval,
		updateSettingsSection,
		userCompleteApproval,
		type ApprovalContextResponse,
		type ApprovalRecord,
		type ApprovalTaskDetail,
		type ApprovalTaskSummary,
	} from '$lib/api/forven';
	import SkillUpdateProposalCard from '$lib/components/approvals/SkillUpdateProposalCard.svelte';
	import { friendlyTitle, isStrategyLifecycleType, payloadRenderer } from '$lib/components/approvals/renderers';

	type PendingDecision = 'approve' | 'deny' | 'revise';
	type ViewMode = 'pending' | 'history';
	type DrawerTab = 'diagnosis' | 'execution';
	type TaskLogEntry = { title: string; summary: string; detail: string; timestamp: string | null; error?: boolean };
	type TroubleshootReport = {
		summary: string;
		rootCause: string;
		evidence: string[];
		affectedFiles: string[];
		recommendedFix: string[];
		validationPlan: string[];
		riskLevel: string;
		confidence: string;
	};

	let approvals: ApprovalRecord[] = [];
	let loading = true;
	let refreshing = false;
	let error: string | null = null;
	let actionMessage: string | null = null;
	let busyApprovals = new Set<number>();
	let reviseInput: Record<number, string> = {};
	let viewMode: ViewMode = 'pending';
	let autoApproveCodeEdits = false;
	let autoApprovePromotions = false;
	let settingsLoading = true;
	let approvalModes: Record<string, string> = {};
	let defaultApprovalMode = '';

	let filterType = '';
	let filterText = '';
	let collapsedGroups: Set<string> = new Set();
	let denyPickerId: number | null = null;
	let denyPreset = '';
	let denyFreeText = '';
	const DENY_PRESETS = ['Performing fine', 'Insufficient evidence', 'Wrong target stage'];

	let selectedApprovalId: number | null = null;
	let approvalContext: ApprovalContextResponse | null = null;
	let drawerTab: DrawerTab = 'diagnosis';
	let contextLoading = false;
	let contextRefreshing = false;
	let contextError: string | null = null;
	let launchingTroubleshootApprovalId: number | null = null;
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let wsCleanup: (() => void) | null = null;

	const isBusy = (approvalId: number) => busyApprovals.has(approvalId);
	const isLaunchingTroubleshoot = (approvalId: number) => launchingTroubleshootApprovalId === approvalId;
	const taskStatus = (task?: ApprovalTaskSummary | null) => String(task?.status || 'pending').toLowerCase();
	const approvalStatus = (approval?: ApprovalRecord | null) => String(approval?.status || 'pending_approval').toLowerCase();
	const activeTask = (task?: ApprovalTaskSummary | null) => ['pending', 'running', 'blocked'].includes(taskStatus(task));
	const taskLabel = (task?: ApprovalTaskSummary | null) => task?.display_id || (task ? `Task #${task.id}` : 'Not linked');
	const taskDetailUrl = (task?: ApprovalTaskSummary | null) => task?.display_id ? `/tasks/${encodeURIComponent(task.display_id)}?returnTo=${encodeURIComponent('/approval')}` : '';

	function setBusy(approvalId: number, busy: boolean) {
		const next = new Set(busyApprovals);
		if (busy) next.add(approvalId);
		else next.delete(approvalId);
		busyApprovals = next;
	}

	function parseDate(value: string | null | undefined): number {
		if (!value) return 0;
		const parsed = new Date(value);
		return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
	}

	function fmtDate(value: unknown): string {
		if (!value) return '--';
		const date = new Date(String(value));
		if (Number.isNaN(date.getTime())) return '--';
		return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
	}

	function fmtAge(value: string | null | undefined): string {
		const ts = parseDate(value);
		if (!ts) return '--';
		const seconds = Math.max(0, Math.round((Date.now() - ts) / 1000));
		if (seconds < 60) return `${seconds}s`;
		if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
		if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
		return `${Math.round(seconds / 86400)}d`;
	}

	function compact(value: unknown, maxLen = 120): string {
		const text = String(value ?? '').trim();
		if (!text) return '--';
		return text.length <= maxLen ? text : `${text.slice(0, maxLen)}...`;
	}

	function pretty(value: unknown): string {
		if (value === null || value === undefined) return '--';
		if (typeof value === 'string') return value.trim() || '--';
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	function statusClass(status: string): string {
		switch (String(status || '').toLowerCase()) {
			case 'running':
				return 'text-[#888] border-[#333] bg-[#111]';
			case 'approved':
			case 'done':
			case 'completed':
				return 'text-emerald-300 border-emerald-700 bg-emerald-900/20';
			case 'blocked':
			case 'pending_approval':
				return 'text-yellow-300 border-yellow-700 bg-yellow-900/20';
			case 'failed':
			case 'denied':
				return 'text-red-300 border-red-700 bg-red-900/20';
			case 'revised':
				return 'text-[#888] border-[#333] bg-[#111]';
			default:
				return 'text-[#888] border-[#333] bg-[#111]';
		}
	}

	function reasonText(approval: ApprovalRecord): string {
		if (approval.reason?.trim()) return approval.reason;
		const payload = approval.payload as Record<string, unknown> | null;
		return typeof payload?.description === 'string' && payload.description.trim() ? payload.description : 'No reasoning provided.';
	}

	function canTroubleshoot(approval: ApprovalRecord): boolean {
		if (approval.can_troubleshoot) return true;
		const payload = approval.payload as Record<string, unknown> | null;
		return Boolean(payload?.task_id || payload?.task_display_id);
	}

	function classifierBadgeClass(rec: string | null | undefined): string {
		switch ((rec || '').toLowerCase()) {
			case 'auto_approve':
				return 'border-emerald-700 bg-emerald-900/30 text-emerald-300';
			case 'escalate':
				return 'border-red-700 bg-red-900/30 text-red-300';
			case 'hold':
				return 'border-yellow-700 bg-yellow-900/30 text-yellow-300';
			default:
				return 'border-[#333] bg-[#111] text-[#888]';
		}
	}

	function classifierLabel(rec: string | null | undefined): string {
		switch ((rec || '').toLowerCase()) {
			case 'auto_approve':
				return 'Auto-approve';
			case 'escalate':
				return 'Escalate';
			case 'hold':
				return 'Hold';
			default:
				return 'Unclassified';
		}
	}

	function effectiveMode(approvalType: string | null | undefined): string {
		const key = String(approvalType || '').trim().toLowerCase();
		return (key && approvalModes[key]) || defaultApprovalMode || '';
	}

	function modeBadgeClass(mode: string): string {
		switch (mode.toLowerCase()) {
			case 'smart':
				return 'border-[#333] bg-[#111] text-[#888]';
			case 'off':
				return 'border-emerald-700 bg-emerald-900/30 text-emerald-300';
			case 'manual':
				return 'border-yellow-700 bg-yellow-900/30 text-yellow-300';
			default:
				return 'border-[#333] bg-[#111] text-[#666]';
		}
	}

	function deadlineState(approval: ApprovalRecord): { label: string; className: string } | null {
		if (!approval.expires_at) return null;
		const expiry = parseDate(approval.expires_at);
		if (!expiry) return null;
		const now = Date.now();
		const diffSeconds = Math.round((expiry - now) / 1000);
		if (diffSeconds <= 0) {
			return { label: 'Expired', className: 'text-red-300 border-red-800 bg-red-900/30' };
		}
		const hours = diffSeconds / 3600;
		const niceLabel =
			hours >= 24
				? `${Math.round(hours / 24)}d left`
				: hours >= 1
					? `${Math.round(hours)}h left`
					: `${Math.max(1, Math.round(diffSeconds / 60))}m left`;
		const className =
			hours <= 6
				? 'text-red-300 border-red-800 bg-red-900/30'
				: hours <= 24
					? 'text-yellow-300 border-yellow-800 bg-yellow-900/30'
					: 'text-[#888] border-[#333] bg-[#111]';
		return { label: niceLabel, className };
	}

	let bulkApproving = false;

	function autoApprovableIds(): number[] {
		return approvals
			.filter(
				(a) =>
					(a.classifier_recommendation || '').toLowerCase() === 'auto_approve' &&
					(a.status || '').toLowerCase() === 'pending_approval',
			)
			.map((a) => a.id);
	}

	async function runClassify(approvalId: number) {
		setBusy(approvalId, true);
		try {
			await classifyApproval(approvalId);
			await loadApprovals(true);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			setBusy(approvalId, false);
		}
	}

	async function runBulkApprove() {
		const ids = autoApprovableIds();
		if (ids.length === 0) {
			actionMessage = 'No auto_approve candidates to bulk approve.';
			return;
		}
		bulkApproving = true;
		try {
			const res = await bulkApproveApprovals(ids, { actor: 'operator', feedback: 'bulk-approve from /approval' });
			actionMessage = `Bulk approve: ${res.approved.length} approved, ${res.skipped.length} skipped, ${res.missing.length} missing.`;
			await loadApprovals(true);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			bulkApproving = false;
		}
	}

	function toStringArray(value: unknown): string[] {
		return Array.isArray(value) ? value.map((item) => String(item)).filter((item) => item.trim().length > 0) : [];
	}

	function buildTaskLog(task: ApprovalTaskSummary | null | undefined, detail: ApprovalTaskDetail | null | undefined): TaskLogEntry[] {
		const taskRow = (detail?.task as Record<string, unknown> | undefined) ?? (task as unknown as Record<string, unknown> | undefined) ?? {};
		const entries: Array<TaskLogEntry & { sortKey: number }> = [];
		const push = (entry: TaskLogEntry) => entries.push({ ...entry, sortKey: parseDate(entry.timestamp) });
		push({
			title: 'Container Created',
			summary: `${taskLabel(task)} assigned to ${String(taskRow.agent_id || task?.agent_id || '--')}`,
			detail: String(taskRow.title || task?.title || ''),
			timestamp: typeof taskRow.created_at === 'string' ? taskRow.created_at : (task?.created_at ?? null),
		});
		if (taskRow.started_at) {
			push({
				title: 'Execution Started',
				summary: `Status moved to ${String(taskRow.status || task?.status || 'running')}`,
				detail: String(taskRow.agent_id || task?.agent_id || '--'),
				timestamp: String(taskRow.started_at),
			});
		}
		if (taskRow.completed_at) {
			push({
				title: 'Execution Completed',
				summary: `Finished with status ${String(taskRow.status || task?.status || 'done')}`,
				detail: String(taskRow.error || task?.error || ''),
				timestamp: String(taskRow.completed_at),
				error: String(taskRow.status || task?.status || '').toLowerCase() === 'failed',
			});
		}
		for (const item of Array.isArray(detail?.audit_log) ? detail.audit_log : []) {
			push({
				title: `Audit: ${String(item.event || item.action || 'audit')}`,
				summary: `${String(item.from || '--')} -> ${String(item.to || '--')}`,
				detail: String(item.reason || ''),
				timestamp: typeof item.timestamp === 'string' ? item.timestamp : null,
			});
		}
		for (const call of Array.isArray(detail?.tool_calls) ? detail.tool_calls : []) {
			push({
				title: `Tool: ${String(call.tool_name || call.tool || 'tool')}`,
				summary: `Duration ${Number.isFinite(Number(call.duration_ms)) ? `${Number(call.duration_ms).toFixed(0)}ms` : '--'}`,
				detail: compact(call.output_summary || call.error || call.input_json, 180),
				timestamp: typeof call.created_at === 'string' ? call.created_at : null,
				error: Boolean(call.error),
			});
		}
		return entries.sort((left, right) => left.sortKey - right.sortKey);
	}

	function responseText(detail?: ApprovalTaskDetail | null): string {
		const output = ((detail?.task as Record<string, unknown> | undefined)?.output_data);
		if (typeof output === 'string') return output;
		if (output && typeof output === 'object' && !Array.isArray(output)) {
			return typeof (output as Record<string, unknown>).response === 'string'
				? String((output as Record<string, unknown>).response)
				: pretty(output);
		}
		return '';
	}

	function parseTroubleshootReport(detail?: ApprovalTaskDetail | null): TroubleshootReport | null {
		const raw = responseText(detail).trim();
		if (!raw) return null;
		const direct = raw.startsWith('{') ? raw : raw.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1] || '';
		try {
			const parsed = JSON.parse(direct || raw) as Record<string, unknown>;
			return {
				summary: String(parsed.summary || '').trim(),
				rootCause: String(parsed.root_cause || parsed.rootCause || '').trim(),
				evidence: toStringArray(parsed.evidence),
				affectedFiles: toStringArray(parsed.affected_files ?? parsed.affectedFiles),
				recommendedFix: toStringArray(parsed.recommended_fix ?? parsed.recommendedFix),
				validationPlan: toStringArray(parsed.validation_plan ?? parsed.validationPlan),
				riskLevel: String(parsed.risk_level || parsed.riskLevel || '').trim(),
				confidence: String(parsed.confidence || '').trim(),
			};
		} catch {
			return null;
		}
	}

	async function loadApprovals(background = false) {
		if (background) refreshing = true;
		else loading = true;
		error = null;
		try {
			const rows = await getApprovals(viewMode === 'pending' ? { status: 'pending_approval' } : {});
			approvals = [...(viewMode === 'history' ? rows.filter((row) => row.status !== 'pending_approval') : rows)]
				.sort((left, right) => viewMode === 'pending' ? parseDate(left.created_at) - parseDate(right.created_at) : parseDate(right.created_at) - parseDate(left.created_at));
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load approvals';
		} finally {
			if (background) refreshing = false;
			else loading = false;
		}
	}

	async function loadSettings() {
		try {
			const settings = await getSettings();
			autoApproveCodeEdits = String(settings.auto_approve_code_edits || 'false').toLowerCase() === 'true';
			autoApprovePromotions = String(settings.auto_approve_promotions || 'false').toLowerCase() === 'true';
		} finally {
			settingsLoading = false;
		}
	}

	async function loadApprovalModes() {
		try {
			const modes = await getApprovalModes();
			approvalModes = Object.fromEntries(
				Object.entries(modes.modes || {}).map(([key, value]) => [key.toLowerCase(), value]),
			);
			defaultApprovalMode = modes.default_mode || '';
		} catch {
			// Policy surfacing is best-effort; absence just hides the mode badges.
		}
	}

	async function toggleAutoApproveCodeEdits() {
		const nextState = !autoApproveCodeEdits;
		if (nextState && !window.confirm('Enable automatic approval for code edits? Code changes will proceed without manual review until you turn it back off.')) return;
		await updateSettingsSection('bot-operations', { auto_approve_code_edits: String(nextState) });
		autoApproveCodeEdits = nextState;
		actionMessage = `Code edit auto-approval ${nextState ? 'enabled' : 'disabled'}.`;
		setTimeout(() => actionMessage = null, 3000);
	}

	async function toggleAutoApprovePromotions() {
		const nextState = !autoApprovePromotions;
		if (nextState && !window.confirm('Enable automatic approval for promotions? Strategy promotions and config changes will proceed without manual review until you turn it back off.')) return;
		await updateSettingsSection('bot-operations', { auto_approve_promotions: String(nextState) });
		autoApprovePromotions = nextState;
		actionMessage = `Promotion auto-approval ${nextState ? 'enabled' : 'disabled'}.`;
		setTimeout(() => actionMessage = null, 3000);
	}

	function stopPolling() {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	function syncPolling() {
		stopPolling();
		if (!approvalContext || selectedApprovalId === null) return;
		if (!activeTask(approvalContext.linked_task) && !activeTask(approvalContext.troubleshoot_task)) return;
		pollTimer = setInterval(() => {
			if (selectedApprovalId !== null) void loadApprovalContext(selectedApprovalId, true);
		}, 2000);
	}

	async function loadApprovalContext(approvalId: number, background = false, preferredTab?: DrawerTab) {
		if (background) contextRefreshing = true;
		else contextLoading = true;
		contextError = null;
		try {
			approvalContext = await getApprovalContext(approvalId);
			if (preferredTab) drawerTab = preferredTab;
			else if (!background) drawerTab = approvalContext.recommended_mode === 'execution' && approvalContext.linked_task ? 'execution' : 'diagnosis';
			if (drawerTab === 'execution' && !approvalContext.linked_task) drawerTab = 'diagnosis';
		} catch (e) {
			contextError = e instanceof Error ? e.message : `Failed to load approval #${approvalId}`;
		} finally {
			if (background) contextRefreshing = false;
			else contextLoading = false;
			syncPolling();
		}
	}

	function openInspector(approval: ApprovalRecord, preferredTab: DrawerTab = 'diagnosis') {
		openInspectorById(approval.id, preferredTab);
	}

	function openInspectorById(approvalId: number, preferredTab: DrawerTab = 'diagnosis') {
		selectedApprovalId = approvalId;
		approvalContext = null;
		drawerTab = preferredTab;
		void loadApprovalContext(approvalId, false, preferredTab);
	}

	function closeInspector() {
		selectedApprovalId = null;
		approvalContext = null;
		contextError = null;
		contextLoading = false;
		contextRefreshing = false;
		drawerTab = 'diagnosis';
		stopPolling();
	}

	function onReviseInput(approvalId: number, value: string) {
		reviseInput = { ...reviseInput, [approvalId]: value };
	}

	function toggleGroup(groupType: string) {
		const next = new Set(collapsedGroups);
		if (next.has(groupType)) next.delete(groupType);
		else next.add(groupType);
		collapsedGroups = next;
	}

	// Denying a strategy dethrone/promotion arms an escalating cooldown on the
	// backend, so it deserves a real reason instead of the old boilerplate
	// "Manual decision via UI: deny". Other types keep one-click deny.
	function handleDenyClick(approval: ApprovalRecord) {
		if (isStrategyLifecycleType(approval.approval_type)) {
			denyPickerId = denyPickerId === approval.id ? null : approval.id;
			denyPreset = '';
			denyFreeText = '';
			return;
		}
		void submitDecision(approval.id, 'deny');
	}

	async function confirmDeny(approvalId: number) {
		const reason = [denyPreset, denyFreeText.trim()].filter(Boolean).join(' — ') || 'Denied via UI';
		denyPickerId = null;
		await submitDecision(approvalId, 'deny', undefined, reason);
	}

	async function submitDecision(approvalId: number, action: PendingDecision, preferredTab?: DrawerTab, reasonOverride?: string) {
		if (isBusy(approvalId)) return;
		// GO-LIVE-1: approving a paper→live promotion is never a bare click — the
		// backend requires a typed "GO LIVE" confirmation plus an initial per-asset
		// notional ceiling (USD), so collect both before submitting.
		const record = approvals.find((row) => row.id === approvalId);
		let goLive: { confirm: string; live_notional_ceiling_usd: number } | undefined;
		if (
			action === 'approve' &&
			record?.approval_type === 'strategy_promotion_approval' &&
			(record?.requested_status || '').toLowerCase() === 'live_graduated'
		) {
			const ceilingRaw = window.prompt(
				`Going LIVE trades real capital.\n\nInitial per-asset notional ceiling (USD) for ${record?.target_id ?? 'this strategy'} — the largest live position it may hold. Enforced on every order; editable later.`,
				'1000'
			);
			if (ceilingRaw === null) return;
			const ceiling = Number(ceilingRaw);
			if (!Number.isFinite(ceiling) || ceiling <= 0) {
				error = 'Go-live needs a positive notional ceiling (USD).';
				return;
			}
			const typed = window.prompt(`Type GO LIVE to confirm promoting ${record?.target_id ?? 'this strategy'} to live trading:`);
			if (typed === null) return;
			if (typed.trim().toUpperCase() !== 'GO LIVE') {
				error = 'Go-live not confirmed — you must type GO LIVE exactly.';
				return;
			}
			goLive = { confirm: 'GO LIVE', live_notional_ceiling_usd: ceiling };
		}
		setBusy(approvalId, true);
		actionMessage = null;
		error = null;
		try {
			const payload = {
				actor: 'operator',
				reason: reasonOverride || `Manual decision via UI: ${action}`,
				feedback: (reviseInput[approvalId] || '').trim() || undefined,
				...(goLive ?? {}),
			};
			if (action === 'approve') await approveApproval(approvalId, payload);
			else if (action === 'deny') await denyApproval(approvalId, payload);
			else await reviseApproval(approvalId, payload);
			actionMessage = `Approval #${approvalId} ${action}d.`;
			reviseInput = { ...reviseInput, [approvalId]: '' };
			await loadApprovals(true);
			if (selectedApprovalId === approvalId || preferredTab) {
				selectedApprovalId = approvalId;
				await loadApprovalContext(approvalId, false, preferredTab);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to ${action} approval #${approvalId}`;
		} finally {
			setBusy(approvalId, false);
		}
	}

	async function handleUserComplete(approvalId: number) {
		if (isBusy(approvalId)) return;
		setBusy(approvalId, true);
		actionMessage = null;
		error = null;
		try {
			const payload = {
				actor: 'user',
				reason: 'Completed manually by the user',
				feedback: (reviseInput[approvalId] || '').trim() || undefined,
			};
			await userCompleteApproval(approvalId, payload);
			actionMessage = `Approval #${approvalId} marked as completed by user. Brain notified.`;
			reviseInput = { ...reviseInput, [approvalId]: '' };
			await loadApprovals(true);
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to complete approval #${approvalId}`;
		} finally {
			setBusy(approvalId, false);
		}
	}

	async function launchTroubleshoot(approvalId: number) {
		if (isLaunchingTroubleshoot(approvalId)) return;
		launchingTroubleshootApprovalId = approvalId;
		actionMessage = null;
		error = null;
		try {
			const result = await troubleshootApproval(approvalId, { agent_id: 'full-stack-engineer' });
			actionMessage = result.created
				? `Started troubleshoot task ${result.task.display_id || result.task.id} for approval #${approvalId}.`
				: `Using existing troubleshoot task ${result.task.display_id || result.task.id} for approval #${approvalId}.`;
			selectedApprovalId = approvalId;
			await loadApprovals(true);
			await loadApprovalContext(approvalId, false, 'diagnosis');
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to troubleshoot approval #${approvalId}`;
		} finally {
			launchingTroubleshootApprovalId = null;
		}
	}

	function collectEventIds(detail: Record<string, unknown>): { taskIds: Set<string>; approvalIds: Set<number> } {
		const taskIds = new Set<string>();
		const approvalIds = new Set<number>();
		const visit = (node: unknown, depth: number) => {
			if (!node || depth > 4) return;
			if (Array.isArray(node)) {
				for (const item of node) visit(item, depth + 1);
				return;
			}
			if (typeof node !== 'object') return;
			for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
				const lkey = key.toLowerCase();
				if ((lkey === 'display_id' || lkey === 'task_display_id') && typeof value === 'string' && value.trim()) {
					taskIds.add(value.trim().toLowerCase());
				} else if (lkey === 'approval_id') {
					const num = Number(value);
					if (Number.isFinite(num)) approvalIds.add(num);
				} else if (value && typeof value === 'object') {
					visit(value, depth + 1);
				}
			}
		};
		visit(detail, 0);
		return { taskIds, approvalIds };
	}

	function attachRealtimeRefresh() {
		if (typeof window === 'undefined' || wsCleanup) return;
		const handler = (event: Event) => {
			if (selectedApprovalId === null) return;
			const detail = (event as CustomEvent<Record<string, unknown>>).detail ?? {};
			const { taskIds, approvalIds } = collectEventIds(detail);
			const linked = String(approvalContext?.linked_task?.display_id || '').toLowerCase();
			const troubleshoot = String(approvalContext?.troubleshoot_task?.display_id || '').toLowerCase();
			const relevant =
				approvalIds.has(selectedApprovalId) ||
				(linked && taskIds.has(linked)) ||
				(troubleshoot && taskIds.has(troubleshoot));
			if (relevant) {
				void loadApprovalContext(selectedApprovalId, true);
				void loadApprovals(true);
			}
		};
		window.addEventListener('forven:event', handler);
		wsCleanup = () => window.removeEventListener('forven:event', handler);
	}

	onMount(() => {
		attachRealtimeRefresh();
		void loadSettings();
		void loadApprovalModes();
		void loadApprovals();
		const deepLinkId = Number($page.url.searchParams.get('approval_id'));
		if (Number.isFinite(deepLinkId) && deepLinkId > 0) {
			openInspectorById(deepLinkId);
		}
	});

	onDestroy(() => {
		stopPolling();
		wsCleanup?.();
	});

	function switchView(mode: ViewMode) {
		if (viewMode === mode) return;
		viewMode = mode;
		void loadApprovals();
	}
	$: oldestVisibleAge = approvals.length > 0
		? fmtAge(approvals.reduce((oldest, row) => (parseDate(row.created_at) < parseDate(oldest.created_at) ? row : oldest)).created_at)
		: '--';
	$: presentTypes = [...new Set(approvals.map((row) => row.approval_type))].sort();
	$: filteredApprovals = approvals.filter((row) => {
		if (filterType && row.approval_type !== filterType) return false;
		const needle = filterText.trim().toLowerCase();
		if (needle) {
			const haystack = `${row.target_id || ''} ${row.reason || ''} ${row.actor || ''} ${row.approval_type || ''}`.toLowerCase();
			if (!haystack.includes(needle)) return false;
		}
		return true;
	});
	// Pending view groups by type (dethrone recs dominate the queue); history
	// stays a flat newest-first list under a single hidden header.
	$: displayGroups = (() => {
		if (viewMode !== 'pending') return [['__all__', filteredApprovals]] as Array<[string, ApprovalRecord[]]>;
		const groups = new Map<string, ApprovalRecord[]>();
		for (const row of filteredApprovals) {
			const key = row.approval_type || 'unknown';
			if (!groups.has(key)) groups.set(key, []);
			groups.get(key)!.push(row);
		}
		return [...groups.entries()].sort((left, right) => right[1].length - left[1].length);
	})();
	$: selectedApproval = approvalContext?.approval ?? approvals.find((approval) => approval.id === selectedApprovalId) ?? null;
	$: diagnosisLog = buildTaskLog(approvalContext?.troubleshoot_task, approvalContext?.troubleshoot_task_detail);
	$: executionLog = buildTaskLog(approvalContext?.linked_task, approvalContext?.linked_task_detail);
	$: troubleshootReport = parseTroubleshootReport(approvalContext?.troubleshoot_task_detail);
	$: troubleshootRaw = responseText(approvalContext?.troubleshoot_task_detail);
	$: executionRaw = responseText(approvalContext?.linked_task_detail);
</script>

<div class="p-4 space-y-4 text-sm">
	<header class="flex items-center justify-between gap-4">
		<div class="flex items-center gap-4">
			<h1 class="text-lg font-bold uppercase tracking-widest text-white">Approvals</h1>
			<div class="flex bg-[#111] border border-[#222] p-0.5">
				<button class="px-3 py-1 text-xs {viewMode === 'pending' ? 'bg-[#333] text-white' : 'text-[#888]'}" on:click={() => switchView('pending')}>Pending</button>
				<button class="px-3 py-1 text-xs {viewMode === 'history' ? 'bg-[#333] text-white' : 'text-[#888]'}" on:click={() => switchView('history')}>History</button>
			</div>
		</div>
		<div class="flex items-center gap-3">
			{#if !settingsLoading}
				<button type="button" class="flex items-center gap-2 px-3 py-1.5 border {autoApprovePromotions ? 'bg-emerald-900/30 border-emerald-700 text-emerald-400' : 'bg-[#111] border-[#333] text-[#888]'}" on:click={toggleAutoApprovePromotions}>
					<div class="w-3 h-3 rounded-full {autoApprovePromotions ? 'bg-emerald-500' : 'bg-[#444]'}"></div>
					<span class="text-xs font-semibold uppercase tracking-wider">Promotions</span>
				</button>
				<button type="button" class="flex items-center gap-2 px-3 py-1.5 border {autoApproveCodeEdits ? 'bg-emerald-900/30 border-emerald-700 text-emerald-400' : 'bg-[#111] border-[#333] text-[#888]'}" on:click={toggleAutoApproveCodeEdits}>
					<div class="w-3 h-3 rounded-full {autoApproveCodeEdits ? 'bg-emerald-500' : 'bg-[#444]'}"></div>
					<span class="text-xs font-semibold uppercase tracking-wider">Code edits</span>
				</button>
			{/if}
			<a href="/settings/approvals" class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white hover:border-[#555]">Configure approval modes</a>
			{#if viewMode === 'pending' && autoApprovableIds().length > 0}
				<button
					type="button"
					disabled={bulkApproving}
					class="text-xs border border-emerald-700 bg-emerald-900/20 hover:bg-emerald-900/40 text-emerald-300 px-3 py-1.5 disabled:opacity-40"
					on:click={() => void runBulkApprove()}
				>
					{bulkApproving ? 'Approving...' : `Bulk approve (${autoApprovableIds().length})`}
				</button>
			{/if}
			<button type="button" disabled={refreshing} class="text-xs border border-[#333] px-3 py-1.5 text-[#888] disabled:opacity-40" on:click={() => void loadApprovals(true)}>{refreshing ? 'Refreshing...' : 'Refresh'}</button>
		</div>
	</header>

	{#if actionMessage}<div class="bg-emerald-900/20 border border-emerald-800 text-emerald-300 text-xs px-3 py-2 rounded">{actionMessage}</div>{/if}
	{#if error}<div class="bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{error}</div>{/if}

	<div class="grid gap-3 md:grid-cols-4">
		<div class="border px-4 py-3 {autoApprovePromotions ? 'border-yellow-700 bg-yellow-900/20' : 'border-emerald-700 bg-emerald-900/20'}">
			<div class="text-[10px] uppercase tracking-wider text-[#888]">Promotions</div>
			<div class="mt-1 text-sm font-semibold {autoApprovePromotions ? 'text-yellow-300' : 'text-emerald-300'}">{autoApprovePromotions ? 'Auto-approve' : 'Manual review'}</div>
		</div>
		<div class="border px-4 py-3 {autoApproveCodeEdits ? 'border-yellow-700 bg-yellow-900/20' : 'border-[#333] bg-[#111]'}">
			<div class="text-[10px] uppercase tracking-wider text-[#888]">Code edits</div>
			<div class="mt-1 text-sm font-semibold {autoApproveCodeEdits ? 'text-yellow-300' : 'text-[#888]'}">{autoApproveCodeEdits ? 'Auto-approve' : 'Logged for review'}</div>
		</div>
		<div class="border border-[#222] bg-[#050505] px-4 py-3">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Visible approvals</div>
			<div class="mt-1 text-sm font-semibold text-white">{approvals.length}</div>
		</div>
		<div class="border border-[#222] bg-[#050505] px-4 py-3">
			<div class="text-[10px] uppercase tracking-wider text-[#666]">Oldest visible age</div>
			<div class="mt-1 text-sm font-semibold text-white">{oldestVisibleAge}</div>
		</div>
	</div>

	<div class="flex flex-wrap items-center gap-2">
		<select bind:value={filterType} class="bg-black border border-[#222] text-xs px-2 py-1.5 text-white">
			<option value="">All types</option>
			{#each presentTypes as presentType}
				<option value={presentType}>{friendlyTitle(presentType)}</option>
			{/each}
		</select>
		<input
			type="text"
			placeholder="Filter by strategy, reason, actor..."
			class="flex-1 min-w-[220px] max-w-md bg-black border border-[#222] text-xs px-3 py-1.5 text-white"
			bind:value={filterText}
		/>
		{#if filterType || filterText}
			<button type="button" class="text-xs border border-[#333] px-3 py-1.5 text-[#888] hover:text-white" on:click={() => { filterType = ''; filterText = ''; }}>Clear filters</button>
			<span class="text-xs text-[#666]">{filteredApprovals.length} of {approvals.length} shown</span>
		{/if}
	</div>

	{#if loading}
		<div class="text-[#666]">Loading approvals...</div>
	{:else if filteredApprovals.length === 0}
		<div class="text-[#666]">
			{approvals.length === 0
				? `No ${viewMode === 'pending' ? 'pending' : 'historical'} approvals.`
				: 'No approvals match the current filters.'}
		</div>
	{:else}
		<div class="space-y-4">
			{#each displayGroups as [groupType, groupRows] (groupType)}
			<section class="space-y-3">
			{#if groupType !== '__all__'}
				<button
					type="button"
					class="w-full flex items-center gap-2 border border-[#222] bg-[#0a0a0a] px-3 py-2 text-left hover:border-[#444]"
					on:click={() => toggleGroup(groupType)}
				>
					<span class="text-sm font-semibold text-white uppercase tracking-wider">{friendlyTitle(groupType)}</span>
					<span class="inline-flex items-center justify-center min-w-[1.5rem] px-1.5 py-0.5 text-xs border border-[#333] bg-black text-[#ccc]">{groupRows.length}</span>
					<span class="ml-auto text-[#666] text-xs">{collapsedGroups.has(groupType) ? '+ expand' : '− collapse'}</span>
				</button>
			{/if}
			{#if !collapsedGroups.has(groupType)}
			{#each groupRows as approval (approval.id)}
				<article class="terminal-card p-4 space-y-3">
					<div class="flex items-start justify-between gap-3">
						<div>
							<div class="text-xs uppercase tracking-wider text-[#666]">Approval #{approval.id} <span class="font-mono normal-case text-[#444]">· {approval.approval_type}</span></div>
							<div class="text-lg font-semibold text-white">{friendlyTitle(approval.approval_type)}</div>
							<div class="mt-1 text-sm text-[#888]">{reasonText(approval)}</div>
							<div class="mt-2 flex flex-wrap items-center gap-2">
								<span
									class="inline-flex items-center px-2 py-0.5 border text-[10px] uppercase tracking-wider {classifierBadgeClass(approval.classifier_recommendation)}"
									title={approval.classifier_reasoning || 'Smart-approval classifier has not run yet for this item.'}
								>
									{classifierLabel(approval.classifier_recommendation)}
								</span>
								{#if effectiveMode(approval.approval_type)}
									<span
										class="inline-flex items-center px-2 py-0.5 border text-[10px] uppercase tracking-wider {modeBadgeClass(effectiveMode(approval.approval_type))}"
										title="Active policy for '{approval.approval_type}' (configure under Approval Modes). 'smart' auto-approves classifier auto_approve rows; 'off' auto-approves; 'manual' always requires review."
									>
										{effectiveMode(approval.approval_type)}
									</span>
								{/if}
								{#if approval.classifier_model}
									<span class="text-[10px] text-[#666]">{approval.classifier_model}</span>
								{/if}
								{#if viewMode === 'pending'}
									<button
										type="button"
										disabled={isBusy(approval.id)}
										class="text-[10px] border border-[#333] px-2 py-0.5 text-[#888] hover:text-white disabled:opacity-40"
										on:click={() => void runClassify(approval.id)}
									>
										{approval.classifier_recommendation ? 'Re-classify' : 'Classify'}
									</button>
								{/if}
								{#if approval.auto_approved}
									<span class="text-[10px] border border-emerald-800 bg-emerald-900/30 text-emerald-300 px-2 py-0.5 uppercase tracking-wider">Auto-approved</span>
								{/if}
							</div>
							{#if approval.classifier_reasoning}
								<div class="mt-1 text-[11px] text-[#666] italic">{compact(approval.classifier_reasoning, 200)}</div>
							{/if}
						</div>
						<div class="text-right text-xs text-[#666]">
							<div class="inline-flex items-center px-2 py-0.5 border uppercase {statusClass(approval.status)}">{approval.status}</div>
							<div class="mt-1">{fmtDate(approval.created_at)}</div>
							{#if approval.actor}
								<div class="mt-1 font-mono" title="Requesting actor">{approval.actor}</div>
							{/if}
							{#if deadlineState(approval)}
								<div class="mt-1 inline-flex items-center px-2 py-0.5 border uppercase tracking-wider {deadlineState(approval)!.className}">{deadlineState(approval)!.label}</div>
							{/if}
						</div>
					</div>

					<div class="grid gap-3 sm:grid-cols-2 text-xs">
						<div class="border border-[#222] bg-black/30 px-3 py-2">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Execution task</div>
							<div class="mt-1 font-mono">{#if taskDetailUrl(approval.linked_task)}<button type="button" class="text-[#888] hover:text-white hover:underline" on:click={() => goto(taskDetailUrl(approval.linked_task))}>{taskLabel(approval.linked_task)}</button>{:else}<span class="text-[#888]">{taskLabel(approval.linked_task)}</span>{/if}</div>
							{#if approval.linked_task}
								<div class="mt-1 text-[#888]">{compact(approval.linked_task.title || approval.linked_task.description)}</div>
							{/if}
						</div>
						<div class="border border-[#222] bg-black/30 px-3 py-2">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Troubleshoot</div>
							<div class="mt-1 font-mono">{#if taskDetailUrl(approval.troubleshoot_task)}<button type="button" class="text-yellow-300 hover:text-yellow-200 hover:underline" on:click={() => goto(taskDetailUrl(approval.troubleshoot_task))}>{taskLabel(approval.troubleshoot_task)}</button>{:else}<span class="text-yellow-300">{taskLabel(approval.troubleshoot_task)}</span>{/if}</div>
							<div class="mt-1 text-[#666]">{approval.troubleshoot_task ? taskStatus(approval.troubleshoot_task) : 'Not started'}</div>
						</div>
					</div>

					{#if approval.approval_type === 'skill_update_proposal' && typeof approval.payload === 'object' && approval.payload}
						<SkillUpdateProposalCard payload={approval.payload as Record<string, unknown>} />
					{:else if payloadRenderer(approval.approval_type) && typeof approval.payload === 'object' && approval.payload}
						<svelte:component this={payloadRenderer(approval.approval_type)} {approval} />
					{:else}
						<pre class="text-[11px] text-[#888] bg-black border border-[#222] p-2 max-h-40 overflow-auto whitespace-pre-wrap">{typeof approval.payload === 'object' ? JSON.stringify(approval.payload, null, 2) : String(approval.payload || '-')}</pre>
					{/if}

					<div class="flex flex-wrap gap-2">
						{#if canTroubleshoot(approval)}
							<button type="button" disabled={isLaunchingTroubleshoot(approval.id)} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => void launchTroubleshoot(approval.id)}>
								{isLaunchingTroubleshoot(approval.id) ? 'Launching...' : approval.troubleshoot_task ? 'Open Troubleshoot' : 'Troubleshoot'}
							</button>
						{/if}
						<button type="button" class="terminal-button text-xs px-3 py-2" on:click={() => openInspector(approval, approvalStatus(approval) === 'approved' ? 'execution' : 'diagnosis')}>
							{approvalStatus(approval) === 'approved' ? 'Watch Task' : 'Details'}
						</button>
						{#if viewMode === 'pending'}
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button-primary text-xs px-3 py-2 disabled:opacity-40" on:click={() => void submitDecision(approval.id, 'approve')}>{isBusy(approval.id) ? 'Approving...' : 'Approve'}</button>
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => void handleUserComplete(approval.id)}>{isBusy(approval.id) ? 'Completing...' : 'I Did This'}</button>
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button-danger text-xs px-3 py-2 disabled:opacity-40" on:click={() => handleDenyClick(approval)}>{isBusy(approval.id) ? 'Denying...' : 'Deny'}</button>
						{/if}
					</div>

					{#if denyPickerId === approval.id && viewMode === 'pending'}
						<div class="border border-red-900/60 bg-red-950/15 p-3 space-y-2">
							<div class="text-[10px] uppercase tracking-wider text-red-300">Why deny?</div>
							<div class="flex flex-wrap gap-2">
								{#each DENY_PRESETS as preset}
									<button
										type="button"
										class="text-xs border px-3 py-1.5 {denyPreset === preset ? 'border-red-500 bg-red-900/40 text-red-200' : 'border-[#333] text-[#888] hover:text-white'}"
										on:click={() => denyPreset = denyPreset === preset ? '' : preset}
									>
										{preset}
									</button>
								{/each}
							</div>
							<input type="text" placeholder="Optional details..." class="w-full bg-black border border-[#222] text-xs px-3 py-2 text-white" bind:value={denyFreeText} />
							<div class="text-[10px] text-[#666]">Denying pauses dethrone re-asks for this strategy: 24h on the first deny, then 3 days, then 7 days. Approving one (or the strategy leaving paper/live) resets the ladder.</div>
							<div class="flex gap-2">
								<button type="button" disabled={isBusy(approval.id)} class="terminal-button-danger text-xs px-3 py-2 disabled:opacity-40" on:click={() => void confirmDeny(approval.id)}>Confirm deny</button>
								<button type="button" class="terminal-button text-xs px-3 py-2" on:click={() => denyPickerId = null}>Cancel</button>
							</div>
						</div>
					{/if}

					{#if viewMode === 'pending'}
						<div class="flex gap-2">
							<input type="text" placeholder="Revision feedback..." class="flex-1 bg-black border border-[#222] text-xs px-3 py-2 text-white" value={reviseInput[approval.id] || ''} on:input={(event) => onReviseInput(approval.id, (event.currentTarget as HTMLInputElement).value)} />
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => void submitDecision(approval.id, 'revise')}>{isBusy(approval.id) ? 'Revising...' : 'Revise'}</button>
						</div>
					{:else}
						<div class="grid sm:grid-cols-4 gap-3 text-xs border-t border-[#222] pt-3">
							<div><div class="text-[#666] uppercase tracking-wider">Decision</div><div class="text-white font-semibold">{approval.decision || 'N/A'}</div></div>
							<div><div class="text-[#666] uppercase tracking-wider">Decided</div><div class="text-white">{fmtDate(approval.decided_at)}</div></div>
							<div><div class="text-[#666] uppercase tracking-wider">Actor</div><div class="text-white font-mono">{approval.actor || '-'}</div></div>
							<div><div class="text-[#666] uppercase tracking-wider">Feedback</div><div class="text-white">{approval.feedback || '-'}</div></div>
						</div>
					{/if}
				</article>
			{/each}
			{/if}
			</section>
			{/each}
		</div>
	{/if}
</div>

{#if selectedApprovalId !== null}
	<button type="button" class="fixed inset-0 z-40 bg-black/70" aria-label="Close approval inspector" on:click={closeInspector}></button>
	<aside class="fixed inset-y-0 right-0 z-50 w-full max-w-[780px] bg-[#050505] border-l border-[#222] flex flex-col">
		<header class="border-b border-[#222] px-6 py-4 space-y-3">
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="text-[11px] uppercase tracking-[0.18em] text-[#666]">Approval Inspector</div>
					<div class="mt-1 text-xl font-semibold text-white">{selectedApproval ? `Approval #${selectedApproval.id}` : `Approval #${selectedApprovalId}`}</div>
					<div class="mt-1 text-sm text-[#888]">{selectedApproval ? reasonText(selectedApproval) : 'Loading approval context...'}</div>
				</div>
				<div class="flex items-center gap-2">
					{#if selectedApproval && canTroubleshoot(selectedApproval)}
						<button type="button" disabled={isLaunchingTroubleshoot(selectedApproval.id)} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => void launchTroubleshoot(selectedApproval.id)}>
							{isLaunchingTroubleshoot(selectedApproval.id) ? 'Launching...' : selectedApproval.troubleshoot_task ? 'Refresh Diagnosis' : 'Run Troubleshoot'}
						</button>
					{/if}
					<button type="button" disabled={contextLoading || contextRefreshing} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => selectedApprovalId !== null && void loadApprovalContext(selectedApprovalId)}>{contextRefreshing ? 'Refreshing...' : 'Refresh'}</button>
					<button type="button" class="terminal-button text-xs px-3 py-2" on:click={closeInspector}>Close</button>
				</div>
			</div>

			<div class="grid gap-3 md:grid-cols-3 text-xs">
				<div class="border border-[#222] bg-black/30 px-3 py-2"><div class="text-[10px] uppercase tracking-wider text-[#666]">Approval status</div><div class="mt-1 inline-flex items-center px-2 py-0.5 border uppercase {statusClass(approvalStatus(selectedApproval))}">{selectedApproval?.status || 'loading'}</div></div>
				<div class="border border-[#222] bg-black/30 px-3 py-2"><div class="text-[10px] uppercase tracking-wider text-[#666]">Execution</div><div class="mt-1 font-mono">{#if taskDetailUrl(approvalContext?.linked_task)}<button type="button" class="text-[#888] hover:text-white hover:underline" on:click={() => goto(taskDetailUrl(approvalContext?.linked_task))}>{taskLabel(approvalContext?.linked_task)}</button>{:else}<span class="text-[#888]">{taskLabel(approvalContext?.linked_task)}</span>{/if}</div></div>
				<div class="border border-[#222] bg-black/30 px-3 py-2"><div class="text-[10px] uppercase tracking-wider text-[#666]">Troubleshoot</div><div class="mt-1 font-mono">{#if taskDetailUrl(approvalContext?.troubleshoot_task)}<button type="button" class="text-yellow-300 hover:text-yellow-200 hover:underline" on:click={() => goto(taskDetailUrl(approvalContext?.troubleshoot_task))}>{taskLabel(approvalContext?.troubleshoot_task)}</button>{:else}<span class="text-yellow-300">{taskLabel(approvalContext?.troubleshoot_task)}</span>{/if}</div></div>
			</div>

			<div class="flex items-center gap-2">
				<button type="button" class="px-3 py-1.5 text-xs border uppercase {drawerTab === 'diagnosis' ? 'border-yellow-500 text-yellow-200 bg-yellow-900/20' : 'border-[#333] text-[#888]'}" on:click={() => drawerTab = 'diagnosis'}>Diagnosis</button>
				<button type="button" disabled={!approvalContext?.linked_task} class="px-3 py-1.5 text-xs border uppercase {drawerTab === 'execution' ? 'border-white text-white bg-[#111]' : 'border-[#333] text-[#888]'} disabled:opacity-40" on:click={() => drawerTab = 'execution'}>Execution</button>
			</div>

			{#if selectedApproval && approvalStatus(selectedApproval) === 'pending_approval'}
				<div class="flex flex-wrap gap-2">
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button-primary text-xs px-3 py-2 disabled:opacity-40" on:click={() => void submitDecision(selectedApproval.id, 'approve', 'execution')}>{isBusy(selectedApproval.id) ? 'Approving...' : 'Approve + Watch'}</button>
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button text-xs px-3 py-2 disabled:opacity-40" on:click={() => void handleUserComplete(selectedApproval.id)}>{isBusy(selectedApproval.id) ? 'Completing...' : 'I Did This'}</button>
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button-danger text-xs px-3 py-2 disabled:opacity-40" on:click={() => void submitDecision(selectedApproval.id, 'deny')}>{isBusy(selectedApproval.id) ? 'Denying...' : 'Deny'}</button>
				</div>
			{/if}
		</header>

		<div class="flex-1 overflow-auto p-6 space-y-4">
			{#if contextError}<div class="bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{contextError}</div>{/if}
			{#if contextLoading && !approvalContext}
				<div class="text-[#666]">Loading approval context...</div>
			{:else if drawerTab === 'diagnosis'}
				<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Diagnosis output</div>
					{#if !approvalContext?.troubleshoot_task}
						<div class="text-xs text-[#888]">No troubleshoot run yet. Start one to get a root-cause report before approving the fix.</div>
					{:else if troubleshootReport}
						<div class="space-y-3 text-sm">
							<div><div class="text-[10px] uppercase tracking-wider text-[#666]">Summary</div><div class="mt-1 text-white">{troubleshootReport.summary || 'No summary returned.'}</div></div>
							<div><div class="text-[10px] uppercase tracking-wider text-[#666]">Root Cause</div><div class="mt-1 text-yellow-200">{troubleshootReport.rootCause || 'No root cause returned.'}</div></div>
							<div><div class="text-[10px] uppercase tracking-wider text-[#666]">Recommended Fix</div><div class="mt-1 text-white">{troubleshootReport.recommendedFix.length > 0 ? troubleshootReport.recommendedFix.join(' | ') : 'No fix recommendation yet.'}</div></div>
							<div><div class="text-[10px] uppercase tracking-wider text-[#666]">Validation Plan</div><div class="mt-1 text-white">{troubleshootReport.validationPlan.length > 0 ? troubleshootReport.validationPlan.join(' | ') : 'No validation plan yet.'}</div></div>
							<div class="text-[11px] text-[#666]">Files: {troubleshootReport.affectedFiles.length > 0 ? troubleshootReport.affectedFiles.join(', ') : '--'} | Risk {troubleshootReport.riskLevel || '--'} | Confidence {troubleshootReport.confidence || '--'}</div>
						</div>
					{:else}
						<pre class="max-h-[260px] overflow-auto bg-black/40 border border-[#1b1b1b] p-3 text-[11px] text-[#888] whitespace-pre-wrap break-words">{troubleshootRaw || 'Diagnosis is still gathering details.'}</pre>
					{/if}
				</div>
				<div class="border border-[#222] bg-[#050505] p-4">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Troubleshoot Timeline</div>
					<div class="mt-3 space-y-2">
						{#if diagnosisLog.length === 0}
							<div class="text-xs text-[#666]">No timeline events yet.</div>
						{:else}
							{#each diagnosisLog as entry}
								<div class="border border-[#1d1d1d] bg-black/30 px-3 py-2">
									<div class="flex items-start justify-between gap-3">
										<div><div class="text-xs font-semibold {entry.error ? 'text-red-300' : 'text-white'}">{entry.title}</div><div class="mt-1 text-xs text-[#888]">{entry.summary}</div>{#if entry.detail}<div class="mt-1 text-[11px] text-[#666]">{entry.detail}</div>{/if}</div>
										<div class="text-[10px] text-[#555] whitespace-nowrap">{fmtDate(entry.timestamp)}</div>
									</div>
								</div>
							{/each}
						{/if}
					</div>
				</div>
			{:else}
				<div class="border border-[#222] bg-[#050505] p-4 space-y-3">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Execution task</div>
					<div class="text-sm">{#if taskDetailUrl(approvalContext?.linked_task)}<button type="button" class="text-white hover:text-white hover:underline font-mono" on:click={() => goto(taskDetailUrl(approvalContext?.linked_task))}>{taskLabel(approvalContext?.linked_task)}</button>{:else}<span class="text-white">{approvalContext?.linked_task ? taskLabel(approvalContext.linked_task) : 'No linked task'}</span>{/if}</div>
					{#if approvalContext?.linked_task}
						<div class="text-xs text-[#888]">{compact(approvalContext.linked_task.title || approvalContext.linked_task.description, 180)}</div>
						<div class="inline-flex items-center px-2 py-0.5 border uppercase {statusClass(taskStatus(approvalContext.linked_task))}">{taskStatus(approvalContext.linked_task)}</div>
					{/if}
					{#if executionRaw}<pre class="max-h-[220px] overflow-auto bg-black/40 border border-[#1b1b1b] p-3 text-[11px] text-[#888] whitespace-pre-wrap break-words">{executionRaw}</pre>{/if}
				</div>
				<div class="border border-[#222] bg-[#050505] p-4">
					<div class="text-[10px] uppercase tracking-wider text-[#666]">Execution Timeline</div>
					<div class="mt-3 space-y-2">
						{#if executionLog.length === 0}
							<div class="text-xs text-[#666]">No execution events yet.</div>
						{:else}
							{#each executionLog as entry}
								<div class="border border-[#1d1d1d] bg-black/30 px-3 py-2">
									<div class="flex items-start justify-between gap-3">
										<div><div class="text-xs font-semibold {entry.error ? 'text-red-300' : 'text-white'}">{entry.title}</div><div class="mt-1 text-xs text-[#888]">{entry.summary}</div>{#if entry.detail}<div class="mt-1 text-[11px] text-[#666]">{entry.detail}</div>{/if}</div>
										<div class="text-[10px] text-[#555] whitespace-nowrap">{fmtDate(entry.timestamp)}</div>
									</div>
								</div>
							{/each}
						{/if}
					</div>
				</div>
			{/if}
		</div>
	</aside>
{/if}
