# Security Policy

Forven is self-hosted software that holds **your** exchange (testnet) and LLM
credentials, runs a local API, and executes AI-generated strategy code. Please
help keep it safe by reporting vulnerabilities responsibly.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

- **Preferred:** use GitHub's private vulnerability reporting —
  **Security → Report a vulnerability** on this repository.
- **Alternatively:** email **judder@forven.app**.

Please include: a description of the issue, the affected version/commit, steps to
reproduce (a proof-of-concept if possible), and the impact you foresee. We aim to
acknowledge reports within a few days; this is a small project, so please allow
reasonable time for a fix before any public disclosure (coordinated disclosure).

## Supported versions

Security fixes target the latest release and the `main` branch. Older versions
are not maintained — please update before reporting.

## Scope & hardening notes

The most security-relevant configuration choices are documented in
[`.env.example`](.env.example). When self-hosting:

- **Keep the API on localhost.** It is unauthenticated by default. If you set
  `FORVEN_BIND_HOST` to a non-loopback address, the app requires `FORVEN_API_KEY`
  (and ideally `FORVEN_OPERATOR_KEY`) or it refuses to start. Never expose it to
  the internet without auth and a TLS-terminating proxy.
- **The agent shell tool is disabled by default** (`FORVEN_ENABLE_SHELL_TOOL=0`).
  Enabling it lets LLM-driven, web-influenced content run shell commands — only
  enable it if you understand the prompt-injection risk.
- **Live/mainnet trading is unsupported and off by default.** Reaching a
  real-money order requires multiple deliberate opt-ins; see
  [`DISCLAIMER.md`](DISCLAIMER.md).
- **AI-generated and custom strategy code runs in-process**, with the same
  privileges as the app — including access to your local database and any API
  credentials in the environment. Before import it is statically screened by an
  AST guard that blocks the obvious dangerous constructs (OS/network/subprocess
  imports, `eval`/`exec`/`compile`, file reads, and aliased builtins), but a
  static denylist is **not a sandbox** and not a complete trust boundary. Only
  register strategy code you trust, and keep the API on localhost.

Thank you for helping keep Forven and its users safe.
