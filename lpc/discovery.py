"""Discovery tracking: sets served and received, their response deadlines, status, and limits.

Saving a set with a service date proposes its response deadline from the matter's rules (the
ruleset's `discovery:` map names the entry, e.g. ndil -> frcp.interrogatory_responses). A set we
received gets a hard deadline ("Respond to ..."); a set we served gets a soft one ("Responses due
on our ..."), so overdue answers from the other side show up. While that deadline is still only
proposed it follows edits to the set; once confirmed it is never changed here. Where the rules
name no deadline (subpoenas, deposition notices, AAA/JAMS) a response date typed on the set is
saved as a confirmed deadline, the same as any deadline entered by hand.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from .matters import log_change
from .rules.engine import compute, discovery_entry, discovery_limit, load_rulesets

SET_TYPES = {"interrogatories": "Interrogatories", "rfp": "Requests for production", "rfa": "Requests to admit",
             "subpoena": "Subpoena", "depo_notice": "Deposition notice", "initial_disclosures": "Initial disclosures",
             "expert_disclosure": "Expert disclosure"}
STATUSES = {"drafting": "Drafting", "open": "Open", "responded": "Responded", "deficient": "Deficient",
            "meet_confer": "Meet and confer", "motion": "Motion", "resolved": "Resolved"}
SERVICE_METHODS = ("electronic", "mail", "hand", "other_consented")
FIELDS = ("direction", "set_type", "set_label", "party_matter_party_id", "served_on", "service_method",
          "request_count", "response_due_on", "responded_on", "status", "request_document_id",
          "response_document_id", "notes")
CHASE_STATUSES = ("deficient", "meet_confer", "motion")
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


class DiscoveryError(ValueError):
    """A problem the user can fix; the message says what to do."""


def label_of(s: sqlite3.Row | dict) -> str:
    return s["set_label"] or SET_TYPES.get(s["set_type"], s["set_type"])


def save_set(conn: sqlite3.Connection, matter_id: int, data: dict, set_id: int | None = None) -> tuple[int, str | None]:
    """Insert or update a set, then propose or update its response deadline.

    Returns (set id, a note for the user or None). Does not commit.
    """
    values = {f: data.get(f) for f in FIELDS if f in data}
    values = {f: (v.strip() if isinstance(v, str) else v) for f, v in values.items()}
    values = {f: (None if v == "" else v) for f, v in values.items()}
    if values.get("direction") not in ("served", "received"):
        raise DiscoveryError("Say whether we served this set or received it.")
    if values.get("set_type") not in SET_TYPES:
        raise DiscoveryError("Choose what kind of discovery this is.")
    values["status"] = values.get("status") or "open"
    if values["status"] not in STATUSES:
        raise DiscoveryError(f"Unknown status: {values['status']}.")
    for field in ("served_on", "response_due_on", "responded_on"):
        if values.get(field):
            try:
                dt.date.fromisoformat(values[field])
            except ValueError:
                raise DiscoveryError(f"Enter the {field.replace('_', ' ')} date as YYYY-MM-DD.") from None
    if values.get("service_method") and values["service_method"] not in SERVICE_METHODS:
        raise DiscoveryError(f"Unknown service method: {values['service_method']}.")
    if values.get("request_count") is not None:
        try:
            values["request_count"] = int(values["request_count"])
        except (TypeError, ValueError):
            raise DiscoveryError("The number of requests must be a whole number.") from None
    for field, table in (("party_matter_party_id", "matter_parties"), ("request_document_id", "documents"),
                         ("response_document_id", "documents")):
        if values.get(field) is not None and not conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ? AND matter_id = ?", (values[field], matter_id)).fetchone():
            raise DiscoveryError("That person or document is not in this matter.")
    if values["status"] == "responded" and not values.get("responded_on"):
        values["responded_on"] = dt.date.today().isoformat()

    if set_id is None:
        cols = ", ".join(values)
        cur = conn.execute(f"INSERT INTO discovery_sets (matter_id, {cols}) VALUES (?, {', '.join('?' * len(values))})",
                           (matter_id, *values.values()))
        set_id = cur.lastrowid
        log_change(conn, matter_id, "discovery_sets", set_id,
                   f"Added discovery {values['direction']}: "
                   f"{label_of({'set_label': values.get('set_label'), 'set_type': values['set_type']})}")
    else:
        old = conn.execute("SELECT * FROM discovery_sets WHERE id = ?", (set_id,)).fetchone()
        conn.execute(f"UPDATE discovery_sets SET {', '.join(f'{f} = ?' for f in values)} WHERE id = ?",
                     (*values.values(), set_id))
        if old["status"] != values["status"]:
            log_change(conn, matter_id, "discovery_sets", set_id,
                       f"Discovery {label_of(old)}: {STATUSES[old['status']]} -> {STATUSES[values['status']]}")
        else:
            log_change(conn, matter_id, "discovery_sets", set_id, f"Edited discovery {label_of(old)}")
    return set_id, sync_deadline(conn, set_id)


def set_status(conn: sqlite3.Connection, set_id: int, status: str) -> str | None:
    """Move a set to a new status (logged with the date). Does not commit."""
    row = conn.execute("SELECT * FROM discovery_sets WHERE id = ?", (set_id,)).fetchone()
    data = dict(row) | {"status": status}
    return save_set(conn, row["matter_id"], data, set_id)[1]


def _deadline_title(s: sqlite3.Row, party: str | None) -> str:
    label = label_of(s)
    if s["direction"] == "received":
        return f"Respond to {label}" + (f" ({party})" if party else "")
    return "Responses due" + (f" from {party}" if party else "") + f" to our {label}"


def sync_deadline(conn: sqlite3.Connection, set_id: int) -> str | None:
    """Create or update the set's response deadline. Returns a note for the user, or None."""
    s = conn.execute("SELECT ds.*, m.rules_profile, p.name AS party FROM discovery_sets ds"
                     " JOIN matters m ON m.id = ds.matter_id"
                     " LEFT JOIN matter_parties mp ON mp.id = ds.party_matter_party_id"
                     " LEFT JOIN parties p ON p.id = mp.party_id WHERE ds.id = ?", (set_id,)).fetchone()
    existing = conn.execute("SELECT * FROM deadlines WHERE id = ?", (s["deadline_id"],)).fetchone() if s["deadline_id"] else None
    title = _deadline_title(s, s["party"])
    hard = 1 if s["direction"] == "received" else 0

    if s["status"] in ("responded", "resolved"):
        if existing and existing["status"] in ("proposed", "confirmed"):
            conn.execute("UPDATE deadlines SET status = 'done' WHERE id = ?", (existing["id"],))
            log_change(conn, s["matter_id"], "deadlines", existing["id"], f"Deadline done: {existing['title']}")
        return None
    if s["status"] == "drafting" or not s["served_on"]:
        return None

    entry = discovery_entry(s["rules_profile"], s["set_type"]) if s["rules_profile"] in load_rulesets() else None
    if entry is None:
        # No rule: a response date typed on the set becomes a confirmed deadline (you typed it).
        if not s["response_due_on"]:
            return ("The matter's rules give no response period for this kind of set; "
                    "enter the response date if one applies.")
        if existing:
            if existing["due_on"] != s["response_due_on"] and existing["status"] in ("proposed", "confirmed"):
                conn.execute("UPDATE deadlines SET due_on = ?, title = ? WHERE id = ?",
                             (s["response_due_on"], title, existing["id"]))
            return None
        cur = conn.execute(
            "INSERT INTO deadlines (matter_id, title, due_on, trigger_event, trigger_date, computation, hard, status,"
            f" source, confirmed_at) VALUES (?, ?, ?, 'service of the set', ?, ?, ?, 'confirmed', 'manual', {NOW})",
            (s["matter_id"], title, s["response_due_on"], s["served_on"],
             "Entered on the discovery set (no rule gives this period).", hard))
        conn.execute("UPDATE discovery_sets SET deadline_id = ? WHERE id = ?", (cur.lastrowid, set_id))
        log_change(conn, s["matter_id"], "deadlines", cur.lastrowid, f"Added deadline: {title} {s['response_due_on']}")
        return None

    comp = compute(entry.id, dt.date.fromisoformat(s["served_on"]), s["service_method"], profile=s["rules_profile"])
    due = comp.due.isoformat()
    explanation = comp.explanation + (" " + " ".join(comp.warnings) if comp.warnings else "")
    if existing and existing["status"] == "confirmed":
        if existing["due_on"] != due:
            return (f"The response deadline is already confirmed for {existing['due_on']}; the rules now give {due}. "
                    "Change the deadline itself if it should move.")
        return None
    if existing and existing["status"] == "proposed":
        conn.execute("UPDATE deadlines SET title = ?, due_on = ?, trigger_date = ?, service_method = ?, computation = ?,"
                     " hard = ?, rule_id = ?, rule_cite = ? WHERE id = ?",
                     (title, due, s["served_on"], s["service_method"], explanation, hard, entry.id, entry.cite,
                      existing["id"]))
        conn.execute("UPDATE discovery_sets SET response_due_on = ? WHERE id = ?", (due, set_id))
        return None
    cur = conn.execute(
        "INSERT INTO deadlines (matter_id, title, due_on, rule_id, rule_cite, trigger_event, trigger_date,"
        " service_method, computation, hard, status, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', 'rules_engine')",
        (s["matter_id"], title, due, entry.id, entry.cite, entry.trigger, s["served_on"], s["service_method"],
         explanation, hard))
    conn.execute("UPDATE discovery_sets SET deadline_id = ?, response_due_on = ? WHERE id = ?", (cur.lastrowid, due, set_id))
    log_change(conn, s["matter_id"], "deadlines", cur.lastrowid, f"Proposed deadline: {title} {due}", actor="system")
    return None


