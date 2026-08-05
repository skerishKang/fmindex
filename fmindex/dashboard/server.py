"""Chart.js dashboard server — serves the FMIndex dashboard.

The product UI lives in separate files (templates/ + static/) instead of a
giant inline HTML string:

    fmindex/dashboard/templates/index.html
    fmindex/dashboard/static/css/dashboard.css
    fmindex/dashboard/static/js/dashboard.js

write_dashboard_files copies them into the output directory so the local
http server can serve them as ordinary static assets.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))

_PKG_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = _PKG_DIR / "templates" / "index.html"
STATIC_CSS_PATH = _PKG_DIR / "static" / "css" / "dashboard.css"
STATIC_JS_PATH = _PKG_DIR / "static" / "js" / "dashboard.js"


def filter_by_period(data: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
    """Filter joined records by timestamp-based period.

    The cutoff is anchored to the latest valid record timestamp (not the
    wall-clock time) so historical and fixture datasets remain inspectable
    and the result is deterministic. Boundary rule: timestamp >= cutoff.

    Args:
        data: List of joined records with 'timestamp' field.
        period: '24h', '7d', '30d', or 'all'.

    Returns:
        Filtered list of records.
    """
    if period == "all" or not data:
        return list(data)

    valid: List[tuple[datetime, Dict[str, Any]]] = []
    for rec in data:
        ts = rec.get("timestamp", "")
        try:
            rec_dt = datetime.fromisoformat(ts)
            if rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=KST)
            else:
                rec_dt = rec_dt.astimezone(KST)
            valid.append((rec_dt, rec))
        except (ValueError, TypeError):
            continue

    if not valid:
        return []

    latest = max(dt for dt, _ in valid)
    if period == "24h":
        cutoff = latest - timedelta(hours=24)
    elif period == "7d":
        cutoff = latest - timedelta(days=7)
    elif period == "30d":
        cutoff = latest - timedelta(days=30)
    else:
        return list(data)

    return [rec for dt, rec in valid if dt >= cutoff]


def _read_asset(path: Path, fallback: str = "") -> str:
    """Read a template/static asset, falling back when the file is missing."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return fallback


def generate_dashboard_data(
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the dashboard data JSON.

    Missing summary fields are filled with defaults via setdefault; caller
    (pipeline) values are never overwritten. Overnight defaults to the
    unavailable contract when not provided.
    """
    payload_summary = dict(summary or {})
    payload_summary.setdefault("methodologyVersion", "fmindex-v1")
    payload_summary.setdefault("dataMode", "unavailable")
    payload_summary.setdefault("provider", "")
    if joined and "lastUpdated" not in payload_summary:
        payload_summary["lastUpdated"] = joined[-1].get("timestamp")
    if joined and "marketSource" not in payload_summary:
        payload_summary["marketSource"] = joined[-1].get("source", "")

    overnight_payload = overnight
    if overnight_payload is None:
        overnight_payload = {
            "status": "unavailable",
            "reason": "real_session_data_not_available",
        }

    return {
        "joined": joined,
        "overnight": overnight_payload,
        "summary": payload_summary,
    }


def write_dashboard_files(
    output_dir: str,
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Write dashboard HTML, static assets, and data JSON to output directory.

    Returns the path to the HTML file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    data = generate_dashboard_data(joined, overnight, summary)
    data_path = out / "api" / "data.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    css_dir = out / "css"
    js_dir = out / "js"
    css_dir.mkdir(parents=True, exist_ok=True)
    js_dir.mkdir(parents=True, exist_ok=True)

    (css_dir / "dashboard.css").write_text(
        _read_asset(STATIC_CSS_PATH), encoding="utf-8"
    )
    (js_dir / "dashboard.js").write_text(
        _read_asset(STATIC_JS_PATH), encoding="utf-8"
    )

    html_path = out / "index.html"
    html_path.write_text(_read_asset(TEMPLATE_PATH), encoding="utf-8")

    return str(html_path)


def serve_dashboard(directory: str, port: int = 8420, host: str = "127.0.0.1") -> None:
    """Serve the dashboard directory on localhost.

    The handler receives the root directory explicitly; the process-wide
    current working directory is never changed.
    """
    root = str(Path(directory).resolve())

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

    print(f"FMIndex dashboard: http://{host}:{port}")
    HTTPServer((host, port), Handler).serve_forever()
