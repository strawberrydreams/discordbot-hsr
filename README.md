# Hyacine — Discord Bot HSR

[한국어](README.kr.md)

Hyacine is a **self-hosted community utility bot** for Korean-speaking Discord servers. It provides party recruitment, server events, forbidden-word moderation, GPT conversation and image generation, and game profile cards. It uses Python 3.12 and SQLite. The container and CI are pinned to 3.12, so that is the verified combination — building the host virtualenv on a newer interpreter makes `requirements.lock` additionally pull `audioop-lts` on 3.13 and above. It runs on **POSIX hosts** such as macOS and Linux; on Windows, run it under WSL2 (see [Runtime environment](#runtime-environment) below).

Every operator creates their own Discord Application and token and runs their own instance. The repository owner does not host the bot for anyone else and does not administer anyone's Discord server. Hyacine is an unofficial *Honkai: Star Rail* fan project and is neither affiliated with nor endorsed by HoYoverse.

Forbidden-word counts, parties, guild settings, and game UID registrations are isolated per server by `guild_id`. The AI usage limit is the one explicit exception: it is per user and shared across the whole bot instance.

## Features

- `/프로필` — server join date, forbidden-word warning count, and bio
- `/프로필설정` — the bio shown on this server (up to 200 characters; empty clears it)
- Game selection panel — choosing a game in `🎮-디스코-파티` creates that game's recruitment panel
- `/이벤트` — a dedicated per-server channel can be selected in the web admin; unset means every channel
- `/기본대화`, `/고급대화`, `/이미지`
- `/상태` — this channel's AI conversation state and how many AI uses are left today
- Forbidden-word warnings — can be turned off per server, with a configurable response template and allow list
- `/인사` — a static greeting that works without any AI key
- `/등록`, `/등록해제`, `/게임카드` — showcase character cards for Genshin Impact, Honkai: Star Rail, and Zenless Zone Zero
- `/설정 시작`, `/설정 공지허용`, `/설정 확인` — a server administrator with the `Administrator` permission creates the default party channel, decides whether to receive host announcements, and reviews the current settings

Slash command names are the user interface and stay in Korean.

## Quick start

### Runtime environment

The install and run commands below assume a **POSIX host**. They work as written in a **macOS or Linux terminal**, and production runs through **Docker Compose** on any OS.

**On Windows, run under WSL2.** The bot uses Unix-only facilities (file locks, owner permission checks, atomic file replacement), so it does not run on native Windows Python. Use one of these two options.

- **Docker Desktop (WSL2 backend)** — use the Docker Compose procedure in steps 4 and 5 below as written. The container is Linux, so no code changes are needed. Enable WSL2 integration when installing Docker Desktop.
- **A WSL2 distribution shell (Ubuntu or similar)** — open the WSL2 shell and run every step below exactly as on macOS or Linux. Use this shell too when running the bot directly from the host virtualenv (`.venv`).

Either way, **clone the repository inside the WSL Linux filesystem** (for example `~/discordbot-hsr`). On a Windows-side path (`/mnt/c/…`), `chmod`, ownership, and bind-mount permissions are not mapped the Linux way, so the env-file permission check and the ownership setup in Compose step 5 will fail.

### 1. Prepare a Discord Application and token

Create your own Discord Application and bot token in the Discord Developer Portal. In the Bot settings, enable the privileged intents `Message Content` and `Server Members`. The first is required by the forbidden-word filter; the second is required to clean up party membership, forbidden-word counts, and game UID registrations when a member leaves. For an operator-only app, turn `Public Bot` off. These are Portal settings — the code in this repository does not change Portal settings. Guild installation happens in step 6, after the bot is running.

### 2. Write the environment files

Create two files based on `.env.example` and fill in the values before moving to the next step.

- `.env.secrets`: the Discord token you just created, plus optional OpenAI and Google credentials
- `.env.runtime`: data and backup paths, backup interval, AI cooldown and limits

To enable the web admin, generate `ADMIN_TOKEN` as a 32-byte random value as shown below and put it in `.env.secrets`. The bot refuses to start if it is shorter than 32 characters.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```bash
touch .env.secrets .env.runtime
chmod 600 .env.secrets .env.runtime
```

Before startup the bot checks that both files are regular files owned by the current user with no group or world permissions; if either is a symlink or has loose permissions, it exits without reading the secrets.

### 3. Initialize the settings files

Copy all three examples.

```bash
cp settings/persona.example.json settings/persona.json
cp settings/forbidden_words.example.json settings/forbidden_words.json
cp settings/games.example.json settings/games.json
mkdir -p runtime/data runtime/backups runtime/logs runtime/enka
```

### 4. Host test environment and image build

Install the host virtualenv for the two test suites that deployment and rollback run, then build the image.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -r requirements-audit.txt
.venv/bin/python -m pip_audit -r requirements.lock
docker compose config --quiet
docker compose build bot
```

### 5. Set bind-mount ownership and permissions, then start

Apply the image's non-root `bot` UID/GID to the host bind mounts. `bot` and `backup` use the same image, so this ownership lets the bot write settings and runtime while backup reads settings read-only. Do not start the services until every check below succeeds.

```bash
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

### 6. Guild installation and Discord setup

In the Developer Portal, Installation must use **Guild Install only**, with the scopes `bot`, `applications.commands`. Grant only the following bot permissions, then install into the guild.

- `View Channel`
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Attach Files`
- `Manage Channels` — creates the bot-only category and the party channel

After installation, a server administrator with the `Administrator` permission runs `/설정 시작` in Discord. Channel IDs are not environment variables: the party, announcement, and event channels and whether the forbidden-word filter is enabled are stored per guild in the localhost web admin on the bot host machine. Consent to receive host announcements is changed only by the server administrator through `/설정 공지허용` in Discord, and `/설정 확인` shows the current state.

Slash commands are registered globally, so it can take up to an hour after an invite for them to appear. When a member leaves a server, their forbidden-word count, party membership, and game UID registration for that server are deleted; when the bot is removed from a server, that server's forbidden-word counts, parties, settings, and game UID registrations are all deleted. Backup copies taken before deletion are removed after the default retention period of up to 30 days.

### Panels and optional features

The `🎮-디스코-파티` channel initially shows a single game selection panel. Whoever presses a game button becomes the host, and that game's roster panel appears. Role buttons handle joining and role changes, and a separate `나가기` button leaves the party. When the last member leaves, that game's panel disappears immediately; panels older than 24 hours are removed by a cleanup loop that runs every 10 minutes — so actual deletion can be **up to 10 minutes later** than the 24-hour mark. Restart the bot after changing the game or role configuration.

#### Forbidden-word filter

The actual word list lives in `settings/forbidden_words.json`. The list applies to a whole bot instance, while warning counts are tallied per server.

**Confirm this behaviour before installing.** What reads as a joke among friends can land very differently on a server with strangers.

- The original message is **not deleted.** The bot posts one additional response in the same channel.
- The matched word is **echoed verbatim in the response body.**
- The author is mentioned. Mention notifications are disabled bot-wide, so the mention **renders but sends no notification.**
- Messages sent by bots are not checked. Neither are webhook messages.
- The bot responds on every match and tallies the warning count.
- The bot host operator can turn it off per guild from the localhost web admin. The default is on.

The document may be an array of words, or an object that also carries the response template and the allow list. Existing array files keep working, so there is no need to change them.

```json
{
  "words": ["금지어"],
  "template": "🛑 {mention} 님, {word} 는 금지입니다.",
  "allow": ["금지어가 들어간 멀쩡한 표현"]
}
```

- Only `{mention}` and `{word}` are substituted in `template`. Any other braces are left as literal text. Omit the field to use the default message.
- Matching is a substring match after whitespace and symbols are stripped, so false positives happen. A match found inside an expression listed in `allow` is ignored. The default is empty, so add false positives as you encounter them.
- `words` and `allow` hold at most 1,000 entries each, with at most 100 characters per normalized entry. `template` must stay within Discord's 2,000-character limit once the longest mention and word are substituted.

#### Game cards

After registering an account with `/등록 <game> <UID>`, `/게임카드 <game>` shows the Discord display name and the game name in the title, and the in-game nickname, account level, and the first showcase character with its level across two lines in the body. The data comes from [Enka Network](https://enka.network).

- UID registration is **per server.** Registering on one server does not carry over to another, and where you registered is not visible across servers.
- Existence is verified against Enka at registration time, so a nonexistent UID cannot be registered.
- Enka returns **only characters placed in the in-game showcase.** If the UID is correct but the showcase is empty the card comes back empty, and the bot then points to that game's showcase setting.
- Responses are cached per UID for a few minutes. If Enka is slow or under maintenance only this command fails; the rest of the bot keeps working.
- The card is rendered by the Discord embed pulling Enka's first character image directly.
- Game data files are downloaded to `.enka_py/` (`runtime/enka/` under Compose) on first lookup. That directory must be writable.

Hyacine is not affiliated with HoYoverse and is unrelated to Enka Network.

`ADMIN_TOKEN` is optional. If it is empty the web admin does not start; when set, it must be a random value of at least 32 characters. Running directly on the host, the web admin binds to `127.0.0.1:8080`. Under Docker Compose it listens on `0.0.0.0:8080` inside the container and is published only to the host's `127.0.0.1:8080`, so either way you open it at `http://127.0.0.1:8080` from a browser on the machine running the bot, and it is not exposed on any other network interface. The HTTP `Host` header is restricted to `127.0.0.1` and `localhost`, and failed logins are rate limited. Remote access, reverse proxies, TLS, OAuth, and web access for guild administrators are not supported. If you need them, design the security model first.

The web admin edits the AI persona (system prompt and greeting), the forbidden-word list, and the party game list. The system prompt allows up to 16,000 characters and the greeting up to 1,974. Forbidden words are reloaded as soon as they are saved, the persona applies from newly started AI channel sessions, and the game list applies after a bot restart. The same screen saves each guild's party, announcement, and event channels and the forbidden-word filter toggle field by field, and shows the announcement opt-in read-only. Announcements can send an embed plus one optional image (up to 8 MiB) to the designated channel of each guild that opted in through Discord, and the compose form includes a Discord Markdown syntax guide. When a send is skipped or fails, the result is shown along with the key cause for each guild. The screen is available in Korean and English, switchable from the top bar.

AI commands apply a per-user daily KST limit separately to each command. Adjust them with `LIMIT_LIGHT`, `LIMIT_DEEP`, and `LIMIT_IMAGE` in `.env.runtime`; they reset at midnight KST. The limit is shared per user across every guild within the same bot instance. Per-date usage records are deleted on the next AI reservation after `AI_USAGE_RETENTION_DAYS` (30 by default). Independently of the app-level limit, also set a budget cap on your OpenAI and Google provider accounts.

When OpenAI returns `credit_balance_exhausted`, `/기본대화` and `/고급대화` tell the user that the operator needs to top up API credit. When the Google Gemini API returns `429` or `RESOURCE_EXHAUSTED` for `/이미지`, a separate message asks the operator to check the quota or billing limit.

Production runs on Docker Compose. Follow the [operations guide](docs/operations.md) for backup and restore procedures.

## Operating cautions

- Never add `.env.secrets`, `.env.runtime`, or `runtime/` to Git. Do not use `git add -f` either.
- SQLite is the only supported production backend.
- Keep `settings/` and `runtime/` owner-only and enable full-disk encryption on the host. When moving backups to another host or to object storage, apply encryption in transit and at rest separately.
- The plain-HTTP admin session cookie is bound to the host, not the port. Set `ADMIN_TOKEN` only when you trust every local service and process within the OS user and loopback host boundary where the localhost web admin runs.

## Licence and contributions

Hyacine is distributed under the [GNU General Public License v3.0](LICENSE). This repository does not accept user contributions.

Because [`enka`](https://pypi.org/project/enka/), which the profile cards use, is GPL-3.0, any distribution that links this library becomes GPL-3.0 as a whole. Operators using an earlier version that was MIT may continue under the MIT terms from the history before that commit. If you fork and distribute, you must follow the GPL-3.0 terms, including the obligation to publish source.

## Detailed operations

See the [operations guide](docs/operations.md) for running Docker Compose, backups, restores, deployment, and rollback. Release-by-release changes are in the [CHANGELOG](docs/CHANGELOG.md).
