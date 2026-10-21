import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

export const ssr = false;

// The operator profile moved into Settings > AI & agents ("What the assistant
// knows about you", 2026-10). Old links land there.
export const load: PageLoad = () => {
	redirect(307, '/settings#ai');
};
