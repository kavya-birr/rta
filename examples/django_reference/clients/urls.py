from django.urls import path

from . import views

app_name = "clients"

urlpatterns = [
    path("", views.client_list, name="list"),
    path("arn/", views.arn_dashboard, name="arn"),
    # ── CRM (must come before the <pan>/ catch-all) ──
    path("crm/",                          views.crm_board,        name="crm"),
    path("crm/new/",                      views.crm_create,       name="crm_create"),
    path("crm/<str:lead_id>/",            views.crm_detail,       name="crm_detail"),
    path("crm/<str:lead_id>/update/",     views.crm_update,       name="crm_update"),
    path("crm/<str:lead_id>/stage/",      views.crm_set_stage,    name="crm_set_stage"),
    path("crm/<str:lead_id>/task/",       views.crm_toggle_task,  name="crm_toggle_task"),
    path("crm/<str:lead_id>/note/",       views.crm_add_note,     name="crm_add_note"),
    path("crm/<str:lead_id>/risk/",       views.crm_risk,         name="crm_risk"),
    path("crm/<str:lead_id>/delete/",     views.crm_delete,       name="crm_delete"),
    # ── Yield Optimizer ──
    path("yield/",                        views.yield_view,       name="yield"),
    # ── Calendar ──
    path("calendar/",                     views.calendar_view,    name="calendar"),
    path("calendar/new/",                 views.calendar_create,  name="calendar_create"),
    path("calendar/<str:event_id>/update/", views.calendar_update, name="calendar_update"),
    path("calendar/<str:event_id>/delete/", views.calendar_delete, name="calendar_delete"),
    path("calendar/feed.ics",             views.calendar_ics,     name="calendar_ics"),
    # ── Research ──
    path("research/",                     views.research_home,    name="research"),
    path("research/fund-search/",         views.research_fund_search, name="research_fund_search"),
    path("research/rules/",               views.research_rules_view, name="research_rules"),
    path("research/generate/<str:pan>/",  views.research_generate_client, name="research_generate"),
    path("research/clear-cache/",         views.research_clear_cache, name="research_clear_cache"),
    path("research/fund/<str:scheme_code>/", views.research_fund, name="research_fund"),
    # ── Client portfolio drilldown ──
    path("<str:pan>/", views.client_detail, name="detail"),
    path("<str:pan>/transactions/", views.all_transactions, name="transactions"),
    path("<str:pan>/scheme/<str:scheme_code>/", views.holding_detail, name="holding"),
]
