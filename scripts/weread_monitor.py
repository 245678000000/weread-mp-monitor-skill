#!/usr/bin/env python3
"""WeRead public-account monitor companion.

Purpose-built for agent/Skill workflows. It uses the same WeRead internal
endpoints documented by steptian/weread-mp, but keeps monitor configuration
and per-account seen state so scheduled checks only return genuinely new
articles.

No cookie is stored inside the Skill package. Supply authentication through
WEREAD_COOKIE, WEREAD_COOKIE_FILE, --cookie-file, or a supported local
Chromium browser.

Requires Python 3.9 or newer.
"""

from __future__ import annotations

import argparse
import contextlib
import html as html_mod
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

try:  # POSIX only; locking degrades to best-effort elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__version__ = "0.2.0"

WEREAD_HOST = "https://weread.qq.com"
DEFAULT_STATE_FILE = os.path.expanduser("~/.weread_mp_monitor_state.json")
STATE_VERSION = 2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
SUPPORTED_BROWSERS = ["chrome", "edge", "brave", "chromium", "opera", "vivaldi"]
MAX_SEEN_PER_ACCOUNT = 500
MAX_ARTICLE_PAGES = 10
DEFAULT_AUTH_WARN_HOURS = 20
MATCH_MODES = ("any", "all")


class MonitorError(Exception):
    code = "monitor_error"


class AuthError(MonitorError):
    code = "auth_expired"


class AccountNotFound(MonitorError):
    code = "account_not_found"


class StateError(MonitorError):
    code = "state_unreadable"


class AmbiguousAccount(MonitorError):
    code = "ambiguous_account"

    def __init__(self, name: str, candidates: list[dict[str, Any]]):
        super().__init__(name)
        self.name = name
        self.candidates = candidates


class BatchNotFound(MonitorError):
    code = "batch_not_found"


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ts_to_iso(value: Any) -> str:
    """Convert a WeRead unix timestamp into a local ISO-8601 string."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

@contextlib.contextmanager
def state_lock(path: str) -> Iterator[None]:
    """Serialize read-modify-write cycles across threads and processes."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    lock_path = f"{path}.lock"
    handle = open(lock_path, "a+")  # noqa: SIM115 - released in finally
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # e.g. network filesystems without flock support
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def migrate_state(data: dict[str, Any]) -> dict[str, Any]:
    """Bring any older state document up to STATE_VERSION."""
    data.setdefault("monitors", {})
    data.setdefault("last_auth_warning_at", None)
    if not isinstance(data["monitors"], dict):
        raise StateError("state 'monitors' must be an object")
    for book_id, item in data["monitors"].items():
        if not isinstance(item, dict):
            raise StateError(f"monitor {book_id} must be an object")
        item.setdefault("name", book_id)
        item.setdefault("book_id", book_id)
        item.setdefault("keywords", [])
        item.setdefault("match_mode", "any")
        item.setdefault("match_body", False)
        item.setdefault("created_at", None)
        item.setdefault("last_checked_at", None)
        item.setdefault("seen", [])
        item.setdefault("pending", None)
        if item["match_mode"] not in MATCH_MODES:
            item["match_mode"] = "any"
    data["version"] = STATE_VERSION
    return data


