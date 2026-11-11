import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import FakeShipyard

import plugin


@pytest.fixture
def shipyard():
    fake = FakeShipyard()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    monkeypatch.setattr(plugin, "REQUEST_RETRY_DELAY", 0)
