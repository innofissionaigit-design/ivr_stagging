"""One-shot migration: bring an existing clinic.db up to the schema the
written-confirmation work needs. Run it by hand.

    cd clinic-api && python migrate_notifications.py            # do it
    cd clinic-api && python migrate_notifications.py --dry-run  # look first

WHY THIS IS A SCRIPT AND NOT A STARTUP STEP
-------------------------------------------
main.py's startup already refuses to reseed a non-empty database, on the
grounds that an unconditional destructive step at boot "would wipe every
appointment booked since the last boot, turning a convenience into data
loss". This migration rebuilds the `appointments` table, which is that same
hazard aimed at the one table whose contents cannot be regenerated. It runs
when a person decides it runs.

WHY A REBUILD AND NOT THREE ALTERs
----------------------------------
Two of the three changes are plain column additions and ALTER TABLE handles
them. The third is not: the unique constraint has to go from

    UNIQUE (doctor_id, date, time_slot)
to
    UNIQUE (doctor_id, date, time_slot, slot_lock)

and SQLite implements a table-level UNIQUE as an internal auto-index that
cannot be dropped or altered. The only way to change it is the standard
twelve-step dance from SQLite's own ALTER TABLE documentation: create the
new table, copy the rows, drop the old, rename. Doing it in one transaction
means an interruption leaves the original table untouched.

Without that constraint change, cancellation is broken in a way that is
invisible until it bites: a cancelled row keeps occupying its slot, so the
slot it released can never be booked by anyone again.

WHAT IT DOES TO EXISTING ROWS
-----------------------------
Every existing appointment is treated as live: status="booked",
slot_lock="ACTIVE", updated_at=NULL. That is correct by construction --
before this change there was no way to cancel an appointment, so no
existing row can be a cancellation.

NO NOTIFICATION ROWS ARE BACKFILLED. Appointments booked before this
change were genuinely never messaged, and inventing ledger rows saying
otherwise would put a lie in the delivery record. They simply have no
history, which is the truth.
"""
from __future__ import annotations

import os
import shutil
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import inspect, text          # noqa: E402

from db import DATABASE_URL, engine           # noqa: E402
from models import Base                       # noqa: E402

NEW_COLUMNS = ("status", "slot_lock", "updated_at")


def _backup_sqlite() -> str | None:
    """Copy the database file next to itself before touching it.

    Only possible for SQLite, which is the default and the only backend
    this deployment actually runs (see db.py). A Postgres deployment is
    expected to have its own backup story, and the script says so rather
    than pretending it took one.
    """
    if not DATABASE_URL.startswith("sqlite"):
        print("! not SQLite -- no backup taken. Take your own before continuing.")
        return None
    path = DATABASE_URL.split("///", 1)[-1]
    if not os.path.exists(path):
        print(f"  no database file at {path} -- nothing to back up")
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.pre-notifications.{stamp}"
    shutil.copy2(path, dest)
    print(f"  backup: {dest}")
    return dest


def main(dry_run: bool = False) -> int:
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if "appointments" not in tables:
        print("appointments table does not exist yet -- nothing to migrate.")
        print("Start clinic-api normally; create_all() will build the new schema.")
        return 0

    columns = {c["name"] for c in insp.get_columns("appointments")}
    missing = [c for c in NEW_COLUMNS if c not in columns]
    needs_ledger = "notification_attempts" not in tables

    print(f"database: {DATABASE_URL}")
    print(f"appointments columns missing: {missing or 'none'}")
    print(f"notification_attempts table:  {'MISSING' if needs_ledger else 'present'}")

    if not missing and not needs_ledger:
        print("\nAlready migrated. Nothing to do.")
        return 0

    with engine.connect() as conn:
        row_count = conn.execute(text("SELECT COUNT(*) FROM appointments")).scalar_one()
    print(f"appointments to carry over:   {row_count}")

    if dry_run:
        print("\n--dry-run: stopping before any change.")
        return 0

    print("\nmigrating...")
    _backup_sqlite()

    # The ledger is a brand-new table, so create_all is enough for it and
    # touches nothing that already exists.
    Base.metadata.create_all(engine)

    if missing:
        with engine.begin() as conn:
            # Sequenced exactly as SQLite's ALTER TABLE docs prescribe.
            # engine.begin() wraps it in one transaction: an interruption
            # anywhere below leaves the original appointments table intact.
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("""
                CREATE TABLE appointments_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    confirmation_id VARCHAR NOT NULL UNIQUE,
                    doctor_id INTEGER NOT NULL REFERENCES doctors (id),
                    date VARCHAR NOT NULL,
                    time_slot VARCHAR NOT NULL,
                    patient_name VARCHAR NOT NULL,
                    phone VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'booked',
                    slot_lock VARCHAR NOT NULL DEFAULT 'ACTIVE',
                    updated_at DATETIME,
                    CONSTRAINT uq_doctor_slot UNIQUE (doctor_id, date, time_slot, slot_lock)
                )
            """))
            conn.execute(text("""
                INSERT INTO appointments_new
                    (id, confirmation_id, doctor_id, date, time_slot,
                     patient_name, phone, created_at, status, slot_lock, updated_at)
                SELECT id, confirmation_id, doctor_id, date, time_slot,
                       patient_name, phone, created_at, 'booked', 'ACTIVE', NULL
                FROM appointments
            """))
            conn.execute(text("DROP TABLE appointments"))
            conn.execute(text("ALTER TABLE appointments_new RENAME TO appointments"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
        print(f"  appointments rebuilt with {', '.join(missing)} and the 4-column constraint")

    if needs_ledger:
        print("  notification_attempts created")

    with engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM appointments")).scalar_one()
    print(f"\ndone. appointments before={row_count} after={after}")
    if after != row_count:
        print("!! ROW COUNT CHANGED -- restore the backup and investigate before serving.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