def load_state(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return migrate_state({"version": STATE_VERSION})
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise StateError(
            f"Cannot read state file {path}: {exc}. Move it aside to start a fresh registry."
        ) from exc
    if not isinstance(data, dict):
        raise StateError(f"State file {path} must contain a JSON object.")
    return migrate_state(data)


def save_state(path: str, state: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------

def read_cookie_file(path: str) -> str:
    with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
        return f.read().strip()


def extract_browser_cookie(preferred: str | None = None) -> str:
    """Read the weread.qq.com session straight out of a local Chromium profile.

    This is what makes scan-to-sign-in sufficient: the user never has to open
    DevTools or handle the cookie value themselves.
    """
    try:
        import browser_cookie3 as bc  # type: ignore
    except ImportError as exc:
        raise AuthError(
            "browser-cookie3 is not installed, so the scanned browser session cannot be read. "
            "Run: python3 -m pip install browser-cookie3"
        ) from exc

    order = [preferred] if preferred else SUPPORTED_BROWSERS
    last_error: Exception | None = None
    for browser_name in order:
        loader = getattr(bc, browser_name, None)
        if loader is None:
            continue
        try:
            jar = loader(domain_name="weread.qq.com")
            parts = []
            for item in jar:
                value = item.value
                if any(ch in value for ch in [" ", ",", ";", '"']):
                    value = urllib.parse.quote(value)
                parts.append(f"{item.name}={value}")
            if parts:
                return "; ".join(parts)
        except Exception as exc:  # pragma: no cover - OS/browser dependent
            last_error = exc
    suffix = f" Last browser error: {type(last_error).__name__}" if last_error else ""
    raise AuthError(
        "No weread.qq.com session found in a local browser. Open weread.qq.com and "
        "scan to sign in." + suffix
    )


def save_cookie_file(path: str, cookie: str) -> str:
    """Persist a session for a scheduled runtime. Owner-readable only."""
    target = os.path.expanduser(path)
    parent = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(parent, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(cookie)
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return target


def wait_for_login(
    browser: str | None = None,
    timeout: int = 180,
    poll_seconds: float = 3.0,
    open_browser: bool = True,
    save_to: str | None = None,
) -> dict[str, Any]:
    """Guide a scan-to-sign-in: open WeRead, then poll the local browser session.

    Returns only metadata. The cookie value is never placed in the result, so it
    cannot leak into a transcript or a log.
    """
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(WEREAD_HOST))
        except Exception:  # pragma: no cover - headless environments
            opened = False

    deadline = time.monotonic() + max(0, timeout)
    attempts = 0
    last_reason = "no session found yet"

    while True:
        attempts += 1
        try:
            cookie = extract_browser_cookie(browser)
            accounts = get_accounts(cookie)
        except AuthError as exc:
            last_reason = str(exc)
        except MonitorError as exc:
            last_reason = str(exc)
        else:
            result: dict[str, Any] = {
                "status": "ok",
                "auth": "valid",
                "browser_opened": opened,
                "followed_accounts": len(accounts),
                "attempts": attempts,
            }
            if save_to:
                result["cookie_file"] = save_cookie_file(save_to, cookie)
                result["note"] = (
                    "Point WEREAD_COOKIE_FILE at this path for scheduled runs. "
                    "The file holds a live session: keep it on this machine, chmod 600."
                )
            return result

        if time.monotonic() >= deadline:
            raise AuthError(
                f"Timed out after {timeout}s waiting for a WeRead sign-in. "
                f"Open {WEREAD_HOST} in Chrome/Edge/Brave and scan with WeChat, then retry. "
                f"Last reason: {last_reason}"
            )
        time.sleep(poll_seconds)


def resolve_cookie(args: argparse.Namespace) -> str:
    """Resolve the WeRead cookie. Never accepts a cookie as a CLI argument."""
    if getattr(args, "cookie_file", None):
        return read_cookie_file(args.cookie_file)
    env_file = os.environ.get("WEREAD_COOKIE_FILE", "").strip()
    if env_file:
        return read_cookie_file(env_file)
    env_cookie = os.environ.get("WEREAD_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    if getattr(args, "no_browser_cookie", False):
        raise AuthError("No WeRead cookie supplied.")
    return extract_browser_cookie(getattr(args, "browser", None))


# --------------------------------------------------------------------------
# WeRead API
# --------------------------------------------------------------------------

def api_call(path: str, cookie: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{WEREAD_HOST}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json, text/plain, */*")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError("WeRead login expired; scan to sign in again at weread.qq.com.") from exc
        raise MonitorError(f"WeRead HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MonitorError(f"WeRead request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(
            "WeRead returned a non-JSON response; the internal endpoint may have changed."
        ) from exc
    except Exception as exc:
        raise MonitorError(f"WeRead request failed: {exc}") from exc

    if isinstance(data, dict):
        err_code = data.get("errCode") or data.get("errcode")
        if err_code not in (None, 0, "0"):
            msg = str(data.get("errMsg") or data.get("errmsg") or "")
            if any(x in msg.lower() for x in ["login", "token", "expired"]) or any(
                x in msg for x in ["登录", "过期", "用户不存在"]
            ):
                raise AuthError("WeRead login expired; scan to sign in again at weread.qq.com.")
            raise MonitorError(f"WeRead API error {err_code}: {msg}")
    return data


def get_accounts(cookie: str) -> list[dict[str, str]]:
    data = api_call("/web/shelf/sync", cookie, {"userVid": "", "synckey": 0})
    out: list[dict[str, str]] = []
    if isinstance(data, dict):
        for book in data.get("books", []):
            book_id = book.get("bookId")
            if isinstance(book_id, str) and book_id.startswith("MP_WXS_"):
                out.append({"name": str(book.get("title") or "未知公众号"), "book_id": book_id})
    return out


def _article_from_mp_info(info: dict[str, Any]) -> dict[str, Any]:
    original_id = str(info.get("originalId") or "")
    return {
        "title": str(info.get("title") or ""),
        "summary": str(info.get("content") or ""),
        "mp_name": str(info.get("mp_name") or ""),
        "original_id": original_id,
        "url": f"https://mp.weixin.qq.com/s/{original_id}" if original_id else "",
        "read_num": info.get("readNum", 0),
        "like_num": info.get("likeNum", 0),
        "time": info.get("time", 0),
        "published_at": ts_to_iso(info.get("time", 0)),
    }


def get_articles(
    cookie: str,
    book_id: str,
    limit: int = 20,
    max_pages: int = MAX_ARTICLE_PAGES,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` recent articles, newest first.

    The upstream cursor is a createTime watermark. Guard against a cursor that
    stops advancing (which previously spun forever) and against duplicate rows
    across pages.
    """
    articles: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    cursor = 0
    pages = 0

    while len(articles) < limit and pages < max_pages:
        pages += 1
        data = api_call("/web/mp/articles", cookie, {"bookId": book_id, "offset": cursor})
        if not isinstance(data, dict):
            break
        reviews = data.get("reviews") or []
        if not reviews:
            break

        for review in reviews:
            for sub in review.get("subReviews", []):
                info = sub.get("review", {}).get("mpInfo", {})
                if not info.get("title"):
                    continue
                article = _article_from_mp_info(info)
                original_id = article["original_id"]
                if original_id:
                    if original_id in known_ids:
                        continue
                    known_ids.add(original_id)
                articles.append(article)
                if len(articles) >= limit:
                    break
            if len(articles) >= limit:
                break

        if len(articles) >= limit or data.get("clearAll"):
            break
        times = [r.get("createTime", 0) for r in reviews if r.get("createTime")]
        if not times:
            break
        next_cursor = min(times)
        if next_cursor == cursor:
            break  # cursor is not advancing; stop instead of looping forever
        cursor = next_cursor
        if len(reviews) < 10:
            break
        time.sleep(0.25)

    return articles[:limit]


# --------------------------------------------------------------------------
# article body extraction
# --------------------------------------------------------------------------

_JS_CONTENT_OPEN = re.compile(r'<div\b[^>]*\bid="js_content"[^>]*>', re.I)
_DIV_TAG = re.compile(r"<(/?)div\b[^>]*?(/?)>", re.I)
_BLOCKED_MARKERS = ("环境异常", "去验证", "该内容已被发布者删除", "此内容因违规无法查看")


def extract_js_content(text: str) -> str:
    """Return the raw inner HTML of #js_content, honouring nested <div>s.

    A non-greedy `(.*?)</div>` truncates at the first nested closing tag, and
    WeChat wraps images and sections in nested divs constantly.
    """
    opening = _JS_CONTENT_OPEN.search(text)
    if not opening:
        return ""
    start = opening.end()
    depth = 1
    pos = start
    while True:
        tag = _DIV_TAG.search(text, pos)
        if not tag:
            return text[start:]  # unbalanced markup: take the remainder
        pos = tag.end()
        if tag.group(2) == "/":
            continue  # self-closing <div/>
        if tag.group(1) == "/":
            depth -= 1
            if depth == 0:
                return text[start:tag.start()]
        else:
            depth += 1


def html_to_text(fragment: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", fragment, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|section|div|li|h[1-6]|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def get_article_body(url: str, max_chars: int = 4000) -> dict[str, Any]:
    """Fetch and flatten a mp.weixin.qq.com article.

    body_status is one of: ok, empty, blocked, fetch_failed, no_url.
    Callers must fall back to the list summary when it is not `ok`.
    """
    result: dict[str, Any] = {"body": "", "pub_time": "", "body_status": "no_url", "body_truncated": False}
    if not url:
        return result

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        result["body_status"] = "fetch_failed"
        return result

    pub_time = ""
    m = re.search(r'<em[^>]*id="publish_time"[^>]*>(.*?)</em>', text, re.S)
    if m:
        pub_time = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not pub_time:
        m = re.search(r'"publish_time":"(\d{4}-\d{2}-\d{2}[^"]*)"', text)
        if m:
            pub_time = m.group(1)
    result["pub_time"] = pub_time

    body = html_to_text(extract_js_content(text))
    if not body:
        result["body_status"] = "blocked" if any(x in text for x in _BLOCKED_MARKERS) else "empty"
        return result

    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars]
        result["body_truncated"] = True
    result["body"] = body
    result["body_status"] = "ok"
    return result


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def matched_keywords(article: dict[str, Any], keywords: list[str], include_body: bool = False) -> list[str]:
    parts = [str(article.get("title", "")), str(article.get("summary", ""))]
    if include_body:
        parts.append(str(article.get("body", "")))
    haystack = " ".join(parts).casefold()
    return [kw for kw in keywords if kw.casefold() in haystack]


def article_matches(
    article: dict[str, Any],
    keywords: list[str],
    mode: str = "any",
    include_body: bool = False,
) -> bool:
    if not keywords:
        return True
    hits = matched_keywords(article, keywords, include_body)
    if mode == "all":
        return len(hits) == len(keywords)
    return bool(hits)


def resolve_account(accounts: list[dict[str, str]], query: str) -> dict[str, str]:
    q = normalize_name(query)
    exact = [a for a in accounts if normalize_name(a["name"]) == q]
    if len(exact) == 1:
        return exact[0]
    partial = [a for a in accounts if q in normalize_name(a["name"]) or normalize_name(a["name"]) in q]
    if len(partial) == 1:
        return partial[0]
    if len(exact) > 1:
        raise AmbiguousAccount(query, exact)
    if len(partial) > 1:
        raise AmbiguousAccount(query, partial)
    raise AccountNotFound(query)


def select_monitors(state: dict[str, Any], query: str) -> list[tuple[str, dict[str, Any]]]:
    q = normalize_name(query)
    exact = [
        (book_id, item)
        for book_id, item in state["monitors"].items()
        if q == normalize_name(item.get("name", "")) or q == normalize_name(book_id)
    ]
    if exact:
        return exact
    return [
        (book_id, item)
        for book_id, item in state["monitors"].items()
        if q in normalize_name(item.get("name", ""))
    ]


def require_one_monitor(state: dict[str, Any], query: str) -> tuple[str, dict[str, Any]]:
    candidates = select_monitors(state, query)
    if not candidates:
        raise AccountNotFound(query)
    if len(candidates) > 1:
        raise AmbiguousAccount(query, [monitor_view(x[1]) for x in candidates])
    return candidates[0]


def monitor_view(item: dict[str, Any]) -> dict[str, Any]:
    pending = item.get("pending") or {}
    return {
        "name": item.get("name"),
        "book_id": item.get("book_id"),
        "keywords": item.get("keywords", []),
        "match_mode": item.get("match_mode", "any"),
        "match_body": bool(item.get("match_body", False)),
        "created_at": item.get("created_at"),
        "last_checked_at": item.get("last_checked_at"),
        "seen_count": len(item.get("seen", [])),
        "pending_batch": pending.get("batch_id"),
        "pending_count": len(pending.get("ids", [])),
    }


# --------------------------------------------------------------------------
# registry operations
# --------------------------------------------------------------------------

def add_monitor(
    cookie: str,
    state: dict[str, Any],
    name: str,
    keywords: list[str],
    baseline: int,
    match_mode: str = "any",
    match_body: bool = False,
    append_keywords: list[str] | None = None,
    reset_baseline: bool = False,
    clear_keywords: bool = False,
) -> dict[str, Any]:
    account = resolve_account(get_accounts(cookie), name)
    book_id = account["book_id"]
    current = state["monitors"].get(book_id)

    if current:
        current["name"] = account["name"]
        if clear_keywords:
            current["keywords"] = []
        elif append_keywords:
            merged = list(current.get("keywords", []))
            for kw in append_keywords:
                if kw not in merged:
                    merged.append(kw)
            current["keywords"] = merged
        elif keywords or not current.get("keywords"):
            # A bare re-add keeps existing filters; use --clear-keywords to drop them.
            current["keywords"] = keywords
        current["match_mode"] = match_mode
        current["match_body"] = match_body
        baseline_count = 0
        if reset_baseline:
            articles = get_articles(cookie, book_id, limit=max(1, baseline)) if baseline else []
            seen = [a["original_id"] for a in articles if a.get("original_id")]
            current["seen"] = seen[-MAX_SEEN_PER_ACCOUNT:]
            current["pending"] = None
            baseline_count = len(seen)
        return {"status": "updated", "monitor": monitor_view(current), "baseline_count": baseline_count}

    all_keywords = list(keywords)
    for kw in append_keywords or []:
        if kw not in all_keywords:
            all_keywords.append(kw)
    articles = get_articles(cookie, book_id, limit=max(1, baseline)) if baseline else []
    seen = [a["original_id"] for a in articles if a.get("original_id")]
    item = {
        "name": account["name"],
        "book_id": book_id,
        "keywords": all_keywords,
        "match_mode": match_mode,
        "match_body": match_body,
        "created_at": now_iso(),
        "last_checked_at": None,
        "seen": seen[-MAX_SEEN_PER_ACCOUNT:],
        "pending": None,
    }
    state["monitors"][book_id] = item
    return {"status": "created", "monitor": monitor_view(item), "baseline_count": len(seen)}


def remove_monitor(state: dict[str, Any], query: str) -> dict[str, Any]:
    book_id, item = require_one_monitor(state, query)
    del state["monitors"][book_id]
    return {
        "status": "removed",
        "monitor": monitor_view(item),
        "remaining": len(state["monitors"]),
    }


def _append_seen(item: dict[str, Any], article_ids: list[str]) -> None:
    seen = list(item.get("seen", []))
    seen_set = set(seen)
    for original_id in article_ids:
        if original_id and original_id not in seen_set:
            seen.append(original_id)
            seen_set.add(original_id)
    item["seen"] = seen[-MAX_SEEN_PER_ACCOUNT:]


def pending_batches(state: dict[str, Any]) -> list[str]:
    ids = []
    for item in state["monitors"].values():
        batch_id = (item.get("pending") or {}).get("batch_id")
        if batch_id and batch_id not in ids:
            ids.append(batch_id)
    return ids


def ack_batch(state: dict[str, Any], batch_id: str | None, ack_all: bool = False) -> dict[str, Any]:
    """Commit a deferred check batch: move pending ids into the seen list.

    Acking an unknown id is an error rather than a no-op: a later check
    supersedes an earlier batch, and silently accepting a stale id would mark
    articles seen that were never delivered.
    """
    acked: list[dict[str, Any]] = []
    for item in state["monitors"].values():
        pending = item.get("pending") or {}
        if not pending.get("ids"):
            continue
        if not ack_all and pending.get("batch_id") != batch_id:
            continue
        _append_seen(item, list(pending.get("ids", [])))
        item["pending"] = None
        acked.append({"name": item.get("name"), "acked": len(pending.get("ids", []))})
    if not acked and not ack_all:
        outstanding = pending_batches(state)
        if outstanding:
            raise BatchNotFound(
                f"Batch {batch_id} was superseded by a later check. "
                f"Currently pending: {', '.join(outstanding)}. "
                "Ack the batch id returned by your most recent check, or re-run check."
            )
        raise BatchNotFound(
            f"No pending batch named {batch_id}. Nothing is awaiting acknowledgement; "
            "it was probably already acked."
        )
    return {
        "status": "ok",
        "batch_id": batch_id if not ack_all else None,
        "acked_accounts": acked,
        "acked_total": sum(x["acked"] for x in acked),
    }


def check_monitors(
    cookie: str,
    state: dict[str, Any],
    only_name: str | None,
    limit: int,
    with_body: bool,
    body_chars: int,
    defer_ack: bool = False,
    mark_seen: bool = True,
) -> dict[str, Any]:
    if only_name:
        selected = [require_one_monitor(state, only_name)]
    else:
        selected = list(state["monitors"].items())

    checked_at = now_iso()
    batch_id = f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    all_new: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    auth_failed = False

    for index, (book_id, item) in enumerate(selected):
        name = item.get("name")
        if auth_failed:
            account_results.append({"name": name, "book_id": book_id, "status": "skipped"})
            continue
        try:
            articles = get_articles(cookie, book_id, limit=limit)
        except AuthError as exc:
            auth_failed = True
            errors.append({"name": name, "book_id": book_id, "code": exc.code, "message": str(exc)})
            account_results.append({"name": name, "book_id": book_id, "status": "error"})
            continue
        except MonitorError as exc:
            # One flaky account must not abort the whole run.
            errors.append({"name": name, "book_id": book_id, "code": exc.code, "message": str(exc)})
            account_results.append({"name": name, "book_id": book_id, "status": "error"})
            continue

        seen_set = set(item.get("seen", []))
        keywords = list(item.get("keywords", []))
        mode = item.get("match_mode", "any")
        match_body = bool(item.get("match_body", False))
        unseen = [a for a in articles if a.get("original_id") and a["original_id"] not in seen_set]

        # When filtering on the body, the body must be fetched before matching.
        if unseen and keywords and match_body:
            for article in unseen:
                article.update(get_article_body(article.get("url", ""), body_chars))

        matched = [a for a in unseen if article_matches(a, keywords, mode, match_body)]

        if with_body:
            for article in matched:
                if "body_status" not in article:
                    article.update(get_article_body(article.get("url", ""), body_chars))

        if not articles:
            warnings.append(
                {
                    "name": name,
                    "book_id": book_id,
                    "code": "account_empty",
                    "message": "No articles returned. Confirm the account is still followed in the WeRead mobile app.",
                }
            )
        elif len(articles) >= limit and len(unseen) == len(articles):
            warnings.append(
                {
                    "name": name,
                    "book_id": book_id,
                    "code": "limit_reached",
                    "message": f"Every one of the {limit} scanned articles was unseen; older updates may have been missed. Re-run with a larger --limit.",
                }
            )

        scanned_ids = [a["original_id"] for a in articles if a.get("original_id")]
        if mark_seen:
            if defer_ack:
                # Hold the ids until the caller confirms delivery, so a crashed
                # notification does not silently swallow the articles.
                item["pending"] = {"batch_id": batch_id, "ids": scanned_ids, "created_at": checked_at}
            else:
                # Mark every scanned article as seen, not only keyword matches, so
                # later keyword edits cannot resurface old content as "new".
                _append_seen(item, list(reversed(scanned_ids)))
                item["pending"] = None
            item["last_checked_at"] = checked_at

        for article in matched:
            article["monitor_name"] = name
            article["keywords"] = keywords
            article["matched_keywords"] = matched_keywords(article, keywords, match_body)
        all_new.extend(matched)
        account_results.append(
            {
                "name": name,
                "book_id": book_id,
                "status": "ok",
                "scanned": len(articles),
                "unseen": len(unseen),
                "new_matching": len(matched),
                "keywords": keywords,
                "match_mode": mode,
                "match_body": match_body,
            }
        )

    all_new.sort(key=lambda x: x.get("time", 0), reverse=True)
    result = {
        "status": "ok" if not errors else "partial",
        "checked_at": checked_at,
        "monitor_count": len(selected),
        "new_count": len(all_new),
        "accounts": account_results,
        "new_articles": all_new,
        "errors": errors,
        "warnings": warnings,
        "auth_expired": auth_failed,
        "dry_run": not mark_seen,
    }
    if mark_seen and defer_ack:
        result["batch_id"] = batch_id
        result["ack_required"] = True
    return result


# --------------------------------------------------------------------------
# auth warning throttling
# --------------------------------------------------------------------------

def auth_warning_payload(state: dict[str, Any], message: str, warn_hours: float) -> dict[str, Any]:
    """Decide whether the user should be told again that WeRead needs a re-login."""
    last = parse_iso(state.get("last_auth_warning_at"))
    now = datetime.now().astimezone()
    suppress = bool(last and warn_hours > 0 and now - last < timedelta(hours=warn_hours))
    if not suppress:
        state["last_auth_warning_at"] = now_iso()
    return {
        "status": "error",
        "code": AuthError.code,
        "message": message,
        "notify_user": not suppress,
        "last_warned_at": state.get("last_auth_warning_at"),
        "next_action": "Run `login` to open WeRead and scan with WeChat. Never ask the user to copy a cookie out of DevTools.",
    }


def clear_auth_warning(state: dict[str, Any]) -> None:
    state["last_auth_warning_at"] = None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict) and data.get("new_articles") is not None:
        print(f"Checked {data['monitor_count']} monitor(s); {data['new_count']} new matching article(s).")
        for article in data["new_articles"]:
            print(f"- [{article.get('monitor_name')}] {article.get('title')}")
            if article.get("published_at"):
                print(f"  {article['published_at']}")
            if article.get("summary"):
                print(f"  {article['summary'][:180]}")
            if article.get("url"):
                print(f"  {article['url']}")
        for warning in data.get("warnings", []):
            print(f"! [{warning.get('name')}] {warning.get('code')}: {warning.get('message')}")
        for error in data.get("errors", []):
            print(f"x [{error.get('name')}] {error.get('code')}: {error.get('message')}")
        if data.get("batch_id"):
            print(f"Pending batch {data['batch_id']} — run `ack {data['batch_id']}` after delivering.")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Monitor followed WeRead public accounts for new articles")
    p.add_argument("--version", action="version", version=f"weread-mp-monitor {__version__}")
    p.add_argument("--state", default=os.environ.get("WEREAD_MONITOR_STATE", DEFAULT_STATE_FILE))
    p.add_argument("--cookie-file", help="File containing the WeRead Cookie header")
    p.add_argument("--browser", choices=SUPPORTED_BROWSERS, help="Read the session from this browser only")
    p.add_argument("--no-browser-cookie", action="store_true", help="Do not try local browser-cookie3 fallback")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.add_argument(
        "--auth-warn-hours",
        type=float,
        default=DEFAULT_AUTH_WARN_HOURS,
        help="Suppress a repeat auth warning within this many hours (default 20)",
    )

    sub = p.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="Guide scan-to-sign-in, then confirm the session works")
    login.add_argument("--timeout", type=int, default=180, help="Seconds to wait for the scan (default 180)")
    login.add_argument("--no-open", action="store_true", help="Do not open weread.qq.com automatically")
    login.add_argument("--save", help="Also write the session to this file for scheduled runs (chmod 600)")

    sub.add_parser("doctor", help="Check authentication and count followed public accounts")
    sub.add_parser("accounts", help="List public accounts followed in WeRead")
    sub.add_parser("list", help="List configured monitors")

    add = sub.add_parser("add", help="Add or update a monitored public account")
    add.add_argument("name")
    add.add_argument("--keyword", action="append", default=[], help="Replacement filter set; repeatable")
    add.add_argument("--add-keyword", action="append", default=[], help="Append to the existing filter set; repeatable")
    add.add_argument("--clear-keywords", action="store_true", help="Drop all filters for an existing monitor")
    add.add_argument("--match-mode", choices=MATCH_MODES, default="any", help="any = 任一命中 (default), all = 全部命中")
    add.add_argument("--match-body", action="store_true", help="Also match keywords against the article body")
    add.add_argument("--baseline", type=int, default=20, help="Mark current N articles as already seen (default 20)")
    add.add_argument("--reset-baseline", action="store_true", help="Re-seed the baseline for an existing monitor")

    rm = sub.add_parser("remove", aliases=["rm"], help="Remove a monitored account")
    rm.add_argument("name")

    check = sub.add_parser("check", help="Check monitors and return only unseen matching articles")
    check.add_argument("name", nargs="?", help="Optional monitored account name/book ID")
    check.add_argument("--limit", type=int, default=20, help="Articles scanned per account (default 20)")
    check.add_argument("--with-body", action="store_true", help="Fetch article body for newly matched articles")
    check.add_argument("--body-chars", type=int, default=4000)
    group = check.add_mutually_exclusive_group()
    group.add_argument(
        "--defer-ack",
        action="store_true",
        help="Hold results in a pending batch until `ack` confirms delivery",
    )
    group.add_argument(
        "--no-mark",
        action="store_true",
        help="Dry run: report matches without writing any state",
    )

    ack = sub.add_parser("ack", help="Confirm delivery of a deferred check batch")
    ack.add_argument("batch_id", nargs="?", help="Batch id from `check --defer-ack`")
    ack.add_argument("--all", dest="ack_all", action="store_true", help="Acknowledge every pending batch")
    return p


def dispatch(args: argparse.Namespace) -> tuple[int, Any]:
    read_only = args.command in ("list",)

    if read_only:
        state = load_state(args.state)
        return 0, {
            "status": "ok",
            "monitors": [monitor_view(v) for v in state["monitors"].values()],
        }

    if args.command in ("remove", "rm", "ack"):
        with state_lock(args.state):
            state = load_state(args.state)
            if args.command == "ack":
                result = ack_batch(state, args.batch_id, args.ack_all)
            else:
                result = remove_monitor(state, args.name)
            save_state(args.state, state)
        return 0, result

    # Commands below need WeRead credentials.
    with state_lock(args.state):
        state = load_state(args.state)
        persist = True
        try:
            cookie = "" if args.command == "login" else resolve_cookie(args)
            if args.command == "login":
                result: Any = wait_for_login(
                    browser=args.browser,
                    timeout=args.timeout,
                    open_browser=not args.no_open,
                    save_to=args.save,
                )
            elif args.command == "doctor":
                accounts = get_accounts(cookie)
                result = {
                    "status": "ok",
                    "auth": "valid",
                    "followed_accounts": len(accounts),
                    "monitor_count": len(state["monitors"]),
                    "version": __version__,
                }
            elif args.command == "accounts":
                result = {"status": "ok", "accounts": get_accounts(cookie)}
            elif args.command == "add":
                result = add_monitor(
                    cookie,
                    state,
                    args.name,
                    list(args.keyword),
                    max(0, args.baseline),
                    match_mode=args.match_mode,
                    match_body=args.match_body,
                    append_keywords=list(args.add_keyword),
                    reset_baseline=args.reset_baseline,
                    clear_keywords=args.clear_keywords,
                )
            elif args.command == "check":
                result = check_monitors(
                    cookie,
                    state,
                    args.name,
                    max(1, args.limit),
                    args.with_body,
                    max(0, args.body_chars),
                    defer_ack=args.defer_ack,
                    mark_seen=not args.no_mark,
                )
                persist = not args.no_mark
                if result.get("auth_expired"):
                    auth_note = auth_warning_payload(
                        state,
                        "WeRead login expired during the run; scan to sign in again at weread.qq.com.",
                        args.auth_warn_hours,
                    )
                    result["auth_notice"] = auth_note
                    save_state(args.state, state)
                    return 2, result
            else:
                raise MonitorError(f"Unknown command: {args.command}")
        except AuthError as exc:
            payload = auth_warning_payload(state, str(exc), args.auth_warn_hours)
            save_state(args.state, state)
            return 2, payload

        clear_auth_warning(state)
        if persist:
            save_state(args.state, state)
        return 0, result


def main() -> int:
    args = build_parser().parse_args()
    try:
        code, payload = dispatch(args)
    except AmbiguousAccount as exc:
        code, payload = 3, {
            "status": "error",
            "code": exc.code,
            "message": f"Multiple accounts match: {exc.name}",
            "candidates": exc.candidates,
        }
    except AccountNotFound as exc:
        code, payload = 3, {
            "status": "error",
            "code": exc.code,
            "message": f"Public account not found among followed WeRead accounts/monitors: {exc}",
            "next_action": "Follow the public account in the WeRead mobile app, then retry.",
        }
    except BatchNotFound as exc:
        code, payload = 3, {"status": "error", "code": exc.code, "message": str(exc)}
    except StateError as exc:
        code, payload = 4, {"status": "error", "code": exc.code, "message": str(exc)}
    except MonitorError as exc:
        code, payload = 1, {"status": "error", "code": exc.code, "message": str(exc)}
    emit(payload, args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
