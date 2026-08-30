# Discord Bot HSR operations guide

[한국어](operations.kr.md)

Follow the [README](../README.md) for first-time installation and environment setup.

Every `.venv/bin/python` command in this document assumes the host virtualenv created in README step 4. Commands that run inside a Docker container are marked separately with `docker compose run`.

## Environment configuration

Slash commands are registered globally. The bot cannot know in advance which servers it will be invited to, so guild-scoped sync is not used. Global registration can take up to an hour to propagate on Discord's side.

Channels are not configured through environment variables; they are per-server settings. A server administrator with the `Administrator` permission creates the `🎮-디스코-파티` channel with `/설정 시작`, decides the web announcement opt-in with `/설정 공지허용`, and reviews the current values with `/설정 확인`. The party, announcement, and event channels and the forbidden-word filter toggle are stored per guild in `guild_settings.db` through the localhost web admin on the bot host machine. The announcement opt-in is read-only on the web screen. The party channel keeps one game selection panel; pressing a game creates that game's active recruitment panel. Role buttons handle joining and changing roles and a separate `나가기` button leaves. When the last member leaves, the game panel is deleted immediately; panels older than 24 hours are removed by a cleanup loop that runs every 10 minutes (so the actual deletion is **delayed by up to 10 minutes**). After changing `settings/games.json`, restart the bot so the new game configuration and panels take effect. If no dedicated `/이벤트` channel is set, the command works in any server channel.

`/설정 시작` requires the bot's `Manage Channels` permission.

`ADMIN_TOKEN` is optional. The web admin starts only when it is set, and it must be a random value of at least 32 characters. Compose publishes the container's internal `0.0.0.0:8080` only to the host's `127.0.0.1:8080`. Open it at `http://127.0.0.1:8080` from a browser on the machine running the bot; it is not exposed on any other network interface. The HTTP `Host` header is restricted to `127.0.0.1` and `localhost`, and failed login attempts are rate limited. Remote access, reverse proxies, TLS, OAuth, and web access for guild administrators are not supported — design the security model first if you need them. The admin session cookie is host-scoped, not port-scoped. Enable plain-HTTP administration only when you trust every local service and process within the same OS user and loopback host trust boundary.

Web admin announcements are sent only to the designated announcement channel of guilds that opted in through `/설정 공지허용` in Discord. Each announcement may attach one PNG, JPEG, GIF, or WebP image of up to 8 MiB. When sends are skipped or fail, the screen shows the key cause per guild — channel unset or deleted, missing permissions, a Discord error, or a timeout.

Data is never mixed between servers. Forbidden-word counts, parties, settings, and game UID registrations are separated at the schema level by `guild_id`. Even when the same user is on several servers, the counts stay independent per server. The daily AI limit is the exception: it is shared per user across a whole bot instance. DM messages have no server to attribute them to, so they are excluded from forbidden-word tallies, and party panel buttons are rejected outside a server or on any message that is not the latest panel.

When a member leaves a server, that server's forbidden-word count, party membership, and game UID registration are deleted. When the bot is kicked from or leaves a server, `on_guild_remove` deletes all of that server's forbidden-word counts, parties, settings, and game UID registrations. Backup copies taken before deletion are removed after the default retention period of up to 30 days. Other servers' data is unaffected.

### Daily AI limits

| Command | Per-user daily KST limit |
|---|---|
| `/기본대화` | `LIMIT_LIGHT` |
| `/고급대화` | `LIMIT_DEEP` |
| `/이미지` | `LIMIT_IMAGE` |

Limits apply per command, are tallied per user across the whole bot instance, and reset at midnight KST. Adjust each command's count with `LIMIT_LIGHT`, `LIMIT_DEEP`, and `LIMIT_IMAGE` in `.env.runtime`; `/상태` shows how many uses are left today. `AI_COOLDOWN_SECONDS` (15 by default) additionally rate-limits consecutive calls per user. Per-date records older than `AI_USAGE_RETENTION_DAYS` (30 by default) are deleted at the next AI reservation. Restart the bot after changing these values.

