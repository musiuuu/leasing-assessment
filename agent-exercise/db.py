"""SQLite data-access layer for the leasing agent.

Pure data access: every function opens a short-lived connection, runs
parameterised SQL, and returns plain dicts. No business rules and no
price formatting live here -- the tool layer in app.py owns those, so the
enforcement points stay in one place.

`rent` is returned exactly as stored: an int, or None when the price is not
on file. Callers must handle None explicitly (never invent a number).
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listings.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def search_units(city=None, max_rent=None, min_beds=None, limit=25):
    """Active units matching the filters, with building name + address.

    Important grounding rule: a unit whose rent is NULL is *included* in the
    results even when `max_rent` is set. A missing price cannot be excluded by
    a price filter -- we surface it (rent=None) instead of silently hiding it.
    Priced units are filtered by `max_rent` as normal.
    """
    where = ["u.is_active = 1"]
    params = []
    if city:
        where.append("LOWER(b.city) = LOWER(?)")
        params.append(city)
    if min_beds is not None:
        where.append("u.beds >= ?")
        params.append(min_beds)
    if max_rent is not None:
        # Keep priced units within budget, but never drop NULL-rent units.
        where.append("(u.rent IS NULL OR u.rent <= ?)")
        params.append(max_rent)

    sql = (
        "SELECT u.id, u.unit_number, u.beds, u.baths, u.rent, u.available_from, "
        "       b.name AS building_name, b.address, b.city, b.state, b.zip "
        "FROM units u JOIN buildings b ON u.building_id = b.id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY (u.rent IS NULL), u.rent, u.id LIMIT ?"
    )
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_unit_details(unit_id):
    """Everything about one unit (any status), or None if it doesn't exist."""
    sql = (
        "SELECT u.id, u.unit_number, u.beds, u.baths, u.rent, u.available_from, "
        "       u.is_active, b.name AS building_name, b.address, b.city, "
        "       b.state, b.zip "
        "FROM units u JOIN buildings b ON u.building_id = b.id "
        "WHERE u.id = ?"
    )
    with _connect() as conn:
        row = conn.execute(sql, (unit_id,)).fetchone()
        return dict(row) if row else None


def get_unit_raw(unit_id):
    """Minimal row used by the code-side tour validation (id, active, rent)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, is_active, rent FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        return dict(row) if row else None


def insert_tour(unit_id, tour_at, client_name, created_at):
    """Insert a booked tour and return the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tours (unit_id, tour_at, client_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (unit_id, tour_at, client_name, created_at),
        )
        conn.commit()
        return cur.lastrowid
