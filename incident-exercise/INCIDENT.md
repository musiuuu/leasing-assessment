# notifyd — Incident Forensics

All three tickets are **data/configuration problems, not code defects** — consistent
with the note that the running code is exactly what was audited. Evidence below is
reproducible against the supplied `incident.db`; fixes are in `fixes.sql` and are
demonstrated on a copy (`incident_fixed.db`), leaving the snapshot pristine.

## Method note: the snapshot's clock

The snapshot is anchored around **2026-08-12** (latest message `2026-08-12T21:08`,
latest heartbeat `2026-08-12T20:54`; tours run to 08-14). Its effective "now" is
≈ `2026-08-12T21:09:44`. This matters because `notifyd`'s selection is relative to
`datetime.now()`: run on a later real date, nothing is due (all tours are "past").
Selection/fix demonstrations below therefore pin the clock to snapshot time. It is
also why N1's onset ("about four days ago") lines up with the 08-08 change.

```sql
SELECT MAX(sent_at) AS last_msg,
       (SELECT MAX(ts) FROM heartbeats) AS last_heartbeat,
       (SELECT MAX(tour_at) FROM appointments) AS last_tour
FROM messages;
-- 2026-08-12T21:08:23 | 2026-08-12T20:54:44 | 2026-08-14T22:14:40
```

---

## N1 — Reminders went nocturnal

### Root cause
On **2026-08-08T14:32**, `changed_by = 'ops-console'` (a manual console change — no
deploy) **inverted the quiet-hours window**: `quiet_start` 21→9 and `quiet_end` 9→21.
Quiet hours are now **09:00–21:00**, i.e. the entire working day. `in_quiet_hours`
therefore defers every daytime send, and reminders only go out overnight. Morning
and daytime tours — whose reminder would naturally fire during the day — are pushed
into the middle of the night or missed entirely.

### Evidence

**1. The audit trail — a manual, non-deploy inversion four days before the reports:**
```sql
SELECT id, key, old_value, new_value, changed_by, changed_at
FROM settings_audit
WHERE key IN ('quiet_start','quiet_end')
ORDER BY changed_at;
```
```
1 | quiet_start | (null) | 21 | deploy      | 2026-05-12T21:09:44
2 | quiet_end   | (null) | 9  | deploy      | 2026-05-12T21:09:44
4 | quiet_start | 21     | 9  | ops-console | 2026-08-08T14:32:05   <-- inverted
5 | quiet_end   | 9      | 21 | ops-console | 2026-08-08T14:32:16   <-- inverted
```

**2. Current settings confirm the inverted (daytime-quiet) state:**
```sql
SELECT key, value FROM settings WHERE key IN ('quiet_start','quiet_end');
-- quiet_start = 9 , quiet_end = 21
```

**3. Deterministic effect (from the code): with these settings the only sendable
hours are overnight.** Evaluating `in_quiet_hours` for a Chicago client across all
24 hours under the current vs the correct settings:
```
CURRENT (quiet_start=9,  quiet_end=21): sendable = 00–08, 21–23   (overnight only)
CORRECT (quiet_start=21, quiet_end=9 ): sendable = 09–20          (business hours)
```

**4. Corroboration in real sends — notifyd's own deliveries after the change are
entirely nocturnal:**
```sql
SELECT substr(sent_at,12,2) AS hour, COUNT(*) AS n
FROM messages
WHERE sender = 'notifyd' AND sent_at >= '2026-08-08T14:32:16'
GROUP BY hour;
-- 21 | 14     (every post-change notifyd send is at hour 21)
```

### Immediate fix
Restore the intended window (see `fixes.sql`):
```sql
UPDATE settings SET value = '21' WHERE key = 'quiet_start';
UPDATE settings SET value = '9'  WHERE key = 'quiet_end';
```
Verified on `incident_fixed.db`: sendable hours return to `09–20`.

### Prevention
- Validate settings writes: reject a quiet window that covers business hours
  (e.g. require `quiet_start` in the evening).
- Alert on any `settings_audit` change to `quiet_start`/`quiet_end`.
- Add a startup/tick sanity check that logs a warning if the effective send window
  excludes daytime.

---

## N2 — Chronic double reminders

### Root cause
There are **two independent senders** writing to `messages`: `notifyd` and a legacy
job **`cron-v1`** that was never decommissioned. `cron-v1` only ever reminds
**`legacy_form`-sourced** appointments, which `notifyd` also covers — so those
clients get two reminders. That is the trait the affected clients share
("the same kind of client"). notifyd's `last_reminded` flag cannot dedupe against a
different system's sends.

### Evidence

**1. Two senders in the ledger:**
```sql
SELECT sender, COUNT(*) FROM messages GROUP BY sender;
-- notifyd | 254
-- cron-v1 | 44
```

**2. Doubles are entirely a `legacy_form` phenomenon:**
```sql
SELECT a.source,
       SUM(CASE WHEN d.n > 1 THEN 1 ELSE 0 END) AS doubled,
       COUNT(*) AS total
FROM appointments a
LEFT JOIN (SELECT appointment_id, COUNT(*) n FROM messages GROUP BY appointment_id) d
       ON d.appointment_id = a.id
GROUP BY a.source;
-- legacy_form | 44 | 45      (nearly all)
-- web         | 0  | 218     (none)
```