The limit is reserved before the API call starts and released if the call fails before it is made. A failure after the provider accepted the call may have incurred cost, so it consumes the limit; but when Gemini rejects the request outright with `429`/`RESOURCE_EXHAUSTED`, the image reservation is released.

**The last line of defence is the OpenAI account budget limit, not the app.** There is no global kill switch in the app, so be sure to set a monthly budget cap in the OpenAI dashboard.

When OpenAI returns `429` together with `credit_balance_exhausted`, that is account credit exhaustion, not model congestion. `/기본대화` and `/고급대화` work again only after topping up OpenAI API billing credit. `/이미지` uses a separate Gemini API; when Gemini returns `429` or `RESOURCE_EXHAUSTED`, the bot asks the operator to check the quota, plan, and billing status in Google AI Studio.

Party `created_at` values from earlier versions that carry no timezone may have been written as UTC or KST depending on how the bot was run. To avoid premature expiry, the migration interprets ambiguous values as UTC, so some existing parties may last up to 9 hours longer than their original expiry — but they are never deleted early.

Party DB v2 stores the host and limits a user to one active party per server. If duplicate membership data exists before the upgrade, only the first party in ascending game-name order is kept, and an existing party's host is decided as the smallest remaining participant user ID. Perform the backup procedure before deploying.

### Server profile bio

Members write it themselves with `/프로필설정 자기소개:<text>`. `/프로필` shows it alongside the server join date and forbidden-word warning count, and the field is omitted entirely when empty. The maximum is 200 characters; leaving the argument blank clears it.

It is stored in the `bio` column of the `users` table in `usage_data.db` (schema v4). The primary key is already `(guild_id, user_id)`, so bios are **separated per server**, and the existing deletion paths remove them when a member leaves or the bot is removed. There is deliberately no way for the operator to write someone else's bio from the web admin.

The forbidden-word filter is not applied to bios. Mention notifications are disabled bot-wide, so a mention in a bio notifies no one.

### Web admin screen language

The screen supports Korean and English. Switch with the `한국어` / `English` links in the top bar (on the login screen, in the brand panel); the links are plain `<a href="?lang=en">` anchors, so no JavaScript is involved.

The language is decided in this order.

1. The `?lang=ko` or `?lang=en` query
2. The `admin_lang` cookie (baked for one year when you switch with the link)
3. The browser's `Accept-Language` header
4. The default, `ko`

Unknown values fall back silently to the default. Screens reached by a redirect after a POST carry no query, so the cookie preserves the language.

All strings live in one place, `STRINGS` in `module/i18n.py`. **The two languages must define exactly the same keys** — a missing key renders as a silent empty string, so tests check key-set equality, that no value is empty, that `{}` placeholders match, and that no key is unused. Server-generated messages, including save results and error text, use the same catalogue.

Bot names and guild names are not translated.

### Web admin session

A login session lasts 8 hours and only one exists at a time (logging in again ends the previous session). The top right of the screen shows the remaining time as `남은 시간: 7시간 52분 (만료 21:14 KST)`, and the `세션 연장` button pushes the expiry another 8 hours out. There is no cap on the number of extensions — this screen is loopback-only with a single session, and a cap would create the worse failure of being forcibly logged out mid-task.

**The remaining time is not a clock ticking down by the second.** It refreshes when you reopen the page or press the extend button. The CSP is `default-src 'self'`, which blocks inline scripts, and widening the CSP for a countdown was not worth it. The web admin screen uses no JavaScript at all.

The screen styles are a single file, `module/static/admin.css`, served at `GET /static/admin.css`. The login screen uses it too, so it sits outside authentication; only that one fixed path is served, so path traversal cannot reach any other file. The bot name shown on screen is taken from the name registered on the Discord Application.

### Forbidden-word filter

The filter is turned on and off per guild from the localhost web admin. The default is on, and the value is stored in `forbidden_filter_enabled` in `guild_settings` schema v6. v2 through v5 databases are upgraded to v6 at startup and existing servers stay enabled. A change made on the web takes effect immediately — the value is cached in the process so the filter does not query the database for every message, and saving on the web invalidates that cache.

