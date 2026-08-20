# Automatizaciones BeeStation

Two independent scripts, meant to run from cron on the BeeStation:

- **`radar_convocatorias.py`** — watches grant/funding-call listing pages
  and emails new ones.
- **`recordatorios.py`** — a personal reminder tracker (meetings, events,
  "I need to send this email") that emails a digest when something is due,
  with configurable advance notice and daily nagging on overdue items.

Both share the same "only mark as sent after a successful email" pattern,
so a failed send is retried on the next run instead of silently lost.

## radar_convocatorias.py

Watches configured "convocatorias" (grant/funding call) listing pages and
emails a digest whenever a new one shows up. `sources.json` ships with seven
sources by default — edit that file to add, remove, or retune sources; no
code changes needed.

| Source | Scope |
|---|---|
| Gobierno de Aragón — Subvenciones, premios y otras convocatorias | Regional (Aragón) |
| Gobierno de Aragón — Ayudas y subvenciones (listado de trámites) | Regional (Aragón) |
| Universidad de Zaragoza — Convocatorias de investigación | Unizar (internal) |
| Agencia Estatal de Investigación (AEI) — Buscador de convocatorias | National (Ministerio) |
| BOE — Últimas ayudas y subvenciones publicadas | National (Ministerio) |
| Unión Europea — EU Funding & Tenders Portal (Calls for proposals) | EU |
| AECID — Subvenciones | Cooperación al desarrollo |

## How it works

- `sources.json` lists the pages to watch, each with a fallback chain of
  CSS selectors used to find listing entries (the first selector that
  matches enough items wins, so minor page redesigns don't break it).
- Every run fetches each source, extracts `(title, link)` pairs, and
  compares them against `state.json` (a set of previously-seen items).
- Items not seen before are emailed as a digest, grouped by source.
- `state.json` is only updated *after* the email is sent successfully, so a
  failed send is retried (and re-reported) on the next run instead of being
  silently dropped.

> **Note:** the default selectors in `sources.json` are a best-effort
> starting point — they were written and unit-tested against a local
> fixture, not against the live pages (this environment's network egress is
> blocked to `aragon.es`, `unizar.es`, `aei.gob.es`, `boe.es`, `ec.europa.eu`
> and `aecid.es`, so none of it could be inspected directly). Run with
> `--dry-run -v` after deploying and check the `INFO`/`WARNING` log lines;
> if a source logs "no configured selector matched", open the page in a
> browser, inspect the listing markup, and add/adjust a selector in
> `sources.json` (each source also carries an optional `"notes"` field with
> known caveats — the script ignores it, it's just a note to whoever edits
> the file).
>
> Two sources need special attention because they're likely **JavaScript
> single-page apps** whose content this scraper (plain HTTP GET +
> BeautifulSoup, no JS execution) probably can't see in the static HTML:
> - **AEI — Buscador de convocatorias**: may render results via an API call;
>   if `--dry-run` finds nothing, look for a plainer static listing page on
>   `aei.gob.es` to point at instead.
> - **EU Funding & Tenders Portal**: this is confirmed to be an Angular app
>   that loads calls via JS/API after the initial page load — the static
>   scraper will almost certainly find 0 items here. It's included as a
>   placeholder; making it work for real needs either a headless-browser
>   fetch (e.g. Playwright) or the portal's public search API, neither of
>   which this script currently implements.

## recordatorios.py

A personal reminder tracker. Reminders live in `recordatorios.json` (start
from `recordatorios.example.json`, or create entries with `--add`). Each
reminder has a due date and a list of "notify me N days before" offsets
(e.g. `[7, 1, 0]` = a week before, a day before, and the day itself).

- Running with no flags checks today's date against every pending reminder
  and emails a digest of what's due — grouped into **overdue** (nags every
  run until you mark it done) and **próximos** (each advance offset fires
  once, tracked in `recordatorios_state.json`).
- `recordatorios.json` and `recordatorios_state.json` are gitignored (they
  hold your personal data) — only the `.example.json` template is tracked.

### Managing reminders

```bash
# Add one
python3 recordatorios.py --add --title "Enviar email de seguimiento" \
    --date 2026-08-20 --type email --notify-days 7,1,0 \
    --notes "Adjuntar el borrador de memoria"

# List everything (pending + done), with days remaining/overdue
python3 recordatorios.py --list

# Mark one done (stops it from nagging)
python3 recordatorios.py --done <id>

# See what would be emailed today, without sending or touching state
python3 recordatorios.py --dry-run -v
```

`--type` is free-form labeling (`evento`, `reunion`, `email`, `tarea` are
the ones used above) — it's only used to prefix the reminder in the email,
so any short word works.

### Environment variables

| Variable                       | Required | Purpose                        |
|----------------------------------|:--------:|-----------------------------------|
| `RECORDATORIOS_SMTP_HOST`        | yes      | SMTP server hostname               |
| `RECORDATORIOS_SMTP_PORT`        | yes      | SMTP port (587 for STARTTLS)       |
| `RECORDATORIOS_SMTP_USER`        | yes      | SMTP auth username                 |
| `RECORDATORIOS_SMTP_PASSWORD`    | yes      | SMTP auth password / app password  |
| `RECORDATORIOS_EMAIL_FROM`       | yes      | `From:` address                    |
| `RECORDATORIOS_EMAIL_TO`         | yes      | Comma-separated recipient list      |
| `RECORDATORIOS_FILE`             | no       | Override path to `recordatorios.json` |
| `RECORDATORIOS_STATE_FILE`       | no       | Override path to `recordatorios_state.json` |
| `RECORDATORIOS_LOG_FILE`         | no       | Also log to this file              |

