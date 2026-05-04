"""CRM lead funnel — prospect tracking and onboarding journey.

Stores leads in a JSON file alongside the Django project (no migration
required, mirroring the rest of the demo's storage philosophy). Each lead
carries a funnel stage (``new_prospect`` → ``contacted`` → ``kyc_pending``
→ ``invested``) plus a structured onboarding checklist so the advisor can
see exactly where the prospect is and what's blocking them.

Once a lead reaches ``invested`` and a PAN is recorded, we link it to the
matching Account row in the SQLAlchemy DB so portfolio data flows in.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings

# Funnel stages — order matters for the kanban display & "next step" logic.
STAGES: list[tuple[str, str, str]] = [
    # (key, display label, color css class)
    ("new_prospect", "New Prospects", "blue"),
    ("contacted",    "Contacted",     "cyan"),
    ("kyc_pending",  "KYC Pending",   "amber"),
    ("invested",     "Invested",      "emerald"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_LABELS = {k: lbl for k, lbl, _ in STAGES}
STAGE_COLORS = {k: c for k, _, c in STAGES}

# Default onboarding checklist applied to every new lead. The advisor checks
# items off as the prospect progresses; the lead's funnel stage is also
# inferred from which items are done (see ``recompute_stage``).
DEFAULT_JOURNEY: list[dict[str, Any]] = [
    {"key": "initial_contact",  "label": "Initial contact made",        "done": False, "stage": "new_prospect"},
    {"key": "risk_profile",     "label": "Risk profile assessment",     "done": False, "stage": "contacted"},
    {"key": "goals_discussed",  "label": "Investment goals discussed",  "done": False, "stage": "contacted"},
    {"key": "kyc_collected",    "label": "KYC documents collected",     "done": False, "stage": "kyc_pending"},
    {"key": "kyc_submitted",    "label": "KYC submitted to RTA",        "done": False, "stage": "kyc_pending"},
    {"key": "kyc_verified",     "label": "KYC verified",                "done": False, "stage": "kyc_pending"},
    {"key": "folio_created",    "label": "Folio created with AMC",      "done": False, "stage": "invested"},
    {"key": "first_investment", "label": "First investment made",       "done": False, "stage": "invested"},
]


def _store_path() -> Path:
    """Return the JSON file backing the lead store, ensuring its parent exists."""
    base = Path(getattr(settings, "DATA_DIR", getattr(settings, "BASE_DIR", ".")))
    p = base / "crm_leads.json"
    return p


def _load() -> list[dict[str, Any]]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _save(leads: list[dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, default=str)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def list_leads(stage: str | None = None) -> list[dict[str, Any]]:
    """All leads, optionally filtered to a single stage. Sorted newest first."""
    leads = _load()
    if stage:
        leads = [l for l in leads if l.get("stage") == stage]
    leads.sort(key=lambda l: l.get("updated_at") or l.get("created_at") or "", reverse=True)
    return leads


def funnel_counts() -> dict[str, int]:
    """Count of leads in each funnel stage (always returns all stage keys)."""
    counts = {k: 0 for k in STAGE_KEYS}
    for lead in _load():
        s = lead.get("stage")
        if s in counts:
            counts[s] += 1
    return counts


def get_lead(lead_id: str) -> dict[str, Any] | None:
    for lead in _load():
        if lead.get("id") == lead_id:
            return lead
    return None


def create_lead(
    name: str,
    phone: str = "",
    email: str = "",
    pan: str = "",
    source: str = "",
    notes: str = "",
    expected_aum: float = 0.0,
) -> dict[str, Any]:
    """Create a new lead in the New Prospects column."""
    lead: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "").strip(),
        "phone": (phone or "").strip(),
        "email": (email or "").strip(),
        "pan": (pan or "").strip().upper(),
        "source": (source or "").strip(),
        "expected_aum": float(expected_aum or 0),
        "stage": "new_prospect",
        "notes": (notes or "").strip(),
        "journey": [dict(item) for item in DEFAULT_JOURNEY],
        "activity": [{"at": _now_iso(), "kind": "created", "text": "Lead created"}],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    leads = _load()
    leads.append(lead)
    _save(leads)
    return lead


def update_lead(lead_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Update profile fields (name, phone, email, pan, source, notes, expected_aum)."""
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue
        editable = {"name", "phone", "email", "pan", "source", "notes", "expected_aum"}
        changes = []
        for k, v in fields.items():
            if k not in editable:
                continue
            new_val = v
            if k == "pan":
                new_val = (v or "").strip().upper()
            elif k == "expected_aum":
                try:
                    new_val = float(v or 0)
                except (TypeError, ValueError):
                    new_val = 0.0
            else:
                new_val = (v or "").strip() if isinstance(v, str) else v
            if lead.get(k) != new_val:
                changes.append(k)
                lead[k] = new_val
        if changes:
            lead["activity"].append({
                "at": _now_iso(), "kind": "edit",
                "text": f"Updated {', '.join(changes)}",
            })
            lead["updated_at"] = _now_iso()
        leads[i] = lead
        _save(leads)
        return lead
    return None


