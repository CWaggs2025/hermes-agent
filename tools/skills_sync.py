#!/usr/bin/env python3
"""
Skills Sync -- Manifest-based seeding and updating of bundled skills.

Copies bundled skills from the repo's skills/ directory into ~/.hermes/skills/
and uses a manifest to track which skills have been synced and their origin hash.

Manifest format (v2): each line is "skill_name:origin_hash" where origin_hash
is the MD5 of the bundled skill at the time it was last synced to the user dir.
Old v1 manifests (plain names without hashes) are auto-migrated.

Update logic:
  - NEW skills (not in manifest): copied to user dir, origin hash recorded.
  - EXISTING skills (in manifest, present in user dir):
      * If bundled still matches origin hash: no update → skip without reading
        the user copy.
      * If bundled changed and user copy matches origin hash: safe to update.
      * If bundled changed and user copy differs: user customized it → SKIP.
  - DELETED by user (in manifest, absent from user dir): respected, not re-added.
  - REMOVED from bundled (in manifest, gone from repo): cleaned from manifest.

The manifest lives at ~/.hermes/skills/.bundled_manifest.
"""

import hashlib
import json
import logging
import multiprocessing
import os
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# Force stdout/stderr to UTF-8. On non-UTF-8 Windows locales (e.g. CP936/GBK
# on zh-CN), Python's default stream encoding can't represent the checkmark /
# arrow glyphs this script prints (✓ U+2713, ↑ U+2191), raising
# UnicodeEncodeError mid-run. The bootstrap installer (install.ps1) captures
# this script's stdout and parses it as UTF-8; a GBK byte stream then surfaces
# as "stream did not contain valid UTF-8" and aborts the config-templates
# stage even though the script itself exits 0. Reconfigure unconditionally so
# output is valid UTF-8 regardless of the active codepage or caller.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, TypeError):
            pass
from hermes_constants import get_bundled_skills_dir, get_hermes_home, get_optional_skills_dir
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS,
    EXTERNAL_SKILLS_CATALOG_VERSION,
    EXTERNAL_SKILLS_MAX_CATALOG_BYTES,
    EXTERNAL_SKILLS_MAX_CATALOG_NAMES,
    EXTERNAL_SKILLS_MAX_NAME_BYTES,
    EXTERNAL_SKILLS_SCAN_TIMEOUT_DEFAULT,
    EXTERNAL_SKILLS_SCAN_TIMEOUT_MAX,
    EXTERNAL_SKILLS_SCAN_TIMEOUT_MIN,
    EXTERNAL_SKILLS_SNAPSHOT_READ_LOCK_SUFFIX,
    SKILL_SUPPORT_DIRS,
    external_skills_catalog_path,
    external_skills_roots_fingerprint,
    external_skills_snapshot_dir,
    get_gateway_external_skills_snapshot,
    get_external_skills_scan_settings,
    is_excluded_skill_path,
    _read_bounded_regular_file,
)
from typing import Any, Dict, List, Optional, Set, Tuple
from utils import atomic_replace, atomic_write_text

logger = logging.getLogger(__name__)


HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"

# Import-time snapshots backing the call-time accessors below. Same bug class
# and same fix as skills_tool (f8723c478) and skill_manager_tool (c6a3d412d):
# long-lived multi-profile runtimes (Dashboard console, TUI/Desktop backend,
# cron, kanban workers) import this module once under the launch HERMES_HOME
# and later scope requests to a different profile via
# set_hermes_home_override(). Frozen module constants would then resolve —
# and for reset_bundled_skill() DELETE — against the wrong profile's skills
# root (#65828). The accessors honor an explicitly patched module global
# (tests, and web_server's _profile_scope retargeting) and otherwise
# re-resolve from the live profile-scoped HERMES_HOME on every call.
_HERMES_HOME_AT_IMPORT = HERMES_HOME
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR
_MANIFEST_FILE_AT_IMPORT = MANIFEST_FILE


def _hermes_home() -> Path:
    """Return the active profile's HERMES_HOME at call time."""
    configured = Path(HERMES_HOME)
    if configured != _HERMES_HOME_AT_IMPORT:
        return configured
    return get_hermes_home()


def _skills_dir() -> Path:
    """Return the active profile's skills directory at call time."""
    configured = Path(SKILLS_DIR)
    if configured != _SKILLS_DIR_AT_IMPORT:
        return configured
    return _hermes_home() / "skills"


def _manifest_file() -> Path:
    """Return the active profile's bundled-skills manifest at call time."""
    configured = Path(MANIFEST_FILE)
    if configured != _MANIFEST_FILE_AT_IMPORT:
        return configured
    return _skills_dir() / ".bundled_manifest"

# Marker file written by `hermes profile create --no-skills` (named profiles)
# and by the installer's `--no-skills` flag (the default ~/.hermes profile).
# When present in HERMES_HOME, sync_skills() is a no-op so neither the
# installer, `hermes update`, nor a direct sync re-injects bundled skills.
# Delete the file to opt back in. Mirrors
# hermes_cli.profiles.NO_BUNDLED_SKILLS_MARKER (kept as a literal here to
# avoid importing the CLI layer into this low-level sync module).
NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"

# External skill roots can live on network filesystems. Keep every operation
# that can touch those roots in a disposable child process so a stuck
# ``stat``/``scandir`` cannot wedge the caller (especially gateway startup).
_DEFAULT_EXTERNAL_SCAN_TIMEOUT_SECONDS = EXTERNAL_SKILLS_SCAN_TIMEOUT_DEFAULT
_MIN_EXTERNAL_SCAN_TIMEOUT_SECONDS = EXTERNAL_SKILLS_SCAN_TIMEOUT_MIN
_MAX_EXTERNAL_SCAN_TIMEOUT_SECONDS = EXTERNAL_SKILLS_SCAN_TIMEOUT_MAX
_EXTERNAL_SCAN_TERMINATE_GRACE_SECONDS = 0.25
_EXTERNAL_SCAN_KILL_GRACE_SECONDS = 0.25
_MAX_EXTERNAL_SCAN_DIRECTORIES = 50_000
_MAX_EXTERNAL_SCAN_ENTRIES = 100_000
_MAX_EXTERNAL_SCAN_FILES = 50_000
_MAX_EXTERNAL_SKILL_FILES = 10_000
_MAX_EXTERNAL_SCAN_DEPTH = 128
_MAX_EXTERNAL_MATERIALIZED_FILE_BYTES = 64 * 1024 * 1024
_MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES = 256 * 1024 * 1024
# Retain at most four complete immutable generations (current, its immediate
# predecessor, and at most two older snapshots). Pre-scan GC reserves one full
# materialization slot, so even a cleanup failure after publication cannot
# grow the retained complete set past this 1 GiB content ceiling.
_MAX_EXTERNAL_SNAPSHOT_GENERATIONS = 4
_MAX_EXTERNAL_SNAPSHOT_TOTAL_BYTES = (
    _MAX_EXTERNAL_SNAPSHOT_GENERATIONS * _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES
)
_MAX_EXTERNAL_SNAPSHOT_INDEX_ENTRIES = 10_000
_EXTERNAL_SNAPSHOT_COMPLETE_MARKER = ".complete.json"
_EXTERNAL_SNAPSHOT_GC_PREFIX = ".gc-"
_MAX_SKILL_FRONTMATTER_BYTES = 4_000
_EXTERNAL_SCAN_BACKOFF_BASE_SECONDS = 10.0
_EXTERNAL_SCAN_BACKOFF_MAX_SECONDS = 300.0
_EXTERNAL_CATALOG_VERSION = EXTERNAL_SKILLS_CATALOG_VERSION
_EXTERNAL_SCAN_THREAD_LOCK = threading.Lock()


class ExternalSkillIndex(set):
    """A set-compatible external catalog with freshness metadata."""

    def __init__(self, values=(), *, status: str = "fresh", reason: str = ""):
        super().__init__(values)
        self.status = status
        self.reason = reason


class ExternalSkillIndexUnavailable(RuntimeError):
    """Raised when a configured external catalog cannot be known safely."""


class ExternalScanResult:
    """Validated child result plus its versioned local materialization."""

    def __init__(
        self,
        names: Set[str],
        materialized_roots: Tuple[str, ...],
        scan_id: str,
        materialized_bytes: int,
    ):
        self.names = names
        self.materialized_roots = materialized_roots
        self.scan_id = scan_id
        self.materialized_bytes = materialized_bytes


def _essential_names() -> frozenset:
    """Names of skills that must always exist (see skill_utils.ESSENTIAL_SKILLS)."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        return ESSENTIAL_SKILLS
    except Exception:
        return frozenset({"hermes-agent"})


def _get_bundled_dir() -> Path:
    """Locate the bundled skills/ directory.

    Checks HERMES_BUNDLED_SKILLS env var first (set by Nix wrapper),
    then falls back to the relative path from this source file.
    """
    return get_bundled_skills_dir(Path(__file__).parent.parent / "skills")


def _get_optional_dir() -> Path:
    """Locate the official optional-skills/ directory."""
    return get_optional_skills_dir(Path(__file__).parent.parent / "optional-skills")


def _configured_external_scan_settings() -> Tuple[Tuple[str, ...], float]:
    """Use the shared lexical resolver; never stat an external root here."""
    try:
        return get_external_skills_scan_settings(
            hermes_home=_hermes_home(),
            skills_dir=_skills_dir(),
        )
    except ValueError as exc:
        raise ExternalSkillIndexUnavailable(
            str(exc)
        ) from exc


def _external_catalog_fingerprint(roots: Tuple[str, ...]) -> str:
    return external_skills_roots_fingerprint(roots)


def _external_catalog_cache_path() -> Path:
    return external_skills_catalog_path(_hermes_home())


def _external_materialized_snapshot_dir() -> Path:
    return external_skills_snapshot_dir(_hermes_home())


def _external_scan_backoff_path() -> Path:
    return _hermes_home() / "cache" / "external-skills-scan-backoff.json"


def _external_scan_lock_path() -> Path:
    return _hermes_home() / "cache" / "external-skills-scan.lock"


def _external_scan_orphan_path() -> Path:
    return _hermes_home() / "cache" / "external-skills-scan-orphan.json"


def _external_empty_confirmation_path() -> Path:
    return _hermes_home() / "cache" / "external-skills-empty-confirmation.json"


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        encoded = _read_bounded_regular_file(
            path,
            EXTERNAL_SKILLS_MAX_CATALOG_BYTES,
        )
        parsed = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_json_object_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Publish private scanner state without following a planted symlink.

    ``utils.atomic_write_text`` intentionally preserves symlink targets for
    user-managed config files. Scanner leases and catalog pointers need the
    opposite contract: they are private runtime state, so replacing a symlink
    itself is safer than rewriting whatever it targets.
    """
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > EXTERNAL_SKILLS_MAX_CATALOG_BYTES:
        raise OSError(
            "external scanner state exceeded the total byte safety limit "
            f"({EXTERNAL_SKILLS_MAX_CATALOG_BYTES})"
        )

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError(f"external scanner state parent is unsafe: {path.parent}")
    try:
        existing = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise OSError(f"external scanner state path is unsafe: {path}")

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Same-directory os.replace is atomic and replaces a symlink entry
        # rather than following it. Do not use the shared cross-device/in-place
        # fallback here: a failed replace must leave the LKG pointer untouched.
        os.replace(temporary_name, path)
        temporary_name = ""
        if os.name != "nt":
            try:
                parent_fd = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                pass
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _load_external_catalog_snapshot(fingerprint: str) -> Optional[Set[str]]:
    payload = _read_json_object(_external_catalog_cache_path())
    if (
        not payload
        or payload.get("version") != _EXTERNAL_CATALOG_VERSION
        or payload.get("roots_fingerprint") != fingerprint
    ):
        return None
    names = payload.get("names")
    if (
        not isinstance(names, list)
        or len(names) > EXTERNAL_SKILLS_MAX_CATALOG_NAMES
        or any(
            not isinstance(name, str)
            or not name
            or len(name.encode("utf-8")) > EXTERNAL_SKILLS_MAX_NAME_BYTES
            for name in names
        )
    ):
        return None
    return set(names)


def _publish_external_catalog_snapshot(
    fingerprint: str,
    roots: Tuple[str, ...],
    names: Set[str],
    materialized_roots: Tuple[str, ...],
) -> None:
    _write_json_object_atomic(
        _external_catalog_cache_path(),
        {
            "version": _EXTERNAL_CATALOG_VERSION,
            "roots_fingerprint": fingerprint,
            "roots": list(roots),
            "names": sorted(names),
            "materialized_complete": True,
            "materialized_roots": list(materialized_roots),
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        },
    )


