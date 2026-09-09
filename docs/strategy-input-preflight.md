# Strategy input preflight

Strategy Creator's AI mode offers **Check data**, a read-only operation with no
model call. Generation repeats the same server-side check, so checking first is
optional. The panel lists detected inputs, problems and the Data-page link. It
discards reports for edited inputs and never applies a delayed generated draft
over subsequent edits.

Candidate development and direct generation run the same check before their
first model call. They use explicit task scope or stored hypothesis targets,
resolving canonical market names and checking up to 16 market/timeframe pairs.
Unspecified scopes or descriptive timeframes must be resolved first. Missing
local candles or detected enrichment inputs block generation. Named external
inputs without a catalog mapping (CDD, HODL waves, ETF flows, VIX) also block;
they need a verified integration and corresponding catalog/check support, or an
explicit revision of the hypothesis. Merely downloading a file cannot resolve
an unsupported integration.

Data-blocked tasks retain a preflight checkpoint. After fixing collection,
**Resume** re-runs the check before allowing generation. There is no automatic
retry loop and no bypass for interrupted tools. Previously blocked tasks without
a safe checkpoint are not retroactively marked resumable.

Detection uses a limited set of named-input aliases, not full language
understanding. A successful check means local files were found; it does not
certify sufficient history, non-null enrichment, causal alignment or profitability.
Existing backtest and promotion gates remain necessary. Source requirements are
not inferred completely from arbitrary prose; generated code still needs review.

Backend and standalone agent processes must restart to load these Python changes.
The frontend reloads through Vite. No trading-service restart is part of this edit.
