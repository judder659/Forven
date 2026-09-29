<script lang="ts">
	export let active: 'ai-clients' | 'tool-servers' = 'ai-clients';

	const tabs = [
		{
			id: 'ai-clients',
			href: '/integrations',
			label: 'AI Clients',
			description: 'Connect outside assistants to Forven and watch Drop Zone sessions',
		},
		{
			id: 'tool-servers',
			href: '/integrations/mcp',
			label: 'Agent Tool Servers',
			description: 'Connect external MCP tools for Forven agents to call',
		},
	] as const;
</script>

<div class="min-h-full bg-sc-bg text-sc-ink2">
	<div class="border-b border-sc-line bg-sc-bg px-4 py-3">
		<div class="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
			<div>
				<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Integrations</h1>
				<p class="mt-1 max-w-3xl text-[11px] leading-5 text-sc-ink3">
					One MCP control room: AI clients connect into Forven, while agent tool servers let
					Forven agents connect out to external tools.
				</p>
			</div>

			<nav class="flex flex-wrap gap-2" aria-label="Integration sections">
				{#each tabs as tab}
					<a
						href={tab.href}
						class="min-w-[180px] border-l-2 px-3 py-2 transition-colors {active === tab.id
							? 'border-l-sc-ink bg-sc-panel2 text-sc-ink'
							: 'border-l-transparent bg-sc-bg text-sc-ink2 hover:bg-sc-panel hover:text-sc-ink'}"
						aria-current={active === tab.id ? 'page' : undefined}
					>
						<span class="block font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em]">{tab.label}</span>
						<span class="mt-1 block text-[10px] leading-4 text-sc-ink3">{tab.description}</span>
					</a>
				{/each}
			</nav>
		</div>
	</div>

	<slot />
</div>
