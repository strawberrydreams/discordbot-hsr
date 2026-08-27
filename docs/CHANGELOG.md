# Changelog

[한국어 미러](CHANGELOG.kr.md)

This changelog records how Hyacine evolved as a product, with an emphasis on
user-visible behavior, compatibility, and operational impact rather than a list
of commits.

The repository did not use release tags during the period covered below. The
version numbers are therefore retrospective milestone labels, and each date is
the last change included in that milestone.

## [Unreleased]

### Added

- The web admin can save party, announcement, and event channels plus the
  forbidden-filter toggle per guild, and includes a Discord syntax guide.

### Improved

- Restricted `/설정` subcommands to members with the `Administrator`
  permission.
- Game cards now show the Discord nickname and game name in the title, followed
  by the game account and first showcase character on two body lines.
- Removed the separate image API explanation from exhausted OpenAI credit
  messages.
- Kept `/설정 시작`, `/설정 공지허용`, and `/설정 확인`, moving channel and
  forbidden-filter controls to the localhost web admin. Only guild administrators
  can change announcement consent in Discord, and skipped or failed web operations
  now include a concise cause.
- Added actionable guidance for exhausted Gemini image quota or billing limits.
- Restricted SQLite, backup, export, and generated-image files to the owner, and
  hardened environment-file loading, localhost authentication, and dependency
  auditing.

### Fixed

- Fixed web admin requests failing when current aiohttp versions invoked the
  security-header middleware.
- Fixed party-channel save failures discarding panel recovery state or reporting
  success, and stopped rejected Gemini quota requests consuming a user's daily
  image allowance.
- Restored the Python-version marker required for reproducible Python 3.12 image
  builds, upgraded the vulnerable `cryptography` lock entry, and rejected non-ASCII
  game UIDs before contacting Enka.

### Breaking Changes

- Removed attendance, point-economy, and music features together with their
  commands and persistent fields. Before upgrading an older deployment, run
  `python -m module.export_legacy` as described in the operations guide; startup
  migration then removes the retired tables and columns.

## [0.7.0] - 2026-08-21 — Game cards and selective party recruitment

### Added

- Added one game selector to the party channel; a recruitment panel now appears
  only for the selected game. Every active panel has an explicit leave button.
- Added per-guild web announcement channels and one optional image attachment.

### Improved

- Renamed `/프로필카드` to `/게임카드` and now display the first in-game showcase
  character's Enka image directly in a Discord embed.
- Made `/상태` private to the invoking user and clarified that daily quotas are
  per-user across the bot instance.
- Existing default `🎮-파티` channels are renamed to `🎮-디스코-파티` on restart.

### Fixed

- Removed the ten-second forbidden-word response cooldown.
- Distinguished exhausted OpenAI credit from transient rate limiting and explain
  that the separate Gemini image command remains available.

### Breaking Changes

- `/프로필카드` has been replaced by `/게임카드`.
- Guild settings migrate automatically to schema v5. Guilds using web
  announcements must select a channel once with `/설정 공지채널`.

## [0.6.0] - 2026-08-11 — Host-operated community service

Hyacine grew from a Discord-only bot into a service that an operator can manage
locally without editing files for every routine change.

### Added

- Added optional voice playback with a persistent music control panel, so a
  guild can manage a URL queue from Discord without a separate music bot.
- Added a localhost-only web administration page, enabled by `ADMIN_TOKEN`, so
  the host operator can edit the AI persona, forbidden words, and game roster.
- Added a guild overview and opt-in host announcements, so one self-hosted bot
  can be operated across several communities without broadcasting by default.
- Added `/설정 음악채널` and music panel state to guild settings, so each guild
  can choose where its controls live.

### Improved

- Music controls became persistent across restarts and now enforce requester
  and administrator permissions for queue removal, skipping, pausing, and
  stopping.
- Settings edited through the web page are validated and written atomically;
  forbidden words reload immediately, while persona and game changes take
  effect at their documented lifecycle boundaries.
- Docker Compose now publishes the administration page only on host loopback,
  making the same local operator workflow available in container deployments.

### Fixed

- Slow media resolution no longer blocks the bot's event loop or makes other
  commands unresponsive.
- Persistent music and party panels reject stale interactions and recover more
  reliably after restarts or channel changes.
- Web administration now rejects unsafe filesystem layouts, invalid sessions,
  cross-site requests, oversized input, and invalid target guilds.
- Settings read failures are surfaced to the operator instead of silently
  replacing existing values with defaults.

### Breaking Changes

- None. The web interface and music extension are both opt-in; installations
  that leave `ADMIN_TOKEN` unset or omit `PyNaCl`/`yt-dlp` keep the existing bot
  behavior without a required data migration.

### Internal

- Added focused command, web-security, filesystem, and panel-state regression
  coverage for the new operator surfaces.
- Reused the bot image for the Compose backup service while keeping provider
  credentials out of that service.

## [0.5.0] - 2026-08-04 — Public self-hosting and persistent panels

The project was prepared for operators outside the original server, replacing
source-code customization with validated settings and guild-owned setup.