class _ExternalSnapshotGeneration:
    """Bounded metadata for one immutable materialized generation."""

    def __init__(
        self,
        path: Path,
        materialized_bytes: int,
        created_at_ns: int,
        *,
        complete: bool,
        quarantined: bool = False,
    ):
        self.path = path
        self.materialized_bytes = materialized_bytes
        self.created_at_ns = created_at_ns
        self.complete = complete
        self.quarantined = quarantined


def _external_snapshot_generation_from_roots(raw_roots) -> Optional[Path]:
    """Resolve one catalog root list to its lexical local generation path."""
    if not isinstance(raw_roots, list) or not raw_roots:
        return None
    generation_parts = None
    for root_index, raw_relative in enumerate(raw_roots):
        if not isinstance(raw_relative, str) or not raw_relative:
            return None
        relative = PurePosixPath(raw_relative)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 3
            or relative.parts[2] != f"root-{root_index:04d}"
            or any("\\" in part or "/" in part for part in relative.parts)
        ):
            return None
        candidate_parts = relative.parts[:2]
        if generation_parts is None:
            generation_parts = candidate_parts
        elif candidate_parts != generation_parts:
            return None
    if generation_parts is None:
        return None
    return _external_materialized_snapshot_dir().joinpath(*generation_parts)


def _current_external_snapshot_generation() -> Optional[Path]:
    payload = _read_json_object(_external_catalog_cache_path())
    if (
        not payload
        or payload.get("version") != _EXTERNAL_CATALOG_VERSION
        or payload.get("materialized_complete") is not True
    ):
        return None
    return _external_snapshot_generation_from_roots(
        payload.get("materialized_roots")
    )


def _external_catalog_pointer_exists() -> bool:
    try:
        _external_catalog_cache_path().stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        # Unknown must fail closed in retention callers.
        return True
    return True


def _write_external_snapshot_complete_marker(
    staging: Path,
    *,
    fingerprint: str,
    scan_id: str,
    materialized_bytes: int,
) -> None:
    if not isinstance(materialized_bytes, int) or not (
        0 <= materialized_bytes <= _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES
    ):
        raise OSError("external snapshot reported an invalid byte count")
    _write_json_object_atomic(
        staging / _EXTERNAL_SNAPSHOT_COMPLETE_MARKER,
        {
            "version": _EXTERNAL_CATALOG_VERSION,
            "roots_fingerprint": fingerprint,
            "scan_id": scan_id,
            "materialized_bytes": materialized_bytes,
            "created_at_ns": time.time_ns(),
        },
    )


def _publish_external_materialized_generation(
    staging: Path,
    *,
    fingerprint: str,
    scan_id: str,
) -> Path:
    """Move completed staging under no-follow destination dir descriptors."""
    if not _external_fd_traversal_supported():
        raise OSError(
            "race-safe descriptor-relative snapshot publication is unavailable"
        )
    base = _external_materialized_snapshot_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base_fd = -1
    fingerprint_fd = -1
    try:
        base_fd = _open_external_directory_at(base)
        try:
            os.mkdir(fingerprint, mode=0o700, dir_fd=base_fd)
        except FileExistsError:
            pass
        fingerprint_fd = _open_external_directory_at(
            fingerprint,
            parent_fd=base_fd,
        )
        try:
            os.fchmod(base_fd, 0o700)
            os.fchmod(fingerprint_fd, 0o700)
        except (AttributeError, OSError):
            pass
        os.rename(
            staging,
            scan_id,
            dst_dir_fd=fingerprint_fd,
        )
        try:
            os.fsync(fingerprint_fd)
            os.fsync(base_fd)
        except OSError:
            pass
    finally:
        if fingerprint_fd >= 0:
            os.close(fingerprint_fd)
        if base_fd >= 0:
            os.close(base_fd)
    return base / fingerprint / scan_id


def _external_snapshot_generation_metadata(
    generation: Path,
    *,
    quarantined: bool = False,
) -> _ExternalSnapshotGeneration:
    marker = _read_json_object(generation / _EXTERNAL_SNAPSHOT_COMPLETE_MARKER)
    marker_scan_id = marker.get("scan_id") if marker else None
    scan_id_matches = bool(
        isinstance(marker_scan_id, str)
        and marker_scan_id
        and (
            marker_scan_id == generation.name
            or (
                quarantined
                and generation.name.startswith(
                    f"{_EXTERNAL_SNAPSHOT_GC_PREFIX}{marker_scan_id}-"
                )
            )
        )
    )
    complete = bool(
        marker
        and marker.get("version") == _EXTERNAL_CATALOG_VERSION
        and marker.get("roots_fingerprint") == generation.parent.name
        and scan_id_matches
    )
    try:
        materialized_bytes = int(marker.get("materialized_bytes", -1))
        created_at_ns = int(marker.get("created_at_ns", -1))
    except (AttributeError, TypeError, ValueError):
        materialized_bytes = -1
        created_at_ns = -1
    if not (
        0 <= materialized_bytes <= _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES
        and created_at_ns >= 0
    ):
        complete = False
    if not complete:
        # Unknown generations are never deleted automatically, but charge the
        # maximum possible scan size so they cannot be ignored while deciding
        # whether another materialization is safe to admit.
        materialized_bytes = _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES
        try:
            created_at_ns = generation.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            created_at_ns = 0
    return _ExternalSnapshotGeneration(
        generation,
        materialized_bytes,
        created_at_ns,
        complete=complete,
        quarantined=quarantined,
    )


def _list_external_snapshot_generations() -> List[_ExternalSnapshotGeneration]:
    """List at most a bounded number of local two-level generations."""
    base = _external_materialized_snapshot_dir()
    try:
        metadata = base.stat(follow_symlinks=False)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"external snapshot root is unsafe: {base}")

    generations: List[_ExternalSnapshotGeneration] = []
    entries_seen = 0
    with os.scandir(base) as fingerprints:
        for fingerprint_entry in fingerprints:
            entries_seen += 1
            if entries_seen > _MAX_EXTERNAL_SNAPSHOT_INDEX_ENTRIES:
                raise OSError("external snapshot index exceeded its entry limit")
            if fingerprint_entry.is_symlink():
                raise OSError("external snapshot index contains a symlink")
            if not fingerprint_entry.is_dir(follow_symlinks=False):
                continue
            fingerprint_dir = base / fingerprint_entry.name
            with os.scandir(fingerprint_dir) as candidates:
                for candidate_entry in candidates:
                    entries_seen += 1
                    if entries_seen > _MAX_EXTERNAL_SNAPSHOT_INDEX_ENTRIES:
                        raise OSError(
                            "external snapshot index exceeded its entry limit"
                        )
                    if candidate_entry.is_symlink():
                        raise OSError(
                            "external snapshot generation index contains a symlink"
                        )
                    if not candidate_entry.is_dir(follow_symlinks=False):
                        continue
                    candidate = fingerprint_dir / candidate_entry.name
                    generations.append(
                        _external_snapshot_generation_metadata(
                            candidate,
                            quarantined=candidate_entry.name.startswith(
                                _EXTERNAL_SNAPSHOT_GC_PREFIX
                            ),
                        )
                    )
    return generations


def _try_acquire_external_snapshot_gc_lock(generation: Path):
    """Take the generation's exclusive reader fence, or return ``None``."""
    parent_fd = -1
    lease_fd = -1
    handle = None
    lease_name = (
        f".{generation.name}{EXTERNAL_SKILLS_SNAPSHOT_READ_LOCK_SUFFIX}"
    )
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        lease_flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        supports_dir_fd = os.open in getattr(os, "supports_dir_fd", ())
        if supports_dir_fd:
            parent_fd = os.open(generation.parent, directory_flags)
            lease_fd = os.open(
                lease_name,
                lease_flags,
                0o600,
                dir_fd=parent_fd,
            )
        else:
            if generation.is_symlink():
                return None
            lease_path = generation.parent / lease_name
            try:
                lease_stat = lease_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                lease_stat = None
            if lease_stat is not None and stat.S_ISLNK(lease_stat.st_mode):
                return None
            lease_fd = os.open(lease_path, lease_flags, 0o600)
        if not stat.S_ISREG(os.fstat(lease_fd).st_mode):
            return None
        handle = os.fdopen(lease_fd, "a+b")
        lease_fd = -1
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\n")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (BlockingIOError, OSError, PermissionError):
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None
    finally:
        if lease_fd >= 0:
            os.close(lease_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _release_external_snapshot_gc_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, PermissionError, ValueError):
        pass
    finally:
        try:
            handle.close()
        except (OSError, ValueError):
            pass


def _remove_external_snapshot_generation(
    generation: _ExternalSnapshotGeneration,
) -> bool:
    """Quarantine then remove one unreferenced, exclusively fenced generation."""
    handle = _try_acquire_external_snapshot_gc_lock(generation.path)
    if handle is None:
        return False
    quarantine = generation.path
    parent_fd = -1
    original_lease_path = generation.path.parent / (
        f".{generation.path.name}{EXTERNAL_SKILLS_SNAPSHOT_READ_LOCK_SUFFIX}"
    )
    try:
        current = _current_external_snapshot_generation()
        if current is None and _external_catalog_pointer_exists():
            return False
        if current is not None and current == generation.path:
            return False
        if not generation.quarantined:
            quarantine = generation.path.with_name(
                f"{_EXTERNAL_SNAPSHOT_GC_PREFIX}{generation.path.name}-"
                f"{secrets.token_hex(4)}"
            )
            parent_fd = _open_external_directory_at(generation.path.parent)
            os.rename(
                generation.path.name,
                quarantine.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
    except OSError:
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        _release_external_snapshot_gc_lock(handle)

    if quarantine != generation.path:
        try:
            original_lease_path.unlink(missing_ok=True)
        except OSError:
            logger.debug(
                "Could not remove retired external snapshot lease file %s",
                original_lease_path,
                exc_info=True,
            )

    try:
        shutil.rmtree(quarantine)
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning(
            "Could not remove retired external skill snapshot %s",
            quarantine,
            exc_info=True,
        )
        return False
    lease_path = generation.path.parent / (
        f".{generation.path.name}{EXTERNAL_SKILLS_SNAPSHOT_READ_LOCK_SUFFIX}"
    )
    try:
        lease_path.unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "Could not remove retired external snapshot lease file %s",
            lease_path,
            exc_info=True,
        )
    try:
        generation.path.parent.rmdir()
    except OSError:
        pass
    return True


def _enforce_external_snapshot_retention(
    *,
    reserve_generations: int = 0,
    reserve_bytes: int = 0,
    protected_generations: Tuple[Path, ...] = (),
) -> bool:
    """Prune old complete generations without risking current/LKG/readers."""
    allowed_generations = max(
        0,
        _MAX_EXTERNAL_SNAPSHOT_GENERATIONS - max(0, reserve_generations),
    )
    allowed_bytes = max(
        0,
        _MAX_EXTERNAL_SNAPSHOT_TOTAL_BYTES - max(0, reserve_bytes),
    )
    generations = _list_external_snapshot_generations()
    current = _current_external_snapshot_generation()
    if current is None and _external_catalog_pointer_exists():
        return False
    protected = {Path(path) for path in protected_generations if path is not None}
    if current is not None:
        protected.add(current)

    # The newest complete predecessor is the rollback/read-race cushion. It
    # remains protected even when no reader currently holds its shared lease.
    predecessors = sorted(
        (
            item
            for item in generations
            if item.complete
            and not item.quarantined
            and item.path not in protected
        ),
        key=lambda item: item.created_at_ns,
        reverse=True,
    )
    if predecessors:
        protected.add(predecessors[0].path)

    remaining_count = len(generations)
    remaining_bytes = sum(item.materialized_bytes for item in generations)
    for generation in sorted(
        generations,
        key=lambda item: (item.created_at_ns, str(item.path)),
    ):
        if (
            remaining_count <= allowed_generations
            and remaining_bytes <= allowed_bytes
        ):
            break
        if generation.path in protected or not generation.complete:
            continue
        if not _remove_external_snapshot_generation(generation):
            continue
        remaining_count -= 1
        remaining_bytes -= generation.materialized_bytes

    return (
        remaining_count <= allowed_generations
        and remaining_bytes <= allowed_bytes
    )


