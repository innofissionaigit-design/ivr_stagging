"""Clinical golden set: deterministic behaviour the agent must not drift from.

Author: Chakravardhan

Two layers of this agent decide what a caller hears without a model in the
loop, and both are pure functions of their input:

  * the fast path (agent/fast_path.py) -- which utterances are answered
    WITHOUT the LLM, with which test or doctor, and which it abstains on;
  * the reply templates (agent/reply_templates.py) -- the exact sentence a
    caller hears for a given backend answer, in Bengali, Hindi and English.

The golden set pins both. A change that makes the fast path claim an
utterance it used to abstain on, or alters what a caller is told when a test
is not found, fails tests/test_gate_golden_set.py and must be reviewed as a
clinical behaviour change -- the golden file is gate configuration
(tests/golden/*), so regenerating it sets gate_config_touched.

The catalogue is read from clinic-api/seed.py by parsing its literals, never
by importing it, so no database is touched.

    python scripts/gate_golden.py --write    # regenerate (a gate-config change)
    python scripts/gate_golden.py --check    # exit 1 on any drift
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "tests" / "golden" / "golden_set.json"
TODAY = datetime.date(2026, 9, 14)
LANGS = ("bn", "hi", "en")
TRILINGUAL_ENV = {
    "VOICE_AGENT_LANGUAGES": "bn,hi,en",
    "VOICE_AGENT_NEMO_FILE_HI": "/fake/hi.nemo",
    "VOICE_AGENT_NEMO_FILE_EN": "/fake/en.nemo",
}

# (case id, utterance, category). "abstention" means the fast path MUST hand
# the turn to the LLM rather than answer it itself -- the safety property is
# that anything non-routine, or anything carrying PII, never gets a
# string-matched answer.
FAST_PATH_CASES = [
    ("fp-rate-cbc", "সিবিসি টেস্টের রেট কত", "routine"),
    ("fp-rate-lipid", "লিপিড প্রোফাইলের দাম কত", "routine"),
    ("fp-rate-cholesterol", "কোলেস্টেরল টেস্ট করাতে কত টাকা লাগে", "routine"),
    ("fp-rate-thyroid", "থাইরয়েড টেস্টের খরচ কত", "routine"),
    ("fp-rate-hepatitis-b", "হেপাটাইটিস বি টেস্টের দাম কত", "routine"),
    ("fp-rate-ecg", "ইসিজি করাতে কত পড়বে", "routine"),
    ("fp-avail-sen-today", "ডাক্তার সেন আজ বসবেন", "routine"),
    ("fp-avail-ghosh-tomorrow", "ডক্টর ঘোষ কাল চেম্বারে থাকবেন", "routine"),
    ("fp-greeting", "নমস্কার", "routine"),
    ("fp-thanks", "ধন্যবাদ", "routine"),
    ("fp-abstain-booking-pii", "ডাক্তার সেনের অ্যাপয়েন্টমেন্ট বুক করতে চাই", "abstention"),
    ("fp-abstain-two-questions", "সিবিসির দাম আর ডাক্তার সেন কবে বসেন", "abstention"),
    ("fp-abstain-weekday-date", "ডাক্তার রায় সোমবার বসবেন", "abstention"),
    ("fp-abstain-explicit-date", "ডাক্তার সেন ১৫ তারিখে বসবেন", "abstention"),
    ("fp-abstain-unknown-test", "জিনোম সিকোয়েন্সিং টেস্টের রেট কত", "abstention"),
    ("fp-abstain-unknown-doctor", "ডাক্তার ঠাকুর কবে বসেন", "abstention"),
    ("fp-abstain-negation", "সিবিসি না অন্য টেস্টের দাম কত", "abstention"),
    ("fp-abstain-empty", "   ", "abstention"),
    ("fp-abstain-history", "আমার আগের টেস্টগুলো কী কী", "abstention"),
    ("fp-abstain-payment-method", "টাকা কীভাবে দেব", "abstention"),
]

_BOOKED = {
    "doctor_name": "সেন",
    "date": "2026-09-14",
    "time_slot": "18:15",
    "patient_name": "Riya Das",
    "phone": "9000000001",
}
_CBC_FOUND = {
    "found": True,
    "test_name": "Complete Blood Count (CBC)",
    "test_name_bn": "সিবিসি",
    "rate_inr": 400,
    "sample_type": "Blood",
    "report_time_hours": 6,
}
_HISTORY_TWO = {
    "found": True,
    "tests": [
        {
            "test_name": "Lipid Profile",
            "test_name_bn": "লিপিড প্রোফাইল",
            "taken_on": "2026-08-01",
            "report_ready": True,
        },
        {"test_name": "TSH", "test_name_bn": "টিএসএইচ", "taken_on": "2026-09-01", "report_ready": False},
    ],
}
_HISTORY_FIVE = {
    "found": True,
    "tests": [
        {
            "test_name": f"Test {i}",
            "test_name_bn": f"টেস্ট {i}",
            "taken_on": f"2026-0{i}-01",
            "report_ready": i % 2 == 0,
        }
        for i in range(1, 6)
    ],
}

# (case id, reply function, positional args, keyword args, category)
REPLY_CASES = [
    ("rt-test-found", "test_rate_reply", [{"test_name": "সিবিসি"}, _CBC_FOUND], {}, "routine"),
    (
        "rt-test-not-found-suggest",
        "test_rate_reply",
        [
            {"test_name": "সিবিস"},
            {"found": False, "query": "সিবিস", "did_you_mean": ["Complete Blood Count (CBC)"]},
        ],
        {},
        "abstention",
    ),
    (
        "rt-test-not-found",
        "test_rate_reply",
        [{"test_name": "অজানা"}, {"found": False, "query": "অজানা", "did_you_mean": []}],
        {},
        "abstention",
    ),
    (
        "rt-doctor-available",
        "doctor_availability_reply",
        [
            {"doctor_name": "সেন", "date": "2026-09-14"},
            {
                "found": True,
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": "2026-09-14",
                "available": True,
                "chamber_hours": "18:00-20:00",
                "next_available_date": None,
            },
        ],
        {},
        "routine",
    ),
    (
        "rt-doctor-next-date",
        "doctor_availability_reply",
        [
            {"doctor_name": "সেন", "date": "2026-09-14"},
            {
                "found": True,
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": "2026-09-14",
                "available": False,
                "chamber_hours": None,
                "next_available_date": "2026-09-16",
            },
        ],
        {},
        "routine",
    ),
    (
        "rt-doctor-not-found",
        "doctor_availability_reply",
        [{"doctor_name": "নোবডি"}, {"found": False, "query": "নোবডি"}],
        {},
        "abstention",
    ),
    (
        "rt-booking-success-sms",
        "booking_reply",
        [
            _BOOKED,
            {
                "success": True,
                "confirmation_id": "KCD-20260914-0031",
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": "2026-09-14",
                "time_slot": "18:15",
                "notification": {"status": "queued"},
            },
        ],
        {},
        "routine",
    ),
    (
        "rt-booking-success-no-sms",
        "booking_reply",
        [
            _BOOKED,
            {
                "success": True,
                "confirmation_id": "KCD-20260914-0032",
                "doctor_name": "Dr. A. Sen",
                "doctor_name_bn": "সেন",
                "date": "2026-09-14",
                "time_slot": "18:15",
                "notification": {"status": "skipped"},
            },
        ],
        {},
        "routine",
    ),
    (
        "rt-booking-slot-taken",
        "booking_reply",
        [_BOOKED, {"success": False, "reason": "slot_taken", "alternative_slots": ["18:30", "18:45"]}],
        {},
        "routine",
    ),
    (
        "rt-booking-failed",
        "booking_reply",
        [_BOOKED, {"success": False, "reason": "doctor_not_found"}],
        {},
        "abstention",
    ),
    ("rt-payment-with-rate", "payment_reply", [{"test_name": "সিবিসি"}, _CBC_FOUND], {}, "routine"),
    ("rt-payment-no-lookup", "payment_reply", [{}, {}], {}, "routine"),
    ("rt-report-with-hours", "report_collection_reply", [{"test_name": "সিবিসি"}, _CBC_FOUND], {}, "routine"),
    ("rt-report-unknown", "report_collection_reply", [{}, {}], {}, "routine"),
    ("rt-counter", "counter_fallback", [], {}, "handoff"),
    ("rt-counter-hours", "counter_fallback", [], {"hours": "9am-8pm"}, "handoff"),
    ("rt-verify-pin", "verification_prompt", ["pin"], {}, "phi"),
    ("rt-verify-dob", "verification_prompt", ["dob"], {}, "phi"),
    ("rt-verify-retry", "verification_failed_reply", [False], {}, "phi"),
    ("rt-verify-exhausted", "verification_failed_reply", [True], {}, "handoff"),
    ("rt-verify-locked", "verification_locked_reply", [], {}, "handoff"),
    ("rt-disclosure-speakerphone", "disclosure_blocked_reply", ["speakerphone"], {}, "phi"),
    ("rt-disclosure-unknown-path", "disclosure_blocked_reply", ["path_unknown"], {}, "phi"),
    ("rt-disclosure-disabled", "disclosure_blocked_reply", ["disclosure_disabled"], {}, "handoff"),
    ("rt-history-two", "history_reply", [_HISTORY_TWO], {}, "phi"),
    ("rt-history-five", "history_reply", [_HISTORY_FIVE], {}, "phi"),
    ("rt-history-none", "history_reply", [{"found": True, "tests": []}], {}, "phi"),
    ("rt-ask-test-name", "missing_slot_prompt", ["test_rate", "test_name"], {}, "abstention"),
    ("rt-ask-doctor-name", "missing_slot_prompt", ["doctor_availability", "doctor_name"], {}, "abstention"),
    ("rt-ask-booking-date", "missing_slot_prompt", ["book_appointment", "date"], {}, "abstention"),
    ("rt-ask-booking-phone", "missing_slot_prompt", ["book_appointment", "phone"], {}, "abstention"),
    (
        "rt-department-list",
        "doctors_by_department_reply",
        [
            {"department": "অর্থো", "date": "2026-09-14"},
            {
                "found": True,
                "department": "Orthopaedics",
                "date": "2026-09-14",
                "doctors": [
                    {"name": "Dr. D. Das", "doctor_name_bn": "দাস", "chamber_hours": "10:00-12:00"},
                    {"name": "Dr. T. Bose", "doctor_name_bn": "বসু", "chamber_hours": "17:30-19:30"},
                ],
            },
        ],
        {},
        "routine",
    ),
    (
        "rt-department-none-today",
        "doctors_by_department_reply",
        [
            {"department": "অর্থো", "date": "2026-09-14"},
            {"found": True, "department": "Orthopaedics", "date": "2026-09-14", "doctors": []},
        ],
        {},
        "routine",
    ),
    (
        "rt-department-not-found",
        "doctors_by_department_reply",
        [{"department": "নেই"}, {"found": False, "query": "নেই"}],
        {},
        "abstention",
    ),
]


def seed_catalogue() -> dict:
    """The catalogue payload /api/v1/catalogue would serve, built from
    seed.py's literals."""
    tree = ast.parse((ROOT / "clinic-api" / "seed.py").read_text(encoding="utf-8"))
    consts = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("LAB_TESTS", "DEPARTMENTS", "SURNAME_BN")
        ):
            consts[node.targets[0].id] = ast.literal_eval(node.value)
    tests = [{"name": row[0], "aliases_bn": list(row[1])} for row in consts["LAB_TESTS"]]
    doctors = []
    for docs in consts["DEPARTMENTS"].values():
        for name, _quals in docs:
            surname = name.split()[-1]
            doctors.append(
                {"name": name, "surname": surname, "aliases_bn": consts["SURNAME_BN"].get(surname, [])}
            )
    return {"tests": tests, "doctors": doctors}


