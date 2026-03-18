# codex-cli-telegram-wrapper

A minimal Telegram bridge for Codex CLI.

This repository lets you run Codex on your own machine and control it from a
Telegram chat. It is intentionally small: one Windows launcher, one Python
bridge script, one example environment file, and this README.

The bridge is designed for a single trusted operator. It is not a multi-user bot
platform.


## Repository layout

```text
codex-cli-telegram-wrapper/
├─ README.md
├─ LICENSE
├─ .gitignore
├─ .env.example
├─ codex_tg_bridge.py
└─ codex-tg.cmd
```

## What it does

The bridge sits between Telegram and Codex CLI:

- Telegram receives your prompt.
- The Python bridge launches `codex exec --json` in your chosen working directory.
- The bridge parses Codex JSONL events.
- One Telegram message is edited in place to show a rolling live log.
- A final Telegram message is sent with the result or the error summary.

The default behavior is to create a dedicated Telegram forum topic for each bridge
run. If you want to stay in the main chat, use `--tg-no-topic`.

## Features

- Single-user allowlist via `TELEGRAM_ALLOWED_USER_ID`
- Single-chat targeting via `TELEGRAM_CHAT_ID`
- Dedicated topic mode by default
- Rolling live log display via one editable Telegram message
- Durable final result / error messages
- Session reuse with `/new` to reset
- `/cancel`, `/status`, `/log`, `/help`
- `--tg-discover-ids` helper for setup
- Unknown command-line flags are forwarded to `codex exec`

## Requirements

You need the following on the machine that actually runs Codex:

- Python 3.10+
- `requests` for Python
- Codex CLI installed and authenticated
- A Telegram bot token from `@BotFather`
- A Telegram chat where the bot can read and write messages

Official Codex references:

- Codex CLI: https://developers.openai.com/codex/cli/
- Authentication: https://developers.openai.com/codex/auth/
- Windows notes: https://developers.openai.com/codex/windows/

## 1) Install Codex CLI

Follow the official Codex docs. A typical install looks like this:

```bash
npm i -g @openai/codex
```

Then start Codex once locally and sign in:

```bash
codex
```

If you are on Windows, also review the official Windows guidance linked above.

## 2) Install the Python dependency

```bash
pip install requests
```

## 3) Create your local `.env`

Copy `.env.example` to `.env` and fill in your real values.

```bash
copy .env.example .env
```

or in PowerShell:

```powershell
Copy-Item .env.example .env
```

Never commit `.env`.

## 4) Discover your Telegram IDs

The most annoying part of a first setup is usually the numeric Telegram IDs.
This repository includes a discovery mode for that.

First, put only your bot token in `.env`:

```env
TELEGRAM_BOT_TOKEN=1234567890:replace_with_your_real_bot_token
```

Then run:

```bat
codex-tg.cmd --tg-discover-ids
```

Now send a message from the Telegram account and chat you want to authorize.
The script will print lines such as:

```text
from.id           = 123456789
chat.id           = -1001234567890
message_thread_id = 42
```

Use these values as follows:

- `from.id` -> `TELEGRAM_ALLOWED_USER_ID`
- `chat.id` -> `TELEGRAM_CHAT_ID`

You do not need to store `message_thread_id` in `.env`. In topic mode, the
bridge creates its own dedicated topic automatically.

## 5) Fill the rest of `.env`

A typical `.env` looks like this:

```env
TELEGRAM_BOT_TOKEN=1234567890:replace_with_your_real_bot_token
TELEGRAM_ALLOWED_USER_ID=123456789
TELEGRAM_CHAT_ID=-1001234567890

CODEX_TG_SEND_INTERVAL_SEC=1.0
CODEX_TG_TAIL_LINES=60
CODEX_TG_TAIL_EDIT_SEC=0.8
```

Optional extras:

```env
CODEX_TG_TOPIC_LABEL=My Codex Bridge
CODEX_TG_TOPIC_TIME_FORMAT=%m-%d %H:%M
CODEX_TG_PROMPT_SUFFIX=Always answer in English and keep code comments detailed.
```

## 6) Start the bridge

Run the launcher from the project directory that you want Codex to work in.
This is important: the current working directory is preserved on purpose.

Typical examples:

```bat
codex-tg.cmd --full-auto --model gpt-5-codex --search
```