def _external_catalog_names_digest(names: Set[str]) -> str:
    payload = json.dumps(sorted(names), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _confirm_empty_external_catalog_transition(
    fingerprint: str,
    prior_names: Set[str],
    scan_id: str,
) -> bool:
    """Require two distinct successful empty scans after a non-empty catalog."""
    path = _external_empty_confirmation_path()
    prior_digest = _external_catalog_names_digest(prior_names)
    pending = _read_json_object(path) or {}
    if (
        pending.get("roots_fingerprint") == fingerprint
        and pending.get("prior_names_digest") == prior_digest
        and isinstance(pending.get("scan_id"), str)
        and pending.get("scan_id") != scan_id
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not clear external empty confirmation", exc_info=True)
        return True
    _write_json_object_atomic(
        path,
        {
            "version": _EXTERNAL_CATALOG_VERSION,
            "roots_fingerprint": fingerprint,
            "prior_names_digest": prior_digest,
            "scan_id": scan_id,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return False


def _clear_external_empty_confirmation() -> None:
    try:
        _external_empty_confirmation_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear external empty confirmation", exc_info=True)


def _active_external_scan_backoff(fingerprint: str) -> float:
    payload = _read_json_object(_external_scan_backoff_path())
    if not payload or payload.get("roots_fingerprint") != fingerprint:
        return 0.0
    try:
        retry_after = float(payload.get("retry_after", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return min(
        _EXTERNAL_SCAN_BACKOFF_MAX_SECONDS,
        max(0.0, retry_after - time.time()),
    )


def _record_external_scan_failure(fingerprint: str, reason: str) -> float:
    path = _external_scan_backoff_path()
    prior = _read_json_object(path) or {}
    failures = 0
    if prior.get("roots_fingerprint") == fingerprint:
        try:
            failures = max(0, int(prior.get("failures", 0)))
        except (TypeError, ValueError):
            failures = 0
    failures += 1
    delay = min(
        _EXTERNAL_SCAN_BACKOFF_MAX_SECONDS,
        _EXTERNAL_SCAN_BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 8)),
    )
    _write_json_object_atomic(
        path,
        {
            "version": _EXTERNAL_CATALOG_VERSION,
            "roots_fingerprint": fingerprint,
            "failures": failures,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retry_after": time.time() + delay,
            "reason": reason[:1000],
        },
    )
    return delay


def _clear_external_scan_backoff() -> None:
    try:
        _external_scan_backoff_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear external skill scan backoff", exc_info=True)


_EXTERNAL_SCAN_ORPHAN_PIDS: Set[int] = set()
_EXTERNAL_SCAN_ORPHAN_PROCESSES: Dict[int, Any] = {}


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid in _EXTERNAL_SCAN_ORPHAN_PIDS:
        # Continue to probe: the set is the fallback when the durable lease
        # cannot be written, not a permanent false-positive latch.
        pass
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        # psutil is a core dependency. If a partial installation omitted it,
        # fail closed on Windows: ``os.kill(pid, 0)`` sends CTRL_C_EVENT there
        # and can terminate an unrelated process group. POSIX signal 0 remains
        # a non-mutating fallback.
        if os.name == "nt":
            return True
        try:
            os.kill(pid, 0)  # windows-footgun: ok — POSIX-only branch above
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def _safe_cleanup_external_scan_work_dir(
    raw_path: str,
    raw_cache_dir: Optional[str] = None,
) -> None:
    path = Path(raw_path)
    cache_dir = Path(raw_cache_dir) if raw_cache_dir else _hermes_home() / "cache"
    if path.parent != cache_dir or not path.name.startswith(".external-scan-"):
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not clean external scan work dir %s", path, exc_info=True)


def _schedule_external_scan_work_dir_cleanup(
    raw_path: str,
    *,
    cache_dir: Optional[Path] = None,
) -> None:
    """Remove bounded local staging data without extending the scan deadline."""
    captured_cache_dir = str(cache_dir or (_hermes_home() / "cache"))
    threading.Thread(
        target=_safe_cleanup_external_scan_work_dir,
        args=(raw_path, captured_cache_dir),
        daemon=True,
        name="external-skill-scan-cleanup",
    ).start()


def _record_external_scan_orphan(
    pid: int,
    work_dir: Path,
    *,
    process: Any = None,
) -> None:
    _EXTERNAL_SCAN_ORPHAN_PIDS.add(pid)
    if process is not None:
        # Retain the multiprocessing handle so a child that exits just after
        # the bounded kill grace can still be waitpid()-reaped on the next
        # attempt. A PID-only probe sees Unix zombies as alive forever.
        _EXTERNAL_SCAN_ORPHAN_PROCESSES[pid] = process
    _write_json_object_atomic(
        _external_scan_orphan_path(),
        {
            "version": _EXTERNAL_CATALOG_VERSION,
            "pid": pid,
            "work_dir": str(work_dir),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _active_external_scan_orphan() -> str:
    """Return a reason while a previously killed scan remains unreaped."""
    known_reaped: Set[int] = set()
    for pid in list(_EXTERNAL_SCAN_ORPHAN_PIDS):
        process = _EXTERNAL_SCAN_ORPHAN_PROCESSES.get(pid)
        if process is not None:
            try:
                _join_external_scan_process(process, 0.0)
                if process.is_alive():
                    return f"external skill scan child PID {pid} is still alive"
                process.close()
            except (OSError, ValueError):
                return f"external skill scan child PID {pid} could not be reaped"
            _EXTERNAL_SCAN_ORPHAN_PROCESSES.pop(pid, None)
            _EXTERNAL_SCAN_ORPHAN_PIDS.discard(pid)
            known_reaped.add(pid)
            continue
        if _pid_is_alive(pid):
            return f"external skill scan child PID {pid} is still alive"
        _EXTERNAL_SCAN_ORPHAN_PIDS.discard(pid)

    path = _external_scan_orphan_path()
    if not path.exists():
        return ""
    payload = _read_json_object(path)
    if not payload:
        return "external skill scan orphan lease is invalid"
    try:
        pid = int(payload.get("pid", 0))
    except (TypeError, ValueError):
        return "external skill scan orphan lease has an invalid PID"
    if pid not in known_reaped and _pid_is_alive(pid):
        _EXTERNAL_SCAN_ORPHAN_PIDS.add(pid)
        return f"external skill scan child PID {pid} is still alive"
    work_dir = payload.get("work_dir")
    if isinstance(work_dir, str):
        _schedule_external_scan_work_dir_cleanup(work_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return "external skill scan orphan lease could not be cleared"
    return ""


def _try_acquire_external_scan_file_lock():
    """Acquire the profile-wide scan lease without waiting."""
    path = _external_scan_lock_path()
    handle = None
    descriptor = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            descriptor = -1
            return None
        handle = os.fdopen(descriptor, "a+b")
        descriptor = -1
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\n")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (BlockingIOError, OSError, PermissionError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            handle.close()
        except (AttributeError, OSError):
            pass
        return None


def _release_external_scan_file_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, PermissionError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _new_external_scan_budget() -> Dict[str, int]:
    return {
        "directories": 0,
        "entries": 0,
        "files": 0,
        "skill_files": 0,
        "bytes": 0,
    }


def _count_external_directory(budget: Dict[str, int]) -> None:
    budget["directories"] += 1
    if budget["directories"] > _MAX_EXTERNAL_SCAN_DIRECTORIES:
        raise OSError(
            "external skill scan exceeded the directory safety limit "
            f"({_MAX_EXTERNAL_SCAN_DIRECTORIES})"
        )


def _external_fd_traversal_supported() -> bool:
    """Whether this runtime can walk directories from already-open handles."""
    return (
        os.name != "nt"
        and os.open in getattr(os, "supports_dir_fd", ())
        and os.scandir in getattr(os, "supports_fd", ())
    )


def _open_external_directory_at(
    path_or_name,
    *,
    parent_fd: Optional[int] = None,
) -> int:
    """Open a directory without following its final path component."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if parent_fd is None:
        descriptor = os.open(path_or_name, flags)
    else:
        descriptor = os.open(path_or_name, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"external scan path is not a directory: {path_or_name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_external_root_fd(root: Path) -> int:
    """Open every component of an absolute root through no-follow dirfds."""
    if not root.is_absolute():
        raise OSError(f"configured external skill root must be absolute: {root}")
    descriptor = -1
    try:
        descriptor = _open_external_directory_at(root.anchor)
        for component in root.parts[1:]:
            next_descriptor = _open_external_directory_at(
                component,
                parent_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise OSError(
            "configured external skill root is unavailable or traverses a "
            f"symlink: {root}"
        ) from exc


def _bounded_external_directory_entries_fd(
    directory_fd: int,
    budget: Dict[str, int],
) -> List[Tuple[str, str]]:
    """Return bounded descriptor-relative entries without following links."""
    entries: List[Tuple[str, str]] = []
    _count_external_directory(budget)
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            budget["entries"] += 1
            if budget["entries"] > _MAX_EXTERNAL_SCAN_ENTRIES:
                raise OSError(
                    "external skill scan exceeded the entry safety limit "
                    f"({_MAX_EXTERNAL_SCAN_ENTRIES})"
                )
            if entry.is_symlink():
                entries.append((entry.name, "symlink"))
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append((entry.name, "directory"))
                continue
            if entry.is_file(follow_symlinks=False):
                budget["files"] += 1
                if budget["files"] > _MAX_EXTERNAL_SCAN_FILES:
                    raise OSError(
                        "external skill scan exceeded the file safety limit "
                        f"({_MAX_EXTERNAL_SCAN_FILES})"
                    )
                entries.append((entry.name, "file"))
                continue
            entries.append((entry.name, "special"))
    return entries


def _open_external_regular_file_at(directory_fd: int, name: str) -> Tuple[int, Any]:
    """Open a child file without blocking on or accepting special files."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"external scan path is not a regular file: {name}")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _parse_external_skill_name(content: str, fallback: str) -> str:
    in_frontmatter = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def _read_external_skill_name_at(
    directory_fd: int,
    name: str,
    fallback: str,
) -> str:
    descriptor, _metadata = _open_external_regular_file_at(directory_fd, name)
    try:
        while True:
            try:
                encoded = os.read(descriptor, _MAX_SKILL_FRONTMATTER_BYTES)
                break
            except InterruptedError:
                continue
    finally:
        os.close(descriptor)
    return _parse_external_skill_name(
        encoded.decode("utf-8", errors="replace"),
        fallback,
    )


def _add_external_catalog_name(names: Set[str], name: str) -> None:
    if not name:
        return
    if len(name.encode("utf-8")) > EXTERNAL_SKILLS_MAX_NAME_BYTES:
        raise OSError(
            "external skill scan exceeded the per-name byte safety limit "
            f"({EXTERNAL_SKILLS_MAX_NAME_BYTES})"
        )
    names.add(name)
    if len(names) > EXTERNAL_SKILLS_MAX_CATALOG_NAMES:
        raise OSError(
            "external skill scan exceeded the catalog item safety limit "
            f"({EXTERNAL_SKILLS_MAX_CATALOG_NAMES})"
        )


def _copy_external_snapshot_file_at(
    source_directory_fd: int,
    source_name: str,
    destination: Path,
    budget: Dict[str, int],
) -> None:
    """Copy one descriptor-relative regular file into local staging."""
    source_fd, source_stat = _open_external_regular_file_at(
        source_directory_fd,
        source_name,
    )
    if source_stat.st_size > _MAX_EXTERNAL_MATERIALIZED_FILE_BYTES:
        os.close(source_fd)
        raise OSError(
            "external skill snapshot exceeded the per-file byte safety limit "
            f"({_MAX_EXTERNAL_MATERIALIZED_FILE_BYTES})"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with os.fdopen(source_fd, "rb", closefd=True) as source_handle, destination.open(
            "xb"
        ) as destination_handle:
            source_fd = -1
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > _MAX_EXTERNAL_MATERIALIZED_FILE_BYTES:
                    raise OSError(
                        "external skill snapshot exceeded the per-file byte safety limit "
                        f"({_MAX_EXTERNAL_MATERIALIZED_FILE_BYTES})"
                    )
                if budget["bytes"] + len(chunk) > _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES:
                    raise OSError(
                        "external skill snapshot exceeded the total byte safety limit "
                        f"({_MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES})"
                    )
                destination_handle.write(chunk)
                budget["bytes"] += len(chunk)

            final_stat = os.fstat(source_handle.fileno())
            if (
                final_stat.st_size != source_stat.st_size
                or final_stat.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise OSError(
                    f"external skill file changed while being materialized: {source_name}"
                )
    finally:
        if source_fd >= 0:
            os.close(source_fd)
    try:
        destination.chmod(stat.S_IMODE(source_stat.st_mode) & 0o700)
    except OSError:
        pass


def _materialize_external_skill_package_fd(
    source_fd: int,
    destination: Path,
    budget: Dict[str, int],
    *,
    depth: int,
    require_skill_md: bool = False,
) -> None:
    """Copy a package through no-follow descriptors, one directory at a time."""
    if depth > _MAX_EXTERNAL_SCAN_DEPTH:
        raise OSError(
            "external skill scan exceeded the depth safety limit "
            f"({_MAX_EXTERNAL_SCAN_DEPTH})"
        )
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = _bounded_external_directory_entries_fd(source_fd, budget)
    if require_skill_md and dict(entries).get("SKILL.md") != "file":
        raise OSError("external skill package changed before materialization")
    for name, kind in entries:
        if kind == "symlink" or kind == "special":
            continue
        destination_entry = destination / name
        if kind == "directory":
            if name in EXCLUDED_SKILL_DIRS:
                continue
            child_fd = _open_external_directory_at(name, parent_fd=source_fd)
            try:
                _materialize_external_skill_package_fd(
                    child_fd,
                    destination_entry,
                    budget,
                    depth=depth + 1,
                )
            finally:
                os.close(child_fd)
        elif kind == "file":
            _copy_external_snapshot_file_at(
                source_fd,
                name,
                destination_entry,
                budget,
            )


def _scan_external_directory_fd(
    directory_fd: int,
    *,
    directory_name: str,
    relative: Path,
    destination_root: Optional[Path],
    names: Set[str],
    budget: Dict[str, int],
    depth: int,
) -> None:
    if depth > _MAX_EXTERNAL_SCAN_DEPTH:
        raise OSError(
            "external skill scan exceeded the depth safety limit "
            f"({_MAX_EXTERNAL_SCAN_DEPTH})"
        )
    entries = _bounded_external_directory_entries_fd(directory_fd, budget)
    kinds = {name: kind for name, kind in entries}
    if kinds.get("SKILL.md") == "file":
        budget["skill_files"] += 1
        if budget["skill_files"] > _MAX_EXTERNAL_SKILL_FILES:
            raise OSError(
                "external skill scan exceeded the skill-file safety limit "
                f"({_MAX_EXTERNAL_SKILL_FILES})"
            )
        _add_external_catalog_name(names, directory_name)
        _add_external_catalog_name(
            names,
            _read_external_skill_name_at(directory_fd, "SKILL.md", ""),
        )
        if destination_root is not None:
            _materialize_external_skill_package_fd(
                directory_fd,
                destination_root / relative,
                budget,
                depth=depth,
                require_skill_md=True,
            )
        return

    if destination_root is not None and kinds.get("DESCRIPTION.md") == "file":
        _copy_external_snapshot_file_at(
            directory_fd,
            "DESCRIPTION.md",
            destination_root / relative / "DESCRIPTION.md",
            budget,
        )
    for name, kind in entries:
        if kind != "directory" or name in EXCLUDED_SKILL_DIRS:
            continue
        # Classification above is only a hint. The descriptor-relative,
        # no-follow open is the authority and fails the entire scan if a
        # directory was replaced by a symlink at the race boundary.
        child_fd = _open_external_directory_at(name, parent_fd=directory_fd)
        try:
            _scan_external_directory_fd(
                child_fd,
                directory_name=name,
                relative=relative / name,
                destination_root=destination_root,
                names=names,
                budget=budget,
                depth=depth + 1,
            )
        finally:
            os.close(child_fd)


def _scan_external_roots_fd(
    roots: Tuple[str, ...],
    materialized_root: Optional[Path],
    budget: Optional[Dict[str, int]] = None,
) -> Set[str]:
    external_names: Set[str] = set()
    if budget is None:
        budget = _new_external_scan_budget()

    if materialized_root is not None:
        materialized_root.mkdir(parents=True, exist_ok=False, mode=0o700)

    for root_index, root_text in enumerate(roots):
        root = Path(root_text)
        root_fd = _open_external_root_fd(root)
        destination_root = None
        try:
            if materialized_root is not None:
                destination_root = materialized_root / f"root-{root_index:04d}"
                destination_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            _scan_external_directory_fd(
                root_fd,
                directory_name=root.name,
                relative=Path(),
                destination_root=destination_root,
                names=external_names,
                budget=budget,
                depth=0,
            )
        finally:
            os.close(root_fd)
    return external_names


def _scan_external_roots_path_fallback(
    roots: Tuple[str, ...],
    materialized_root: Optional[Path] = None,
    budget: Optional[Dict[str, int]] = None,
) -> Set[str]:
    """Fail closed when race-safe descriptor traversal is unavailable.

    A path-based walk can check every observed component for symlinks, but it
    cannot prevent a directory from being replaced by a symlink between that
    check and descent. External catalog reconciliation is optional, so an
    explicit defer is safer than publishing a materialization from outside the
    configured root. A native Windows handle-relative walker can replace this
    boundary in a future release.
    """
    del roots, materialized_root, budget
    raise OSError(
        "race-safe descriptor-relative external skill traversal is unavailable "
        "on this platform"
    )


def _scan_external_roots(
    roots: Tuple[str, ...],
    materialized_root: Optional[Path] = None,
    *,
    budget: Optional[Dict[str, int]] = None,
) -> Set[str]:
    """Boundedly scan roots and optionally materialize complete skill packages.

    Supported runtimes traverse through directory descriptors so a component
    cannot be swapped to a symlink between classification and descent. The
    entire operation still lives in the killable child process on all
    platforms; runtimes without descriptor-relative ``scandir`` fail closed.
    """
    if _external_fd_traversal_supported():
        return _scan_external_roots_fd(roots, materialized_root, budget)
    return _scan_external_roots_path_fallback(roots, materialized_root, budget)


def _write_external_scan_result(path: Path, payload: Dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > EXTERNAL_SKILLS_MAX_CATALOG_BYTES:
        raise OSError(
            "external skill scan result exceeded the total byte safety limit "
            f"({EXTERNAL_SKILLS_MAX_CATALOG_BYTES})"
        )
    with path.open("xb") as handle:
        handle.write(encoded)


def _external_scan_worker(
    result_path: str,
    roots: Tuple[str, ...],
    materialized_root: str,
) -> None:
    """Child entrypoint: all external I/O, with a bounded local result file."""
    path = Path(result_path)
    try:
        budget = _new_external_scan_budget()
        names = _scan_external_roots(
            roots,
            Path(materialized_root),
            budget=budget,
        )
        _write_external_scan_result(
            path,
            {
                "ok": True,
                "names": sorted(names),
                "materialized_bytes": budget["bytes"],
            },
        )
    except Exception as exc:
        try:
            if path.exists():
                path.unlink()
            _write_external_scan_result(
                path,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]},
            )
        except Exception:
            pass


def _join_external_scan_process(process, wait_seconds: float) -> None:
    process.join(max(0.0, wait_seconds))


def _terminate_external_scan_process(process, *, deadline: Optional[float] = None) -> bool:
    """Terminate, then kill, without exceeding the caller's hard deadline."""
    try:
        process.terminate()
    except (OSError, ValueError):
        pass
    terminate_wait = _EXTERNAL_SCAN_TERMINATE_GRACE_SECONDS
    if deadline is not None:
        terminate_wait = min(terminate_wait, max(0.0, deadline - time.monotonic()))
    _join_external_scan_process(process, terminate_wait)
    if process.is_alive():
        try:
            process.kill()
        except (AttributeError, OSError, ValueError):
            pass
        kill_wait = _EXTERNAL_SCAN_KILL_GRACE_SECONDS
        if deadline is not None:
            kill_wait = min(kill_wait, max(0.0, deadline - time.monotonic()))
        _join_external_scan_process(process, kill_wait)
    return not process.is_alive()


def _read_external_scan_result(path: Path) -> Dict[str, Any]:
    try:
        encoded = _read_bounded_regular_file(
            path,
            EXTERNAL_SKILLS_MAX_CATALOG_BYTES,
        )
    except OSError as exc:
        if "exceeds" in str(exc):
            raise RuntimeError(
                "external skill scan returned an oversized result"
            ) from exc
        raise RuntimeError("external skill scan exited without a result") from exc
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError("external skill scan returned an invalid result") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        reason = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(reason or "external skill scan failed")
    names = payload.get("names")
    if (
        not isinstance(names, list)
        or len(names) > EXTERNAL_SKILLS_MAX_CATALOG_NAMES
        or any(
            not isinstance(name, str)
            or not name
            or len(name.encode("utf-8")) > EXTERNAL_SKILLS_MAX_NAME_BYTES
            for name in names
        )
    ):
        raise RuntimeError("external skill scan returned an invalid catalog")
    materialized_bytes = payload.get("materialized_bytes")
    if not isinstance(materialized_bytes, int) or not (
        0 <= materialized_bytes <= _MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES
    ):
        raise RuntimeError(
            "external skill scan returned an invalid materialized byte count"
        )
    return payload


def _run_external_scan_subprocess(
    roots: Tuple[str, ...], timeout_seconds: float
) -> ExternalScanResult:
    """Scan through a result file under one hard, at-most-10-second deadline."""
    timeout_seconds = max(
        _MIN_EXTERNAL_SCAN_TIMEOUT_SECONDS,
        min(_MAX_EXTERNAL_SCAN_TIMEOUT_SECONDS, float(timeout_seconds)),
    )
    deadline = time.monotonic() + timeout_seconds
    cache_dir = _hermes_home() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=".external-scan-", dir=cache_dir))
    result_path = work_dir / "result.json"
    materialized_staging = work_dir / "materialized"
    scan_id = f"{time.time_ns()}-{secrets.token_hex(8)}"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_external_scan_worker,
        args=(str(result_path), roots, str(materialized_staging)),
        daemon=True,
        name="hermes-external-skill-scan",
    )
    keep_work_dir = False
    try:
        process.start()
        # Reserve the final half-second (or 10% for short configured bounds)
        # for terminate/kill escalation and local bounded-result validation.
        reserve = min(1.0, max(0.2, timeout_seconds * 0.2))
        _join_external_scan_process(
            process,
            max(0.0, deadline - time.monotonic() - reserve),
        )
        if process.is_alive():
            metadata_reserve = min(0.1, timeout_seconds * 0.05)
            reaped = _terminate_external_scan_process(
                process,
                deadline=deadline - metadata_reserve,
            )
            if not reaped:
                keep_work_dir = True
                try:
                    _record_external_scan_orphan(
                        int(process.pid),
                        work_dir,
                        process=process,
                    )
                except Exception:
                    logger.error(
                        "Could not persist unreaped external scan child PID %s",
                        getattr(process, "pid", None),
                        exc_info=True,
                    )
            suffix = "" if reaped else "; child did not reap after kill"
            raise TimeoutError(
                f"external skill scan exceeded {timeout_seconds:g}s{suffix}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"external skill scan exceeded {timeout_seconds:g}s")
        payload = _read_external_scan_result(result_path)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"external skill scan exceeded {timeout_seconds:g}s")
        names = set(payload["names"])
        materialized_bytes = payload["materialized_bytes"]

        fingerprint = _external_catalog_fingerprint(roots)
        generation_relative = Path(fingerprint) / scan_id
        _write_external_snapshot_complete_marker(
            materialized_staging,
            fingerprint=fingerprint,
            scan_id=scan_id,
            materialized_bytes=materialized_bytes,
        )
        _publish_external_materialized_generation(
            materialized_staging,
            fingerprint=fingerprint,
            scan_id=scan_id,
        )
        materialized_roots = tuple(
            (generation_relative / f"root-{index:04d}").as_posix()
            for index in range(len(roots))
        )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"external skill scan exceeded {timeout_seconds:g}s")
        return ExternalScanResult(
            names,
            materialized_roots,
            scan_id,
            materialized_bytes,
        )
    finally:
        if not keep_work_dir:
            _schedule_external_scan_work_dir_cleanup(
                str(work_dir),
                cache_dir=cache_dir,
            )
            try:
                process.close()
            except (AttributeError, ValueError):
                pass


def _external_catalog_fallback(
    fingerprint: str,
    *,
    status: str,
    reason: str,
) -> ExternalSkillIndex:
    snapshot = _load_external_catalog_snapshot(fingerprint)
    # An empty successful scan is valid, but an empty snapshot is not safe to
    # publish as the answer to a failed scan: doing so recreates the original
    # shadowing bug. Defer the entire bundled sync instead.
    if snapshot:
        logger.warning(
            "External skill catalog %s (%s); using last-known-good snapshot",
            status,
            reason,
        )
        return ExternalSkillIndex(
            snapshot,
            status="last_known_good",
            reason=reason,
        )
    raise ExternalSkillIndexUnavailable(reason)


def _build_external_skill_index() -> Set[str]:
    """Return a safe external skill catalog without trusting failed scans.

    Foreground callers scan configured roots in a killable child. Concurrent
    callers are single-flighted across threads and processes, repeated
    failures back off, and only successful scans atomically replace the
    last-known-good snapshot. Gateway callers consume only that local snapshot.
    """
    try:
        roots, timeout_seconds = _configured_external_scan_settings()
    except ExternalSkillIndexUnavailable:
        raise
    except Exception as exc:
        raise ExternalSkillIndexUnavailable(
            f"external skill scan configuration could not be read: {exc}"
        ) from exc
    if not roots:
        return ExternalSkillIndex(status="disabled")

    # The always-on gateway is a consumer of the atomically published local
    # catalog, never the supervisor of external/NAS traversal.  Returning a
    # non-fresh status makes sync_skills() defer before any local mutation;
    # a separately supervised foreground sync must perform reconciliation.
    if os.environ.get("_HERMES_GATEWAY") == "1":
        snapshot = get_gateway_external_skills_snapshot(
            roots,
            hermes_home=_hermes_home(),
        )
        if snapshot is None:
            raise ExternalSkillIndexUnavailable(
                "gateway has no validated local external skill snapshot; "
                "run a separately supervised skill sync"
            )
        names, _materialized_roots = snapshot
        return ExternalSkillIndex(
            names,
            status="gateway_snapshot",
            reason=(
                "gateway external catalog reconciliation requires a separately "
                "supervised skill sync"
            ),
        )

    fingerprint = _external_catalog_fingerprint(roots)
    if not _external_fd_traversal_supported():
        return _external_catalog_fallback(
            fingerprint,
            status="cannot scan safely on this platform",
            reason=(
                "race-safe descriptor-relative external skill traversal is "
                "unavailable on this platform"
            ),
        )
    orphan_reason = _active_external_scan_orphan()
    if orphan_reason:
        return _external_catalog_fallback(
            fingerprint,
            status="has an unreaped child",
            reason=orphan_reason,
        )
    backoff_remaining = _active_external_scan_backoff(fingerprint)
    if backoff_remaining > 0:
        return _external_catalog_fallback(
            fingerprint,
            status="is in backoff",
            reason=f"retry deferred for {backoff_remaining:.1f}s",
        )

    if not _EXTERNAL_SCAN_THREAD_LOCK.acquire(blocking=False):
        return _external_catalog_fallback(
            fingerprint,
            status="is already running",
            reason="another external skill scan is already in progress",
        )

    file_lock = None
    try:
        file_lock = _try_acquire_external_scan_file_lock()
        if file_lock is None:
            return _external_catalog_fallback(
                fingerprint,
                status="is already running",
                reason="another process owns the external skill scan lease",
            )

        # A different process could have recorded an orphan or backoff while
        # we waited for the in-process lock; re-check after acquiring the
        # profile-wide lease.
        orphan_reason = _active_external_scan_orphan()
        if orphan_reason:
            return _external_catalog_fallback(
                fingerprint,
                status="has an unreaped child",
                reason=orphan_reason,
            )
        backoff_remaining = _active_external_scan_backoff(fingerprint)
        if backoff_remaining > 0:
            return _external_catalog_fallback(
                fingerprint,
                status="is in backoff",
                reason=f"retry deferred for {backoff_remaining:.1f}s",
            )

        # Reserve one worst-case materialization slot before touching an
        # external root. If old complete generations cannot be reclaimed
        # (active reader lease, cleanup failure, or unsafe metadata), defer
        # with the current catalog intact instead of allowing disk growth.
        try:
            retention_ready = _enforce_external_snapshot_retention(
                reserve_generations=1,
                reserve_bytes=_MAX_EXTERNAL_MATERIALIZED_TOTAL_BYTES,
            )
        except Exception as exc:
            return _external_catalog_fallback(
                fingerprint,
                status="could not enforce snapshot retention",
                reason=f"{type(exc).__name__}: {exc}",
            )
        if not retention_ready:
            return _external_catalog_fallback(
                fingerprint,
                status="has no safe snapshot capacity",
                reason=(
                    "external snapshot retention could not reserve one "
                    "bounded materialization slot"
                ),
            )

        prior_generation = _current_external_snapshot_generation()
        try:
            scan_result = _run_external_scan_subprocess(roots, timeout_seconds)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            try:
                retry_delay = _record_external_scan_failure(fingerprint, reason)
                reason = f"{reason}; retry in {retry_delay:.0f}s"
            except Exception:
                logger.debug(
                    "Could not persist external skill scan backoff",
                    exc_info=True,
                )
            return _external_catalog_fallback(
                fingerprint,
                status="failed",
                reason=reason,
            )

        names = scan_result.names
        prior_names = _load_external_catalog_snapshot(fingerprint)
        if not names and prior_names:
            try:
                empty_confirmed = _confirm_empty_external_catalog_transition(
                    fingerprint,
                    prior_names,
                    scan_result.scan_id,
                )
            except Exception as exc:
                return _external_catalog_fallback(
                    fingerprint,
                    status="could not persist empty-catalog confirmation",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            if not empty_confirmed:
                _clear_external_scan_backoff()
                return _external_catalog_fallback(
                    fingerprint,
                    status="reported empty once",
                    reason="a second independent successful scan is required",
                )
        else:
            _clear_external_empty_confirmation()

        try:
            _publish_external_catalog_snapshot(
                fingerprint,
                roots,
                names,
                scan_result.materialized_roots,
            )
            _clear_external_scan_backoff()
        except Exception as exc:
            # A scan is not authoritative to gateway consumers until its
            # materialization pointer is atomically durable. Never mutate the
            # local bundled tree against a result the gateway cannot consume.
            logger.warning(
                "External skill catalog scan succeeded but its snapshot could "
                "not be persisted",
                exc_info=True,
            )
            return _external_catalog_fallback(
                fingerprint,
                status="could not publish",
                reason=f"{type(exc).__name__}: {exc}",
            )

        # Publication is already durable and authoritative at this point.
        # Cleanup is deliberately best-effort: a failure is logged, while the
        # pre-scan reservation guarantees this success did not exceed the hard
        # retained-generation/content ceiling. Never roll back or invalidate
        # the newly published catalog because retirement failed.
        try:
            retained = _enforce_external_snapshot_retention(
                protected_generations=(prior_generation,)
                if prior_generation is not None
                else (),
            )
            if not retained:
                logger.warning(
                    "External skill snapshot cleanup left protected or "
                    "unremovable generations at the retention boundary"
                )
        except Exception:
            logger.warning(
                "External skill snapshot cleanup failed after publication",
                exc_info=True,
            )
        return ExternalSkillIndex(names, status="fresh")
    finally:
        _release_external_scan_file_lock(file_lock)
        _EXTERNAL_SCAN_THREAD_LOCK.release()


def _read_manifest() -> Dict[str, str]:
    """
    Read the manifest as a dict of {skill_name: origin_hash}.

    Handles both v1 (plain names) and v2 (name:hash) formats.
    v1 entries get an empty hash string which triggers migration on next sync.
    """
    if not _manifest_file().exists():
        return {}
    try:
        result = {}
        for line in _manifest_file().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                # v2 format: name:hash
                name, _, hash_val = line.partition(":")
                result[name.strip()] = hash_val.strip()
            else:
                # v1 format: plain name — empty hash triggers migration
                result[line] = ""
        return result
    except (OSError, IOError):
        return {}


def _read_suppressed_names() -> set:
    """Built-in skills the curator pruned — must NOT be re-seeded on sync.

    Delegates to ``tools.skill_usage`` (single source of truth) and falls back
    to reading ``~/.hermes/skills/.curator_suppressed`` directly if that import
    is unavailable in a packaged/update context.
    """
    try:
        from tools.skill_usage import read_suppressed_names

        return read_suppressed_names()
    except Exception:
        path = _skills_dir() / ".curator_suppressed"
        if not path.exists():
            return set()
        names = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    names.add(line)
        except OSError:
            pass
        return names


def _write_manifest(entries: Dict[str, str]):
    """Write the manifest file atomically in v2 format (name:hash).

    Uses the shared atomic writer so an existing manifest's permission
    bits (and owner, best-effort) survive the replace instead of being
    reset to mkstemp's 0600 — the same mode-preservation contract as the
    skill manager's document writes.
    """
    _manifest_file().parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items())) + "\n"

    try:
        atomic_write_text(
            _manifest_file(),
            data,
            tmp_prefix=".bundled_manifest_",
            preserve_mode=True,
        )
    except Exception as e:
        logger.debug("Failed to write skills manifest %s: %s", _manifest_file(), e, exc_info=True)


def _read_skill_name(
    skill_md: Path,
    fallback: str,
    *,
    raise_on_error: bool = False,
) -> str:
    """Read the name field from SKILL.md YAML frontmatter, falling back to *fallback*."""
    file_descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_descriptor = os.open(skill_md, flags)
        source_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"skill metadata is not a regular file: {skill_md}")
        content = os.read(file_descriptor, _MAX_SKILL_FRONTMATTER_BYTES).decode(
            "utf-8",
            errors="replace",
        )
    except OSError:
        if raise_on_error:
            raise
        return fallback
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    return _parse_external_skill_name(content, fallback)


def _discover_bundled_skills(bundled_dir: Path) -> List[Tuple[str, Path]]:
    """
    Find all SKILL.md files in the bundled directory.
    Returns list of (skill_name, skill_directory_path) tuples.
    """
    skills = []
    if not bundled_dir.exists():
        return skills

    for skill_md in bundled_dir.rglob("SKILL.md"):
        # Exclusions apply inside the bundled tree. The install prefix itself
        # may legitimately contain names such as ``venv`` or ``site-packages``;
        # treating those parent components as skill content makes every wheel
        # install discover zero bundled skills.
        if is_excluded_skill_path(
            skill_md.relative_to(bundled_dir), root=bundled_dir
        ):
            continue
        skill_dir = skill_md.parent
        skill_name = _read_skill_name(skill_md, skill_dir.name)
        skills.append((skill_name, skill_dir))

    return skills


def _compute_relative_dest(skill_dir: Path, bundled_dir: Path) -> Path:
    """
    Compute the destination path in the skills dir preserving the category structure.
    e.g., bundled/skills/mlops/axolotl -> ~/.hermes/skills/mlops/axolotl
    """
    rel = skill_dir.relative_to(bundled_dir)
    return _skills_dir() / rel


def _dir_hash(directory: Path) -> str:
    """Compute a hash of all file contents in a directory for change detection."""
    hasher = hashlib.md5()
    try:
        for fpath in sorted(directory.rglob("*")):
            if fpath.is_file():
                rel = fpath.relative_to(directory)
                hasher.update(str(rel).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    except (OSError, IOError):
        pass
    return hasher.hexdigest()


def _safe_rel_install_path(path: Path, base: Path) -> str:
    """Return a normalized relative POSIX path, rejecting traversal/absolute paths."""
    rel = path.relative_to(base)
    posix = rel.as_posix()
    pure = PurePosixPath(posix)
    parts = [part for part in pure.parts if part not in {"", "."}]
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe optional skill path: {posix}")
    return "/".join(parts)


def _skill_file_list(skill_dir: Path) -> List[str]:
    """List files inside a skill directory in lock-file format."""
    files: List[str] = []
    for fpath in sorted(skill_dir.rglob("*")):
        if fpath.is_file():
            files.append(fpath.relative_to(skill_dir).as_posix())
    return files


def _content_hash(directory: Path) -> str:
    """Return the same hash style the skills hub lock uses, falling back locally."""
    try:
        from tools.skills_guard import content_hash

        return content_hash(directory)
    except Exception:
        # Hashing is provenance metadata only; keep sync resilient if guard
        # dependencies are unavailable in a packaged/update context.
        return _dir_hash(directory)


def _optional_skill_index() -> Dict[str, Tuple[str, str, Path]]:
    """Return official optional skills keyed by folder name and frontmatter name.

    Values are ``(folder_name, install_path, source_dir)``. Multiple keys may
    point to the same skill so callers can accept either the folder slug used
    by the hub lock or the user-facing frontmatter name.
    """
    optional_dir = _get_optional_dir()
    index: Dict[str, Tuple[str, str, Path]] = {}
    if not optional_dir.exists():
        return index
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(
            skill_md.relative_to(optional_dir), root=optional_dir
        ):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError:
            continue
        folder_name = src.name
        frontmatter_name = _read_skill_name(skill_md, folder_name)
        value = (folder_name, install_path, src)
        index[folder_name] = value
        index[frontmatter_name] = value
    return index


def _move_to_restore_backup(path: Path, backup_root: Path) -> str:
    """Move an existing skill directory into a restore backup, preserving rel path."""
    rel = path.relative_to(_skills_dir())
    target = backup_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        suffix = 1
        while target.with_name(f"{target.name}-{suffix}").exists():
            suffix += 1
        target = target.with_name(f"{target.name}-{suffix}")
    shutil.move(str(path), str(target))
    return rel.as_posix()


def restore_official_optional_skill(name: str, *, restore: bool = False) -> dict:
    """Restore one or all official optional skills from repo source.

    ``restore=False`` only performs exact-match provenance backfill. ``restore=True``
    repairs already-mutated/reorganized skills by backing up matching active
    copies and copying the official optional source into its canonical path.
    """
    index = _optional_skill_index()
    if not index:
        return {"ok": False, "message": "No official optional skills directory found.", "restored": [], "backfilled": [], "backed_up": []}

    targets = sorted(set(index.values()), key=lambda item: item[1]) if name in {"all", "*"} else []
    if not targets:
        target = index.get(name)
        if target is None:
            return {"ok": False, "message": f"Official optional skill not found: {name}", "restored": [], "backfilled": [], "backed_up": []}
        targets = [target]

    restored: List[str] = []
    backed_up: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = _skills_dir() / ".restore-backups" / f"official-optional-{timestamp}"

    for folder_name, install_path, src in targets:
        dest = _skills_dir() / Path(*install_path.split("/"))
        src_hash = _dir_hash(src)
        canonical_ok = dest.exists() and _dir_hash(dest) == src_hash

        # Find already-active copies of this official skill by frontmatter name
        # or folder slug, even if curator moved it into another category.
        src_frontmatter = _read_skill_name(src / "SKILL.md", folder_name)
        matches: List[Path] = []
        if _skills_dir().exists():
            for skill_md in sorted(_skills_dir().rglob("SKILL.md")):
                if is_excluded_skill_path(skill_md):
                    continue
                candidate = skill_md.parent
                try:
                    candidate.relative_to(_skills_dir())
                except ValueError:
                    continue
                candidate_name = _read_skill_name(skill_md, candidate.name)
                if candidate == dest:
                    continue
                if candidate.name == folder_name or candidate_name in {folder_name, src_frontmatter}:
                    matches.append(candidate)

        if restore:
            for match in matches:
                if match.exists():
                    backed_up.append(_move_to_restore_backup(match, backup_root))
            if dest.exists() and not canonical_ok:
                backed_up.append(_move_to_restore_backup(dest, backup_root))
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest)
                restored.append(folder_name)
        elif not canonical_ok:
            continue

    backfilled = _backfill_optional_provenance(quiet=True)
    return {
        "ok": True,
        "message": "Official optional skill repair complete.",
        "restored": restored,
        "backfilled": backfilled,
        "backed_up": backed_up,
        "backup_dir": str(backup_root) if backed_up else "",
    }


def _index_installed_skill_dirs_by_name() -> Dict[str, List[Path]]:
    """Index installed skills by directory name with one active-tree scan."""
    index: Dict[str, List[Path]] = {}
    if not _skills_dir().exists():
        return index
    for skill_md in _skills_dir().rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        candidate = skill_md.parent
        # Never reach outside the skills tree (symlinked/external dirs).
        try:
            candidate.resolve().relative_to(_skills_dir().resolve())
        except (OSError, ValueError):
            continue
        index.setdefault(candidate.name, []).append(candidate)
    return index


def _find_installed_skill_dir_by_name(
    skill_dir_name: str,
    installed_index: Optional[Dict[str, List[Path]]] = None,
) -> Optional[Path]:
    """Locate an installed skill directory by its directory name.

    Used only as a fallback when the repo-derived install path doesn't exist in
    the active tree (upstream recategorized the skill after it was installed).
    Returns None when there is no match, or when the name is AMBIGUOUS — two
    skills sharing a directory name give us no basis to pick one, and guessing
    would write provenance onto the wrong skill. The caller still verifies a
    byte-identical content hash before recording anything.
    """
    if not skill_dir_name or not _skills_dir().exists():
        return None
    if installed_index is None:
        installed_index = _index_installed_skill_dirs_by_name()
    matches = installed_index.get(skill_dir_name, [])
    if len(matches) != 1:
        return None
    return matches[0]


def _backfill_optional_provenance(quiet: bool = False) -> List[str]:
    """Mark already-present official optional skills as hub-installed.

    This covers the migration case where a skill used to be bundled (or was
    manually copied into the active skills tree) and later lives under
    optional-skills/. If the active copy is byte-identical to the official
    optional source, record official hub provenance without copying or
    reinstalling anything. Modified/local skills are left alone.
    """
    optional_dir = _get_optional_dir()
    if not optional_dir.exists():
        return []

    lock_path = _skills_dir() / ".hub" / "lock.json"
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {"version": 1, "installed": {}}
    except (json.JSONDecodeError, OSError):
        data = {"version": 1, "installed": {}}
    installed = data.setdefault("installed", {})
    existing_paths = {
        entry.get("install_path")
        for entry in installed.values()
        if isinstance(entry, dict)
    }

    backfilled: List[str] = []
    changed = False
    installed_dir_index: Optional[Dict[str, List[Path]]] = None
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError as e:
            logger.debug("Skipping optional skill with unsafe path %s: %s", src, e)
            continue
        lock_name = src.name
        if lock_name in installed or install_path in existing_paths:
            continue
        dest = _skills_dir() / Path(*install_path.split("/"))
        if not dest.exists() or not dest.is_dir():
            # The active tree may hold the same skill under a DIFFERENT
            # category path than the repo uses — categories get reorganized
            # upstream (e.g. mlops/chroma → mlops/vector-databases/chroma)
            # while the already-installed copy keeps its old location. A
            # path-only lookup misses every one of those, so provenance repair
            # silently skips them forever. Fall back to a unique
            # same-directory-name match anywhere in the tree, then still
            # require a byte-identical hash below before claiming provenance.
            if installed_dir_index is None:
                installed_dir_index = _index_installed_skill_dirs_by_name()
            dest = _find_installed_skill_dir_by_name(src.name, installed_dir_index)
            if dest is None:
                continue
            try:
                install_path = _safe_rel_install_path(dest, _skills_dir())
            except ValueError as e:
                logger.debug("Skipping relocated optional skill %s: %s", dest, e)
                continue
        if install_path in existing_paths:
            continue
        if _dir_hash(dest) != _dir_hash(src):
            continue

        timestamp = datetime.now(timezone.utc).isoformat()
        installed[lock_name] = {
            "source": "official",
            "identifier": f"official/{install_path}",
            "trust_level": "builtin",
            "scan_verdict": "backfilled",
            "content_hash": _content_hash(dest),
            "install_path": install_path,
            "files": _skill_file_list(dest),
            "metadata": {"backfilled_from": "optional-skills"},
            "installed_at": timestamp,
            "updated_at": timestamp,
        }
        existing_paths.add(install_path)
        backfilled.append(lock_name)
        changed = True
        if not quiet:
            print(f"  = {lock_name} (official optional provenance backfilled)")

    if changed:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-write can't silently wipe all provenance
        # via the JSONDecodeError fallback above (which resets `installed` to
        # an empty dict).
        import tempfile

        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=str(lock_path.parent),
            prefix=".lock_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, lock_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return backfilled


def _read_hub_install_paths() -> Set[str]:
    """Return install paths recorded in the skills-hub lock, as POSIX strings.

    Hub-installed skills are owned by the hub (``hermes skills uninstall``),
    never by bundled sync. Rename recovery must not move them even when their
    content happens to match a bundled origin hash, or the lock's
    ``install_path`` would point at a directory that no longer exists.
    """
    lock_path = _skills_dir() / ".hub" / "lock.json"
    if not lock_path.exists():
        return set()
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    paths: Set[str] = set()
    for entry in (data.get("installed") or {}).values():
        if isinstance(entry, dict):
            install_path = entry.get("install_path")
            if install_path:
                paths.add(str(install_path).strip("/"))
    return paths


def _index_active_skills() -> Dict[str, List[Path]]:
    """Index every skill in the user's tree by frontmatter name.

    Returns ``{skill_name: [skill_dir, ...]}``. Used by rename recovery to
    locate a bundled skill that upstream moved to a new category/directory.
    """
    index: Dict[str, List[Path]] = {}
    if not _skills_dir().exists():
        return index
    for skill_md in _skills_dir().rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        skill_dir = skill_md.parent
        name = _read_skill_name(skill_md, skill_dir.name)
        index.setdefault(name, []).append(skill_dir)
    return index


def _recover_renamed_skill(
    skill_name: str,
    origin_hash: str,
    dest: Path,
    active_index: Dict[str, List[Path]],
    hub_paths: Set[str],
    quiet: bool,
) -> Optional[str]:
    """Move a bundled skill's stale copy to its new canonical path.

    When upstream RENAMES or RECATEGORIZES a bundled skill, the manifest key
    (frontmatter name) still matches but ``dest`` is a brand-new path that does
    not exist yet. Without recovery, ``sync_skills()`` falls through to its
    "in manifest but not on disk" branch and misreads the skill as
    *user-deleted*: the old directory is stranded forever and never receives
    another update.

    A stale copy is only moved when it is byte-identical to ``origin_hash`` —
    the hash recorded the last time sync wrote that skill — which proves the
    directory is the copy *we* placed there rather than the user's own work.
    Anything else (user-edited, hub-installed) is left untouched.

    Returns the relative source path when a move happened, else ``None``.
    """
    if not origin_hash:
        return None

    for candidate in active_index.get(skill_name, []):
        if candidate == dest or not candidate.is_dir():
            continue
        try:
            rel = candidate.relative_to(_skills_dir()).as_posix()
        except ValueError:
            continue
        # Never relocate a hub-installed skill — the hub owns its path.
        if rel in hub_paths:
            continue
        if _dir_hash(candidate) != origin_hash:
            # User customized the copy at the old path. Moving it would edit
            # their work; leaving it avoids a duplicate-name collision. Warn
            # so they can migrate deliberately.
            if not quiet:
                print(
                    f"  ⚠ {skill_name}: upstream moved this skill to "
                    f"{dest.relative_to(_skills_dir()).as_posix()}, but your "
                    f"modified copy at {rel} was kept — it will not receive "
                    f"updates. Run `hermes skills reset {skill_name} --restore` "
                    f"to move to the new location."
                )
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(dest))
        except (OSError, IOError):
            logger.warning(
                "Could not relocate renamed skill %s -> %s", candidate, dest,
                exc_info=True,
            )
            return None
        logger.info("Relocated renamed bundled skill: %s -> %s", candidate, dest)
        if not quiet:
            print(f"  → {skill_name} (moved {rel} → {dest.relative_to(_skills_dir()).as_posix()})")
        return rel
    return None


def sync_skills(quiet: bool = False) -> dict:
    """
    Sync bundled skills into ~/.hermes/skills/ using the manifest.

    Returns:
        dict with keys: copied (list), updated (list), skipped (int),
                        user_modified (list), cleaned (list), total_bundled (int)

    Raises:
        ExternalSkillIndexUnavailable: A configured external catalog could
            not be reconciled from a fresh, authoritative scan.
    """
    # Opt-out: a profile (named or the default ~/.hermes) that wrote the
    # .no-bundled-skills marker gets zero bundled-skill seeding — EXCEPT the
    # essential skills (agent/skill_utils.ESSENTIAL_SKILLS). The
    # ``hermes-agent`` skill is the agent's own operating manual and the
    # system prompt always points at it, so even a Blank Slate / --no-skills
    # profile keeps that one skill. Returning the empty-result shape with
    # skipped_opt_out lets callers report "opted out" instead of
    # "synced 0 / failed". This is the default-profile counterpart to
    # seed_profile_skills()'s marker check for named profiles.
    essential_only = (_hermes_home() / NO_BUNDLED_SKILLS_MARKER).exists()
    if essential_only and not quiet:
        print(
            "  (profile opted out of bundled skills via .no-bundled-skills — "
            "seeding essential skills only)"
        )

    bundled_dir = _get_bundled_dir()
    if not bundled_dir.exists():
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "suppressed": [], "total_bundled": 0,
            "optional_provenance_backfilled": [],
        }

    bundled_skills = _discover_bundled_skills(bundled_dir)
    if essential_only:
        # Opted-out profile: only the essential skills are synced.
        bundled_skills = [
            (name, src) for name, src in bundled_skills
            if name in _essential_names()
        ]
    bundled_names = {name for name, _ in bundled_skills}

    # Resolve the external catalog before touching the local skills tree or
    # manifest. If a configured mount is unavailable and there is no usable
    # last-known-good catalog, an empty set is not authoritative: defer the
    # entire bundled sync rather than creating local shadows.
    try:
        external_index = _build_external_skill_index()
    except ExternalSkillIndexUnavailable as exc:
        logger.warning(
            "Bundled skill sync deferred because the external catalog is "
            "unavailable: %s",
            exc,
        )
        raise
    if getattr(external_index, "status", "fresh") not in {"fresh", "disabled"}:
        reason = getattr(external_index, "reason", "external catalog is stale")
        logger.warning(
            "Bundled skill sync deferred because only a stale external "
            "catalog is available: %s",
            reason,
        )
        raise ExternalSkillIndexUnavailable(reason)

    _skills_dir().mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest()
    suppressed = _read_suppressed_names()
    shadowed_by_external: List[str] = []
    # Rename recovery indexes are expensive on host bind mounts. Build them
    # only if a tracked skill is actually missing from its canonical path.
    active_index: Optional[Dict[str, List[Path]]] = None
    hub_paths: Optional[Set[str]] = None

    copied = []
    updated = []
    user_modified = []
    suppressed_skipped: List[str] = []
    relocated: List[str] = []
    skipped = 0

    for skill_name, skill_src in bundled_skills:
        # Curator-pruned built-ins: do not re-seed. The suppression list
        # (~/.hermes/skills/.curator_suppressed) is written when the curator
        # archives a bundled skill with curator.prune_builtins enabled. Without
        # this skip, every `hermes update` would resurrect a skill the user
        # deliberately pruned. Restoring the skill clears its suppression entry.
        # Essential skills are exempt — they must always come back.
        if skill_name in suppressed and skill_name not in _essential_names():
            suppressed_skipped.append(skill_name)
            continue

        dest = _compute_relative_dest(skill_src, bundled_dir)
        bundled_hash = _dir_hash(skill_src)

        if skill_name in external_index:
            # Establish external ownership before orphan/rename recovery. The
            # active-skill index also contains materialized external packages;
            # moving one into the local bundled tree would steal externally
            # owned content and invalidate the immutable snapshot.
            shadowed_by_external.append(skill_name)
            skipped += 1
            if not quiet:
                print(
                    f"  ⇢ {skill_name} (deferred to external_dirs, "
                    "not written to local tree)"
                )
            # Self-healing: a prior sync may have left a local shadow. We own
            # it only when it is byte-identical to the bundled source.
            if dest.exists() and _dir_hash(dest) == bundled_hash:
                _rmtree_writable(dest)
                if not quiet:
                    print(f"  ✓ removed stale shadow of {skill_name}")
                manifest.pop(skill_name, None)
            continue

        # Recover an orphaned backup before classifying. If a previous
        # update was interrupted between moving dest aside and copying the
        # new version in, the user's only copy sits in ``dest.bak`` while
        # dest is gone — without this, the "in manifest but not on disk"
        # branch below misreads the skill as user-deleted and it silently
        # vanishes from discovery.
        _orphan = dest.with_suffix(".bak")
        if _orphan.exists() and not dest.exists():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(_orphan), str(dest))
                logger.info("Recovered orphaned skill backup: %s", _orphan)
            except (OSError, IOError):
                logger.warning(
                    "Could not recover orphaned skill backup %s", _orphan,
                    exc_info=True,
                )

        # Recover an upstream RENAME / RECATEGORIZATION before classifying.
        # The manifest key (frontmatter name) survives a directory move, but
        # ``dest`` is a new path that does not exist yet — without this the
        # "in manifest but not on disk" branch below misreads the skill as
        # user-deleted, stranding the old copy at its stale path forever.
        if not dest.exists() and skill_name in manifest:
            if active_index is None:
                active_index = _index_active_skills()
                hub_paths = _read_hub_install_paths()
            _moved_from = _recover_renamed_skill(
                skill_name,
                manifest.get(skill_name, ""),
                dest,
                active_index,
                hub_paths or set(),
                quiet,
            )
            if _moved_from:
                relocated.append(skill_name)

        if skill_name not in manifest:
            # ── New skill — never offered before ──
            try:
                if dest.exists():
                    # User already has a skill with the same name — don't overwrite.
                    # Only baseline in the manifest when the on-disk copy is
                    # byte-identical to bundled (e.g. a reset that re-syncs, or
                    # a coincidentally identical install); that case is harmless
                    # to track. If the copy differs (custom skill, hub-installed,
                    # or user-edited) skip the manifest write: recording
                    # bundled_hash there would poison update detection by making
                    # user_hash != origin_hash read as "user-modified" on every
                    # subsequent sync, permanently blocking bundled updates.
                    skipped += 1
                    if _dir_hash(dest) == bundled_hash:
                        manifest[skill_name] = bundled_hash
                    elif not quiet:
                        print(
                            f"  ⚠ {skill_name}: bundled version shipped but you "
                            f"already have a local skill by this name — yours "
                            f"was kept. Run `hermes skills reset {skill_name}` "
                            f"to replace it with the bundled version."
                        )
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(skill_src, dest)
                    copied.append(skill_name)
                    manifest[skill_name] = bundled_hash
                    if not quiet:
                        print(f"  + {skill_name}")
            except (OSError, IOError) as e:
                if not quiet:
                    print(f"  ! Failed to copy {skill_name}: {e}")
                # Do NOT add to manifest — next sync should retry

        elif dest.exists():
            # ── Existing skill — in manifest AND on disk ──
            origin_hash = manifest.get(skill_name, "")

            # If the bundled source still matches the version recorded when
            # it was installed, there is no update to apply. Avoid recursively
            # hashing the user's copy just to rediscover that fact; when the
            # bundled source changes, the normal user-modification check below
            # still protects local edits before any overwrite.
            if origin_hash and bundled_hash == origin_hash:
                skipped += 1
                continue

            user_hash = _dir_hash(dest)

            if not origin_hash:
                # v1 migration: no origin hash recorded. Set baseline from
                # user's current copy so future syncs can detect modifications.
                manifest[skill_name] = user_hash
                if user_hash == bundled_hash:
                    skipped += 1  # already in sync
                else:
                    # Can't tell if user modified or bundled changed — be safe
                    skipped += 1
                continue

            if _is_tracked_user_modification(origin_hash, user_hash):
                # User modified this skill — don't overwrite their changes
                user_modified.append(skill_name)
                if not quiet:
                    print(f"  ~ {skill_name} (user-modified, skipping)")
                continue

            # User copy matches origin — check if bundled has a newer version
            if bundled_hash != origin_hash:
                try:
                    # Move old copy to a backup so we can restore on failure
                    backup = dest.with_suffix(".bak")
                    # A stale backup left by an earlier failure would make
                    # shutil.move() nest dest *inside* it (or fail outright)
                    # and would poison the restore path below. The current
                    # dest is the authoritative copy — clear the leftover.
                    if backup.exists():
                        _rmtree_writable(backup)
                    shutil.move(str(dest), str(backup))
                    try:
                        shutil.copytree(skill_src, dest)
                        manifest[skill_name] = bundled_hash
                        updated.append(skill_name)
                        if not quiet:
                            print(f"  ↑ {skill_name} (updated)")
                        # Remove backup after successful copy
                        try:
                            _rmtree_writable(backup)
                        except (OSError, IOError):
                            logger.debug("Could not remove backup %s", backup, exc_info=True)
                    except (OSError, IOError):
                        # Restore from backup. A partially-written dest must
                        # not shadow the user's copy or block the restore —
                        # clear it first, then move the backup home.
                        if backup.exists():
                            if dest.exists():
                                try:
                                    _rmtree_writable(dest)
                                except (OSError, IOError):
                                    logger.warning(
                                        "Could not clear partial copy %s during restore",
                                        dest, exc_info=True,
                                    )
                            if not dest.exists():
                                shutil.move(str(backup), str(dest))
                        raise
                except (OSError, IOError) as e:
                    if not quiet:
                        print(f"  ! Failed to update {skill_name}: {e}")
            else:
                skipped += 1  # bundled unchanged, user unchanged

        else:
            # ── In manifest but not on disk — user deleted it ──
            skipped += 1

    # Clean stale manifest entries (skills removed from bundled dir).
    # Skip on an opted-out profile: bundled_skills was filtered to the
    # essential set there, and cleaning would drop tracking for every other
    # previously-synced skill still on disk.
    if essential_only:
        cleaned = []
    else:
        cleaned = sorted(set(manifest.keys()) - bundled_names)
        for name in cleaned:
            del manifest[name]

    # Also copy DESCRIPTION.md files for categories (if not already present).
    # On an opted-out profile only the essential skills' own category
    # descriptions are seeded — not the full catalog's.
    _essential_cat_dirs = {
        _compute_relative_dest(src, bundled_dir).parent
        for _, src in bundled_skills
    } if essential_only else None
    for desc_md in bundled_dir.rglob("DESCRIPTION.md"):
        rel = desc_md.relative_to(bundled_dir)
        dest_desc = _skills_dir() / rel
        if _essential_cat_dirs is not None and dest_desc.parent not in _essential_cat_dirs:
            continue
        if not dest_desc.exists():
            try:
                dest_desc.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(desc_md, dest_desc)
            except (OSError, IOError) as e:
                logger.debug("Could not copy %s: %s", desc_md, e)

    _write_manifest(manifest)
    optional_provenance_backfilled = _backfill_optional_provenance(quiet=quiet)

    return {
        "copied": copied,
        "updated": updated,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "suppressed": suppressed_skipped,
        "relocated": relocated,
        "total_bundled": len(bundled_skills),
        "optional_provenance_backfilled": optional_provenance_backfilled,
        "shadowed_by_external": shadowed_by_external,
        "external_scan_status": getattr(external_index, "status", "fresh"),
        "external_scan_error": getattr(external_index, "reason", ""),
        # Opted-out profiles still seed essential skills; the flag lets
        # callers report "opted out" rather than a normal full sync.
        "skipped_opt_out": essential_only,
    }


