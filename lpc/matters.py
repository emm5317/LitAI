"""Matters, parties and the conflicts check."""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import sqlite3
from pathlib import Path

MATTER_FOLDERS = ("00 Record", "10 Source", "20 Work Product", "30 Authorities", "90 Archive")

_ENTITY_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "corp", "corporation", "co", "company", "ltd", "limited",
    "lp", "llp", "pc", "pllc", "plc", "na", "n a", "the", "group", "holdings",
}
_SAFE_FOLDER = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and common entity suffixes, collapse whitespace."""
    text = re.sub(r"[^\w\s]", " ", name.lower())
    tokens = [t for t in text.split() if t not in _ENTITY_SUFFIXES]
    return " ".join(tokens)


class MatterError(ValueError):
    """A problem the user can fix, such as a duplicate short name. The message says what to do."""


def folder_name(short_name: str) -> str:
    return _SAFE_FOLDER.sub("-", short_name).strip(" .")


def matter_folder(matter: sqlite3.Row | dict, matters_root: Path) -> Path:
    """The matter's folder on disk.

    folder_path is stored relative to the matters root so the data survives a move to a new
    PC or a different OneDrive path. (An older absolute path still works: joining a root with
    an absolute path gives the absolute path.)
    """
    return matters_root / matter["folder_path"]


def log_change(conn: sqlite3.Connection, matter_id: int | None, entity: str, entity_id: int | None,
               summary: str, actor: str = "user") -> None:
    conn.execute(
        "INSERT INTO changelog (matter_id, actor, entity, entity_id, summary) VALUES (?, ?, ?, ?, ?)",
        (matter_id, actor, entity, entity_id, summary),
    )


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------
def get_or_create_party(conn: sqlite3.Connection, name: str, kind: str = "person", **fields) -> int:
    norm = normalize_name(name)
    row = conn.execute(
        "SELECT id FROM parties WHERE normalized_name = ? AND kind = ?", (norm, kind)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO parties (name, normalized_name, kind, email, phone, organization, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), norm, kind, fields.get("email"), fields.get("phone"),
         fields.get("organization"), fields.get("notes")),
    )
    return cur.lastrowid


def add_alias(conn: sqlite3.Connection, party_id: int, alias: str, kind: str = "aka") -> None:
    conn.execute(
        "INSERT INTO party_aliases (party_id, alias, normalized_alias, kind) VALUES (?, ?, ?, ?)",
        (party_id, alias.strip(), normalize_name(alias), kind),
    )


def add_party_to_matter(conn: sqlite3.Connection, matter_id: int, name: str, role: str,
                        kind: str = "person", title: str | None = None, notes: str | None = None) -> int:
    party_id = get_or_create_party(conn, name, kind)
    cur = conn.execute(
        "INSERT OR IGNORE INTO matter_parties (matter_id, party_id, role, title, notes)"
        " VALUES (?, ?, ?, ?, ?)",
        (matter_id, party_id, role, title, notes),
    )
    log_change(conn, matter_id, "matter_parties", cur.lastrowid, f"Added {name} as {role.replace('_', ' ')}")
    return party_id


# ---------------------------------------------------------------------------
# Matters
# ---------------------------------------------------------------------------
MATTER_FIELDS = (
    "short_name", "caption", "side", "matter_type", "practice_area", "claim_categories", "forum_type",
    "forum_name", "rules_profile", "case_number", "judge", "magistrate_judge", "arbitrator", "status",
    "phase", "ai_permitted", "commenced_on", "served_on", "summary", "notes",
)


def create_matter(conn: sqlite3.Connection, data: dict, matters_root: Path, *, commit: bool = True) -> int:
    """Insert a matter, create its folder tree, and log it. `data` keys follow MATTER_FIELDS.

    Pass commit=False to add parties in the same transaction and commit once at the end.
    An existing folder with the same name is adopted, not replaced.
    """
    values = {k: data[k] for k in MATTER_FIELDS if data.get(k) not in (None, "")}
    short_name = values.get("short_name", "")
    folder = folder_name(short_name)
    if not any(ch.isalnum() for ch in folder):
        raise MatterError("The short name needs at least one letter or number.")
    if conn.execute("SELECT 1 FROM matters WHERE short_name = ?", (short_name,)).fetchone():
        raise MatterError(f"A matter named \"{short_name}\" already exists. Choose a different short name.")
    # Windows folder names ignore case, and characters like : / ? become "-", so two
    # different short names can map to the same folder.
    other = conn.execute("SELECT short_name FROM matters WHERE lower(folder_path) = lower(?)", (folder,)).fetchone()
    if other:
        raise MatterError(f"The folder \"{folder}\" is already used by the matter \"{other['short_name']}\". "
                          "Choose a different short name.")
    values["folder_path"] = folder
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO matters ({cols}) VALUES ({marks})", tuple(values.values()))
    matter_id = cur.lastrowid
    for sub in MATTER_FOLDERS:
        (matters_root / folder / sub).mkdir(parents=True, exist_ok=True)
    log_change(conn, matter_id, "matters", matter_id, f"Opened matter {short_name}")
    if commit:
        conn.commit()
    return matter_id


# Choices for the edit form; they mirror the CHECK constraints in 001_matter_schema.sql.
MATTER_TYPES = ("single_plaintiff", "class", "collective", "hybrid_class_collective", "individual_arbitration",
                "mass_arbitration", "pre_suit", "agency_charge", "counseling", "appeal")
FORUM_TYPES = ("federal_district", "federal_appeals", "state_trial", "state_appeals", "aaa", "jams",
               "other_arbitration", "agency", "pre_suit")
STATUSES = ("intake", "active", "stayed", "settled_pending", "appeal", "closed", "declined")
PHASES = ("pre_suit", "pleadings", "discovery", "dispositive_motions", "class_cert", "mediation", "trial",
          "post_trial", "appeal", "settlement_admin")

# Everything but short_name, which names the matter folder and stays fixed once opened.
EDITABLE_FIELDS = (
    "caption", "side", "matter_type", "claim_categories", "forum_type", "forum_name", "rules_profile",
    "case_number", "judge", "magistrate_judge", "arbitrator", "status", "phase", "ai_permitted",
    "commenced_on", "served_on", "summary", "notes",
)
_LONG_TEXT = {"summary", "notes"}


def _describe_change(field: str, old, new) -> str:
    if field == "ai_permitted":
        return "AI consent turned " + ("on" if new else "off")
    label = field.replace("_", " ")
    if field in _LONG_TEXT:
        return f"Updated {label}"
    show = lambda v: str(v).replace("_", " ") if v not in (None, "") else "(blank)"  # noqa: E731
    return f"Changed {label}: {show(old)} -> {show(new)}"


def update_matter(conn: sqlite3.Connection, matter_id: int, data: dict) -> list[str]:
    """Save edited fields, log each change, and return the change summaries (empty if nothing changed).

    Blank values are stored as NULL. Closing a matter stamps closed_on; reopening clears it.
    Does not commit.
    """
    current = get_matter(conn, matter_id)
    if current is None:
        raise MatterError("Matter not found.")
    changes: dict = {}
    for field in EDITABLE_FIELDS:
        if field not in data:
            continue
        new = None if data[field] in ("", None) else data[field]
        if new != current[field]:
            changes[field] = new
    if not changes:
        return []
    summaries = [_describe_change(f, current[f], v) for f, v in changes.items()]
    if "status" in changes:
        if changes["status"] == "closed" and not current["closed_on"]:
            changes["closed_on"] = dt.date.today().isoformat()
        elif changes["status"] != "closed":
            changes["closed_on"] = None
    assignments = ", ".join(f"{field} = ?" for field in changes)
    conn.execute(f"UPDATE matters SET {assignments}, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                 " WHERE id = ?", (*changes.values(), matter_id))
    for summary in summaries:
        log_change(conn, matter_id, "matters", matter_id, summary)
    return summaries


def get_matter(conn: sqlite3.Connection, matter_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM matters WHERE id = ?", (matter_id,)).fetchone()


def list_matters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM v_matter_dashboard ORDER BY CASE status WHEN 'closed' THEN 1 ELSE 0 END,"
        " next_deadline IS NULL, next_deadline, short_name"
    ).fetchall()


def matter_parties(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT mp.*, p.name, p.kind FROM matter_parties mp JOIN parties p ON p.id = mp.party_id"
        " WHERE mp.matter_id = ? ORDER BY mp.role, p.name",
        (matter_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Conflicts check
# ---------------------------------------------------------------------------
def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if ta and tb and (ta <= tb or tb <= ta) and min(len(ta), len(tb)) >= 1:
        # every token of the shorter name appears in the longer one ("acme" vs "acme logistics")
        overlap = 0.9 if min(len(ta), len(tb)) > 1 else 0.8
    else:
        overlap = 0.0
    return max(overlap, difflib.SequenceMatcher(None, a, b).ratio())


def conflicts_search(conn: sqlite3.Connection, names: list[str], threshold: float = 0.82) -> list[dict]:
    """Find every prior appearance of a similar name in any matter, by party name or alias."""
    candidates = conn.execute(
        "SELECT p.id AS party_id, p.name, p.normalized_name AS norm, NULL AS alias FROM parties p"
        " UNION ALL"
        " SELECT a.party_id, p.name, a.normalized_alias AS norm, a.alias FROM party_aliases a"
        " JOIN parties p ON p.id = a.party_id"
    ).fetchall()
    hits: dict[tuple, dict] = {}
    for query in (n for n in names if n and n.strip()):
        qn = normalize_name(query)
        for cand in candidates:
            score = _similar(qn, cand["norm"])
            if score < threshold:
                continue
            for mp in conn.execute(
                "SELECT mp.role, m.id AS matter_id, m.short_name, m.caption, m.side, m.status"
                " FROM matter_parties mp JOIN matters m ON m.id = mp.matter_id WHERE mp.party_id = ?",
                (cand["party_id"],),
            ):
                key = (query, cand["party_id"], mp["matter_id"], mp["role"])
                if key not in hits or hits[key]["score"] < score:
                    hits[key] = {
                        "query": query, "matched_name": cand["name"], "matched_alias": cand["alias"],
                        "score": round(score, 2), "role": mp["role"], "matter_id": mp["matter_id"],
                        "short_name": mp["short_name"], "caption": mp["caption"],
                        "side": mp["side"], "status": mp["status"],
                    }
    return sorted(hits.values(), key=lambda h: (-h["score"], h["query"]))


def record_conflict_check(conn: sqlite3.Connection, names: list[str], results: list[dict],
                          outcome: str | None = None, matter_id: int | None = None,
                          notes: str | None = None) -> int:
    if outcome is None:
        outcome = "clear" if not results else "potential"
    cur = conn.execute(
        "INSERT INTO conflict_checks (names, results, outcome, matter_id, notes) VALUES (?, ?, ?, ?, ?)",
        (json.dumps(names), json.dumps(results), outcome, matter_id, notes),
    )
    conn.commit()
    return cur.lastrowid
