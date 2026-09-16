"""
SQLAlchemy models for the clinic's dummy PostgreSQL data.

Prototype-grade on purpose: this exists so the voice agent can call
a realistic backend while being bench-tested.

All data is fictional and intended only for testing.

======================================================================
CHATGPT ADDITION NOTE
======================================================================


1. Add clinic opening and closing time.
2. Add clinic address and directions.
3. Add health packages.
4. Add patient details such as name and phone number.
5. Add patient-specific laboratory reports.
6. Support testing whether a patient's report is ready or not ready.
7. Support report-not-found cases.
8. Add OTP verification for sending reports.
9. Add report delivery tracking and audit information.
10. Add expiring signed-link information instead of permanent attachments.
11. Support English, Hinglish/Banglish and Bengali-script caller inputs.
12. Add enough structure to test edge cases for:
       - report ready
       - report not ready
       - report not found
       - wrong patient
       - wrong phone
       - wrong OTP
       - expired OTP
       - expired report link
       - failed delivery
       - successful delivery
       - repeated delivery attempts
13. Keep the existing doctor, department, schedule, laboratory and
    appointment functionality intact.

Created for Sourav.
======================================================================
"""

from __future__ import annotations

import secrets

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    ForeignKey,
    DateTime,
    UniqueConstraint,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


# ============================================================================
# CLINIC INFORMATION
# ============================================================================
#
# Used for queries such as:
#
# English:
#   "When do you open?"
#   "What time does the clinic close?"
#   "Where is the clinic?"
#   "Give me directions."
#
# Hinglish / Banglish:
#   "Clinic kab khulta hai?"
#   "Clinic koto khon porjonto open thake?"
#   "Address ta ki?"
#   "Kivabe jabo?"
#
# Bengali script:
#   "ক্লিনিক কখন খোলে?"
#   "ক্লিনিক কখন বন্ধ হয়?"
#   "ঠিকানাটা কী?"
#
# CHATGPT ADDITION - CREATED BY SOURAV:
# This section was added to make clinic-information intents testable
# without hardcoding the information inside the voice agent.
# ============================================================================


class ClinicInfo(Base):
    __tablename__ = "clinic_info"

    id = Column(Integer, primary_key=True)

    clinic_name = Column(String, nullable=False)

    # Main contact number of the clinic.
    phone = Column(String, nullable=False)

    # Full physical address.
    address = Column(Text, nullable=False)

    # Simple landmark / directions text.
    directions = Column(Text, nullable=False)

    # Weekly opening and closing times.
    #
    # Example:
    # Monday = 09:00 - 21:00
    #
    # Stored as strings intentionally for prototype simplicity.
    monday_open = Column(String, nullable=False, default="09:00")
    monday_close = Column(String, nullable=False, default="21:00")

    tuesday_open = Column(String, nullable=False, default="09:00")
    tuesday_close = Column(String, nullable=False, default="21:00")

    wednesday_open = Column(String, nullable=False, default="09:00")
    wednesday_close = Column(String, nullable=False, default="21:00")

    thursday_open = Column(String, nullable=False, default="09:00")
    thursday_close = Column(String, nullable=False, default="21:00")

    friday_open = Column(String, nullable=False, default="09:00")
    friday_close = Column(String, nullable=False, default="21:00")

    saturday_open = Column(String, nullable=False, default="09:00")
    saturday_close = Column(String, nullable=False, default="21:00")

    # Nullable because Sunday can be closed.
    sunday_open = Column(String, nullable=True)
    sunday_close = Column(String, nullable=True)

    sunday_closed = Column(Boolean, nullable=False, default=True)


# ============================================================================
# HEALTH PACKAGES
# ============================================================================
#
# Used for:
#
#   "What health packages do you have?"
#   "Diabetes package ache?"
#   "Heart checkup package koto?"
#   "General health package e ki ki test ache?"
#
# CHATGPT ADDITION - CREATED BY SOURAV:
# Added so package-related calls can be tested against database data
# instead of hardcoded responses.
# ============================================================================


class HealthPackage(Base):
    __tablename__ = "health_packages"

    id = Column(Integer, primary_key=True)

    name = Column(String, nullable=False, unique=True)

    # Bengali / Banglish / English aliases.
    #
    # Example:
    # "diabetes checkup|diabetes package|ডায়াবেটিস প্যাকেজ"
    aliases = Column(String, nullable=False, default="")

    description = Column(Text, nullable=False)

    price_inr = Column(Float, nullable=False)

    active = Column(Boolean, nullable=False, default=True)

    tests = relationship(
        "HealthPackageTest",
        back_populates="package",
        cascade="all, delete-orphan",
    )


class HealthPackageTest(Base):
    __tablename__ = "health_package_tests"

    id = Column(Integer, primary_key=True)

    package_id = Column(
        Integer,
        ForeignKey("health_packages.id"),
        nullable=False,
    )

    lab_test_id = Column(
        Integer,
        ForeignKey("lab_tests.id"),
        nullable=False,
    )

    package = relationship(
        "HealthPackage",
        back_populates="tests",
    )

    lab_test = relationship("LabTest")

    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "lab_test_id",
            name="uq_package_test",
        ),
    )