def _rmtree_writable(path: Path) -> None:
    """Remove a directory tree, making read-only entries writable first.

    Handles immutable package sources (Nix store, deb/rpm installs) that
    preserve read-only permissions on copied files *and* directories
    (``r-xr-xr-x``).  Removing a child requires write permission on its
    parent directory, so the retry handler makes the failing path **and its
    parent** writable before re-attempting.  See #34860, #34972.
    """
    # Defense in depth (#48200): refuse to rmtree anything outside
    # ``HERMES_HOME/skills/`` to prevent the catastrophic wipe of
    # ``~/.hermes/`` (``.env``, ``MEMORY.md``, ``kanban.db``, custom
    # skills, scripts, …) that an earlier incident observed. Five call
    # sites in this file invoke this helper; if any one of them ever
    # computes a destination outside the skills root — through a bad
    # path join, a missing ``HERMES_HOME`` default, a malicious
    # bundled-manifest entry, or a mid-flight exception that leaves a
    # stale path in scope — this guard turns the resulting
    # ``shutil.rmtree(~/.hermes)`` into a loud, recoverable ``ValueError``
    # instead of silently destroying the user's install.
    target = Path(path).resolve()
    skills_root = _skills_dir().resolve()
    # Every legitimate caller passes a skill directory or its ``.bak``
    # sibling — always a strict child of the skills root. The skills root
    # itself must never be removed: a ``dest`` that collapses to
    # ``SKILLS_DIR`` (e.g. a relative path resolving to ``.``) would wipe
    # every installed skill, and its ``.bak`` sibling lands one level up in
    # ``HERMES_HOME``. Require a strict-child relationship so both escape
    # into the skills root and out of it are refused.
    if skills_root not in target.parents:
        raise ValueError(
            f"refusing to rmtree {target!r}: not strictly under {skills_root!r} "
            f"(scope guard — see #48200)"
        )
    import stat

    def _on_error(func, fpath, exc_info):
        # Unlinking a child requires the parent dir to be writable, so chmod
        # the parent as well as the failing path, then retry.
        for target in (os.path.dirname(fpath), fpath):
            try:
                os.chmod(target, stat.S_IRWXU)
            except OSError:
                pass
        func(fpath)

    shutil.rmtree(path, onerror=_on_error)


