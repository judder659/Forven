import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

export const ssr = false;

// Legacy combined Trades page — split into /paper-trades and /live-trades.
// Redirect by the old ?view= param and carry the ?select= deep-link (used by
// old All Trades blotter links and external bookmarks) to the right page.
export const load: PageLoad = ({ url }) => {
	const view = url.searchParams.get('view');
	const select = url.searchParams.get('select');
	const target = view === 'live' ? '/live-trades' : '/paper-trades';
	const query = select ? `?select=${encodeURIComponent(select)}` : '';
	redirect(307, `${target}${query}`);
};