# ============================================================================
# DEPARTMENT
# ============================================================================


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True)

    name = Column(String, nullable=False, unique=True)

    # English + Bengali-script + Banglish aliases.
    #
    # Example:
    # "ortho|orthopedics|অর্থোপেডিক্স|অর্থো|bone"
    aliases_bn = Column(
        String,
        nullable=False,
        default="",
    )

    doctors = relationship(
        "Doctor",
        back_populates="department",
    )


# ============================================================================
# DOCTOR
# ============================================================================


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True)

    name = Column(
        String,
        nullable=False,
    )

    qualifications = Column(
        String,
        nullable=False,
    )

    # Bengali-script and spoken aliases.
    #
    # Example:
    # "সেন|ডক্টর সেন|Dr Sen|doctor sen"
    aliases_bn = Column(
        String,
        nullable=False,
        default="",
    )

    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
    )

    department = relationship(
        "Department",
        back_populates="doctors",
    )

    schedule = relationship(
        "DoctorSchedule",
        back_populates="doctor",
        cascade="all, delete-orphan",
    )


# ============================================================================
# DOCTOR SCHEDULE
# ============================================================================


class DoctorSchedule(Base):
    """
    One row per weekday a doctor sits.

    weekday:
        0 = Monday
        1 = Tuesday
        ...
        6 = Sunday
    """

    __tablename__ = "doctor_schedule"

    id = Column(Integer, primary_key=True)

    doctor_id = Column(
        Integer,
        ForeignKey("doctors.id"),
        nullable=False,
    )

    weekday = Column(
        Integer,
        nullable=False,
    )

    start_time = Column(
        String,
        nullable=False,
    )

    end_time = Column(
        String,
        nullable=False,
    )

    doctor = relationship(
        "Doctor",
        back_populates="schedule",
    )

    __table_args__ = (
        UniqueConstraint(
            "doctor_id",
            "weekday",
            name="uq_doctor_weekday",
        ),
    )


# ============================================================================
# LAB TEST
# ============================================================================


class LabTest(Base):
    __tablename__ = "lab_tests"

    id = Column(Integer, primary_key=True)

    name = Column(
        String,
        nullable=False,
        unique=True,
    )

    # English + Bengali-script + Banglish aliases.
    #
    # Example:
    # "cbc|সি বি সি|সিবিসি|complete blood count"
    aliases_bn = Column(
        String,
        nullable=False,
        default="",
    )

    rate_inr = Column(
        Float,
        nullable=False,
    )

    sample_type = Column(
        String,
        nullable=False,
    )

    report_time_hours = Column(
        Integer,
        nullable=False,
    )

    # ========================================================================
    # ADDED BY SOURAV -- "Caller asks how to prepare for a test" story.
    #
    # Every column below is nullable with NO default, on purpose: only the
    # tests the business has actually supplied real preparation content
    # for (see clinic-api/seed.py's LAB_TEST_ADVISORIES) ever get these
    # filled in. Every other LabTest row leaves them all as None.
    #
    # This is a deliberate safety choice, not an oversight. Defaulting
    # fasting_required to False for a test nobody has actually reviewed
    # would be FABRICATING a medical instruction -- "no fasting needed"
    # is not a safe guess, it is a specific claim that could be wrong.
    # Same discipline as RULE 1 elsewhere in this file ("never invent a
    # report"), applied here to something with real physical stakes if
    # gotten wrong: the API layer and the voice agent must both treat
    # "no advisory row" as "we don't know yet, don't answer", never as
    # "assume no restrictions".
    # ========================================================================

    # Whether the caller must fast before this test. None = not seeded
    # yet (see above) -- never treat as False.
    fasting_required = Column(Boolean, nullable=True)

    # Free text, not an int: real fasting windows are given as ranges
    # ("8-12 hours"), not single numbers.
    fasting_hours = Column(String, nullable=True)

    # What the caller may drink while fasting/preparing (e.g. "Only plain
    # water permitted during fasting period").
    water_allowance = Column(String, nullable=True)

    # Which medications (if any) to hold and until when (e.g. "Hold
    # morning anti-diabetic medication until after blood collection").
    medication_hold = Column(Text, nullable=True)

    # Any other timing constraint on the test itself (e.g. "Exactly 2
    # hours post-meal", "Morning sample collection preferred").
    timing_rule = Column(String, nullable=True)

    # Ready-to-speak, business-authored advisory sentences, one per
    # supported language, each containing a literal "{test_name}"
    # placeholder the reply layer fills in at speak-time (see
    # agent/reply_templates.py's test_preparation_reply() -- same
    # canonical-name-vs-Bengali-alias selection every other reply in this
    # codebase already uses). Stored as real per-language text rather
    # than composed from the structured fields above, because the
    # business supplied genuine, reviewed Bengali/Hinglish/Banglish
    # phrasing for these -- unlike ClinicInfo.address/directions or
    # HealthPackage.description, this is NOT the English-only gap
    # flagged elsewhere in this codebase (see those models' own
    # comments); recomposing it programmatically from the structured
    # fields would only risk mangling wording the business already
    # approved.
    advisory_script_en = Column(Text, nullable=True)
    advisory_script_hinglish = Column(Text, nullable=True)
    advisory_script_banglish = Column(Text, nullable=True)
    advisory_script_bn = Column(Text, nullable=True)

    # ========================================================================
    # ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Walk-in
    # Eligibility + Prescription Requirements stories.
    #
    # Same discipline as the advisory columns just above, extended to two
    # more regulatory/operational facts: every column below is nullable
    # with NO default. None means "nobody has reviewed this test for
    # walk-in/prescription policy yet" -- never treat that as "eligible"
    # or "not required". Defaulting walkin_eligible to True, or
    # prescription_required to False, for a test nobody actually
    # confirmed would be fabricating an operational/regulatory fact
    # exactly like guessing a fasting rule would be a medical one -- see
    # this class's own comment above for the identical reasoning.
    # ========================================================================

    # Whether a caller can walk in for this test without a prior
    # appointment. None = not reviewed yet, never treat as False.
    walkin_eligible = Column(Boolean, nullable=True)

    # Free text, not structured hours: real walk-in windows are given as
    # day-specific ranges ("Mon-Sat 7am-11am, no walk-ins Sunday"), not a
    # single machine-parseable value -- same reasoning as fasting_hours
    # above being a string, not an int.
    walkin_hours = Column(String, nullable=True)

    # Whether this test requires a doctor's prescription before it can be
    # performed. None = not reviewed yet, never treat as False.
    prescription_required = Column(Boolean, nullable=True)

    # Pipe-delimited list of accepted submission channels for the
    # prescription (e.g. "whatsapp_photo|email|counter_in_person") --
    # same delimited-list convention as aliases_bn above and
    # HealthPackage.aliases below, not a JSON column (nothing else in
    # this file uses one). Split on "|" at the API layer, same as
    # aliases_bn is split on "|" wherever it's read.
    prescription_channels = Column(String, nullable=True)


