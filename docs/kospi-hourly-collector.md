# KOSPI Hourly Index Collector

FMIndex Issue #3 — actual KOSPI composite index collection from the official
Kiwoom REST API, aggregated to 1-hour OHLC buckets, with backfill,
incremental updates, deduplication, and offline-tested validation.

## Official API basis

- Portal: https://openapi.kiwoom.com (Kiwoom Open Trading API)
- Official guide URL: https://openapi.kiwoom.com/guide/apiguide
- Official example repository: https://github.com/Kiwoom-Securities/Kiwoom-REST-API

The following contract was confirmed from the official docs/examples:

| TR id | Name | Method / Path | Purpose |
|---|---|---|---|
| au10001 | 접근토큰발급 | POST /oauth2/token | OAuth2 access token |
| ka20001 | 업종현재가요청 | POST /api/dostk/sect | KOSPI current quote |
| ka20005 | 업종분봉조회요청 | POST /api/dostk/chart | index minute chart |
| ka20006 | 업종일봉조회요청 | POST /api/dostk/chart | index daily chart |

## KOSPI instrument identity

Official code mapping (from the official ka20005/ka20006 docstrings):

- `inds_cd = "001"` → 종합(KOSPI)
- `mrkt_tp = "0"` → 코스피

A record is accepted as *real KOSPI* only when **all** of the following hold:

- `provider == "kiwoom"`
- `instrumentId == "001"` (or identity resolves to KOSPI)
- `symbol == "KOSPI"`
- `assetType == "index"`
- `dataMode == "real"`
- timestamp parses to a KST-aware session hour
- OHLC present and internally consistent (`high >= max(open, close)`, `low <= min(open, close)`)

Individual stock records (e.g. `005930`, `assetType=stock`) are **rejected** and
never merged into the KOSPI series. Multiple symbols are never merged.

## Authentication

Credentials are read **read-only** from:

1. Environment variables: `KIWOOM_APPKEY`, `KIWOOM_SECRETKEY`, `KIWOOM_BASE_URL`
2. A local `.env` file (project root or `KIWOOM_65STOCK_ENV` path)

The 65stock reference repository (`/mnt/g/Ddrive/BatangD/task/workdiary/65stock`)
is used as a **read-only reference** — its files are never modified and its
credentials are never copied into this repository. Token issuance follows the
official contract: `POST /oauth2/token` with `grant_type=client_credentials`,
`appkey`, `secretkey`. Tokens are held in memory only; no token cache file is
ever written. Secret values are never logged.

## Endpoints and pagination

- `ka20005` (업종분봉): body `{mrkt_tp, inds_cd, tic_scope, base_dt}`, table `inds_min_pole_qry`
- `ka20006` (업종일봉): body `{inds_cd, base_dt}`, table `inds_dt_pole_qry`

Pagination follows the official contract: response `cont_yn == "Y"` plus
`next_key` continues to the next page. The client aborts on repeated keys
(infinite-loop guard) and enforces a `max_pages` budget.

## Rate limits

Per official guidance and measured behavior: per-TR sustained ~1 req/s with a
burst of 2. HTTP 429 or `return_code == 5` aborts collection immediately.
`--request-delay` and `--max-requests` provide additional pacing/budget guards.

## Timezone

All internal timestamps are timezone-aware. Output is Asia/Seoul (KST, +09:00).
Naive timestamps are interpreted as KST (documented contract); UTC/offset
timestamps are converted to KST.

## Trading calendar and sessions

- Regular session: 09:00–15:30 KST, Mon–Fri (KRX official market hours).
- Closing single-price auction: 15:20–15:30.
- Weekend and Korean public holidays are excluded (static 2026 holiday snapshot
  in `market_calendar.py`; the KRX official calendar is the source of truth and
  must be consulted before live backfills covering those dates).
- Out-of-session data is excluded from hourly buckets.
- The final 15:00–15:30 bucket is marked `isPartial = true`.

## Hourly OHLC aggregation

When the API provides true 60-minute candles (`tic_scope="60"`) those are used
directly. For minute buckets, aggregation rules:

- `open` = first candle open
- `high` = max high
- `low` = min low
- `close` = last candle close
- `volume` = sum when all candles provide it; `null` when the index API does not
  provide volume (never fabricated as 0)

Missing candles are never zero-filled; absent buckets are simply omitted.

## Backfill and incremental updates

- CLI requires an explicit `--from YYYY-MM-DD --to YYYY-MM-DD` range. With no
  explicit range, no unbounded backfill is started.
- Per-day collection iterates only trading days.
- Existing JSONL records are loaded and merged incrementally.
- Overlap refresh re-fetches recent buckets (`--overlap-hours`) so revisions are
  picked up; the newest `observedAt` wins.
- Dedup key: `(provider, instrumentId, timestamp)`; the more complete OHLC wins
  on equal `observedAt`.
- Output is sorted by timestamp ascending.

## Output schema

`output/market/kospi-hourly.jsonl` — one JSON object per line:

```json
{
  "timestamp": "2026-08-05T10:00:00+09:00",
  "market": "KOSPI",
  "instrumentId": "001",
  "symbol": "KOSPI",
  "assetType": "index",
  "open": 3200.0,
  "high": 3210.0,
  "low": 3190.0,
  "close": 3205.0,
  "volume": null,
  "changeRate": 0.15,
  "source": "kiwoom-rest-api",
  "provider": "kiwoom",
  "observedAt": "2026-08-05T11:00:00+09:00",
  "dataMode": "real"
}
```

## Metadata schema

`output/market/kospi-hourly.meta.json`:

`provider`, `instrumentId`, `symbol`, `assetType`, `requestedFrom`,
`requestedTo`, `effectiveFrom`, `effectiveTo`, `requestsMade`,
`recordsReceived`, `recordsAccepted`, `recordsRejected`, `duplicatesRemoved`,
`partialBuckets`, `latestTimestamp`, `generatedAt`, `dataMode`,
`apiContractVersion`, `collectorVersion`, `warnings`.

Metadata never contains tokens, app keys, secrets, or Authorization headers.

## Pipeline integration

`python -m fmindex.pipeline --once --market-source <source>`:

- `sample` — explicit sample data (`dataMode=sample`).
- `auto` — validated real KOSPI when available, otherwise sample (explicit).
- `kiwoom` — real KOSPI only (`--market-data <collector.jsonl>`), validated by
  the strict bridge. **Fail-closed**: on failure an error is raised and sample
  data is never silently substituted.

The dashboard UI files are not modified by this work.

## Known limitations

- Live smoke test was **not** run: `LIVE_SMOKE=SKIPPED_NO_CREDENTIALS`
  (no `KIWOOM_APPKEY`/`KIWOOM_SECRETKEY` in this environment).
- The 2026 public-holiday snapshot is best-effort; the KRX official calendar
  should be checked before live backfills over holiday dates.
- 60-minute buckets are produced from `tic_scope="60"`; minute-level
  aggregation (tic_scope < 60) is implemented and covered by tests but not
  exercised against the live API.
