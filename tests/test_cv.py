import json

import httpx
import respx

from job_radar.commands import answer
from job_radar.cv import Cv, render, tailor_cv
from job_radar.db import Db
from job_radar.models import Match, make_job
from job_radar.notify import Notifier
from job_radar.scorer import URL, Scorer

RESUME = ("B.Tech in Computer Science Engineering, 2022-2026, CGPA 7.56. Python, PyTorch, Next.js, FastAPI, Docker. "
          "ManTacAi, forensic AI system (Apr 2025 - Mar 2026): fine-tuned BERT, 96.8% test accuracy. "
          "Head Technical Coordinator, college tech fest. Deep Learning Specialization (DeepLearning.AI).")
ENDPOINT = URL.format(model="m")
GOOD = {"summary": "Computer science graduate building AI and web systems with Python and PyTorch.",
        "education": "B.Tech in Computer Science Engineering, 2022-2026, CGPA 7.56",
        "skills": ["Python", "PyTorch", "FastAPI", "Docker", "Next.js"],
        "projects": [{"heading": "ManTacAi, forensic AI system", "dates": "Apr 2025 - Mar 2026",
                      "bullets": ["Fine-tuned BERT to 96.8% test accuracy."]}],
        "experience": [{"heading": "Head Technical Coordinator, college tech fest", "dates": "", "bullets": []}],
        "certifications": ["Deep Learning Specialization (DeepLearning.AI)"]}
PADDED = {**GOOD, "skills": GOOD["skills"] + ["Kubernetes"]}  # what the posting wanted, not in the resume


def reply(obj):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]})


def scorer():
    return Scorer("k", "m", RESUME, httpx.Client(), "", Db(":memory:"))


JOB = make_job("lever", "1", "ML Engineer", "Acme", "Dubai", "http://a", "Python, PyTorch, Kubernetes.", None)


OK = {"unsupported": []}


def prompt(route, i):
    return json.loads(route.calls[i].request.content)["contents"][0]["parts"][0]["text"]


@respx.mock
def test_clean_cv_tailor_and_review_only():
    route = respx.post(ENDPOINT).mock(side_effect=[reply(GOOD), reply(OK)])
    assert tailor_cv(scorer(), JOB, 400).skills[0] == "Python" and route.call_count == 2


@respx.mock
def test_skill_from_the_posting_is_removed_or_refused():
    route = respx.post(ENDPOINT).mock(side_effect=[reply(PADDED), reply(OK), reply(GOOD)])
    assert "Kubernetes" not in tailor_cv(scorer(), JOB, 400).skills  # the reviewer missed it; the code didn't
    assert "Kubernetes" in prompt(route, 2)
    respx.post(ENDPOINT).mock(side_effect=[reply(PADDED), reply(OK), reply(PADDED)])
    assert tailor_cv(scorer(), JOB, 400) is None


@respx.mock
def test_reviewer_catches_soft_claims_the_code_cannot():
    fluffy = {**GOOD, "summary": "Graduate with experience in regulatory compliance and documentation."}
    route = respx.post(ENDPOINT).mock(side_effect=[
        reply(fluffy), reply({"unsupported": ["experience in regulatory compliance"]}), reply(GOOD)])
    assert tailor_cv(scorer(), JOB, 400).summary == GOOD["summary"]
    assert "regulatory compliance" in prompt(route, 2)


def test_render_is_one_page_pdf_even_with_unicode():
    cv = Cv.model_validate({**GOOD, "summary": GOOD["summary"] + " Ships fast – and “tested” • always."})
    pdf = render(cv, "Test Name", "+91 00000 | me@example.com")
    assert pdf.startswith(b"%PDF") and b"/Count 1" in pdf


def test_render_shrinks_long_cvs_to_one_page(monkeypatch):
    long = {**GOOD, "experience": [{"heading": f"Role {i}", "dates": "2025", "bullets": ["Led the team. " * 25] * 2}
                                   for i in range(2)],
            "projects": [{"heading": f"Project {i}", "dates": "2025",
                          "bullets": ["Built and shipped a thing with Python and PyTorch. " * 5] * 3}
                         for i in range(3)]}
    from job_radar import cv as cvmod
    sizes, orig = [], cvmod._draw
    monkeypatch.setattr(cvmod, "_draw", lambda *a: sizes.append(a[3]) or orig(*a))
    pdf = render(Cv.model_validate(long), "N", "C")
    assert sizes[0] == 10.0 and len(sizes) > 1 and b"/Count 1" in pdf  # too long at 10pt, fits once shrunk


@respx.mock
def test_cv_command_sends_a_pdf(cfg, monkeypatch):
    monkeypatch.setenv("CV_NAME", "Test Name")
    respx.post(ENDPOINT).mock(return_value=reply(GOOD))
    doc = respx.post("https://api.telegram.org/botT/sendDocument").respond(json={"ok": True})
    db = Db(":memory:")
    j = make_job("lever", "1", "ML Engineer", "Acme", "Dubai", "http://a", "Python role", None)
    db.save(j, "notified")
    db.save_posting(j)
    db.save_match(j.id, Match(score=80, reason="fit"))
    answer(f"/cv #{j.id[:6]}", cfg, db, scorer(), Notifier("T", "42", httpx.Client()))
    body = doc.calls[0].request.content
    assert b"CV_Acme_ML_Engineer.pdf" in body and b"%PDF" in body and b"Read it before sending" in body