### Added

- Added example-backed `settings/persona.json`,
  `settings/forbidden_words.json`, and `settings/games.json` files, so public
  installations can customize behavior without modifying Python modules.
- Added `/설정 시작`, `/설정 파티채널`, `/설정 공지허용`, and `/설정 확인`, so
  guild administrators can configure their own server from Discord.
- Added persistent game panels that restore after restart, so party recruitment
  remains available without repeatedly issuing setup commands.
- Added configurable, per-user KST daily limits for light chat, deep chat, and
  image generation, so provider spending has an application-level guard in
  addition to point costs.
- Added SQLite schema versions and settings-inclusive backups, so upgrades and
  restores can validate the data format they are handling.
- Added the MIT license and a self-hosting-oriented installation guide, so the
  project can be distributed with clear operating expectations.

### Improved

- Only `DISCORD_TOKEN` is required for core startup; AI and image extensions are
  skipped when their provider keys are absent, so non-AI deployments remain
  usable.
- AI model names, cooldowns, costs-related limits, data paths, and backup policy
  moved to runtime configuration where operators can change them safely.
- Backups now include operator JSON settings alongside verified SQLite
  snapshots, giving a restore enough information to reproduce bot behavior.
- The event command can be used from any guild channel and presents a stable,
  deterministic event list.

### Fixed

- Missing, malformed, non-UTF-8, or partially invalid settings no longer take
  down unrelated extensions; errors are reported and safe fallbacks are used.
- Game names, role lists, player limits, and Discord component sizes are
  validated before panels are created.
- Legacy database upgrade paths are preserved and newer unsupported schemas
  fail clearly instead of being opened with an incompatible layout.
- Panel setup defers slow interactions, keeps administrative responses private,
  and enforces one current panel per guild and game.

### Breaking Changes

- The party command API `/모집`, `/파티`, `/나가기`, and `/변경` was replaced by
  persistent game-panel buttons. Operators must configure a party channel with
  `/설정` after upgrading.
- Hard-coded game and persona configuration was replaced by the three JSON
  files under `settings/`. The legacy forbidden-word file at
  `runtime/data/forbidden_words.json` must be moved to
  `settings/forbidden_words.json`.
- Channel IDs moved from environment variables into `guild_settings.db`; each
  guild now owns its party-channel and announcement preferences.
- Party schema v2 added `host_id`, restricts each user to one active party per
  guild, and makes role occupancy unique. During migration, duplicate legacy
  memberships are reduced to one deterministic entry and a host is assigned.
- `attendance_data.db` gained an `ai_usage` table keyed by user, KST date, and
  command. AI usage is intentionally shared across guilds even though point
  balances remain guild-scoped.
- The guild settings data model changed from legacy recruit/event channel fields
  to party channel, music channel, panel message, and announcement opt-in
  fields. Supported legacy rows are migrated automatically.

### Internal

- Introduced shared JSON validation and atomic-write contracts used by both
  runtime loading and later operator tooling.
- Expanded keyless tests, schema migration tests, backup staging checks, and
  public-distribution contract checks.

## [0.4.0] - 2026-07-31 — Reliable multi-guild production operation

Hyacine moved from a single-server personal deployment to a locally operated,
multi-guild bot with recoverable data and explicit economic rules.

### Added

- Added verified online SQLite backups, retention, manifests, and staged restore
  checks, so operators can recover data without trusting an unverified copy.
- Added Docker Compose and macOS LaunchAgent deployment paths with startup
  validation, logs, and an operations runbook.
- Added an append-only point ledger, so every charge, reward, and refund can be
  reconciled against a user's balance.
- Added per-user AI cooldowns and bounded chat sessions, so one user or channel
  cannot grow memory or request pressure without limit.
- Added guild-scoped settings and data ownership, so one bot instance can serve
  multiple Discord servers without mixing their points, moderation counts, or
  parties.

### Improved

- Personal command prompts and responses became ephemeral where appropriate,
  reducing accidental disclosure in shared channels.
- Party interactions survive restarts, release slots when members leave, and
  enforce creation, capacity, role, and membership rules at the database layer.
- Finance requests moved off the event loop and gained timeouts and caching, so
  a slow upstream does not freeze the bot.
- SQLite uses WAL mode, a longer busy timeout, and disciplined connection
  closing for safer concurrent bot and backup access.
- Forbidden-word checks now cover edited messages, while all bot output disables
  mention expansion to prevent user-supplied text from pinging a guild.
- Event listings became deterministic and display names prefer guild nicknames,
  making repeated results easier to understand.

### Fixed

- Attendance claims and point deductions are atomic, preventing duplicate daily
  rewards and negative balances under concurrent requests.
- Failed deep-chat and image requests refund exactly once, and failures before a
  Discord interaction defer no longer charge the user.
- Ghost party members, duplicate roles, stale timestamps, abandoned slots, and
  restart-broken buttons are cleaned up or rejected consistently.
- Startup now validates databases before loading cogs and fails closed when
  production paths or state are unsafe.
