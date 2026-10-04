"""Universe strategies: one pre-registered rule held across many coins at once.

A single-coin strategy makes too few trades in six months to tell skill from luck;
a rule held across a universe produces far more independent evidence per week.

- ``panel``: daily close + funding panels (research reads SEALED at the research
  holdout cutoff; the forward paper book reads unsealed, like every paper path).
- ``strategies``: the pre-registered books (docs/universe-trend-blend-spec.md).
- ``engine``: daily book simulation and the evidence statistics.
- ``research``: the sealed research report per book, cached by cutoff.
- ``book``: the forward paper book, started at deployment and never backfilled.

Paper only: nothing here places orders.
"""