def reset_bundled_skill(name: str, restore: bool = False) -> dict:
    """
    Reset a bundled skill's manifest tracking so future syncs work normally.

    When a user edits a bundled skill, subsequent syncs mark it as
    ``user_modified`` and skip it forever — even if the user later copies
    the bundled version back into place, because the manifest still holds
    the *old* origin hash. This function breaks that loop.

    Args:
        name: The skill name (matches the manifest key / skill frontmatter name).
        restore: If True, also delete the user's copy in the skills dir and let
                 the next sync re-copy the current bundled version. If False
                 (default), only clear the manifest entry — the user's
                 current copy is preserved but future updates work again.

    Returns:
        dict with keys:
          - ok: bool, whether the reset succeeded
          - action: one of "manifest_cleared", "restored", "not_in_manifest",
                    "bundled_missing"
          - message: human-readable description
          - synced: dict from sync_skills() if a sync was triggered, else None
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_by_name = dict(bundled_skills)

    in_manifest = name in manifest
    is_bundled = name in bundled_by_name

    if not in_manifest and not is_bundled:
        return {
            "ok": False,
            "action": "not_in_manifest",
            "message": (
                f"'{name}' is not a tracked bundled skill. Nothing to reset. "
                f"(Hub-installed skills use `hermes skills uninstall`.)"
            ),
            "synced": None,
        }

    # Step 1 (optional): delete the user's copy so next sync re-copies bundled.
    # Must happen BEFORE manifest deletion so that a failed rmtree does not
    # leave the skill in a manifest-less limbo state (see #34972).
    deleted_user_copy = False
    if restore:
        if not is_bundled:
            return {
                "ok": False,
                "action": "bundled_missing",
                "message": (
                    f"'{name}' has no bundled source — manifest entry preserved "
                    f"but cannot restore from bundled (skill was removed upstream)."
                ),
                "synced": None,
            }
        dest = _compute_relative_dest(bundled_by_name[name], bundled_dir)
        if dest.exists():
            try:
                _rmtree_writable(dest)
                deleted_user_copy = True
            except (OSError, IOError) as e:
                return {
                    "ok": False,
                    "action": "not_reset",
                    "message": (
                        f"Could not delete user copy at {dest}: {e}. "
                        f"Manifest entry preserved — nothing was changed."
                    ),
                    "synced": None,
                }

    # Step 2: drop the manifest entry so next sync treats it as new
    if in_manifest:
        del manifest[name]
        _write_manifest(manifest)

    # Step 3: run sync to re-baseline (or re-copy if we deleted)
    synced = sync_skills(quiet=True)

    if restore and deleted_user_copy:
        action = "restored"
        message = f"Restored '{name}' from bundled source."
    elif restore:
        # Nothing on disk to delete, but we re-synced — acts like a fresh install
        action = "restored"
        message = f"Restored '{name}' (no prior user copy, re-copied from bundled)."
    else:
        action = "manifest_cleared"
        message = (
            f"Cleared manifest entry for '{name}'. Future `hermes update` runs "
            f"will re-baseline against your current copy and accept upstream changes."
        )

    return {"ok": True, "action": action, "message": message, "synced": synced}


def _is_tracked_user_modification(origin_hash: str, user_hash: str) -> bool:
    """Whether an on-disk skill counts as a user modification ``hermes update`` keeps.

    Shared by the sync loop (which decides what to skip) and
    ``list_user_modified_bundled_skills`` (which surfaces the names) so the two
    can never drift. A skill is a tracked modification only when it has a
    recorded origin hash (an un-baselined / v1 entry with an empty hash is not)
    and its current content hash differs from that origin.
    """
    return bool(origin_hash) and user_hash != origin_hash


def list_user_modified_bundled_skills() -> List[dict]:
    """Return the bundled skills that ``hermes update`` keeps because the user
    edited them locally.

    A skill counts as user-modified when its on-disk copy no longer matches the
    origin hash recorded in the manifest the last time it was synced — the exact
    same test the sync loop uses to decide what to skip. This is the discovery
    half of that behavior, so a user can find the names the ``~ N user-modified
    (kept)`` notice only counts.

    Returns a list (sorted by name) of dicts:
        ``{"name": str, "dest": Path, "bundled_src": Path}``
    where ``dest`` is the user's copy and ``bundled_src`` is the current stock
    copy (so callers can diff or restore).
    """
    manifest = _read_manifest()
    if not manifest:
        return []
    bundled_dir = _get_bundled_dir()
    modified: List[dict] = []
    for skill_name, skill_dir in _discover_bundled_skills(bundled_dir):
        origin_hash = manifest.get(skill_name, "")
        # No entry, or a v1 entry not yet baselined (empty hash): not a tracked
        # modification — the next sync handles it.
        if not origin_hash:
            continue
        dest = _compute_relative_dest(skill_dir, bundled_dir)
        if not dest.exists():
            continue
        if _is_tracked_user_modification(origin_hash, _dir_hash(dest)):
            modified.append(
                {"name": skill_name, "dest": dest, "bundled_src": skill_dir}
            )
    modified.sort(key=lambda e: e["name"])
    return modified


def _read_for_diff(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """Read a file once for diffing.

    Returns ``(raw_bytes, text)`` where ``text`` is ``None`` if the file is
    binary; ``(None, None)`` if it could not be read. Returning the raw bytes
    lets the caller compare binary files without re-reading them.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if b"\x00" in data:
        return data, None
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        return data, None


