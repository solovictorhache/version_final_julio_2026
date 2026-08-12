#!/usr/bin/env python3
"""
recordatorios.py

Personal reminder tracker: keep a list of dated items (a meeting, an event,
"I need to send this email") in recordatorios.json, and get an email digest
whenever one is due — with configurable advance notice (e.g. 7 and 1 days
before), and daily nagging once something becomes overdue and isn't marked
done.

Meant to run daily from cron, alongside radar_convocatorias.py (see
README.md). Uses the same "only mark as notified after a successful email"
pattern: a failed send is retried on the next run instead of being lost.

Environment variables (all required to actually send mail):
    RECORDATORIOS_SMTP_HOST       SMTP server hostname
    RECORDATORIOS_SMTP_PORT       SMTP port (587 for STARTTLS)
    RECORDATORIOS_SMTP_USER       SMTP auth username
    RECORDATORIOS_SMTP_PASSWORD   SMTP auth password / app password
    RECORDATORIOS_EMAIL_FROM      From: address
    RECORDATORIOS_EMAIL_TO        Comma-separated recipient list

    (These can point at the same Gmail account/app password already used
    for radar_convocatorias.py's RADAR_SMTP_* variables.)

Optional:
    RECORDATORIOS_FILE   Path to recordatorios.json (default: next to this
                          script)
    RECORDATORIOS_STATE_FILE   Path to state.json (default: next to this
                                script)
    RECORDATORIOS_LOG_FILE     Also log to this file, in addition to stdout.

Usage:
    recordatorios.py                    Check and email what's due today.
    recordatorios.py --list             List all pending reminders.
    recordatorios.py --add ...          Add a new reminder.
    recordatorios.py --done ID          Mark a reminder as done.
    recordatorios.py --dry-run          Show what would be emailed, no send.
    recordatorios.py --reset-state      Forget which offsets were already
                                         notified (so they can fire again).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FILE = SCRIPT_DIR / "recordatorios.json"
DEFAULT_STATE_FILE = SCRIPT_DIR / "recordatorios_state.json"
REQUEST_TIMEOUT = 30
VALID_TYPES = {"evento", "reunion", "email", "tarea"}

logger = logging.getLogger("recordatorios")


@dataclass
class Reminder:
    id: str
    title: str
    date: str  # ISO date, YYYY-MM-DD
    type: str = "tarea"
    time: str | None = None
    notify_days_before: list[int] = field(default_factory=lambda: [7, 1, 0])
    notes: str = ""
    done: bool = False

    @property
    def due_date(self) -> date:
        return date.fromisoformat(self.date)

    def days_left(self, today: date) -> int:
        return (self.due_date - today).days

    def label(self) -> str:
        when = self.date + (f" {self.time}" if self.time else "")
        return f"[{self.type}] {self.title} — {when}"


def load_reminders(path: Path) -> list[Reminder]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return [Reminder(**entry) for entry in raw]


def save_reminders(path: Path, reminders: list[Reminder]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in reminders], fh, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"notified": []}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: Path, state: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def find_due(reminders: list[Reminder], notified: set[str], today: date):
    """Return (advance_due, overdue): items to notify now.

    advance_due entries are deduplicated via `notified` (each offset fires
    once). overdue entries are NOT deduplicated: they nag every run until
    the reminder is marked done, since a missed deadline deserves repeating.
    """
    advance_due: list[tuple[Reminder, int]] = []
    overdue: list[tuple[Reminder, int]] = []

    for reminder in reminders:
        if reminder.done:
            continue
        days_left = reminder.days_left(today)

        if days_left < 0:
            overdue.append((reminder, days_left))
            continue

        for offset in reminder.notify_days_before:
            key = f"{reminder.id}:{offset}"
            if days_left == offset and key not in notified:
                advance_due.append((reminder, offset))

    return advance_due, overdue


def build_email_body(advance_due, overdue, today: date) -> tuple[str, str]:
    total = len(advance_due) + len(overdue)
    lines = [f"Recordatorios para hoy ({today.isoformat()}):", ""]

    if overdue:
        lines.append("== VENCIDOS (sin marcar como hechos) ==")
        for reminder, days_left in sorted(overdue, key=lambda x: x[1]):
            lines.append(f"- {reminder.label()}  (hace {-days_left} día(s)) [id: {reminder.id}]")
        lines.append("")

    if advance_due:
        lines.append("== Próximos ==")
        for reminder, offset in sorted(advance_due, key=lambda x: x[1]):
            when = "hoy" if offset == 0 else f"en {offset} día(s)"
            lines.append(f"- {reminder.label()}  ({when}) [id: {reminder.id}]")
            if reminder.notes:
                lines.append(f"  Notas: {reminder.notes}")
        lines.append("")

    lines.append('Marca uno como hecho con: recordatorios.py --done <id>')

    subject = f"Recordatorios: {total} pendiente(s) hoy"
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    host = os.environ["RECORDATORIOS_SMTP_HOST"]
    port = int(os.environ["RECORDATORIOS_SMTP_PORT"])
    user = os.environ["RECORDATORIOS_SMTP_USER"]
    password = os.environ["RECORDATORIOS_SMTP_PASSWORD"]
    sender = os.environ["RECORDATORIOS_EMAIL_FROM"]
    recipients = [a.strip() for a in os.environ["RECORDATORIOS_EMAIL_TO"].split(",") if a.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    logger.info("Sending email to %s via %s:%s", recipients, host, port)
    with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(sender, recipients, msg.as_string())


def cmd_add(args: argparse.Namespace, reminders_path: Path) -> int:
    if not args.title or not args.date:
        print("--add requiere --title y --date (YYYY-MM-DD)", file=sys.stderr)
        return 1
    try:
        date.fromisoformat(args.date)
    except ValueError:
        print(f"Fecha inválida: {args.date!r} (usa YYYY-MM-DD)", file=sys.stderr)
        return 1
    if args.type not in VALID_TYPES:
        print(f"Tipo inválido: {args.type!r} (usa uno de {sorted(VALID_TYPES)})", file=sys.stderr)
        return 1

    notify_days_before = [int(x) for x in args.notify_days.split(",")] if args.notify_days else [7, 1, 0]

    reminders = load_reminders(reminders_path)
    new = Reminder(
        id=uuid.uuid4().hex[:8],
        title=args.title,
        date=args.date,
        type=args.type,
        time=args.time,
        notify_days_before=notify_days_before,
        notes=args.notes or "",
    )
    reminders.append(new)
    save_reminders(reminders_path, reminders)
    print(f"Añadido [{new.id}]: {new.label()}")
    return 0


def cmd_list(reminders: list[Reminder], today: date) -> int:
    if not reminders:
        print("No hay recordatorios.")
        return 0
    pending = sorted((r for r in reminders if not r.done), key=lambda r: r.due_date)
    done = [r for r in reminders if r.done]

    print("Pendientes:")
    for r in pending:
        days_left = r.days_left(today)
        status = f"vencido hace {-days_left} día(s)" if days_left < 0 else f"en {days_left} día(s)"
        print(f"  [{r.id}] {r.label()}  ({status})")
    if done:
        print("\nHechos:")
        for r in done:
            print(f"  [{r.id}] {r.label()}")
    return 0


def cmd_done(reminder_id: str, reminders: list[Reminder], reminders_path: Path) -> int:
    for r in reminders:
        if r.id == reminder_id:
            r.done = True
            save_reminders(reminders_path, reminders)
            print(f"Marcado como hecho: [{r.id}] {r.label()}")
            return 0
    print(f"No se encontró el recordatorio con id {reminder_id!r}", file=sys.stderr)
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=Path(os.environ.get("RECORDATORIOS_FILE", DEFAULT_FILE)))
    parser.add_argument("--state-file", type=Path, default=Path(os.environ.get("RECORDATORIOS_STATE_FILE", DEFAULT_STATE_FILE)))
    parser.add_argument("--reset-state", action="store_true", help="Olvida qué avisos ya se enviaron.")
    parser.add_argument("--dry-run", action="store_true", help="Muestra qué se enviaría, sin mandar email ni tocar el estado.")
    parser.add_argument("-v", "--verbose", action="store_true")

    parser.add_argument("--list", action="store_true", help="Lista todos los recordatorios y sale.")
    parser.add_argument("--done", metavar="ID", help="Marca el recordatorio ID como hecho y sale.")

    add_group = parser.add_argument_group("--add (crea un recordatorio nuevo y sale)")
    add_group.add_argument("--add", action="store_true")
    add_group.add_argument("--title")
    add_group.add_argument("--date", help="YYYY-MM-DD")
    add_group.add_argument("--time", help="HH:MM (opcional)")
    add_group.add_argument("--type", default="tarea", help="evento | reunion | email | tarea")
    add_group.add_argument("--notify-days", help="Días de antelación separados por coma, p.ej. '7,1,0'")
    add_group.add_argument("--notes", default="")

    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    log_file = os.environ.get("RECORDATORIOS_LOG_FILE")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    today = date.today()

    if args.add:
        return cmd_add(args, args.file)

    try:
        reminders = load_reminders(args.file)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.error("No se pudo leer %s: %s", args.file, exc)
        return 1

    if args.list:
        return cmd_list(reminders, today)

    if args.done:
        return cmd_done(args.done, reminders, args.file)

    if args.reset_state and args.state_file.exists():
        logger.info("Reiniciando estado %s", args.state_file)
        args.state_file.unlink()

    state = load_state(args.state_file)
    notified = set(state.get("notified", []))

    advance_due, overdue = find_due(reminders, notified, today)
    total = len(advance_due) + len(overdue)
    logger.info("%d aviso(s) próximo(s), %d vencido(s)", len(advance_due), len(overdue))

    if args.dry_run:
        if total:
            subject, body = build_email_body(advance_due, overdue, today)
            print(f"Subject: {subject}\n\n{body}")
        else:
            print("Nada pendiente hoy.")
        return 0

    if total == 0:
        logger.info("Nada pendiente; no se envía email.")
        return 0

    subject, body = build_email_body(advance_due, overdue, today)
    try:
        send_email(subject, body)
    except KeyError as exc:
        logger.error("Falta variable de entorno: %s", exc)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Fallo al enviar el email: %s", exc)
        return 1

    # Only the advance (non-overdue) offsets get deduplicated; overdue items
    # intentionally have no state entry so they nag again next run.
    for reminder, offset in advance_due:
        notified.add(f"{reminder.id}:{offset}")
    state["notified"] = sorted(notified)
    save_state(args.state_file, state)
    logger.info("Estado actualizado con %d aviso(s) conocido(s).", len(state["notified"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
