"""Client-facing dashboard views — list, detail, per-holding drill-down."""
from __future__ import annotations

from collections import defaultdict

import datetime as dt

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from sqlalchemy import select

from openreversefeed.db.models import Scheme
from reference_app.ofr_bridge import get_session_factory

from openreversefeed.db.models import Account

from . import (
    benchmarks, calendar_store, crm, insights_cache, nav_history, research,
    research_rules, yield_optimizer,
)
from .amfi_nav import lookup_nav
from .arn_dashboard import compute_arn_dashboard
from .enrich import backfill_isins_from_amfi, backfill_scheme_names, deduplicate_accounts
from .portfolio import (
    classify_asset,
    compute_client_holdings,
    compute_portfolio_summary,
    enrich_with_current_value,
    get_client,
    get_client_folios,
    list_all_client_transactions,
    list_clients_summary,
    list_holding_transactions,
)


def arn_dashboard(request):
    """Firm-level (ARN) dashboard — aggregates all clients into a single view."""
    Session = get_session_factory()
    with Session() as session:
        # Run idempotent enrichments first so AMC names / ISINs / dedupes are in place
        backfill_scheme_names(session)
        backfill_isins_from_amfi(session)
        deduplicate_accounts(session)
        data = compute_arn_dashboard(session)
    return render(request, "clients/arn_dashboard.html", {"active": "arn", "d": data})


def client_list(request):
    """List all clients with aggregate stats + Current AUM."""
    Session = get_session_factory()
    with Session() as session:
        backfill_scheme_names(session)  # idempotent — only fills missing names/ISINs
        backfill_isins_from_amfi(session)  # fill ISINs from AMFI for CAMS schemes that lack them
        deduplicate_accounts(session)  # merge rows sharing same PAN + name + ownership
        clients = list_clients_summary(session, nav_lookup=lookup_nav)

    q = (request.GET.get("q") or "").strip().lower()
    if q:
        clients = [
            c for c in clients
            if q in (c["name"] or "").lower() or q in (c["pan"] or "").lower()
        ]

    return render(
        request,
        "clients/list.html",
        {
            "clients": clients,
            "q": q,
            "active": "clients",
            "total_clients": len(clients),
        },
    )


def client_detail(request, pan: str):
    """Client dashboard — profile, portfolio summary, folios, holdings."""
    Session = get_session_factory()
    with Session() as session:
        backfill_scheme_names(session)
        account = get_client(session, pan)
        if account is None:
            raise Http404(f"No client with PAN {pan}")

        folios = get_client_folios(session, account.id)
        holdings = compute_client_holdings(session, account.id)
        enrich_with_current_value(holdings, lookup_nav)
        summary = compute_portfolio_summary(holdings)

    # Group holdings by folio for display
    holdings_by_folio: dict[str, list] = defaultdict(list)
    for h in holdings:
        holdings_by_folio[h.folio_number].append(h)

    # Attach asset category to each holding for the holdings table
    for h in holdings:
        h.asset_class = classify_asset(h.scheme_name)

    # Insights tab — read whatever's already in the cache (no compute on page load)
    cached_insights = insights_cache.get(pan)
    insight_freshness = (
        insights_cache.freshness_label(cached_insights["generated_at"])
        if cached_insights else None
    )
    # Pre-compute severity breakdown so the template doesn't need JS counting
    insight_sev_counts = {"high": 0, "medium": 0, "low": 0}
    if cached_insights:
        for ins in cached_insights.get("insights", []):
            s = ins.get("severity")
            if s in insight_sev_counts:
                insight_sev_counts[s] += 1

    # Honour ?tab=insights / ?tab=portfolio / ?tab=profile coming from a redirect
    requested_tab = (request.GET.get("tab") or "").strip().lower()
    if requested_tab not in ("profile", "portfolio", "insights"):
        requested_tab = ""

    return render(
        request,
        "clients/detail.html",
        {
            "active": "clients",
            "account": account,
            "pan": pan,
            "folios": folios,
            "holdings": holdings,
            "holdings_by_folio": dict(holdings_by_folio),
            "summary": summary,
            "cached_insights": cached_insights,
            "insight_freshness": insight_freshness,
            "insight_sev_counts": insight_sev_counts,
            "requested_tab": requested_tab,
        },
    )


