import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import bminfo.web as web


def _request(cookies=None):
    return SimpleNamespace(
        cookies=cookies or {},
        query_params=SimpleNamespace(get=lambda name, default=None: default),
        headers=SimpleNamespace(get=lambda name, default="": default),
        state=SimpleNamespace(),
    )


def test_callsign_search_requires_authentication():
    response = web._dashboard_access_error(_request(), callsign="EA7KLK")

    assert response is not None
    assert response.status_code == 401


def test_status_reports_database_collector_and_usage_metrics(monkeypatch):
    class StatusStore:
        def status_snapshot(self, stale_after_seconds):
            assert stale_after_seconds == max(web.settings.collector_heartbeat_seconds * 3, 90)
            return {
                "status": "ok",
                "database": {"status": "healthy", "connection": "ok"},
                "collector": {"status": "healthy", "age_seconds": 4},
                "tables": {"raw_events": 123, "qsos": 45},
                "active_users": 2,
            }

    monkeypatch.setattr(web, "get_store", lambda: StatusStore())

    response = web.status()

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["database"]["connection"] == "ok"
    assert payload["collector"]["status"] == "healthy"
    assert payload["tables"] == {"raw_events": 123, "qsos": 45}
    assert payload["active_users"] == 2


def test_nightly_raw_cleanup_is_scheduled_at_two_local_time():
    before = datetime(2026, 8, 4, 0, 30, tzinfo=ZoneInfo("Europe/Madrid")).astimezone(timezone.utc)
    after = datetime(2026, 8, 4, 3, 30, tzinfo=ZoneInfo("Europe/Madrid")).astimezone(timezone.utc)

    before_target = web._next_nightly_raw_events_cleanup(before).astimezone(ZoneInfo("Europe/Madrid"))
    after_target = web._next_nightly_raw_events_cleanup(after).astimezone(ZoneInfo("Europe/Madrid"))

    assert before_target == datetime(2026, 8, 4, 2, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    assert after_target == datetime(2026, 8, 5, 2, 0, tzinfo=ZoneInfo("Europe/Madrid"))


def test_status_returns_degraded_response_when_collector_is_stale(monkeypatch):
    class StaleStore:
        def status_snapshot(self, stale_after_seconds):
            return {
                "status": "degraded",
                "database": {"status": "healthy", "connection": "ok"},
                "collector": {"status": "unhealthy", "age_seconds": 120},
                "tables": {"raw_events": 123, "qsos": 45},
                "active_users": 0,
            }

    monkeypatch.setattr(web, "get_store", lambda: StaleStore())

    response = web.status()

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "degraded"


def test_status_returns_database_failure(monkeypatch):
    class BrokenStore:
        def status_snapshot(self, stale_after_seconds):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(web, "get_store", lambda: BrokenStore())

    response = web.status()

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "unhealthy"
    assert payload["database"] == {"status": "unhealthy", "connection": "failed"}
    assert payload["tables"] == {"raw_events": None, "qsos": None}


def test_extended_ranges_require_authentication():
    for time_range in ("2w", "1M", "2M", "3M"):
        response = web._dashboard_access_error(_request(), time_range=time_range)

        assert response is not None
        assert response.status_code == 401


def test_public_ranges_without_search_remain_available(monkeypatch):
    request = _request()
    monkeypatch.setattr(web, "_current_user", lambda request: None)

    assert web._dashboard_access_error(request, time_range="1w") is None
    assert web._dashboard_access_error(request, callsign="  ") is None


def test_authenticated_user_can_use_gated_features(monkeypatch):
    request = _request({"session_token": "session-token"})
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "EA7KLK"})

    assert web._dashboard_access_error(request, callsign="EA7KLK") is None
    assert web._dashboard_access_error(request, time_range="1M") is None


def test_dashboard_refreshes_session_and_shows_callsign(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "EA7KLK"})

    page = web.dashboard(_request()).body.decode("utf-8")

    assert 'data-authenticated="true"' in page
    assert 'href="/user/profile" data-user-callsign>EA7KLK</a>' in page
    assert 'action="/user/logout"' in page
    assert 'data-i18n="home.myProfile">My profile</a>' not in page
    assert 'href="/user/login"' not in page
    assert 'href="/user/register"' not in page
    assert 'id="callsign"' in page
    assert 'id="pageMetrics"' in page


