# Automation behavior

This Skill does not schedule anything itself. It expects the host to provide a recurring wake-up, and treats that scheduler as interchangeable.

## Picking the scheduler

| Host | Mechanism | Notes |
|---|---|---|
| ChatGPT | Automations | Condition-style tasks supported; the cloud runtime cannot read the user's local browser session — see `references/setup.md` §3 |
| Claude Code | `/schedule` (scheduled cloud agents) or `/loop` for a session-bound interval | `/schedule` is the durable one |
| Any machine | `cron` / `launchd` / systemd timer | The most reliable option for local mode, because it runs on the machine that holds the WeRead login |

Whichever is used, the registry, dedup, and filtering all live in the script. The scheduler only supplies the wake-up and the delivery channel.

### Plain cron example

```cron
0 9 * * * /usr/bin/python3 /absolute/path/to/skill/scripts/weread_monitor.py --json check --with-body >> ~/weread-monitor.log 2>&1
```

For cron there is no agent to acknowledge delivery, so omit `--defer-ack`.

## Shared task design

Prefer one recurring task for all configured accounts.

Recommended title:

`公众号每日监控`

Recommended future-run instruction:

> Use the weread-mp-monitor skill to check all configured WeRead public-account monitors with `check --with-body --defer-ack`. Return only genuinely new articles since the previous acknowledged scan. If there are new matching articles, summarize each one from the retrieved article body when `body_status` is `ok` and otherwise from the list summary, group by public account, include the original link, and notify me. After the notification is delivered, acknowledge the batch. If any account reported an error or an `account_empty` warning, tell me which one. If WeRead authentication has expired and `auth_notice.notify_user` is true, notify me that I need to scan to sign in again. Otherwise do not send operational noise.

## Schedule policy

- `每天` with no exact time: use a once-daily flexible schedule.
- exact time such as `每天晚上 8 点`: use that exact local time.
- `每周一`: use weekly recurrence.
- do not exceed the scheduler's minimum supported interval.
- default to daily rather than hourly for ordinary public-account monitoring.

## Adding more accounts

When a shared task already exists, adding a new public account only changes the monitor registry. Do not create another daily task.

If the user explicitly asks for different schedules, such as “豆包每天、最高法每周五”, separate tasks are acceptable because the cadence differs materially. Give each one a `check <公众号名>` scoped to its account.

## Removing accounts

After removing a monitor, read `remaining` from the JSON result:

- `remaining > 0`: keep the shared task;
- `remaining == 0`: disable/delete the shared task, if the scheduler permits it.

## Notification policy

Notify only for:

1. one or more genuinely new matching articles; or
2. a problem that requires user action — authentication expiry (`auth_notice.notify_user == true`), a per-account error, or an `account_empty` warning that suggests the account was unfollowed.

Do not send “checked, no updates” messages for condition-style tasks. But do not stay silent about a failed run either: a run where every account errored is not the same as a run with no news, and the JSON distinguishes them via `status: "partial"` and a non-empty `errors` array.

## Acknowledging delivery

The scheduled flow is two calls, not one:

1. `check --with-body --defer-ack` → note `batch_id`
2. deliver the notification
3. `ack <batch_id>`

Skipping step 3 means the same articles reappear next run. That is deliberate: an unacknowledged batch is assumed undelivered.

## Digest format

Use this shape for multiple results:

```text
豆包 更新 2 篇

1. 标题
   发布：2026-09-09
   摘要：……
   重点：……
   命中关键词：Agent
   链接：https://mp.weixin.qq.com/...

2. 标题
   摘要：……（正文获取失败，以上为列表摘要）
   链接：https://mp.weixin.qq.com/...
```

For several accounts, group the same structure under each account name.