def all_transactions(request, pan: str):
    """All transactions for a client across all schemes, with tag filtering."""
    Session = get_session_factory()
    with Session() as session:
        account = get_client(session, pan)
        if account is None:
            raise Http404(f"No client with PAN {pan}")
        txns = list_all_client_transactions(session, account.id)

    # Filters
    tag_filter = (request.GET.get("tag") or "all").lower()
    action_filter = (request.GET.get("action") or "all").lower()
    search = (request.GET.get("q") or "").strip().lower()

    filtered = txns
    if tag_filter != "all":
        filtered = [t for t in filtered if (t["tag"] or "").lower() == tag_filter]
    if action_filter != "all":
        filtered = [t for t in filtered if (t["action"] or "").lower() == action_filter]
    if search:
        filtered = [
            t for t in filtered
            if search in (t["scheme_code"] or "").lower()
            or search in (t["scheme_name"] or "").lower()
            or search in (t["folio_number"] or "").lower()
            or search in (t["reference"] or "").lower()
        ]

    # Available tags & action counts for filter chips
    tag_counts: dict[str, int] = defaultdict(int)
    action_counts: dict[str, int] = defaultdict(int)
    for t in txns:
        tag_counts[t["tag"] or "other"] += 1
        action_counts[t["action"] or "other"] += 1

    return render(
        request,
        "clients/transactions.html",
        {
            "active": "clients",
            "account": account,
            "pan": pan,
            "transactions": filtered,
            "total_count": len(txns),
            "filtered_count": len(filtered),
            "tag_counts": sorted(tag_counts.items(), key=lambda kv: -kv[1]),
            "action_counts": sorted(action_counts.items(), key=lambda kv: -kv[1]),
            "tag_filter": tag_filter,
            "action_filter": action_filter,
            "search": search,
        },
    )


def holding_detail(request, pan: str, scheme_code: str):
    """Per-holding drill-down — all transactions for this scheme."""
    Session = get_session_factory()
    with Session() as session:
        account = get_client(session, pan)
        if account is None:
            raise Http404(f"No client with PAN {pan}")

        scheme = session.execute(
            select(Scheme).where(Scheme.scheme_code == scheme_code)
        ).scalars().first()
        if scheme is None:
            raise Http404(f"No scheme with code {scheme_code}")

        # Aggregate scheme-level holding (across folios for this client)
        all_holdings = compute_client_holdings(session, account.id)
        matching = [h for h in all_holdings if h.scheme_code == scheme_code]
        enrich_with_current_value(matching, lookup_nav)

        txns = list_holding_transactions(session, account.id, scheme.id)

    # SIP Book — filter the account's systematic plans to this scheme
    all_plans = (account.meta or {}).get("systematic_plans", [])
    sip_book = [
        p for p in all_plans if p.get("scheme_code") == scheme_code
    ]

    # Combine per-folio holdings for this scheme
    total_units = sum(float(h.units_held) for h in matching)
    total_invested = sum(float(h.invested) for h in matching)
    total_current = sum(h.current_value for h in matching if h.current_value) or 0
    total_realized = sum(h.realized_gain for h in matching)
    total_unrealized = (total_current - total_invested) if total_current else 0
    total_gain = total_realized + total_unrealized if (total_current or total_realized) else None

    # Per-tag breakdown
    by_tag: dict[str, dict] = defaultdict(lambda: {"count": 0, "units": 0.0, "amount": 0.0})
    for t in txns:
        tag = t["tag"] or "other"
        by_tag[tag]["count"] += 1
        by_tag[tag]["units"] += t["units"]
        by_tag[tag]["amount"] += t["amount"]
    tag_summary = [
        {"tag": k, **v} for k, v in sorted(by_tag.items(), key=lambda kv: -kv[1]["count"])
    ]

    current_nav = matching[0].current_nav if matching else None
    nav_date = matching[0].nav_date if matching else None

    return render(
        request,
        "clients/holding.html",
        {
            "active": "clients",
            "account": account,
            "pan": pan,
            "scheme": scheme,
            "scheme_name": matching[0].scheme_name if matching else scheme.name,
            "asset_class": classify_asset(matching[0].scheme_name if matching else scheme.name),
            "total_units": round(total_units, 3),
            "total_invested": round(total_invested, 2),
            "total_current": round(total_current, 2) if total_current else None,
            "total_realized": round(total_realized, 2),
            "total_unrealized": round(total_unrealized, 2) if total_current else None,
            "total_gain": round(total_gain, 2) if total_gain is not None else None,
            "current_nav": current_nav,
            "nav_date": nav_date,
            "xirr_pct": matching[0].xirr_pct if matching else None,
            "per_folio": matching,
            "transactions": txns,
            "tag_summary": tag_summary,
            "sip_book": sip_book,
            "active_sips": [p for p in sip_book if p.get("status") == "active"],
            "paused_sips": [p for p in sip_book if p.get("status") == "paused"],
            "cancelled_sips": [p for p in sip_book if p.get("status") in ("cancelled", "completed")],
        },
    )