# ============================================================================
# INSURANCE PROVIDER / INSURANCE POLICY
# ============================================================================
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Insurance
# Coverage Policy story.
#
# A caller asks "does my Star Health cover this test" by naming their
# insurer OUT LOUD, the same way they name a test or a doctor -- so
# InsuranceProvider gets the same name+aliases voice-matching shape as
# LabTest.aliases_bn / Doctor.aliases_bn / HealthPackage.aliases, instead
# of being a bare fixed string/enum on the policy row. InsurancePolicy
# itself is a (test, provider) pair, per the plan: one row is "does
# provider X cover test Y", never a whole-provider blanket answer --
# a real insurer's coverage differs test-by-test.
# ============================================================================


class InsuranceProvider(Base):
    __tablename__ = "insurance_providers"

    id = Column(Integer, primary_key=True)

    name = Column(String, nullable=False, unique=True)

    # English + Bengali-script + Hinglish/Banglish aliases, same "|"-joined
    # convention as LabTest.aliases_bn.
    #
    # Example:
    # "star health|স্টার হেলথ|star"
    aliases = Column(String, nullable=False, default="")

    active = Column(Boolean, nullable=False, default=True)

    policies = relationship(
        "InsurancePolicy",
        back_populates="provider",
        cascade="all, delete-orphan",
    )


class InsurancePolicy(Base):
    __tablename__ = "insurance_policies"

    id = Column(Integer, primary_key=True)

    test_id = Column(
        Integer,
        ForeignKey("lab_tests.id"),
        nullable=False,
    )

    provider_id = Column(
        Integer,
        ForeignKey("insurance_providers.id"),
        nullable=False,
    )

    # COVERED / NOT_COVERED / PARTIAL. Nullable with no default, same
    # reasoning as LabTest's advisory/walk-in/prescription columns: no
    # row for a (test, provider) pair means "we don't know", never a
    # guessed COVERED or NOT_COVERED.
    coverage_status = Column(String, nullable=True)

    pre_auth_required = Column(Boolean, nullable=True)

    lab_test = relationship("LabTest")

    provider = relationship(
        "InsuranceProvider",
        back_populates="policies",
    )

    __table_args__ = (
        UniqueConstraint(
            "test_id",
            "provider_id",
            name="uq_test_provider_policy",
        ),
    )