def diff_bundled_skill(name: str) -> dict:
    """Diff a user's copy of a bundled skill against the current stock version.

    Lets a user see exactly what diverged before deciding whether to keep their
    edits or ``hermes skills reset`` back to upstream.

    Returns a dict:
        ``ok`` (bool), ``name`` (str), ``found`` (bool — bundled source exists),
        ``modified`` (bool), ``message`` (str),
        ``diffs``: list of ``{"path": str, "status": str, "diff": str}`` where
        status is one of ``modified`` / ``added`` (only in user copy) /
        ``removed`` (only in bundled) / ``binary``.
    """
    import difflib

    bundled_dir = _get_bundled_dir()
    bundled_by_name = dict(_discover_bundled_skills(bundled_dir))
    bundled_src = bundled_by_name.get(name)
    if bundled_src is None:
        return {
            "ok": False,
            "name": name,
            "found": False,
            "modified": False,
            "diffs": [],
            "message": (
                f"'{name}' is not a tracked bundled skill (no stock version to "
                f"diff against). Hub-installed skills use `hermes skills inspect`."
            ),
        }
    dest = _compute_relative_dest(bundled_src, bundled_dir)
    if not dest.exists():
        return {
            "ok": False,
            "name": name,
            "found": True,
            "modified": False,
            "diffs": [],
            "message": f"No local copy of '{name}' found at {dest}.",
        }

    user_files = set(_skill_file_list(dest))
    stock_files = set(_skill_file_list(bundled_src))

    diffs: List[dict] = []
    for rel in sorted(user_files | stock_files):
        in_user = rel in user_files
        in_stock = rel in stock_files
        user_bytes, user_text = (
            _read_for_diff(dest / rel) if in_user else (None, None)
        )
        stock_bytes, stock_text = (
            _read_for_diff(bundled_src / rel) if in_stock else (None, None)
        )

        if in_user and in_stock:
            if user_text is None or stock_text is None:
                # At least one side is binary — report only if bytes differ
                # (reuse the bytes already read above, no second read).
                if user_bytes != stock_bytes:
                    diffs.append(
                        {"path": rel, "status": "binary", "diff": "<binary file differs>"}
                    )
                continue
            if user_text == stock_text:
                continue
            text = "".join(
                difflib.unified_diff(
                    stock_text.splitlines(keepends=True),
                    user_text.splitlines(keepends=True),
                    fromfile=f"stock/{rel}",
                    tofile=f"yours/{rel}",
                )
            )
            diffs.append({"path": rel, "status": "modified", "diff": text})
        elif in_user:
            diffs.append(
                {"path": rel, "status": "added", "diff": f"+ only in your copy: {rel}"}
            )
        else:
            diffs.append(
                {"path": rel, "status": "removed", "diff": f"- only in stock: {rel}"}
            )

    modified = bool(diffs)
    return {
        "ok": True,
        "name": name,
        "found": True,
        "modified": modified,
        "diffs": diffs,
        "message": (
            f"'{name}' matches the stock version."
            if not modified
            else f"'{name}' differs from the stock version in {len(diffs)} file(s)."
        ),
    }


