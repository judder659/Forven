<script lang="ts">
	// Lets a test change ReadinessChip's query on a live instance.
	import ReadinessChip from '$lib/components/data-readiness/ReadinessChip.svelte';
	import type { ReadinessQuery } from '$lib/api/dataReadiness';

	export let query: ReadinessQuery | null = null;
	export function setQuery(next: ReadinessQuery | null) {
		query = next;
	}
</script>

<ReadinessChip {query} />