@contextlib.contextmanager
def trilingual():
    saved = {k: os.environ.get(k) for k in TRILINGUAL_ENV}
    os.environ.update(TRILINGUAL_ENV)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def compute() -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from agent import reply_templates
    from agent.fast_path import Catalogue, FastPath

    fast = FastPath(Catalogue(seed_catalogue()), today=TODAY)
    out = {"today": TODAY.isoformat(), "fast_path": {}, "replies": {}}
    for cid, utterance, category in FAST_PATH_CASES:
        hit = fast.resolve(utterance)
        out["fast_path"][cid] = {
            "utterance": utterance,
            "category": category,
            "expected": None
            if hit is None
            else {
                "intent": hit.intent,
                "slots": {k: v for k, v in hit.slots.items() if v is not None},
                "direct_reply_bn": hit.direct_reply_bn,
            },
        }
    with trilingual():
        for cid, fn, args, kwargs, category in REPLY_CASES:
            func = getattr(reply_templates, fn)
            out["replies"][cid] = {
                "fn": fn,
                "args": args,
                "kwargs": kwargs,
                "category": category,
                "expected": {lang: func(*args, **kwargs, lang=lang) for lang in LANGS},
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()
    current = compute()
    if args.write:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        sys.stdout.write(
            f"wrote {len(current['fast_path'])} fast-path and {len(current['replies'])} reply cases\n"
        )
        return 0
    stored = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    drift = [
        cid
        for section in ("fast_path", "replies")
        for cid in stored[section]
        if stored[section][cid]["expected"] != current[section].get(cid, {}).get("expected")
    ]
    sys.stdout.write(json.dumps({"drift": drift}) + "\n")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
