"""AMFI NAV fetcher with daily on-disk cache.

Fetches the latest NAV data from AMFI and builds two lookups:
- ISIN -> NAV, scheme_name, nav_date
- scheme_name (normalized) -> NAV, scheme_name, nav_date

Name matching goes through three layers:
  1. Exact lowercased match
  2. Punctuation-stripped match (handles dashes vs spaces variants)
  3. Token-overlap scoring (Jaccard) with a high threshold

The feed lives at https://www.amfiindia.com/spages/NAVAll.txt and refreshes
daily (EOD). The cache file is stored at BASE_DIR/.nav_cache/nav_YYYY-MM-DD.txt
so we only hit the network once per day.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import requests

_AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
_CACHE_DIR_NAME = ".nav_cache"

_nav_cache: dict[str, Any] | None = None
_nav_cache_date: str | None = None


def _cache_dir() -> Path:
    from django.conf import settings

    cache = Path(getattr(settings, "DATA_DIR", settings.BASE_DIR)) / _CACHE_DIR_NAME
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _today_str() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


# A complete AMFI NAVAll.txt is consistently ~1.5–2 MB. Anything substantially
# smaller is a truncated/incomplete download (server hiccup, network drop, or
# AMFI returning an error page). Reject it and fall back to the previous day's
# cache instead of silently serving wrong NAVs that would corrupt every XIRR.
_MIN_VALID_BYTES = 800_000


def _is_valid_nav_dump(text: str) -> bool:
    """Quick sanity check on a downloaded/cached AMFI dump."""
    if not text or len(text) < _MIN_VALID_BYTES:
        return False
    # The real file has thousands of pipe-delimited rows; a truncated one
    # often has a few hundred. ≥ 5,000 lines is a safe minimum.
    if text.count("\n") < 5000:
        return False
    return True


def _most_recent_good_cache() -> tuple[str, str] | None:
    """Return (date_str, text) of the newest cached file that passes validation."""
    for path in sorted(_cache_dir().glob("nav_*.txt"), reverse=True):
        try:
            if path.stat().st_size < _MIN_VALID_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
            if _is_valid_nav_dump(text):
                # filename "nav_YYYY-MM-DD.txt"
                return path.stem.replace("nav_", ""), text
        except OSError:
            continue
    return None


def _fetch_raw(date_str: str) -> str:
    """Fetch AMFI raw text, caching on disk per day.

    Validates both cached and freshly-downloaded responses against a minimum
    size — a truncated file silently corrupts every NAV lookup and turns all
    client XIRRs negative, so we'd rather fall back to a stale-but-complete
    cache than trust a partial dump.
    """
    cache_file = _cache_dir() / f"nav_{date_str}.txt"

    # 1. Use today's cache only if it looks complete
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        if _is_valid_nav_dump(text):
            return text
        # Bad cache — delete it so the next call retries cleanly
        try:
            cache_file.unlink()
        except OSError:
            pass

    # 2. Try a live fetch
    try:
        resp = requests.get(_AMFI_URL, timeout=30)
        resp.raise_for_status()
        if _is_valid_nav_dump(resp.text):
            cache_file.write_text(resp.text, encoding="utf-8")
            return resp.text
    except requests.RequestException:
        pass

    # 3. Last-resort: serve the most recent COMPLETE cache (even if days old)
    fallback = _most_recent_good_cache()
    if fallback is not None:
        return fallback[1]

    raise RuntimeError(
        f"No usable AMFI NAV data available — live fetch failed and no good cache exists "
        f"in {_cache_dir()}."
    )


_PUNCT_RE = re.compile(r"[\s\-_/\(\)\.,&]+")
_NOISE_TOKENS = {
    "fund", "plan", "option", "growth", "regular", "direct", "scheme",
    "the", "of", "mutual", "an", "idcw", "payout", "reinvestment",
    "former", "erstwhile", "formerly", "known", "as", "new", "old",
}


_RENAME_RE = re.compile(
    r"\s*[\(\[]?\s*(formerly|former|erstwhile|earlier|previously)\b.*",
    re.IGNORECASE,
)


def _strip_rename_suffix(name: str) -> str:
    """Drop '(Formerly Known As ...)' annotations that pollute token matching."""
    return _RENAME_RE.sub("", name).strip().rstrip("(").strip()


def _normalize_name(name: str) -> str:
    """Punctuation-insensitive normalization for name matching."""
    return _PUNCT_RE.sub(" ", _strip_rename_suffix(name).lower()).strip()


def _tokenize(name: str) -> frozenset[str]:
    """Return informative tokens (excludes common noise words) for fuzzy matching."""
    tokens = _PUNCT_RE.sub(" ", _strip_rename_suffix(name).lower()).split()
    return frozenset(t for t in tokens if t and t not in _NOISE_TOKENS)


def _parse_nav_text(text: str) -> dict[str, Any]:
    """Parse the AMFI text into lookups.

    Format: Scheme Code;ISIN Div Payout/Growth;ISIN Div Reinvest;Scheme Name;NAV;Date
    """
    isin_map: dict[str, dict[str, Any]] = {}
    name_map: dict[str, dict[str, Any]] = {}
    norm_map: dict[str, dict[str, Any]] = {}
    token_index: list[tuple[frozenset[str], dict[str, Any]]] = []
    by_code: dict[str, dict[str, Any]] = {}

    for line in text.splitlines():
        parts = line.strip().split(";")
        if len(parts) != 6:
            continue
        code, isin_g, isin_r, name, nav_s, nav_date = (p.strip() for p in parts)
        if not name or name.startswith("Scheme Name"):
            continue
        try:
            nav_val = float(nav_s)
        except ValueError:
            continue

        record = {
            "amfi_code": code,
            "isin_growth": isin_g,
            "isin_reinvest": isin_r,
            "scheme_name": name,
            "nav": nav_val,
            "nav_date": nav_date,
        }
        by_code[code] = record

        for isin in (isin_g, isin_r):
            if isin and isin != "-":
                isin_map[isin] = record

        name_map[name.lower().strip()] = record
        norm_map[_normalize_name(name)] = record
        token_index.append((_tokenize(name), record))

    return {
        "by_isin": isin_map,
        "by_name": name_map,
        "by_norm": norm_map,
        "token_index": token_index,
        "by_code": by_code,
    }


def load_nav_map(force_refresh: bool = False) -> dict[str, Any]:
    """Return a dict with keys: by_isin, by_name, by_code. Cached for today."""
    global _nav_cache, _nav_cache_date

    today = _today_str()
    if not force_refresh and _nav_cache is not None and _nav_cache_date == today:
        return _nav_cache

    try:
        text = _fetch_raw(today)
    except Exception:
        # Fallback: reuse any cached file, even if it's stale
        candidates = sorted(_cache_dir().glob("nav_*.txt"), reverse=True)
        if candidates:
            text = candidates[0].read_text(encoding="utf-8")
        else:
            return {"by_isin": {}, "by_name": {}, "by_code": {}}

    _nav_cache = _parse_nav_text(text)
    _nav_cache_date = today
    return _nav_cache


def lookup_nav(scheme_code: str, scheme_name: str | None = None, isin: str | None = None):
    """Lookup current NAV for a scheme. Returns (nav, nav_date, matched_name) or (None, None, None).

    Match order:
      1. ISIN (definitive)
      2. Exact lowercased name
      3. Punctuation-stripped normalized name (handles dash/space variants)
      4. Token-overlap (Jaccard ≥ 0.75) — unique best match wins
    """
    nav_map = load_nav_map()

    if isin and isin in nav_map["by_isin"]:
        r = nav_map["by_isin"][isin]
        return r["nav"], r["nav_date"], r["scheme_name"]

    if not scheme_name:
        return None, None, None

    sn = scheme_name.lower().strip()
    if sn in nav_map["by_name"]:
        r = nav_map["by_name"][sn]
        return r["nav"], r["nav_date"], r["scheme_name"]

    normalized = _normalize_name(scheme_name)
    if normalized in nav_map["by_norm"]:
        r = nav_map["by_norm"][normalized]
        return r["nav"], r["nav_date"], r["scheme_name"]

    # Token overlap (Jaccard) fuzzy match with plan-type tiebreakers
    our_tokens = _tokenize(scheme_name)
    if not our_tokens:
        return None, None, None

    source_lower = scheme_name.lower()
    want_growth = "growth" in source_lower
    want_idcw = any(k in source_lower for k in ("idcw", "dividend", "reinvest", "payout"))
    want_direct = "direct" in source_lower
    want_regular = "regular" in source_lower or not want_direct  # default to Regular

    def preference_score(amfi_name: str) -> float:
        """Higher = better match for plan type + growth/dividend option."""
        n = amfi_name.lower()
        score = 0.0
        # Growth vs IDCW: strong preference
        if want_growth and "growth" in n and "idcw" not in n and "dividend" not in n:
            score += 2.0
        elif want_growth and ("idcw" in n or "dividend" in n):
            score -= 2.0
        if want_idcw and ("idcw" in n or "dividend" in n):
            score += 2.0
        # Regular vs Direct: medium preference
        if want_regular and "regular" in n:
            score += 1.0
        elif want_regular and "direct" in n:
            score -= 1.0
        if want_direct and "direct" in n:
            score += 1.0
        return score

    scored: list[tuple[float, float, dict]] = []  # (jaccard, preference, record)
    for tokens, record in nav_map["token_index"]:
        if not tokens:
            continue
        inter = len(our_tokens & tokens)
        if inter == 0:
            continue
        union = len(our_tokens | tokens)
        jaccard = inter / union
        coverage = inter / len(our_tokens)
        if coverage < 0.8 or jaccard < 0.5:
            continue
        pref = preference_score(record["scheme_name"])
        scored.append((jaccard, pref, record))

    if not scored:
        return None, None, None

    # Sort: highest jaccard, then highest preference
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    best_jaccard, best_pref, best_record = scored[0]

    # Accept if: (a) strong jaccard and we have plan-type preference, or
    # (b) clear gap over second-best
    if best_jaccard >= 0.75:
        if len(scored) == 1 or best_pref > scored[1][1]:
            return best_record["nav"], best_record["nav_date"], best_record["scheme_name"]
        # Gap on jaccard
        if (best_jaccard - scored[1][0]) >= 0.05:
            return best_record["nav"], best_record["nav_date"], best_record["scheme_name"]

    # Even with lower jaccard, accept if plan-type preference is decisive
    if best_jaccard >= 0.65 and best_pref >= 3.0:
        second_pref = scored[1][1] if len(scored) > 1 else -10
        if best_pref - second_pref >= 2.0:
            return best_record["nav"], best_record["nav_date"], best_record["scheme_name"]

    return None, None, None


def lookup_amfi_record(scheme_code: str, scheme_name: str | None = None, isin: str | None = None) -> dict | None:
    """Like ``lookup_nav`` but returns the FULL AMFI record (incl. amfi_code,
    isin variants, name) so callers can drive history fetches via mfapi.in.

    Returns None when no match is found above the same confidence thresholds
    used by ``lookup_nav``.
    """
    nav_map = load_nav_map()

    if isin and isin in nav_map["by_isin"]:
        return nav_map["by_isin"][isin]

    if not scheme_name:
        return None

    sn = scheme_name.lower().strip()
    if sn in nav_map["by_name"]:
        return nav_map["by_name"][sn]

    normalized = _normalize_name(scheme_name)
    if normalized in nav_map["by_norm"]:
        return nav_map["by_norm"][normalized]

    # Fuzzy match — replicate lookup_nav's logic but return record on success
    our_tokens = _tokenize(scheme_name)
    if not our_tokens:
        return None
    source_lower = scheme_name.lower()
    want_growth = "growth" in source_lower
    want_idcw = any(k in source_lower for k in ("idcw", "dividend", "reinvest", "payout"))
    want_direct = "direct" in source_lower
    want_regular = "regular" in source_lower or not want_direct

    def pref_score(amfi_name: str) -> float:
        n = amfi_name.lower()
        s = 0.0
        if want_growth and "growth" in n and "idcw" not in n and "dividend" not in n:
            s += 2.0
        elif want_growth and ("idcw" in n or "dividend" in n):
            s -= 2.0
        if want_idcw and ("idcw" in n or "dividend" in n):
            s += 2.0
        if want_regular and "regular" in n:
            s += 1.0
        elif want_regular and "direct" in n:
            s -= 1.0
        if want_direct and "direct" in n:
            s += 1.0
        return s

    scored = []
    for tokens, record in nav_map["token_index"]:
        if not tokens:
            continue
        inter = len(our_tokens & tokens)
        if inter == 0:
            continue
        jaccard = inter / len(our_tokens | tokens)
        coverage = inter / len(our_tokens)
        if coverage < 0.8 or jaccard < 0.5:
            continue
        scored.append((jaccard, pref_score(record["scheme_name"]), record))

    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_j, best_p, best_r = scored[0]
    if best_j >= 0.75 and (len(scored) == 1 or best_p > scored[1][1] or (best_j - scored[1][0]) >= 0.05):
        return best_r
    if best_j >= 0.65 and best_p >= 3.0:
        second = scored[1][1] if len(scored) > 1 else -10
        if best_p - second >= 2.0:
            return best_r
    return None