Each time a forbidden word is caught, the bot responds and tallies `forbidden_count`.

`settings/forbidden_words.json` is either an array of words or an object holding `words`, `template`, and `allow`. `words` and `allow` hold at most 1,000 entries each with at most 100 characters per entry, and the substituted `template` must be 2,000 characters or fewer. The AI persona system prompt must be 16,000 characters or fewer and the greeting 1,974 or fewer. See the README for the format. Saving from the web admin preserves whichever shape came in.

### Game cards

The `game_uids` table in `game_uid_data.db` holds the UID per `(guild_id, user_id, game)`. It is included in the backup and restore paths along with the other databases.

Game data files are downloaded to `.enka_py/`, relative to the process working directory, on first lookup. Compose bind-mounts this path to `./runtime/enka`, so recreating the container does not re-download them. The directory must be writable by the bot UID.

The `/게임카드` title is `Discord display name · game name`, and the body shows the in-game nickname and account level and the first showcase character with its level across two lines. The character image is loaded by the Discord embed directly from the Enka URL.

If Enka Network is slow or under maintenance, only this command fails and everything else keeps working. After 3 consecutive transport failures, lookups for that game pause for 60 seconds before resuming. Lookup results are cached per UID for 5 minutes.

### Migrating an existing installation's forbidden-word file

Only operators who already have a forbidden-word file need to stop the bot and copy the list once. New installations follow the copy commands in the README instead.

```bash
(
set -euo pipefail
legacy_file=runtime/data/"forbidden_words.json"
target_file=settings/forbidden_words.json
test -f "$legacy_file"
if test -e "$target_file"; then
  echo "The target file already exists. Review it and merge manually: $target_file" >&2
  exit 1
fi
cp -p "$legacy_file" "$target_file"
cmp -s "$legacy_file" "$target_file"
chmod 600 "$target_file"
)
```

## Running with Docker Desktop

The Docker image contains only the dependencies and the `module/` sources. `.env.secrets`, `.env.runtime`, the real forbidden words, databases, backups, and logs are not in the image. Both environment files stay on the host and Compose injects them into the `bot` and `backup` processes. Compose uses the last file in the list for duplicate names, so keep the order `.env.runtime`, `.env.secrets` to give credentials priority. Compose bind-mounts the host's `runtime/data/` and `runtime/backups/`, so data and secrets remain on the host even after the containers are recreated. The `bot` service's `settings` mount is read/write for the web admin's atomic replacement, while the `backup` service's is read-only. There are no published ports, including the web port.

Configure Docker Desktop to start at login. After building the image, apply the non-root `bot` user's UID/GID to every bind mount and restrict directories to `700` and files to `600`. `bot` and `backup` use the same image user, so the bot writes settings and runtime while backup reads settings read-only. Start the services only after the verification succeeds.

```bash
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
sudo find settings runtime -type d -exec chmod 700 {} +
sudo find settings runtime -type f -exec chmod 600 {} +
test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
docker compose run --rm --no-deps --entrypoint sh bot -c '
  test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
  test -r /app/settings/games.json && test -w /app/settings/games.json &&
  test -w /app/runtime/data && test -w /app/runtime/backups'
docker compose run --rm --no-deps --entrypoint sh backup -c '
  test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
docker compose up -d
docker compose logs --tail=100 bot
```

In a development environment without the Docker Compose CLI, the console suite reports the rendered-Compose check as `SKIP`. On CI or a real Docker deployment host, `docker compose config --quiet` must succeed, and this `SKIP` cannot stand in for deployment verification.

When the Mac sleeps, Docker Desktop containers stop too.

## Migrating an existing installation's database filenames

Two database files were renamed to match their contents. The bot looks only for the new names, so an installation that predates this change must move the files **once, with the bot stopped**. Without the move, empty databases are created and the existing records are invisible.

| Before | After | Contents |
|---|---|---|
| `attendance_data.db` | `usage_data.db` | Forbidden-word warning counts and daily AI usage (the attendance feature was already removed) |
| `profile_data.db` | `game_uid_data.db` | Game UIDs |

