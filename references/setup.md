# Setup and deployment

All commands use `python3` and an absolute path to the script. `python` does not exist on macOS or most Linux distributions, and the working directory is not the Skill directory.

```bash
MONITOR="python3 /absolute/path/to/this/skill/scripts/weread_monitor.py"
```

Requires Python 3.9 or newer. The script uses only the standard library; `browser-cookie3` is an optional convenience.

## 1. What the user must do

The data source only exposes public accounts already followed in the **WeRead mobile app**. Following the account only in WeChat is not sufficient.

For local use:

1. Install the session reader once: `python3 -m pip install browser-cookie3`
2. Run `$MONITOR --json login`
3. WeRead opens; scan the QR code with WeChat
4. `login` polls the local browser profile, picks up the session by itself, and returns `followed_accounts` on success
5. In the WeRead mobile app, search for and follow each public account to monitor
6. Inspect what it can see: `$MONITOR --json accounts`

That is the whole setup. **Do not walk the user through DevTools.** Copying a `cookie:` header out of the Network tab is error-prone, puts a live session into the clipboard and shell history, and is unnecessary — `browser-cookie3` reads the same session directly from the browser profile.

On macOS the first read triggers a one-time Keychain prompt (“Chrome Safe Storage 想要使用您的钥匙串”). The user clicks 允许. This is expected. If Chrome's profile cannot be decrypted at all, try `login --browser edge` or another Chromium browser rather than falling back to manual copying.

`login` options:

| Flag | Use |
|---|---|
| `--save PATH` | Also write the session to `PATH` (chmod 600) for a scheduled runtime |
| `--browser NAME` | The user scanned somewhere other than the default browser |
| `--no-open` | WeRead is already open |
| `--timeout N` | Wait longer than the default 180s |

`login` returns metadata only — followed-account count, attempts, and (with `--save`) the file path. The session value is never in the JSON, so it cannot end up in a transcript or a log.

There is no `--cookie` flag by design: a cookie passed as an argument lands in shell history and is visible to every process on the machine via `ps`.

## 2. Local mode

Local mode is appropriate when the agent execution actually runs on the user's Mac/Windows machine and can read that machine's browser profile.

The default state file is:

`~/.weread_mp_monitor_state.json`

Override it with:

`WEREAD_MONITOR_STATE=/path/to/state.json`

The state contains monitor configuration, article IDs, and pending delivery batches — never the WeRead cookie. It is written atomically with `0600` permissions, and every read-modify-write cycle takes an exclusive `flock` on `<state>.lock`, so concurrent CLI runs and service requests cannot clobber each other.

If the state file is ever corrupted, the CLI exits `4` with `{"code": "state_unreadable"}` rather than a traceback. Move the file aside to start a fresh registry.

## 3. Why a cloud task cannot automatically read the Mac cookie

A cloud-scheduled task runs separately from the user's desktop browser. Uploading this Skill does not transfer Chrome's WeRead session into the cloud and does not make the user's Mac available during future runs.

Therefore, true unattended push requires one of these supported execution shapes:

- a scheduled environment that can run the companion on the same machine/profile as the WeRead login; or
- a user-controlled remote companion connected to the host through an authenticated connector/plugin/MCP/tool.

Do not claim “scan once and the Skill will always work in cloud” unless that bridge is actually configured and verified.

## 4. Optional remote companion service

`scripts/weread_monitor_service.py` exposes a small JSON service for deployment on infrastructure the user controls.

Required environment variables:

- `MONITOR_API_TOKEN`: random bearer token, **at least 24 characters** (the service refuses to start otherwise)
- `WEREAD_COOKIE` **or** `WEREAD_COOKIE_FILE`
- optional `WEREAD_MONITOR_STATE`
- optional `MONITOR_HOST` (default `127.0.0.1`) and `MONITOR_PORT` (default `8787`)
- optional `MONITOR_AUTH_WARN_HOURS` (default `20`)

Example local launch:

```bash
export MONITOR_API_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export WEREAD_COOKIE_FILE='/secure/path/weread.cookie'
python3 scripts/weread_monitor_service.py
```

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | no auth, liveness only |
| `GET` | `/doctor` | auth check + followed-account count |
| `GET` | `/accounts` | followed public accounts |
| `GET` | `/monitors` | configured monitors |
| `POST` | `/monitors` | add/update `{name, keywords, add_keywords, match_mode, match_body, baseline, reset_baseline}` |
| `POST` | `/check` | check monitors `{name, limit, with_body, body_chars, defer_ack, no_mark}` |
| `POST` | `/ack` | commit a deferred batch `{batch_id}` or `{all: true}` |
| `DELETE` | `/monitors/<name>` | remove monitor |

Security properties:

- The bearer token is compared with `hmac.compare_digest`, so a wrong token does not leak its prefix through response timing.
- Request bodies are capped at 1 MiB and must be a JSON object; malformed input returns `400`, not a traceback.
- State writes are serialized by the same `flock` the CLI uses, so the threaded server cannot lose updates.
- The service never returns the WeRead cookie.

Never expose this service directly over plaintext public HTTP. Bind to localhost/private network and place it behind HTTPS plus access control.

A public HTTPS endpoint by itself is not sufficient for cloud scheduling if the runtime has no secure way to send the bearer token. Prefer a proper connector/plugin/MCP that stores secrets outside chat content.

## 5. Cookie expiry

WeRead browser login can expire. HTTP 401/403 and login/token/expired API messages are mapped to `auth_expired` (exit code `2`).

Repeat warnings are throttled by the script itself: the state records `last_auth_warning_at`, and the payload's `notify_user` field tells the agent whether to alert the user this time. The window is `--auth-warn-hours` (default 20). A successful run clears the throttle. The agent must not implement a second throttle on top.

When authentication expires:

1. Run `$MONITOR --json login` and ask the user to scan again. That is the entire recovery — do not ask for a cookie value.
2. If the scheduled runtime uses a saved file, re-run `login --save <same path>` to refresh it.
3. Resume the existing monitor registry; do not recreate it. Expiry never invalidates monitors or seen state.

## 6. First-run baseline

Adding an account defaults to marking the newest 20 articles as already seen. This is intentional: a new monitor should notify about future posts, not flood the user with historical content.

Use `--baseline 0` only when the user explicitly wants existing posts to be eligible as new on the first check. Use `--reset-baseline` to re-seed an existing monitor from the current article list.

## 7. Delivery safety

`check --defer-ack` records the scanned article ids in a pending batch instead of marking them seen, and returns a `batch_id`. Run `ack <batch_id>` only after the notification actually reached the user.

If the run crashes between the check and the ack, the pending batch is simply superseded on the next check and the articles are reported again. Losing a notification is recoverable; silently marking an undelivered article as seen is not.

Use `check --no-mark` for a pure dry run that writes nothing at all.
