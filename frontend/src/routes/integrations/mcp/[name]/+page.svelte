<script lang="ts">
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import IntegrationTabs from '$lib/components/integrations/IntegrationTabs.svelte';
	import {
		getMCPServer,
		updateMCPServer,
		deleteMCPServer,
		listMCPServerTools,
		testMCPServer,
		type MCPServer,
		type MCPToolDef,
		type MCPServerUpdatePayload,
		type MCPTransport,
	} from '$lib/api/mcp';

	$: name = decodeURIComponent($page.params.name || '');

	let server: MCPServer | null = null;
	let tools: MCPToolDef[] = [];
	let toolsError = '';
	let toolsBusy = false;
	let loading = true;
	let error = '';

	let toggleBusy = false;
	let toggleMsg = '';
	let testBusy = false;
	let testMsg = '';
	let testServerInfo: Record<string, unknown> | null = null;

	let deleteBusy = false;

	// Edit form (mirrors the create form on the list page)
	let showEdit = false;
	let editTransport: MCPTransport = 'stdio';
	let editCommand = '';
	let editArgs = '';
	let editUrl = '';
	let editEnv = '';
	let editToolsInclude = '';
	let editToolsExclude = '';
	let editError = '';
	let editBusy = false;
	let expandedSchema: Record<string, boolean> = {};

	async function loadTools() {
		toolsBusy = true;
		toolsError = '';
		try {
			const r = await listMCPServerTools(name);
			tools = r.tools || [];
		} catch (e) {
			tools = [];
			toolsError = e instanceof Error ? e.message : String(e);
		} finally {
			toolsBusy = false;
		}
	}

	async function load() {
		loading = true;
		error = '';
		try {
			server = await getMCPServer(name);
			await loadTools();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function openEdit() {
		if (!server) return;
		editTransport = server.transport;
		editCommand = server.command || '';
		editArgs = server.args.join(' ');
		editUrl = server.url || '';
		editEnv = Object.entries(server.env)
			.map(([k, v]) => `${k}=${v}`)
			.join('\n');
		editToolsInclude = server.tools_include?.join(' ') || '';
		editToolsExclude = server.tools_exclude.join(' ');
		editError = '';
		showEdit = true;
	}

	function parseEnv(raw: string): Record<string, string> {
		const env: Record<string, string> = {};
		for (const line of raw.split('\n')) {
			const trimmed = line.trim();
			if (!trimmed) continue;
			const eq = trimmed.indexOf('=');
			if (eq <= 0) continue;
			env[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
		}
		return env;
	}

	function parseList(raw: string): string[] {
		return raw
			.trim()
			.split(/\s+/)
			.filter((s) => s.length > 0);
	}

	async function handleEdit() {
		if (!server) return;
		editError = '';
		if (editTransport === 'stdio' && !editCommand.trim()) {
			editError = 'Command is required for stdio transport.';
			return;
		}
		if (editTransport === 'http' && !editUrl.trim()) {
			editError = 'URL is required for http transport.';
			return;
		}
		editBusy = true;
		try {
			const includeArr = parseList(editToolsInclude);
			const payload: MCPServerUpdatePayload = {
				transport: editTransport,
				command: editTransport === 'stdio' ? editCommand.trim() : null,
				args: parseList(editArgs),
				url: editTransport === 'http' ? editUrl.trim() : null,
				env: parseEnv(editEnv),
				tools_include: includeArr.length ? includeArr : null,
				tools_exclude: parseList(editToolsExclude),
			};
			await updateMCPServer(server.name, payload);
			showEdit = false;
			await load();
		} catch (e) {
			editError = e instanceof Error ? e.message : String(e);
		} finally {
			editBusy = false;
		}
	}

	async function handleToggleEnabled() {
		if (!server) return;
		toggleBusy = true;
		toggleMsg = '';
		try {
			await updateMCPServer(server.name, { enabled: !server.enabled });
			await load();
		} catch (e) {
			toggleMsg = `FAIL — ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			toggleBusy = false;
		}
	}

	async function handleDelete() {
		if (!server) return;
		if (!confirm(`Delete MCP server '${server.name}'? This will revoke all grants.`)) return;
		deleteBusy = true;
		try {
			await deleteMCPServer(server.name);
			await goto('/integrations/mcp');
		} catch (e) {
			toggleMsg = `FAIL — ${e instanceof Error ? e.message : String(e)}`;
			deleteBusy = false;
		}
	}

	async function handleTest() {
		if (!server) return;
		testBusy = true;
		testMsg = '';
		testServerInfo = null;
		try {
			const r = await testMCPServer(server.name);
			testServerInfo = r.server_info ?? null;
			testMsg = r.ok
				? `OK — protocol ${r.protocol_version}`
				: `FAIL — ${r.error || 'unknown'}`;
		} catch (e) {
			testMsg = `FAIL — ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			testBusy = false;
		}
	}

	function formatStatusAt(at: string | null): string {
		return at ? new Date(at).toLocaleString() : '';
	}

	$: if (name) load();
</script>

<svelte:head>
	<title>{name} · MCP · Forven</title>
</svelte:head>

<IntegrationTabs active="tool-servers">
<div class="p-4 text-[#888]">
	<a href="/integrations/mcp" class="text-[11px] uppercase tracking-wider text-[#555] hover:text-white">← All MCP servers</a>

	{#if loading}
		<div class="mt-4 text-xs text-[#666]">Loading…</div>
	{:else if error || !server}
		<div class="mt-4 text-xs text-red-400">{error || 'Server not found'}</div>
	{:else}
		<div class="mt-3 flex items-center justify-between">
			<div>
				<h1 class="text-xl font-bold font-mono text-white">{server.name}</h1>
				<p class="text-xs text-[#666] mt-0.5">
					{server.transport} • {server.enabled ? 'enabled' : 'disabled'}
				</p>
			</div>
			<div class="flex gap-2">
				<button
					class="px-3 py-1.5 border border-[#333] text-[#888] hover:border-[#555] hover:text-white text-xs transition-colors disabled:opacity-50"
					on:click={handleTest}
					disabled={testBusy}
				>
					{testBusy ? 'Testing…' : 'Test connection'}
				</button>
				<button
					class="px-3 py-1.5 border border-[#333] text-[#888] hover:border-[#555] hover:text-white text-xs transition-colors"
					on:click={() => (showEdit ? (showEdit = false) : openEdit())}
				>
					{showEdit ? 'Cancel edit' : 'Edit'}
				</button>
				<button
					class="px-3 py-1.5 text-xs transition-colors disabled:opacity-50 {server.enabled
						? 'border border-yellow-800 text-yellow-400 hover:bg-yellow-500/10'
						: 'border border-[#333] text-[#888] hover:border-[#555] hover:text-white'}"
					on:click={handleToggleEnabled}
					disabled={toggleBusy}
				>
					{server.enabled ? 'Disable' : 'Enable'}
				</button>
				<button
					class="px-3 py-1.5 border border-red-900 text-red-500 hover:bg-red-500 hover:text-white text-xs transition-colors disabled:opacity-50"
					on:click={handleDelete}
					disabled={deleteBusy}
				>
					{deleteBusy ? 'Deleting…' : 'Delete'}
				</button>
			</div>
		</div>

		{#if toggleMsg}
			<div
				class="mt-3 px-3 py-2 text-xs border border-red-900 bg-red-500/5 text-red-400"
			>
				{toggleMsg}
			</div>
		{/if}

		{#if testMsg}
			<div
				class="mt-3 px-3 py-2 text-xs border"
				class:border-emerald-900={testMsg.startsWith('OK')}
				class:bg-emerald-950={testMsg.startsWith('OK')}
				class:text-emerald-400={testMsg.startsWith('OK')}
				class:border-red-900={!testMsg.startsWith('OK')}
				class:bg-red-950={!testMsg.startsWith('OK')}
				class:text-red-400={!testMsg.startsWith('OK')}
			>
				{testMsg}
				{#if testServerInfo && Object.keys(testServerInfo).length}
					<pre class="mt-1 text-[10px] whitespace-pre-wrap break-all opacity-80">{JSON.stringify(
							testServerInfo,
							null,
							2,
						)}</pre>
				{/if}
			</div>
		{/if}

		{#if showEdit}
			<div class="mt-4 border border-[#222] bg-[#050505] p-4">
				<h2 class="text-sm font-semibold mb-3">Edit configuration</h2>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
					<label class="flex flex-col gap-1">
						<span class="text-[#888]">Transport</span>
						<select
							class="bg-black border border-[#333] px-2 py-1"
							bind:value={editTransport}
						>
							<option value="stdio">stdio (subprocess)</option>
							<option value="http">http</option>
						</select>
					</label>
					<div></div>
					{#if editTransport === 'stdio'}
						<label class="flex flex-col gap-1 md:col-span-2">
							<span class="text-[#888]">Command</span>
							<input
								class="bg-black border border-[#333] px-2 py-1 font-mono"
								bind:value={editCommand}
								placeholder="npx"
							/>
						</label>
						<label class="flex flex-col gap-1 md:col-span-2">
							<span class="text-[#888]">Args (space-separated)</span>
							<input
								class="bg-black border border-[#333] px-2 py-1 font-mono"
								bind:value={editArgs}
								placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
							/>
						</label>
					{:else}
						<label class="flex flex-col gap-1 md:col-span-2">
							<span class="text-[#888]">URL</span>
							<input
								class="bg-black border border-[#333] px-2 py-1 font-mono"
								bind:value={editUrl}
								placeholder="https://mcp.example.com/rpc"
							/>
						</label>
					{/if}
					<label class="flex flex-col gap-1 md:col-span-2">
						<span class="text-[#888]">Env (KEY=value, one per line)</span>
						<textarea
							class="bg-black border border-[#333] px-2 py-1 font-mono"
							rows="3"
							bind:value={editEnv}
							placeholder="API_KEY=xxxx"
						></textarea>
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-[#888]">Tools include (space-separated, blank = all)</span>
						<input
							class="bg-black border border-[#333] px-2 py-1 font-mono"
							bind:value={editToolsInclude}
							placeholder="read_file write_file"
						/>
					</label>
					<label class="flex flex-col gap-1">
						<span class="text-[#888]">Tools exclude (space-separated)</span>
						<input
							class="bg-black border border-[#333] px-2 py-1 font-mono"
							bind:value={editToolsExclude}
							placeholder="delete_file"
						/>
					</label>
				</div>
				{#if editError}
					<div class="mt-2 text-xs text-red-400">{editError}</div>
				{/if}
				<div class="mt-3 flex justify-end gap-2">
					<button
						class="px-3 py-1.5 text-xs text-[#888] hover:text-white"
						on:click={() => (showEdit = false)}
						disabled={editBusy}
					>
						Cancel
					</button>
					<button
						class="terminal-button-primary text-xs disabled:opacity-50"
						on:click={handleEdit}
						disabled={editBusy}
					>
						{editBusy ? 'Saving…' : 'Save changes'}
					</button>
				</div>
			</div>
		{/if}

		<div class="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
			<div class="border border-[#222] bg-[#050505] p-3">
				<h2 class="text-sm font-semibold mb-2">Configuration</h2>
				<dl class="text-xs space-y-1.5">
					<div class="flex">
						<dt class="text-[#666] w-32">Transport</dt>
						<dd class="font-mono">{server.transport}</dd>
					</div>
					{#if server.transport === 'stdio'}
						<div class="flex">
							<dt class="text-[#666] w-32">Command</dt>
							<dd class="font-mono break-all">{server.command || '—'}</dd>
						</div>
						<div class="flex">
							<dt class="text-[#666] w-32">Args</dt>
							<dd class="font-mono break-all">
								{server.args.length ? server.args.join(' ') : '—'}
							</dd>
						</div>
					{:else}
						<div class="flex">
							<dt class="text-[#666] w-32">URL</dt>
							<dd class="font-mono break-all">{server.url || '—'}</dd>
						</div>
					{/if}
					<div class="flex">
						<dt class="text-[#666] w-32">Env keys</dt>
						<dd class="font-mono">
							{Object.keys(server.env).length
								? Object.keys(server.env).join(', ')
								: '—'}
						</dd>
					</div>
					<div class="flex">
						<dt class="text-[#666] w-32">Tools include</dt>
						<dd class="font-mono">
							{server.tools_include?.length ? server.tools_include.join(', ') : 'all'}
						</dd>
					</div>
					<div class="flex">
						<dt class="text-[#666] w-32">Tools exclude</dt>
						<dd class="font-mono">
							{server.tools_exclude.length ? server.tools_exclude.join(', ') : '—'}
						</dd>
					</div>
					<div class="flex">
						<dt class="text-[#666] w-32">Last status</dt>
						<dd>{server.last_status || '—'}</dd>
					</div>
					<div class="flex">
						<dt class="text-[#666] w-32">Last status at</dt>
						<dd>{formatStatusAt(server.last_status_at) || '—'}</dd>
					</div>
					{#if server.last_error}
						<div class="flex">
							<dt class="text-[#666] w-32">Last error</dt>
							<dd class="text-red-400 break-all">{server.last_error}</dd>
						</div>
					{/if}
				</dl>
			</div>

			<div class="border border-[#222] bg-[#050505] p-3">
				<div class="flex items-center justify-between mb-2">
					<h2 class="text-sm font-semibold">
						Discovered tools ({tools.length})
					</h2>
					<button
						class="px-2 py-0.5 text-[11px] text-[#888] hover:text-white disabled:opacity-50"
						on:click={loadTools}
						disabled={toolsBusy}
					>
						{toolsBusy ? 'Refreshing…' : 'Refresh tools'}
					</button>
				</div>
				{#if toolsError}
					<p class="text-xs text-red-400">
						Could not reach server: {toolsError}
					</p>
				{:else if tools.length === 0}
					<p class="text-xs text-[#666]">Server exposes no tools.</p>
				{:else}
					<ul class="text-xs space-y-2">
						{#each tools as t (t.name)}
							<li class="border-l border-[#333] pl-2">
								<div class="flex items-center gap-2">
									<span class="font-mono text-white">{t.name}</span>
									{#if t.inputSchema && Object.keys(t.inputSchema).length}
										<button
											class="text-[10px] text-[#888] hover:text-white"
											on:click={() =>
												(expandedSchema = {
													...expandedSchema,
													[t.name]: !expandedSchema[t.name],
												})}
										>
											{expandedSchema[t.name] ? 'hide schema' : 'schema'}
										</button>
									{/if}
								</div>
								{#if t.description}
									<div class="text-[#666] mt-0.5">{t.description}</div>
								{/if}
								{#if expandedSchema[t.name] && t.inputSchema}
									<pre
										class="mt-1 text-[10px] text-[#888] whitespace-pre-wrap break-all bg-black border border-[#222] p-1.5">{JSON.stringify(
											t.inputSchema,
											null,
											2,
										)}</pre>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	{/if}
</div>
</IntegrationTabs>
