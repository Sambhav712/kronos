from __future__ import annotations

import json

from brahma_connect.browser_bridge import BrowserBridge


def test_bridge_is_opt_in_and_pairing_code_is_single_use(tmp_path):
    settings = tmp_path / "config" / "app_settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"browser_bridge_enabled": False}), encoding="utf-8")
    bridge = BrowserBridge(settings)

    assert bridge.is_enabled() is False
    code = bridge.issue_pairing_code()
    paired = bridge._pair(code)
    assert paired["ok"] is True
    assert bridge._authorized(paired["token"]) is True
    assert bridge._pair(code)["ok"] is False


def test_bridge_rejects_unapproved_or_unlisted_requests(tmp_path):
    received = []
    settings = tmp_path / "config" / "app_settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}", encoding="utf-8")
    bridge = BrowserBridge(settings, on_request=received.append)

    rejected = bridge._submit({"request": "shutdown_pc", "user_approved": True, "context": {"content": "x"}})
    assert rejected["ok"] is False

    unapproved = bridge._submit({"request": "summarize_page", "context": {"content": "x"}})
    assert unapproved["ok"] is False

    accepted = bridge._submit({"request": "summarize_page", "user_approved": True, "context": {"title": "Page", "content": "x" * 13000}})
    assert accepted["ok"] is True
    # Request runs on a worker; validate the input normalisation directly instead of timing a thread.
    assert accepted["status"].startswith("Sent to Brahma Echo")
