// Open a URL in the user's system browser — not in the embedded Tauri
// webview. `window.open(url, "_blank")` is a no-op in a packaged Tauri v2
// shell (tauri v2.10 doesn't hand blank-target navigations to the OS), so
// OAuth flows to openai.com / minimax / etc. were silently failing: the
// "Waiting for sign-in..." row would sit forever because the authorize page
// never actually loaded anywhere.
//
// In Tauri we reach for the registered `opener` plugin (see src-tauri
// capabilities/default.json :: opener:default). With `withGlobalTauri: true`
// in tauri.conf.json, the plugin is invocable via the injected
// `window.__TAURI__.core.invoke` wrapper without any npm package install.
//
// In a plain browser (dev / storybook / tests), we fall back to window.open,
// which behaves normally there.

type TauriInvoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

interface TauriGlobal {
	core?: { invoke?: TauriInvoke };
	opener?: {
		openUrl?: (url: string) => Promise<unknown>;
		open?: (url: string) => Promise<unknown>;
	};
	// Older v1-shape kept for defense; v2 everything lives under `core`.
	invoke?: TauriInvoke;
}

declare global {
	interface Window {
		__TAURI__?: TauriGlobal;
		__TAURI_INTERNALS__?: unknown;
	}
}

function tauriInvoke(): TauriInvoke | null {
	if (typeof window === 'undefined') return null;
	const g = window.__TAURI__;
	if (!g) return null;
	return g.core?.invoke ?? g.invoke ?? null;
}

export function isInTauri(): boolean {
	return tauriInvoke() !== null;
}

/**
 * Open `url` in the user's default browser. Returns true when the handoff
 * succeeded; returns false when neither Tauri nor window.open could take
 * the URL, so callers can show a "copy this link" fallback.
 */
export async function openExternal(url: string): Promise<boolean> {
	if (!url) return false;
	const g = typeof window === 'undefined' ? undefined : window.__TAURI__;
	if (g?.opener?.openUrl) {
		try {
			await g.opener.openUrl(url);
			return true;
		} catch (err) {
			console.warn('[openExternal] tauri opener.openUrl failed, falling back:', err);
		}
	}
	if (g?.opener?.open) {
		try {
			await g.opener.open(url);
			return true;
		} catch (err) {
			console.warn('[openExternal] tauri opener.open failed, falling back:', err);
		}
	}
	const invoke = tauriInvoke();
	if (invoke) {
		for (const command of ['plugin:opener|open_url', 'plugin:opener|openUrl']) {
			try {
				// `plugin:opener|open_url` is the v2 command name. The argument
				// key is `url` — NOT `path`. `path` belongs to the sibling
				// `open_path` command for filesystem paths.
				await invoke(command, { url });
				return true;
			} catch (err) {
				// Fall through to the next Tauri shape, then to window.open so dev
				// tools / browser preview still work when a capability is misconfigured.
				console.warn(`[openExternal] tauri ${command} failed, falling back:`, err);
			}
		}
	}
	try {
		// Do NOT pass 'noopener' in the features string: per spec window.open()
		// then returns null even when the tab opened fine, which misreported a
		// working browser handoff as "could not open". Open plainly (null now
		// really means blocked) and sever the opener link ourselves.
		const win = window.open(url, '_blank');
		if (!win) return false;
		try {
			win.opener = null;
		} catch {
			/* cross-origin — opener access denied, already isolated */
		}
		return true;
	} catch {
		return false;
	}
}
