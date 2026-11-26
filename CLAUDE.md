# CLAUDE.md

LitAI (package `lpc`, "Law Practice Console"): a local-first **litigation** desktop app for a
solo employment-law practice (both employer and employee side). Windows target.
**Out of scope:** billing, invoicing, time entries, trust accounting, payments. Do not add them.

## Commands

```powershell
.\scripts\setup.ps1                              # venv + pip install -e ".[desktop,dev]"
.\.venv\Scripts\python.exe -m pytest -q          # run before every commit; must stay green
.\scripts\dev.ps1 -Browser                       # server only: http://127.0.0.1:8765
.\scripts\dev.ps1                                # desktop window (pywebview / WebView2)
.\.venv\Scripts\python.exe -m lpc.rules.checklist  # regenerate docs/RULES_CHECKLIST.md after any ruleset edit
.\scripts\build.ps1                              # PyInstaller -> dist\LawPracticeConsole\
```
On Linux/macOS: `python -m pytest -q`, `python -m lpc --server-only --port 8765`.

## Map

| Path | What it is |
| --- | --- |
| `lpc/app.py` | FastAPI routes; pages + HTMX partials (`templates/partials/`) |
| `lpc/matters.py` | matters, party registry, conflicts check, `log_change()` |
| `lpc/ingest.py` | copy into matter folder, SHA-256 dedupe, PyMuPDF page text, FTS5 search |
| `lpc/ai/gateway.py` | the ONLY path to a model (consent check, ZDR model block, audit log) |
| `lpc/backup.py` | daily `VACUUM INTO` backup, keeps 14; started from `__main__.py` |
| `lpc/config.py` | data / matters / backup folders (OneDrive default; `LPC_*_DIR` overrides) |
| `lpc/migrations/NNN_*.sql` | schema; applied in order by `lpc/db.py` |
| `lpc/rules/engine.py` | deadline math; `compute(entry_id, trigger, service_method, profile)` |
| `lpc/rules/holidays.py` | federal + Illinois calendars; `data/closures.yaml` for one-offs |
| `lpc/rules/data/rulesets/*.yaml` | one file per forum; `base:` inheritance (ndil -> frcp, federal_statutes) |
| `lpc/rules/sources.json` | official source + version note per ruleset |
| `docs/SCHEMA.md` | workbook tab -> table map |

## Hard rules

1. **Deadlines are data, never invented.** Every ruleset entry needs a real `cite`. Never set
   `verified: true`; only the user does that after checking the source text. New entries start
   `verified: false`. Paraphrase rule text in titles/notes; do not paste AAA/JAMS rule text.
2. **Nothing auto-confirms.** Computed deadlines are stored `status='proposed'` with the
   arithmetic in `computation`; only a user action moves them to `confirmed`.
3. **All model calls go through `lpc/ai/gateway.complete()`.** Plain Messages API, inline
   content only. No Files API, batch, or prompt caching (they break zero data retention).
   Respect `matters.ai_permitted`. Keep `ai_calls` logging on every path (ok/blocked/error).
4. **Schema changes = new migration file** (`002_...sql`, `003_...`). Never edit an applied
   migration. Closed vocabularies are `CHECK` constraints; `matters.side` stays required.
5. **Local only.** Server binds `127.0.0.1` and refuses other Host headers and cross-origin POSTs
   (`local_only_problem()` in `app.py`; tests use `base_url="http://127.0.0.1"`). No CDNs or remote assets; vendor JS into
   `lpc/static/vendor/`. No telemetry. No client data, real names, or `.sqlite3` files in the repo.
6. **Escape untrusted text.** Document text is rendered escaped; search hits use the `\x02/\x03`
   markers + `highlight` filter, never `|safe` on raw text. FTS input goes through `_fts_query()`.
7. **Tests stay offline.** Use the `settings`/`conn` fixtures (temp dirs via `LPC_DATA_DIR`,
   `LPC_MATTERS_DIR`) and `httpx.MockTransport` for any HTTP. Every feature or bug fix gets a test.

## Conventions

- Python 3.11+, `from __future__ import annotations`, type hints, stdlib `sqlite3` (no ORM).
- UI: server-rendered Jinja + HTMX partial swaps + Alpine for small state. No React, no JS build step.
- Write user-visible changes to `changelog` via `log_change()`.
- User-facing errors: raise `HTTPException(status, "plain message")`; HTMX requests show it in
  `#flash`, full pages get `error.html`. Matter folders resolve via `matter_folder()` (stored relative).
- Matter folders: `00 Record`, `10 Source`, `20 Work Product`, `30 Authorities`, `90 Archive`;
  `documents.file_path` is relative to the matter folder.
- PowerShell scripts must run on Windows PowerShell 5.1 (no `&&`, no PS7-only syntax).
- The owner is a self-taught developer and practicing litigator: keep code plain and readable,
  prefer fewer files and steps, and explain changes in plain language. Overbuilt tooling has
  killed his past projects; each feature should replace something he does weekly.

## Performance

- One SQLite connection per app (WAL, `busy_timeout`); keep queries set-based, avoid per-row
  queries in loops (the conflicts check is the known O(names x parties) hotspot: index or
  prefilter by token before adding fuzzy scoring at scale).
- `load_rulesets()` and holiday lookups are `lru_cache`d; call `load_rulesets.cache_clear()` if
  rules are edited at runtime.
- Long work (OCR, large ingests, mail sync, AI drafting) must not block a request: move it to a
  background job and return an HTMX partial that polls.
- Keep HTMX partials small; swap only the region that changed.

## Status and next work

Phase 0 done (schema, conflicts, deadlines, ingestion, search, gateway). M0 groundwork done:
local-only request guard (Host + Origin), per-request DB lock, daily backups (`lpc/backup.py`),
HTMX errors retargeted to `#flash`, all-or-nothing matter creation, edit matter, court-order
deadlines and reschedule-with-history (migration 002). Owner's priorities, in order:
M1 find documents fast (.docx/.eml/.msg text, background ingest jobs, import folder + rescan,
open hit at PDF page, phrase search, document metadata) -> M2 chronology + depo prep (events UI,
"add to chronology" from a hit, .docx export via python-docx, optional AI-proposed events) ->
M3 discovery tracking (sets, auto-proposed response deadlines from ruleset data, limits) ->
M4 deadlines + Outlook (.ics export, trigger sets, unverified badge) -> M5 intake polish.
Later: ECF/NEF parser; email via **Outlook/Graph first** (the practice uses Microsoft 365),
Gmail after; research + cite checks; drafting; settlement analytics.
Approved dependencies for M1/M2: `extract-msg`, `python-docx`.

Open items to verify with the user, not guess: Illinois holiday list (205 ILCS 630/17),
Ill. S. Ct. R. 12 mail-service days, 7th Cir. R. 10(c), FRAP 40(d)(1) subsection, N.D. Ill.
LR 56.1 limits, Cook County entries (none yet), workbook column names vs. `docs/SCHEMA.md`.

## Commits

Small, focused commits; run tests first. Never commit `.venv/`, `rules/sources/*` downloads,
or databases.
