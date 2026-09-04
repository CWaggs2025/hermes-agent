"""Tests for tools/skills_sync.py — manifest-based skill seeding and updating."""

import gc
import shutil
import json
import os
import stat
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.skills_sync import (
    ExternalSkillIndexUnavailable,
    _get_bundled_dir,
    _read_manifest,
    _read_skill_name,
    _write_manifest,
    _discover_bundled_skills,
    _compute_relative_dest,
    _dir_hash,
    sync_skills,
    reset_bundled_skill,
    restore_official_optional_skill,
)


class TestReadWriteManifest:
    def test_write_and_read_roundtrip_v2(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        entries = {"zebra": "hash1", "alpha": "hash2", "middle": "hash3"}

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest(entries)
            result = _read_manifest()

        assert result == entries
        # Entries are written sorted for stable diffs.
        names = [line.split(":")[0] for line in manifest_file.read_text().strip().splitlines()]
        assert names == ["alpha", "middle", "zebra"]

        # A missing manifest reads as empty, not an error.
        with patch("tools.skills_sync.MANIFEST_FILE", tmp_path / "nonexistent"):
            assert _read_manifest() == {}

    def test_reads_v1_lines_blanks_and_mixed_formats(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        # v1 format (plain names, no hashes) reads with empty hashes; blank
        # lines are ignored; mixed v1/v2 lines are handled gracefully.
        manifest_file.write_text("old-skill\n\n  \nnew-skill:abc123\n")

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            result = _read_manifest()

        assert result == {"old-skill": "", "new-skill": "abc123"}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are platform-specific")
    def test_write_manifest_preserves_existing_file_mode(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        manifest_file.write_text("old-skill:oldhash\n", encoding="utf-8")
        os.chmod(manifest_file, 0o660)

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest({"new-skill": "newhash"})

        assert manifest_file.read_text(encoding="utf-8") == "new-skill:newhash\n"
        assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o660


class TestDirHash:
    def test_hash_reflects_content_only(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        for d in (dir_a, dir_b):
            d.mkdir()
            (d / "SKILL.md").write_text("# Test")
            (d / "main.py").write_text("print(1)")
        assert _dir_hash(dir_a) == _dir_hash(dir_b)

        (dir_b / "SKILL.md").write_text("# Version 2")
        assert _dir_hash(dir_a) != _dir_hash(dir_b)

        empty = tmp_path / "empty"
        empty.mkdir()
        assert isinstance(_dir_hash(empty), str) and len(_dir_hash(empty)) == 32
        # A nonexistent dir hashes as empty content rather than raising.
        assert isinstance(_dir_hash(tmp_path / "nope"), str)


class TestDiscoverBundledSkills:
    def test_finds_skill_dirs_and_ignores_non_skills(self, tmp_path):
        (tmp_path / "category" / "skill-a").mkdir(parents=True)
        (tmp_path / "category" / "skill-a" / "SKILL.md").write_text("# Skill A")
        (tmp_path / "skill-b").mkdir()
        (tmp_path / "skill-b" / "SKILL.md").write_text("# Skill B")
        (tmp_path / "not-a-skill").mkdir()
        (tmp_path / "not-a-skill" / "README.md").write_text("Not a skill")
        # .git internals never count as skills.
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        (tmp_path / ".git" / "hooks" / "SKILL.md").write_text("# Fake")

        skills = _discover_bundled_skills(tmp_path)
        assert {name for name, _ in skills} == {"skill-a", "skill-b"}

        assert _discover_bundled_skills(tmp_path / "nonexistent") == []

    def test_ignores_nested_skill_packages_in_support_dirs(self, tmp_path):
        real = tmp_path / "category" / "umbrella"
        nested = real / "references" / "archived-skill"
        nested.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: umbrella\n---\n")
        (nested / "SKILL.md").write_text("---\nname: archived-skill\n---\n")

        assert [name for name, _ in _discover_bundled_skills(tmp_path)] == ["umbrella"]


class TestReadSkillName:
    def test_name_from_frontmatter_with_dir_name_fallbacks(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"

        skill_md.write_text("---\nname: audiocraft-audio-generation\n---\n# Skill")
        assert _read_skill_name(skill_md, "audiocraft") == "audiocraft-audio-generation"

        skill_md.write_text('---\nname: "serving-llms-vllm"\n---\n')
        assert _read_skill_name(skill_md, "vllm") == "serving-llms-vllm"

        skill_md.write_text("# Just a heading\nNo frontmatter here")
        assert _read_skill_name(skill_md, "my-skill") == "my-skill"

        skill_md.write_text("---\nname:\n---\n")
        assert _read_skill_name(skill_md, "fallback") == "fallback"

    def test_discover_uses_frontmatter_name(self, tmp_path):
        skill_dir = tmp_path / "category" / "audiocraft"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: audiocraft-audio-generation\n---\n# Skill"
        )
        skills = _discover_bundled_skills(tmp_path)
        assert skills[0][0] == "audiocraft-audio-generation"

    def test_reads_only_the_bounded_frontmatter_prefix(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_bytes(
            b"---\nname: bounded-name\n---\n" + (b"x" * 32_000)
        )
        observed_sizes = []
        real_read = os.read

        def recording_read(file_descriptor, size):
            observed_sizes.append(size)
            return real_read(file_descriptor, size)

        monkeypatch.setattr(skills_sync_module.os, "read", recording_read)

        assert _read_skill_name(skill_md, "fallback") == "bounded-name"
        assert observed_sizes == [skills_sync_module._MAX_SKILL_FRONTMATTER_BYTES]


class TestComputeRelativeDest:
    def test_preserves_category_structure(self):
        bundled = Path("/repo/skills")
        dest = _compute_relative_dest(Path("/repo/skills/mlops/axolotl"), bundled)
        assert str(dest).endswith("mlops/axolotl")
        # Flat (uncategorized) skills keep their own name.
        assert _compute_relative_dest(Path("/repo/skills/simple"), bundled).name == "simple"


class TestRmtreeWritableScopeGuard:
    """``_rmtree_writable`` must refuse to remove anything outside
    ``HERMES_HOME/skills/``.

    The previous implementation called ``shutil.rmtree(path)`` on whatever
    argument the caller passed. If any of the five call sites in
    ``tools/skills_sync.py`` ever computes a path outside the skills
    root — through a bad join, a missing default, a malicious
    bundled-manifest entry, or a stale path in scope after an
    exception — the result is a silent ``shutil.rmtree(~/.hermes/)``
    that destroys the user's ``.env``, ``MEMORY.md``, ``kanban.db``,
    custom skills, scripts, and the rest of the install in one go
    (#48200).

    The scope guard turns that into a loud ``ValueError`` so the
    failure is observable, reproducible, and recoverable rather than
    a data-loss incident.
    """

    def test_refuses_anything_that_is_not_a_strict_child_of_skills(self, tmp_path):
        """``/``, ``~/.hermes`` itself, a sibling dir, and the skills root
        are all rejected — the root because a ``dest`` that collapses to it
        would wipe every installed skill (the degenerate #48200 path)."""
        from tools.skills_sync import _rmtree_writable

        hermes = tmp_path / "home"
        hermes.mkdir()
        skills = hermes / "skills"
        (skills / "keep").mkdir(parents=True)
        sibling = hermes / "kanban.db"  # any non-skills path
        sibling.mkdir()

        with patch("tools.skills_sync.SKILLS_DIR", skills):
            for target in (Path("/"), hermes, sibling, skills):
                with pytest.raises(ValueError, match="refusing to rmtree"):
                    _rmtree_writable(target)

        assert (skills / "keep").exists()  # nothing was wiped
        assert sibling.exists()

    def test_allows_subdirectory_of_skills(self, tmp_path):
        """Any directory strictly under SKILLS_DIR is allowed."""
        from tools.skills_sync import _rmtree_writable

        skills = tmp_path / "skills"
        skills.mkdir()
        sub = skills / "category" / "old-skill"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text("# old")

        with patch("tools.skills_sync.SKILLS_DIR", skills):
            _rmtree_writable(sub)

        assert skills.exists()
        assert not sub.exists()


class TestExternalDirsIndexing:
    """Tests for external_dirs awareness in sync_skills (#28126)."""

    def _setup_bundled(self, tmp_path):
        """Create a fake bundled skills directory."""
        bundled = tmp_path / "bundled_skills"
        (bundled / "devops" / "clair-qa").mkdir(parents=True)
        (bundled / "devops" / "clair-qa" / "SKILL.md").write_text("# bundled clair")
        (bundled / "creative" / "ascii-art").mkdir(parents=True)
        (bundled / "creative" / "ascii-art" / "SKILL.md").write_text("# bundled ascii")
        return bundled

    def _setup_external(self, tmp_path):
        """Create a fake external skills directory."""
        ext_dir = tmp_path / "external_skills"
        (ext_dir / "devops" / "clair-qa").mkdir(parents=True)
        (ext_dir / "devops" / "clair-qa" / "SKILL.md").write_text("# external clair")
        (ext_dir / "devops" / "clair-qa" / "main.py").write_text("print('ext')")
        return ext_dir

    def _patches(self, bundled, skills_dir, manifest_file):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def _seed_snapshot_generation(
        self,
        skills_sync_module,
        fingerprint,
        scan_id,
        *,
        materialized_bytes=10,
    ):
        generation = (
            skills_sync_module._external_materialized_snapshot_dir()
            / fingerprint
            / scan_id
        )
        (generation / "root-0000").mkdir(parents=True)
        skills_sync_module._write_external_snapshot_complete_marker(
            generation,
            fingerprint=fingerprint,
            scan_id=scan_id,
            materialized_bytes=materialized_bytes,
        )
        return generation

    def test_shadowed_skill_skipped_and_not_manifested(self, tmp_path):
        """When an external dir provides the skill, sync must not write it
        locally — nor baseline it in the manifest.

        Recording bundled_hash for a deferred skill would later make the
        loader misclassify the external copy as a user-deleted bundled skill
        and poison update detection.
        """
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        ext_dir = self._setup_external(tmp_path)

        with self._patches(bundled, skills_dir, manifest_file):
            with patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=((str(ext_dir),), 10.0),
            ):
                result = sync_skills(quiet=True)
                manifest = _read_manifest()

        assert "clair-qa" in result["shadowed_by_external"]
        assert "clair-qa" not in result["copied"]
        assert "ascii-art" in result["copied"]
        assert not (skills_dir / "devops" / "clair-qa").exists()
        assert "clair-qa" not in manifest
        # The non-shadowed skill is still synced and baselined normally.
        assert "ascii-art" in manifest


    def test_no_external_dirs_unchanged(self, tmp_path):
        """Without external_dirs, all bundled skills should be copied normally."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file):
            with patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=((), 10.0),
            ):
                result = sync_skills(quiet=True)

        assert "clair-qa" in result["copied"]
        assert "ascii-art" in result["copied"]
        assert result["shadowed_by_external"] == []

    def test_spawned_scan_prunes_support_exclusions_and_directory_symlinks(
        self, tmp_path
    ):
        """The real child-process path indexes names without escaping packages."""
        from tools.skills_sync import _run_external_scan_subprocess

        external = tmp_path / "external"
        package = external / "team" / "folder-name"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: frontmatter-name\n---\n# Team skill\n",
            encoding="utf-8",
        )
        archived = package / "references" / "archived"
        archived.mkdir(parents=True)
        (archived / "SKILL.md").write_text(
            "---\nname: archived-name\n---\n",
            encoding="utf-8",
        )
        excluded = external / ".git" / "fake"
        excluded.mkdir(parents=True)
        (excluded / "SKILL.md").write_text(
            "---\nname: git-fake\n---\n",
            encoding="utf-8",
        )

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: symlink-escape\n---\n",
            encoding="utf-8",
        )
        symlink_created = True
        try:
            (external / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            symlink_created = False

        hermes_home = tmp_path / "hermes"
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            scan_result = _run_external_scan_subprocess((str(external),), 10.0)
        names = scan_result.names

        assert {"folder-name", "frontmatter-name"} <= names
        assert "archived-name" not in names
        assert "git-fake" not in names
        if symlink_created:
            assert "symlink-escape" not in names
        materialized = (
            hermes_home / "cache" / "external-skills-snapshots"
            / scan_result.materialized_roots[0]
        )
        assert (materialized / "team" / "folder-name" / "SKILL.md").is_file()
        assert (
            materialized / "team" / "folder-name" / "references" / "archived" / "SKILL.md"
        ).is_file()
        assert not (materialized / "linked").exists()

    def test_scan_rejects_symlink_in_configured_root_ancestor(self, tmp_path):
        import tools.skills_sync as skills_sync_module

        real_parent = tmp_path / "real-parent"
        external = real_parent / "external"
        external.mkdir(parents=True)
        (external / "SKILL.md").write_text("# external\n", encoding="utf-8")
        linked_parent = tmp_path / "linked-parent"
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")

        with pytest.raises(OSError, match="traverses a symlink"):
            skills_sync_module._scan_external_roots(
                (str(linked_parent / "external"),)
            )

    def test_descriptor_scan_rejects_directory_swapped_to_symlink(
        self, tmp_path, monkeypatch
    ):
        """A classification/open race must fail, never traverse the new target."""
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative directory traversal is unavailable")

        external = tmp_path / "external"
        child = external / "child"
        child.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: escaped\n---\n",
            encoding="utf-8",
        )
        original_entries = (
            skills_sync_module._bounded_external_directory_entries_fd
        )
        root_enumerated = False

        def swap_after_root_enumeration(directory_fd, budget):
            nonlocal root_enumerated
            entries = original_entries(directory_fd, budget)
            if not root_enumerated:
                root_enumerated = True
                child.rename(external / "child-before-race")
                child.symlink_to(outside, target_is_directory=True)
            return entries

        monkeypatch.setattr(
            skills_sync_module,
            "_bounded_external_directory_entries_fd",
            swap_after_root_enumeration,
        )

        with pytest.raises(OSError):
            skills_sync_module._scan_external_roots((str(external),))

    def test_runtime_without_descriptor_traversal_fails_closed(
        self, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        monkeypatch.setattr(
            skills_sync_module,
            "_external_fd_traversal_supported",
            lambda: False,
        )
        with patch.object(
            Path,
            "is_dir",
            side_effect=AssertionError("unsafe path fallback touched the root"),
        ):
            with pytest.raises(
                OSError,
                match="race-safe descriptor-relative.*unavailable",
            ):
                skills_sync_module._scan_external_roots(("/must-not-be-touched",))

    def test_scanner_state_write_refuses_symlink_target(self, tmp_path):
        import tools.skills_sync as skills_sync_module

        target = tmp_path / "must-not-change.json"
        target.write_text("sentinel\n", encoding="utf-8")
        state = tmp_path / "state.json"
        try:
            state.symlink_to(target)
        except OSError:
            pytest.skip("file symlinks are unavailable")

        with pytest.raises(OSError, match="state path is unsafe"):
            skills_sync_module._write_json_object_atomic(state, {"ok": True})

        assert target.read_text(encoding="utf-8") == "sentinel\n"

    def test_redirected_cache_blocks_lock_work_index_publish_and_cleanup(
        self,
        tmp_path,
    ):
        """A planted cache symlink is rejected before any target-side effect."""
        import tools.skills_sync as skills_sync_module

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        remote = tmp_path / "remote-cache"
        remote.mkdir()
        (remote / "external-skills-catalog.json").write_text(
            json.dumps(
                {
                    "version": skills_sync_module._EXTERNAL_CATALOG_VERSION,
                    "roots_fingerprint": "remote",
                    "names": ["must-not-be-read"],
                }
            ),
            encoding="utf-8",
        )
        remote_work = remote / ".external-scan-sentinel"
        remote_work.mkdir()
        (remote_work / "keep.txt").write_text("keep", encoding="utf-8")
        try:
            (hermes_home / "cache").symlink_to(remote, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")

        with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
            "tools.skills_sync.os.scandir",
            side_effect=AssertionError("scanner enumerated redirected cache"),
        ) as scandir:
            assert skills_sync_module._read_json_object(
                skills_sync_module._external_catalog_cache_path()
            ) is None
            assert skills_sync_module._try_acquire_external_scan_file_lock() is None
            with pytest.raises(OSError):
                skills_sync_module._create_external_scan_work_dir(
                    hermes_home / "cache"
                )
            with pytest.raises(OSError):
                skills_sync_module._list_external_snapshot_generations()
            with pytest.raises(OSError):
                skills_sync_module._write_json_object_atomic(
                    skills_sync_module._external_catalog_cache_path(),
                    {"ok": True},
                )
            skills_sync_module._safe_cleanup_external_scan_work_dir(
                str(hermes_home / "cache" / remote_work.name),
                str(hermes_home / "cache"),
            )

        scandir.assert_not_called()
        assert not (remote / "external-skills-scan.lock").exists()
        assert not list(remote.glob(".external-scan-*tmp*"))
        assert (remote_work / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert "must-not-be-read" in (
            remote / "external-skills-catalog.json"
        ).read_text(encoding="utf-8")

    def test_redirected_snapshot_root_blocks_publish_index_and_delete(self, tmp_path):
        """Nested cache redirects cannot publish, enumerate, or delete remotely."""
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative snapshot operations are unavailable")
        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "cache"
        cache.mkdir(parents=True)
        remote_snapshots = tmp_path / "remote-snapshots"
        generation_target = remote_snapshots / "fp" / "gen"
        generation_target.mkdir(parents=True)
        sentinel = generation_target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        snapshot_link = cache / "external-skills-snapshots"
        try:
            snapshot_link.symlink_to(remote_snapshots, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        staging = cache / ".external-scan-staging" / "materialized"
        staging.mkdir(parents=True)
        generation = skills_sync_module._ExternalSnapshotGeneration(
            snapshot_link / "fp" / "gen",
            1,
            1,
            complete=True,
        )

        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            with pytest.raises(OSError):
                skills_sync_module._list_external_snapshot_generations()
            with pytest.raises(OSError):
                skills_sync_module._publish_external_materialized_generation(
                    staging,
                    fingerprint="new-fingerprint",
                    scan_id="new-generation",
                )
            assert not skills_sync_module._remove_external_snapshot_generation(
                generation
            )

        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert not (remote_snapshots / "new-fingerprint").exists()

    def test_runtime_without_cache_dirfd_support_has_no_cache_side_effects(
        self,
        tmp_path,
    ):
        """A safe-looking cache still fails closed without handle-relative I/O."""
        import tools.skills_sync as skills_sync_module

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        cache = hermes_home / "cache"

        with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
            "tools.skills_sync._external_cache_dirfd_supported",
            return_value=False,
        ), patch(
            "tools.skills_sync.os.open",
            side_effect=AssertionError("unsupported runtime opened cache state"),
        ) as open_file, patch(
            "tools.skills_sync.os.mkdir",
            side_effect=AssertionError("unsupported runtime created cache state"),
        ) as mkdir, patch(
            "tools.skills_sync.os.scandir",
            side_effect=AssertionError("unsupported runtime scanned cache state"),
        ) as scandir, patch(
            "tools.skills_sync.shutil.rmtree",
            side_effect=AssertionError("unsupported runtime deleted cache state"),
        ) as remove:
            with pytest.raises(
                OSError,
                match="race-safe descriptor-relative scanner cache access is unavailable",
            ):
                skills_sync_module._create_external_scan_work_dir(cache)

        open_file.assert_not_called()
        mkdir.assert_not_called()
        scandir.assert_not_called()
        remove.assert_not_called()
        assert not cache.exists()

    def test_materialization_stays_on_open_cache_after_lexical_parent_swap(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A cache-path swap after safe open cannot redirect a copied file."""
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_cache_dirfd_supported():
            pytest.skip("descriptor-relative cache operations are unavailable")

        external = tmp_path / "external"
        external.mkdir()
        (external / "SKILL.md").write_text(
            "---\nname: pinned-cache-copy\n---\n",
            encoding="utf-8",
        )
        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "cache"
        remote = tmp_path / "remote-cache"
        remote.mkdir()
        detached = hermes_home / "cache-detached"

        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            work_dir = skills_sync_module._create_external_scan_work_dir(cache)
            materialized_root = work_dir / "materialized"
            package_destination = materialized_root / "root-0000"
            original_open = skills_sync_module._open_external_cache_directory
            target_opens = 0
            swapped = False

            def swap_after_safe_open(path, *, create=False, hermes_home=None):
                nonlocal target_opens, swapped
                descriptor = original_open(
                    path,
                    create=create,
                    hermes_home=hermes_home,
                )
                if Path(path) == package_destination:
                    target_opens += 1
                    if target_opens == 2:
                        cache.rename(detached)
                        cache.symlink_to(remote, target_is_directory=True)
                        swapped = True
                return descriptor

            monkeypatch.setattr(
                skills_sync_module,
                "_open_external_cache_directory",
                swap_after_safe_open,
            )
            try:
                names = skills_sync_module._scan_external_roots_fd(
                    (str(external),),
                    materialized_root,
                )
            finally:
                if cache.is_symlink():
                    cache.unlink()
                    detached.rename(cache)

        assert swapped
        assert "pinned-cache-copy" in names
        assert (package_destination / "SKILL.md").is_file()
        assert list(remote.iterdir()) == []

    def test_windows_reparse_cache_parent_blocks_all_path_fallbacks(self, tmp_path):
        """Python 3.11 junction metadata fails before path-based cache I/O."""
        import tools.skills_sync as skills_sync_module

        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "cache"
        cache.mkdir(parents=True)
        remote_work = cache / ".external-scan-sentinel"
        remote_work.mkdir()
        (remote_work / "keep.txt").write_text("keep", encoding="utf-8")
        real_stat = Path.stat
        reparse_metadata = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        def mocked_stat(path, *args, **kwargs):
            if Path(path) == cache and kwargs.get("follow_symlinks") is False:
                return reparse_metadata
            return real_stat(path, *args, **kwargs)

        with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch.object(
            Path,
            "stat",
            mocked_stat,
        ), patch(
            "tools.skills_sync._external_cache_dirfd_supported",
            return_value=False,
        ), patch(
            "tools.skills_sync.os.open",
            side_effect=AssertionError("fallback opened through junction"),
        ) as open_file, patch(
            "tools.skills_sync.os.scandir",
            side_effect=AssertionError("fallback scanned through junction"),
        ) as scandir, patch(
            "tools.skills_sync.tempfile.mkdtemp",
            side_effect=AssertionError("fallback created remote work"),
        ) as mkdtemp, patch(
            "tools.skills_sync.shutil.rmtree",
            side_effect=AssertionError("fallback deleted remote work"),
        ) as remove:
            assert skills_sync_module._try_acquire_external_scan_file_lock() is None
            with pytest.raises(OSError, match="ancestry is redirected"):
                skills_sync_module._create_external_scan_work_dir(cache)
            with pytest.raises(OSError, match="ancestry is redirected"):
                skills_sync_module._list_external_snapshot_generations()
            with pytest.raises(OSError, match="ancestry is redirected"):
                skills_sync_module._write_json_object_atomic(
                    skills_sync_module._external_catalog_cache_path(),
                    {"ok": True},
                )
            skills_sync_module._safe_cleanup_external_scan_work_dir(
                str(remote_work),
                str(cache),
            )

        open_file.assert_not_called()
        scandir.assert_not_called()
        mkdtemp.assert_not_called()
        remove.assert_not_called()
        assert (remote_work / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_snapshot_retention_bounds_complete_generation_count_and_bytes(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative snapshot GC is unavailable")

        hermes_home = tmp_path / "hermes"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            generations = [
                self._seed_snapshot_generation(
                    skills_sync_module,
                    fingerprint,
                    f"{index}-aaaaaaaaaaaaaaaa",
                    materialized_bytes=10,
                )
                for index in range(4)
            ]
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"external"},
                (f"{fingerprint}/3-aaaaaaaaaaaaaaaa/root-0000",),
            )
            monkeypatch.setattr(
                skills_sync_module, "_MAX_EXTERNAL_SNAPSHOT_GENERATIONS", 3
            )
            monkeypatch.setattr(
                skills_sync_module, "_MAX_EXTERNAL_SNAPSHOT_TOTAL_BYTES", 25
            )

            assert skills_sync_module._enforce_external_snapshot_retention()
            remaining = {
                item.path
                for item in skills_sync_module._list_external_snapshot_generations()
            }

        assert remaining == set(generations[-2:])
        assert generations[-1].is_dir()

    def test_snapshot_reader_lease_survives_pointer_swap_until_reader_releases(
        self, tmp_path, monkeypatch
    ):
        from agent import skill_utils
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative snapshot GC is unavailable")

        hermes_home = tmp_path / "hermes"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            oldest = self._seed_snapshot_generation(
                skills_sync_module,
                fingerprint,
                "1-aaaaaaaaaaaaaaaa",
            )
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"external"},
                (f"{fingerprint}/1-aaaaaaaaaaaaaaaa/root-0000",),
            )
            snapshot = skill_utils.get_gateway_external_skills_snapshot(
                roots,
                hermes_home=hermes_home,
            )
            assert snapshot is not None

            previous = self._seed_snapshot_generation(
                skills_sync_module,
                fingerprint,
                "2-aaaaaaaaaaaaaaaa",
            )
            current = self._seed_snapshot_generation(
                skills_sync_module,
                fingerprint,
                "3-aaaaaaaaaaaaaaaa",
            )
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"external"},
                (f"{fingerprint}/3-aaaaaaaaaaaaaaaa/root-0000",),
            )
            monkeypatch.setattr(
                skills_sync_module, "_MAX_EXTERNAL_SNAPSHOT_GENERATIONS", 2
            )
            monkeypatch.setattr(
                skills_sync_module,
                "_MAX_EXTERNAL_SNAPSHOT_TOTAL_BYTES",
                2 * skills_sync_module._MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES,
            )

            assert not skills_sync_module._enforce_external_snapshot_retention()
            assert oldest.is_dir()
            assert previous.is_dir()
            assert current.is_dir()

            del snapshot
            gc.collect()
            assert skills_sync_module._enforce_external_snapshot_retention()

        assert not oldest.exists()
        assert previous.is_dir()
        assert current.is_dir()

    def test_gateway_lease_cache_swap_cannot_create_remote_lock(self, tmp_path):
        """The gateway descends from HERMES_HOME before creating a read lease."""
        from agent import skill_utils
        import tools.skills_sync as skills_sync_module

        if not skill_utils._gateway_cache_dirfd_supported():
            pytest.skip("descriptor-relative gateway cache access is unavailable")

        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "cache"
        detached = hermes_home / "cache-detached"
        remote = tmp_path / "remote-cache"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        generation_name = "1-aaaaaaaaaaaaaaaa"

        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            self._seed_snapshot_generation(
                skills_sync_module,
                fingerprint,
                generation_name,
            )
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"external"},
                (f"{fingerprint}/{generation_name}/root-0000",),
            )
        shutil.copytree(cache, remote)

        original_acquire = skill_utils._acquire_gateway_external_snapshot_lease
        swapped = False

        def swap_before_lease(generation, *, hermes_home):
            nonlocal swapped
            cache.rename(detached)
            cache.symlink_to(remote, target_is_directory=True)
            swapped = True
            return original_acquire(
                generation,
                hermes_home=hermes_home,
            )

        try:
            with patch.object(
                skill_utils,
                "_acquire_gateway_external_snapshot_lease",
                swap_before_lease,
            ):
                snapshot = skill_utils.get_gateway_external_skills_snapshot(
                    roots,
                    hermes_home=hermes_home,
                )
        finally:
            if cache.is_symlink():
                cache.unlink()
                detached.rename(cache)

        assert swapped
        assert snapshot is None
        remote_lease = (
            remote
            / "external-skills-snapshots"
            / fingerprint
            / f".{generation_name}{skill_utils.EXTERNAL_SKILLS_SNAPSHOT_READ_LOCK_SUFFIX}"
        )
        assert not remote_lease.exists()

    @pytest.mark.parametrize("minor", [12, 13])
    def test_gateway_snapshot_reader_fails_closed_on_newer_pathlib(
        self,
        tmp_path,
        minor,
    ):
        """Python 3.12/3.13 must not use the 3.11 private Path capability."""
        from agent import skill_utils

        roots = (str(tmp_path / "external-never-touched"),)
        with patch.object(
            skill_utils.sys,
            "version_info",
            (3, minor, 0),
        ), patch.object(
            skill_utils.os,
            "open",
        ) as raw_open:
            assert not skill_utils._gateway_cache_dirfd_supported()
            assert (
                skill_utils.get_gateway_external_skills_snapshot(
                    roots,
                    hermes_home=tmp_path / "hermes",
                )
                is None
            )
        raw_open.assert_not_called()

    def test_gateway_snapshot_reader_fails_closed_on_unknown_pathlib_runtime(
        self,
    ):
        """The private 3.11 capability is restricted to its tested runtime."""
        from agent import skill_utils

        with patch.object(
            skill_utils.sys,
            "implementation",
            SimpleNamespace(name="other-python"),
        ):
            assert not skill_utils._gateway_cache_dirfd_supported()

    def test_gateway_yielded_skill_file_refuses_post_scan_cache_swap(
        self,
        tmp_path,
    ):
        """The consumer's final read remains anchored after iterator yield."""
        from agent import skill_utils
        import tools.skills_sync as skills_sync_module

        if not skill_utils._gateway_cache_dirfd_supported():
            pytest.skip("descriptor-relative gateway cache access is unavailable")

        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "cache"
        detached = hermes_home / "cache-detached"
        remote = tmp_path / "remote-cache"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        generation_name = "1-bbbbbbbbbbbbbbbb"
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            generation = self._seed_snapshot_generation(
                skills_sync_module,
                fingerprint,
                generation_name,
            )
            local_skill = generation / "root-0000" / "example" / "SKILL.md"
            local_skill.parent.mkdir()
            local_skill.write_text("LOCAL\n", encoding="utf-8")
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"example"},
                (f"{fingerprint}/{generation_name}/root-0000",),
            )
        snapshot = skill_utils.get_gateway_external_skills_snapshot(
            roots,
            hermes_home=hermes_home,
        )
        assert snapshot is not None
        root = snapshot[1][0]
        with patch.dict(os.environ, {"_HERMES_GATEWAY": "1"}):
            matched = next(skill_utils.iter_skill_index_files(root, "SKILL.md"))
        derived = root / "example" / "SKILL.md"
        assert derived.read_text(encoding="utf-8") == "LOCAL\n"
        assert [path.relative_to(root) for path in root.rglob("SKILL.md")] == [
            Path("example/SKILL.md")
        ]

        shutil.copytree(cache, remote)
        remote_skill = (
            remote
            / "external-skills-snapshots"
            / fingerprint
            / generation_name
            / "root-0000"
            / "example"
            / "SKILL.md"
        )
        remote_skill.write_text("REMOTE\n", encoding="utf-8")
        (remote_skill.parent / "REMOTE_ONLY.md").write_text(
            "REMOTE ONLY\n",
            encoding="utf-8",
        )
        cache.rename(detached)
        cache.symlink_to(remote, target_is_directory=True)
        try:
            with pytest.raises(OSError):
                matched.read_text(encoding="utf-8")
            with pytest.raises(OSError):
                derived.read_text(encoding="utf-8")
            assert list(root.rglob("REMOTE_ONLY.md")) == []
            assert not root.exists()
            with pytest.raises(OSError, match="read-only"):
                (root / "example" / "REMOTE_ONLY.md").unlink()
            with pytest.raises(OSError, match="read-only"):
                (root / "remote-created").mkdir()
            with pytest.raises(OSError, match="read-only"):
                (root / "example" / "REMOTE_ONLY.md").write_text(
                    "changed\n",
                    encoding="utf-8",
                )
        finally:
            cache.unlink()
            detached.rename(cache)

        assert matched.read_text(encoding="utf-8") == "LOCAL\n"
        assert derived.read_text(encoding="utf-8") == "LOCAL\n"
        assert remote_skill.read_text(encoding="utf-8") == "REMOTE\n"
        assert (remote_skill.parent / "REMOTE_ONLY.md").read_text(
            encoding="utf-8"
        ) == "REMOTE ONLY\n"
        assert not (remote / "remote-created").exists()

        from tools import skills_tool

        with patch.object(
            skills_tool,
            "_skills_dir",
            return_value=tmp_path / "empty-local-skills",
        ), patch(
            "agent.skill_utils.get_project_skills_dirs",
            return_value=[],
        ), patch(
            "agent.skill_utils.get_external_skills_dirs",
            return_value=[root],
        ), patch.dict(
            os.environ,
            {"_HERMES_GATEWAY": "1"},
        ):
            loaded = json.loads(skills_tool.skill_view("example", preprocess=False))

        assert loaded["success"] is True
        assert loaded["content"] == "LOCAL\n"

        original_guarded_stat = type(root).stat
        signature_swapped = False

        def swap_after_signature_stat(path, *, follow_symlinks=True):
            nonlocal signature_swapped
            metadata = original_guarded_stat(
                path,
                follow_symlinks=follow_symlinks,
            )
            if str(path) == str(root) and not signature_swapped:
                cache.rename(detached)
                cache.symlink_to(remote, target_is_directory=True)
                signature_swapped = True
            return metadata

        try:
            with patch.object(
                type(root),
                "stat",
                swap_after_signature_stat,
            ):
                signature = skills_tool._skills_scan_signature([root], set())
        finally:
            if cache.is_symlink():
                cache.unlink()
                detached.rename(cache)

        assert signature_swapped
        assert signature[0][0][0] == str(root)
        assert (remote_skill.parent / "REMOTE_ONLY.md").is_file()

    def test_snapshot_gc_rereads_pointer_before_quarantine(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative snapshot GC is unavailable")

        hermes_home = tmp_path / "hermes"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            oldest = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "1-aaaaaaaaaaaaaaaa"
            )
            previous = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "2-aaaaaaaaaaaaaaaa"
            )
            current = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "3-aaaaaaaaaaaaaaaa"
            )
            monkeypatch.setattr(
                skills_sync_module, "_MAX_EXTERNAL_SNAPSHOT_GENERATIONS", 2
            )
            current_reads = [current, oldest]
            monkeypatch.setattr(
                skills_sync_module,
                "_current_external_snapshot_generation",
                lambda: current_reads.pop(0) if current_reads else oldest,
            )

            assert not skills_sync_module._enforce_external_snapshot_retention()

        assert oldest.is_dir()
        assert previous.is_dir()
        assert current.is_dir()

    def test_snapshot_cleanup_failure_never_invalidates_current_or_lkg(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        if not skills_sync_module._external_fd_traversal_supported():
            pytest.skip("descriptor-relative snapshot GC is unavailable")

        hermes_home = tmp_path / "hermes"
        roots = (str(tmp_path / "external"),)
        fingerprint = skills_sync_module._external_catalog_fingerprint(roots)
        with patch("tools.skills_sync.HERMES_HOME", hermes_home):
            oldest = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "1-aaaaaaaaaaaaaaaa"
            )
            previous = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "2-aaaaaaaaaaaaaaaa"
            )
            current = self._seed_snapshot_generation(
                skills_sync_module, fingerprint, "3-aaaaaaaaaaaaaaaa"
            )
            skills_sync_module._publish_external_catalog_snapshot(
                fingerprint,
                roots,
                {"external"},
                (f"{fingerprint}/3-aaaaaaaaaaaaaaaa/root-0000",),
            )
            catalog_before = (
                skills_sync_module._external_catalog_cache_path().read_bytes()
            )
            monkeypatch.setattr(
                skills_sync_module, "_MAX_EXTERNAL_SNAPSHOT_GENERATIONS", 2
            )
            with patch(
                "tools.skills_sync._remove_external_cache_tree",
                side_effect=OSError("simulated cleanup failure"),
            ):
                assert not skills_sync_module._enforce_external_snapshot_retention()

            assert (
                skills_sync_module._external_catalog_cache_path().read_bytes()
                == catalog_before
            )
            assert current.is_dir()
            assert previous.is_dir()
            assert not oldest.exists()
            quarantined = list(
                oldest.parent.glob(
                    f"{skills_sync_module._EXTERNAL_SNAPSHOT_GC_PREFIX}*"
                )
            )
            assert len(quarantined) == 1

            assert skills_sync_module._enforce_external_snapshot_retention()
            assert not quarantined[0].exists()

        assert current.is_dir()
        assert previous.is_dir()

    def test_traversal_limit_fails_the_catalog_instead_of_publishing_partial_data(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        (external / "one" / "two").mkdir(parents=True)
        (external / "one" / "SKILL.md").write_text("# one\n", encoding="utf-8")
        monkeypatch.setattr(
            skills_sync_module, "_MAX_EXTERNAL_SCAN_DIRECTORIES", 1
        )

        with pytest.raises(OSError, match="directory safety limit"):
            skills_sync_module._scan_external_roots((str(external),))

    def test_entry_limit_counts_regular_files_not_only_directories(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        external.mkdir()
        (external / "one.txt").write_text("1", encoding="utf-8")
        (external / "two.txt").write_text("2", encoding="utf-8")
        monkeypatch.setattr(skills_sync_module, "_MAX_EXTERNAL_SCAN_ENTRIES", 1)

        with pytest.raises(OSError, match="entry safety limit"):
            skills_sync_module._scan_external_roots((str(external),))

    def test_file_limit_counts_every_regular_file(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        external.mkdir()
        (external / "one.txt").write_text("1", encoding="utf-8")
        (external / "two.txt").write_text("2", encoding="utf-8")
        monkeypatch.setattr(skills_sync_module, "_MAX_EXTERNAL_SCAN_FILES", 1)

        with pytest.raises(OSError, match="file safety limit"):
            skills_sync_module._scan_external_roots((str(external),))

    def test_unavailable_root_without_snapshot_defers_without_local_mutation(
        self, tmp_path
    ):
        bundled = self._setup_bundled(tmp_path)
        hermes_home = tmp_path / "hermes"
        skills_dir = hermes_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"
        missing = tmp_path / "missing-external"

        with self._patches(bundled, skills_dir, manifest_file):
            with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=((str(missing),), 10.0),
            ):
                with pytest.raises(
                    ExternalSkillIndexUnavailable,
                    match="configured external skill root is unavailable",
                ):
                    sync_skills(quiet=True)

        assert not skills_dir.exists()
        assert not manifest_file.exists()

    def test_gateway_mode_never_launches_external_scan(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "hung-external"
        hermes_home = tmp_path / "hermes"
        settings = ((str(external),), 10.0)

        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
            "tools.skills_sync._configured_external_scan_settings",
            return_value=settings,
        ), patch(
            "tools.skills_sync._run_external_scan_subprocess",
            side_effect=AssertionError("gateway launched an external scan"),
        ) as run_scan:
            with pytest.raises(
                ExternalSkillIndexUnavailable,
                match="no validated local external skill snapshot",
            ):
                skills_sync_module._build_external_skill_index()

        run_scan.assert_not_called()

    def test_failed_scan_reuses_atomic_nonempty_snapshot_and_enters_backoff(
        self, tmp_path
    ):
        import tools.skills_sync as skills_sync_module

        bundled = self._setup_bundled(tmp_path)
        external = self._setup_external(tmp_path)
        hermes_home = tmp_path / "hermes"
        skills_dir = hermes_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"
        settings = ((str(external),), 10.0)

        with self._patches(bundled, skills_dir, manifest_file):
            with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=settings,
            ):
                first = sync_skills(quiet=True)
                snapshot_path = (
                    hermes_home / "cache" / "external-skills-catalog.json"
                )
                snapshot_before = snapshot_path.read_text(encoding="utf-8")
                generation_before = (
                    skills_sync_module._current_external_snapshot_generation()
                )
                assert generation_before is not None
                local_before = {
                    str(path.relative_to(skills_dir)): path.read_bytes()
                    for path in skills_dir.rglob("*")
                    if path.is_file()
                }

                shutil.rmtree(external)
                with pytest.raises(
                    ExternalSkillIndexUnavailable,
                    match="configured external skill root is unavailable",
                ):
                    sync_skills(quiet=True)

                # Backoff suppresses a third child launch while preserving the
                # same last-known-good pointer and the complete local tree.
                with patch(
                    "tools.skills_sync._run_external_scan_subprocess"
                ) as run_scan:
                    with pytest.raises(
                        ExternalSkillIndexUnavailable,
                        match="retry deferred",
                    ):
                        sync_skills(quiet=True)
                    run_scan.assert_not_called()

                snapshot_after = snapshot_path.read_text(encoding="utf-8")
                generation_after = (
                    skills_sync_module._current_external_snapshot_generation()
                )
                local_after = {
                    str(path.relative_to(skills_dir)): path.read_bytes()
                    for path in skills_dir.rglob("*")
                    if path.is_file()
                }

        assert first["external_scan_status"] == "fresh"
        assert snapshot_after == snapshot_before
        assert generation_after == generation_before
        assert generation_after.is_dir()
        assert local_after == local_before
        assert not (skills_dir / "devops" / "clair-qa").exists()

    def test_persisted_backoff_is_hard_capped_after_wall_clock_rollback(
        self, tmp_path
    ):
        import tools.skills_sync as skills_sync_module

        fingerprint = "a" * 64
        with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"), patch(
            "tools.skills_sync.time.time", return_value=100.0
        ):
            skills_sync_module._write_json_object_atomic(
                skills_sync_module._external_scan_backoff_path(),
                {
                    "roots_fingerprint": fingerprint,
                    "retry_after": 100_000.0,
                },
            )
            assert skills_sync_module._active_external_scan_backoff(
                fingerprint
            ) == skills_sync_module._EXTERNAL_SCAN_BACKOFF_MAX_SECONDS

    def test_nonempty_to_empty_requires_two_independent_successful_scans(
        self, tmp_path
    ):
        bundled = self._setup_bundled(tmp_path)
        external = self._setup_external(tmp_path)
        hermes_home = tmp_path / "hermes"
        skills_dir = hermes_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"
        settings = ((str(external),), 10.0)

        with self._patches(bundled, skills_dir, manifest_file):
            with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=settings,
            ):
                sync_skills(quiet=True)
                snapshot_path = hermes_home / "cache" / "external-skills-catalog.json"
                first_snapshot = snapshot_path.read_bytes()
                shutil.rmtree(external)
                external.mkdir()

                with pytest.raises(
                    ExternalSkillIndexUnavailable,
                    match="second independent successful scan",
                ):
                    sync_skills(quiet=True)
                assert snapshot_path.read_bytes() == first_snapshot
                assert not (skills_dir / "devops" / "clair-qa").exists()

                confirmed = sync_skills(quiet=True)
                published = json.loads(snapshot_path.read_text(encoding="utf-8"))

        assert confirmed["external_scan_status"] == "fresh"
        assert published["names"] == []
        assert "clair-qa" in confirmed["copied"]

    def test_external_ownership_is_checked_before_rename_recovery(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        external = self._setup_external(tmp_path)
        hermes_home = tmp_path / "hermes"
        skills_dir = hermes_home / "skills"
        skills_dir.mkdir(parents=True)
        manifest_file = skills_dir / ".bundled_manifest"
        manifest_file.write_text("clair-qa:old-origin\n", encoding="utf-8")

        with self._patches(bundled, skills_dir, manifest_file):
            with patch("tools.skills_sync.HERMES_HOME", hermes_home), patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=((str(external),), 10.0),
            ), patch(
                "tools.skills_sync._recover_renamed_skill",
                side_effect=AssertionError("external ownership checked too late"),
            ) as recover:
                result = sync_skills(quiet=True)

        recover.assert_not_called()
        assert "clair-qa" in result["shadowed_by_external"]

    def test_large_catalog_exceeding_pipe_buffer_succeeds_via_result_file(
        self, tmp_path
    ):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        for index in range(900):
            name = f"skill-{index:04d}-" + ("x" * 80)
            package = external / name
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n",
                encoding="utf-8",
            )

        with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"):
            result = skills_sync_module._run_external_scan_subprocess(
                (str(external),), 10.0
            )

        encoded = json.dumps(sorted(result.names)).encode("utf-8")
        assert len(encoded) > 64 * 1024
        assert len(result.names) == 900

    @pytest.mark.parametrize(
        ("limit_name", "limit", "names", "error"),
        [
            ("EXTERNAL_SKILLS_MAX_CATALOG_NAMES", 1, ["one", "two"], "invalid catalog"),
            ("EXTERNAL_SKILLS_MAX_NAME_BYTES", 3, ["four"], "invalid catalog"),
        ],
    )
    def test_result_file_caps_items_and_name_bytes(
        self, tmp_path, monkeypatch, limit_name, limit, names, error
    ):
        import tools.skills_sync as skills_sync_module

        result_path = tmp_path / "result.json"
        result_path.write_text(
            json.dumps(
                {"ok": True, "names": names, "materialized_bytes": 0}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(skills_sync_module, limit_name, limit)

        with pytest.raises(RuntimeError, match=error):
            skills_sync_module._read_external_scan_result(result_path)

    def test_result_file_caps_total_bytes(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        result_path = tmp_path / "result.json"
        result_path.write_bytes(b"x" * 17)
        monkeypatch.setattr(skills_sync_module, "EXTERNAL_SKILLS_MAX_CATALOG_BYTES", 16)

        with pytest.raises(RuntimeError, match="oversized result"):
            skills_sync_module._read_external_scan_result(result_path)

    def test_timeout_escalates_from_terminate_to_kill(self):
        from tools.skills_sync import _terminate_external_scan_process

        class StubbornProcess:
            def __init__(self):
                self.alive = True
                self.calls = []

            def terminate(self):
                self.calls.append("terminate")

            def join(self, timeout):
                self.calls.append(("join", timeout))

            def is_alive(self):
                return self.alive

            def kill(self):
                self.calls.append("kill")
                self.alive = False

        process = StubbornProcess()

        assert _terminate_external_scan_process(process) is True
        assert process.calls[0] == "terminate"
        assert process.calls[2] == "kill"
        assert process.calls[-1][0] == "join"

    def test_subprocess_timeout_invokes_bounded_termination(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        class HungProcess:
            exitcode = None
            pid = 424242

            def start(self):
                pass

            def join(self, timeout):
                pass

            def is_alive(self):
                return True

            def close(self):
                raise ValueError("still running")

        process = HungProcess()

        class Context:
            def Process(self, **kwargs):
                assert kwargs["daemon"] is True
                return process

        monkeypatch.setattr(
            skills_sync_module.multiprocessing,
            "get_context",
            lambda method: Context(),
        )
        terminated = []
        monkeypatch.setattr(
            skills_sync_module,
            "_terminate_external_scan_process",
            lambda candidate, **kwargs: terminated.append(candidate) or True,
        )

        with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"):
            with pytest.raises(TimeoutError, match="exceeded 10s"):
                skills_sync_module._run_external_scan_subprocess(
                    ("/never-touched",), 10.0
                )

        assert terminated == [process]

    def test_unreaped_child_lease_refuses_a_second_scan(self, tmp_path, monkeypatch):
        import tools.skills_sync as skills_sync_module

        class HungProcess:
            exitcode = None
            pid = os.getpid()

            def start(self):
                pass

            def join(self, timeout):
                pass

            def is_alive(self):
                return True

            def close(self):
                raise ValueError("still running")

        process = HungProcess()

        class Context:
            def Process(self, **kwargs):
                return process

        monkeypatch.setattr(
            skills_sync_module.multiprocessing,
            "get_context",
            lambda method: Context(),
        )
        monkeypatch.setattr(
            skills_sync_module,
            "_terminate_external_scan_process",
            lambda candidate, **kwargs: False,
        )
        hermes_home = tmp_path / "hermes"
        settings = ((str(tmp_path / "external"),), 1.0)
        try:
            with patch("tools.skills_sync.HERMES_HOME", hermes_home):
                with pytest.raises(TimeoutError, match="did not reap"):
                    skills_sync_module._run_external_scan_subprocess(*settings)
                with patch(
                    "tools.skills_sync._configured_external_scan_settings",
                    return_value=settings,
                ), patch(
                    "tools.skills_sync._run_external_scan_subprocess"
                ) as second_scan:
                    with pytest.raises(
                        ExternalSkillIndexUnavailable,
                        match="still alive",
                    ):
                        skills_sync_module._build_external_skill_index()
                    second_scan.assert_not_called()
        finally:
            skills_sync_module._EXTERNAL_SCAN_ORPHAN_PIDS.clear()
            skills_sync_module._EXTERNAL_SCAN_ORPHAN_PROCESSES.clear()

    def test_late_child_exit_is_reaped_before_orphan_lease_clears(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        class ExitedProcess:
            def __init__(self):
                self.joined = False
                self.closed = False

            def join(self, timeout):
                assert timeout == 0.0
                self.joined = True

            def is_alive(self):
                return False

            def close(self):
                self.closed = True

        process = ExitedProcess()
        pid = 987654321
        work_dir = tmp_path / "hermes" / "cache" / ".external-scan-late"
        work_dir.mkdir(parents=True)
        cleaned = []
        monkeypatch.setattr(
            skills_sync_module,
            "_schedule_external_scan_work_dir_cleanup",
            lambda path: cleaned.append(path),
        )
        try:
            with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"):
                skills_sync_module._record_external_scan_orphan(
                    pid,
                    work_dir,
                    process=process,
                )
                assert skills_sync_module._active_external_scan_orphan() == ""
                assert not skills_sync_module._external_scan_orphan_path().exists()
        finally:
            skills_sync_module._EXTERNAL_SCAN_ORPHAN_PIDS.clear()
            skills_sync_module._EXTERNAL_SCAN_ORPHAN_PROCESSES.clear()

        assert process.joined is True
        assert process.closed is True
        assert cleaned == [str(work_dir)]

    def test_in_process_single_flight_does_not_launch_a_second_scan(
        self, tmp_path
    ):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        external.mkdir()
        settings = ((str(external),), 10.0)
        acquired = skills_sync_module._EXTERNAL_SCAN_THREAD_LOCK.acquire(
            blocking=False
        )
        assert acquired is True
        try:
            with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"), patch(
                "tools.skills_sync._configured_external_scan_settings",
                return_value=settings,
            ), patch("tools.skills_sync._run_external_scan_subprocess") as run_scan:
                with pytest.raises(
                    skills_sync_module.ExternalSkillIndexUnavailable,
                    match="already in progress",
                ):
                    skills_sync_module._build_external_skill_index()
                run_scan.assert_not_called()
        finally:
            skills_sync_module._EXTERNAL_SCAN_THREAD_LOCK.release()

    def test_profile_lock_does_not_launch_a_second_process_scan(
        self, tmp_path, monkeypatch
    ):
        import tools.skills_sync as skills_sync_module

        external = tmp_path / "external"
        external.mkdir()
        settings = ((str(external),), 10.0)
        with patch("tools.skills_sync.HERMES_HOME", tmp_path / "hermes"), patch(
            "tools.skills_sync._configured_external_scan_settings",
            return_value=settings,
        ), patch(
            "tools.skills_sync._try_acquire_external_scan_file_lock",
            return_value=None,
        ), patch("tools.skills_sync._run_external_scan_subprocess") as run_scan:
            with pytest.raises(
                skills_sync_module.ExternalSkillIndexUnavailable,
                match="another process owns",
            ):
                skills_sync_module._build_external_skill_index()

        run_scan.assert_not_called()

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(-5, 1.0), (10, 10.0), (500, 10.0), ("invalid", 10.0)],
    )
    def test_scan_timeout_config_is_bounded(self, configured, expected):
        from tools.skills_sync import _configured_external_scan_settings

        with patch(
            "agent.skill_utils._load_raw_config",
            return_value={
                "skills": {
                    "external_dirs": ["shared-skills"],
                    "external_scan_timeout_seconds": configured,
                }
            },
        ):
            roots, timeout = _configured_external_scan_settings()

        assert len(roots) == 1
        assert timeout == expected


class TestRenamedBundledSkillRecovery:
    """Upstream renames/recategorizations must not strand the user's copy.

    ``sync_skills()`` keys the manifest by frontmatter *name*, but computes the
    destination from the bundled *path*. When upstream moves a skill, the name
    still matches while the new dest does not exist yet — the pre-fix code fell
    into its "in manifest but not on disk" branch, misread the skill as
    user-deleted, and left the old directory stranded at the stale path forever.
    """

    def _patches(self, bundled, skills_dir, manifest_file):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(
            patch(
                "tools.skills_sync._get_optional_dir",
                return_value=bundled.parent / "optional-skills",
            )
        )
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def _skill(self, root, rel, body="# Body\n", name="moved-skill"):
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}")
        return d

    def test_rename_relocates_unmodified_copy(self, tmp_path):
        """The stale copy is moved to the new path and updated, not stranded."""
        bundled = tmp_path / "bundled"
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # User's copy sits at the OLD path, byte-identical to what sync wrote.
        old = self._skill(skills_dir, "oldcat/moved-skill")
        origin_hash = _dir_hash(old)
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(f"moved-skill:{origin_hash}\n")

        # Upstream moved it to a NEW category and changed the content.
        self._skill(bundled, "newcat/moved-skill", body="# Updated upstream\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            # Manifest now tracks the current bundled hash (read inside the
            # patch context — MANIFEST_FILE is a module global).
            recorded = _read_manifest()["moved-skill"]

        new = skills_dir / "newcat" / "moved-skill"
        assert new.exists(), "renamed skill was not relocated to the new path"
        assert not old.exists(), "stale copy left behind — would shadow forever"
        assert "moved-skill" in result["relocated"]
        # Having been relocated, it then takes the normal update path.
        assert "moved-skill" in result["updated"]
        assert "Updated upstream" in (new / "SKILL.md").read_text()
        # Future syncs can now detect further upstream changes.
        assert recorded == _dir_hash(bundled / "newcat" / "moved-skill")

    def test_rename_leaves_user_modified_and_hub_owned_copies_alone(self, tmp_path):
        """A user-edited copy is never moved or overwritten, and a hub-owned
        path is never relocated — the hub lock owns it."""
        bundled = tmp_path / "bundled"
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        edited = self._skill(skills_dir, "oldcat/moved-skill")
        edited_hash = _dir_hash(edited)
        # User then edits their copy, so it no longer matches the origin hash.
        (edited / "SKILL.md").write_text("---\nname: moved-skill\n---\n# MY EDITS\n")

        hub = self._skill(skills_dir, "oldcat/hub-skill", name="hub-skill")
        hub_hash = _dir_hash(hub)

        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(
            f"moved-skill:{edited_hash}\nhub-skill:{hub_hash}\n"
        )
        lock = skills_dir / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps(
                {
                    "version": 1,
                    "installed": {
                        "hub-skill": {"install_path": "oldcat/hub-skill"}
                    },
                }
            )
        )

        # Upstream moved both into a new category.
        self._skill(bundled, "newcat/moved-skill", body="# Updated upstream\n")
        self._skill(
            bundled, "newcat/hub-skill", body="# Updated upstream\n", name="hub-skill"
        )

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert edited.exists(), "user's modified copy must not be moved"
        assert "MY EDITS" in (edited / "SKILL.md").read_text()
        assert hub.exists(), "hub-installed skill must not be relocated"
        assert "moved-skill" not in result.get("relocated", [])
        assert "hub-skill" not in result.get("relocated", [])

    def test_genuine_user_deletion_still_respected(self, tmp_path):
        """No copy anywhere on disk = a real deletion; must not be resurrected."""
        bundled = tmp_path / "bundled"
        skills_dir = tmp_path / "user_skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = skills_dir / ".bundled_manifest"
        manifest_file.write_text("moved-skill:deadbeef\n")

        self._skill(bundled, "newcat/moved-skill")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert not (skills_dir / "newcat" / "moved-skill").exists()
        assert "moved-skill" not in result["copied"]
        assert "moved-skill" not in result.get("relocated", [])


class TestSyncSkills:
    def _setup_bundled(self, tmp_path):
        """Create a fake bundled skills directory."""
        bundled = tmp_path / "bundled_skills"
        (bundled / "category" / "new-skill").mkdir(parents=True)
        (bundled / "category" / "new-skill" / "SKILL.md").write_text("# New")
        (bundled / "category" / "new-skill" / "main.py").write_text("print(1)")
        (bundled / "category" / "DESCRIPTION.md").write_text("Category desc")
        (bundled / "old-skill").mkdir()
        (bundled / "old-skill" / "SKILL.md").write_text("# Old")
        return bundled

    def _patches(self, bundled, skills_dir, manifest_file):
        """Return context manager stack for patching sync globals."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def test_suppressed_builtin_not_reseeded(self, tmp_path):
        """A curator-pruned built-in in the suppression list must NOT be
        re-copied on sync — that's what makes the prune durable across updates.
        """
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file), \
                patch("tools.skills_sync._read_suppressed_names", return_value={"old-skill"}):
            result = sync_skills(quiet=True)

        # old-skill is suppressed → skipped, not copied.
        assert "old-skill" in result["suppressed"]
        assert "old-skill" not in result["copied"]
        assert not (skills_dir / "old-skill").exists()
        # The non-suppressed bundled skill is still copied normally.
        assert "new-skill" in result["copied"]
        assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()

    def test_fresh_install_copies_all_and_records_origin_hashes(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            manifest = _read_manifest()

        assert len(result["copied"]) == 2
        assert result["total_bundled"] == 2
        assert result["updated"] == []
        assert result["user_modified"] == []
        assert result["cleaned"] == []
        assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()
        assert (skills_dir / "old-skill" / "SKILL.md").exists()
        assert (skills_dir / "category" / "DESCRIPTION.md").exists()
        # v2 manifest: non-empty MD5 hashes for both skills.
        assert len(manifest["new-skill"]) == 32
        assert len(manifest["old-skill"]) == 32

    def test_user_deleted_skill_not_re_added_and_stale_entries_cleaned(self, tmp_path):
        """In manifest but not on disk = user deleted it; don't re-add. And a
        manifest entry no longer present in bundled gets cleaned out."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        skills_dir.mkdir(parents=True)
        old_hash = _dir_hash(bundled / "old-skill")
        manifest_file.write_text(f"old-skill:{old_hash}\nremoved-skill:def456\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            manifest = _read_manifest()

        assert "new-skill" in result["copied"]
        assert "old-skill" not in result["copied"]
        assert "old-skill" not in result.get("updated", [])
        assert not (skills_dir / "old-skill").exists()
        assert "removed-skill" in result["cleaned"]
        assert "removed-skill" not in manifest


    def test_copy_failure_does_not_poison_manifest_or_destroy_user_copy(self, tmp_path):
        """A failed copytree must leave nothing in the manifest (otherwise the
        next sync treats it as 'user deleted' and never retries) and must not
        destroy the user's existing copy on the update path."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # An already-synced, unmodified copy so the update path runs too.
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old v1")
        manifest_file.write_text(f"old-skill:{_dir_hash(user_skill)}\n")

        with self._patches(bundled, skills_dir, manifest_file):
            def failing_copytree(src, dst, *a, **kw):
                Path(dst).mkdir(parents=True, exist_ok=True)
                (Path(dst) / "PARTIAL").write_text("incomplete")
                raise OSError("Simulated disk full")

            with patch("shutil.copytree", side_effect=failing_copytree):
                result = sync_skills(quiet=True)

            assert "new-skill" not in result["copied"]
            assert "old-skill" not in result.get("updated", [])
            assert user_skill.exists(), (
                "Update failure destroyed user's skill copy without replacing it"
            )
            assert "new-skill" not in _read_manifest(), (
                "Failed copy was recorded in manifest — next sync will "
                "treat it as 'user deleted' and never retry"
            )
            assert not list(skills_dir.rglob(".bundled-sync-staging")), (
                "failed private copies must be cleaned without becoming visible"
            )

            # Now run sync again (copytree works this time) — it should retry.
            result2 = sync_skills(quiet=True)
            assert "new-skill" in result2["copied"]
            assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()


class TestGetBundledDir:
    def test_env_var_override_with_default_fallback(self, tmp_path, monkeypatch):
        custom_dir = tmp_path / "custom_skills"
        custom_dir.mkdir()
        monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(custom_dir))
        assert _get_bundled_dir() == custom_dir

        # Empty or unset falls back to the relative path from __file__.
        monkeypatch.setenv("HERMES_BUNDLED_SKILLS", "")
        assert _get_bundled_dir().name == "skills"
        monkeypatch.delenv("HERMES_BUNDLED_SKILLS", raising=False)
        assert _get_bundled_dir().name == "skills"


class TestResetBundledSkill:
    """Covers reset_bundled_skill() — the escape hatch for the 'user-modified' trap."""

    def _setup_bundled(self, tmp_path):
        """Create a minimal bundled skills tree with a single 'google-workspace' skill."""
        bundled = tmp_path / "bundled_skills"
        (bundled / "productivity" / "google-workspace").mkdir(parents=True)
        (bundled / "productivity" / "google-workspace" / "SKILL.md").write_text(
            "---\nname: google-workspace\n---\n# GW v2 (upstream)\n"
        )
        return bundled

    def _patches(self, bundled, skills_dir, manifest_file):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def test_reset_clears_stuck_user_modified_flag(self, tmp_path):
        """The core bug repro: copy-pasted bundled restore doesn't un-stick the flag; reset does."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Simulate the stuck state: user edited the skill on an older bundled version,
        # so manifest has an old origin hash that no longer matches anything on disk.
        dest = skills_dir / "productivity" / "google-workspace"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("---\nname: google-workspace\n---\n# GW v2 (upstream)\n")
        # Stale origin_hash — from some prior bundled version. User "restored" by pasting
        # the current bundled contents, so user_hash == current bundled_hash, but manifest
        # still points at the stale hash → treated as user_modified forever.
        manifest_file.write_text("google-workspace:STALEHASH000000000000000000000000\n")

        with self._patches(bundled, skills_dir, manifest_file):
            # Sanity check: without reset, sync would flag it user_modified
            pre = sync_skills(quiet=True)
            assert "google-workspace" in pre["user_modified"]

            # Reset (no --restore) should clear the manifest entry and re-baseline
            result = reset_bundled_skill("google-workspace", restore=False)

            assert result["ok"] is True
            assert result["action"] == "manifest_cleared"

            # After reset, the manifest should hold the *current* bundled hash
            manifest_after = _read_manifest()
            expected = _dir_hash(bundled / "productivity" / "google-workspace")
            assert manifest_after["google-workspace"] == expected
        # User's copy was preserved (we didn't delete)
        assert dest.exists()
        assert "GW v2" in (dest / "SKILL.md").read_text()


    def test_reset_errors_when_untracked_or_removed_upstream(self, tmp_path):
        """Untracked skills and skills removed upstream both fail clearly."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        skills_dir.mkdir(parents=True)
        manifest_file.write_text("")

        with self._patches(bundled, skills_dir, manifest_file):
            untracked = reset_bundled_skill("some-hub-skill", restore=False)

        assert untracked["ok"] is False
        assert untracked["action"] == "not_in_manifest"
        assert "not a tracked bundled skill" in untracked["message"]

        # Tracked in the manifest, but no longer shipped upstream.
        ghost = skills_dir / "productivity" / "ghost-skill"
        ghost.mkdir(parents=True)
        (ghost / "SKILL.md").write_text("---\nname: ghost-skill\n---\n# Ghost\n")
        manifest_file.write_text("ghost-skill:OLDHASH00000000000000000000000000\n")

        with self._patches(bundled, skills_dir, manifest_file):
            removed = reset_bundled_skill("ghost-skill", restore=True)

        assert removed["ok"] is False
        assert removed["action"] == "bundled_missing"

    def test_reset_restore_succeeds_on_readonly_nix_tree(self, tmp_path):
        """#34972: --restore must succeed even when the user copy is a fully
        read-only tree (r-xr-xr-x dirs + files), as produced by copying a
        Nix-store source. The manifest is re-baselined and bundled re-copied."""
        import os
        import stat

        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        dest = skills_dir / "productivity" / "google-workspace"
        sub = dest / "references"
        sub.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# user version\n")
        (sub / "ref.md").write_text("# nested ref\n")
        manifest_file.write_text(
            "google-workspace:STALEHASH000000000000000000000000\n"
        )

        # Read-only files AND directories — the real Nix-store case.
        ro_dir = (
            stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP
            | stat.S_IROTH | stat.S_IXOTH
        )
        os.chmod(sub / "ref.md", stat.S_IREAD)
        os.chmod(dest / "SKILL.md", stat.S_IREAD)
        os.chmod(sub, ro_dir)
        os.chmod(dest, ro_dir)

        try:
            with self._patches(bundled, skills_dir, manifest_file):
                result = reset_bundled_skill("google-workspace", restore=True)

            assert result["ok"] is True
            assert result["action"] == "restored"
            # Bundled version was re-copied over the (deleted) user copy.
            assert "upstream" in (dest / "SKILL.md").read_text()
            # The read-only nested user dir/file was fully removed, not left behind.
            assert not (sub / "ref.md").exists()
            # sync ran and re-copied the skill (not stuck in limbo).
            assert "google-workspace" in result["synced"]["copied"]
        finally:
            # Restore perms so tmp_path teardown can remove anything left.
            for p in (sub, dest):
                if p.exists():
                    os.chmod(p, stat.S_IRWXU)

    def test_reset_restore_preserves_manifest_on_rmtree_failure(self, tmp_path):
        """#34972: when the user copy genuinely cannot be removed, the manifest
        entry must NOT be deleted — otherwise the skill enters a limbo state
        where future syncs silently skip it forever."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        dest = skills_dir / "productivity" / "google-workspace"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# user version\n")
        manifest_file.write_text(
            "google-workspace:STALEHASH000000000000000000000000\n"
        )

        # Simulate an unremovable tree (e.g. a busy mountpoint or a path even
        # chmod can't rescue) by making the removal helper raise.
        def _boom(_path):
            raise PermissionError(13, "Permission denied")

        with self._patches(bundled, skills_dir, manifest_file), patch(
            "tools.skills_sync._rmtree_writable", side_effect=_boom
        ):
            result = reset_bundled_skill("google-workspace", restore=True)

        # Restore failed, and the manifest must be left untouched.
        assert result["ok"] is False
        assert result["action"] == "not_reset"
        assert "Manifest entry preserved" in result["message"]
        manifest_after = manifest_file.read_text()
        assert "google-workspace" in manifest_after
        # User copy is still on disk (we changed nothing).
        assert (dest / "SKILL.md").exists()


class TestNoBundledSkillsOptOut:
    """The .no-bundled-skills marker makes sync_skills() a no-op.

    This is what `hermes profile create --no-skills` (named profiles) and the
    installer's `--no-skills` flag (default ~/.hermes) rely on so bundled
    skills are never seeded at install time NOR re-injected by `hermes update`.
    """

    def test_marker_skips_sync_and_removal_seeds_normally(self, tmp_path):
        bundled = tmp_path / "bundled"
        skill = bundled / "category" / "new-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: new-skill\n---\nbody\n")

        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        hermes_home = tmp_path / "home"
        hermes_home.mkdir()
        marker = hermes_home / ".no-bundled-skills"
        marker.write_text("opted out\n")

        from contextlib import ExitStack

        def _patches():
            stack = ExitStack()
            stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
            stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
            stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
            stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
            stack.enter_context(patch("tools.skills_sync.HERMES_HOME", hermes_home))
            return stack

        with _patches():
            opted_out = sync_skills(quiet=True)

        # Opt-out signalled, nothing copied, nothing written to disk.
        assert opted_out["skipped_opt_out"] is True
        assert opted_out["copied"] == []
        assert opted_out["total_bundled"] == 0
        assert not (skills_dir / "category" / "new-skill" / "SKILL.md").exists()

        marker.unlink()
        with _patches():
            seeded = sync_skills(quiet=True)

        assert seeded.get("skipped_opt_out") is not True
        assert "new-skill" in seeded["copied"]
        assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()


