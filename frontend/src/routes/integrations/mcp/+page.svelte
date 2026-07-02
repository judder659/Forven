<script lang="ts">
	import { onMount } from 'svelte';
	import IntegrationTabs from '$lib/components/integrations/IntegrationTabs.svelte';
	import {
		listMCPServers,
		createMCPServer,
		deleteMCPServer,
		testMCPServer,
		type MCPServer,
		type MCPTransport,
	} from '$lib/api/mcp';

	let servers: MCPServer[] = [];
	let loading = true;
	let loadError = '';

	let showCreate = false;
	let createName = '';
	let createTransport: MCPTransport = 'stdio';
	let createCommand = '';
	let createArgs: string[] = [''];
	let createUrl = '';
	let createEnabled = true;
	let createEnv: { key: string; value: string }[] = [{ key: '', value: '' }];
	let createHeaders: { key: string; value: string }[] = [{ key: '', value: '' }];
	let createToolsInclude = '';
	let createToolsExclude = '';
	let createError = '';
	let creating = false;

	let busyName = '';
	let testResult: Record<string, { ok: boolean; msg: string }> = {};
	let actionError = '';
	let confirmDeleteName = '';

	async function load() {
		loading = true;
		loadError = '';
		try {
			const r = await listMCPServers();
			servers = r.servers || [];
		} catch (e) {
			loadError = e instanceof Error ? e.message : String(e);
			servers = [];
		} finally {
			loading = false;
		}
	}

	function resetCreate() {
		createName = '';
		createTransport = 'stdio';
		createCommand = '';
		createArgs = [''];
		createUrl = '';
		createEnabled = true;
		createEnv = [{ key: '', value: '' }];
		createHeaders = [{ key: '', value: '' }];
		createToolsInclude = '';
		createToolsExclude = '';
		createError = '';
	}

	function pairsToObject(rows: { key: string; value: string }[]): Record<string, string> {
		const out: Record<string, string> = {};
		for (const r of rows) {
			const k = r.key.trim();
			if (k) out[k] = r.value;
		}
		return out;
	}

	function parseToolList(s: string): string[] {
		return s
			.split(',')
			.map((t) => t.trim())
			.filter((t) => t.length > 0);
	}

	async function handleCreate() {
		createError = '';
		if (!createName.trim()) {
			createError = 'Name is required.';
			return;
		}
		if (createTransport === 'stdio' && !createCommand.trim()) {
			createError = 'Command is required for stdio transport.';
			return;
		}
		if (createTransport === 'http' && !createUrl.trim()) {
			createError = 'URL is required for http transport.';
			return;
		}
		creating = true;
		try {
			const argsArr = createArgs.map((a) => a.trim()).filter((a) => a.length > 0);
			const include = parseToolList(createToolsInclude);
			await createMCPServer({
				name: createName.trim(),
				transport: createTransport,
				command: createTransport === 'stdio' ? createCommand.trim() : null,
				args: argsArr,
				env: pairsToObject(createEnv),
				url: createTransport === 'http' ? createUrl.trim() : null,
				headers: createTransport === 'http' ? pairsToObject(createHeaders) : {},
				enabled: createEnabled,
				tools_include: include.length ? include : null,
				tools_exclude: parseToolList(createToolsExclude),
			});
			showCreate = false;
			resetCreate();
			await load();
		} catch (e) {
			createError = e instanceof Error ? e.message : String(e);
		} finally {
			creating = false;
		}
	}

	async function handleDelete() {
		const name = confirmDeleteName;
		if (!name) return;
		confirmDeleteName = '';
		actionError = '';
		busyName = name;
		try {
			await deleteMCPServer(name);
			await load();
		} catch (e) {
			actionError = `Delete '${name}' failed: ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			busyName = '';
		}
	}

	async function handleTest(name: string) {
		actionError = '';
		busyName = name;
		try {
			const r = await testMCPServer(name);
			if (r.ok) {
				testResult[name] = { ok: true, msg: `OK (proto ${r.protocol_version})` };
			} else {
				testResult[name] = { ok: false, msg: r.error || 'unknown error' };
			}
			testResult = { ...testResult };
			// Refresh persisted status / tool count so the table reflects reality.
			await load();
		} catch (e) {
			testResult[name] = { ok: false, msg: e instanceof Error ? e.message : String(e) };
			testResult = { ...testResult };
		} finally {
			busyName = '';
		}
	}

	function formatStatus(s: MCPServer): string {
		if (!s.last_status) return '—';
		const at = s.last_status_at ? new Date(s.last_status_at).toLocaleString() : '';
		return `${s.last_status}${at ? ' • ' + at : ''}`;
	}

	function statusClass(s: MCPServer): string {
		if (s.last_status === 'ok') return 'text-green-400';
		if (s.last_status === 'error') return 'text-red-400';
		return 'text-gray-400';
	}

	onMount(load);
</script>

<svelte:head>
	<title>Integrations · Forven</title>
</svelte:head>

<IntegrationTabs active="tool-servers">
<div class="p-4 text-gray-200">
	<div class="flex items-center justify-between mb-4">
		<div>
			<h1 class="text-xl font-semibold">Agent Tool Servers</h1>
			<p class="text-xs text-gray-500 mt-0.5 max-w-2xl">
				The <span class="text-gray-300">outbound</span> direction: register an external MCP server
				(a filesystem server, web search, a ticker API, …) and its tools become callable by
				<span class="text-gray-300">Forven's own internal agents</span> — scoped per agent via
				grants on each agent's page. Connecting an outside assistant <em>into</em> Forven is the
				other tab (AI Clients); most setups never need anything here.
			</p>
		</div>
		<button
			class="terminal-button-primary text-sm"
			on:click={() => {
				resetCreate();
				showCreate = !showCreate;
			}}
		>
			{showCreate ? 'Cancel' : '+ Add Server'}
		</button>
	</div>

	{#if showCreate}
		<div class="bg-[#0d0d0d] border border-[#222] rounded p-4 mb-4">
			<h2 class="text-sm font-semibold mb-3">New MCP Server</h2>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
				<label class="flex flex-col gap-1">
					<span class="text-gray-400">Name</span>
					<input
						class="bg-black border border-[#333] px-2 py-1 rounded font-mono"
						bind:value={createName}
						placeholder="my-fs-server"
					/>
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-gray-400">Transport</span>
					<select
						class="bg-black border border-[#333] px-2 py-1 rounded"
						bind:value={createTransport}
					>
						<option value="stdio">stdio (subprocess)</option>
						<option value="http">http</option>
					</select>
				</label>
				{#if createTransport === 'stdio'}
					<label class="flex flex-col gap-1 md:col-span-2">
						<span class="text-gray-400">Command</span>
						<input
							class="bg-black border border-[#333] px-2 py-1 rounded font-mono"
							bind:value={createCommand}
							placeholder="npx"
						/>
					</label>
					<div class="flex flex-col gap-1 md:col-span-2">
						<span class="text-gray-400">Args (one per row — preserves spaces/quotes)</span>
						{#each createArgs as _, i}
							<div class="flex gap-2">
								<input
									class="flex-1 bg-black border border-[#333] px-2 py-1 rounded font-mono"
									bind:value={createArgs[i]}
									placeholder={i === 0 ? '-y' : i === 1 ? '@modelcontextprotocol/server-filesystem' : '/tmp/my dir'}
								/>
								<button
									type="button"
									class="px-2 text-gray-500 hover:text-red-300"
									on:click={() => (createArgs = createArgs.filter((_, j) => j !== i))}
									title="Remove arg"
								>
									×
								</button>
							</div>
						{/each}
						<button
							type="button"
							class="self-start text-[11px] text-[#888] hover:text-white"
							on:click={() => (createArgs = [...createArgs, ''])}
						>
							+ Add arg
						</button>
					</div>
				{:else}
					<label class="flex flex-col gap-1 md:col-span-2">
						<span class="text-gray-400">URL</span>
						<input
							class="bg-black border border-[#333] px-2 py-1 rounded font-mono"
							bind:value={createUrl}
							placeholder="https://mcp.example.com/rpc"
						/>
					</label>
					<div class="flex flex-col gap-1 md:col-span-2">
						<span class="text-gray-400">Headers</span>
						{#each createHeaders as _, i}
							<div class="flex gap-2">
								<input
									class="flex-1 bg-black border border-[#333] px-2 py-1 rounded font-mono"
									bind:value={createHeaders[i].key}
									placeholder="Authorization"
								/>
								<input
									class="flex-1 bg-black border border-[#333] px-2 py-1 rounded font-mono"
									bind:value={createHeaders[i].value}
									placeholder="Bearer …"
								/>
								<button
									type="button"
									class="px-2 text-gray-500 hover:text-red-300"
									on:click={() => (createHeaders = createHeaders.filter((_, j) => j !== i))}
									title="Remove header"
								>
									×
								</button>
							</div>
						{/each}
						<button
							type="button"
							class="self-start text-[11px] text-[#888] hover:text-white"
							on:click={() => (createHeaders = [...createHeaders, { key: '', value: '' }])}
						>
							+ Add header
						</button>
					</div>
				{/if}
				<div class="flex flex-col gap-1 md:col-span-2">
					<span class="text-gray-400">Environment variables</span>
					{#each createEnv as _, i}
						<div class="flex gap-2">
							<input
								class="flex-1 bg-black border border-[#333] px-2 py-1 rounded font-mono"
								bind:value={createEnv[i].key}
								placeholder="API_KEY"
							/>
							<input
								class="flex-1 bg-black border border-[#333] px-2 py-1 rounded font-mono"
								bind:value={createEnv[i].value}
								placeholder="value"
							/>
							<button
								type="button"
								class="px-2 text-gray-500 hover:text-red-300"
								on:click={() => (createEnv = createEnv.filter((_, j) => j !== i))}
								title="Remove env var"
							>
								×
							</button>
						</div>
					{/each}
					<button
						type="button"
						class="self-start text-[11px] text-[#888] hover:text-white"
						on:click={() => (createEnv = [...createEnv, { key: '', value: '' }])}
					>
						+ Add env var
					</button>
				</div>
				<label class="flex flex-col gap-1">
					<span class="text-gray-400">Tools include (comma-separated, blank = all)</span>
					<input
						class="bg-black border border-[#333] px-2 py-1 rounded font-mono"
						bind:value={createToolsInclude}
						placeholder="read_file, list_dir"
					/>
				</label>
				<label class="flex flex-col gap-1">
					<span class="text-gray-400">Tools exclude (comma-separated)</span>
					<input
						class="bg-black border border-[#333] px-2 py-1 rounded font-mono"
						bind:value={createToolsExclude}
						placeholder="write_file"
					/>
				</label>
				<label class="flex items-center gap-2 md:col-span-2">
					<input type="checkbox" bind:checked={createEnabled} />
					<span class="text-gray-300">Enabled (register tools immediately)</span>
				</label>
			</div>
			{#if createError}
				<div class="mt-2 text-xs text-red-400">{createError}</div>
			{/if}
			<div class="mt-3 flex justify-end gap-2">
				<button
					class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200"
					on:click={() => (showCreate = false)}
					disabled={creating}
				>
					Cancel
				</button>
				<button
					class="terminal-button-primary text-xs disabled:opacity-50"
					on:click={handleCreate}
					disabled={creating}
				>
					{creating ? 'Creating…' : 'Create'}
				</button>
			</div>
		</div>
	{/if}

	{#if actionError}
		<div
			class="mb-4 flex items-start justify-between gap-2 bg-red-950 border border-red-800 text-red-300 text-xs rounded px-3 py-2"
		>
			<span>{actionError}</span>
			<button
				class="text-red-400 hover:text-red-200"
				on:click={() => (actionError = '')}
				aria-label="Dismiss"
			>
				×
			</button>
		</div>
	{/if}

	{#if loading}
		<div class="text-xs text-gray-500">Loading…</div>
	{:else if loadError}
		<div class="text-xs text-red-400">{loadError}</div>
	{:else if servers.length === 0}
		<div class="bg-[#0d0d0d] border border-[#222] rounded p-6 text-center">
			<p class="text-sm text-gray-400">No agent tool servers configured — that's the normal state.</p>
			<p class="text-xs text-gray-500 mt-1">
				Add one only when a Forven agent needs an external tool (e.g. web search during research).
				To connect Claude/Cursor/Codex into Forven, use the AI Clients tab instead.
			</p>
		</div>
	{:else}
		<div class="bg-[#0d0d0d] border border-[#222] rounded overflow-hidden">
			<table class="w-full text-xs">
				<thead class="bg-black border-b border-[#222]">
					<tr class="text-left text-gray-500">
						<th class="px-3 py-2 font-medium">Name</th>
						<th class="px-3 py-2 font-medium">Transport</th>
						<th class="px-3 py-2 font-medium">Enabled</th>
						<th class="px-3 py-2 font-medium">Tools</th>
						<th class="px-3 py-2 font-medium">Last Status</th>
						<th class="px-3 py-2 font-medium text-right">Actions</th>
					</tr>
				</thead>
				<tbody>
					{#each servers as s (s.name)}
						<tr class="border-t border-[#1a1a1a] hover:bg-[#111]">
							<td class="px-3 py-2 font-mono">
								<a
									href={`/integrations/mcp/${encodeURIComponent(s.name)}`}
									class="text-white hover:underline"
								>
									{s.name}
								</a>
							</td>
							<td class="px-3 py-2 text-gray-400">{s.transport}</td>
							<td class="px-3 py-2">
								{#if s.enabled}
									<span class="text-green-400">on</span>
								{:else}
									<span class="text-gray-500">off</span>
								{/if}
							</td>
							<td class="px-3 py-2 text-gray-400">{s.registered_tool_count ?? 0}</td>
							<td class="px-3 py-2 {statusClass(s)}">
								{formatStatus(s)}
								{#if testResult[s.name]}
									<div class="mt-0.5 text-[10px] {testResult[s.name].ok ? 'text-green-300' : 'text-red-300'}">
										test: {testResult[s.name].msg}
									</div>
								{/if}
							</td>
							<td class="px-3 py-2 text-right">
								<button
									class="px-2 py-0.5 text-[11px] text-[#888] hover:text-white disabled:opacity-50"
									on:click={() => handleTest(s.name)}
									disabled={busyName === s.name}
								>
									{busyName === s.name ? '…' : 'Test'}
								</button>
								<button
									class="px-2 py-0.5 text-[11px] text-red-400 hover:text-red-300 disabled:opacity-50"
									on:click={() => (confirmDeleteName = s.name)}
									disabled={busyName === s.name}
								>
									Delete
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	{#if confirmDeleteName}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
			<div
				class="bg-[#0d0d0d] border border-[#222] rounded p-4 max-w-sm w-full mx-4"
				role="dialog"
				aria-modal="true"
			>
				<h2 class="text-sm font-semibold mb-2">Delete MCP server</h2>
				<p class="text-xs text-gray-400">
					Delete <span class="font-mono text-gray-200">{confirmDeleteName}</span>? This
					revokes all agent grants for it and cannot be undone.
				</p>
				<div class="mt-4 flex justify-end gap-2">
					<button
						class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200"
						on:click={() => (confirmDeleteName = '')}
					>
						Cancel
					</button>
					<button
						class="px-3 py-1.5 bg-red-800 hover:bg-red-700 text-white text-xs rounded"
						on:click={handleDelete}
					>
						Delete
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>
</IntegrationTabs>
