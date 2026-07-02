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

<div class="min-h-full bg-black text-[#888]">
	<div class="border-b border-[#222] bg-black px-4 py-3">
		<div class="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
			<div>
				<h1 class="text-sm font-semibold uppercase tracking-[0.22em] text-white">Integrations</h1>
				<p class="mt-1 max-w-3xl text-[11px] leading-5 text-[#666]">
					One MCP control room: AI clients connect into Forven, while agent tool servers let
					Forven agents connect out to external tools.
				</p>
			</div>

			<nav class="flex flex-wrap gap-2" aria-label="Integration sections">
				{#each tabs as tab}
					<a
						href={tab.href}
						class="min-w-[180px] border-l-2 px-3 py-2 transition-colors {active === tab.id
							? 'border-l-white bg-[#111] text-white'
							: 'border-l-transparent bg-black text-[#888] hover:bg-[#0a0a0a] hover:text-white'}"
						aria-current={active === tab.id ? 'page' : undefined}
					>
						<span class="block text-[11px] font-semibold uppercase tracking-widest">{tab.label}</span>
						<span class="mt-1 block text-[10px] leading-4 text-[#666]">{tab.description}</span>
					</a>
				{/each}
			</nav>
		</div>
	</div>

	<slot />
</div>