# ============================================================================
# CRM — lead funnel + onboarding journey
# ============================================================================

def crm_board(request):
    """Kanban-style funnel: New Prospects → Contacted → KYC Pending → Invested."""
    leads = crm.list_leads()
    counts = crm.funnel_counts()
    columns = [
        {
            "key": key,
            "label": label,
            "color": color,
            "count": counts.get(key, 0),
            "leads": [
                {**l, "progress": crm.journey_progress(l)}
                for l in leads if l.get("stage") == key
            ],
        }
        for key, label, color in crm.STAGES
    ]
    total_pipeline = sum(float(l.get("expected_aum") or 0) for l in leads if l.get("stage") != "invested")
    invested_aum = sum(float(l.get("expected_aum") or 0) for l in leads if l.get("stage") == "invested")
    return render(
        request,
        "clients/crm_board.html",
        {
            "active": "crm",
            "columns": columns,
            "stages": crm.STAGES,
            "total_leads": len(leads),
            "total_pipeline": total_pipeline,
            "invested_aum": invested_aum,
            "conversion_rate": (counts["invested"] / len(leads) * 100) if leads else 0,
        },
    )


@require_POST
def crm_create(request):
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Lead name is required.")
        return redirect(reverse("clients:crm"))
    crm.create_lead(
        name=name,
        phone=request.POST.get("phone", ""),
        email=request.POST.get("email", ""),
        pan=request.POST.get("pan", ""),
        source=request.POST.get("source", ""),
        notes=request.POST.get("notes", ""),
        expected_aum=request.POST.get("expected_aum", 0) or 0,
    )
    messages.success(request, f"Added {name} to the funnel.")
    return redirect(reverse("clients:crm"))


def crm_detail(request, lead_id: str):
    """Full onboarding journey for a single lead — checklist + activity log."""
    lead = crm.get_lead(lead_id)
    if not lead:
        raise Http404("Lead not found")

    # Group journey items by stage so the timeline reads top-to-bottom
    journey_by_stage: dict[str, list[dict]] = {k: [] for k in crm.STAGE_KEYS}
    for item in lead.get("journey", []):
        s = item.get("stage", "new_prospect")
        journey_by_stage.setdefault(s, []).append(item)

    done_count, total_count = crm.journey_progress(lead)
    pct = int(done_count / total_count * 100) if total_count else 0

    # Activity in reverse chronological order
    activity = list(reversed(lead.get("activity", [])))

    return render(
        request,
        "clients/crm_detail.html",
        {
            "active": "crm",
            "lead": lead,
            "stages": crm.STAGES,
            "stage_labels": crm.STAGE_LABELS,
            "journey_by_stage": journey_by_stage,
            "progress_done": done_count,
            "progress_total": total_count,
            "progress_pct": pct,
            "activity": activity,
        },
    )


@require_POST
def crm_update(request, lead_id: str):
    fields = {
        "name": request.POST.get("name", ""),
        "phone": request.POST.get("phone", ""),
        "email": request.POST.get("email", ""),
        "pan": request.POST.get("pan", ""),
        "source": request.POST.get("source", ""),
        "notes": request.POST.get("notes", ""),
        "expected_aum": request.POST.get("expected_aum", 0) or 0,
    }
    if crm.update_lead(lead_id, fields) is None:
        raise Http404("Lead not found")
    messages.success(request, "Lead updated.")
    return redirect(reverse("clients:crm_detail", args=[lead_id]))


@require_POST
def crm_set_stage(request, lead_id: str):
    stage = request.POST.get("stage", "")
    if crm.set_stage(lead_id, stage) is None:
        messages.error(request, "Invalid stage.")
    return redirect(request.POST.get("next") or reverse("clients:crm"))


