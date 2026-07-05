import type { PageLoad } from './$types';
import { getForvenDashboard } from '$lib/api';
import type { ForvenDashboardResponse } from '$lib/api';

export const ssr = false;

export const load: PageLoad = async () => {
	const dashboardResult = await Promise.allSettled([getForvenDashboard()]);
	const dashboard = dashboardResult[0].status === 'fulfilled' ? dashboardResult[0].value : null;
	return { dashboard } satisfies { dashboard: ForvenDashboardResponse | null };
};
