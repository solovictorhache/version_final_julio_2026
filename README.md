# radar_convocatorias

Watches configured "convocatorias" (grant/funding call) listing pages and
emails a digest whenever a new one shows up. By default it tracks two
Gobierno de Aragón grant-listing pages (see `sources.json`); edit that file
to add, remove, or retune sources — no code changes needed.

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
> fixture, not against the live `aragon.es` pages (this environment could
> not reach them to inspect the real markup). Run with `--dry-run -v` after
> deploying and check the `INFO`/`WARNING` log lines; if a source logs
> "no configured selector matched", open the page in a browser, inspect the
> listing markup, and add/adjust a selector in `sources.json`.

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

# 6. Once step 4 delivered an email, schedule it weekly (Monday 8:00)
crontab -e
# Add this line at the end of the file that opens (adjust the path to
# match where you cloned/copied the repo, and use an absolute path):
0 8 * * 1 RADAR_SMTP_HOST=smtp.gmail.com RADAR_SMTP_PORT=587 RADAR_SMTP_USER=tu_cuenta@gmail.com RADAR_SMTP_PASSWORD=xxxxxxxxxxxxxxxx RADAR_EMAIL_FROM=tu_cuenta@gmail.com RADAR_EMAIL_TO=victorhugo.perez@unizar.es RADAR_LOG_FILE=/home/solovictorhache/radar/radar.log python3 /home/solovictorhache/radar/radar_convocatorias.py

# 7. Verify the cron entry was saved
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