# ============================================================================
# PATIENT
# ============================================================================
#
# This is one of the most important additions.
#
# The old database had patient_name and phone directly inside Appointment.
#
# That is not enough for report-related testing because the system needs
# to identify the SAME patient across multiple reports and conversations.
#
# CHATGPT ADDITION - CREATED BY SOURAV:
# Added patient identity, phone, language preference and spoken-name aliases
# so the agent can test patient-specific report queries.
# ============================================================================


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)

    # Patient's canonical name.
    name = Column(
        String,
        nullable=False,
    )

    # Alternate names / spellings.
    #
    # Example:
    # "Sourav Upadhyay|Sourav|সৌরভ|সৌরভ উপাধ্যায়"
    #
    # This helps test ASR variations.
    name_aliases = Column(
        String,
        nullable=False,
        default="",
    )

    # Phone number used for patient verification and report delivery.
    phone = Column(
        String,
        nullable=False,
        unique=True,
    )

    # Optional secondary phone.
    alternate_phone = Column(
        String,
        nullable=True,
    )

    # Preferred caller language.
    #
    # Supported test values:
    #   english
    #   hinglish
    #   banglish
    #   bengali
    language = Column(
        String,
        nullable=False,
        default="english",
    )

    date_of_birth = Column(
        String,
        nullable=True,
    )

    gender = Column(
        String,
        nullable=True,
    )

    active = Column(
        Boolean,
        nullable=False,
        default=True,
    )

    # ADDED BY CHAKRAVARDHAN -- "History disclosed only after verification"
    # story. This branch independently defined its OWN second `class
    # Patient(Base)` with `__tablename__ = "patients"` further down this
    # file (see the "PATIENT IDENTITY AND HISTORY" section, where
    # TestRecord/DisclosureAudit now live) -- two SQLAlchemy model classes
    # mapping the same table name is not something git's line-based merge
    # can see as a conflict (the two class bodies never touch the same
    # lines), but it is a real one: SQLAlchemy would refuse to map both,
    # and whichever "Patient" name Python bound last would silently shadow
    # the other everywhere `reports`/`appointments` back_populates and
    # every phone-lookup elsewhere in this file expect it. Folded in here
    # instead, onto the one Patient class that LabReport.patient and
    # Appointment.patient already back_populate against.
    #
    # `full_name` is kept as its own column, separate from `name` above,
    # rather than renamed to it: clinic-api/verification.py and
    # clinic-api/history_service.py (both unconflicted, outside this
    # merge's 15 touched files) were not inspected here and may reference
    # `full_name` specifically. Consolidating the two into one name field
    # is a real follow-up, not a change to make blind in a merge.
    full_name = Column(String, nullable=True)

    # PBKDF2 of a 4-digit PIN, set at the counter. NEVER the PIN itself.
    # See the (former) standalone Patient class's docstring, ported to
    # this class's own docstring is future work -- the short version: an
    # SMS OTP proves possession of the shared handset, not identity, so
    # verification here is knowledge-based (PIN set in person, or DOB).
    pin_hash = Column(String, nullable=True)
    pin_salt = Column(String, nullable=True)
    pin_set_at = Column(DateTime, nullable=True)

    # Lockout state, counted PER PATIENT (not per call), so hanging up and
    # redialling does not reset an attacker's attempt budget.
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_verified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=True)

    reports = relationship(
        "LabReport",
        back_populates="patient",
        cascade="all, delete-orphan",
    )

    appointments = relationship(
        "Appointment",
        back_populates="patient",
    )


# ============================================================================
# PATIENT BILLING
# ============================================================================
#
# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Outstanding
# Balance / Billing story.
#
# One row per patient (per the plan's schema), not itemized per invoice --
# a running "what do they currently owe" total, mirroring how a caller
# actually asks ("do I have any dues pending"), not an itemized statement.
# NO ROW for a patient is a real, distinct outcome from a row with
# outstanding_amount=0.0: the former means "we have no billing record for
# this patient at all" (honest not-found, same as LabTest's advisory
# columns being None), the latter is a real, reviewed "confirmed zero
# balance". Never create a row just to fill it with a guessed 0.
#
# Identity is resolved by PHONE (patient_id, via Patient.phone), same
# RULE 14/15 discipline as reports -- never by name. Per an explicit
# scoping decision for this first pass: a balance is spoken after a plain
# phone-based lookup, the same friction level as report_status, NOT
# gated behind OTP verification the way report delivery is. Revisit this
# if it should be tightened later -- it was a deliberate choice, not an
# oversight.
# ============================================================================


class PatientBilling(Base):
    __tablename__ = "patient_billing"

    id = Column(Integer, primary_key=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        unique=True,
    )

    # None = no billing record for this patient (see class docstring
    # above) -- never treat as 0.
    outstanding_amount = Column(Float, nullable=True)

    due_date = Column(DateTime, nullable=True)

    # When this row was last reviewed/updated -- lets a future story
    # answer "how current is this" without guessing.
    updated_at = Column(DateTime, nullable=True)

    patient = relationship("Patient")


# ============================================================================
# LAB REPORT
# ============================================================================
#
# This directly supports:
#
#   "Is my report ready?"
#   "Amar report ready?"
#   "Amar report ta ready hoyeche?"
#   "Report ready na?"
#
# Important statuses:
#
#   READY
#   NOT_READY
#   PROCESSING
#   CANCELLED
#
# A separate NOT_FOUND case happens when no report exists for the
# requested patient/report/test.
#
# CHATGPT ADDITION - CREATED BY SOURAV:
# Added patient-specific report status so the voice agent can test
# ready vs not-ready vs missing-report outcomes.
# ============================================================================