# ----------------------------------------------------------------- reading
def matter_sets(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ds.*, p.name AS party, d.status AS deadline_status, d.due_on AS deadline_due,"
        " rq.title AS request_title, rs.title AS response_title FROM discovery_sets ds"
        " LEFT JOIN matter_parties mp ON mp.id = ds.party_matter_party_id LEFT JOIN parties p ON p.id = mp.party_id"
        " LEFT JOIN deadlines d ON d.id = ds.deadline_id"
        " LEFT JOIN documents rq ON rq.id = ds.request_document_id LEFT JOIN documents rs ON rs.id = ds.response_document_id"
        " WHERE ds.matter_id = ? ORDER BY ds.direction, ds.served_on IS NULL, ds.served_on, ds.id", (matter_id,)).fetchall()


def limits_summary(conn: sqlite3.Connection, matter_id: int, profile: str | None) -> list[dict]:
    """Requests counted against each limit, per direction and party: 'served on Acme: 18 of 25'."""
    out = []
    rows = conn.execute(
        "SELECT ds.direction, ds.set_type, p.name AS party, SUM(COALESCE(ds.request_count, 0)) AS used, COUNT(*) AS sets"
        " FROM discovery_sets ds LEFT JOIN matter_parties mp ON mp.id = ds.party_matter_party_id"
        " LEFT JOIN parties p ON p.id = mp.party_id WHERE ds.matter_id = ? AND ds.status != 'drafting'"
        " GROUP BY ds.direction, ds.set_type, p.name ORDER BY ds.direction, ds.set_type, p.name", (matter_id,)).fetchall()
    for r in rows:
        limit = discovery_limit(profile, r["set_type"])
        if limit:
            out.append({**dict(r), "limit": limit["count"], "cite": limit["cite"], "note": limit.get("note"),
                        "verified": bool(limit.get("verified")), "over": r["used"] > limit["count"]})
    return out


def to_chase(conn: sqlite3.Connection, today: dt.date | None = None) -> list[sqlite3.Row]:
    """Across all matters: responses the other side owes us that are overdue, and sets stuck in
    deficiency, meet-and-confer or motion practice."""
    today = (today or dt.date.today()).isoformat()
    return conn.execute(
        "SELECT ds.*, m.short_name, p.name AS party FROM discovery_sets ds JOIN matters m ON m.id = ds.matter_id"
        " LEFT JOIN matter_parties mp ON mp.id = ds.party_matter_party_id LEFT JOIN parties p ON p.id = mp.party_id"
        " WHERE m.status NOT IN ('closed', 'declined') AND ("
        "   (ds.direction = 'served' AND ds.status = 'open' AND ds.response_due_on < ?)"
        f"   OR ds.status IN ({', '.join('?' * len(CHASE_STATUSES))}))"
        " ORDER BY ds.response_due_on IS NULL, ds.response_due_on", (today, *CHASE_STATUSES)).fetchall()
