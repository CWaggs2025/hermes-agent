"""Tests for external skill directories (skills.external_dirs config)."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def external_skills_dir(tmp_path):
    """Create a temp dir with a sample external skill."""
    ext_dir = tmp_path / "external-skills"
    skill_dir = ext_dir / "my-external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-external-skill\ndescription: A skill from an external directory\n---\n\n# My External Skill\n\nDo external things.\n"
    )
    return ext_dir


@pytest.fixture
def hermes_home(tmp_path):
    """Create a minimal HERMES_HOME with config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    return home


class TestGetExternalSkillsDirs:
    def test_empty_config(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  external_dirs: []\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert result == []


    def test_valid_dir_returned(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert len(result) == 1
        assert result[0] == external_skills_dir.resolve()

    def test_gateway_without_snapshot_never_resolves_configured_root(
        self, hermes_home, monkeypatch
    ):
        from agent import skill_utils

        external = hermes_home.parent / "network-mount-that-must-not-be-touched"
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external}\n",
            encoding="utf-8",
        )
        original_resolve = Path.resolve
        original_is_dir = Path.is_dir

        def guarded_resolve(path, *args, **kwargs):
            if str(path).startswith(str(external)):
                raise AssertionError("gateway resolved a configured external root")
            return original_resolve(path, *args, **kwargs)

        def guarded_is_dir(path):
            if str(path).startswith(str(external)):
                raise AssertionError("gateway stated a configured external root")
            return original_is_dir(path)

        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(hermes_home), "_HERMES_GATEWAY": "1"},
        ):
            skill_utils._external_dirs_cache_clear()
            monkeypatch.setattr(Path, "resolve", guarded_resolve)
            monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
            assert skill_utils.get_external_skills_dirs() == []

    def test_gateway_uses_only_matching_materialized_snapshot(
        self, hermes_home, external_skills_dir, monkeypatch
    ):
        from agent import skill_utils

        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(hermes_home), "_HERMES_GATEWAY": "1"},
        ):
            skill_utils._external_dirs_cache_clear()
            roots, _timeout = skill_utils.get_external_skills_scan_settings()
            fingerprint = skill_utils.external_skills_roots_fingerprint(roots)
            relative_root = f"{fingerprint}/generation/root-0000"
            materialized = (
                skill_utils.external_skills_snapshot_dir() / relative_root
            )
            package = materialized / "my-external-skill"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: my-external-skill\ndescription: Materialized\n---\n",
                encoding="utf-8",
            )
            catalog = skill_utils.external_skills_catalog_path()
            catalog.parent.mkdir(parents=True, exist_ok=True)
            catalog.write_text(
                json.dumps(
                    {
                        "version": skill_utils.EXTERNAL_SKILLS_CATALOG_VERSION,
                        "roots_fingerprint": fingerprint,
                        "roots": list(roots),
                        "names": ["my-external-skill"],
                        "materialized_complete": True,
                        "materialized_roots": [relative_root],
                    }
                ),
                encoding="utf-8",
            )

            original_resolve = Path.resolve
            original_is_dir = Path.is_dir

            def guarded_resolve(path, *args, **kwargs):
                if str(path).startswith(str(external_skills_dir)):
                    raise AssertionError("gateway resolved the live external root")
                return original_resolve(path, *args, **kwargs)

            def guarded_is_dir(path):
                if str(path).startswith(str(external_skills_dir)):
                    raise AssertionError("gateway stated the live external root")
                return original_is_dir(path)

            monkeypatch.setattr(Path, "resolve", guarded_resolve)
            monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
            skill_utils._external_dirs_cache_clear()
            assert skill_utils.get_external_skills_dirs() == [materialized]

    def test_gateway_rejects_symlinked_materialized_root(
        self, hermes_home, external_skills_dir
    ):
        from agent import skill_utils

        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(hermes_home), "_HERMES_GATEWAY": "1"},
        ):
            skill_utils._external_dirs_cache_clear()
            roots, _timeout = skill_utils.get_external_skills_scan_settings()
            fingerprint = skill_utils.external_skills_roots_fingerprint(roots)
            relative_root = f"{fingerprint}/generation/root-0000"
            candidate = skill_utils.external_skills_snapshot_dir() / relative_root
            candidate.parent.mkdir(parents=True)
            try:
                candidate.symlink_to(external_skills_dir, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are unavailable")
            catalog = skill_utils.external_skills_catalog_path()
            catalog.parent.mkdir(parents=True, exist_ok=True)
            catalog.write_text(
                json.dumps(
                    {
                        "version": skill_utils.EXTERNAL_SKILLS_CATALOG_VERSION,
                        "roots_fingerprint": fingerprint,
                        "roots": list(roots),
                        "names": ["my-external-skill"],
                        "materialized_complete": True,
                        "materialized_roots": [relative_root],
                    }
                ),
                encoding="utf-8",
            )
            skill_utils._external_dirs_cache_clear()
            assert skill_utils.get_external_skills_dirs() == []

    def test_gateway_rejects_symlinked_catalog_pointer(
        self, hermes_home, external_skills_dir
    ):
        from agent import skill_utils

        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(hermes_home), "_HERMES_GATEWAY": "1"},
        ):
            skill_utils._external_dirs_cache_clear()
            roots, _timeout = skill_utils.get_external_skills_scan_settings()
            fingerprint = skill_utils.external_skills_roots_fingerprint(roots)
            relative_root = f"{fingerprint}/generation/root-0000"
            materialized = skill_utils.external_skills_snapshot_dir() / relative_root
            materialized.mkdir(parents=True)
            external_catalog = external_skills_dir / "catalog.json"
            external_catalog.write_text(
                json.dumps(
                    {
                        "version": skill_utils.EXTERNAL_SKILLS_CATALOG_VERSION,
                        "roots_fingerprint": fingerprint,
                        "roots": list(roots),
                        "names": ["my-external-skill"],
                        "materialized_complete": True,
                        "materialized_roots": [relative_root],
                    }
                ),
                encoding="utf-8",
            )
            catalog = skill_utils.external_skills_catalog_path()
            catalog.parent.mkdir(parents=True, exist_ok=True)
            try:
                catalog.symlink_to(external_catalog)
            except OSError:
                pytest.skip("file symlinks are unavailable")

            skill_utils._external_dirs_cache_clear()
            assert skill_utils.get_external_skills_dirs() == []

    def test_gateway_catalog_fifo_fails_closed_without_blocking(
        self, hermes_home, external_skills_dir
    ):
        from agent import skill_utils

        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are unavailable")
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n",
            encoding="utf-8",
        )
        catalog = skill_utils.external_skills_catalog_path(hermes_home)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(catalog)
        completed = threading.Event()
        observed = []
        errors = []

        def resolve_gateway_dirs():
            try:
                observed.extend(skill_utils.get_external_skills_dirs())
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        with patch.dict(
            os.environ,
            {"HERMES_HOME": str(hermes_home), "_HERMES_GATEWAY": "1"},
        ):
            skill_utils._external_dirs_cache_clear()
            thread = threading.Thread(target=resolve_gateway_dirs, daemon=True)
            thread.start()
            assert completed.wait(timeout=1.0), "gateway blocked opening catalog FIFO"
            thread.join(timeout=1.0)

        assert observed == []
        assert errors == []


