---
name: weread-mp-monitor
description: Monitor WeChat public accounts through the user's WeRead subscriptions and turn natural-language requests into persistent new-article monitoring. Use when the user says things like “监控豆包公众号”, “以后每天看这个公众号有没有更新”, “停止监控某公众号”, “查看我监控的公众号”, “只推送包含 AI Agent 的文章”, or asks for daily/recurring WeChat public-account update notifications. Support WeRead sign-in/follow checks, account resolution, baseline initialization, deduplication, keyword filters, article-body retrieval, and coordination with the host's recurring-task scheduler.
---

# WeRead MP Monitor

Use this skill as the control plane for recurring WeChat public-account monitoring. Keep account state and seen-article state in the companion script; use the host's scheduler only as the wake-up/notifier.

## Running the script

Every command below is written as `$MONITOR`. Resolve it once per session:

```bash
MONITOR="python3 /absolute/path/to/this/skill/scripts/weread_monitor.py"
```

Rules:

- Always `python3`, never `python`. On macOS and most Linux distributions `python` does not exist.
- Always an absolute path to `scripts/weread_monitor.py`. The working directory is not the Skill directory.
- Requires Python 3.9 or newer. No third-party packages are required; `browser-cookie3` is optional.
- Always pass `--json` and parse the result. Every command emits a JSON object with a `status` field, including on failure.

Exit codes: `0` ok · `1` generic error · `2` `auth_expired` · `3` account/batch not found or ambiguous · `4` state file unreadable.

## Core model

Maintain one monitor registry containing many public accounts. Prefer one recurring task named `公众号每日监控` that checks the whole registry, rather than one task per account.

Treat these as separate layers:

1. **WeRead data access** — `scripts/weread_monitor.py`.
2. **Persistent monitor state** — stored outside the Skill package in `~/.weread_mp_monitor_state.json` or `WEREAD_MONITOR_STATE`.
3. **Recurring wake-up** — the host's scheduler. See `references/automation.md` for ChatGPT Automations, Claude Code, and cron.
4. **Cloud credential bridge** — a user-controlled companion service/connector is required if the scheduled runtime cannot reach the user's local browser session. Read `references/setup.md` before claiming cloud monitoring is ready.

Never claim that the Skill file alone can keep running in the background or securely retain a WeRead browser cookie.

## Intent routing

Determine the user's intent, then follow exactly one branch.

### Add or update a monitor

For requests such as `监控豆包`, `以后每天看豆包公众号`, or `只推送豆包里和 AI Agent 有关的文章`:

1. Verify data access: `$MONITOR --json doctor`
2. If authentication fails, run `$MONITOR --json login` and ask the user to scan the QR code. Never ask the user to copy a cookie out of DevTools — see **Authentication rules**.
3. Add the account: `$MONITOR --json add "<公众号名>"`
4. Add each requested topic filter as a repeatable `--keyword "<关键词>"` argument. Choose the match semantics deliberately — see **Keyword filters** below.
5. Keep the default baseline of 20 current articles unless the user explicitly wants existing articles delivered. Baseline initialization prevents historical posts from being treated as new.
6. If the account is not found (`account_not_found`), tell the user to follow it in the **WeRead mobile app**, then retry. Do not confuse a WeChat follow with a WeRead follow.
7. If the result is `ambiguous_account`, show the `candidates` list and ask which one.
8. Ensure a single daily recurring task exists. Use the behavior in `references/automation.md`.
9. Confirm the account name, filter and its match mode, cadence, and that only future new matching articles will notify.

### Run a scheduled check

For a recurring task execution, use the deferred-acknowledgement flow so a failed delivery cannot swallow articles:

1. Run: `$MONITOR --json check --with-body --defer-ack`
2. Parse JSON.
3. Handle the run outcome **before** looking at articles:
   - `errors` is non-empty — some accounts failed this run. Report which ones; the rest of the digest is still valid.
   - `warnings` contains `account_empty` — that account returned nothing. Tell the user to confirm it is still followed in the WeRead mobile app.
   - `warnings` contains `limit_reached` — every scanned article was unseen, so older updates may have been missed. Re-run that account with a larger `--limit`.
   - `auth_expired` is `true` — read `auth_notice.notify_user`. Notify only when it is `true`; the script throttles repeat warnings for you. Do not implement your own throttle.
4. If `new_count` is `0`, produce no user notification unless the host requires a visible completion message.
5. If `new_count` is greater than `0`, notify with a concise digest containing account, title, publish time, 2-4 sentence summary, why it matters when inferable, and article link.
6. Summarize from `body` **only when `body_status` is `"ok"`**. For `empty`, `blocked`, `fetch_failed`, or `no_url`, fall back to `summary` and say the full text was unavailable. Never invent missing article content.
7. **After the notification is delivered**, run `$MONITOR --json ack <batch_id>` using the `batch_id` from step 1. Until you ack, those articles stay unseen and will be reported again on the next run — that is the intended safety net, not a bug.
8. Let the script manage seen state. Do not maintain a second deduplication list in conversation text.

To inspect without touching state (debugging, or answering "有什么新的吗" ad hoc), use `check --no-mark`. It reports matches and writes nothing.

### Remove a monitor

For `停止监控豆包` or equivalent:

1. Run: `$MONITOR --json remove "<公众号名>"`
2. Read `remaining` from the result.
3. If `remaining` is `0`, disable/delete the recurring `公众号每日监控` task when the scheduler permits it.
4. Otherwise keep the shared task unchanged.

