import pytest

from job_radar import notify
from job_radar.config import load_config
from job_radar.sources import base


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(base, "PACE", 0)
    monkeypatch.setattr(notify, "PACE", 0)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)


@pytest.fixture
def cfg():
    return load_config("config.yaml")
