# UI Light Chart-First Slice

This document records the product UI boundary for Issue #5.

## Implemented

- Light theme as the default, with an optional persistent dark theme
- FMKorea-inspired blue navigation, white surfaces, Korean system typography, and compact information density
- Chart-first home layout rather than a copied board list
- KOSPI/NASDAQ selector
- 24h/7d/30d/all timestamp filters
- Companion/lead/divergence view controls
- Korea/US/custom rise-fall color modes stored in `localStorage`
- Explicit sample/unavailable data states
- Domestic overnight/next-session and US daytime/US-session panel structures
- Responsive desktop and mobile navigation
- Static HTML/CSS/JavaScript assets exported by the Python pipeline

## Intentionally unavailable

- Real KOSPI and NASDAQ data
- Real overnight futures data
- Live FMKorea collection
- Real lead-lag or direction-agreement percentages
- Real topic extraction

The UI renders unavailable states instead of inventing financial results.

## Validation commands

```bash
python -m pytest -q
python -m fmindex.pipeline --once
python -m fmindex.pipeline --serve
```

Then open `http://127.0.0.1:8420` and verify light/dark, market, period, view, and color settings at desktop and mobile widths.