### List monitors

For `我现在监控了哪些公众号`:

Run: `$MONITOR --json list`

Report account names, keyword filters, match mode, and last-check time. Do not expose cookie values or local credential paths.

## Keyword filters

`--keyword` is repeatable. Two dimensions control the semantics:

- `--match-mode any` (default) — 任一关键词命中即推送.
- `--match-mode all` — 必须全部命中. Use this when the user says `又要…又要…` or `同时包含`.
- `--match-body` — also match against the article body, not just title and summary. Costs one page fetch per unseen article, so reserve it for accounts whose titles are uninformative.

When the user says `豆包只看 Agent 和编程`, **ask which they mean** before choosing — "和" is ambiguous between AND and OR in Chinese. Then state the chosen semantics back in your confirmation.

Changing filters:

- Replacement (default): rerun `add` for the same account with the full desired keyword set.
  `$MONITOR --json add "豆包" --keyword "Agent" --keyword "编程"`
- Appending, for `再加一个关键词`: use `--add-keyword`, which preserves the existing set. No need to read `list` first.
  `$MONITOR --json add "豆包" --add-keyword "RAG"`
- Clearing, for `豆包不要过滤了，全都推给我`: `$MONITOR --json add "豆包" --clear-keywords`

A bare re-add with no keyword flags keeps the existing filters rather than silently wiping them.

Editing filters never re-delivers old articles, because every scanned article is marked seen regardless of whether it matched.

## Authentication rules

**The user's only job is to scan a QR code.** Never walk them through DevTools, never ask them to find a `cookie:` header, never ask them to paste a cookie anywhere. If you are about to type the words "开发者工具", "Network", or "Request Headers", you have taken the wrong branch.

The correct flow when authentication is missing or expired:

```bash
$MONITOR --json login
```

`login` opens `weread.qq.com`, waits for the user to scan with WeChat, reads the session out of the local browser profile itself, and confirms it worked by returning `followed_accounts`. Tell the user only: “我打开微信读书了，用微信扫一下码，扫完告诉我。” Then run the command and report the result.

Useful variants:

- `login --save ~/.weread.cookie` — also persist the session for a scheduled runtime that starts a fresh process. Report the *path*, never the contents.
- `login --browser edge` — the user scanned in a browser other than the default.
- `login --no-open` — the page is already open.
- `login --timeout 300` — the user needs longer.

Prerequisite, install once: `python3 -m pip install browser-cookie3`. Without it `login` cannot read the scanned session, and the error says exactly this. On macOS the first read triggers a one-time Keychain prompt (“Chrome Safe Storage”); tell the user to click 允许 — that is expected, not an error.

Resolution order once signed in:

1. `WEREAD_COOKIE_FILE`
2. `WEREAD_COOKIE`
3. local `browser-cookie3` extraction from a supported Chromium browser

There is deliberately **no `--cookie` command-line flag** — it would leak the session into shell history and the process list. Do not put raw cookies, bearer tokens, or login secrets in `SKILL.md`, scheduler prompts, generated reports, or chat responses.

### When the scheduled runtime is not this machine

`login` reads a *local* browser profile. If the recurring task runs in the cloud, scanning on the user's laptop does not sign the cloud runtime in. Do not paper over this by asking for the cookie value. Either move the schedule onto the machine that holds the login (cron/launchd — see `references/automation.md`), or stand up the companion service from `references/setup.md` §4 and feed it `login --save`. Say which one you are doing.

For local setup, remote companion deployment, and cloud limitations, read `references/setup.md`.

## Automation rules

For scheduling semantics and the recommended shared-task prompt, read `references/automation.md`.

Use a condition-style recurring task when the host supports it: check daily, notify only if new matching articles exist or authentication needs user action. If the user supplies an exact time, use it. If the user only says `每天`, keep a once-daily flexible cadence instead of inventing a precise clock time.

Do not create a new task for every account. Reuse the shared monitor task unless the user explicitly requests different schedules for different accounts.

## Output rules

For new-article notifications:

- Lead with `<公众号名> 更新 N 篇`.
- Show each title and original link.
- Keep routine summaries compact.
- Surface `matched_keywords` explicitly when filters exist.
- If several monitored accounts update on the same run, group by account.
- Do not notify for zero-update runs.
- Report `errors` and `warnings` even on a zero-update run, since a silent failure otherwise looks identical to "no news".

## Reliability and limits

Use modest fetch limits and daily cadence by default. Avoid bulk scraping or aggressive polling.

Known boundaries, all surfaced in the JSON rather than hidden:

- `--limit` (default 20) caps articles scanned per account per run. Exceeding it raises a `limit_reached` warning.
- Body retrieval fails independently of the list API; always check `body_status`.
- A single failing account no longer aborts the run, but it does appear in `errors`.

The underlying WeRead endpoints are undocumented/internal and can change. If a command that previously worked starts failing despite valid login — especially with a `monitor_error` mentioning a non-JSON response — report this as a possible upstream API change rather than repeatedly retrying.

Read `references/source-and-limits.md` for provenance and usage constraints.

## Tests

The parsing, matching, state, and dedup logic is covered offline (no network, no WeRead account):

```bash
python3 -m unittest discover -s tests -v
```

Run it after changing anything in `scripts/`.
