# Claude Code as an AI provider

Forven can run its agents, bots and other AI calls through the Claude Code
program installed on the same computer. Usage then counts against whatever
Claude Code is signed in with: a Claude Pro/Max/Team plan, or an Anthropic API
key.

## Set it up

1. Install Claude Code: <https://code.claude.com/docs/en/setup>.
2. Sign in once from a terminal, as the same Windows/macOS/Linux user that runs
   Forven:

   ```
   claude auth login
   ```

3. In Forven, open **Agents → Setup → Providers**, find **Claude Code** under
   "Add a provider" and press **Connect**. Forven checks that `claude` is
   installed and signed in; nothing is typed or pasted.
4. Pick Claude Code models (`sonnet`, `opus`, `haiku`, `fable`) for agents and
   bots like any other provider's models.

If `claude` isn't on the `PATH` Forven sees, set `FORVEN_CLAUDE_CODE_PATH` to
the full path of the executable.

## How it works

- **One-off calls** (bots, Brain chat, summaries): Forven runs
  `claude -p --output-format json` with Claude Code's own tools switched off
  (`--tools ""`) and reads the answer.
- **Agent tasks**: Forven starts a small MCP server on a random
  `127.0.0.1` port, protected by a random per-run token, that lists the task's
  Forven tools. It runs `claude -p --output-format stream-json` with that server
  as its only tool source and Claude Code's shell, file and web tools off. Each
  tool call Claude makes runs through Forven's normal tool path (ownership
  checks, transcript, checkpoints). The task's transcript shows each step as it
  does for other providers.
- Runs use a throwaway working folder and `--no-session-persistence`, so they
  don't appear in your own Claude Code session history.
- Forven records tokens but prices Claude Code usage at $0, because it comes out
  of your own plan.

## Login and Anthropic's terms

Forven never shows a Claude sign-in page, never reads Claude Code's credential
files, and never stores a Claude token: Claude Code keeps its own login.
Anthropic's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
bars apps from offering Claude.ai login or routing requests through a user's
plan credentials, and allows a user to sign in to the unmodified Claude Code
program with their own plan, including when another product runs it. Each
Forven install uses only its own operator's login.

Plan limits are meant for ordinary individual use and are shared with your own
Claude Code and claude.ai use. When Claude Code reports a usage limit, Forven
treats it like any provider's rate limit (backoff, then the slot's fallback, if
one is configured).

## Limits

- Assistant chat and Deep Dive get plain text answers from Claude Code, without
  tool use. Agent tasks and bots get the full behavior above.
- An agent task that runs out of turns starts again from its original prompt
  when resumed, since the conversation lives inside Claude Code.
