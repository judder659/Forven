import { fetchApi } from './core';

/** An idea: the written reason behind a strategy (backend `hypotheses` row). */
export interface IdeaStrategy {
	id: string;
	display_id?: string | null;
	name?: string | null;
	stage?: string | null;
	symbol?: string | null;
	timeframe?: string | null;
}

export interface IdeaArtifact {
	id: string;
	source_type?: string | null;
	source_title?: string | null;
	source_ref?: string | null;
	claimed_edge?: string | null;
	created_at?: string | null;
}

export interface Idea {
	id: string;
	display_id?: string | null;
	title: string;
	market_thesis?: string | null;
	mechanism?: string | null;
	disproof?: string | null;
	why_now?: string | null;
	target_assets?: string[];
	target_timeframes?: string[];
	source_type?: string | null;
	origin_agent_id?: string | null;
	created_at?: string | null;
	strategies: IdeaStrategy[];
	artifacts: IdeaArtifact[];
}

export interface SubmitIdeaRequest {
	text?: string;
	title?: string;
	market_thesis?: string;
	mechanism?: string;
	target_assets?: string[];
	target_timeframes?: string[];
	notes?: string;
	url?: string;
}

export type SubmitIdeaResponse =
	| { ok: true; task_id: number | null }
	| { ok: false; error_code?: string; error: string; source_type?: string | null };

export type IdeaUrlPreview =
	| {
			ok: true;
			source_type?: string | null;
			url: string;
			title: string;
			content_preview: string;
			content_bytes: number;
			preview_truncated: boolean;
	  }
	| { ok: false; source_type?: string | null; error_code?: string; error: string };

/** Queue a strategy-creation task for the operator's idea (text, fields and/or a URL). */
export async function submitIdea(body: SubmitIdeaRequest): Promise<SubmitIdeaResponse> {
	return fetchApi('/ideas', { method: 'POST', body: JSON.stringify(body) });
}

/** Fetch a URL for the submit dialog's preview; nothing is saved. */
export async function previewIdeaUrl(url: string): Promise<IdeaUrlPreview> {
	return fetchApi('/ideas/preview_url', { method: 'POST', body: JSON.stringify({ url }) });
}

/** An idea with the strategies built from it. */
export async function getIdea(id: string): Promise<Idea> {
	return fetchApi(`/ideas/${encodeURIComponent(id)}`);
}