def test_anonymous_dashboard_disables_gated_controls():
    page = web.dashboard().body.decode("utf-8")

    assert 'data-authenticated="false"' in page
    assert 'value="2w" data-auth-required' not in page
    assert 'value="1M" data-auth-required' not in page
    assert 'id="callsign"' not in page
    assert 'id="talkgroups"' not in page


def test_account_pages_include_translated_query_metrics_footer():
    page = web._account_page_with_metrics(
        "Test page",
        "<section>Content</section>",
        "es",
        records_retrieved=42,
        query_seconds=0.1254,
    ).body.decode("utf-8")

    assert "Registros recuperados: 42, la consulta tardó 0.125 segundos" in page
    assert '<footer class="page-footer">' in page


def test_subpage_uses_dashboard_hero_header():
    page = web._account_page("Test page", "<section>Content</section>").body.decode("utf-8")

    assert '<header class="panel hero">' in page
    assert "🔊 BrandMeister Lastheard" in page
    assert "Live DMR activity, talkgroup statistics and talk time" in page
    assert 'class="live"><i></i> live feed' in page


def test_authenticated_subpage_header_uses_callsign_navigation():
    page = web._account_page(
        "Test page",
        "<section>Content</section>",
        user={"callsign": "EA7KLK"},
    ).body.decode("utf-8")

    assert 'href="/user/profile">EA7KLK</a>' in page
    assert 'href="/user/live-qsos">Live QSOs</a>' in page
    assert 'action="/user/logout"' in page
    assert 'href="/user/login">Log in</a>' not in page


def test_authenticated_dashboard_includes_gated_controls(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "EA7KLK"})

    page = web.dashboard(_request()).body.decode("utf-8")

    assert 'value="2w" data-auth-required' in page
    assert 'value="1M" data-auth-required' in page
    assert 'value="2M" data-auth-required' in page
    assert 'value="3M" data-auth-required' in page
    assert 'id="talkgroups"' in page


def test_active_talkgroups_endpoint_requires_authentication(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: None)

    response = web.active_user_talkgroups(_request())

    assert response.status_code == 401


def test_active_talkgroups_endpoint_returns_traffic_labels(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "EA7KLK"})
    monkeypatch.setattr(
        web,
        "get_store",
        lambda: type(
            "TalkgroupStore",
            (),
            {
                "active_talkgroups": lambda self, start, continent, country: [
                    {"value": 214, "label": "Spain", "count": 3, "total_duration_ms": 9000}
                ]
            },
        )(),
    )

    response = web.active_user_talkgroups(_request(), continent="Europe", country="ES")

    assert response == [{"value": 214, "label": "Spain", "count": 3, "totalDuration": 9.0}]


def test_live_qsos_page_and_data_require_authentication(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: None)

    assert web.user_live_qsos(_request()).status_code == 303
    assert web.live_qsos_data(_request()).status_code == 401


def test_authenticated_live_qsos_preserves_main_filters(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "OWNER1"})
    calls = {}

    class LiveStore:
        def continents(self):
            return ["Europe"]

        def countries(self, continent=None):
            return [{"value": "ES", "label": "Spain"}]

        def active_talkgroups(self, start, continent, country):
            calls["active"] = (continent, country)
            return [{"value": 214, "label": "Spain", "count": 3}]

        def list_qsos(self, *args):
            calls["list"] = args
            return []

    monkeypatch.setattr(web, "get_store", lambda: LiveStore())

    page = web.user_live_qsos(
        _request(),
        timeRange="2w",
        continent="Europe",
        country="ES",
        talkgroup=[214, 91],
        callsign="EA7KLK",
        rows=15,
    ).body.decode("utf-8")

    assert calls["active"] == ("Europe", "ES")
    assert calls["list"][0:4] == (15, 0, "EA7KLK", [214, 91])
    assert calls["list"][-1] == web.settings.kerchunk_threshold_seconds
    assert 'name="callsign"' in page
    assert 'name="talkgroup" multiple' in page
    assert 'id="liveTimeRange"' not in page
    assert 'text-align:left' in page
    assert 'value="Europe" selected' in page
    assert 'value="ES" selected' in page
    assert 'value="214" selected' in page
    assert 'Generate report' not in page
    assert 'id="liveAutoRefresh"' not in page
    assert '/user/live-qsos/ws' in page
    assert 'new WebSocket' in page
    assert 'liveForm.submit()' not in page