```bash
docker compose stop bot backup
cd runtime/data
for suffix in "" "-wal" "-shm"; do
  test -e "attendance_data.db$suffix" && mv "attendance_data.db$suffix" "usage_data.db$suffix"
  test -e "profile_data.db$suffix" && mv "profile_data.db$suffix" "game_uid_data.db$suffix"
done
cd -
docker compose up -d bot backup
docker compose logs --tail=50 bot
```

Leave the existing backup filenames in `runtime/backups/` as they are. The manifest records the names as they were, and the restore procedure follows the manifest.

## Backup operations

Run manual checks from the `backup` container.

```bash
docker compose run --rm --no-deps backup python -m module.backup create
docker compose run --rm --no-deps backup python -m module.backup verify
docker compose run --rm --no-deps backup python -m module.backup restore-test
```

`verify` and `restore-test` use the most recent manifest in `runtime/backups/`. Each backup set puts `usage_data.db`, `party_data.db`, `guild_settings.db`, `game_uid_data.db` and whichever of `settings/persona.json`, `settings/forbidden_words.json`, and `settings/games.json` existed at the time into the same manifest. The default backup interval is 21,600 seconds (6 hours) and the retention period is 30 days. The Docker `backup` service uses `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` from `.env.runtime` to create SQLite online backups. After changing the values, recreate the backup service with `docker compose up -d --force-recreate backup`.

The databases run in WAL mode. A WAL database must be able to create `-shm`/`-wal` files **even for a read-only connection**, so the Docker `backup` service's `./runtime/data` mount must be writable rather than `:ro`. If you switch it back to `:ro`, backups fail with `attempt to write a readonly database` only while the bot is stopped (that is, when `-shm` is absent), and `module.backup loop` swallows the exception and silently retries forever. The backup code only reads through the SQLite online backup API, so a read-write mount does not change the data.

`.env.secrets` is not passed to the `backup` service. `module.backup` uses no Discord, OpenAI, or Google credentials and `module/config.py` does not call `validate_config()` at import time, so it starts fine without any token.

Always include `runtime/backups/` in Time Machine or external-disk backups. Databases and backups are not encrypted at the application level, so enable full-disk encryption on the host and keep `0700`/`0600` permissions. When copying to another host or to object storage, apply encryption in transit and at rest with separate key management. Do not put `runtime/data/` itself, which holds the live databases, in a cloud sync folder such as iCloud Drive or Dropbox.

## Exporting data slated for deletion

The migration that removes the points, attendance, and music features drops those tables and columns irreversibly. **Before** upgrading to a version that contains the migration, run the following to save the data as human-readable JSON.

```bash
.venv/bin/python -m module.export_legacy
```

Under Docker, run it in the `bot` container. The output lands in `runtime/backups/`, so it stays on the host.

```bash
docker compose run --rm --no-deps bot python -m module.export_legacy
```

You may also pass the output path as an argument. The default is `runtime/backups/legacy-export-<UTC timestamp>.json`.

Three items are exported.

- `users` — the point balance (`points`) and last attendance date (`last_attendance_date`) per guild and user
- `point_ledger` — the entire point movement ledger
- `music_settings` — the music channel and panel message ID per guild

The original databases are neither read nor written directly. A snapshot is taken through the SQLite online backup API and read from that copy, so it is safe while the bot is running and the result is consistent as of a point in time. Running it against an already-migrated database does not fail; missing items are simply recorded as `null`. The export is created as a `0600` exclusive file and never overwrites an existing file or symlink.

`forbidden_count` and AI usage (`ai_usage`) are not slated for deletion, so they are not in the export and survive the migration unchanged.

## Restoring for real from a verified backup

Restore is not automated. Proceed in this order: stop the services → verify the manifest and run the built-in stage restore → replace the databases → replace each settings file that exists, with confirmation → check file permissions → start the services → check health and logs. Choose `MANIFEST` yourself in the block below and keep an emergency copy before overwriting anything. `stage_restore` verifies the manifest's sizes, checksums, and database integrity, and upgrades only the staged copies to the current schema. If any step fails or a copy is declined, do not start the services.