**3. A doubled appointment shows one send from each system:**
```sql
SELECT appointment_id, sender, sent_at FROM messages
WHERE appointment_id = 257 ORDER BY sent_at;
-- 257 | cron-v1 | 2026-08-12T12:57:47
-- 257 | notifyd | 2026-08-12T21:08:23
```
`cron-v1` has only ever messaged `legacy_form` appointments (confirmable via a join
on `sender = 'cron-v1'`), and the paired sends land minutes apart for many clients
(tightest gaps ≈ 6 minutes), matching the report of two reminders a few minutes apart.

### Immediate fix
**Operational, not a data change: decommission the `cron-v1` job** (disable its cron
entry / stop the service). `notifyd` already covers every appointment, including
`legacy_form`. No row edit belongs in `fixes.sql` for this.

### Prevention
- Single owner for reminders; retire legacy senders as part of any migration.
- **Make the message ledger the source of truth** in notifyd — skip an appointment
  that already has *any* reminder message rather than trusting `last_reminded`:
  ```
  ... AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.appointment_id = appointments.id)
  ```
  This is sender-agnostic (would have suppressed the `cron-v1` duplicates) and, as a
  bonus, also fixes N3 (see below).

---

## N3 — Dana never gets reminded

### Root cause
All of Dana Whitfield's appointments carry an **invalid future `last_reminded`
(`2027-03-14T09:00:00`) with no corresponding message**. `due_appointments` selects
only `last_reminded IS NULL`, so she is permanently skipped. Her phone is irrelevant —
she is filtered out before any send is attempted. (The snapshot has no
appointment-level audit trail, so *how* that value was written is not provable and is
not claimed; what is provable is that the flag is invalid and unbacked by a send.)

### Evidence

**1. Every Dana appointment is "reminded" with an impossible future timestamp:**
```sql
SELECT id, tour_at, last_reminded
FROM appointments WHERE client_name = 'Dana Whitfield' ORDER BY id;
-- 86  | 2026-07-03T21:09:44 | 2027-03-14T09:00:00
-- 202 | 2026-07-31T21:09:44 | 2027-03-14T09:00:00
-- 259 | 2026-08-13T19:09:44 | 2027-03-14T09:00:00
```
`last_reminded` is after every `tour_at` — a reminder supposedly sent months *after*
the tour.

**2. She has zero reminder messages despite all three being "reminded":**
```sql
SELECT COUNT(*) FROM messages m
JOIN appointments a ON a.id = m.appointment_id
WHERE a.client_name = 'Dana Whitfield';
-- 0
```
The flag and the ledger disagree — the flag is invalid data, not a record of a send.

**3. Her upcoming tour is inside the 24h window but the flag blocks selection**
(snapshot now ≈ `2026-08-12T21:09:44`):
```sql
SELECT id, tour_at,
       (tour_at > '2026-08-12T21:09:44'
        AND tour_at <= '2026-08-13T21:09:44') AS within_24h,
       (last_reminded IS NULL)                AS passes_selector
FROM appointments WHERE id = 259;
-- 259 | 2026-08-13T19:09:44 | 1 | 0
```
Qualifies on time (`within_24h = 1`), rejected solely by the flag (`passes_selector = 0`).

### Immediate fix
Reset **only the upcoming appointment**, and only if no reminder was truly sent —
the `NOT EXISTS` guard makes it safe and idempotent (see `fixes.sql`):
```sql
UPDATE appointments
SET last_reminded = NULL
WHERE id = 259
  AND client_name = 'Dana Whitfield'
  AND phone = '2145552427'
  AND NOT EXISTS (SELECT 1 FROM messages WHERE appointment_id = 259);
```
Her two past tours are left untouched — even with a NULL flag, `tour_at > now`
excludes them, so no reminder fires for tours that already happened.

### Verification (clock pinned to snapshot time)
After the reset, running one tick during business hours (`now = 2026-08-13T09:00`,
within 24h of her 19:09 tour and inside the restored send window) delivers her reminder:
```
reminded 2145552427 for tour at 2026-08-13 19:09
```
```sql
SELECT a.id, a.last_reminded, m.sender, m.sent_at, m.body
FROM appointments a JOIN messages m ON m.appointment_id = a.id
WHERE a.id = 259;
-- 259 | 2026-08-13T09:00:00 | notifyd | 2026-08-13T09:00:00 |
--     Hi Dana! Reminder: your apartment tour is at 2026-08-13 19:09. Reply R to reschedule.
```

### Prevention (class of problem: reminder-state drifting from the ledger)
- Constrain the value: reject `last_reminded` in the future or later than `tour_at`.
- **Derive "reminded?" from the messages ledger** rather than a mutable flag — this
  also fixes N2.
- Alert on any appointment with `last_reminded IS NOT NULL` and no message row.
- Extend audit logging to administrative/import writes on `appointments.last_reminded`.

---

## Fix summary

| Ticket | Immediate fix | Type |
|--------|---------------|------|
| N1 | Restore `quiet_start=21`, `quiet_end=9` | Config (`fixes.sql`) |
| N2 | Decommission the `cron-v1` job | Operational |
| N3 | Reset `last_reminded` on appointment 259 (guarded) | Data (`fixes.sql`) |

Reproduce the fixes on a copy:
```bash
cp incident.db incident_fixed.db
sqlite3 incident_fixed.db < fixes.sql
```