@require_POST
def crm_toggle_task(request, lead_id: str):
    item_key = request.POST.get("key", "")
    done = request.POST.get("done") == "1"
    crm.toggle_journey_item(lead_id, item_key, done)
    return redirect(reverse("clients:crm_detail", args=[lead_id]))


@require_POST
def crm_add_note(request, lead_id: str):
    crm.add_note(lead_id, request.POST.get("text", ""))
    return redirect(reverse("clients:crm_detail", args=[lead_id]))


@require_POST
def crm_delete(request, lead_id: str):
    if crm.delete_lead(lead_id):
        messages.success(request, "Lead removed.")
    return redirect(reverse("clients:crm"))


def crm_risk(request, lead_id: str):
    """Risk profile assessment — GET shows the form (or saved result),
    POST scores the answers and saves the categorised profile."""
    lead = crm.get_lead(lead_id)
    if not lead:
        raise Http404("Lead not found")

    if request.method == "POST":
        action = request.POST.get("action", "submit")
        if action == "reset":
            crm.reset_risk_profile(lead_id)
            messages.info(request, "Risk profile cleared — retake the assessment.")
            return redirect(reverse("clients:crm_risk", args=[lead_id]))

        # Submit: gather all answers
        answers: dict[str, int] = {}
        missing: list[str] = []
        for q in crm.RISK_QUESTIONS:
            raw = request.POST.get(q["key"], "")
            try:
                answers[q["key"]] = int(raw)
            except (TypeError, ValueError):
                missing.append(q["q"])
        if missing:
            messages.error(request, f"Please answer all {len(crm.RISK_QUESTIONS)} questions.")
            return render(
                request,
                "clients/crm_risk.html",
                {
                    "active": "crm",
                    "lead": lead,
                    "questions": crm.RISK_QUESTIONS,
                    "answers": answers,
                    "result": None,
                },
            )
        crm.set_risk_profile(lead_id, answers)
        messages.success(request, "Risk profile saved.")
        return redirect(reverse("clients:crm_risk", args=[lead_id]))

    # GET — show either the form or the saved result
    return render(
        request,
        "clients/crm_risk.html",
        {
            "active": "crm",
            "lead": lead,
            "questions": crm.RISK_QUESTIONS,
            "answers": {},
            "result": lead.get("risk_profile"),
        },
    )


# ============================================================================
# Yield Optimizer
# ============================================================================

def yield_view(request):
    """Surface low-yield holdings + same-category higher-yield alternatives."""
    Session = get_session_factory()
    with Session() as session:
        backfill_scheme_names(session)
        result = yield_optimizer.compute_yield_analysis(session, lookup_nav)
    return render(
        request,
        "clients/yield_optimizer.html",
        {
            "active": "yield",
            "categories": result["categories"],
            "suggestions": result["suggestions"],
            "stats": result["stats"],
        },
    )


# ============================================================================
# Calendar
# ============================================================================