def set_stage(lead_id: str, stage: str) -> dict[str, Any] | None:
    """Move a lead to a specific funnel stage (manual override)."""
    if stage not in STAGE_KEYS:
        return None
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue
        old = lead.get("stage")
        if old != stage:
            lead["stage"] = stage
            lead["activity"].append({
                "at": _now_iso(), "kind": "stage",
                "text": f"Moved {STAGE_LABELS.get(old, old)} → {STAGE_LABELS.get(stage, stage)}",
            })
            lead["updated_at"] = _now_iso()
        leads[i] = lead
        _save(leads)
        return lead
    return None


def toggle_journey_item(lead_id: str, item_key: str, done: bool) -> dict[str, Any] | None:
    """Mark an onboarding-journey checkbox done/undone & auto-advance the stage."""
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue
        for item in lead.get("journey", []):
            if item.get("key") == item_key:
                if item.get("done") != done:
                    item["done"] = bool(done)
                    item["completed_at"] = _now_iso() if done else None
                    lead["activity"].append({
                        "at": _now_iso(), "kind": "task",
                        "text": ("✓ " if done else "↺ ") + item.get("label", item_key),
                    })
                break
        # Auto-advance funnel stage based on which tasks are done
        new_stage = recompute_stage(lead)
        if new_stage != lead.get("stage"):
            lead["activity"].append({
                "at": _now_iso(), "kind": "stage_auto",
                "text": f"Auto-advanced to {STAGE_LABELS.get(new_stage, new_stage)}",
            })
            lead["stage"] = new_stage
        lead["updated_at"] = _now_iso()
        leads[i] = lead
        _save(leads)
        return lead
    return None


