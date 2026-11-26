# Matter schema

The schema lives in `lpc/migrations/001_matter_schema.sql` (SQLite). It follows the
Litigation Case Workbook's tabs and the Case Wiki notes, adds a required `side` field,
and adds what a console needs that a workbook does not: a party registry for conflicts,
page-level document text for search, deadlines with their arithmetic, and an AI audit log.

Billing, invoicing and trust accounting are deliberately out of scope.

## Workbook tab → table

| Workbook tab / wiki note | Table(s) | Notes |
| --- | --- | --- |
| Dashboard / 00-HUB | `matters`, view `v_matter_dashboard` | `side` is required: `employer` or `employee`. `rules_profile` picks the deadline rules (e.g. `ndil`, `cook`, `aaa_employment`). `ai_permitted` records client consent. |
| People / 30-People | `parties`, `party_aliases`, `matter_parties` | One registry across all matters, so the conflicts check sees every prior client, adversary, witness and counsel. Roles: client, adverse party, opposing counsel, witness, decision-maker, comparator, opt-in, expert, and more. |
| Key Events / 10-Chronology | `events`, `event_people` | `event_code` (E-001...) is the stable ID that depo outlines key to. Partial dates allowed with `date_precision`. Each event can cite a source document and pin. |
| Claims / 20-Claims | `claims`, `claim_elements`, `defenses` | Elements carry proof notes and a strength flag. |
| Document Log / 40-Documents | `documents`, `document_pages`, `document_pages_fts` | Bates range, docket number, SHA-256 (no duplicate files per matter), text status (`low_text` flags OCR candidates). Page text is indexed with SQLite FTS5. |
| Task List | `tasks` | Optional link to a deadline. Practice-level tasks have no matter. |
| (none) | `deadlines` | Every computed deadline is stored as `proposed` with its rule citation and the arithmetic, and must be confirmed. |
| 50-Discovery | `discovery_sets` | Served/received sets with response dates and meet-and-confer status. |
| 60-Issues | `issues` | Issue frame for research and drafting. |
| 99-Changelog | `changelog` | Who changed what, including the AI and system. |
| Hidden Lists sheet | `CHECK` constraints | Closed vocabularies are enforced in the schema. |
| (none) | `docket_entries`, `conflict_checks`, `ai_calls`, `settings` | ECF/AAA/JAMS docket, logged conflicts checks, every model call. |

## Reconcile with your workbook

This version was built from the workbook's tab names, not its column headers. When you
have the template handy, compare its columns against these tables; anything missing is a
one-line `ALTER TABLE` in a new migration file (`002_...sql`), which the app applies on
next start.

## Conventions

- Dates are ISO text (`YYYY-MM-DD`); timestamps are UTC ISO text.
- Matter-scoped rows are deleted with their matter (`ON DELETE CASCADE`); parties are not.
- Matter folders mirror your OneDrive layout: `00 Record`, `10 Source`, `20 Work Product`,
  `30 Authorities`, `90 Archive`. Document paths are stored relative to the matter folder.