def calendar_view(request):
    """Month view + today's-at-a-glance panel."""
    today = dt.date.today()
    # Parse view & focus date from query string
    try:
        focus = dt.date.fromisoformat(request.GET.get("date") or today.isoformat())
    except ValueError:
        focus = today

    # First/last day of focus month
    first_of_month = focus.replace(day=1)
    if first_of_month.month == 12:
        next_month = first_of_month.replace(year=first_of_month.year + 1, month=1)
    else:
        next_month = first_of_month.replace(month=first_of_month.month + 1)
    last_of_month = next_month - dt.timedelta(days=1)

    # Pad calendar grid to start on Monday
    pad_before = first_of_month.weekday()  # Mon=0
    grid_start = first_of_month - dt.timedelta(days=pad_before)
    pad_after = (6 - last_of_month.weekday()) % 7
    grid_end = last_of_month + dt.timedelta(days=pad_after)

    events_in_window = calendar_store.events_in_range(grid_start, grid_end)
    events_by_date: dict[str, list] = {}
    for ev in events_in_window:
        date_key = ev["starts_at"][:10]
        events_by_date.setdefault(date_key, []).append(ev)

    # Build the grid
    weeks = []
    cur = grid_start
    while cur <= grid_end:
        row = []
        for _ in range(7):
            row.append({
                "date": cur,
                "iso": cur.isoformat(),
                "in_month": cur.month == focus.month,
                "is_today": cur == today,
                "events": events_by_date.get(cur.isoformat(), []),
            })
            cur += dt.timedelta(days=1)
        weeks.append(row)

    todays_stats = calendar_store.stats_for_today()

    # Prev/next month nav
    if focus.month == 1:
        prev_focus = focus.replace(year=focus.year - 1, month=12, day=1)
    else:
        prev_focus = focus.replace(month=focus.month - 1, day=1)
    next_focus = next_month

    # Client list for the "with whom" picker
    Session = get_session_factory()
    with Session() as session:
        accounts = session.execute(select(Account).order_by(Account.name)).scalars().all()
        client_options = [{"name": a.name, "pan": a.pan or ""} for a in accounts]

    return render(
        request,
        "clients/calendar.html",
        {
            "active": "calendar",
            "today": today,
            "focus": focus,
            "weeks": weeks,
            "month_label": focus.strftime("%B %Y"),
            "prev_focus": prev_focus,
            "next_focus": next_focus,
            "todays_stats": todays_stats,
            "kinds": calendar_store.EVENT_KINDS,
            "client_options": client_options,
            "ics_url": reverse("clients:calendar_ics"),
        },
    )


@require_POST
def calendar_create(request):
    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, "Meeting title is required.")
        return redirect(reverse("clients:calendar"))
    calendar_store.create_event(
        title=title,
        starts_at=request.POST.get("starts_at", ""),
        ends_at=request.POST.get("ends_at", ""),
        with_who=request.POST.get("with_who", ""),
        pan=request.POST.get("pan", ""),
        kind=request.POST.get("kind", "other"),
        location=request.POST.get("location", ""),
        notes=request.POST.get("notes", ""),
    )
    messages.success(request, f"Scheduled: {title}")
    next_url = request.POST.get("next") or reverse("clients:calendar")
    return redirect(next_url)


@require_POST
def calendar_update(request, event_id: str):
    fields = {k: request.POST.get(k, "") for k in
              ("title", "with_who", "pan", "kind", "starts_at", "ends_at",
               "location", "notes", "status")}
    if calendar_store.update_event(event_id, fields) is None:
        raise Http404("Event not found")
    messages.success(request, "Meeting updated.")
    return redirect(reverse("clients:calendar"))


@require_POST
def calendar_delete(request, event_id: str):
    if calendar_store.delete_event(event_id):
        messages.success(request, "Meeting removed.")
    return redirect(reverse("clients:calendar"))


def calendar_ics(request):
    """ICS feed — Google Calendar / Outlook / Teams subscribable URL."""
    body = calendar_store.render_ics()
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = "inline; filename=advisor_calendar.ics"
    return response


# ============================================================================
# Research
# ============================================================================

def research_home(request):
    """Research hub — book-wide insights, market news, and fund search.

    Per-client insights are NOT computed here — they live on each client's
    Insights tab (clients/<pan>/?tab=insights) and are generated on demand.
    """
    category = request.GET.get("category", "all")
    news_items = research.get_market_news(category)

    Session = get_session_factory()
    with Session() as session:
        backfill_scheme_names(session)
        # Book-wide stays server-rendered each load (you asked to keep it visible)
        book_wide = research.compute_book_wide_insights(session, lookup_nav)
        schemes = research.list_searchable_schemes(session)

    q = (request.GET.get("q") or "").strip().lower()
    if q:
        schemes = [
            s for s in schemes
            if q in s["name"].lower() or q in s["scheme_code"].lower()
            or q in (s["isin"] or "").lower()
        ]
    # Group by AMC (no upper cap — every scheme is reachable via collapsible sections)
    schemes_grouped = research.group_schemes_by_amc(schemes)

    # Sub-tab inside the Research page (book-wide / news / funds).
    sub = (request.GET.get("sub") or "insights").lower()
    if sub not in ("insights", "news", "funds"):
        sub = "insights"

    return render(
        request,
        "clients/research.html",
        {
            "active": "research",
            "sub": sub,
            "news_items": news_items,
            "news_categories": research.NEWS_CATEGORIES,
            "selected_category": category,
            "book_wide": book_wide,
            "schemes_grouped": schemes_grouped,
            "scheme_total": len(schemes),
            "amc_total": len(schemes_grouped),
            "search_q": q,
            "now": dt.datetime.now(),
        },
    )


