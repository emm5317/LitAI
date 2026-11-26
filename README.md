# Law Practice Console

A local-first litigation console for a solo practice: matters, conflicts, deadlines,
documents and search, with research, drafting, email and settlement analysis to follow.
Everything runs on your Windows machine; client data never leaves it except for AI calls
you allow, which go through a zero-data-retention gateway and are logged.

This is the **Phase 0 scaffold**: FastAPI + HTMX + Alpine + SQLite, wrapped in a desktop
window by pywebview (Microsoft Edge WebView2).

## Set up on Windows

1. Install Python 3.12: `winget install Python.Python.3.12` (or python.org, with "Add to PATH").
2. In PowerShell, from this folder:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if scripts are blocked
   .\scripts\setup.ps1
   .\scripts\dev.ps1            # desktop window
   .\scripts\dev.ps1 -Browser   # or http://127.0.0.1:8765 in your browser
   ```
3. Run the tests: `.\.venv\Scripts\python.exe -m pytest`
4. Optional, for AI features later: store your Anthropic API key in Windows Credential Manager
   (the command is printed at the end of `setup.ps1`). Request zero data retention on that API
   organization before sending any client material.

The database lives in `%LOCALAPPDATA%\LawPracticeConsole\lpc.sqlite3` and never syncs. Matter
folders go in `OneDrive\Law Practice Console\Matters` (or `Documents\...` without OneDrive).
Override with `LPC_DATA_DIR` / `LPC_MATTERS_DIR`. With OneDrive's Files On-Demand, adding a
cloud-only file to a matter downloads it first.

**Backups.** Each launch (and hourly while open) the app copies the database to
`OneDrive\Law Practice Console\Backups\lpc-YYYY-MM-DD.sqlite3` (or `Backups` in the data folder
without OneDrive), keeping the newest 14; override with `LPC_BACKUP_DIR`. The dashboard shows
the last backup date. To restore: close the app, delete `lpc.sqlite3` (and any `-wal`/`-shm`
files) in the data folder, copy a backup there and rename it `lpc.sqlite3`.
Turn on BitLocker (or Device Encryption) before putting real client files here.

## What works now

- **Open a matter** with a logged conflicts check (fuzzy matching across every party,
  alias and prior matter), the side you represent, forum and rules profile. The matter
  folder tree is created automatically.
- **Edit a matter** (status, phase, judge, case number, AI consent...); every change is logged.
- **Deadlines**: pick a rule, enter the trigger date and service method, and the engine
  proposes a date with its arithmetic shown. Nothing is calendared until you confirm it.
  Court-order and stipulated deadlines can be entered by hand, and any open deadline can be
  moved to a new date with a reason; the old date is kept in its history.
- **Documents**: drop files in; PDFs are split into pages, hashed, de-duplicated, and
  indexed. Low-text PDFs are flagged for OCR.
- **Search**: Ctrl+K searches every document; each matter page searches just that matter.
- **Local only**: the app refuses requests that are not addressed to 127.0.0.1/localhost and
  forms submitted from other sites, so a web page in your browser cannot reach your data.
- **AI gateway** (no UI yet): blocks calls without client consent or on non-ZDR models,
  sends plain Messages API requests, and logs every call to `ai_calls`.

## Layout

```
lpc/
  app.py                 FastAPI routes (pages + HTMX partials)
  matters.py             matters, parties, conflicts check
  ingest.py              document ingestion and full-text search
  ai/gateway.py          the only path to a model
  migrations/            SQL schema (docs/SCHEMA.md)
  rules/
    engine.py            deadline arithmetic (FRCP 6(a), 5 ILCS 70/1.11, AAA, JAMS)
    holidays.py          federal + Illinois holiday calendars
    data/rulesets/       one YAML file per forum: frcp, frap, ndil, ca7, illinois, cook,
                         aaa_employment, jams_employment, federal_statutes
    sources.json         official source for every ruleset, with version notes
  templates/, static/    HTMX + Alpine UI (vendored, no CDN)
scripts/                 setup.ps1, dev.ps1, build.ps1, fetch_rules.ps1
docs/                    SCHEMA.md, RULES_CHECKLIST.md
```

## Deadline rules

Rules are data. Each ruleset names its counting method, holiday calendars and service
rules, can inherit from a base (`ndil` inherits `frcp` and `federal_statutes`; `cook`
inherits `illinois`), and lists entries with a citation, trigger and period. A new court
is a new YAML file, not new code.

**Every entry ships with `verified: false`.** The periods were written from the rules as
they stood on Nov. 26, 2026, but they are paraphrased data and must be checked:

1. `.\scripts\fetch_rules.ps1` downloads the official texts it can into `rules\sources\`
   and lists the ones to save by hand.
2. Work through `docs/RULES_CHECKLIST.md` (regenerate with `python -m lpc.rules.checklist`).
3. Set `verified: true` on each entry once it matches. Unverified entries show a warning
   every time they are used.

Also review `lpc/rules/data/holidays_illinois.yaml` against 205 ILCS 630/17 and the Cook
County court-holiday list each year, and add court closures to `closures.yaml`.
Judges' standing orders and scheduling orders override all of this; enter those as
court-order deadlines.

## Build an .exe

`.\scripts\build.ps1` produces `dist\LawPracticeConsole\LawPracticeConsole.exe`. It is
unsigned, so SmartScreen will warn until you sign it with a code-signing certificate.