def test_live_subscription_filters_are_normalized():
    subscription = web._live_subscription(
        {
            "timeRange": "30m",
            "continent": "Europe",
            "country": "ES",
            "callsign": " EA7KLK ",
            "talkgroups": [214, "91", "invalid"],
            "rows": 250,
        }
    )

    assert subscription == {
        "time_range": "30m",
        "continent": "Europe",
        "country": "ES",
        "callsign": "EA7KLK",
        "talkgroups": {214, 91},
        "rows": 100,
    }


def test_live_qso_filter_matches_selected_scope():
    subscription = web._live_subscription(
        {"continent": "Europe", "country": "ES", "callsign": "EA7", "talkgroups": [214]}
    )
    row = {
        "start_at": datetime.now(timezone.utc) - timedelta(seconds=5),
        "continent": "Europe",
        "country": "ES",
        "source_call": "EA7KLK",
        "destination_id": 214,
        "duration_ms": 8000,
    }

    assert web._live_qso_matches(row, subscription) is True
    row["destination_id"] = 91
    assert web._live_qso_matches(row, subscription) is False


def test_dashboard_includes_callsign_activity_charts():
    page = web.dashboard().body.decode("utf-8")

    assert 'id="callsignQsoChart"' in page
    assert 'id="callsignDurationChart"' in page


def test_user_profile_has_logout_control(monkeypatch):
    monkeypatch.setattr(
        web,
        "_current_user",
        lambda request: {
            "id": 7,
            "callsign": "EA7KLK",
            "name": "Test Operator",
            "email": "test@example.com",
        },
    )
    monkeypatch.setattr(
        web,
        "get_store",
        lambda: type(
            "ProfileStore",
            (),
            {
                "user_statistics": lambda self, callsign: {
                    "qso_count": 0,
                    "duration_seconds": 0,
                    "unique_talkgroups": 0,
                    "last_qso_at": None,
                    "top_talkgroups": [],
                }
            },
        )(),
    )

    page = web.user_profile(_request()).body.decode("utf-8")

    assert 'action="/user/logout"' in page


def test_user_profile_has_email_change_form(monkeypatch):
    monkeypatch.setattr(
        web,
        "_current_user",
        lambda request: {
            "id": 7,
            "callsign": "EA7KLK",
            "name": "Test Operator",
            "email": "test@example.com",
        },
    )
    monkeypatch.setattr(
        web,
        "get_store",
        lambda: type(
            "ProfileStore",
            (),
            {
                "user_statistics": lambda self, callsign: {
                    "qso_count": 0,
                    "duration_seconds": 0,
                    "unique_talkgroups": 0,
                    "last_qso_at": None,
                    "top_talkgroups": [],
                }
            },
        )(),
    )

    page = web.user_profile(_request()).body.decode("utf-8")

    assert 'action="/user/change-email"' in page
    assert 'name="new_email"' in page


def test_admin_maintenance_endpoints_require_admin(monkeypatch):
    monkeypatch.setattr(web, "_admin_allowed", lambda request: False)

    assert web.admin_rebuild_qsos(_request()).status_code == 401
    assert web.admin_clear_raw_events(1, _request()).status_code == 401
    assert web.admin_clear_qsos(1, _request()).status_code == 401


def test_admin_can_expire_sessions_for_one_user(monkeypatch):
    monkeypatch.setattr(web, "_admin_allowed", lambda request: True)
    calls = {}

    class SessionStore:
        def expire_user_sessions(self, user_id):
            calls["user_id"] = user_id
            return 3

    monkeypatch.setattr(web, "get_store", lambda: SessionStore())

    response = web.admin_user_expire_sessions(7, _request())

    assert calls["user_id"] == 7
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?notice=sessions&count=3"


def test_admin_user_row_includes_individual_session_expiry_action():
    row = web._admin_user_row(
        {
            "id": 7,
            "callsign": "EA7KLK",
            "name": "Test Operator",
            "email": "test@example.com",
            "is_active": True,
            "qso_count": 0,
            "duration_seconds": 0,
        }
    )

    assert 'action="/admin/users/7/expire-sessions"' in row
    assert "Expire sessions" in row


def test_admin_maintenance_rejects_unsupported_retention_period(monkeypatch):
    monkeypatch.setattr(web, "_admin_allowed", lambda request: True)

    assert web.admin_clear_raw_events(5, _request()).status_code == 400
    assert web.admin_clear_qsos(5, _request()).status_code == 400


