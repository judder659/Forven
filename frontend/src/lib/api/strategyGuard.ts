/**
 * Strategy AST-guard client — backs the AI Drop Zone "Scan" pre-flight.
 *
 * Calls POST /api/strategy-guard/scan, which runs the static AST guard
 * (forven/sandbox/ast_guard.py) against a strategy file WITHOUT executing it.
 * This is a UX pre-check only; registration enforces the same guard server-side
 * regardless. Kinds mirror the backend's AstReport.Finding.kind exactly.
 */

import { fetchApi } from './core';

export type AstFindingKind =
	| 'forbidden_import'
	| 'dynamic_exec'
	| 'file_too_large'
	| 'too_many_lines'
	| 'syntax_error';

export interface AstFinding {
	kind: AstFindingKind;
	lineno: number;
	col: number;
	message: string;
	node_repr: string;
}

export interface AstReport {
	ok: boolean;
	findings: AstFinding[];
	file_size_bytes: number;
	line_count: number;
}

export async function scanStrategy(path: string): Promise<AstReport> {
	return fetchApi<AstReport>('/strategy-guard/scan', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path })
	});
}
