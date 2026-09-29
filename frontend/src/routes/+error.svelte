<script lang="ts">
	export let error: App.Error;
	export let status: number;

	$: message = error?.message || 'An unexpected error occurred.';
</script>

<svelte:head>
	<title>{status} | Forven</title>
	<meta name="description" content="Forven encountered an unexpected route error." />
</svelte:head>

<div class="h-full min-h-0 w-full flex items-center justify-center bg-sc-bg text-sc-ink p-6">
	<div class="rounded-md w-full max-w-2xl border border-sc-line bg-sc-panel p-6">
		<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Route Failure</div>
		<h1 class="mt-2 text-2xl font-bold">{status}</h1>
		<p class="mt-2 text-sm text-sc-ink2">{message}</p>
		<a
			href="/"
			class="inline-flex mt-5 px-3 py-1.5 border border-sc-line2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2 hover:text-sc-ink hover:border-sc-ink transition-colors"
		>
			Return To Dashboard
		</a>
	</div>
</div>
