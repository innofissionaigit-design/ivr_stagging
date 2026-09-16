"""The delivery ledger's operations -- everything that touches both the
gateway and the database.

WHY THIS IS A THIRD FILE
------------------------
notifications.py deliberately imports nothing from the ORM: it is the
gateway contract, the status vocabulary and the receipt mapping, and it can
be unit-tested with no database at all (tests/test_notifications.py does
exactly that). models.py is the schema. This file is the only place the two
meet, so main.py's endpoints stay readable as endpoints -- validate, mutate
the appointment, queue the message, return -- with the ledger mechanics out
of the way.

THE ORDERING THAT MATTERS
-------------------------
Every write here follows the same sequence, and it is the sequence the
acceptance criterion depends on:

    1. the appointment change is committed;
    2. the ledger row is committed alongside it, as `queued`;
    3. ONLY THEN is a network call attempted, from a background task.

Steps 1 and 2 share a transaction. That is what makes "silently dropped"
unrepresentable: a crash after step 2 leaves a `queued` row that the staff
queue reports as stale, and a crash before step 2 leaves no appointment
either, so there is nothing to have failed to announce.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

import message_templates as mt
import notifications as notify
from db import SessionLocal
from models import Appointment, NotificationAttempt

log = logging.getLogger("clinic-api.notify")


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _variables(appt: Appointment, doctor_name: str) -> dict:
    """The values every template draws from.

    Doctor names are stored with an honorific ("Dr. S. Mukherjee") and the
    templates already print "ডাঃ " themselves, so the prefix is stripped
    here rather than in three template bodies. It also buys back four of
    the thirty characters DLT allows a variable -- see
    message_templates.VAR_MAX_CHARS, which a long "Dr. " -prefixed name can
    genuinely exceed.
    """
    name = (doctor_name or "").strip()
    for prefix in ("Dr. ", "Dr ", "ডাঃ ", "ডা. "):
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            break

    # The patient name is the ONLY variable that can realistically breach
    # DLT's 30-character ceiling, and it is the one value here that did not
    # come from our own database in a controlled shape -- main.py's
    # _clean_patient_name() hands over whatever the caller said, minus a
    # leading "আমার নাম". A chatty answer, or ASR running two words
    # together, produces a perfectly ordinary Bengali name of 33 characters.
    #
    # Clamping here rather than letting render() reject it is deliberate.
    # render() is the compliance gate and stays strict; this is the layer
    # that feeds it compliant values. The alternative -- a TemplateError,
    # recorded as a failure -- means a patient gets NO written confirmation
    # at all because their name was long, which is a worse outcome than a
    # name trimmed to fit on the SMS.
    patient = (appt.patient_name or "").strip()
    if len(patient) > mt.VAR_MAX_CHARS:
        patient = patient[:mt.VAR_MAX_CHARS].strip()

    return {
        "patient_name": patient,
        "doctor_name": name,
        "date": appt.date,
        "time_slot": appt.time_slot,
        "confirmation_id": appt.confirmation_id,
    }


def queue_message(db: Session, appt: Appointment, event: str, doctor_name: str,
                  config: notify.GatewayConfig | None = None) -> NotificationAttempt:
    """Write the ledger row for one event. Does NOT commit, and does NOT
    send -- the caller commits it in the same transaction as the
    appointment change, then hands the row id to deliver_now().

    Always returns a row. There is no path that decides a message is not
    worth recording: a template that cannot render, a gateway that is not
    configured and a registration that is missing all produce a row with
    the reason in error_code, because a patient who was never messaged is
    exactly the case staff need to be able to see.
    """
    config = config or notify.load_config()
    now = _now()

    attempt = NotificationAttempt(
        confirmation_id=appt.confirmation_id,
        event=event,
        channel=notify.CHANNEL_SMS,
        phone=notify.msisdn(appt.phone, config.country_code),
        template_id="",
        body="",
        status=notify.STATUS_QUEUED,
        attempts=0,
        created_at=now,
        updated_at=now,
    )

    try:
        text, tpl = mt.render(event, _variables(appt, doctor_name))
    except mt.TemplateError as e:
        # A bug in this codebase, not a patient-facing condition. Recorded
        # as a failure so it reaches the staff queue and gets fixed, and
        # logged at error so it also reaches whoever reads the service log.
        log.error("cannot render %s message for %s: %s", event, appt.confirmation_id, e)
        attempt.status = notify.STATUS_FAILED
        attempt.error_code = "template_render"
        attempt.error_detail = str(e)[:500]
        db.add(attempt)
        return attempt

    attempt.body = text
    attempt.template_id = tpl.template_id

    blocked = notify.preflight(config, tpl)
    if blocked:
        # Not configured at all is a bench pod, and every booking there
        # would otherwise raise a staff alert -- so it is recorded as
        # `skipped`, which stays visible and countable but stays out of the
        # failure queue. Anything else is a real misconfiguration in a
        # deployment that DOES have a gateway, and staff must see it.
        attempt.status = (notify.STATUS_SKIPPED if blocked == "gateway_not_configured"
                          else notify.STATUS_FAILED)
        attempt.error_code = blocked
        attempt.error_detail = f"preflight refused to send: {blocked}"
        if attempt.status == notify.STATUS_FAILED:
            log.error("%s message for %s blocked: %s", event, appt.confirmation_id, blocked)
        else:
            log.info("no gateway configured -- %s message for %s recorded but not sent",
                     event, appt.confirmation_id)

    db.add(attempt)
    return attempt


def deliver_now(attempt_id: int) -> None:
    """Send one queued row. THE BACKGROUND TASK ENTRY POINT.

    Runs after the response has been returned, on its own session, and
    swallows everything: this is the end of the line, and an exception
    escaping here would leave the row `queued` with no reason recorded --
    the one outcome the whole design exists to prevent.

    Re-reads the row and re-checks its status rather than trusting the
    caller, so a staff retry and the original background task racing on the
    same row cannot send the same message twice.
    """
    db = SessionLocal()
    try:
        attempt = db.get(NotificationAttempt, attempt_id)
        if attempt is None:
            log.error("delivery task for missing ledger row %s", attempt_id)
            return
        if attempt.status != notify.STATUS_QUEUED:
            log.info("ledger row %s is %s, not queued -- not sending again",
                     attempt_id, attempt.status)
            return

        config = notify.load_config()
        registry_tpl = mt.TEMPLATES.get(attempt.event)
        if registry_tpl is None:
            attempt.status = notify.STATUS_FAILED
            attempt.error_code = "template_missing"
            attempt.error_detail = f"no template registered for event {attempt.event!r}"
            attempt.updated_at = _now()
            db.commit()
            return

        # SUBMIT THE TEMPLATE ID THIS MESSAGE WAS COMPOSED UNDER, not
        # whatever the registry holds now. The two diverge on a staff retry
        # of an old row after the templates were re-registered: `body` is
        # the stored text, and pairing stored text with a NEW template id is
        # precisely the mismatch the operator rejects -- silently, from the
        # patient's side. Falling back to the registry only when nothing was
        # recorded is what lets a retry succeed after the ids are finally
        # configured on a pod that had none.
        tpl = dataclasses.replace(
            registry_tpl, template_id=attempt.template_id or registry_tpl.template_id)

        # Re-checked here, not just at queue time. requeue() will put a
        # `skipped` row back in the queue, and without this a staff retry on
        # a pod with no gateway would hand an unregistered template to
        # send() and pay for the rejection.
        blocked = notify.preflight(config, tpl)
        if blocked:
            attempt.status = (notify.STATUS_SKIPPED if blocked == "gateway_not_configured"
                              else notify.STATUS_FAILED)
            attempt.error_code = blocked
            attempt.error_detail = f"preflight refused to send: {blocked}"
            attempt.updated_at = _now()
            db.commit()
            return

        attempt.template_id = tpl.template_id
        result = notify.send(config, to=attempt.phone, text=attempt.body, tpl=tpl,
                             client_ref=str(attempt.id))
        attempt.attempts += 1
        if result.accepted:
            attempt.status = notify.STATUS_SENT
            attempt.provider_message_id = result.provider_message_id
            attempt.error_code = None
            attempt.error_detail = None
            log.info("%s message for %s accepted by gateway (provider id %s)",
                     attempt.event, attempt.confirmation_id, result.provider_message_id)
        else:
            attempt.status = notify.STATUS_FAILED
            attempt.error_code = result.error_code
            attempt.error_detail = result.error_detail
            # WARNING, not INFO: this is a patient who will arrive at
            # reception without the message they were told to expect.
            log.warning("%s message for %s REJECTED by gateway: %s / %s",
                        attempt.event, attempt.confirmation_id,
                        result.error_code, result.error_detail)

        attempt.updated_at = _now()
        db.commit()
    except Exception:                                    # noqa: BLE001
        log.exception("delivery task for ledger row %s crashed", attempt_id)
        db.rollback()
    finally:
        db.close()


def apply_receipt(db: Session, *, raw_status: str, provider_message_id: str | None = None,
                  client_ref: str | None = None, error_code: str | None = None,
                  error_detail: str | None = None) -> NotificationAttempt | None:
    """Record a delivery receipt against its ledger row. -> the row, or None
    if the receipt could not be matched to one.

    Matched on provider_message_id first and on client_ref (our own row id,
    echoed back) second. The fallback matters: if the gateway's HTTP
    response was lost in transit we accepted the send but never learned the
    provider's id, and client_ref is then the only way its receipt can find
    the row it belongs to.

    An unmatched receipt returns None rather than raising -- the endpoint
    logs it and answers 200, because a gateway that gets an error back will
    usually retry the receipt forever.
    """
    attempt = None
    if provider_message_id:
        attempt = (db.query(NotificationAttempt)
                     .filter_by(provider_message_id=str(provider_message_id)).first())
    if attempt is None and client_ref:
        try:
            attempt = db.get(NotificationAttempt, int(client_ref))
        except (TypeError, ValueError):
            attempt = None
    if attempt is None:
        return None

    new_status = notify.classify_receipt(raw_status)

    # A receipt never reopens a settled row. Operators re-send receipts,
    # and out-of-order duplicates are normal; letting a stale "enroute"
    # arrive after "delivered" would put a delivered message back into the
    # staff queue.
    if attempt.status == notify.STATUS_DELIVERED:
        return attempt

    attempt.status = new_status
    if provider_message_id and not attempt.provider_message_id:
        attempt.provider_message_id = str(provider_message_id)[:120]
    if new_status == notify.STATUS_DELIVERED:
        attempt.delivered_at = _now()
        attempt.error_code = None
        attempt.error_detail = None
    elif new_status == notify.STATUS_FAILED:
        attempt.error_code = (error_code or raw_status or "receipt_failed")[:120]
        attempt.error_detail = (error_detail or f"gateway reported {raw_status!r}")[:500]
        log.warning("%s message for %s NOT DELIVERED: %s",
                    attempt.event, attempt.confirmation_id, attempt.error_code)
    attempt.updated_at = _now()
    return attempt


def open_failures(db: Session, stale_minutes: int = notify.DEFAULT_STALE_MINUTES,
                  include_acknowledged: bool = False,
                  include_skipped: bool = False) -> list[NotificationAttempt]:
    """The staff queue: every message a patient is still owed.

    THREE THINGS COUNT AS A FAILURE HERE, and the third is the one this
    endpoint exists for:

      * status == failed -- the gateway or a receipt said so outright;
      * status == queued and older than `stale_minutes` -- the background
        task never ran, or the process died between committing the row and
        sending it;
      * status == sent and older than `stale_minutes` -- the gateway
        accepted it and no receipt ever came back. This is the silent case.
        Nothing errored, no log line was written, and without this rule the
        patient simply turns up at reception with nothing.

    `skipped` rows are excluded by default: on a bench pod with no gateway
    that is every row, and a queue that is always full is a queue nobody
    reads.
    """
    cutoff = _now() - datetime.timedelta(minutes=stale_minutes)

    q = db.query(NotificationAttempt)
    open_clause = [
        NotificationAttempt.status == notify.STATUS_FAILED,
        (NotificationAttempt.status.in_(notify.OPEN_STATUSES))
        & (NotificationAttempt.updated_at < cutoff),
    ]
    if include_skipped:
        open_clause.append(NotificationAttempt.status == notify.STATUS_SKIPPED)
    q = q.filter(or_(*open_clause))
    if not include_acknowledged:
        q = q.filter(NotificationAttempt.acknowledged_at.is_(None))
    return q.order_by(NotificationAttempt.created_at.desc()).all()


def acknowledge(db: Session, attempt_id: int, staff: str) -> NotificationAttempt | None:
    """Staff take a failure on. Leaves `status` alone deliberately -- the
    message still failed, and rewriting that to make a queue look tidy
    would falsify the delivery record this table exists to be."""
    attempt = db.get(NotificationAttempt, attempt_id)
    if attempt is None:
        return None
    attempt.acknowledged_by = (staff or "staff")[:120]
    attempt.acknowledged_at = _now()
    attempt.updated_at = attempt.acknowledged_at
    return attempt


def requeue(db: Session, attempt_id: int) -> NotificationAttempt | None:
    """Put a failed row back in the queue for one more attempt.

    Only a settled row can be requeued -- requeuing something already
    `queued` would let a staff click race the original background task and
    send the patient two identical messages.
    """
    attempt = db.get(NotificationAttempt, attempt_id)
    if attempt is None or attempt.status == notify.STATUS_QUEUED:
        return None
    attempt.status = notify.STATUS_QUEUED
    attempt.error_code = None
    attempt.error_detail = None
    attempt.updated_at = _now()
    return attempt


def as_dict(attempt: NotificationAttempt) -> dict:
    """The staff-facing shape. `body` is included on purpose: reception is
    dealing with a patient in front of them who says they were told to
    expect a message, and the useful answer is what that message said."""
    return {
        "id": attempt.id,
        "confirmation_id": attempt.confirmation_id,
        "event": attempt.event,
        "channel": attempt.channel,
        "phone": attempt.phone,
        "template_id": attempt.template_id,
        "body": attempt.body,
        "status": attempt.status,
        "provider_message_id": attempt.provider_message_id,
        "attempts": attempt.attempts,
        "error_code": attempt.error_code,
        "error_detail": attempt.error_detail,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "updated_at": attempt.updated_at.isoformat() if attempt.updated_at else None,
        "delivered_at": attempt.delivered_at.isoformat() if attempt.delivered_at else None,
        "acknowledged_by": attempt.acknowledged_by,
        "acknowledged_at": (attempt.acknowledged_at.isoformat()
                            if attempt.acknowledged_at else None),
    }


def summary(db: Session, stale_minutes: int = notify.DEFAULT_STALE_MINUTES) -> dict:
    """Counts for /api/health, so the state of the ledger is visible to
    whatever already polls that endpoint instead of needing someone to
    remember to look at a separate one."""
    counts = {}
    for status in (notify.STATUS_QUEUED, notify.STATUS_SENT, notify.STATUS_DELIVERED,
                   notify.STATUS_FAILED, notify.STATUS_SKIPPED):
        counts[status] = db.query(NotificationAttempt).filter_by(status=status).count()
    counts["open_failures"] = len(open_failures(db, stale_minutes=stale_minutes))
    return counts
