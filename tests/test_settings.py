"""Phase 1: dependency-free settings module."""

from sqlmodel import Session

from app.services import settings as app_settings


def test_get_default_when_missing(fresh_engine):
    with Session(fresh_engine) as s:
        assert app_settings.get(s, "nope", "fallback") == "fallback"


def test_get_bool(fresh_engine):
    with Session(fresh_engine) as s:
        app_settings.set(s, "flag_true", "true")
        app_settings.set(s, "flag_false", "false")
        assert app_settings.get_bool(s, "flag_true") is True
        assert app_settings.get_bool(s, "flag_false") is False
        assert app_settings.get_bool(s, "missing", True) is True


def test_get_json_valid(fresh_engine):
    with Session(fresh_engine) as s:
        app_settings.set(s, "fm", '{"Wii": "wii"}')
        assert app_settings.get_json(s, "fm", {}) == {"Wii": "wii"}


def test_get_json_malformed_returns_default(fresh_engine):
    # The crash class: a corrupted folder_map must NOT raise / 500 a page.
    with Session(fresh_engine) as s:
        app_settings.set(s, "fm", "{not valid json")
        assert app_settings.get_json(s, "fm", {}) == {}


def test_get_json_empty_returns_default(fresh_engine):
    with Session(fresh_engine) as s:
        app_settings.set(s, "fm", "")
        assert app_settings.get_json(s, "fm", {"d": 1}) == {"d": 1}


def test_set_upserts(fresh_engine):
    with Session(fresh_engine) as s:
        app_settings.set(s, "k", "v1")
        app_settings.set(s, "k", "v2")
        assert app_settings.get(s, "k") == "v2"


def test_extension_config_prefix_strip(fresh_engine):
    with Session(fresh_engine) as s:
        app_settings.set(s, "ext_vimm_headless", "true")
        app_settings.set(s, "ext_vimm_api_key", "abc")
        app_settings.set(s, "ext_other_x", "1")
        cfg = app_settings.get_extension_config(s, "vimm")
        assert cfg == {"headless": "true", "api_key": "abc"}