class TestOptOutToggleAndRemove:
    """`hermes skills opt-out/opt-in` core: marker toggle + safe removal."""

    def _setup_bundled(self, tmp_path):
        bundled = tmp_path / "bundled"
        for n in ("alpha", "beta"):
            d = bundled / n
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {n}\n---\nbody {n}\n")
        return bundled

    def test_marker_toggle(self, tmp_path):
        from tools.skills_sync import (
            set_bundled_skills_opt_out, is_bundled_skills_opt_out,
        )
        home = tmp_path / "home"
        home.mkdir()
        with patch("tools.skills_sync.HERMES_HOME", home):
            assert is_bundled_skills_opt_out() is False
            r = set_bundled_skills_opt_out(True)
            assert r["ok"] and r["changed"]
            assert is_bundled_skills_opt_out() is True
            # idempotent
            r2 = set_bundled_skills_opt_out(True)
            assert r2["ok"] and r2["changed"] is False
            # opt back in
            r3 = set_bundled_skills_opt_out(False)
            assert r3["ok"] and r3["changed"]
            assert is_bundled_skills_opt_out() is False

    def test_remove_keeps_user_modified(self, tmp_path):
        from tools.skills_sync import (
            sync_skills, remove_pristine_bundled_skills,
        )
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        home = tmp_path / "home"
        home.mkdir()
        with patch("tools.skills_sync._get_bundled_dir", return_value=bundled), \
             patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"), \
             patch("tools.skills_sync.SKILLS_DIR", skills_dir), \
             patch("tools.skills_sync.MANIFEST_FILE", manifest_file), \
             patch("tools.skills_sync.HERMES_HOME", home):
            sync_skills(quiet=True)
            # User edits 'beta'
            (skills_dir / "beta" / "SKILL.md").write_text("---\nname: beta\n---\nEDITED\n")
            # A hand-written, non-bundled skill must also survive.
            (skills_dir / "mine").mkdir()
            (skills_dir / "mine" / "SKILL.md").write_text("---\nname: mine\n---\nlocal\n")

            preview = remove_pristine_bundled_skills(dry_run=True)
            assert "alpha" in preview["removed"]
            assert "beta" not in preview["removed"]

            result = remove_pristine_bundled_skills(dry_run=False)
            assert "alpha" in result["removed"]
            assert not (skills_dir / "alpha").exists()
            # user-modified bundled skill kept
            assert (skills_dir / "beta" / "SKILL.md").exists()
            assert "EDITED" in (skills_dir / "beta" / "SKILL.md").read_text()
            # non-bundled local skill never considered
            assert (skills_dir / "mine" / "SKILL.md").exists()