def research_fund_search(request):
    """JSON autocomplete endpoint for the fund-search box.

    Returns up to 12 best matches across name / scheme_code / ISIN.
    """
    from django.http import JsonResponse
    q = (request.GET.get("q") or "").strip().lower()
    if len(q) < 2:
        return JsonResponse({"results": []})

    Session = get_session_factory()
    with Session() as session:
        all_schemes = research.list_searchable_schemes(session)

    matches: list[tuple[int, dict]] = []  # (rank, scheme)
    for s in all_schemes:
        nm = s["name"].lower()
        sc = s["scheme_code"].lower()
        isin = (s["isin"] or "").lower()
        if not (q in nm or q in sc or q in isin):
            continue
        # Rank: prefix-match on name beats substring; scheme-code match next; ISIN last
        if nm.startswith(q):
            rank = 0
        elif sc == q or sc.startswith(q):
            rank = 1
        elif q in nm:
            rank = 2
        elif isin and q in isin:
            rank = 3
        else:
            rank = 4
        matches.append((rank, s))

    matches.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    out = [
        {
            "scheme_code": m[1]["scheme_code"],
            "name": m[1]["name"],
            "amc_name": m[1]["amc_name"],
            "asset_class": m[1]["asset_class"],
            "isin": m[1]["isin"],
        }
        for m in matches[:12]
    ]
    return JsonResponse({"results": out, "count": len(matches)})


@require_POST
def research_generate_client(request, pan: str):
    """On-demand insight generation for a single client. Caches the result."""
    Session = get_session_factory()
    with Session() as session:
        # Find by PAN (uppercase) — accepts case-insensitive match
        account = session.execute(
            select(Account).where(Account.pan == pan.upper())
        ).scalars().first()
        if account is None:
            messages.error(request, f"No client with PAN {pan}.")
            return redirect(reverse("clients:research"))
        payload = research.compute_client_insights(session, account, lookup_nav)
    insights_cache.store(account.pan or "", payload)
    n = payload["insights"]
    if n:
        messages.success(
            request,
            f"Generated {len(n)} insight{'s' if len(n) != 1 else ''} for {account.name}.",
        )
    else:
        messages.success(request, f"{account.name}: no flags — portfolio passes all rules.")
    # Defensive: only honour a `next` value that begins with "/" — anything
    # else (full URLs, Windows paths, etc.) is rejected in favour of the
    # client detail page (insights tab).
    raw_next = request.POST.get("next") or ""
    if raw_next.startswith("/") and ":" not in raw_next.split("?", 1)[0]:
        next_url = raw_next
    else:
        next_url = reverse("clients:detail", args=[account.pan]) + "?tab=insights"
    return redirect(next_url)


@require_POST
def research_clear_cache(request):
    """Wipe all cached client insights — useful after rule changes."""
    insights_cache.clear_all()
    messages.success(request, "Insights cache cleared. Generate per-client to recompute.")
    return redirect(request.POST.get("next") or reverse("clients:research"))


def research_rules_view(request):
    """Editable rule thresholds for the AI Insights engine."""
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "reset":
            research_rules.reset_to_defaults()
            messages.success(request, "Rules reset to defaults.")
            return redirect(reverse("clients:research_rules"))

        # Save: pull only known keys from POST
        overrides: dict[str, float] = {}
        for r in research_rules.RULE_DEFS:
            raw = request.POST.get(r["key"], "")
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            v = max(float(r["min"]), min(float(r["max"]), v))
            # Only persist if it differs from the default
            if v != float(r["default"]):
                overrides[r["key"]] = v
        research_rules.save_overrides(overrides)
        messages.success(
            request,
            f"Saved {len(overrides)} custom rule(s); the rest use defaults.",
        )
        return redirect(reverse("clients:research_rules"))

    return render(
        request,
        "clients/research_rules.html",
        {
            "active": "research_rules",
            "groups": research_rules.grouped_for_editor(),
            "override_count": len(research_rules.load_overrides()),
        },
    )


