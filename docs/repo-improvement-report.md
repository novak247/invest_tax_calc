# Repository Improvement Report

## Overview

`invest_tax_calc` is a local-first Czech securities tax helper centered on Trading 212 PDF import, FIFO tax analysis, planning, quote lookup, and Gmail attachment import. The core package is compact and already has focused unittest coverage for tax logic, parsing, planning, prices, opportunities, and email import behavior.

The audit found the strongest improvement opportunities around reducing duplicate analysis work, making the Czech 100,000 CZK gross-proceeds rule safer for real taxpayer-wide use, and smoothing the default test workflow. This report records the ranked backlog and the top three items selected for implementation in this run.

## Top Findings

1. Analyze and tax-opportunity flows duplicate work.
   - `/api/analyze` computes the main tax result, while `/api/tax-opportunities` reparses/reanalyzes the same uploaded transaction set through `tax_opportunities()`.
   - This creates extra request/parse latency and increases the chance that UI state drifts between the headline analysis and opportunity panel.

2. The 100,000 CZK proceeds exemption currently sees only imported transactions.
   - Czech annual gross sale proceeds are taxpayer-wide, but the current analysis is limited to statements loaded into this app.
   - A user with other brokers or manually executed sales can receive an overly optimistic exemption result unless those external proceeds are represented somehow.

3. Default unittest discovery is easy to miss.
   - Tests live under `tests/`, but the project currently relies on explicit discovery such as `uv run python -m unittest discover -s tests`.
   - Running the default `python -m unittest discover` from the repo root should find the suite without extra flags.

4. App orchestration is concentrated in a large `app.py`.
   - The HTTP server, request handlers, static serving, Gmail import helpers, parsing orchestration, and reload behavior share one large module.
   - This is workable for an MVP, but future endpoint growth will be easier if request parsing and domain orchestration move behind smaller functions.

5. External integrations need clear stale-data and failure surfaces.
   - Price lookup, parsed PDF cache reuse, and Gmail imports are thoughtfully isolated, but user-facing workflows should keep making cache age, provider failures, and reparse controls explicit.

## Ranked Improvement Backlog

| Rank | Improvement | Impact | Effort |
| ---: | --- | --- | --- |
| 1 | Include tax opportunities in `/api/analyze` output so the UI can avoid a second parse/request for the same statement set. | Faster analysis workflow, less duplicated server work, fewer state mismatch risks. | Medium |
| 2 | Add an "other annual securities proceeds" adjustment to analysis inputs and summaries for the taxpayer-wide 100,000 CZK rule. | More accurate exemption handling for users with sales outside imported Trading 212 statements. | Medium |
| 3 | Make default unittest discovery find tests from the repo root. | Lower contributor friction and safer routine verification. | Low |
| 4 | Split `app.py` endpoint orchestration into smaller request/service helpers. | Easier maintenance as API surface grows. | Medium |
| 5 | Add regression tests around analyze/opportunity payload consistency once opportunities are embedded. | Protects the highest-priority workflow change. | Low |
| 6 | Document cache invalidation semantics for parsed PDFs and price quotes in one operations section. | Reduces confusion when parser or quote behavior changes. | Low |
| 7 | Add a structured manual transaction/proceeds import path beyond Trading 212 PDFs. | Extends correctness for multi-broker taxpayers. | High |
| 8 | Expand UI tests or endpoint-level smoke tests for planner/analyze flows. | Catches integration regressions that unit tests may miss. | Medium |

## Top 3 Selected For Implementation

1. Include tax opportunities in analyze.
   - Target behavior: `/api/analyze` returns the current analysis plus the tax-opportunity summary derived from the same transaction parse and analysis context.
   - Acceptance checks: the existing tax-opportunities endpoint can remain compatible, but the main analyze response should be sufficient for the UI to render opportunities without issuing a second request for unchanged inputs.

2. Add other annual securities proceeds adjustment.
   - Target behavior: analysis accepts a CZK amount for other same-tax-year securities gross proceeds outside the imported statements.
   - Acceptance checks: the 100,000 CZK exemption decision uses imported annual proceeds plus the adjustment, while summaries clearly separate imported proceeds, external adjustment, and combined gross proceeds used for the rule.

3. Make default unittest discovery find tests.
   - Target behavior: from the repository root, `uv run python -m unittest discover` discovers and runs the suite without requiring `-s tests`.
   - Acceptance checks: the explicit `uv run python -m unittest discover -s tests` path should keep working, and the default command should report the same test set.
