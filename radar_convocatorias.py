#!/usr/bin/env python3
"""
radar_convocatorias.py

Watches one or more "convocatorias" (grant/funding call) listing pages,
detects entries that weren't seen on the previous run, and emails a
digest of the new ones.

Designed to run unattended from cron (see README.md for BeeStation/Synology
deployment steps). Configuration lives in sources.json next to this file
so sources and CSS selectors can be tuned without touching the code.

Environment variables (all required to actually send mail):
    RADAR_SMTP_HOST       SMTP server hostname (e.g. smtp.gmail.com)
    RADAR_SMTP_PORT       SMTP port (e.g. 587 for STARTTLS)
    RADAR_SMTP_USER       SMTP auth username
    RADAR_SMTP_PASSWORD   SMTP auth password / app password
    RADAR_EMAIL_FROM      From: address
    RADAR_EMAIL_TO        Comma-separated list of recipient addresses

Optional environment variables:
    RADAR_STATE_FILE      Path to the JSON state file (default: state.json
                           next to this script)
    RADAR_SOURCES_FILE    Path to the sources config (default: sources.json
                           next to this script)
    RADAR_LOG_FILE        Path to a log file. If unset, logs go to stdout
                           only (fine under cron, which mails stdout/stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCES_FILE = SCRIPT_DIR / "sources.json"
DEFAULT_STATE_FILE = SCRIPT_DIR / "state.json"
USER_AGENT = "radar_convocatorias/1.0 (+https://github.com/solovictorhache/version_final_julio_2026)"
REQUEST_TIMEOUT = 30

logger = logging.getLogger("radar_convocatorias")


@dataclass
class Source:
    name: str
    url: str
    item_selectors: list[str]
    title_selector: str | None = None
    link_selector: str | None = None
    link_base: str | None = None
    min_items: int = 1


@dataclass
class Item:
    source: str
    title: str
    link: str

    @property
    def uid(self) -> str:
        digest = hashlib.sha256(self.link.encode("utf-8")).hexdigest()
        return f"{self.source}:{digest}"


def load_sources(path: Path) -> list[Source]:
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    sources = []
    for entry in raw:
        sources.append(
            Source(
                name=entry["name"],
                url=entry["url"],
                item_selectors=entry["item_selectors"],
                title_selector=entry.get("title_selector"),
                link_selector=entry.get("link_selector"),
                link_base=entry.get("link_base", entry["url"]),
                min_items=entry.get("min_items", 1),
            )
        )
    return sources


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": []}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: Path, state: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def fetch_source(source: Source) -> list[Item]:
    logger.info("Fetching source %r (%s)", source.name, source.url)
    response = requests.get(
        source.url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    # Parse raw bytes (not response.text) so BeautifulSoup/lxml can sniff the
    # encoding from the HTML itself; requests falls back to Latin-1 for
    # text/html responses that omit an explicit charset, which mangles
    # UTF-8 pages (e.g. accented Spanish text).
    soup = BeautifulSoup(response.content, "lxml")

    elements = []
    for selector in source.item_selectors:
        elements = soup.select(selector)
        if len(elements) >= source.min_items:
            logger.debug(
                "Source %r: selector %r matched %d item(s)",
                source.name, selector, len(elements),
            )
            break
    else:
        logger.warning(
            "Source %r: no configured selector matched >= %d item(s); "
            "the page layout may have changed. Check sources.json.",
            source.name, source.min_items,
        )
        return []

    items: list[Item] = []
    seen_links: set[str] = set()
    for element in elements:
        link_el = element.select_one(source.link_selector) if source.link_selector else element
        if link_el is None or not link_el.has_attr("href"):
            link_el = element.find("a", href=True)
        if link_el is None:
            continue
        href = link_el["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        link = urljoin(source.link_base or source.url, href)

        title_el = element.select_one(source.title_selector) if source.title_selector else link_el
        title = title_el.get_text(strip=True) if title_el is not None else link
        if not title:
            title = link

        if link in seen_links:
            continue
        seen_links.add(link)
        items.append(Item(source=source.name, title=title, link=link))

    logger.info("Source %r: %d item(s) extracted", source.name, len(items))
    return items


def find_new_items(items: list[Item], seen_uids: set[str]) -> list[Item]:
    return [item for item in items if item.uid not in seen_uids]


def build_email_body(new_items: list[Item]) -> tuple[str, str]:
    by_source: dict[str, list[Item]] = {}
    for item in new_items:
        by_source.setdefault(item.source, []).append(item)

    lines = [f"Se han detectado {len(new_items)} convocatoria(s) nueva(s):", ""]
    for source_name, source_items in by_source.items():
        lines.append(f"== {source_name} ==")
        for item in source_items:
            lines.append(f"- {item.title}")
            lines.append(f"  {item.link}")
        lines.append("")

    subject = f"Radar de convocatorias: {len(new_items)} novedad(es)"
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    host = os.environ["RADAR_SMTP_HOST"]
    port = int(os.environ["RADAR_SMTP_PORT"])
    user = os.environ["RADAR_SMTP_USER"]
    password = os.environ["RADAR_SMTP_PASSWORD"]
    sender = os.environ["RADAR_EMAIL_FROM"]
    recipients = [addr.strip() for addr in os.environ["RADAR_EMAIL_TO"].split(",") if addr.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    logger.info("Sending email to %s via %s:%s", recipients, host, port)
    with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(sender, recipients, msg.as_string())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources-file",
        type=Path,
        default=Path(os.environ.get("RADAR_SOURCES_FILE", DEFAULT_SOURCES_FILE)),
        help="Path to sources.json (default: %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("RADAR_STATE_FILE", DEFAULT_STATE_FILE)),
        help="Path to the state file (default: %(default)s)",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Discard previously stored state before running, so every "
             "currently listed item is treated as new. Useful for a first "
             "manual test run to confirm email delivery works end to end.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch sources and print what would be emailed, but do not "
             "send mail and do not update the state file.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    log_file = os.environ.get("RADAR_LOG_FILE")
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

    if args.reset_state and args.state_file.exists():
        logger.info("Resetting state file %s", args.state_file)
        args.state_file.unlink()

    try:
        sources = load_sources(args.sources_file)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        logger.error("Could not load sources file %s: %s", args.sources_file, exc)
        return 1

    state = load_state(args.state_file)
    seen_uids = set(state.get("seen", []))

    all_items: list[Item] = []
    had_source_error = False
    for source in sources:
        try:
            all_items.extend(fetch_source(source))
        except requests.RequestException as exc:
            had_source_error = True
            logger.error("Failed to fetch source %r: %s", source.name, exc)

    new_items = find_new_items(all_items, seen_uids)
    logger.info("%d new item(s) out of %d total", len(new_items), len(all_items))

    if args.dry_run:
        if new_items:
            subject, body = build_email_body(new_items)
            print(f"Subject: {subject}\n\n{body}")
        else:
            print("No new items found.")
        return 0

    if not new_items:
        logger.info("Nothing new; not sending email.")
        return 1 if had_source_error else 0

    subject, body = build_email_body(new_items)
    try:
        send_email(subject, body)
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Failed to send email: %s", exc)
        return 1

    # Only mark items as seen once the email actually went out, so a failed
    # send is retried (and re-reported) on the next run instead of being lost.
    state["seen"] = sorted({item.uid for item in all_items} | seen_uids)
    save_state(args.state_file, state)
    logger.info("State updated with %d known item(s).", len(state["seen"]))

    return 1 if had_source_error else 0


if __name__ == "__main__":
    sys.exit(main())