class TestUpdateBackupRecovery:
    """Regression tests for backup handling in the bundled-update path.

    Covers three failure modes around ``dest.with_suffix(".bak")``:
    a stale backup poisoning the next update's move/restore, an orphaned
    backup (crash between move and copytree) being misread as a user
    deletion, and a partially-written dest blocking restore-on-failure.
    """

    def _setup(self, tmp_path, bundled_text="# Old v2 (updated)"):
        """Bundled dir with one flat skill, plus user dirs."""
        bundled = tmp_path / "bundled_skills"
        (bundled / "old-skill").mkdir(parents=True)
        (bundled / "old-skill" / "SKILL.md").write_text(bundled_text)
        skills_dir = tmp_path / "user_skills"
        skills_dir.mkdir()
        manifest_file = skills_dir / ".bundled_manifest"
        return bundled, skills_dir, manifest_file

    def _patches(self, bundled, skills_dir, manifest_file):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def _seed_synced_copy(self, skills_dir, manifest_file, text="# Old v1"):
        """User copy of old-skill whose hash matches the manifest origin."""
        dest = skills_dir / "old-skill"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(text)
        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest({"old-skill": _dir_hash(dest)})
        return dest

    def test_stale_backup_does_not_poison_failed_update(self, tmp_path):
        """A leftover .bak must not nest the live copy or corrupt restore.

        With a stale ``old-skill.bak`` present, ``shutil.move(dest, backup)``
        moves the live copy *inside* the stale dir. If copytree then fails,
        restore drags the stale junk (with the live copy nested in it) back
        to dest — corrupting the skill and wedging it as "user-modified".
        """
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        dest = self._seed_synced_copy(skills_dir, manifest_file)

        stale = skills_dir / "old-skill.bak"
        stale.mkdir()
        (stale / "SKILL.md").write_text("# stale junk from an earlier failure")

        def _boom(src, dst, **kwargs):
            raise OSError("simulated copy failure")

        with self._patches(bundled, skills_dir, manifest_file), \
                patch("tools.skills_sync.shutil.copytree", side_effect=_boom):
            sync_skills(quiet=True)

        # The live copy must survive the failed update untouched...
        assert (dest / "SKILL.md").read_text() == "# Old v1"
        # ...not be nested inside recycled stale-backup content.
        assert not (dest / "old-skill").exists()
        # And no backup directory may linger.
        assert not (skills_dir / "old-skill.bak").exists()

    def test_orphaned_backup_is_recovered_not_treated_as_deleted(self, tmp_path):
        """Crash between move and copytree must not lose the skill.

        After such a crash, dest is gone and the user's only copy sits in
        ``old-skill.bak``. The sync loop's "in manifest but not on disk"
        branch reads that as a deliberate user deletion and skips — the
        skill silently vanishes. It must instead be recovered and updated.
        """
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        dest = self._seed_synced_copy(skills_dir, manifest_file)
        # Simulate the crash: dest was moved aside, new copy never arrived.
        shutil.move(str(dest), str(skills_dir / "old-skill.bak"))

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        # Recovered and then updated to the new bundled version in one run.
        assert (dest / "SKILL.md").exists()
        assert (dest / "SKILL.md").read_text() == "# Old v2 (updated)"
        assert "old-skill" in result["updated"]
        assert not (skills_dir / "old-skill.bak").exists()

    def test_partial_copy_failure_restores_original(self, tmp_path):
        """A half-written dest must not block restore-on-failure.

        If copytree dies after creating dest, the ``not dest.exists()``
        guard skips the restore: the user keeps a broken partial skill,
        the .bak lingers, and the partial hash wedges the skill as
        "user-modified" on every later sync.
        """
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        dest = self._seed_synced_copy(skills_dir, manifest_file)

        def _partial_then_fail(src, dst, **kwargs):
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "PARTIAL").write_text("half-written")
            raise OSError("simulated failure mid-copy")

        with self._patches(bundled, skills_dir, manifest_file), \
                patch("tools.skills_sync.shutil.copytree", side_effect=_partial_then_fail):
            sync_skills(quiet=True)

        # Original content restored, partial debris and backup gone.
        assert (dest / "SKILL.md").read_text() == "# Old v1"
        assert not (dest / "PARTIAL").exists()
        assert not (skills_dir / "old-skill.bak").exists()
        assert not list(skills_dir.rglob(".bundled-sync-staging"))

        # And the skill is not wedged: a later normal sync updates cleanly.
        with self._patches(bundled, skills_dir, manifest_file):
            result2 = sync_skills(quiet=True)
        assert "old-skill" in result2["updated"]
        assert result2["user_modified"] == []

    def test_completed_promotion_with_stale_manifest_is_reconciled(self, tmp_path):
        """A crash after atomic rename must not wedge the new copy as edited."""
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        dest = self._seed_synced_copy(skills_dir, manifest_file)
        backup = skills_dir / "old-skill.bak"
        shutil.copytree(dest, backup)
        shutil.rmtree(dest)
        shutil.copytree(bundled / "old-skill", dest)

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            recorded = _read_manifest()["old-skill"]

        assert recorded == _dir_hash(bundled / "old-skill")
        assert "old-skill" in result["updated"]
        assert result["user_modified"] == []
        assert not backup.exists()

    def test_manifest_commit_failure_retains_backup_until_recovery(self, tmp_path):
        """A failed commit must not erase the only interrupted-update receipt."""
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        dest = self._seed_synced_copy(skills_dir, manifest_file)
        old_manifest = manifest_file.read_bytes()
        backup = skills_dir / "old-skill.bak"

        with self._patches(bundled, skills_dir, manifest_file), patch(
            "tools.skills_sync.atomic_write_text",
            side_effect=OSError("simulated manifest fsync failure"),
        ):
            result = sync_skills(quiet=True)

        assert "old-skill" in result["updated"]
        assert (dest / "SKILL.md").read_text() == "# Old v2 (updated)"
        assert backup.is_dir()
        assert (backup / "SKILL.md").read_text() == "# Old v1"
        assert manifest_file.read_bytes() == old_manifest

        # The next successful run recognizes the complete promotion, commits
        # the new hash, and only then retires the retained backup.
        with self._patches(bundled, skills_dir, manifest_file):
            recovered = sync_skills(quiet=True)
            recorded = _read_manifest()["old-skill"]

        assert "old-skill" in recovered["updated"]
        assert recorded == _dir_hash(bundled / "old-skill")
        assert not backup.exists()

    def test_promotion_receipt_survives_bundle_advancing_before_recovery(
        self,
        tmp_path,
    ):
        """A v3 bundle must not make a complete uncommitted v2 look edited."""
        import tools.skills_sync as skills_sync_module

        bundled, skills_dir, manifest_file = self._setup(
            tmp_path,
            bundled_text="# Bundled v2",
        )
        dest = self._seed_synced_copy(
            skills_dir,
            manifest_file,
            text="# Bundled v1",
        )
        v1_hash = _dir_hash(dest)
        backup = skills_dir / "old-skill.bak"
        receipt = skills_sync_module._bundled_promotion_receipt_path(dest)

        def recorded_hash():
            with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
                return _read_manifest()["old-skill"]

        with self._patches(bundled, skills_dir, manifest_file), patch(
            "tools.skills_sync.atomic_write_text",
            side_effect=OSError("simulated manifest fsync failure"),
        ):
            first = sync_skills(quiet=True)

        v2_hash = _dir_hash(dest)
        assert "old-skill" in first["updated"]
        assert v2_hash != v1_hash
        assert backup.is_dir()
        assert receipt.is_file()
        assert recorded_hash() == v1_hash

        # The packaged source advances before the next process gets a chance
        # to reconcile the failed v2 manifest commit.
        (bundled / "old-skill" / "SKILL.md").write_text("# Bundled v3")
        v3_hash = _dir_hash(bundled / "old-skill")

        with self._patches(bundled, skills_dir, manifest_file):
            recovered = sync_skills(quiet=True)

        assert "old-skill" in recovered["updated"]
        assert _dir_hash(dest) == v2_hash
        assert recorded_hash() == v2_hash
        assert not backup.exists()
        assert not receipt.exists()

        # With v2 durably baselined, the normal atomic update can now install
        # v3 without classifying v2 as a user edit.
        with self._patches(bundled, skills_dir, manifest_file):
            advanced = sync_skills(quiet=True)

        assert "old-skill" in advanced["updated"]
        assert advanced["user_modified"] == []
        assert _dir_hash(dest) == v3_hash
        assert recorded_hash() == v3_hash
        assert not backup.exists()
        assert not receipt.exists()

    def test_sync_cleans_only_stale_owned_staging_entries(self, tmp_path):
        """SIGKILL debris is bounded and fresh/concurrent staging survives."""
        bundled, skills_dir, manifest_file = self._setup(tmp_path)
        staging_root = skills_dir / ".bundled-sync-staging"
        stale = staging_root / "old-skill.tmp-staleowned"
        fresh = staging_root / "old-skill.tmp-freshowned"
        unknown = staging_root / "operator-note"
        for path in (stale, fresh, unknown):
            path.mkdir(parents=True, exist_ok=True)
            (path / "PARTIAL").write_text("preserve only when appropriate")
        old = 1_000_000.0
        os.utime(stale, (old, old))
        os.utime(unknown, (old, old))

        with self._patches(bundled, skills_dir, manifest_file), patch(
            "tools.skills_sync.time.time",
            return_value=old + 2 * 24 * 60 * 60,
        ):
            sync_skills(quiet=True)

        assert not stale.exists()
        assert fresh.is_dir()
        assert unknown.is_dir()

    def test_staging_cleanup_refuses_windows_reparse_root(self, tmp_path):
        """Python 3.11 Windows junctions must not be enumerated as staging."""
        import tools.skills_sync as skills_sync_module

        _bundled, skills_dir, _manifest_file = self._setup(tmp_path)
        staging_root = skills_dir / ".bundled-sync-staging"
        staging_root.mkdir()
        real_stat = Path.stat
        reparse_metadata = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_mtime=0.0,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        def mocked_stat(path, *args, **kwargs):
            if Path(path) == staging_root and kwargs.get("follow_symlinks") is False:
                return reparse_metadata
            return real_stat(path, *args, **kwargs)

        with patch("tools.skills_sync.SKILLS_DIR", skills_dir), patch.object(
            Path,
            "stat",
            mocked_stat,
        ), patch(
            "tools.skills_sync.os.scandir",
            side_effect=AssertionError("cleanup traversed reparse staging root"),
        ) as scandir:
            assert (
                skills_sync_module._cleanup_stale_bundled_sync_staging(
                    staging_root,
                    now=100_000.0,
                )
                == 0
            )

        scandir.assert_not_called()

    def test_staging_cleanup_preserves_windows_reparse_entry(self, tmp_path):
        """A reparse child is never handed to recursive deletion."""
        import tools.skills_sync as skills_sync_module

        _bundled, skills_dir, _manifest_file = self._setup(tmp_path)
        staging_root = skills_dir / ".bundled-sync-staging"
        staging_root.mkdir()
        entry_path = staging_root / "old-skill.tmp-reparse"
        entry_path.mkdir()

        class ReparseEntry:
            name = entry_path.name
            path = str(entry_path)

            @staticmethod
            def stat(*, follow_symlinks):
                assert follow_symlinks is False
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_mtime=0.0,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )

        class Entries:
            def __enter__(self):
                return iter((ReparseEntry(),))

            def __exit__(self, *_args):
                return False

        with patch("tools.skills_sync.SKILLS_DIR", skills_dir), patch(
            "tools.skills_sync.os.scandir",
            return_value=Entries(),
        ), patch(
            "tools.skills_sync._rmtree_writable",
            side_effect=AssertionError("cleanup deleted a reparse entry"),
        ) as remove:
            assert (
                skills_sync_module._cleanup_stale_bundled_sync_staging(
                    staging_root,
                    now=100_000.0,
                )
                == 0
            )

        remove.assert_not_called()
        assert entry_path.is_dir()