def add_note(lead_id: str, text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue
        lead["activity"].append({"at": _now_iso(), "kind": "note", "text": text})
        lead["updated_at"] = _now_iso()
        leads[i] = lead
        _save(leads)
        return lead
    return None


def delete_lead(lead_id: str) -> bool:
    leads = _load()
    new = [l for l in leads if l.get("id") != lead_id]
    if len(new) == len(leads):
        return False
    _save(new)
    return True


def recompute_stage(lead: dict[str, Any]) -> str:
    """Infer the furthest funnel stage based on completed journey items.

    Rule: a stage is reached when at least one of its journey items is done.
    The lead's stage is the furthest reached stage (or unchanged if none).
    """
    done_stages = {item.get("stage") for item in lead.get("journey", []) if item.get("done")}
    furthest = lead.get("stage") or "new_prospect"
    for key in STAGE_KEYS:
        if key in done_stages:
            furthest = key
    return furthest


def journey_progress(lead: dict[str, Any]) -> tuple[int, int]:
    """Return (done_count, total_count) for the onboarding checklist."""
    items = lead.get("journey", []) or []
    return sum(1 for i in items if i.get("done")), len(items)


# ============================================================================
# Risk Profile Assessment
# ============================================================================
#
# A short structured questionnaire that produces a categorical risk profile
# (Conservative / Moderately Conservative / Moderate / Moderately Aggressive
# / Aggressive). The result drives the asset allocation we'll recommend to
# the client during onboarding.
#
# Each question carries 5 options scored 1-5. Score is summed across all
# six questions (range: 6 → 30) and bucketed into a category.

RISK_QUESTIONS: list[dict[str, Any]] = [
    {
        "key": "horizon",
        "q": "Over what time horizon do you plan to stay invested?",
        "opts": [
            ("Less than 1 year", 1),
            ("1 to 3 years", 2),
            ("3 to 5 years", 3),
            ("5 to 10 years", 4),
            ("More than 10 years", 5),
        ],
    },
    {
        "key": "objective",
        "q": "Which best describes your primary investment objective?",
        "opts": [
            ("Capital preservation — protect principal at all costs", 1),
            ("Regular income with minimal risk", 2),
            ("Balanced growth with moderate risk", 3),
            ("Long-term capital growth, willing to accept volatility", 4),
            ("Aggressive growth — maximise returns, high volatility OK", 5),
        ],
    },
    {
        "key": "drawdown",
        "q": "If your portfolio dropped 20% in a month, what would you do?",
        "opts": [
            ("Sell everything immediately", 1),
            ("Sell some to limit further losses", 2),
            ("Do nothing — wait it out", 3),
            ("Add a little — see opportunity", 4),
            ("Buy aggressively — markets are on sale", 5),
        ],
    },
    {
        "key": "experience",
        "q": "How would you describe your investment experience?",
        "opts": [
            ("None — this is my first investment", 1),
            ("Basic — only fixed deposits / savings", 2),
            ("Moderate — held mutual funds for a few years", 3),
            ("Experienced — actively manage equity & MF portfolio", 4),
            ("Expert — comfortable with derivatives, direct equity, alts", 5),
        ],
    },
    {
        "key": "income_stability",
        "q": "How stable is your primary source of income?",
        "opts": [
            ("Unstable / between jobs", 1),
            ("Irregular (freelance, business income varies)", 2),
            ("Stable salaried with one income source", 3),
            ("Very stable salaried with strong job security", 4),
            ("Multiple stable income sources / financially independent", 5),
        ],
    },
    {
        "key": "share_of_wealth",
        "q": "What % of your total net worth will this investment represent?",
        "opts": [
            ("More than 75%", 1),
            ("50% to 75%", 2),
            ("25% to 50%", 3),
            ("10% to 25%", 4),
            ("Less than 10%", 5),
        ],
    },
]

# (min_score, max_score, category, color, suggested allocation summary)
RISK_BUCKETS: list[dict[str, Any]] = [
    {
        "category": "Conservative",
        "min": 6, "max": 12,
        "color": "blue",
        "allocation": "20% Equity · 70% Debt · 10% Liquid",
        "blurb": "Capital safety is the priority. Recommend debt-heavy portfolio with a small equity sliver for inflation protection. Liquid funds for emergencies.",
    },
    {
        "category": "Moderately Conservative",
        "min": 13, "max": 18,
        "color": "cyan",
        "allocation": "40% Equity · 50% Debt · 10% Hybrid",
        "blurb": "Income with modest growth. Mostly debt and conservative hybrid funds; equity exposure via large-cap and balanced advantage funds.",
    },
    {
        "category": "Moderate",
        "min": 19, "max": 22,
        "color": "violet",
        "allocation": "55% Equity · 35% Debt · 10% Hybrid",
        "blurb": "Balanced approach. Diversified equity across large/mid/flexi-cap, debt for stability. Comfortable riding out 15-20% drawdowns.",
    },
    {
        "category": "Moderately Aggressive",
        "min": 23, "max": 26,
        "color": "amber",
        "allocation": "70% Equity · 20% Debt · 10% Alternatives",
        "blurb": "Growth-focused. Heavy equity tilt with mid/small-cap exposure, supplemented by debt for ballast and select alternatives (gold, REITs).",
    },
    {
        "category": "Aggressive",
        "min": 27, "max": 30,
        "color": "rose",
        "allocation": "85% Equity · 5% Debt · 10% Alternatives",
        "blurb": "Long-horizon wealth maximisation. Heavy in mid/small-cap and thematic equity. Significant volatility expected — needs strong stomach.",
    },
]


def categorise_score(score: int) -> dict[str, Any]:
    """Map a numeric score to its risk-profile bucket. Falls back to 'Moderate'."""
    for b in RISK_BUCKETS:
        if b["min"] <= score <= b["max"]:
            return b
    # Defensive fallback
    return RISK_BUCKETS[2]


def set_risk_profile(lead_id: str, answers: dict[str, int]) -> dict[str, Any] | None:
    """Save risk-assessment answers, compute the category, and mark the
    'risk_profile' onboarding task done.

    ``answers`` maps each question key (from RISK_QUESTIONS) to the chosen
    score (1-5). Missing keys are treated as 0 (no answer).
    """
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue

        # Build the structured response: { key, question, answer_label, score }
        responses: list[dict[str, Any]] = []
        total = 0
        for q in RISK_QUESTIONS:
            score = int(answers.get(q["key"], 0) or 0)
            label = ""
            for opt_label, opt_score in q["opts"]:
                if opt_score == score:
                    label = opt_label
                    break
            responses.append({
                "key": q["key"],
                "question": q["q"],
                "answer_label": label,
                "score": score,
            })
            total += score

        bucket = categorise_score(total)
        lead["risk_profile"] = {
            "score": total,
            "max_score": sum(5 for _ in RISK_QUESTIONS),
            "category": bucket["category"],
            "color": bucket["color"],
            "allocation": bucket["allocation"],
            "blurb": bucket["blurb"],
            "responses": responses,
            "completed_at": _now_iso(),
        }

        # Mark the 'risk_profile' journey task done
        for item in lead.get("journey", []):
            if item.get("key") == "risk_profile" and not item.get("done"):
                item["done"] = True
                item["completed_at"] = _now_iso()

        # Auto-advance funnel
        new_stage = recompute_stage(lead)
        if new_stage != lead.get("stage"):
            lead["stage"] = new_stage

        lead["activity"].append({
            "at": _now_iso(),
            "kind": "risk",
            "text": f"Risk assessment completed: {bucket['category']} ({total}/30)",
        })
        lead["updated_at"] = _now_iso()
        leads[i] = lead
        _save(leads)
        return lead
    return None


def reset_risk_profile(lead_id: str) -> dict[str, Any] | None:
    """Clear an existing risk profile so the assessment can be retaken."""
    leads = _load()
    for i, lead in enumerate(leads):
        if lead.get("id") != lead_id:
            continue
        if "risk_profile" in lead:
            lead.pop("risk_profile", None)
            for item in lead.get("journey", []):
                if item.get("key") == "risk_profile":
                    item["done"] = False
                    item["completed_at"] = None
            lead["activity"].append({
                "at": _now_iso(), "kind": "risk",
                "text": "Risk assessment reset",
            })
            lead["updated_at"] = _now_iso()
            leads[i] = lead
            _save(leads)
        return lead
    return None
