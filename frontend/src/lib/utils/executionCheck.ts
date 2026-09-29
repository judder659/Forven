/** Plain-language execution check explanations and the pre-save warning for executing strategies. */
import type { ExecutionCheck } from '$lib/api/executionCheck';

const LIVE_STAGES = new Set(['live_graduated', 'deployed', 'live']);
const PAPER_STAGES = new Set(['paper', 'paper_trading']);

export function isLiveStage(stage: string | null | undefined): boolean {
	return LIVE_STAGES.has(String(stage ?? '').toLowerCase());
}

export function isExecutingStage(stage: string | null | undefined): boolean {
	const value = String(stage ?? '').toLowerCase();
	return LIVE_STAGES.has(value) || PAPER_STAGES.has(value);
}

export interface CheckExplanation {
	title: string;
	body: string;
	/** What resolves it, in words; the panel's buttons act on it. */
	fix: string;
}

export function explainCheck(check: ExecutionCheck): CheckExplanation {
	const live = isLiveStage(check.stage);
	const noun = live ? 'live' : 'paper';
	const title = `New ${noun} entries are blocked`;
	switch (check.kind) {
		case 'params_changed':
			return {
				title,
				body: `Its settings changed after they were validated. A ${noun} strategy only trades the exact settings it was accepted with.`,
				fix: live
					? 'Accept a backtest of the new settings as the live baseline, or restore the validated settings.'
					: 'Restore the validated settings, or move it back to Gauntlet so the pipeline validates the new ones and promotes it back to paper.',
			};
		case 'unverified':
			return live
				? {
						title,
						body: 'There is no accepted execution evidence for this live configuration.',
						fix: 'Run a backtest of the current settings, then accept it here as the live baseline.',
					}
				: {
						title,
						body: 'Its paper promotion has no verified backtest of the current settings.',
						fix: 'Move it back to Gauntlet (stage control on its Forge page). The pipeline re-runs the gauntlet on the current settings and, if it passes, promotes it back to paper with fresh evidence. A Forge backtest alone does not verify paper, and there is no operator shortcut.',
					};
		case 'engine_changed':
			return { title, body: 'The backtest engine changed since the settings were validated.', fix: live ? 'Run a fresh backtest of the current settings and accept it here.' : 'Move it back to Gauntlet so it re-validates on the current engine.' };
		case 'source_changed':
			return { title, body: "The strategy's code changed since it was validated.", fix: live ? 'Run a fresh backtest of the current code and accept it here.' : 'Move it back to Gauntlet so it re-validates on the current code.' };
		case 'source_unavailable':
			return {
				title,
				body: "The strategy's code could not be loaded for this check. Right after a restart the strategies take a moment to load, so this usually clears on its own.",
				fix: 'If it lasts more than a few minutes, the strategy file may be missing or failing to import.',
			};
		case 'config_changed':
			return { title, body: 'Its market or timeframe changed since it was validated.', fix: 'Restore the validated market and timeframe, or validate the new ones.' };
		case 'unavailable':
			return { title, body: 'The validation evidence could not be read just now, so entries are held until it can.', fix: 'This usually clears on the next check.' };
		default:
			return { title, body: check.reason ?? 'The execution evidence does not match the current settings.', fix: '' };
	}
}

export function formatSetting(value: unknown): string {
	if (value === null || value === undefined) return 'not set';
	if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(10)));
	if (typeof value === 'string') return value;
	if (typeof value === 'boolean') return value ? 'true' : 'false';
	return JSON.stringify(value);
}

function flatten(value: unknown, prefix = '', out: Record<string, unknown> = {}): Record<string, unknown> {
	if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
		for (const [key, inner] of Object.entries(value as Record<string, unknown>)) flatten(inner, prefix ? `${prefix}.${key}` : key, out);
	} else if (prefix) {
		out[prefix] = value;
	}
	return out;
}

export interface SettingChange {
	key: string;
	from: unknown;
	to: unknown;
}

/** Settings a save would change, as dotted keys. */
export function settingChanges(current: Record<string, unknown> | null | undefined, next: Record<string, unknown> | null | undefined): SettingChange[] {
	const before = flatten(current ?? {});
	const after = flatten(next ?? {});
	const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
	return keys
		.filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]))
		.map((key) => ({ key, from: before[key], to: after[key] }));
}

/**
 * The warning before saving settings on a paper or live strategy: any change to the
 * validated settings refuses new entries until it is accepted or undone. Null when
 * the strategy is not trading or the save changes nothing.
 */
export function executingEditWarning(input: {
	strategyId: string;
	stage: string | null | undefined;
	current: Record<string, unknown> | null | undefined;
	next: Record<string, unknown> | null | undefined;
}): string | null {
	if (!isExecutingStage(input.stage)) return null;
	const changes = settingChanges(input.current, input.next);
	if (!changes.length) return null;
	const live = isLiveStage(input.stage);
	const shown = changes.slice(0, 6).map((change) => `• ${change.key}: ${formatSetting(change.from)} → ${formatSetting(change.to)}`);
	const more = changes.length > shown.length ? `\n• …and ${changes.length - shown.length} more` : '';
	const resolve = live
		? 'accept a backtest of the new settings as the live baseline, or restore the validated settings'
		: 'move it back to Gauntlet to validate the new settings, or restore the validated settings';
	return `${input.strategyId} trades ${live ? 'LIVE' : 'on paper'}. Saving changes:\n${shown.join('\n')}${more}\n\nIf these differ from the settings it was validated with, new ${live ? 'live' : 'paper'} entries are refused until you ${resolve} (Execution check at the top of the strategy page). Save anyway?`;
}