class LabReport(Base):
    __tablename__ = "lab_reports"

    id = Column(Integer, primary_key=True)

    # Human-readable report identifier.
    #
    # Example:
    # LAB-2026-0001
    report_number = Column(
        String,
        nullable=False,
        unique=True,
    )

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
    )

    lab_test_id = Column(
        Integer,
        ForeignKey("lab_tests.id"),
        nullable=False,
    )

    # Sample collection date.
    collected_at = Column(
        DateTime,
        nullable=False,
    )

    # Expected completion time.
    expected_ready_at = Column(
        DateTime,
        nullable=False,
    )

    # Actual completion time.
    ready_at = Column(
        DateTime,
        nullable=True,
    )

    # READY / NOT_READY / PROCESSING / CANCELLED
    status = Column(
        String,
        nullable=False,
        default="PROCESSING",
    )

    # Optional reason when report is delayed.
    status_reason = Column(
        String,
        nullable=True,
    )

    # Whether the report can currently be delivered.
    delivery_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    # Version helps test situations where a report is regenerated.
    report_version = Column(
        Integer,
        nullable=False,
        default=1,
    )

    patient = relationship(
        "Patient",
        back_populates="reports",
    )

    lab_test = relationship(
        "LabTest",
    )

    deliveries = relationship(
        "ReportDelivery",
        back_populates="report",
        cascade="all, delete-orphan",
    )

    otp_verifications = relationship(
        "ReportOTP",
        back_populates="report",
        cascade="all, delete-orphan",
    )


# ============================================================================
# REPORT OTP
# ============================================================================
#
# Used for:
#
#   "Send my report"
#   "Report pathanor age OTP lagbe?"
#   "OTP is 123456"
#
# Testable states:
#
#   valid OTP
#   wrong OTP
#   expired OTP
#   already-used OTP
#   too many attempts
#
# NOTE:
# In a real production system the OTP should NOT be stored in plaintext.
# This prototype intentionally stores a dummy value so testing is easy.
#
# CHATGPT ADDITION - CREATED BY SOURAV.
# ============================================================================


# ADDED BY SOURAV -- the user's own explicit instruction: "the otp and
# other things will not be hardcoded". This replaces two previously
# hardcoded, guessable literals:
#   - clinic-api/main.py's own FRESH_OTP_CODE constant ("135790"), minted
#     every time request_report_delivery() has no reusable OTP row to
#     hand back out.
#   - clinic-api/seed.py's OTP_DATA list, which used to give every seeded
#     ReportOTP row ("482913", "615204", "903217", "731846") a fixed
#     literal too.
# Both call sites now call THIS function instead, so there is exactly one
# place in the whole codebase that decides what an OTP code looks like.
# `secrets.randbelow` (not `random`) because this is a real, security-
# relevant credential (RULE 9: it gates report delivery) even though this
# is a prototype -- see this class's own "Production should store a hash
# instead" comment just below for the next hardening step past this one.
def generate_otp_code() -> str:
    """A real random 6-digit OTP, zero-padded (e.g. "042913") -- never a
    fixed, predictable value. How this code actually reaches the patient
    is a separate, deliberately pluggable concern -- see
    clinic-api/otp_messaging_config.py, the one file a deploying company
    edits to connect this to their own SMS/WhatsApp/e-mail provider."""
    return f"{secrets.randbelow(1_000_000):06d}"


class ReportOTP(Base):
    __tablename__ = "report_otps"

    id = Column(Integer, primary_key=True)

    report_id = Column(
        Integer,
        ForeignKey("lab_reports.id"),
        nullable=False,
    )

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
    )

    # Phone number to which OTP was sent.
    phone = Column(
        String,
        nullable=False,
    )

    # Prototype-only OTP.
    #
    # Production should store a hash instead.
    otp_code = Column(
        String,
        nullable=False,
    )

    created_at = Column(
        DateTime,
        nullable=False,
    )

    expires_at = Column(
        DateTime,
        nullable=False,
    )

    verified_at = Column(
        DateTime,
        nullable=True,
    )

    used = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
    )

    max_attempts = Column(
        Integer,
        nullable=False,
        default=3,
    )

    report = relationship(
        "LabReport",
        back_populates="otp_verifications",
    )

    patient = relationship(
        "Patient",
    )


# ============================================================================
# REPORT DELIVERY
# ============================================================================
#
# This handles the second story:
#
#   "Send my report."
#
# The acceptance criteria require:
#
#   - OTP verification
#   - expiring signed link
#   - audit trail
#   - recipient
#   - verification path
#   - failed verification should offer collection in person
#
# CHATGPT ADDITION - CREATED BY SOURAV:
# Added explicit delivery state and signed-link expiry so the test system
# can intentionally create success and failure scenarios.
# ============================================================================


