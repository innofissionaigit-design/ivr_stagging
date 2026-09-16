"""DB session setup.

Defaults to SQLite ON THE NETWORK VOLUME, not Postgres. That is a
deliberate reversal, and the reasoning is worth keeping:

Postgres cannot live on /workspace. It is a MooseFS mount that reports
every file as root:root no matter what it is chowned to, and Postgres
refuses to start unless its data directory is owned by the postgres user
-- a check with no override. So Postgres could only ever run on the pod's
LOCAL overlay filesystem, which RunPod wipes on every restart. The result
was absurd: the clinic database, the one component whose entire job is to
remember things, was the only component in the stack that could not
survive a reboot. It was lost to three separate restarts, each time
needing a reinstall and a reseed.

SQLite has no ownership model to violate -- it is one file. It sits on
/workspace and persists. For this workload the things Postgres is better
at simply do not apply: a 74-row read-mostly catalogue, one writer
process, no concurrent writers, no replication.

JOURNAL MODE IS NOW PROVIDER-DEPENDENT
--------------------------------------
This file used to pin DELETE journalling unconditionally, reasoning that
WAL needs a shared-memory index file alongside the database and that is
exactly the primitive a network filesystem is least reliable at.

That reasoning is correct -- on RunPod, where /workspace is a MooseFS
network mount. It does NOT hold on vast.ai, where /workspace is the
instance's own local disk (deploy/env.vast.sh spells this out: "An
instance's container filesystem IS the rented disk"). So the constraint was
being inherited by a platform that does not have it.

The cost of getting this wrong is not throughput, it is CONCURRENCY. Under
DELETE journalling a writer takes an EXCLUSIVE lock on the whole database,
so one caller booking an appointment blocks every other caller's price and
availability lookup for the duration of that write. At the busiest hour --
the most bookings, the most lookups -- that is precisely backwards. WAL
lets readers continue against the last committed snapshot while a write is
in flight, which is the property this workload actually needs.

So the default is now chosen per provider, and CLINIC_DB_JOURNAL_MODE
overrides it explicitly if the detection is ever wrong.

DATABASE_URL still overrides everything, so pointing this back at a real
Postgres for production is a one-line environment change.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

DEFAULT_SQLITE_PATH = os.environ.get("CLINIC_DB_PATH", "/workspace/clinic.db")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")

_is_sqlite = DATABASE_URL.startswith("sqlite")

# WAL wherever the database is on a local disk, DELETE on a network mount.
# See the module docstring. deploy/env.vast.sh exports VOICE_AGENT_PROVIDER.
_DEFAULT_JOURNAL_MODE = "WAL" if os.environ.get("VOICE_AGENT_PROVIDER") == "vast" else "DELETE"
JOURNAL_MODE = os.environ.get("CLINIC_DB_JOURNAL_MODE", _DEFAULT_JOURNAL_MODE).upper()

# 3s, not the previous 30s.
#
# A busy_timeout only decides how long SQLite waits before giving up. It
# does not decide how long the CALLER waits -- agent/tools_client.py gives
# up on the whole HTTP request after DEFAULT_TIMEOUT_S = 4.0 seconds. So a
# 30s busy_timeout could never actually be reached: the voice agent had
# already abandoned the lookup, spoken its "couldn't check that right now"
# apology and moved on, while this process sat holding a connection waiting
# on a lock for another 26 seconds on behalf of nobody.
#
# Sizing it UNDER the client's timeout means contention surfaces as a clean,
# fast SQLite error we can see and log, instead of as a client-side timeout
# whose cause is invisible from in here.
BUSY_TIMEOUT_S = float(os.environ.get("CLINIC_DB_BUSY_TIMEOUT_S", "3"))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # FastAPI serves requests from a thread pool, so the connection that
    # opens a session is not always the one that closes it.
    connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # Wait rather than raising "database is locked" the instant another
        # connection holds it -- lock handoff is measured in milliseconds.
        cur.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")

        # Set UNCONDITIONALLY, including for DELETE.
        #
        # journal_mode is a property of the FILE, not the connection, and it
        # persists. So "DELETE" is only the default for a database that has
        # never been opened in WAL -- once a file is WAL it stays WAL until
        # something explicitly changes it back. Skipping the pragma for
        # DELETE would therefore make CLINIC_DB_JOURNAL_MODE=DELETE a no-op
        # on exactly the database that most needs it: one already converted
        # to WAL that has to be rolled back. Always asserting it keeps this
        # setting authoritative in both directions.
        cur.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
        if JOURNAL_MODE == "WAL":
            # WAL defaults to synchronous=FULL, which fsyncs the WAL on every
            # commit and throws away most of what WAL is for. NORMAL is the
            # standard pairing: it cannot corrupt the database, it only risks
            # losing the last few commits if the machine loses power
            # mid-write. For a re-seedable clinic catalogue that is an easy
            # trade.
            cur.execute("PRAGMA synchronous=NORMAL")

        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
