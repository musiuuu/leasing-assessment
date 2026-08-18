-- fixes.sql — remediations for notifyd incident tickets N1, N2, N3.
--
-- Apply against a COPY of the production database, never against the forensic
-- snapshot (incident.db is kept pristine so the evidence queries in INCIDENT.md
-- reproduce exactly). Reproduce with:
--
--     cp incident.db incident_fixed.db
--     sqlite3 incident_fixed.db < fixes.sql
--
-- The data is anchored to incident time (~2026-08-12).

-- ============================================================================
-- N1 — Reminders went nocturnal.
-- Root cause: on 2026-08-08T14:32 'ops-console' inverted the quiet-hours window
-- (quiet_start 21->9, quiet_end 9->21). Quiet hours became 09:00-21:00, so
-- notifyd defers every daytime send and only delivers overnight. Restore the
-- intended window (quiet at night, send during the day).
-- ============================================================================
UPDATE settings SET value = '21' WHERE key = 'quiet_start';
UPDATE settings SET value = '9'  WHERE key = 'quiet_end';

-- ============================================================================
-- N2 — Chronic double reminders (legacy_form clients only).
-- Root cause: a legacy reminder job 'cron-v1' is still running alongside notifyd
-- and reminds the same appointments notifyd already covers. 44 of 45 legacy_form
-- clients get two messages; 0 of 218 web clients do. notifyd's last_reminded
-- flag cannot dedupe another system's sends.
--
-- Immediate fix is OPERATIONAL, not a data change: decommission the cron-v1 job
-- (disable its cron entry / stop its service). No SQL row change belongs here.
--
-- Durable code-level defense (also prevents N3): make the messages ledger the
-- source of truth — notifyd should skip an appointment that already has ANY
-- reminder message, rather than trusting the mutable last_reminded flag, e.g.
-- change the due-selection predicate from:
--     ... AND last_reminded IS NULL
-- to:
--     ... AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.appointment_id = appointments.id)
-- ============================================================================

-- ============================================================================
-- N3 — Dana Whitfield never reminded.
-- Root cause: all of Dana's appointments carry an invalid future last_reminded
-- ('2027-03-14T09:00:00') with no corresponding message, so the
-- 'last_reminded IS NULL' selector permanently skips her. Reset ONLY her
-- upcoming tour (id 259), and only if no reminder was actually sent — the
-- NOT EXISTS guard makes this safe and idempotent.
-- ============================================================================
UPDATE appointments
SET last_reminded = NULL
WHERE id = 259
  AND client_name = 'Dana Whitfield'
  AND phone = '2145552427'
  AND NOT EXISTS (SELECT 1 FROM messages WHERE appointment_id = 259);