These can reuse the same Gmail account/app password as `RADAR_SMTP_*` —
just repeat the same values under the `RECORDATORIOS_` names in the cron
line below.

No extra dependencies needed — `recordatorios.py` only uses the Python
standard library.

## Deploying on the BeeStation (or any Linux box with SSH + cron)

```bash
# 1. Connect over SSH
ssh solovictorhache@192.168.0.12

# 2. Get the code onto the NAS (pick one)
git clone https://github.com/solovictorhache/version_final_julio_2026 ~/radar
# ...or scp the files from your Mac:
#   scp radar_convocatorias.py sources.json requirements.txt \
#       solovictorhache@192.168.0.12:~/radar/
cd ~/radar

# 3. Install dependencies
pip3 install --user -r requirements.txt

# 4. Manual test run (fill in real SMTP credentials/recipient).
#    --reset-state forces every currently-listed item to be treated as new,
#    so a successful run should always produce an email on this first try.
RADAR_SMTP_HOST=smtp.gmail.com \
RADAR_SMTP_PORT=587 \
RADAR_SMTP_USER=tu_cuenta@gmail.com \
RADAR_SMTP_PASSWORD=xxxxxxxxxxxxxxxx \
RADAR_EMAIL_FROM=tu_cuenta@gmail.com \
RADAR_EMAIL_TO=victorhugo.perez@unizar.es \
python3 ~/radar/radar_convocatorias.py --reset-state -v

# 5. If nothing arrives, first check what the scraper actually saw:
python3 ~/radar/radar_convocatorias.py --dry-run -v

# 6. Once step 4 delivered an email, schedule radar_convocatorias.py weekly
#    (Monday 8:00), and recordatorios.py daily (8:00) so due reminders/
#    nags go out every morning.
crontab -e
# Add these lines at the end of the file that opens (adjust the path to
# match where you cloned/copied the repo, and use an absolute path):
0 8 * * 1 RADAR_SMTP_HOST=smtp.gmail.com RADAR_SMTP_PORT=587 RADAR_SMTP_USER=tu_cuenta@gmail.com RADAR_SMTP_PASSWORD=xxxxxxxxxxxxxxxx RADAR_EMAIL_FROM=tu_cuenta@gmail.com RADAR_EMAIL_TO=victorhugo.perez@unizar.es RADAR_LOG_FILE=/home/solovictorhache/radar/radar.log python3 /home/solovictorhache/radar/radar_convocatorias.py

0 8 * * * RECORDATORIOS_SMTP_HOST=smtp.gmail.com RECORDATORIOS_SMTP_PORT=587 RECORDATORIOS_SMTP_USER=tu_cuenta@gmail.com RECORDATORIOS_SMTP_PASSWORD=xxxxxxxxxxxxxxxx RECORDATORIOS_EMAIL_FROM=tu_cuenta@gmail.com RECORDATORIOS_EMAIL_TO=victorhugo.perez@unizar.es RECORDATORIOS_LOG_FILE=/home/solovictorhache/radar/recordatorios.log python3 /home/solovictorhache/radar/recordatorios.py

# 7. Verify the cron entries were saved
crontab -l
```

### Environment variables

| Variable              | Required | Purpose                                             |
|------------------------|:--------:|------------------------------------------------------|
| `RADAR_SMTP_HOST`      | yes      | SMTP server hostname                                  |
| `RADAR_SMTP_PORT`      | yes      | SMTP port (587 for STARTTLS)                          |
| `RADAR_SMTP_USER`      | yes      | SMTP auth username                                    |
| `RADAR_SMTP_PASSWORD`  | yes      | SMTP auth password / app password                     |
| `RADAR_EMAIL_FROM`     | yes      | `From:` address                                       |
| `RADAR_EMAIL_TO`       | yes      | Comma-separated recipient list                         |
| `RADAR_SOURCES_FILE`   | no       | Override path to `sources.json`                       |
| `RADAR_STATE_FILE`     | no       | Override path to `state.json`                          |
| `RADAR_LOG_FILE`       | no       | Also log to this file, in addition to stdout          |

Gmail note: if `RADAR_SMTP_USER` is a Gmail account with 2-Step
Verification enabled, `RADAR_SMTP_PASSWORD` must be a 16-character
[App Password](https://myaccount.google.com/apppasswords), not the normal
account password.

### CLI flags

```
python3 radar_convocatorias.py [--sources-file PATH] [--state-file PATH]
                                [--reset-state] [--dry-run] [-v]
```

- `--reset-state`: discard `state.json` before running, so every currently
  listed item counts as new (useful for the first test run).
- `--dry-run`: fetch and print what would be emailed, without sending mail
  or touching `state.json`.
- `-v`: debug-level logging (shows which selector matched, item counts per
  source, etc).