```bash
(
set -euo pipefail

MANIFEST=runtime/backups/20260728T000000Z-manifest.json

docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose services are still running." >&2
  exit 1
fi

MANIFEST_NAME=${MANIFEST##*/}
test "$MANIFEST" = "runtime/backups/$MANIFEST_NAME"
RESTORE_STAGE_NAME="restore-stage.$(date -u +%Y%m%dT%H%M%SZ).$$"
RESTORE_STAGE="runtime/data/$RESTORE_STAGE_NAME"
docker compose run --rm --no-deps -T --entrypoint python backup \
  - "/app/runtime/backups/$MANIFEST_NAME" "/app/runtime/data/$RESTORE_STAGE_NAME" <<'PY'
import sys
from pathlib import Path
from module.backup import stage_restore
stage_restore(Path(sys.argv[1]), Path(sys.argv[2]))
PY
sudo chown -R "$(id -u):$(id -g)" settings runtime

EMERGENCY_DIR=$(mktemp -d "runtime/emergency.$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
for staged in "$RESTORE_STAGE"/*.db; do
  name=${staged##*/}
  cp -p "runtime/data/$name" "$EMERGENCY_DIR/$name"
  cmp -s "runtime/data/$name" "$EMERGENCY_DIR/$name"
done
ls -l "$EMERGENCY_DIR" "$RESTORE_STAGE"

for staged in "$RESTORE_STAGE"/*.db; do
  name=${staged##*/}
  cp -ip "$staged" "runtime/data/$name"
  cmp -s "$staged" "runtime/data/$name"
done

for name in persona.json forbidden_words.json games.json; do
  staged="$RESTORE_STAGE/settings/$name"
  test -f "$staged" || continue
  cp -ip "$staged" "settings/$name"
  cmp -s "$staged" "settings/$name"
done

SERVICE_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
SERVICE_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
sudo chown -R "$SERVICE_UID:$SERVICE_GID" settings runtime
sudo find settings runtime -type d -exec chmod 700 {} +
sudo find settings runtime -type f -exec chmod 600 {} +
test -z "$(sudo find settings runtime \( ! -uid "$SERVICE_UID" -o ! -gid "$SERVICE_GID" -o -perm -022 \) -print -quit)"
docker compose run --rm --no-deps --entrypoint sh bot -c '
  test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
  test -r /app/settings/games.json && test -w /app/settings/games.json &&
  test -w /app/runtime/data && test -w /app/runtime/backups'
docker compose run --rm --no-deps --entrypoint sh backup -c '
  test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
  test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
docker compose start backup
docker compose start bot
BOT_CONTAINER_ID=$(docker compose ps -q bot)
test -n "$BOT_CONTAINER_ID"
docker inspect --format '{{.State.Health.Status}}' "$BOT_CONTAINER_ID"
docker compose logs --tail=100 bot
)
```

Then check `/프로필`, `/게임카드`, and the per-game panels in the party channel from Discord. If anything is wrong, stop the bot again immediately and preserve the emergency copy and the restore stage.

## Linting

The import-ordering rules are defined by `ruff.toml` at the repository root. The linter is not a runtime dependency, so it is not in `requirements.txt`/`requirements.lock`; run it through `uv` as needed.

```bash
uv tool run ruff check --select I module test          # check
uv tool run ruff check --select I --fix module test    # sort automatically
```

## Deployment

Deploy accumulated changes in one pass, in this order: **create and verify an online backup with the current code → pull → test and build → restart bot and backup.** This keeps a backup of the old-schema database from before the new code's migration. The block prints the pre-pull commit first and exits before restarting if any test or backup command fails.

Each deployment and rollback workflow checks that the host virtualenv executable exists before touching Git.

Docker:

```bash
(
set -euo pipefail
test -x .venv/bin/python
git rev-parse HEAD
BACKUP_MANIFEST=$(docker compose run --rm --no-deps backup python -m module.backup create | tail -n 1)
test -n "$BACKUP_MANIFEST"
docker compose run --rm --no-deps backup python -c 'from pathlib import Path; from module.backup import verify_backup_set; import sys; verify_backup_set(Path(sys.argv[1]))' "$BACKUP_MANIFEST"
docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose services are still running." >&2
  exit 1
fi

BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
HOST_UID=$(id -u)
HOST_GID=$(id -g)
restore_bot_mounts() {
  sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
  sudo find settings runtime -type d -exec chmod 700 {} +
  sudo find settings runtime -type f -exec chmod 600 {} +
}
verify_bot_mounts() {
  test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
  docker compose run --rm --no-deps --entrypoint sh bot -c '
    test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
    test -r /app/settings/games.json && test -w /app/settings/games.json &&
    test -w /app/runtime/data && test -w /app/runtime/backups'
  docker compose run --rm --no-deps --entrypoint sh backup -c '
    test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
}
trap restore_bot_mounts EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo chown -R "$HOST_UID:$HOST_GID" settings
git pull --ff-only
.venv/bin/python -m test.console_tests
.venv/bin/python -m unittest test.test_discord_commands
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
restore_bot_mounts
verify_bot_mounts
trap - EXIT HUP INT TERM
docker compose up -d --no-deps bot backup
docker compose logs --tail=100 bot
)
```

## Code rollback

If startup fails, return to the pre-pull commit the block printed. After reviewing the change, put the offending commit in `TARGET_COMMIT` for `ROLLBACK_MODE=revert`, or the previous commit for `ROLLBACK_MODE=checkout`, which recovers only this host temporarily. `git reset --hard` is never used.

Docker:

```bash
(
set -euo pipefail
test -x .venv/bin/python
ROLLBACK_MODE=revert
TARGET_COMMIT=replace-with-reviewed-commit

docker compose stop bot backup
running_services=$(docker compose ps --status running --services)
if grep -Eq '^(bot|backup)$' <<<"$running_services"; then
  echo "Compose services are still running." >&2
  exit 1
fi

BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
HOST_UID=$(id -u)
HOST_GID=$(id -g)
restore_bot_mounts() {
  sudo chown -R "$BOT_UID:$BOT_GID" settings runtime
  sudo find settings runtime -type d -exec chmod 700 {} +
  sudo find settings runtime -type f -exec chmod 600 {} +
}
verify_bot_mounts() {
  test -z "$(sudo find settings runtime \( ! -uid "$BOT_UID" -o ! -gid "$BOT_GID" -o -perm -022 \) -print -quit)"
  docker compose run --rm --no-deps --entrypoint sh bot -c '
    test -r /app/settings/persona.json && test -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -w /app/settings/forbidden_words.json &&
    test -r /app/settings/games.json && test -w /app/settings/games.json &&
    test -w /app/runtime/data && test -w /app/runtime/backups'
  docker compose run --rm --no-deps --entrypoint sh backup -c '
    test -r /app/settings/persona.json && test ! -w /app/settings/persona.json &&
    test -r /app/settings/forbidden_words.json && test -r /app/settings/games.json'
}
trap restore_bot_mounts EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
sudo chown -R "$HOST_UID:$HOST_GID" settings
case "$ROLLBACK_MODE" in
  revert) git revert "$TARGET_COMMIT" ;;
  checkout) git checkout "$TARGET_COMMIT" ;;
  *) echo "ROLLBACK_MODE must be revert or checkout." >&2; exit 1 ;;
esac

.venv/bin/python -m test.console_tests
.venv/bin/python -m unittest test.test_discord_commands
docker compose config --quiet
docker compose build bot
BOT_UID=$(docker compose run --rm --no-deps --entrypoint id bot -u)
BOT_GID=$(docker compose run --rm --no-deps --entrypoint id bot -g)
restore_bot_mounts
verify_bot_mounts
docker compose run --rm --no-deps backup python -m module.backup verify
trap - EXIT HUP INT TERM
docker compose up -d --no-deps bot backup
)
```

Follow the manual restore procedure above separately, only if the database also needs to be restored.

## Host limitations

- Mac sleep stops Docker Desktop containers.
- The Docker approach requires Docker Desktop to start at login.
- A network or power failure takes the bot offline.
