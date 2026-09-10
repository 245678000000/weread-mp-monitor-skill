# Source, provenance, and limits

This Skill is an original monitoring wrapper inspired by the public MIT-licensed project:

- `steptian/weread-mp`
- https://github.com/steptian/weread-mp

The upstream project documents these WeRead behaviors used by this companion:

- obtain followed public accounts from WeRead shelf data;
- obtain public-account article lists through WeRead's web endpoints;
- use the returned original article ID to read a public `mp.weixin.qq.com` page;
- require an authenticated WeRead browser session;
- only discover public accounts followed in the WeRead mobile app.

This repository is released under the MIT License; see `LICENSE`.

## Important limitations

1. The WeRead web endpoints are undocumented/internal and may change without notice. A `monitor_error` mentioning a non-JSON response is the usual first symptom.
2. Browser cookies expire and require the user to sign in again. Repeat warnings are throttled by the script; see `references/setup.md` §5.
3. Article-body retrieval can fail independently of the list API. Every fetch reports a `body_status` of `ok`, `empty`, `blocked`, `fetch_failed`, or `no_url`; only `ok` carries usable text. Long articles are cut at `--body-chars` and flagged with `body_truncated`.
4. The article list cursor is a `createTime` watermark, not a stable offset. Paging stops after `MAX_ARTICLE_PAGES` (10) or as soon as the cursor fails to advance, and duplicate rows across pages are dropped by `original_id`.
5. `--limit` (default 20) caps how many articles are scanned per account per run. If an account publishes more than that between two checks, the overflow is neither notified nor recorded; the run emits a `limit_reached` warning so this is visible rather than silent.
6. Keyword filtering matches title and summary by default. Article bodies are only searched with `--match-body`, which costs one page fetch per unseen article.
7. Use conservative request frequency. Do not turn this into bulk scraping or a commercial high-volume crawler without separately assessing platform terms, legal constraints, and reliability.
8. Keep authentication secrets outside the Skill archive and outside conversation text. There is no CLI flag that accepts a cookie value.