def research_fund(request, scheme_code: str):
    """Single-fund deep-dive — NAV trend chart + stats.

    Data flow:
      1. Look up the scheme in our DB to get name + ISIN
      2. Resolve the AMFI 6-digit scheme code via the AMFI NAV map (we already
         parse this daily for current NAVs — we just expose the code too)
      3. Use the AMFI code to fetch full daily NAV history from mfapi.in
         (cached on disk for 12h)
      4. If no AMFI code can be resolved, OR the mfapi.in call fails, render
         a 'data unavailable' panel — we never synthesise fake data.
    """
    from .amfi_nav import lookup_amfi_record

    Session = get_session_factory()
    with Session() as session:
        scheme = session.execute(
            select(Scheme).where(Scheme.scheme_code == scheme_code)
        ).scalars().first()
        if scheme is None:
            raise Http404(f"Scheme {scheme_code} not found")
        meta = dict(scheme.meta or {})

    # Step 1+2: current NAV + AMFI code from the daily NAVAll.txt parse
    current_nav, nav_date, _matched = lookup_nav(
        scheme_code=scheme.scheme_code,
        scheme_name=scheme.name,
        isin=scheme.isin,
    )
    amfi_record = lookup_amfi_record(
        scheme_code=scheme.scheme_code,
        scheme_name=scheme.name,
        isin=scheme.isin,
    )
    # Prefer the AMFI map's code; fall back to anything ingest stored on meta
    amfi_code = (amfi_record or {}).get("amfi_code") or meta.get("amfi_code", "")

    # Step 3: fetch full history (returns available=False with error msg if missing)
    history = nav_history.fetch_history(amfi_code=amfi_code)

    timeframe = request.GET.get("tf", "1Y").upper()
    if timeframe not in ("1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"):
        timeframe = "1Y"
    series = nav_history.slice_timeframe(history["series"], timeframe) if history["available"] else []

    # Step 4: pick a Nifty benchmark proxy and fetch it from mfapi.in (cached)
    benchmark = benchmarks.pick_benchmark(scheme.name, meta)
    benchmark_history = None
    benchmark_series_raw: list[dict] = []
    if benchmark:
        benchmark_history = nav_history.fetch_history(amfi_code=benchmark["amfi_code"])
        if benchmark_history["available"]:
            benchmark_series_raw = nav_history.slice_timeframe(
                benchmark_history["series"], timeframe
            )

    # Align both series to their common date range so the lines line up
    fund_aligned, bench_aligned = benchmarks.align_series(series, benchmark_series_raw)

    # Re-base both to 100 so they overlay nicely on a single Y-axis
    fund_normalized = benchmarks.normalize_to_base(fund_aligned, base=100.0)
    bench_normalized = benchmarks.normalize_to_base(bench_aligned, base=100.0)

    metrics = nav_history.summary_metrics(fund_aligned) if fund_aligned else {
        "return_pct": None, "high": None, "low": None, "volatility": None,
    }
    bench_metrics = nav_history.summary_metrics(bench_aligned) if bench_aligned else {
        "return_pct": None, "high": None, "low": None, "volatility": None,
    }
    # Alpha = fund return − benchmark return (over the same window)
    alpha_pct = None
    if metrics["return_pct"] is not None and bench_metrics["return_pct"] is not None:
        alpha_pct = round(metrics["return_pct"] - bench_metrics["return_pct"], 2)

    chart_data = {
        "points": fund_aligned,
        "navs": [r["nav"] for r in fund_normalized],
        "dates": [r["date"] for r in fund_normalized],
        "actual_navs": [r["nav"] for r in fund_aligned],  # raw values for tooltip
        "bench_navs": [r["nav"] for r in bench_normalized],
        "bench_actual_navs": [r["nav"] for r in bench_aligned],
        "count": len(fund_normalized),
        "has_benchmark": bool(bench_normalized),
    }

    return render(
        request,
        "clients/research_fund.html",
        {
            "active": "research",
            "scheme": scheme,
            "meta": meta,
            "current_nav": current_nav,
            "nav_date": nav_date,
            "amfi_code": amfi_code,
            "amfi_record": amfi_record,
            "history": history,
            "chart": chart_data,
            "metrics": metrics,
            "bench_metrics": bench_metrics,
            "alpha_pct": alpha_pct,
            "benchmark": benchmark,
            "benchmark_history": benchmark_history,
            "timeframe": timeframe,
            "timeframes": ["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"],
        },
    )