class ReportDelivery(Base):
    __tablename__ = "report_deliveries"

    id = Column(Integer, primary_key=True)

    report_id = Column(
        Integer,
        ForeignKey("lab_reports.id"),
        nullable=False,
    )

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
    )

    # Phone/email destination.
    recipient = Column(
        String,
        nullable=False,
    )

    # PHONE / EMAIL / WHATSAPP etc.
    delivery_channel = Column(
        String,
        nullable=False,
        default="SMS",
    )

    # OTP_REQUIRED / VERIFIED / FAILED / EXPIRED
    verification_status = Column(
        String,
        nullable=False,
        default="OTP_REQUIRED",
    )

    # PENDING / SENT / FAILED / EXPIRED
    delivery_status = Column(
        String,
        nullable=False,
        default="PENDING",
    )

    # Unique signed URL identifier.
    #
    # Do NOT use a real permanent URL in this prototype.
    signed_link_token = Column(
        String,
        nullable=True,
        unique=True,
    )

    # When the signed link becomes invalid.
    signed_link_expires_at = Column(
        DateTime,
        nullable=True,
    )

    # Number of times delivery was attempted.
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
    )

    created_at = Column(
        DateTime,
        nullable=False,
    )

    verified_at = Column(
        DateTime,
        nullable=True,
    )

    sent_at = Column(
        DateTime,
        nullable=True,
    )

    failed_at = Column(
        DateTime,
        nullable=True,
    )

    # Human-readable failure reason.
    #
    # Examples:
    # "WRONG_OTP"
    # "OTP_EXPIRED"
    # "LINK_EXPIRED"
    # "DELIVERY_PROVIDER_FAILURE"
    # "PHONE_MISMATCH"
    failure_reason = Column(
        String,
        nullable=True,
    )

    # Audit information.
    #
    # Example:
    # "OTP verified on registered phone ending 1234"
    audit_note = Column(
        Text,
        nullable=True,
    )

    report = relationship(
        "LabReport",
        back_populates="deliveries",
    )

    patient = relationship(
        "Patient",
    )


# ============================================================================
# APPOINTMENT
# ============================================================================

# Appointment.status values. Persisted, so they are a data format.
APPT_BOOKED = "booked"
APPT_RESCHEDULED = "rescheduled"  # still live; moved at least once
APPT_CANCELLED = "cancelled"

# Appointment.slot_lock -- see the Appointment docstring. A live row holds
# this constant; a cancelled row holds its own confirmation_id instead.
SLOT_LOCK_ACTIVE = "ACTIVE"


class Appointment(Base):
    """One appointment, plus the two columns cancellation forced us to add.

    WHY slot_lock EXISTS
    --------------------
    The unique constraint on (doctor_id, date, time_slot) is what makes
    double-booking impossible even when two callers race -- see
    main.book_appointment()'s IntegrityError handler, which turns that race
    into "that slot just went, here are three others".

    Cancellation breaks that arrangement. A cancelled row has to be KEPT:
    it is the audit trail, and every NotificationAttempt for the
    cancellation message points at it. But a kept row goes on occupying its
    slot under that constraint, so nobody could ever book a slot somebody
    else had released -- the cancellation would free the patient and not
    the appointment.

    Deleting the row instead would solve the constraint and lose the
    history, in the one domain where "we have no record of that
    appointment" is the worst possible answer to give at a counter.

    So the constraint gains a fourth column. A live row sets
    slot_lock=SLOT_LOCK_ACTIVE, so at most one live row can hold a given
    doctor/date/slot -- exactly the old guarantee. A cancelled row sets
    slot_lock to its own confirmation_id, which is unique by its own
    constraint, so any number of cancelled rows can pile up on the same
    slot without ever colliding with each other or with the live one.

    Every "is this slot taken" query must therefore filter on
    slot_lock == SLOT_LOCK_ACTIVE. There are three in book_appointment()
    and one in reschedule_appointment().

    RESCHEDULING NEEDS NO TOMBSTONE: the row itself moves to the new
    date/time_slot and stays ACTIVE, which frees the old slot as a
    side effect of the UPDATE.
    """

    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)

    confirmation_id = Column(
        String,
        nullable=False,
        unique=True,
    )

    doctor_id = Column(
        Integer,
        ForeignKey("doctors.id"),
        nullable=False,
    )

    date = Column(
        String,
        nullable=False,
    )

    time_slot = Column(
        String,
        nullable=False,
    )

    # Keep these fields for backward compatibility with the existing
    # appointment seed/tooling.
    patient_name = Column(
        String,
        nullable=False,
    )

    phone = Column(
        String,
        nullable=False,
    )

    # Optional connection to the new Patient table.
    #
    # Existing appointments can still work even if patient_id is NULL.
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=True,
    )

    created_at = Column(
        DateTime,
        nullable=False,
    )

    # ADDED BY CHAKRAVARDHAN -- see this class's own docstring above for
    # WHY these three exist (the slot_lock scheme that lets cancellation
    # keep the audit row without permanently occupying the slot). Real,
    # actively read/written columns: clinic-api/main.py's book_appointment/
    # reschedule_appointment/cancel_appointment already reference
    # appt.status, appt.slot_lock and the APPT_BOOKED/APPT_CANCELLED/
    # APPT_RESCHEDULED/SLOT_LOCK_ACTIVE constants above this class
    # unconditionally -- this branch's version of this class had simply
    # never gained the columns its own docstring already described.
    status = Column(String, nullable=False, default=APPT_BOOKED)
    slot_lock = Column(String, nullable=False, default=SLOT_LOCK_ACTIVE)
    updated_at = Column(DateTime, nullable=True)

    doctor = relationship(
        "Doctor",
    )

    patient = relationship(
        "Patient",
        back_populates="appointments",
    )

    __table_args__ = (
        UniqueConstraint(
            "doctor_id",
            "date",
            "time_slot",
            # ADDED BY CHAKRAVARDHAN -- slot_lock joins the uniqueness
            # constraint (see the class docstring's "WHY slot_lock EXISTS"):
            # without it, a cancelled row (which is kept, not deleted) would
            # permanently occupy its doctor/date/time_slot forever, and
            # nobody could ever book that slot again.
            "slot_lock",
            name="uq_doctor_slot",
        ),
    )