```bat
codex-tg.cmd --tg-no-topic --full-auto --model gpt-5-codex --search
```

```bat
codex-tg.cmd --tg-workdir D:\my-project --full-auto --model gpt-5-codex --search
```

Notes:

- `--tg-no-topic` posts to the main chat instead of creating a forum topic.
- Any unknown flags are passed through to `codex exec`.
- The bridge internally adds `--json`, so you should not pass that yourself.

## 7) Talk to the bot in Telegram

Once the bridge is running, send normal text messages to the bot in the allowed
chat. Each text message becomes the next Codex prompt.

Available Telegram commands:

- `/help` — show command help
- `/status` — show bridge status
- `/new` — reset the Codex session
- `/cancel` — stop the current Codex run
- `/log [N]` — print the latest N log lines

The bridge reuses the Codex session until you call `/new`. That means follow-up
messages continue the same Codex conversation by default.

## How topic mode works

Topic mode is the default because it keeps one run isolated from the rest of the
chat. On startup the bridge creates a new forum topic, posts a ready message
inside it, and then listens only inside that topic.

Use topic mode when:

- your target chat is a forum-enabled supergroup
- the bot has permission to create/manage topics
- you want a cleaner separation per run

Use `--tg-no-topic` when:

- the chat is not a forum-enabled supergroup
- the bot lacks topic permissions
- you simply want everything in the main chat

## How the bridge behaves internally

A few implementation choices are deliberate:

- Polling is used instead of webhooks, so no public HTTPS endpoint is required.
- One rolling message is edited for the live tail, which keeps the chat readable.
- Final output is sent as a normal Telegram message so it remains easy to find.
- Old pending Telegram updates are skipped at startup by default to avoid replaying stale prompts.
- Use `--tg-replay-pending-updates` if you really want to process old pending messages.

## Security notes

This bridge can trigger a local coding agent on your machine. Treat it as a
remote control surface, not as a toy bot.

At minimum:

- Never commit `.env`.
- If a bot token is exposed, revoke it immediately in `@BotFather`.
- Restrict access with `TELEGRAM_ALLOWED_USER_ID`.
- Prefer a private supergroup or a tightly controlled chat.
- Do not expose this bridge to untrusted users.
- Review your Codex authentication mode and local machine permissions carefully.

## Troubleshooting

### `Cannot find Codex executable in PATH`

Install Codex CLI first, or pass a custom executable path with `--tg-codex-bin`.

### `createForumTopic failed`

Most often one of these is true:

- the target chat is not a forum-enabled supergroup
- the bot is not an admin
- the bot does not have topic-management permission

If you do not need topic mode, start the bridge with:

```bat
codex-tg.cmd --tg-no-topic --full-auto
```

### Nothing happens when I send a Telegram message

Check these items in order:

1. The message is coming from the user ID in `TELEGRAM_ALLOWED_USER_ID`.
2. The message is being sent in the chat ID in `TELEGRAM_CHAT_ID`.
3. If topic mode is on, you are sending the message inside the created topic, not in the main group.
4. The bot can read and write in that chat.
5. The bridge process is still running locally.

### `Codex exited with code ...`

The bridge includes a stderr tail in the final failure message. Also inspect the
local terminal where the bridge is running.

### I see Telegram 429 warnings

You are hitting Telegram rate limits. Increase the pacing values in `.env`:

```env
CODEX_TG_SEND_INTERVAL_SEC=1.5
CODEX_TG_TAIL_EDIT_SEC=1.2
```

### I see `Not inside a trusted directory`

That is a Codex-side trust warning. Make sure you are launching the bridge from
(or pointing `--tg-workdir` at) the repository you actually want Codex to use,
and trust/configure that workspace according to Codex guidance.

## Recommended `.gitignore`

This repository includes a minimal `.gitignore`. If you adapt this bridge into another project, at minimum keep these entries:

```gitignore
.env
__pycache__/
*.pyc
```

## Why there is no webhook server

A webhook-based bot is perfectly valid, but this project chooses long polling for
one reason: reproducibility. Anyone can clone it, install two dependencies
(Codex CLI and `requests`), fill `.env`, and run it locally without needing a
public server or HTTPS setup.

## License

This repository is released under the MIT License. See `LICENSE`.