def set_bundled_skills_opt_out(enabled: bool) -> dict:
    """Toggle the .no-bundled-skills opt-out marker for the active profile.

    When ``enabled`` is True, writes HERMES_HOME/.no-bundled-skills so the
    installer, ``hermes update``, and any direct sync stop seeding bundled
    skills. When False, removes the marker so seeding resumes on the next
    sync. This is the on-disk-state half of ``hermes skills opt-out`` /
    ``opt-in``; removal of already-present skills is a separate, explicit
    step (see ``remove_pristine_bundled_skills``).

    Returns:
        dict with keys: ok (bool), changed (bool), marker (str path),
                        message (str).
    """
    marker = _hermes_home() / NO_BUNDLED_SKILLS_MARKER
    existed = marker.exists()
    try:
        if enabled:
            _hermes_home().mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "This profile opted out of bundled-skill seeding "
                "(`hermes skills opt-out`).\n"
                "Delete this file to re-enable sync on the next `hermes update`.\n",
                encoding="utf-8",
            )
            changed = not existed
            message = (
                "Opted out of bundled skills. Future install / update / sync "
                "runs will not seed bundled skills into this profile."
                if changed
                else "Already opted out — marker was already present."
            )
        else:
            if existed:
                marker.unlink()
            changed = existed
            message = (
                "Opted back in. The next `hermes update` (or `hermes skills "
                "opt-in --sync`) will re-seed bundled skills."
                if changed
                else "Not opted out — no marker to remove."
            )
    except OSError as e:
        return {
            "ok": False, "changed": False, "marker": str(marker),
            "message": f"Could not update opt-out marker at {marker}: {e}",
        }
    return {"ok": True, "changed": changed, "marker": str(marker), "message": message}


