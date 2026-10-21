import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

export const ssr = false;

// The Approvals page was retired (2026-10): pending requests now wait under
// "Waiting for your decision" on the Agents page. Old links and notification
// toasts carry ?approval_id=, which lands on that request.
export const load: PageLoad = ({ url }) => {
	const id = url.searchParams.get('approval_id');
	redirect(307, `/agents${id ? `?decision=${encodeURIComponent(id)}` : ''}#decisions`);
};