class TestCallTimeDirResolution:
    """Regression for #65828: skills_sync bound SKILLS_DIR/MANIFEST_FILE/
    HERMES_HOME at import, so a long-lived dashboard/TUI process serving a
    console skills command for another profile resolved (and for
    reset_bundled_skill DELETED) against whichever home was live at import.
    The accessors must follow set_hermes_home_override() at call time, while
    an explicitly patched module global (tests, _profile_scope retargeting)
    still wins.
    """

    def test_accessors_follow_hermes_home_override(self, tmp_path):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        import tools.skills_sync as ss

        profile_home = tmp_path / "profiles" / "research"
        token = set_hermes_home_override(str(profile_home))
        try:
            assert ss._hermes_home() == profile_home
            assert ss._skills_dir() == profile_home / "skills"
            assert ss._manifest_file() == profile_home / "skills" / ".bundled_manifest"
        finally:
            reset_hermes_home_override(token)

    def test_explicit_module_patch_wins_over_override(self, tmp_path):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        import tools.skills_sync as ss

        patched = tmp_path / "patched-skills"
        token = set_hermes_home_override(str(tmp_path / "other-profile"))
        try:
            with patch("tools.skills_sync.SKILLS_DIR", patched):
                assert ss._skills_dir() == patched
                # MANIFEST_FILE unpatched -> derives from the patched skills dir.
                assert ss._manifest_file() == patched / ".bundled_manifest"
        finally:
            reset_hermes_home_override(token)

    def test_rmtree_guard_anchors_on_overridden_profile(self, tmp_path):
        """The #48200 strict-child rmtree guard must anchor on the OVERRIDDEN
        profile's skills root. Under the stale import-time binding the guard
        was computed against the wrong home (#65828's sharpest edge): a
        legitimate delete in the scoped profile would be refused, and a stale
        path under the import-time home would pass the guard."""
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        import tools.skills_sync as ss

        profile_home = tmp_path / "profiles" / "worker"
        victim = profile_home / "skills" / "doomed-skill"
        victim.mkdir(parents=True)
        (victim / "SKILL.md").write_text("---\nname: doomed-skill\n---\n", encoding="utf-8")

        token = set_hermes_home_override(str(profile_home))
        try:
            # Allowed: strict child of the overridden profile's skills root.
            ss._rmtree_writable(victim)
            assert not victim.exists()

            # Refused: a path under the import-time home is OUTSIDE the
            # overridden profile's skills root now.
            foreign = ss._SKILLS_DIR_AT_IMPORT / "some-skill"
            with pytest.raises(ValueError):
                ss._rmtree_writable(foreign)
        finally:
            reset_hermes_home_override(token)
