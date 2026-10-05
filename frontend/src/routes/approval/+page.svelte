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
	import GoLiveApprovalDialog from '$lib/components/approvals/GoLiveApprovalDialog.svelte';
	import DenyReasonPicker from '$lib/components/approvals/DenyReasonPicker.svelte';
	import ConfirmDialog, { type ConfirmDialogSpec } from '$lib/components/ConfirmDialog.svelte';
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
	// The Brain also self-approves gauntlet->paper when the pipeline's
	// promotion_mode is 'auto' (the shipped default), so the page shows the
	// effective state rather than the one flag it used to read.
	let promotionMode = '';
	let allowAutoLivePromotion = false;
	let autoApproveDethrone = true;
	let settingsLoading = true;
	let approvalModes: Record<string, string> = {};
	let defaultApprovalMode = '';

	let filterType = '';
	let filterText = '';
	let collapsedGroups: Set<string> = new Set();
	let denyPickerId: number | null = null;
	let goLiveApproval: ApprovalRecord | null = null;
	let goLivePreferredTab: DrawerTab | undefined = undefined;
	let confirmSpec: (ConfirmDialogSpec & { run: () => Promise<void> }) | null = null;
	let confirmBusy = false;

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
				return 'text-sc-ink2 border-sc-line2 bg-sc-panel2';
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
				return 'text-sc-ink2 border-sc-line2 bg-sc-panel2';
			default:
				return 'text-sc-ink2 border-sc-line2 bg-sc-panel2';
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
				return 'border-sc-line2 bg-sc-panel2 text-sc-ink2';
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
				return 'border-sc-line2 bg-sc-panel2 text-sc-ink2';
			case 'off':
				return 'border-emerald-700 bg-emerald-900/30 text-emerald-300';
			case 'manual':
				return 'border-yellow-700 bg-yellow-900/30 text-yellow-300';
			default:
				return 'border-sc-line2 bg-sc-panel2 text-sc-ink3';
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
					: 'text-sc-ink2 border-sc-line2 bg-sc-panel2';
		return { label: niceLabel, className };
	}

	let bulkApproving = false;

	function autoApprovableIds(): number[] {
		return approvals
			.filter(
				(a) =>
					(a.classifier_recommendation || '').toLowerCase() === 'auto_approve' &&
					(a.status || '').toLowerCase() === 'pending_approval' &&
					!requiresGoLive(a),
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

	function confirmBulkApprove() {
		const ids = autoApprovableIds();
		if (ids.length === 0) {
			actionMessage = 'No auto_approve candidates to bulk approve.';
			return;
		}
		confirmSpec = {
			title: `Approve ${ids.length} item${ids.length === 1 ? '' : 's'}?`,
			warn: 'Approves every pending item the classifier marked safe to auto-approve. Live promotions are left out: they always need your typed GO LIVE.',
			cta: 'Approve all',
			run: runBulkApprove,
		};
	}

	async function runBulkApprove() {
		const ids = autoApprovableIds();
		if (ids.length === 0) return;
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

	const truthy = (value: unknown, fallback = false) =>
		value === undefined || value === null ? fallback : String(value).toLowerCase() === 'true';

	async function loadSettings() {
		try {
			const settings = (await getSettings()) as unknown as Record<string, unknown>;
			autoApproveCodeEdits = truthy(settings.auto_approve_code_edits);
			autoApprovePromotions = truthy(settings.auto_approve_promotions);
			promotionMode = String(settings.promotion_mode || '').trim().toLowerCase();
			allowAutoLivePromotion = truthy(settings.allow_auto_live_promotion);
			autoApproveDethrone = truthy(settings.auto_approve_dethrone, true);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load approval settings';
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

	function flash(message: string) {
		actionMessage = message;
		setTimeout(() => actionMessage = null, 3000);
	}

	async function runConfirm() {
		if (!confirmSpec) return;
		confirmBusy = true;
		try {
			await confirmSpec.run();
			confirmSpec = null;
		} catch (e) {
			error = e instanceof Error ? e.message : 'The change could not be saved';
			confirmSpec = null;
		} finally {
			confirmBusy = false;
		}
	}

	async function setAutoApproveCodeEdits(nextState: boolean) {
		await updateSettingsSection('bot-operations', { auto_approve_code_edits: String(nextState) });
		autoApproveCodeEdits = nextState;
		flash(`Code edit auto-approval ${nextState ? 'enabled' : 'disabled'}.`);
	}

	function toggleAutoApproveCodeEdits() {
		const nextState = !autoApproveCodeEdits;
		if (!nextState) {
			void setAutoApproveCodeEdits(false).catch((e) => {
				error = e instanceof Error ? e.message : 'Failed to update code edit auto-approval';
			});
			return;
		}
		confirmSpec = {
			title: 'Approve code edits automatically?',
			warn: 'Agent code changes will go ahead without your review until you turn this back off.',
			cta: 'Turn on',
			danger: true,
			run: () => setAutoApproveCodeEdits(true),
		};
	}

	function toggleAutoApprovePromotions() {
		if (promotionsAuto) {
			confirmSpec = {
				title: 'Review promotions yourself?',
				warn: 'Strategies moving from gauntlet to paper will wait here for your approval.',
				rows: promotionModeAuto ? [['Pipeline promotion mode', 'auto → manual']] : [],
				cta: 'Switch to manual review',
				run: async () => {
					await updateSettingsSection('bot-operations', { auto_approve_promotions: 'false' });
					if (promotionModeAuto) await updateSettingsSection('pipeline', { promotion_mode: 'manual' });
					autoApprovePromotions = false;
					promotionMode = promotionModeAuto ? 'manual' : promotionMode;
					flash('Promotions now wait for your approval.');
				},
			};
			return;
		}
		confirmSpec = {
			title: 'Approve promotions automatically?',
			warn: allowAutoLivePromotion
				? 'Gauntlet to paper AND paper to live will approve themselves, because "Allow automatic go-live" is on in Settings.'
				: 'Gauntlet to paper promotions will approve themselves. Going live still always needs your typed GO LIVE.',
			rows: autoApproveDethrone ? [] : [['Dethrones', 'also approve themselves']],
			cta: 'Turn on',
			danger: true,
			run: async () => {
				await updateSettingsSection('bot-operations', { auto_approve_promotions: 'true' });
				autoApprovePromotions = true;
				flash('Promotion auto-approval enabled.');
			},
		};
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
			return;
		}
		void submitDecision(approval.id, 'deny');
	}

	async function confirmDeny(approvalId: number, reason: string) {
		denyPickerId = null;
		await submitDecision(approvalId, 'deny', undefined, reason);
	}

	// GO-LIVE-1: the backend flags rows whose approve needs the typed GO LIVE
	// and a notional ceiling; older backends without the flag fall back to the
	// same target-stage test the approve endpoint applies.
	function requiresGoLive(record: ApprovalRecord | null | undefined): boolean {
		if (!record) return false;
		if (typeof record.requires_go_live === 'boolean') return record.requires_go_live;
		if (record.approval_type !== 'strategy_promotion_approval') return false;
		const payload = (record.payload ?? {}) as Record<string, unknown>;
		const target = String(payload.recommended_target_stage || payload.requested_status || record.requested_status || '');
		return target.trim().toLowerCase() === 'live_graduated';
	}

	function approve(approvalId: number, preferredTab?: DrawerTab) {
		const record =
			approvals.find((row) => row.id === approvalId) ??
			(approvalContext?.approval?.id === approvalId ? approvalContext.approval : null);
		if (requiresGoLive(record)) {
			error = null;
			goLiveApproval = record;
			goLivePreferredTab = preferredTab;
			return;
		}
		void submitDecision(approvalId, 'approve', preferredTab);
	}

	async function confirmGoLive(ceilingUsd: number) {
		if (!goLiveApproval) return;
		const approvalId = goLiveApproval.id;
		const ok = await submitDecision(approvalId, 'approve', goLivePreferredTab, undefined, {
			confirm: 'GO LIVE',
			live_notional_ceiling_usd: ceilingUsd,
		});
		if (ok) goLiveApproval = null;
	}

	async function submitDecision(
		approvalId: number,
		action: PendingDecision,
		preferredTab?: DrawerTab,
		reasonOverride?: string,
		goLive?: { confirm: string; live_notional_ceiling_usd: number },
	): Promise<boolean> {
		if (isBusy(approvalId)) return false;
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
			return true;
		} catch (e) {
			error = e instanceof Error ? e.message : `Failed to ${action} approval #${approvalId}`;
			return false;
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
	$: promotionModeAuto = promotionMode === 'auto';
	$: promotionsAuto = autoApprovePromotions || promotionModeAuto;
	$: promotionsSource = autoApprovePromotions ? 'Approvals toggle' : promotionModeAuto ? 'Pipeline promotion mode' : '';
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
			<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Approvals</h1>
			<div class="rounded-md flex bg-sc-panel2 border border-sc-line p-0.5">
				<button class="px-3 py-1 text-[12px] {viewMode === 'pending' ? 'bg-sc-line2 text-sc-ink' : 'text-sc-ink2'}" on:click={() => switchView('pending')}>Pending</button>
				<button class="px-3 py-1 text-[12px] {viewMode === 'history' ? 'bg-sc-line2 text-sc-ink' : 'text-sc-ink2'}" on:click={() => switchView('history')}>History</button>
			</div>
		</div>
		<div class="flex items-center gap-3">
			{#if !settingsLoading}
				<button type="button" class="rounded-md flex items-center gap-2 px-3 py-1.5 border {promotionsAuto ? 'bg-emerald-900/30 border-emerald-700 text-emerald-400' : 'bg-sc-panel2 border-sc-line2 text-sc-ink2'}" title={promotionsAuto ? `Auto-approving gauntlet to paper (set by ${promotionsSource})` : 'Promotions wait for your review'} on:click={toggleAutoApprovePromotions} data-testid="toggle-auto-promotions">
					<div class="w-3 h-3 rounded-full {promotionsAuto ? 'bg-emerald-500' : 'bg-sc-line2'}"></div>
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">Auto promotions</span>
				</button>
				<button type="button" class="rounded-md flex items-center gap-2 px-3 py-1.5 border {autoApproveCodeEdits ? 'bg-emerald-900/30 border-emerald-700 text-emerald-400' : 'bg-sc-panel2 border-sc-line2 text-sc-ink2'}" on:click={toggleAutoApproveCodeEdits}>
					<div class="w-3 h-3 rounded-full {autoApproveCodeEdits ? 'bg-emerald-500' : 'bg-sc-line2'}"></div>
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">Auto code edits</span>
				</button>
			{/if}
			<a href="/settings/approvals" class="text-xs border border-sc-line2 px-3 py-1.5 text-sc-ink2 hover:text-sc-ink hover:border-sc-line2">Configure approval modes</a>
			{#if viewMode === 'pending' && autoApprovableIds().length > 0}
				<button
					type="button"
					disabled={bulkApproving}
					class="rounded-md text-[12px] border border-emerald-700 bg-emerald-900/20 hover:bg-emerald-900/40 text-emerald-300 px-3 py-1.5 disabled:opacity-40"
					on:click={confirmBulkApprove}
				>
					{bulkApproving ? 'Approving...' : `Bulk approve (${autoApprovableIds().length})`}
				</button>
			{/if}
			<button type="button" disabled={refreshing} class="rounded-md text-[12px] border border-sc-line2 px-3 py-1.5 text-sc-ink2 disabled:opacity-40" on:click={() => void loadApprovals(true)}>{refreshing ? 'Refreshing...' : 'Refresh'}</button>
		</div>
	</header>

	{#if actionMessage}<div class="bg-emerald-900/20 border border-emerald-800 text-emerald-300 text-xs px-3 py-2 rounded">{actionMessage}</div>{/if}
	{#if error}<div class="bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{error}</div>{/if}

	<!-- Who approves what: every lever the backend honours, in one place. -->
	<div class="rounded-md grid grid-cols-2 gap-px border border-sc-line bg-sc-line lg:grid-cols-6" data-testid="who-approves-what">
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Gauntlet → paper</div>
			<div class="mt-1 text-sm font-semibold {promotionsAuto ? 'text-yellow-300' : 'text-emerald-300'}" data-testid="who-promotions">{settingsLoading ? '…' : promotionsAuto ? 'Automatic' : 'You approve'}</div>
			{#if promotionsAuto}<div class="mt-0.5 text-[11px] text-sc-ink3">Set by {promotionsSource}</div>{/if}
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Paper → live</div>
			<div class="mt-1 text-sm font-semibold {allowAutoLivePromotion ? 'text-red-300' : 'text-emerald-300'}" data-testid="who-live">{settingsLoading ? '…' : allowAutoLivePromotion ? 'Automatic' : 'You type GO LIVE'}</div>
			{#if allowAutoLivePromotion}<a href="/settings#trading/bot-operations.allow_auto_live_promotion" class="mt-0.5 block text-[11px] text-red-300 underline">"Allow automatic go-live" is on</a>{/if}
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Dethrones</div>
			<div class="mt-1 text-sm font-semibold text-sc-ink">{settingsLoading ? '…' : autoApproveDethrone || promotionsAuto ? 'Automatic' : 'You approve'}</div>
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Code edits</div>
			<div class="mt-1 text-sm font-semibold {autoApproveCodeEdits ? 'text-yellow-300' : 'text-sc-ink'}">{settingsLoading ? '…' : autoApproveCodeEdits ? 'Automatic' : 'You approve'}</div>
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Showing</div>
			<div class="mt-1 text-sm font-semibold text-sc-ink">{approvals.length}</div>
		</div>
		<div class="bg-sc-panel px-4 py-3">
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Oldest waiting</div>
			<div class="mt-1 text-sm font-semibold text-sc-ink">{oldestVisibleAge}</div>
		</div>
	</div>

	<div class="flex flex-wrap items-center gap-2">
		<select bind:value={filterType} class="rounded-md bg-sc-bg border border-sc-line text-xs px-2 py-1.5 text-sc-ink">
			<option value="">All types</option>
			{#each presentTypes as presentType}
				<option value={presentType}>{friendlyTitle(presentType)}</option>
			{/each}
		</select>
		<input
			type="text"
			placeholder="Filter by strategy, reason, actor..."
			class="rounded-md flex-1 min-w-[220px] max-w-md bg-sc-bg border border-sc-line text-xs px-3 py-1.5 text-sc-ink"
			bind:value={filterText}
		/>
		{#if filterType || filterText}
			<button type="button" class="rounded-md text-[12px] border border-sc-line2 px-3 py-1.5 text-sc-ink2 hover:text-sc-ink" on:click={() => { filterType = ''; filterText = ''; }}>Clear filters</button>
			<span class="text-xs text-sc-ink3">{filteredApprovals.length} of {approvals.length} shown</span>
		{/if}
	</div>

	{#if loading}
		<div class="text-sc-ink3">Loading approvals...</div>
	{:else if filteredApprovals.length === 0}
		<div class="text-sc-ink3">
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
					class="rounded-md w-full flex items-center gap-2 border border-sc-line bg-sc-panel px-3 py-2 text-left hover:border-sc-line2"
					on:click={() => toggleGroup(groupType)}
				>
					<span class="text-sm font-semibold text-sc-ink uppercase tracking-wider">{friendlyTitle(groupType)}</span>
					<span class="rounded-md inline-flex items-center justify-center min-w-[1.5rem] px-1.5 py-0.5 text-xs border border-sc-line2 bg-sc-bg text-sc-ink">{groupRows.length}</span>
					<span class="ml-auto text-sc-ink3 text-xs">{collapsedGroups.has(groupType) ? '+ expand' : '− collapse'}</span>
				</button>
			{/if}
			{#if !collapsedGroups.has(groupType)}
			{#each groupRows as approval (approval.id)}
				<article class="terminal-card p-4 space-y-3">
					<div class="flex items-start justify-between gap-3">
						<div>
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Approval #{approval.id} <span class="font-mono normal-case text-sc-ink4">· {approval.approval_type}</span></div>
							<div class="text-lg font-semibold text-sc-ink">{friendlyTitle(approval.approval_type)}</div>
							<div class="mt-1 text-sm text-sc-ink2">{reasonText(approval)}</div>
							<div class="mt-2 flex flex-wrap items-center gap-2">
								<span
									class="inline-flex items-center px-2 py-0.5 border font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {classifierBadgeClass(approval.classifier_recommendation)}"
									title={approval.classifier_reasoning || 'Smart-approval classifier has not run yet for this item.'}
								>
									{classifierLabel(approval.classifier_recommendation)}
								</span>
								{#if effectiveMode(approval.approval_type)}
									<span
										class="inline-flex items-center px-2 py-0.5 border font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {modeBadgeClass(effectiveMode(approval.approval_type))}"
										title="Active policy for '{approval.approval_type}' (configure under Approval Modes). 'smart' auto-approves classifier auto_approve rows; 'off' auto-approves; 'manual' always requires review."
									>
										{effectiveMode(approval.approval_type)}
									</span>
								{/if}
								{#if approval.classifier_model}
									<span class="text-[10px] text-sc-ink3">{approval.classifier_model}</span>
								{/if}
								{#if viewMode === 'pending'}
									<button
										type="button"
										disabled={isBusy(approval.id)}
										class="rounded-md text-[12px] border border-sc-line2 px-2 py-0.5 text-sc-ink2 hover:text-sc-ink disabled:opacity-40"
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
								<div class="mt-1 text-[11px] text-sc-ink3 italic">{compact(approval.classifier_reasoning, 200)}</div>
							{/if}
						</div>
						<div class="text-right text-xs text-sc-ink3">
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
						<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Agent run</div>
							<div class="mt-1 font-mono">{#if taskDetailUrl(approval.linked_task)}<button type="button" class="text-sc-ink2 hover:text-sc-ink hover:underline" on:click={() => goto(taskDetailUrl(approval.linked_task))}>{taskLabel(approval.linked_task)}</button>{:else}<span class="text-sc-ink2">{taskLabel(approval.linked_task)}</span>{/if}</div>
							{#if approval.linked_task}
								<div class="mt-1 text-sc-ink2">{compact(approval.linked_task.title || approval.linked_task.description)}</div>
							{/if}
						</div>
						<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2">
							<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Troubleshoot</div>
							<div class="mt-1 font-mono">{#if taskDetailUrl(approval.troubleshoot_task)}<button type="button" class="text-yellow-300 hover:text-yellow-200 hover:underline" on:click={() => goto(taskDetailUrl(approval.troubleshoot_task))}>{taskLabel(approval.troubleshoot_task)}</button>{:else}<span class="text-yellow-300">{taskLabel(approval.troubleshoot_task)}</span>{/if}</div>
							<div class="mt-1 text-sc-ink3">{approval.troubleshoot_task ? taskStatus(approval.troubleshoot_task) : 'Not started'}</div>
						</div>
					</div>

					{#if approval.approval_type === 'skill_update_proposal' && typeof approval.payload === 'object' && approval.payload}
						<SkillUpdateProposalCard payload={approval.payload as Record<string, unknown>} />
					{:else if payloadRenderer(approval.approval_type) && typeof approval.payload === 'object' && approval.payload}
						<svelte:component this={payloadRenderer(approval.approval_type)} {approval} />
					{:else}
						<pre class="rounded-md text-[11px] text-sc-ink2 bg-sc-bg border border-sc-line p-2 max-h-40 overflow-auto whitespace-pre-wrap">{typeof approval.payload === 'object' ? JSON.stringify(approval.payload, null, 2) : String(approval.payload || '-')}</pre>
					{/if}

					<div class="flex flex-wrap gap-2">
						{#if canTroubleshoot(approval)}
							<button type="button" disabled={isLaunchingTroubleshoot(approval.id)} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => void launchTroubleshoot(approval.id)}>
								{isLaunchingTroubleshoot(approval.id) ? 'Launching...' : approval.troubleshoot_task ? 'Open Troubleshoot' : 'Troubleshoot'}
							</button>
						{/if}
						<button type="button" class="terminal-button text-[12px] px-3 py-2" on:click={() => openInspector(approval, approvalStatus(approval) === 'approved' ? 'execution' : 'diagnosis')}>
							{approvalStatus(approval) === 'approved' ? 'Watch Run' : 'Details'}
						</button>
						{#if viewMode === 'pending'}
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button-primary text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => approve(approval.id)}>{isBusy(approval.id) ? 'Approving...' : requiresGoLive(approval) ? 'Approve (go live)' : 'Approve'}</button>
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => void handleUserComplete(approval.id)}>{isBusy(approval.id) ? 'Completing...' : 'I Did This'}</button>
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button-danger text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => handleDenyClick(approval)}>{isBusy(approval.id) ? 'Denying...' : 'Deny'}</button>
						{/if}
					</div>

					{#if denyPickerId === approval.id && viewMode === 'pending' && selectedApprovalId === null}
						<DenyReasonPicker busy={isBusy(approval.id)} on:confirm={(event) => void confirmDeny(approval.id, event.detail.reason)} on:cancel={() => denyPickerId = null} />
					{/if}

					{#if viewMode === 'pending'}
						<div class="flex gap-2">
							<input type="text" placeholder="Revision feedback..." class="rounded-md flex-1 bg-sc-bg border border-sc-line text-xs px-3 py-2 text-sc-ink" value={reviseInput[approval.id] || ''} on:input={(event) => onReviseInput(approval.id, (event.currentTarget as HTMLInputElement).value)} />
							<button type="button" disabled={isBusy(approval.id)} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => void submitDecision(approval.id, 'revise')}>{isBusy(approval.id) ? 'Revising...' : 'Revise'}</button>
						</div>
					{:else}
						<div class="grid sm:grid-cols-4 gap-3 text-xs border-t border-sc-line pt-3">
							<div><div class="text-sc-ink3 uppercase tracking-wider">Decision</div><div class="text-sc-ink font-semibold">{approval.decision || 'N/A'}</div></div>
							<div><div class="text-sc-ink3 uppercase tracking-wider">Decided</div><div class="text-sc-ink">{fmtDate(approval.decided_at)}</div></div>
							<div><div class="text-sc-ink3 uppercase tracking-wider">Actor</div><div class="text-sc-ink font-mono">{approval.actor || '-'}</div></div>
							<div><div class="text-sc-ink3 uppercase tracking-wider">Feedback</div><div class="text-sc-ink">{approval.feedback || '-'}</div></div>
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
	<button type="button" class="fixed inset-0 z-40 bg-sc-bg/70" aria-label="Close approval inspector" on:click={closeInspector}></button>
	<aside class="fixed inset-y-0 right-0 z-50 w-full max-w-[780px] bg-sc-panel border-l border-sc-line flex flex-col">
		<header class="border-b border-sc-line px-6 py-4 space-y-3">
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Approval Inspector</div>
					<div class="mt-1 text-xl font-semibold text-sc-ink">{selectedApproval ? `Approval #${selectedApproval.id}` : `Approval #${selectedApprovalId}`}</div>
					<div class="mt-1 text-sm text-sc-ink2">{selectedApproval ? reasonText(selectedApproval) : 'Loading approval context...'}</div>
				</div>
				<div class="flex items-center gap-2">
					{#if selectedApproval && canTroubleshoot(selectedApproval)}
						<button type="button" disabled={isLaunchingTroubleshoot(selectedApproval.id)} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => void launchTroubleshoot(selectedApproval.id)}>
							{isLaunchingTroubleshoot(selectedApproval.id) ? 'Launching...' : selectedApproval.troubleshoot_task ? 'Refresh Diagnosis' : 'Run Troubleshoot'}
						</button>
					{/if}
					<button type="button" disabled={contextLoading || contextRefreshing} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => selectedApprovalId !== null && void loadApprovalContext(selectedApprovalId)}>{contextRefreshing ? 'Refreshing...' : 'Refresh'}</button>
					<button type="button" class="terminal-button text-[12px] px-3 py-2" on:click={closeInspector}>Close</button>
				</div>
			</div>

			<div class="grid gap-3 md:grid-cols-3 text-xs">
				<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Approval status</div><div class="mt-1 inline-flex items-center px-2 py-0.5 border uppercase {statusClass(approvalStatus(selectedApproval))}">{selectedApproval?.status || 'loading'}</div></div>
				<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Execution</div><div class="mt-1 font-mono">{#if taskDetailUrl(approvalContext?.linked_task)}<button type="button" class="text-sc-ink2 hover:text-sc-ink hover:underline" on:click={() => goto(taskDetailUrl(approvalContext?.linked_task))}>{taskLabel(approvalContext?.linked_task)}</button>{:else}<span class="text-sc-ink2">{taskLabel(approvalContext?.linked_task)}</span>{/if}</div></div>
				<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2"><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Troubleshoot</div><div class="mt-1 font-mono">{#if taskDetailUrl(approvalContext?.troubleshoot_task)}<button type="button" class="text-yellow-300 hover:text-yellow-200 hover:underline" on:click={() => goto(taskDetailUrl(approvalContext?.troubleshoot_task))}>{taskLabel(approvalContext?.troubleshoot_task)}</button>{:else}<span class="text-yellow-300">{taskLabel(approvalContext?.troubleshoot_task)}</span>{/if}</div></div>
			</div>

			<div class="flex items-center gap-2">
				<button type="button" class="rounded-md px-3 py-1.5 text-[12px] border {drawerTab === 'diagnosis' ? 'border-yellow-500 text-yellow-200 bg-yellow-900/20' : 'border-sc-line2 text-sc-ink2'}" on:click={() => drawerTab = 'diagnosis'}>Diagnosis</button>
				<button type="button" disabled={!approvalContext?.linked_task} class="rounded-md px-3 py-1.5 text-[12px] border {drawerTab === 'execution' ? 'border-sc-ink text-sc-ink bg-sc-panel2' : 'border-sc-line2 text-sc-ink2'} disabled:opacity-40" on:click={() => drawerTab = 'execution'}>Execution</button>
			</div>

			{#if selectedApproval && approvalStatus(selectedApproval) === 'pending_approval'}
				<div class="flex flex-wrap gap-2">
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button-primary text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => selectedApproval && approve(selectedApproval.id, 'execution')}>{isBusy(selectedApproval.id) ? 'Approving...' : requiresGoLive(selectedApproval) ? 'Approve (go live)' : 'Approve + Watch'}</button>
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => void handleUserComplete(selectedApproval.id)}>{isBusy(selectedApproval.id) ? 'Completing...' : 'I Did This'}</button>
					<button type="button" disabled={isBusy(selectedApproval.id)} class="terminal-button-danger text-[12px] px-3 py-2 disabled:opacity-40" on:click={() => selectedApproval && handleDenyClick(selectedApproval)}>{isBusy(selectedApproval.id) ? 'Denying...' : 'Deny'}</button>
				</div>
				{#if denyPickerId === selectedApproval.id}
					<DenyReasonPicker busy={isBusy(selectedApproval.id)} on:confirm={(event) => selectedApproval && void confirmDeny(selectedApproval.id, event.detail.reason)} on:cancel={() => denyPickerId = null} />
				{/if}
			{/if}
		</header>

		<div class="flex-1 overflow-auto p-6 space-y-4">
			{#if contextError}<div class="bg-red-900/20 border border-red-800 text-red-300 text-xs px-3 py-2 rounded">{contextError}</div>{/if}
			{#if contextLoading && !approvalContext}
				<div class="text-sc-ink3">Loading approval context...</div>
			{:else if drawerTab === 'diagnosis'}
				<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Diagnosis output</div>
					{#if !approvalContext?.troubleshoot_task}
						<div class="text-xs text-sc-ink2">No troubleshoot run yet. Start one to get a root-cause report before approving the fix.</div>
					{:else if troubleshootReport}
						<div class="space-y-3 text-sm">
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Summary</div><div class="mt-1 text-sc-ink">{troubleshootReport.summary || 'No summary returned.'}</div></div>
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Root Cause</div><div class="mt-1 text-yellow-200">{troubleshootReport.rootCause || 'No root cause returned.'}</div></div>
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Recommended Fix</div><div class="mt-1 text-sc-ink">{troubleshootReport.recommendedFix.length > 0 ? troubleshootReport.recommendedFix.join(' | ') : 'No fix recommendation yet.'}</div></div>
							<div><div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Validation Plan</div><div class="mt-1 text-sc-ink">{troubleshootReport.validationPlan.length > 0 ? troubleshootReport.validationPlan.join(' | ') : 'No validation plan yet.'}</div></div>
							<div class="text-[11px] text-sc-ink3">Files: {troubleshootReport.affectedFiles.length > 0 ? troubleshootReport.affectedFiles.join(', ') : '--'} | Risk {troubleshootReport.riskLevel || '--'} | Confidence {troubleshootReport.confidence || '--'}</div>
						</div>
					{:else}
						<pre class="rounded-md max-h-[260px] overflow-auto bg-sc-bg/40 border border-sc-line p-3 text-[11px] text-sc-ink2 whitespace-pre-wrap break-words">{troubleshootRaw || 'Diagnosis is still gathering details.'}</pre>
					{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-4">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Troubleshoot Timeline</div>
					<div class="mt-3 space-y-2">
						{#if diagnosisLog.length === 0}
							<div class="text-xs text-sc-ink3">No timeline events yet.</div>
						{:else}
							{#each diagnosisLog as entry}
								<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2">
									<div class="flex items-start justify-between gap-3">
										<div><div class="text-xs font-semibold {entry.error ? 'text-red-300' : 'text-sc-ink'}">{entry.title}</div><div class="mt-1 text-xs text-sc-ink2">{entry.summary}</div>{#if entry.detail}<div class="mt-1 text-[11px] text-sc-ink3">{entry.detail}</div>{/if}</div>
										<div class="text-[10px] text-sc-ink3 whitespace-nowrap">{fmtDate(entry.timestamp)}</div>
									</div>
								</div>
							{/each}
						{/if}
					</div>
				</div>
			{:else}
				<div class="rounded-md border border-sc-line bg-sc-panel p-4 space-y-3">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Agent run</div>
					<div class="text-sm">{#if taskDetailUrl(approvalContext?.linked_task)}<button type="button" class="text-sc-ink hover:text-sc-ink hover:underline font-mono" on:click={() => goto(taskDetailUrl(approvalContext?.linked_task))}>{taskLabel(approvalContext?.linked_task)}</button>{:else}<span class="text-sc-ink">{approvalContext?.linked_task ? taskLabel(approvalContext.linked_task) : 'No linked task'}</span>{/if}</div>
					{#if approvalContext?.linked_task}
						<div class="text-xs text-sc-ink2">{compact(approvalContext.linked_task.title || approvalContext.linked_task.description, 180)}</div>
						<div class="inline-flex items-center px-2 py-0.5 border uppercase {statusClass(taskStatus(approvalContext.linked_task))}">{taskStatus(approvalContext.linked_task)}</div>
					{/if}
					{#if executionRaw}<pre class="rounded-md max-h-[220px] overflow-auto bg-sc-bg/40 border border-sc-line p-3 text-[11px] text-sc-ink2 whitespace-pre-wrap break-words">{executionRaw}</pre>{/if}
				</div>
				<div class="rounded-md border border-sc-line bg-sc-panel p-4">
					<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Execution Timeline</div>
					<div class="mt-3 space-y-2">
						{#if executionLog.length === 0}
							<div class="text-xs text-sc-ink3">No execution events yet.</div>
						{:else}
							{#each executionLog as entry}
								<div class="rounded-md border border-sc-line bg-sc-bg/30 px-3 py-2">
									<div class="flex items-start justify-between gap-3">
										<div><div class="text-xs font-semibold {entry.error ? 'text-red-300' : 'text-sc-ink'}">{entry.title}</div><div class="mt-1 text-xs text-sc-ink2">{entry.summary}</div>{#if entry.detail}<div class="mt-1 text-[11px] text-sc-ink3">{entry.detail}</div>{/if}</div>
										<div class="text-[10px] text-sc-ink3 whitespace-nowrap">{fmtDate(entry.timestamp)}</div>
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

{#if goLiveApproval}
	<GoLiveApprovalDialog
		approval={goLiveApproval}
		busy={isBusy(goLiveApproval.id)}
		errorMessage={error}
		on:cancel={() => { goLiveApproval = null; }}
		on:confirm={(event) => void confirmGoLive(event.detail.ceilingUsd)}
	/>
{/if}

{#if confirmSpec}
	<ConfirmDialog spec={confirmSpec} busy={confirmBusy} on:cancel={() => { if (!confirmBusy) confirmSpec = null; }} on:confirm={runConfirm} />
{/if}
