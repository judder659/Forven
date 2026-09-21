<!--
Heads up: Forven is NOT yet accepting external code contributions — a contributor
agreement (CLA) isn't in place, so outside pull requests can't be merged for now.
See CONTRIBUTING.md. Issues, ideas, and discussion are very welcome.

If you're the maintainer (or PRs have since opened), fill in the below.
-->

## Summary

What does this change and why?

## Related issues

Closes #

## Testing

- [ ] `python -m pytest tests -q` (or the relevant subset) passes
- [ ] `python -m ruff check forven tests` is clean
- [ ] `cd frontend && pnpm run check` passes (if frontend touched)

## Checklist

- [ ] No secrets, credentials, or personal info added
- [ ] Defaults remain safe (paper/testnet; no new outbound calls to any server)
- [ ] Docs / `.env.example` updated if behavior or config changed
