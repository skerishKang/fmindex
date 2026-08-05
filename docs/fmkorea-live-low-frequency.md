# FMKorea live low-frequency collector

Validates and exercises the FMKorea stock-board (주식 게시판) HTML
structure with a strict low-frequency collection budget. This is NOT a
bulk crawler: one board list request, up to three post detail requests,
and (only when required) one comment page request.

## Network policy

- Concurrency: 1
- Request interval: >= 3 seconds
- Automatic retries: 0
- Cookies / login / proxy / UA rotation: none (honest
  `FMIndexResearch/0.1` UA only)
- `robots.txt` checked once before any board access
- HTTP 403 / 429 / CAPTCHA: immediate full abort, no retries

## Request budget

| resource           | max |
|--------------------|-----|
| robots.txt         | 1   |
| board list         | 1   |
| post detail        | 3   |
| comment page       | 1   |
| total content nav  | 5   |

## Status classification

| condition                          | status            |
|------------------------------------|-------------------|
| HTTP 403                           | forbidden         |
| HTTP 429                           | rate_limited      |
| HTTP 404 / deleted notice          | deleted           |
| HTTP 200 + CAPTCHA marker          | captcha           |
| HTTP 200 + not board/post DOM      | unexpected_content|
| network timeout / transport error  | network_error     |

## Normalized output

Posts are written as private JSONL (`dataMode=real`,
`source=fmkorea-stock`) under gitignored `run/` paths. Personal
identifiers (author, nickname, userId, memberId, profileUrl, avatar,
IP, email, contact) are never stored.

`publishedAt` (site-displayed absolute time) and `firstSeenAt`
(collector observation time in KST) are kept separate; a missing
`publishedAt` is never replaced with `firstSeenAt`.

`contentHash` is a SHA-256 over normalized NFC title/body and
author-free comment bodies plus the `sourcePostId`.

## Pipeline integration

`run_pipeline_once(..., fmkorea_source="fixture|live|sample",
fmkorea_data_path=...)`:

- `fixture`: existing fixture directory (default, backward compatible)
- `live`: verified normalized JSONL required; missing/empty/contract
  invalid -> `RuntimeError`, no sample fallback
- `sample`: explicit sample posts

`communitySource` / `communityDataMode` / `communityPosts` /
`communityComments` / `communityFirstSeenAt` / `communityLastSeenAt`
are added to the summary; market fields are untouched.