def test_admin_maintenance_actions_use_configured_threshold_and_redirect(monkeypatch):
    monkeypatch.setattr(web, "_admin_allowed", lambda request: True)
    calls = {}

    class MaintenanceStore:
        def rebuild_qsos_from_raw_events(self, threshold):
            calls["threshold"] = threshold
            return {"raw_events_scanned": 12, "eligible_qsos": 4, "qsos_rebuilt": 3}

        def clear_irrelevant_raw_events(self, threshold):
            calls["irrelevant_threshold"] = threshold
            return {
                "raw_events_candidates": 5,
                "raw_events_deleted": 4,
                "raw_events_retained": 1,
            }

        def clear_old_raw_events(self, months):
            calls["raw_months"] = months
            return {"months": months, "raw_events_deleted": 8, "qsos_deleted": 2}

        def clear_old_qsos(self, months):
            calls["qso_months"] = months
            return {"months": months, "qsos_deleted": 6}

    monkeypatch.setattr(web, "get_store", lambda: MaintenanceStore())

    rebuild = web.admin_rebuild_qsos(_request())
    irrelevant = web.admin_clear_irrelevant_raw_events(_request())
    raw = web.admin_clear_raw_events(2, _request())
    qsos = web.admin_clear_qsos(3, _request())

    assert calls["threshold"] == web.settings.kerchunk_threshold_seconds
    assert calls["irrelevant_threshold"] == web.settings.kerchunk_threshold_seconds
    assert calls["raw_months"] == 2
    assert calls["qso_months"] == 3
    assert rebuild.headers["location"] == "/admin?notice=rebuild&count=3&raw=12"
    assert raw.headers["location"] == "/admin?notice=raw&months=2&count=8&qsos=2"
    assert qsos.headers["location"] == "/admin?notice=qso&months=3&count=6"


def test_admin_maintenance_json_responses(monkeypatch):
    monkeypatch.setattr(web, "_admin_allowed", lambda request: True)

    class MaintenanceStore:
        def rebuild_qsos_from_raw_events(self, threshold):
            return {"raw_events_scanned": 1, "eligible_qsos": 1, "qsos_rebuilt": 1}

        def clear_old_raw_events(self, months):
            return {"months": months, "raw_events_deleted": 1, "qsos_deleted": 1}

        def clear_old_qsos(self, months):
            return {"months": months, "qsos_deleted": 1}

    monkeypatch.setattr(web, "get_store", lambda: MaintenanceStore())
    request = _request()
    request.headers = SimpleNamespace(
        get=lambda name, default="": "application/json" if name == "accept" else default
    )

    assert web.admin_rebuild_qsos(request).status_code == 200
    assert web.admin_clear_raw_events(1, request).status_code == 200
    assert web.admin_clear_qsos(1, request).status_code == 200


def test_admin_maintenance_localizes_month_units():
    counts = {"raw_events": 2, "dependent_qsos": 1, "qsos": 3}

    english = web._admin_retention_row("en", "raw-events", 1, counts)
    spanish = web._admin_retention_row("es", "raw-events", 2, counts)
    german = web._admin_retention_row("de", "qsos", 6, counts)

    assert "Older than 1 month" in english
    assert "Older than 1 months" not in english
    assert "Más antiguos que 2 meses" in spanish
    assert "Älter als 6 Monate" in german


def _report_fixture():
    return {
        "summary": {
            "qso_count": 2,
            "duration_seconds": 12.5,
            "unique_talkgroups": 1,
            "active_days": 1,
            "first_qso_at": None,
            "last_qso_at": None,
        },
        "daily": [{"day": "2026-07-29", "qso_count": 2, "duration_seconds": 12.5}],
        "histogram": [{
            "bucket": "2026-07-29T00:00:00+00:00",
            "qso_count": 2,
            "duration_seconds": 12.5,
        }],
        "talkgroups": [{
            "talkgroup_id": 214,
            "name": "Worldwide",
            "qso_count": 5,
            "duration_seconds": 12.5,
            "last_seen_at": None,
        }, {
            "talkgroup_id": 91,
            "name": "Europe",
            "qso_count": 2,
            "duration_seconds": 5,
            "last_seen_at": None,
        }],
        "callsigns": [{
            "callsign": "EA7KLK",
            "source_name": "Volker Kerkhoff",
            "countries": "Spain",
            "qso_count": 2,
            "duration_seconds": 12.5,
            "unique_talkgroups": 1,
            "last_seen_at": None,
        }],
        "qsos": [],
    }


