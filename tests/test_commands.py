import httpx
import respx

from job_radar.commands import HELP, fetch_commands, handle
from job_radar.db import Db
from job_radar.main import run
from job_radar.models import Match, make_job
from job_radar.notify import Notifier, format_job

UPDATES = "https://api.telegram.org/botT/getUpdates"


def msg(uid, text, chat=42):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def saved_job(db, score=82):
    j = make_job("lever", "1", "AI Engineer", "Acme", "Dubai", "http://a", "Python role.", None)
    db.save(j, "notified")
    db.save_posting(j)
    if score is not None:
        db.save_match(j.id, Match(score=score, reason="good fit", missing_skills=["Docker"]))
    return j


@respx.mock
def test_only_owner_commands_and_offset_saved():
    route = respx.get(UPDATES).respond(json={"ok": True, "result": [
        msg(5, "/status"), msg(6, "/pause", chat=999), msg(7, "hello"), msg(8, "/why #abc123")]})
    db = Db(":memory:")
    assert fetch_commands(httpx.Client(), "T", "42", db) == ["/status", "/why #abc123"]
    assert db.get_meta("tg:offset") == "9"
    fetch_commands(httpx.Client(), "T", "42", db)
    assert route.calls[1].request.url.params["offset"] == "9"  # each command is answered once


@respx.mock
def test_telegram_error_is_not_fatal():
    respx.get(UPDATES).respond(502)
    assert fetch_commands(httpx.Client(), "T", "42", Db(":memory:")) == []


def test_why_and_unknown_refs(cfg):
    db = Db(":memory:")
    j = saved_job(db)
    out = handle(f"/why #{j.id[:6]}", cfg, db, None)
    assert "82/100" in out and "Docker" in out and "status: notified" in out
    assert handle("/why@JobRadarBot #ffffff", cfg, db, None) == "No job #ffffff found."
    assert "e.g. /why #a1b2c3" in handle("/why", cfg, db, None)
    assert handle("/start", cfg, db, None) == HELP


def test_cover_reuses_saved_note_or_writes_one(cfg, monkeypatch):
    db = Db(":memory:")
    j = saved_job(db)
    calls = []
    monkeypatch.setattr("job_radar.commands.write_note", lambda s, job, *a: calls.append(job) or "Fresh note")
    out = handle(f"/cover {j.id[:6]}", cfg, db, object())
    assert "Fresh note" in out and calls[0].description == "Python role." and calls[0].location == "Dubai"
    assert "Fresh note" in handle(f"/cover #{j.id[:6]}", cfg, db, object()) and len(calls) == 1
    assert handle(f"/cover #{j.id[:6]}", cfg, Db(":memory:"), None).startswith("No job")


def test_status(cfg):
    db = Db(":memory:")
    db.record_health("ibm", False)
    dt = __import__("datetime")
    db.add_usage("jsearch", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"), 12)  # UTC month, like the code
    out = handle("/status", cfg, db, None)
    assert "Alerts: on" in out and "JSearch: 12/180" in out and "ibm (1 runs)" in out


@respx.mock
def test_pause_holds_alerts_and_resume_sends_them(cfg, monkeypatch):
    cfg.telegram.webhook = False  # this test drives commands through polling, whatever config.yaml says
    updates = respx.get(UPDATES)
    tg = respx.post("https://api.telegram.org/botT/sendMessage").respond(json={"ok": True})
    db, client = Db(":memory:"), httpx.Client()
    n = Notifier("T", "42", client)
    j = make_job("lever", "1", "Graduate Engineer", "Acme", "Dubai", "http://a", "Python", None)

    class Src:
        name, errors = "lever", 0

        def fetch(self):
            return [j]

    monkeypatch.setattr("job_radar.main.build_sources", lambda *a, **k: [Src()])
    updates.respond(json={"ok": True, "result": [msg(1, "/pause")]})
    s1 = run(cfg, {}, db, n, client)
    assert s1["held_paused"] == 1 and s1["notified"] == 0 and not db.seen(j.id)
    updates.respond(json={"ok": True, "result": [msg(2, "/resume")]})
    s2 = run(cfg, {}, db, n, client)
    assert s2["notified"] == 1 and db.find_job(j.id[:6])[0].description == "Python"
    texts = [c.request.content.decode() for c in tg.calls]
    assert "paused" in texts[0] and "resumed" in texts[1] and f"#{j.id[:6]}" in texts[2]


@respx.mock
def test_command_mode_answers_and_skips_the_job_search(tmp_path, monkeypatch):
    tg = respx.post("https://api.telegram.org/botT/sendMessage").respond(json={"ok": True})
    monkeypatch.setattr("job_radar.main.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no run")))
    monkeypatch.setattr("job_radar.main.load_env_file", lambda: None)
    for k, v in {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "42", "JOB_RADAR_COMMAND": "/pause"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["job_radar", "--db", str(tmp_path / "s.db")])
    from job_radar.main import main
    main()
    assert "paused" in tg.calls[0].request.content.decode()
    assert Db(tmp_path / "s.db").get_meta("paused") == "1"


@respx.mock
def test_webhook_mode_never_polls(cfg, monkeypatch):
    cfg.telegram.webhook = True
    monkeypatch.setattr("job_radar.main.build_sources", lambda *a, **k: [])
    run(cfg, {}, Db(":memory:"), Notifier("T", "42", httpx.Client()), httpx.Client())  # unmocked getUpdates would fail


def test_alert_shows_ref():
    j = make_job("lever", "1", "Dev", "Acme", "Dubai", "http://a", "x", None)
    assert format_job(j).endswith(f"#{j.id[:6]}")