def is_bundled_skills_opt_out() -> bool:
    """Return True if the active profile carries the opt-out marker."""
    return (_hermes_home() / NO_BUNDLED_SKILLS_MARKER).exists()


def remove_pristine_bundled_skills(dry_run: bool = False) -> dict:
    """Delete bundled skills that are present, manifest-tracked, AND unmodified.

    Safety is the whole point of this function. A skill on disk is removed
    ONLY when all of these hold:
      - it is recorded in the sync manifest (so it is genuinely a bundled
        skill, not a hub-installed or hand-written one), AND
      - it still exists in the bundled source (so we can hash-compare), AND
      - its on-disk copy is byte-identical to the manifest origin hash
        (so the user has not edited it).

    Anything user-modified, hub-installed, or locally authored is left
    untouched and reported under ``skipped``. The manifest entry for each
    removed skill is dropped so a later opt-in re-seed treats it as new.

    Args:
        dry_run: When True, compute what would be removed without deleting.

    Returns:
        dict with keys: ok (bool), removed (list[str]),
                        skipped (list[dict]) where each dict is
                        {name, reason}, dry_run (bool), message (str).
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_by_name = dict(_discover_bundled_skills(bundled_dir))

    removed: List[str] = []
    skipped: List[dict] = []

    for name, origin_hash in sorted(manifest.items()):
        src = bundled_by_name.get(name)
        if src is None:
            # Tracked but no longer bundled upstream — leave it; not ours to judge.
            skipped.append({"name": name, "reason": "no bundled source (removed upstream)"})
            continue
        dest = _compute_relative_dest(src, bundled_dir)
        if not dest.exists():
            # Already gone from disk; just forget the stale manifest entry.
            if not dry_run and name in manifest:
                del manifest[name]
            continue
        on_disk = _dir_hash(dest)
        if on_disk != origin_hash:
            skipped.append({"name": name, "reason": "user-modified (kept)"})
            continue
        # Pristine bundled copy — safe to remove.
        if dry_run:
            removed.append(name)
            continue
        try:
            _rmtree_writable(dest)
        except (OSError, IOError) as e:
            skipped.append({"name": name, "reason": f"delete failed: {e}"})
            continue
        if name in manifest:
            del manifest[name]
        removed.append(name)

    if not dry_run and removed:
        _write_manifest(manifest)

    verb = "Would remove" if dry_run else "Removed"
    message = f"{verb} {len(removed)} pristine bundled skill(s); kept {len(skipped)}."
    return {
        "ok": True, "removed": removed, "skipped": skipped,
        "dry_run": dry_run, "message": message,
    }


if __name__ == "__main__":
    print("Syncing bundled skills into ~/.hermes/skills/ ...")
    try:
        result = sync_skills(quiet=False)
    except ExternalSkillIndexUnavailable as exc:
        print(f"\nDeferred: {exc}", file=sys.stderr)
        raise SystemExit(75) from exc
    parts = [
        f"{len(result['copied'])} new",
        f"{len(result['updated'])} updated",
        f"{result['skipped']} unchanged",
    ]
    if result["user_modified"]:
        names = result["user_modified"]
        MAX_SHOW = 5
        shown = ", ".join(names[:MAX_SHOW])
        if len(names) > MAX_SHOW:
            shown += f", +{len(names) - MAX_SHOW} more"
        parts.append(f"{len(names)} user-modified (kept): {shown}")
    if result["cleaned"]:
        parts.append(f"{len(result['cleaned'])} cleaned from manifest")
    if result.get("optional_provenance_backfilled"):
        parts.append(f"{len(result['optional_provenance_backfilled'])} official optional backfilled")
    print(f"\nDone: {', '.join(parts)}. {result['total_bundled']} total bundled.")
