"""The installed app against this gateway, with the exact JSON its Swift models send and decode (app↔gateway audit,
2026-11-22). The app never sends X-Azel-Result: 2."""
import asyncio
import json
from datetime import datetime, timedelta

from test_approvals import CMD, agw  # noqa: F401  (fixture)
from test_gateway import gw  # noqa: F401  (fixture)
from test_wake_enroll import api  # noqa: F401  (fixture)


def tomorrow_9(g):
    return (datetime.now(g.KST) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)


def test_a_repeating_schedule_from_the_app_gets_its_confirm_and_is_made(gw):  # noqa: F811
    g, c, _ = gw
    at = tomorrow_9(g)
    # ScheduleAPI.actionBody(repeatCron:) — always start_date with repeat_cron, no confirmed on the first try
    body = {"kind": "reminder", "notify": "call", "purpose": "약 먹기", "repeat_cron": "0 9 * * *",
            "start_date": at.date().isoformat()}
    r = c.post("/v1/schedule", json=body)
    assert r.status_code == 200 and r.json()["needs"]["kind"] == "confirm" and r.json()["needs"]["question"], r.text
    assert not g.fake.db["schedule"]  # nothing made before the yes
    r = c.post("/v1/schedule", json={**body, "confirmed": True})  # the app's resend after 네
    assert r.status_code == 201 and r.json()["ok"] and r.json()["spoken"], r.text
    sid = r.json()["data"]["id"]
    # ScheduleAPI.editBody — the editor pre-fills purpose from the note (up to 200 like create's reason)
    long = "물 마시기 " * 20
    r = c.put(f"/v1/schedule/{sid}", json={"notify": "call", "purpose": long.strip(), "scope": "rule", "confirmed": True})
    assert r.status_code == 200 and r.json()["ok"], r.text
    missing = c.delete("/v1/schedule/nope")
    assert missing.status_code == 404 and missing.json()["error"]["message"]  # GatewayError shows this, not "HTTP 404"
    legacy = c.post("/v1/schedule", json={"at": at.isoformat(), "kind": "reminder", "repeat": None, "note": "옛 앱"})
    assert legacy.status_code == 201 and legacy.json()["note"] == "옛 앱"  # ScheduleAPI.create decodes the item itself


def test_an_app_connectors_save_keeps_oauth(gw):  # noqa: F811
    g, c, _ = gw
    server = {"name": "todoist", "url": "https://ai.todoist.net/mcp", "transport": "http", "enabled": True}
    assert c.put("/v1/connectors", json={"claude_ai": [], "custom": [{**server, "oauth": True}]}).status_code == 200
    app_shape = {**server, "has_secret": False}  # Connectors.Custom: name, url, transport, enabled, has_secret
    assert c.put("/v1/connectors", json={"claude_ai": [], "custom": [app_shape]}).status_code == 200
    stored = asyncio.run(g.load_settings())["connectors"]["custom"]
    assert stored[0]["name"] == "todoist" and stored[0].get("oauth") is True, stored


def test_an_app_settings_save_keeps_the_overnight_budget(gw):  # noqa: F811
    g, c, _ = gw
    full = {"window": ["01:00", "06:00"], "report_push": True, "report_call": False, "budget_usd": 12.5}
    assert c.put("/v1/settings", json={"overnight": full}).status_code == 200
    app_shape = {"window": ["00:30", "05:30"], "report_push": False, "report_call": True}  # OvernightSettings: no budget
    assert c.put("/v1/settings", json={"overnight": app_shape}).status_code == 200
    assert c.get("/v1/settings").json()["overnight"] == {**app_shape, "budget_usd": 12.5}


def test_wake_metrics_decode_as_the_apps_string_to_double(api):  # noqa: F811
    m, c, _, _ = api
    (m.DIR / "clips").mkdir(parents=True, exist_ok=True)
    m.write_status(status="ready", trained_at="2026-11-21T00:00:00+00:00", improved=True, metrics={
        "improved": True, "threshold": 0.5, "clips_train": 24, "held_out_few": False, "recall_before": 0.6,
        "recall_after": 0.9, "fp_per_hour_after": None, "seconds": {"total": 300}})
    metrics = c.get("/v1/wake/enroll").json()["metrics"]
    assert metrics == {"threshold": 0.5, "clips_train": 24, "recall_before": 0.6, "recall_after": 0.9}
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in metrics.values())


def test_approval_items_carry_forced_once_and_persona(agw):  # noqa: F811
    g, c, _ = agw
    c.post("/v1/approvals", json={**CMD, "command": "rm -rf build", "requester": "ask:luna:call-1"})
    c.post("/v1/approvals", json={**CMD, "command": "ls -la", "requester": "ask:azel:call-2"})
    items = {a["command"]: a for a in json.loads(c.get("/v1/approvals").content)}  # Gateway.parseApprovals: bare list
    assert items["rm -rf build"]["forced_once"] is True and items["rm -rf build"]["forced_reason"] == g.ALWAYS_NO
    assert items["rm -rf build"]["persona"] == "luna" and items["ls -la"]["persona"] == "azel"
    assert "forced_once" not in items["ls -la"]  # read-only: 항상 허용 stays on offer
