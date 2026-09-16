"""Counter and operations commands for preparation reminders.

Author: Chakravardhan
Story:  "As a patient with a fasting test tomorrow, I want a reminder tonight,
         so that my visit is not wasted."

    cd clinic-api
    python reminders.py opt-out 9876543210 --source counter
    python reminders.py order-tests KCD-20260915-AB12 "Blood Sugar Fasting" "Lipid Profile"
    python reminders.py import-dnd /secure/path/ncpr_scrub.txt
    python reminders.py run-once
    python reminders.py status

WHY A COMMAND AND NOT AN ENDPOINT
---------------------------------
clinic-api's HTTP surface is pinned by scripts/gate-contracts/clinic-api.json;
adding an endpoint is an API-contract change that needs code-owner review.
These are staff operations, so they run here, against the same database and
the same service functions an endpoint would call.

OPT-OUT IS PERMANENT: there is deliberately no command to undo one.

The DND scrub file is one number per line, optionally followed by a comma and
the blocked preference categories ("9876543210,0" or "9876543210,1,4"). Blank
lines and lines starting with # are ignored. It holds phone numbers, so keep it
outside the repository.

Output never repeats a full phone number -- only its last four digits.
"""

from __future__ import annotations

import argparse
import sys

# Run as `python reminders.py` from clinic-api/, whose directory Python puts
# first on sys.path -- the same way main.py imports these modules.
import reminder_service
from db import SessionLocal
from models import Appointment, Base, LabTest


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")


def _masked(phone: str) -> str:
    number = reminder_service.normalize_phone(phone)
    return f"******{number[-4:]}" if number else "(none)"


def _read_scrub(path: str) -> list[tuple[str, str]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            number, _, preference = line.partition(",")
            rows.append((number, preference))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preparation reminder operations")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("opt-out", help="stop every reminder to a number, permanently")
    p.add_argument("phone")
    p.add_argument("--source", default="counter")

    p = sub.add_parser("order-tests", help="attach tests to an appointment")
    p.add_argument("confirmation_id")
    p.add_argument("tests", nargs="+")

    p = sub.add_parser("import-dnd", help="replace the DND registry with a scrub file")
    p.add_argument("path")

    sub.add_parser("run-once", help="plan and send due reminders now")
    sub.add_parser("status", help="counts, quiet hours, DND freshness")

    args = parser.parse_args(argv)
    settings = reminder_service.Settings.from_env()
    now = reminder_service.local_now(settings)

    if args.command == "run-once":
        from db import engine

        Base.metadata.create_all(engine)
        _say(str(reminder_service.tick(now=now, settings=settings)))
        return 0

    db = SessionLocal()
    try:
        Base.metadata.create_all(db.get_bind())
        if args.command == "opt-out":
            try:
                reminder_service.opt_out(db, args.phone, args.source, now)
            except ValueError as e:
                _say(f"not recorded: {e}")
                return 2
            db.commit()
            _say(f"opted out {_masked(args.phone)} -- no reminder will be sent to this number again")
        elif args.command == "order-tests":
            if db.query(Appointment).filter_by(confirmation_id=args.confirmation_id).first() is None:
                _say(f"no appointment {args.confirmation_id}")
                return 2
            known = {t.name for t in db.query(LabTest).all()}
            unknown = [t for t in args.tests if t not in known]
            if unknown:
                _say(f"not in the catalogue: {', '.join(unknown)}")
                return 2
            added = reminder_service.order_tests(db, args.confirmation_id, args.tests, now)
            db.commit()
            _say(f"attached {len(added)} test(s) to {args.confirmation_id}")
        elif args.command == "import-dnd":
            count = reminder_service.import_dnd(db, _read_scrub(args.path), now)
            db.commit()
            _say(f"DND registry replaced: {count} registered number(s)")
        elif args.command == "status":
            _say(str(reminder_service.summary(db, settings)))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