- Database and backup handles are closed reliably, avoiding locked files during
  operation and recovery.

### Breaking Changes

- The chat command API changed from `/대화` plus `/기본` and `/고급` mode switches
  to explicit `/기본대화` and `/고급대화` commands.
- `/럭키박스` and its database fields were removed. Attendance became the only
  source of newly issued points so AI pricing has a stable basis.
- Secret and runtime configuration split into `.env.secrets` and `.env.runtime`;
  deployments using the former `.env` or per-key files must migrate their
  values.
- Runtime data moved under configurable `runtime/data` and `runtime/backups`
  paths, and production startup now rejects unsafe or invalid path layouts.
- Attendance repository APIs now require `(guild_id, user_id)` and party APIs
  require `(guild_id, game)`. Corresponding SQLite primary keys and ledger rows
  gained `guild_id`; pre-isolation data must be migrated before use.
- Only SQLite remains a supported backend. The previously documented but
  unimplemented `DB_URL` extension path was removed.

### Internal

- Added broad concurrency, accounting, moderation, deployment, and process
  lifecycle regression coverage.
- Removed dead scripts and isolated private runtime files from Git worktrees.

## [0.3.0] - 2026-06-11 — Consolidated modular architecture

The codebase was reduced to one supported cog-based application so new features
could share persistence and be tested without Discord connectivity.

### Added

- Added attendance and party repository interfaces with a SQLite
  implementation, centralizing persistence for commands that share user data.
- Added a console test suite that exercises database and core behavior without
  connecting a bot account.

### Improved

- Updated the AI lineup and provider dependencies, including image generation,
  while preserving separate fast and reasoning-oriented chat modes.
- Strengthened forbidden-word matching to catch common obfuscated spellings and
  record warnings in the user's profile.
- Moved active cogs into one `module` package and made one application entry
  point responsible for extension loading.

### Fixed

- Corrected OpenAI Responses API request and response handling after the SDK and
  model migration.
- Centralized database operations that had previously been duplicated across
  command modules, reducing inconsistent balance and party behavior.

### Breaking Changes

- Legacy prefix modules and most standalone scripts were removed. Slash commands
  became the only supported interactive API.
- Python import paths changed from `module.slash.*` to `module.*`, and the
  supported entry point became `python -m module.main`.
- Python 3.11 or newer became the documented runtime baseline for the modular
  application.

### Internal

- Removed several thousand lines of duplicated prefix, standalone, and embedded
  message scripts.
- Established dependency pinning and repeatable console verification as the
  basis for later production hardening.

## [0.2.0] - 2025-12-29 — Community utility expansion

The original utility bot became a persistent community companion with an
economy, AI tools, finance lookup, and a slash-command-first interface.

### Added

- Added daily attendance, wallet, ranking, profile, and Lucky Box commands to
  create a point economy that could pay for AI features.
- Added channel-scoped Hyacine chat with fast and advanced modes, plus AI image
  generation for point-funded creative requests.
- Added `/주가` for a consolidated view of major equities, rates, commodities,
  and cryptocurrency indicators.
- Added SQLite persistence for attendance/profile data and party membership, so
  balances and recruitment survive restarts.
- Added warning counts to user profiles, connecting moderation feedback with the
  community profile system.

### Improved

- Reorganized active features as Discord cogs and made slash commands the
  primary interface, so commands are discoverable in the Discord client.
- Adjusted attendance rewards and capped Lucky Box use per day to keep the new
  point economy bounded.
- Migrated OpenAI chat to the Responses API and updated the default model and
  dependency versions.

### Fixed

- Fixed Responses API errors introduced by the OpenAI SDK migration.
- Removed committed credentials and generated Python cache files from the
  distributed project.

### Breaking Changes

- The supported command surface shifted from prefix commands to slash commands;
  legacy prefix files remained only as references during this milestone.
- Deployment now required Discord, OpenAI, and Google credentials for the full
  feature set, plus a persistent `DATA_DIR` for SQLite files.
- Attendance data introduced a `users` table keyed by `user_id`; party data
  introduced `parties` and `participants` tables keyed by game and user.

### Internal

- Split features into attendance, party, event, moderation, finance, chat, and
  image cogs to make later replacement and testing possible.
- Added an explicit requirements file and removed obsolete generated and
  documentation artifacts.

## [0.1.0] - 2025-05-04 — Initial private-server prototype

The first usable version collected the small Discord utilities that had been
maintained as personal scripts into one bot.

### Added

- Added scheduled-event lookup so members can inspect a server event by number.
- Added in-memory game party recruitment with role buttons, so members can form
  a group without coordinating every slot manually.
- Added automatic forbidden-word warnings and `/금지어리로드`, so an operator
  can update a text-file word list without restarting the bot.

### Improved

- Combined the event, party, and moderation utilities under one Discord client
  with the intents needed by those features.

### Fixed

- No separately documented user-facing fixes were part of this initial
  milestone.

### Breaking Changes

- None. This milestone established the first supported behavior.

### Internal

- Established the first modular files and environment-based Discord token
  loading, replacing a loose collection of one-off snippets.
