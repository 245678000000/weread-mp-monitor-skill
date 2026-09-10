# weread-mp-monitor

Monitor WeChat public accounts (公众号) through your **WeRead** (微信读书) subscriptions, and get notified only about genuinely new articles.

This is an agent Skill: `SKILL.md` drives an assistant, and `scripts/weread_monitor.py` is a plain CLI you can also run by hand.

## Requirements

- Python 3.9+ (standard library only)
- A WeRead web login in Chrome / Edge / Brave / Chromium / Opera / Vivaldi
- Each account you want to monitor must be followed in the **WeRead mobile app** — following it in WeChat alone is not enough
- Optional: `pip install browser-cookie3` to pick up the browser session automatically

## Quick start

```bash
MONITOR="python3 $PWD/scripts/weread_monitor.py"

$MONITOR --json login                         # opens WeRead; scan the QR code with WeChat
$MONITOR --json doctor                        # verify login
$MONITOR --json accounts                      # what WeRead knows you follow
$MONITOR --json add "豆包"                     # start monitoring; last 20 posts marked as seen
$MONITOR --json check --with-body             # report new articles
$MONITOR --json list
$MONITOR --json remove "豆包"
```

## Commands

| Command | Purpose |
|---|---|
| `login` | Open WeRead, wait for the QR scan, pick up the session automatically. `--save PATH`, `--browser`, `--no-open`, `--timeout` |
| `doctor` | Check authentication, count followed accounts |
| `accounts` | List public accounts followed in WeRead |
| `add <name>` | Add/update a monitor. `--keyword` (replace), `--add-keyword` (append), `--clear-keywords`, `--match-mode any\|all`, `--match-body`, `--baseline N`, `--reset-baseline` |
| `list` | Show configured monitors |
| `remove <name>` | Remove a monitor; reports `remaining` |
| `check [name]` | Report unseen matching articles. `--limit`, `--with-body`, `--body-chars`, `--defer-ack`, `--no-mark` |
| `ack <batch_id>` | Commit a deferred batch. `--all` acknowledges everything pending |

Every command accepts `--json` and always emits a JSON object, including on failure.

Exit codes: `0` ok · `1` generic error · `2` auth expired · `3` not found / ambiguous · `4` state file unreadable.

## Keyword filters

```bash
$MONITOR --json add "豆包" --keyword Agent --keyword 编程                  # 任一命中
$MONITOR --json add "豆包" --keyword Agent --keyword 编程 --match-mode all # 全部命中
$MONITOR --json add "豆包" --keyword Agent --match-body                    # 也搜正文
$MONITOR --json add "豆包" --add-keyword RAG                               # 在原有基础上追加
$MONITOR --json add "豆包" --clear-keywords                                # 清空过滤器
```

Every scanned article is marked seen whether or not it matched, so widening a filter later never re-delivers old posts.

## Delivery safety

For scheduled runs, use the two-phase flow so a dropped notification does not lose articles:

```bash
BATCH=$($MONITOR --json check --with-body --defer-ack | python3 -c 'import json,sys;print(json.load(sys.stdin)["batch_id"])')
# ... deliver the digest ...
$MONITOR --json ack "$BATCH"
```

An unacknowledged batch is assumed undelivered and reappears on the next check. Use `check --no-mark` for a dry run that writes nothing.

## Authentication

**Scanning the QR code is the whole flow.** `login` opens WeRead, waits for the scan, and reads the session out of your browser profile itself — no DevTools, no copying a `cookie:` header, no pasting anything.

```bash
python3 -m pip install browser-cookie3   # once
$MONITOR --json login
```

On macOS the first run asks for Keychain access ("Chrome Safe Storage") — click Allow.

For a scheduled runtime that starts a fresh process, `login --save ~/.weread.cookie` writes the session to a `0600` file and prints the path (never the value); point `WEREAD_COOKIE_FILE` at it.

Resolution order once signed in:

1. `WEREAD_COOKIE_FILE` (or `--cookie-file`)
2. `WEREAD_COOKIE`
3. `browser-cookie3` extraction from a local Chromium browser

There is intentionally **no `--cookie` flag**: a cookie passed as an argument ends up in shell history and in `ps` output for every user on the machine.

## State

Stored at `~/.weread_mp_monitor_state.json`, or `WEREAD_MONITOR_STATE`. It holds monitor config, seen-article ids, and pending batches — never your cookie. Written atomically with `0600` permissions and guarded by an exclusive `flock`, so concurrent runs cannot clobber it.

## Remote companion

`scripts/weread_monitor_service.py` is an optional localhost JSON service for hosts that cannot reach your browser session. See [references/setup.md](references/setup.md) §4 — it must stay behind HTTPS and access control.

## Tests

Fully offline: no network, no WeRead account.

```bash
python3 -m unittest discover -s tests -v
```

## Scope and limits

The WeRead endpoints used here are undocumented and can change at any time. Use a daily cadence and modest limits; this is not a bulk scraper. See [references/source-and-limits.md](references/source-and-limits.md).

## License

MIT — see [LICENSE](LICENSE). Inspired by the MIT-licensed [steptian/weread-mp](https://github.com/steptian/weread-mp).