# ============================================================================
# CALLBACK REQUESTS
# ============================================================================
#
# ADDED BY SOURAV -- "Caller asks to be called back" story.
#
# Evidence: "No outbound capability" -- this stack cannot itself place a
# phone call, so a callback "request" is not something this system ever
# fulfils on its own. It is a durable, queryable RECORD that a human staff
# member picks up and acts on -- Acceptance Criterion 2's "tracked to
# fulfilment (e.g., stored in a DB table/queue with pending status)".
# `status` starts at "pending" and is expected to move to "fulfilled" or
# "cancelled" by whatever staff-facing process consumes this queue -- no
# such process exists in this prototype (there is no admin UI anywhere in
# this codebase), so this table alone is the entire scope of that AC: a
# real, persisted, greppable queue, not an in-memory list that a process
# restart would silently lose.
#
# Deliberately its OWN table, not a repurposed Appointment row: a callback
# has no doctor, no date/time_slot pair, and no patient_name -- forcing it
# into Appointment's shape would mean either fabricating those fields or
# making them nullable on a table that today guarantees they are always
# present for every real appointment (see Appointment.doctor_id/date/
# time_slot's own `nullable=False` above).
class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id = Column(Integer, primary_key=True)

    # Human-readable reference, same style/format as Appointment's own
    # confirmation_id (see clinic-api/main.py's book_appointment()) -- a
    # caller or staff member can read this back over the phone or on a
    # printout.
    callback_id = Column(
        String,
        nullable=False,
        unique=True,
    )

    # The number a human should actually dial. Not a foreign key into
    # Patient -- a caller asking for a callback is never required to be an
    # already-registered patient (unlike report_status/report_send/
    # billing_balance, which look up an EXISTING Patient row by phone).
    phone = Column(
        String,
        nullable=False,
    )

    # The caller's own words for when they'd like the call (e.g. "this
    # evening", "tomorrow morning") -- deliberately a free-text String, not
    # a resolved clock time: agent/llm.py's own "callback_time_window" slot
    # rule is explicit that this is copied verbatim, never resolved to a
    # timestamp, since "this evening" is not a fact this system is in a
    # position to convert into one on the caller's behalf.
    time_window = Column(
        String,
        nullable=False,
    )

    # Acceptance Criterion 1's "preserving the conversation context and
    # reason" -- see agent/callback_flow.py's build_callback_reason() for
    # exactly how this is built. Nullable: an honest "no reason given" is a
    # valid, real outcome, never backfilled with an invented one.
    reason = Column(
        Text,
        nullable=True,
    )

    # "pending" | "fulfilled" | "cancelled" -- see this class's own
    # docstring above for why nothing in this prototype ever moves it past
    # "pending" yet.
    status = Column(
        String,
        nullable=False,
        default="pending",
    )

    created_at = Column(
        DateTime,
        nullable=False,
    )


class NotificationAttempt(Base):
    """The delivery ledger: one row per message this service owes a patient.

    THE ROW IS WRITTEN BEFORE ANYTHING IS SENT, in the same transaction as
    the booking it belongs to. That ordering is the whole design. It means
    there is no window in which we have taken a booking and have no record
    that a message was due -- if the process dies before the background
    task runs, the row is still there, still `queued`, and the staff queue
    reports it as stale. "Silently dropped" is not a state this table can
    represent.

    The rendered `body` is stored rather than re-derived on demand. Two
    reasons, both operational: reception needs to see the exact text the
    patient was or was not sent when they turn up disputing it, and
    templates change -- re-rendering a two-week-old failure through today's
    template would show staff a message that was never composed.

    THIS TABLE HOLDS PII (patient name and phone, in a column and again
    inside `body`). So does `appointments`. Retention is not implemented
    here and is flagged in the implementation notes as outstanding work,
    rather than left to look like an oversight.
    """

    __tablename__ = "notification_attempts"
    id = Column(Integer, primary_key=True)

    # Not a ForeignKey on purpose. The ledger has to outlive the row it
    # describes -- if an appointment is ever purged for retention, the
    # evidence that we did or did not message that patient must not be
    # cascaded away with it.
    confirmation_id = Column(String, nullable=False, index=True)

    event = Column(String, nullable=False)  # message_templates.EVENT_*
    channel = Column(String, nullable=False, default="sms")
    phone = Column(String, nullable=False)  # E.164 without '+', as sent
    template_id = Column(String, nullable=False, default="")  # DLT content ID
    body = Column(String, nullable=False)  # exactly what was submitted

    status = Column(String, nullable=False, index=True)  # notifications.STATUS_*
    provider_message_id = Column(String, nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    error_code = Column(String, nullable=True)
    error_detail = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=False)
    # Moves on every state change. The staff queue's staleness rule is
    # measured from here, not from created_at, so a row that was retried
    # ten minutes ago is not immediately stale again.
    updated_at = Column(DateTime, nullable=False)
    delivered_at = Column(DateTime, nullable=True)

    # Set when a member of staff has seen the failure and taken it on.
    # An acknowledged row leaves the queue without its status being
    # rewritten -- the failure stays a failure in the record.
    acknowledged_by = Column(String, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)


