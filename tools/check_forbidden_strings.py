"""Fail CI if any internal brand names, personal identifiers, real registrar
reference data, or hardcoded secrets slip into the tracked source tree.

Run with `python tools/check_forbidden_strings.py`. Exits 1 on any hit.
Scans every file that git is tracking, skipping binaries and a small
allow-list of files that legitimately contain matches (e.g. the Apache
LICENSE has 'NON-INFRINGEMENT' which trips substring matchers).
"""
from __future__ import annotations

import re

# Intentional subprocess use — shells to `git ls-files` for the tracked file list.
import subprocess  # nosec B404
import sys
from pathlib import Path

FORBIDDEN = [
    # Source org / brands
    (r"\baowealth\b", "aowealth (source org)"),
    (r"\bionic\.in\b", "ionic.in (source domain)"),
    (r"\baowealth\.in\b", "aowealth.in (source domain)"),
    (r"\baarambh\b", "aarambh (source internal)"),
    (r"\bAngelOne\b", "AngelOne brand (use org name in URLs only)"),
    # Personal identifiers
    (r"sandeep[._-]?raju", "personal identifier"),
    (r"sandeepraju", "personal identifier"),
    (r"@ionic\.in", "personal email"),
    (r"@aowealth\.in", "personal email"),
    # Source repo paths (sibling repos in the original workspace)
    (r"aow_workspace", "source workspace path"),
    (r"ao_wealth_workspace", "source workspace path"),
    (r"holding/app/util/reverse_feed", "source internal path"),
    (r"crm_portal", "source internal schema"),
    (r"mf_central", "source internal schema"),
    # Real registrar reference data — scheme codes should all use SYN prefix
    (r"INF[0-9][0-9A-Z]{8,9}(?![A-Z])", "real-looking ISIN (use SYN-prefixed synthetic codes)"),
    # Real fund names that might slip in via copy-paste
    (r"\bICICI\s+Pru\b", "real AMC fund name"),
    (r"\bHDFC\s+Top\b", "real AMC fund name"),
    (r"\bSBI\s+Magnum\b", "real AMC fund name"),
    (r"\bAxis\s+Small\s+Cap\b", "real AMC fund name"),
    (r"\bFranklin\s+India\b", "real AMC fund name"),
    # Hardcoded credentials and secrets
    (r'password\s*=\s*["\'][^"\']{3,}["\']', "hardcoded password literal"),
    (r'api[_-]?key\s*=\s*["\'][^"\']{10,}["\']', "hardcoded API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"sk_live_[0-9a-zA-Z]{24,}", "Stripe live secret"),
    (r"ghp_[0-9a-zA-Z]{36}", "GitHub personal access token"),
    (r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----", "private key material"),
]

# Files we never want to scan (content-wise): binaries, license texts, etc.
ALLOW_LIST_FILES = {
    "LICENSE",  # Apache text has "NON-INFRINGEMENT" which false-matches loose patterns
    "NOTICE",
    "tools/check_forbidden_strings.py",  # this file — contains the patterns themselves
}

# Extensions that are never text
BINARY_EXTS = {".dbf", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".whl", ".so", ".pyc"}


def tracked_files() -> list[Path]:
    # git is a trusted binary; argv is a hardcoded constant, no user input.
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)  # nosec B603 B607
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    patterns = [(re.compile(p), label) for p, label in FORBIDDEN]
    hits: list[str] = []

    for path in tracked_files():
        if str(path) in ALLOW_LIST_FILES:
            continue
        if path.suffix.lower() in BINARY_EXTS:
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            for regex, label in patterns:
                if regex.search(line):
                    hits.append(f"{path}:{line_no}: [{label}] {line.strip()[:160]}")

    if hits:
        print("FORBIDDEN-STRINGS SCAN FAILED:")
        for h in hits:
            print(f"  {h}")
        print(f"\n{len(hits)} hit(s). Replace the content and re-run.")
        return 1

    print(f"forbidden-strings scan: clean ({len(tracked_files())} files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
