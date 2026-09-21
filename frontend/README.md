# Forven Frontend

This is the SvelteKit operator UI for Forven.

## Stack

- SvelteKit 2
- Svelte 5
- Tailwind CSS
- Vite
- Vitest
- Playwright

## Development

```bash
pnpm install --frozen-lockfile
pnpm run dev
```

Default local URL: `http://127.0.0.1:5173`

The frontend expects the backend at `http://127.0.0.1:8003` by default. During local development, Vite proxies `/api` and `/health` to the backend.

## Commands

```bash
pnpm run dev
pnpm run build
pnpm run preview
pnpm test
pnpm run check
pnpm run test:e2e
```

## Route Surface

- `/`
- `/agents`
- `/ai-dropzone`
- `/approval`
- `/data`
- `/lab`
- `/lab/strategy/[id]`
- `/memory`
- `/risk`
- `/runs`
- `/settings`
- `/tasks`
- `/trades`

The dashboard also supports `/?view=quant_factory` and `/?view=beta`.

## Conventions

- Use typed API wrappers from `src/lib/api/`
- Use shared stores from `src/lib/stores/`
- Keep reusable UI in `src/lib/components/`
- Prefer route-local components only when the UI is truly route-specific
- Do not add raw `fetch()` calls directly inside components when a typed API module belongs there

For the full repo onboarding flow, go back to the root [README.md](../README.md).

## Package manager

Use pnpm 10.33.0 (the version pinned in `package.json`). Enable it with
`corepack enable` and `corepack prepare pnpm@10.33.0 --activate`, then run
`pnpm install --frozen-lockfile` from this directory. Commit `pnpm-lock.yaml`
when dependencies change.

The workspace config uses a hoisted layout and hard links to the global pnpm
store, so identical package files can share disk space across local checkouts.
Keep the store and checkouts on the same filesystem. For an existing npm
checkout, stop its dev server, remove its old `node_modules`, and reinstall
with pnpm. Do not edit files inside `node_modules`.
