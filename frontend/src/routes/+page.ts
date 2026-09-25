import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import { getLiveFleet } from '$lib/api/dashboard';
import type { LiveFleet } from '$lib/api/dashboard';

export const ssr = false;

export const load: PageLoad = async ({ url }) => {
	const view = url.searchParams.get('view');
	if (view === 'quant_factory' || view === 'quant' || view === 'beta' || view === 'spec') {
		throw redirect(301, '/');
	}

	const [fleet] = await Promise.allSettled([getLiveFleet()]);

	return {
		fleet: fleet.status === 'fulfilled' ? fleet.value : null,
	} satisfies { fleet: LiveFleet | null };
};