class TestGetAllSkillsDirs:
    def test_local_always_first(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_all_skills_dirs
            result = get_all_skills_dirs()
        assert result[0] == hermes_home / "skills"
        assert result[1] == external_skills_dir.resolve()


class TestExternalSkillsInFindAll:
    def test_external_skills_found(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        names = [s["name"] for s in skills]
        assert "my-external-skill" in names

    def test_local_takes_precedence(self, hermes_home, external_skills_dir):
        """If the same skill name exists locally and externally, local wins."""
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "my-external-skill"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: my-external-skill\ndescription: Local version\n---\n\nLocal.\n"
        )
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        matching = [s for s in skills if s["name"] == "my-external-skill"]
        assert len(matching) == 1
        assert matching[0]["description"] == "Local version"


class TestExternalSkillView:
    def test_skill_view_finds_external(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("my-external-skill"))
        assert result["success"] is True
        assert "external things" in result["content"]


def test_gateway_runtime_skill_scan_does_not_follow_directory_symlinks(
    tmp_path, monkeypatch
):
    from agent.skill_utils import iter_skill_index_files

    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    root = tmp_path / "skills"
    local = root / "local"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("# local\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# outside\n", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    found = list(iter_skill_index_files(root, "SKILL.md"))

    assert found == [local / "SKILL.md"]
