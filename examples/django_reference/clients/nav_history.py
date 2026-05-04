"""NAV history fetcher — used by the Research tab's fund-trend charts.

Source: **mfapi.in** (https://api.mfapi.in/mf/{amfi_scheme_code}) — a free,
community-maintained Indian Mutual Fund Open Data API that mirrors AMFI's
historical NAV publication. Keyed by AMFI's 6-digit scheme code.

We need the AMFI scheme code to fetch history. The code is captured during
the daily AMFI NAV-dump parse (clients/amfi_nav.py) and resolved per scheme
via ISIN → AMFI record lookup at chart render time.

When the AMFI code can't be resolved (rare scheme, no ISIN match) OR the
mfapi.in call fails (offline / rate-limited), this module returns a clear
'data unavailable' status — we deliberately do NOT synthesise data, since
showing a fake chart is worse than admitting no data.

Disk cache: 12-hour TTL keyed by AMFI scheme code under .nav_cache/history/.
NAVs update once a day, so a 12h cache shifts almost no real cost off the
mfapi.in service.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.conf import settings


CACHE_TTL_SEC = 60 * 60 * 12   # 12-hour disk cache (NAVs only update once/day)
MFAPI_URL = "https://api.mfapi.in/mf/{amfi_code}"


def _cache_dir() -> Path:
    base = Path(getattr(settings, "DATA_DIR", getattr(settings, "BASE_DIR", ".")))
    p = base / ".nav_cache" / "history"
    p.mkdir(exist_ok=True, parents=True)
    return p


def _cached(amfi_code: str) -> list[dict[str, Any]] | None:
    p = _cache_dir() / f"{amfi_code}.json"
    if not p.exists():
        return None
    if dt.datetime.now().timestamp() - p.stat().st_mtime > CACHE_TTL_SEC:
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(amfi_code: str, series: list[dict[str, Any]]) -> None:
    p = _cache_dir() / f"{amfi_code}.json"
    try:
        with p.open("w", encoding="utf-8") as f:
            json.dump(series, f)
    except OSError:
        pass


def _try_mfapi(amfi_code: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Try fetching NAV history from mfapi.in.

    Returns ``(series, error_msg)``. ``series`` is None on failure.
    """
    if not amfi_code or not amfi_code.isdigit():
        return None, f"AMFI scheme code missing or invalid ({amfi_code!r})"
    url = MFAPI_URL.format(amfi_code=amfi_code)
    try:
        req = Request(url, headers={"User-Agent": "openreversefeed/0.1"})
        with urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        return None, f"mfapi.in fetch failed: {type(e).__name__}"

    rows = data.get("data", [])
    if not rows:
        return None, "mfapi.in returned no historical NAVs for this scheme"

    out = []
    for r in rows:
        try:
            d = dt.datetime.strptime(r["date"], "%d-%m-%Y").date()
            out.append({"date": d.isoformat(), "nav": float(r["nav"])})
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda x: x["date"])
    if not out:
        return None, "mfapi.in returned data but every row failed to parse"
    return out, None


def fetch_history(amfi_code: str = "") -> dict[str, Any]:
    """Return NAV history for a scheme.

    Output:
        {
            "series": [{"date": "YYYY-MM-DD", "nav": float}, ...],  # may be []
            "available": bool,
            "source": "mfapi.in (cached)" | "mfapi.in (live)" | None,
            "source_url": str | None,
            "amfi_code": str,
            "fetched_at": ISO datetime str,
            "error": str | None,   # explanation when available=False
        }
    """
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")
    if not amfi_code or not str(amfi_code).strip().isdigit():
        return {
            "series": [],
            "available": False,
            "source": None,
            "source_url": None,
            "amfi_code": amfi_code or "",
            "fetched_at": fetched_at,
            "error": "AMFI scheme code is not on file for this fund — chart cannot be drawn.",
        }

    cached = _cached(amfi_code)
    if cached is not None:
        return {
            "series": cached,
            "available": True,
            "source": "mfapi.in (cached, ≤12h old)",
            "source_url": MFAPI_URL.format(amfi_code=amfi_code),
            "amfi_code": amfi_code,
            "fetched_at": fetched_at,
            "error": None,
        }

    series, err = _try_mfapi(amfi_code)
    if series is not None:
        _save_cache(amfi_code, series)
        return {
            "series": series,
            "available": True,
            "source": "mfapi.in (live)",
            "source_url": MFAPI_URL.format(amfi_code=amfi_code),
            "amfi_code": amfi_code,
            "fetched_at": fetched_at,
            "error": None,
        }

    return {
        "series": [],
        "available": False,
        "source": None,
        "source_url": MFAPI_URL.format(amfi_code=amfi_code),
        "amfi_code": amfi_code,
        "fetched_at": fetched_at,
        "error": err or "Unknown error fetching from mfapi.in",
    }


def slice_timeframe(series: list[dict], tf: str) -> list[dict]:
    """Filter to a window: 1M, 3M, 6M, 1Y, 3Y, 5Y, ALL."""
    if not series:
        return []
    days_map = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "3Y": 1095, "5Y": 1825}
    if tf not in days_map:
        return series
    last_date = dt.date.fromisoformat(series[-1]["date"])
    cutoff = last_date - dt.timedelta(days=days_map[tf])
    return [r for r in series if dt.date.fromisoformat(r["date"]) >= cutoff]


def summary_metrics(series: list[dict]) -> dict[str, Any]:
    """Compute high-level stats over the given window: return %, high, low,
    annualised volatility (σ × √252)."""
    if not series or len(series) < 2:
        return {"return_pct": None, "high": None, "low": None, "volatility": None}

    navs = [r["nav"] for r in series]
    first, last = navs[0], navs[-1]
    return_pct = round((last / first - 1) * 100, 2) if first else None

    # Daily log returns for volatility
    import math
    rets = []
    for i in range(1, len(navs)):
        if navs[i - 1] > 0:
            rets.append(math.log(navs[i] / navs[i - 1]))
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        vol_annualised = round(math.sqrt(var) * math.sqrt(252) * 100, 2)
    else:
        vol_annualised = None

    return {
        "return_pct": return_pct,
        "high": round(max(navs), 4),
        "low": round(min(navs), 4),
        "volatility": vol_annualised,
    }
