<script lang="ts">
	export let error: App.Error;
	export let status: number;

	$: message = error?.message || 'An unexpected error occurred.';
</script>

<svelte:head>
	<title>{status} | Forven</title>
	<meta name="description" content="Forven encountered an unexpected route error." />
</svelte:head>

<div class="h-full min-h-0 w-full flex items-center justify-center bg-black text-white p-6">
	<div class="w-full max-w-2xl border border-[#222] bg-[#050505] p-6">
		<div class="text-[10px] uppercase tracking-[0.2em] text-[#666]">Route Failure</div>
		<h1 class="mt-2 text-2xl font-bold">{status}</h1>
		<p class="mt-2 text-sm text-[#888]">{message}</p>
		<a
			href="/"
			class="inline-flex mt-5 px-3 py-1.5 border border-[#333] text-xs uppercase tracking-wider text-[#888] hover:text-white hover:border-white transition-colors"
		>
			Return To Dashboard
		</a>
	</div>
</div>
