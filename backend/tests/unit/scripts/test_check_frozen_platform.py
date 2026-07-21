"""Unit tests for the ADR-0042 orchestration-platform freeze guard.

These exercise the guard's pure matching / override / decision logic without
mutating any real frozen file (doing so is exactly what the guard blocks). We
load the standalone script by path and monkeypatch its git seam.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_frozen_platform.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_frozen_platform", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def test_frozen_list_matches_adr0042_and_all_paths_exist() -> None:
    # The guard's list is the machine-readable mirror of ADR-0042 §D1. If a
    # frozen module is renamed/moved without updating the list (or the ADR), this
    # fails — keeping the contract and the enforcement in lock-step.
    repo_root = _SCRIPT.resolve().parents[2]
    assert guard.FROZEN_PATHS, "frozen list must not be empty"
    assert len(guard.FROZEN_PATHS) == len(set(guard.FROZEN_PATHS)), "no duplicates"
    for rel in guard.FROZEN_PATHS:
        assert (repo_root / rel).exists(), f"stale frozen path: {rel}"


def test_is_frozen_matches_exact_files_only() -> None:
    assert guard._is_frozen("backend/app/application/use_cases/workflow/completion_engine.py")
    assert guard._is_frozen("backend/app/infrastructure/ai/dispatcher.py")
    # A concrete adapter is a growth surface, NOT frozen (ADR-0042 §D1).
    assert not guard._is_frozen("backend/app/infrastructure/ai/providers/fal/video.py")
    assert not guard._is_frozen("backend/app/api/v1/routers/media.py")


def test_main_passes_when_no_frozen_path_changed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(guard, "_resolve_base", lambda _explicit: "main")
    monkeypatch.setattr(
        guard,
        "_changed_files",
        lambda _base: {"backend/app/infrastructure/ai/providers/fal/webhook.py"},
    )
    assert guard.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_blocks_when_frozen_path_changed_without_override(monkeypatch, capsys) -> None:
    monkeypatch.setattr(guard, "_resolve_base", lambda _explicit: "main")
    monkeypatch.setattr(
        guard,
        "_changed_files",
        lambda _base: {"backend/app/application/use_cases/workflow/completion_engine.py"},
    )
    monkeypatch.setattr(guard, "_has_override", lambda _base: (False, ""))
    assert guard.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "completion_engine.py" in out


def test_main_allows_frozen_change_with_override(monkeypatch, capsys) -> None:
    monkeypatch.setattr(guard, "_resolve_base", lambda _explicit: "main")
    monkeypatch.setattr(
        guard,
        "_changed_files",
        lambda _base: {"backend/app/application/interfaces/providers.py"},
    )
    monkeypatch.setattr(
        guard, "_has_override", lambda _base: (True, "Freeze-Override: ADR-9999 test")
    )
    assert guard.main([]) == 0
    assert "OVERRIDE accepted" in capsys.readouterr().out


def test_env_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_FROZEN_CHANGES", "1")
    ok, marker = guard._has_override("main")
    assert ok and "env" in marker


def test_missing_base_is_soft_pass_but_strict_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(guard, "_resolve_base", lambda _explicit: None)
    assert guard.main([]) == 0  # soft pass (initial-push edge case)
    assert guard.main(["--strict"]) == 1
