import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// The Task Manager now lives on the Agent Hub (/agents?tab=tasks). Preserve any
// drill-through params (e.g. ?model= / ?role=) so old links keep filtering.
export const load: PageLoad = ({ url }) => {
	const params = new URLSearchParams(url.searchParams);
	params.set('tab', 'tasks');
	redirect(307, `/agents?${params.toString()}`);
};