# ===========================================================================
# PATIENT IDENTITY AND HISTORY
# ---------------------------------------------------------------------------
# Author: Chakravardhan
#
# Added for the story "History disclosed only after verification".
#
# THE THREAT THIS MODELS, stated plainly because every field below follows
# from it: the handset is SHARED. A family phone, a shop phone, a neighbour's
# phone. Somebody who is not the patient dials the clinic from it.
#
# Before this, the system had no notion of a patient at all -- only
# Appointment rows carrying a phone number. Anything keyed on that phone
# number would have read one person's medical history to whoever happened to
# be holding their phone.
# ===========================================================================

# NOTE (merge dev_chakravardhan -> staging_merged): this story originally
# defined its own `class Patient(Base): __tablename__ = "patients"` here,
# duplicating the pre-existing Patient class above (which LabReport.patient
# and Appointment.patient already depend on via back_populates). Two mapped
# classes for the same table name would have broken SQLAlchemy's mapper
# configuration at import time -- git's line-based diff never flagged it
# because the two class bodies never touched the same lines. All of this
# class's fields (full_name, date_of_birth, pin_hash, pin_salt, pin_set_at,
# failed_attempts, locked_until, last_verified_at, created_at) have been
# folded onto the original Patient class instead; nothing below was dropped.


class TestRecord(Base):
    """One test a patient actually had. THE THING THE STORY PROTECTS.

    No such table existed -- `appointments` records doctor visits, not tests
    taken -- so "what tests I have had" had no answer to disclose, safely or
    otherwise. History has to be modelled before it can be withheld.

    Kept deliberately thin. It holds enough to answer "which tests, when,
    and is the report ready", and NOT the results themselves. A voice line
    that reads clinical values aloud is a bigger disclosure surface than
    this story is asking anyone to build, and the counter already exists as
    the path for detail -- see the no-smartphone work.
    """

    __tablename__ = "test_records"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    lab_test_id = Column(Integer, ForeignKey("lab_tests.id"), nullable=True)

    # Denormalised on purpose: the catalogue can be reseeded (seed.py does a
    # drop_all/create_all) and a patient's history must not turn into a list
    # of dangling ids when it is.
    test_name = Column(String, nullable=False)
    test_name_bn = Column(String, nullable=True)

    taken_on = Column(String, nullable=False)  # ISO yyyy-mm-dd
    report_ready = Column(Boolean, nullable=False, default=False)
    report_ready_on = Column(String, nullable=True)

    created_at = Column(DateTime, nullable=False)

    patient = relationship("Patient")


class DisclosureAudit(Base):
    """Every attempt to reach a patient's history, successful or not.

    EXISTS BECAUSE THE FAILURES ARE THE INTERESTING PART. A row per success
    tells you the feature works; a run of failures against one phone number
    at 2am is somebody guessing, and without this table that is invisible.

    NO SECRET IS EVER WRITTEN HERE. Not the PIN, not the date of birth, not
    the answer that was offered. `factor` records WHICH kind of proof was
    attempted and `outcome` records whether it worked -- an audit trail that
    leaks the thing it audits would be worse than none.
    """

    __tablename__ = "disclosure_audit"
    id = Column(Integer, primary_key=True)

    # The number the call came from. Stored because it is the only handle on
    # a repeated attacker; NOT stored as proof of anything.
    phone = Column(String, nullable=False, index=True)
    patient_id = Column(Integer, nullable=True)  # null when no match

    factor = Column(String, nullable=False)  # "pin" | "dob" | "none"
    outcome = Column(String, nullable=False, index=True)
    # "verified" | "wrong_factor" | "no_patient" | "locked_out"
    # | "no_factor_available" | "unsafe_audio_path" | "disclosed"

    detail = Column(String, nullable=True)  # never a secret
    call_id = Column(String, nullable=True)  # ties rows to one call
    created_at = Column(DateTime, nullable=False, index=True)
