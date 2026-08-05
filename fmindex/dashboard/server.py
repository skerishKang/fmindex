"""Static Chart.js dashboard exporter and localhost server."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))
DASHBOARD_ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = DASHBOARD_ROOT / "templates" / "index.html"
CSS_PATH = DASHBOARD_ROOT / "static" / "css" / "dashboard.css"
JS_PATH = DASHBOARD_ROOT / "static" / "js" / "dashboard.js"


def _read_asset(path: Path) -> str:
    """Read a packaged dashboard asset with an explicit failure message."""
    if not path.exists():
        raise FileNotFoundError(f"Dashboard asset not found: {path}")
    return path.read_text(encoding="utf-8")


# Compatibility constant retained for callers that imported the prior embedded HTML.
DASHBOARD_HTML = _read_asset(TEMPLATE_PATH)


def filter_by_period(data: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
    """Filter records by timestamp, anchored to the latest valid record.

    Anchoring to the latest record rather than wall-clock time keeps historical
    and fixture datasets inspectable while preserving true 24h/7d/30d windows.
    """
    if period == "all" or not data:
        return list(data)

    valid: List[tuple[datetime, Dict[str, Any]]] = []
    for record in data:
        try:
            value = datetime.fromisoformat(str(record.get("timestamp", "")))
            if value.tzinfo is None:
                value = value.replace(tzinfo=KST)
            else:
                value = value.astimezone(KST)
            valid.append((value, record))
        except (ValueError, TypeError):
            continue

    if not valid:
        return []

    latest = max(value for value, _ in valid)
    if period == "24h":
        cutoff = latest - timedelta(hours=24)
    elif period == "7d":
        cutoff = latest - timedelta(days=7)
    elif period == "30d":
        cutoff = latest - timedelta(days=30)
    else:
        return list(data)

    return [record for value, record in valid if value >= cutoff]


def generate_dashboard_data(
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create the public dashboard JSON contract."""
    payload_summary = dict(summary or {})
    payload_summary.setdefault("methodologyVersion", "fmindex-v1")
    if joined and "updatedAt" not in payload_summary:
        payload_summary["updatedAt"] = joined[-1].get("timestamp")
    if joined and "marketSource" not in payload_summary:
        payload_summary["marketSource"] = joined[-1].get("source", "")

    return {
        "joined": joined,
        "overnight": overnight or {
            "status": "unavailable",
            "reason": "real_session_data_not_available",
        },
        "summary": payload_summary,
    }


def write_dashboard_files(
    output_dir: str,
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Export HTML, CSS, JavaScript and JSON into a static output directory."""
    out = Path(output_dir)
    api_dir = out / "api"
    css_dir = out / "assets" / "css"
    js_dir = out / "assets" / "js"
    api_dir.mkdir(parents=True, exist_ok=True)
    css_dir.mkdir(parents=True, exist_ok=True)
    js_dir.mkdir(parents=True, exist_ok=True)

    data = generate_dashboard_data(joined, overnight, summary)
    (api_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    html_path = out / "index.html"
    html_path.write_text(_read_asset(TEMPLATE_PATH), encoding="utf-8")
    (css_dir / "dashboard.css").write_text(_read_asset(CSS_PATH), encoding="utf-8")
    (js_dir / "dashboard.js").write_text(_read_asset(JS_PATH), encoding="utf-8")
    return str(html_path)


def serve_dashboard(directory: str, port: int = 8420, host: str = "127.0.0.1") -> None:
    """Serve a generated dashboard from localhost by default."""
    root = str(Path(directory).resolve())

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=root, **kwargs)

    print(f"FMIndex dashboard: http://{host}:{port}")
    HTTPServer((host, port), Handler).serve_forever()
