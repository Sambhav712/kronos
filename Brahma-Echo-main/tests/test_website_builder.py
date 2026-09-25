from __future__ import annotations

import json
from pathlib import Path
import pytest

from actions.website_builder import (
    _safe_slug,
    _sanitize_text,
    _choose_layout_mode,
    _is_safe_workspace,
    _fallback_code_bundle,
    website_builder,
    BASE_DIR,
)
from main import _looks_like_website_request, _looks_like_code_request
from core.undo import _stack


def test_safe_slug_and_sanitize():
    assert _safe_slug("My Coffee Shop!") == "my-coffee-shop"
    assert _safe_slug("   ") == "website"
    assert _sanitize_text("  hello   world  ") == "hello world"
    assert _sanitize_text("", "fallback") == "fallback"


def test_layout_modes_selection():
    assert _choose_layout_mode("Create a calculator") == "minimal"
    assert _choose_layout_mode("A blog and culture article on Indian heritage") == "article"
    assert _choose_layout_mode("A modern photography portfolio showcase") == "gallery"
    assert _choose_layout_mode("A SaaS dashboard studio workspace") == "command_center"
    assert _choose_layout_mode("A coffee shop business landing page") == "split"


def test_safe_workspace_validation(tmp_path):
    # Safe user folder should pass
    assert _is_safe_workspace(tmp_path) is True

    # Brahma Echo source folder should be rejected
    assert _is_safe_workspace(BASE_DIR) is False
    assert _is_safe_workspace(BASE_DIR / "actions") is False

    # Root drive should be rejected
    assert _is_safe_workspace(Path("C:\\")) is False


def test_command_routing_detection():
    # Website intents that MUST route to website_builder
    assert _looks_like_website_request("build a website") is True
    assert _looks_like_website_request("create a landing page") is True
    assert _looks_like_website_request("make a website for my coffee shop") is True
    assert _looks_like_website_request("design a web page for portfolio") is True
    assert _looks_like_website_request("create a homepage for bakery") is True

    # General non-website code requests should not be classified as website-only
    assert _looks_like_code_request("build a python automation script") is True
    assert _looks_like_website_request("write a python automation script") is False


def test_fallback_code_bundle_generation():
    params = {
        "description": "Artisan Bakery & Cafe",
        "site_name": "Crumb & Co",
    }
    bundle = _fallback_code_bundle(params)
    assert bundle["project_name"] == "crumb-co"
    assert bundle["site_name"] == "Crumb & Co"

    files = bundle["files"]
    assert "index.html" in files
    assert "styles.css" in files
    assert "script.js" in files
    assert "site-data.json" in files
    assert "README.md" in files

    # Verify HTML contents
    html = files["index.html"]
    assert "<!DOCTYPE html>" in html
    assert "Crumb &amp; Co" in html or "Crumb & Co" in html
    assert "<title>" in html

    # Verify site-data.json is parseable
    data = json.loads(files["site-data.json"])
    assert data["site_name"] == "Crumb & Co"


def test_website_builder_output_generation(tmp_path):
    output_dir = tmp_path / "Websites"
    logs = []

    class MockPlayer:
        def write_log(self, msg: str):
            logs.append(msg)

    spoken = []

    def mock_speak(msg: str):
        spoken.append(msg)

    result = website_builder(
        parameters={
            "description": "Cozy bookstore in Seattle",
            "site_name": "Rainy Day Books",
            "workspace_path": str(output_dir),
            "preview": False,
            "auto_open": False,
        },
        player=MockPlayer(),
        speak=mock_speak,
    )

    project_dir = output_dir / "rainy-day-books"
    assert project_dir.exists()
    assert (project_dir / "index.html").exists()
    assert (project_dir / "styles.css").exists()
    assert (project_dir / "script.js").exists()
    assert (project_dir / "site-data.json").exists()
    assert (project_dir / "README.md").exists()

    assert "Website built" in result
    assert any("Rainy Day Books" in log or "rainy-day-books" in log for log in logs)


def test_website_builder_undo_flow(tmp_path):
    output_dir = tmp_path / "Websites"
    site_name = "Undoable Cafe"

    res = website_builder(
        parameters={
            "description": "Quick cafe site",
            "site_name": site_name,
            "workspace_path": str(output_dir),
            "preview": False,
            "auto_open": False,
        }
    )

    project_dir = output_dir / "undoable-cafe"
    assert project_dir.exists()

    # Verify an undo entry was pushed to the stack
    assert len(_stack) > 0
    latest = _stack[-1]
    assert "undoable-cafe" in latest.label

    # Execute undo
    undo_result = latest.undo()
    assert "Removed created website directory" in undo_result
    assert not project_dir.exists()


def test_website_builder_overwrite_safety(tmp_path):
    output_dir = tmp_path / "Websites"
    project_dir = output_dir / "protected-site"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Place a user file in the directory
    user_file = project_dir / "personal_notes.txt"
    user_file.write_text("Do not overwrite!", encoding="utf-8")

    # Attempting to build without overwrite=True must be blocked
    res = website_builder(
        parameters={
            "description": "A new site",
            "site_name": "Protected Site",
            "workspace_path": str(output_dir),
            "preview": False,
            "auto_open": False,
            "overwrite": False,
        }
    )
    assert "Please confirm overwrite to proceed safely" in res
    assert user_file.read_text(encoding="utf-8") == "Do not overwrite!"

    # With overwrite=True, it should proceed
    res_overwrite = website_builder(
        parameters={
            "description": "A new site",
            "site_name": "Protected Site",
            "workspace_path": str(output_dir),
            "preview": False,
            "auto_open": False,
            "overwrite": True,
        }
    )
    assert "Website built" in res_overwrite
    assert (project_dir / "index.html").exists()