def test_reports_are_authentication_only(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: None)

    page = web.user_reports(_request())
    csv_response = web.user_report_csv(_request())

    assert page.status_code == 303
    assert page.headers["location"] == "/user/login"
    assert csv_response.status_code == 401


def test_authenticated_report_supports_main_page_filters(monkeypatch):
    monkeypatch.setattr(web, "_current_user", lambda request: {"callsign": "OWNER1"})
    calls = {}

    class ReportStore:
        def continents(self):
            return ["Europe"]

        def countries(self, continent=None):
            assert continent == "Europe"
            return [{"value": "ES", "label": "Spain"}]

        def user_report(self, callsign, start, continent, country, talkgroups, bucket_seconds):
            calls["filters"] = (callsign, continent, country, talkgroups, bucket_seconds)
            return _report_fixture()

    monkeypatch.setattr(web, "get_store", lambda: ReportStore())

    page = web.user_reports(
        _request(),
        timeRange="2M",
        continent="Europe",
        country="ES",
        talkgroup=[214, 91],
        callsign="EA7",
    )
    body = page.body.decode("utf-8")

    assert calls["filters"][0] == "EA7"
    assert calls["filters"][2:4] == ("ES", [214, 91])
    assert calls["filters"][4] == 3 * 24 * 60 * 60
    assert 'name="callsign"' in body
    assert 'name="talkgroup" multiple' in body
    assert 'value="2M" selected' in body
    assert 'value="Europe" selected' in body
    assert 'value="ES" selected' in body
    assert 'value="214" selected' in body
    assert 'value="91" selected' in body
    assert body.index('value="214" selected') < body.index('value="91" selected')
    assert "/user/reports/export.csv?timeRange=2M" in body
    assert 'id="reportTalkgroups"' in body
    assert "Volker Kerkhoff" in body
    assert "Spain" in body
    assert "QSO details" not in body


def test_reports_limit_tables_and_exports_to_top_50_entries():
    report = _report_fixture()
    report["daily"] = [{"day": f"2026-01-{index:02d}", "qso_count": index, "duration_seconds": index} for index in range(1, 61)]
    report["talkgroups"] = [{"talkgroup_id": index, "name": f"TG {index}", "qso_count": index, "duration_seconds": index, "last_seen_at": None} for index in range(1, 61)]
    report["callsigns"] = [{"callsign": f"CALL{index}", "qso_count": index, "duration_seconds": index, "unique_talkgroups": 1, "last_seen_at": None} for index in range(1, 61)]
    report["qsos"] = [{"start_at": f"2026-01-{index:02d}", "destination_name": "TG", "destination_id": 214, "slot": 1, "duration_seconds": index} for index in range(1, 61)]

    limited = web._limit_report_entries(report)

    assert all(len(limited[key]) == 50 for key in ("daily", "talkgroups", "callsigns"))
    assert limited["talkgroups"][0]["qso_count"] == 60
    assert limited["callsigns"][0]["qso_count"] == 60
    assert limited["talkgroups"][-1]["qso_count"] == 11
    assert limited["callsigns"][-1]["qso_count"] == 11
    assert len(web._report_csv(report).decode("utf-8-sig").splitlines()) == 57


def test_pdf_report_contains_graphics():
    report = _report_fixture()
    pdf = web._report_pdf(report, "ALL", "en")

    assert pdf.startswith(b"%PDF")
    assert web._pdf_histogram(report["histogram"], 480) is not None
    assert web._pdf_bar_chart(report["talkgroups"], "name", "qso_count", 480) is not None
    assert web._pdf_bar_chart(web._report_callsign_chart_rows(report["callsigns"]), "report_label", "qso_count", 480) is not None


def test_report_histogram_labels_are_compact():
    assert web._report_histogram_label("2026-07-29T04:05:00+00:00", 3600) == "04:05"
    assert web._report_histogram_label("2026-07-29T04:05:00+00:00", 86400) == "29/07"


def test_report_csv_excludes_qso_details_and_includes_callsign_context():
    csv_text = web._report_csv(_report_fixture()).decode("utf-8-sig")

    assert "Volker Kerkhoff" in csv_text
    assert "Spain" in csv_text
    assert "Talkgroup ID" not in csv_text
    assert "Duration seconds" not in csv_text
