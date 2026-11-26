-- 007: a soft reminder that has passed (e.g. "Responses due from Acme to our First RFPs") no longer
-- counts as a matter's next deadline; the dashboard's "Discovery to chase" list shows it instead.
-- Otherwise the same as the view in 001.
DROP VIEW v_matter_dashboard;
CREATE VIEW v_matter_dashboard AS
SELECT
    m.id, m.short_name, m.caption, m.side, m.status, m.phase, m.forum_name, m.case_number, m.judge,
    (SELECT MIN(d.due_on) FROM deadlines d
      WHERE d.matter_id = m.id AND d.status IN ('proposed', 'confirmed')
        AND (d.hard = 1 OR d.due_on >= date('now', 'localtime'))) AS next_deadline,
    (SELECT COUNT(*) FROM deadlines d
      WHERE d.matter_id = m.id AND d.status = 'proposed') AS unconfirmed_deadlines,
    (SELECT COUNT(*) FROM tasks t
      WHERE t.matter_id = m.id AND t.status NOT IN ('done', 'dropped')) AS open_tasks,
    (SELECT COUNT(*) FROM documents doc WHERE doc.matter_id = m.id) AS documents,
    (SELECT MAX(e.event_date) FROM events e WHERE e.matter_id = m.id) AS last_event
FROM matters m;
