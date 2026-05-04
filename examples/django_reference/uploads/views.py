"""Feed Files (uploads) views — list, new, detail, process, download, export."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from django.contrib import messages
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from sqlalchemy import and_, func, or_, select

from openreversefeed.db.models import (
    Account,
    Folio,
    IngestionRun,
    Scheme,
    SourceFile,
    Transaction,
)
from reference_app.ofr_bridge import (
    get_session_factory,
    process_source_file,
    save_uploaded_file,
)

from .forms import UploadForm

_STATUS_CHOICES = ("all", "pending", "processing", "completed", "failed")
_PROVIDER_CHOICES = ("all", "cams", "kfintech")
_DATE_CHOICES = ("all", "today", "7d", "30d")
_SORT_CHOICES = (
    "date_desc",
    "date_asc",
    "rows_desc",
    "rows_asc",
    "status",
)
_PAGE_SIZE_CHOICES = (10, 20, 50, 100)


def _safe_int(val, default: int) -> int:
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return default


def file_list(request):
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or "all"
    provider = request.GET.get("provider") or "all"
    date_range = request.GET.get("date") or "all"
    sort = request.GET.get("sort") or "date_desc"
    page = _safe_int(request.GET.get("page"), 1)
    page_size = _safe_int(request.GET.get("size"), 20)

    if status not in _STATUS_CHOICES:
        status = "all"
    if provider not in _PROVIDER_CHOICES:
        provider = "all"
    if date_range not in _DATE_CHOICES:
        date_range = "all"
    if sort not in _SORT_CHOICES:
        sort = "date_desc"
    if page_size not in _PAGE_SIZE_CHOICES:
        page_size = 20

    Session = get_session_factory()
    with Session() as session:
        base_query = select(SourceFile)

        filters = []
        if status != "all":
            filters.append(SourceFile.status == status)
        if provider != "all":
            filters.append(SourceFile.registrar == provider)
        if q:
            filters.append(
                or_(
                    SourceFile.filename.ilike(f"%{q}%"),
                    SourceFile.uploaded_by.ilike(f"%{q}%"),
                )
            )
        if date_range == "today":
            filters.append(SourceFile.created_at >= datetime.utcnow() - timedelta(days=1))
        elif date_range == "7d":
            filters.append(SourceFile.created_at >= datetime.utcnow() - timedelta(days=7))
        elif date_range == "30d":
            filters.append(SourceFile.created_at >= datetime.utcnow() - timedelta(days=30))

        if filters:
            base_query = base_query.where(and_(*filters))

        # Sort
        if sort == "date_desc":
            base_query = base_query.order_by(SourceFile.id.desc())
        elif sort == "date_asc":
            base_query = base_query.order_by(SourceFile.id.asc())
        elif sort == "rows_desc":
            base_query = base_query.order_by(
                SourceFile.row_count.desc().nullslast(), SourceFile.id.desc()
            )
        elif sort == "rows_asc":
            base_query = base_query.order_by(
                SourceFile.row_count.asc().nullsfirst(), SourceFile.id.desc()
            )
        elif sort == "status":
            base_query = base_query.order_by(SourceFile.status, SourceFile.id.desc())

        # Total matching this filter set (before pagination)
        filtered_total = session.execute(
            select(func.count()).select_from(base_query.subquery())
        ).scalar()

        # Paginate
        offset = (page - 1) * page_size
        files = (
            session.execute(base_query.offset(offset).limit(page_size)).scalars().all()
        )

        counts = dict(
            session.execute(
                select(Transaction.source_file_id, func.count()).group_by(
                    Transaction.source_file_id
                )
            ).all()
        )

        # Global total (no filters) + per-status breakdown for the summary bar
        total_files = session.execute(select(func.count()).select_from(SourceFile)).scalar()
        status_summary_rows = session.execute(
            select(SourceFile.status, func.count()).group_by(SourceFile.status)
        ).all()
        status_summary = {row[0]: row[1] for row in status_summary_rows}

    # Attach extra fields per row
    rows = []
    for f in files:
        rows.append(
            {
                "id": f.id,
                "filename": f.filename,
                "registrar": f.registrar,
                "status": f.status,
                "created_at": f.created_at,
                "row_count": f.row_count,
                "txn_count": counts.get(f.id, 0),
                "uploaded_by": f.uploaded_by,
                "error": f.error,
            }
        )

    total_pages = max(1, (filtered_total + page_size - 1) // page_size)
    has_prev = page > 1
    has_next = page < total_pages
    prev_page = page - 1 if has_prev else 1
    next_page = page + 1 if has_next else total_pages

    page_window_start = max(1, page - 2)
    page_window_end = min(total_pages, page + 2)
    page_window = list(range(page_window_start, page_window_end + 1))

    return render(
        request,
        "uploads/list.html",
        {
            "rows": rows,
            "q": q,
            "status": status,
            "provider": provider,
            "date_range": date_range,
            "sort": sort,
            "page": page,
            "page_size": page_size,
            "status_choices": _STATUS_CHOICES,
            "provider_choices": _PROVIDER_CHOICES,
            "date_choices": _DATE_CHOICES,
            "sort_choices": _SORT_CHOICES,
            "page_size_choices": _PAGE_SIZE_CHOICES,
            "total_files": total_files,
            "filtered_total": filtered_total,
            "shown_count": len(rows),
            "status_summary": status_summary,
            "total_pages": total_pages,
            "has_prev": has_prev,
            "has_next": has_next,
            "prev_page": prev_page,
            "next_page": next_page,
            "page_window": page_window,
            "active": "files",
        },
    )


def upload_view(request):
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            result = save_uploaded_file(
                uploaded_file=form.cleaned_data["file"],
                registrar=form.cleaned_data["registrar"],
                uploaded_by=form.cleaned_data["uploaded_by"],
            )
            if result["duplicate"]:
                messages.warning(request, f"Duplicate file — {result['message']}")
            else:
                messages.success(request, result["message"])
            return redirect(reverse("uploads:detail", args=[result["source_file_id"]]))
    else:
        form = UploadForm()
    return render(request, "uploads/upload.html", {"form": form, "active": "ingest"})


def file_detail(request, source_file_id: int):
    Session = get_session_factory()
    with Session() as session:
        sf = session.get(SourceFile, source_file_id)
        if sf is None:
            raise Http404
        runs = (
            session.execute(
                select(IngestionRun)
                .where(IngestionRun.source_file_id == source_file_id)
                .order_by(IngestionRun.id.desc())
            )
            .scalars()
            .all()
        )
        # Count total transactions from this file (so the UI can tell the user
        # whether they're seeing all of them).
        total_txns = session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.source_file_id == source_file_id)
        ).scalar_one()

        # Sort direction controlled by ?sort=asc|desc (default: newest first so
        # the most recent activity is visible at a glance).
        sort_dir = (request.GET.get("sort") or "desc").lower()
        order_cols = (
            (Transaction.transaction_date.asc(), Transaction.id.asc())
            if sort_dir == "asc"
            else (Transaction.transaction_date.desc(), Transaction.id.desc())
        )

        # Pagination: large page size by default so the whole file is visible.
        page_size = _safe_int(request.GET.get("size"), 1000)
        page_size = min(page_size, 5000)  # cap to avoid runaway renders
        page = _safe_int(request.GET.get("page"), 1)
        offset = (page - 1) * page_size

        txn_rows = (
            session.execute(
                select(
                    Transaction,
                    Account.name.label("account_name"),
                    Account.pan.label("account_pan"),
                    Scheme.name.label("scheme_name"),
                    Scheme.scheme_code.label("scheme_code"),
                    Folio.folio_number.label("folio_number"),
                )
                .join(Account, Account.id == Transaction.account_id)
                .join(Scheme, Scheme.id == Transaction.scheme_id)
                .join(Folio, Folio.id == Transaction.folio_id)
                .where(Transaction.source_file_id == source_file_id)
                .order_by(*order_cols)
                .offset(offset)
                .limit(page_size)
            )
            .all()
        )

    txns = []
    for r in txn_rows:
        pan = r.account_pan or "XXXXX"
        bucket = (sum(ord(c) for c in pan[-4:]) % 6) + 1
        initials = pan[:2].upper() if pan else "??"
        txns.append(
            {
                "t": r.Transaction,
                "account_name": r.account_name,
                "account_pan": r.account_pan,
                "scheme_code": r.scheme_code,
                "scheme_name": r.scheme_name,
                "folio_number": r.folio_number,
                "avatar_class": f"av-{bucket}",
                "initials": initials,
            }
        )

    total_pages = max(1, (total_txns + page_size - 1) // page_size)
    return render(
        request,
        "uploads/detail.html",
        {
            "sf": sf,
            "runs": runs,
            "txns": txns,
            "active": "files",
            "total_txns": total_txns,
            "shown_txns": len(txns),
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "sort_dir": sort_dir,
        },
    )


def process_view(request, source_file_id: int):
    if request.method != "POST":
        return redirect(reverse("uploads:detail", args=[source_file_id]))

    # Allow re-processing of completed/failed files by resetting status first
    Session = get_session_factory()
    with Session() as session:
        sf = session.get(SourceFile, source_file_id)
        if sf is None:
            raise Http404
        if sf.status in ("completed", "failed"):
            sf.status = "pending"
            sf.error = None
            session.commit()

    result = process_source_file(source_file_id)
    if "error" in result:
        messages.error(request, f"Processing failed: {result['error']}")
    else:
        stats = result.get("stats", {})
        messages.success(
            request,
            f"Processed — new={stats.get('new_txns', 0)} "
            f"duplicates={stats.get('duplicate', 0)} skipped={stats.get('skipped', 0)}",
        )
    # Redirect back to wherever they came from if possible
    next_url = request.POST.get("next") or reverse("uploads:detail", args=[source_file_id])
    return redirect(next_url)


def download_view(request, source_file_id: int):
    """Stream the original uploaded file back to the operator for debugging."""
    Session = get_session_factory()
    with Session() as session:
        sf = session.get(SourceFile, source_file_id)
        if sf is None:
            raise Http404
        storage_uri = sf.storage_uri
        original_name = sf.filename

    if not storage_uri or not storage_uri.startswith("file://"):
        messages.error(request, "Download only supported for local-storage files.")
        return redirect(reverse("uploads:detail", args=[source_file_id]))

    path = Path(storage_uri.removeprefix("file://"))
    if not path.exists():
        messages.error(request, f"File no longer exists on disk: {path}")
        return redirect(reverse("uploads:detail", args=[source_file_id]))

    response = FileResponse(path.open("rb"), as_attachment=True, filename=original_name)
    return response


class _Echo:
    """CSV writer sink that returns each row so StreamingHttpResponse can flush it."""

    def write(self, value):
        return value


def export_transactions_csv(request, source_file_id: int):
    """Stream a CSV of all transactions for this source file."""
    Session = get_session_factory()
    with Session() as session:
        sf = session.get(SourceFile, source_file_id)
        if sf is None:
            raise Http404
        filename = sf.filename
        rows = (
            session.execute(
                select(
                    Transaction.id,
                    Transaction.transaction_date,
                    Transaction.registrar,
                    Transaction.composite_key,
                    Transaction.registrar_transaction_id,
                    Account.pan.label("pan"),
                    Account.name.label("account_name"),
                    Scheme.scheme_code.label("scheme_code"),
                    Folio.folio_number.label("folio_number"),
                    Transaction.action,
                    Transaction.action_tag,
                    Transaction.status,
                    Transaction.units,
                    Transaction.nav,
                    Transaction.amount,
                    Transaction.broker_code,
                )
                .join(Account, Account.id == Transaction.account_id)
                .join(Scheme, Scheme.id == Transaction.scheme_id)
                .join(Folio, Folio.id == Transaction.folio_id)
                .where(Transaction.source_file_id == source_file_id)
                .order_by(Transaction.transaction_date, Transaction.id)
            )
            .all()
        )

    header = [
        "id",
        "transaction_date",
        "registrar",
        "composite_key",
        "registrar_transaction_id",
        "pan",
        "account_name",
        "scheme_code",
        "folio_number",
        "action",
        "action_tag",
        "status",
        "units",
        "nav",
        "amount",
        "broker_code",
    ]

    writer = csv.writer(_Echo())

    def _stream():
        yield writer.writerow(header)
        for r in rows:
            yield writer.writerow(
                [
                    r.id,
                    r.transaction_date.isoformat() if r.transaction_date else "",
                    r.registrar,
                    r.composite_key,
                    r.registrar_transaction_id,
                    r.pan,
                    r.account_name,
                    r.scheme_code,
                    r.folio_number,
                    r.action,
                    r.action_tag,
                    r.status,
                    str(r.units) if r.units is not None else "",
                    str(r.nav) if r.nav is not None else "",
                    str(r.amount) if r.amount is not None else "",
                    r.broker_code or "",
                ]
            )

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    response = StreamingHttpResponse(_stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{stem}_transactions.csv"'
    return response
