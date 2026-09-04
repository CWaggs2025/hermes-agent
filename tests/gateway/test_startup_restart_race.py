import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
)


class StartupRaceAdapter(BasePlatformAdapter):
    def __init__(
        self,
        platform: Platform,
        *,
        on_connect=None,
        wait_for_disconnect: asyncio.Event | None = None,
    ):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.on_connect = on_connect
        self.wait_for_disconnect = wait_for_disconnect
        self.connected = False
        self.disconnected = False
        self.background_cancelled = False

    async def connect(self, *, is_reconnect: bool = False):
        if self.on_connect:
            self.on_connect()
        if self.wait_for_disconnect is not None:
            await self.wait_for_disconnect.wait()
        self.connected = True
        return True

    async def disconnect(self):
        self.disconnected = True

    async def cancel_background_tasks(self):
        self.background_cancelled = True
        await super().cancel_background_tasks()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def make_startup_runner(tmp_path):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.SLACK: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner.adapters = {}
    runner._running = False
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_code = None
    runner._exit_cleanly = False
    runner._exit_with_failure = False
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    runner._stop_task = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._failed_platforms = {}
    runner._voice_mode = {}

    runner.hooks = MagicMock()
    runner.hooks.loaded_hooks = []
    runner.hooks.discover_and_load = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.suspend_recently_active.return_value = 0
    runner.delivery_router = MagicMock()
    runner.delivery_router.adapters = {}

    runner._update_runtime_status = MagicMock()
    runner._update_platform_runtime_status = MagicMock()
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._suspend_stuck_loop_sessions = MagicMock(return_value=0)
    runner._notify_active_sessions_of_shutdown = AsyncMock()
    runner._drain_active_agents = AsyncMock(return_value=({}, False))
    runner._finalize_shutdown_agents = AsyncMock()
    runner._send_update_notification = AsyncMock(return_value=False)
    runner._schedule_update_notification_watch = MagicMock()
    runner._send_restart_notification = AsyncMock()
    runner.wait_for_shutdown = gateway_run.GatewayRunner.wait_for_shutdown.__get__(
        runner, gateway_run.GatewayRunner
    )

    async def no_op_watcher(*args, **kwargs):
        await asyncio.Event().wait()

    runner._session_expiry_watcher = no_op_watcher
    runner._platform_reconnect_watcher = no_op_watcher
    runner._run_process_watcher = no_op_watcher
    runner._safe_adapter_disconnect = gateway_run.GatewayRunner._safe_adapter_disconnect.__get__(
        runner, gateway_run.GatewayRunner
    )
    runner.request_restart = gateway_run.GatewayRunner.request_restart.__get__(
        runner, gateway_run.GatewayRunner
    )
    runner.stop = gateway_run.GatewayRunner.stop.__get__(runner, gateway_run.GatewayRunner)
    return runner


def patch_startup_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("agent.shell_hooks.register_from_config", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.process_registry.process_registry.recover_from_checkpoint", lambda: 0)


def test_post_readiness_skill_sync_returns_while_scan_is_hung(monkeypatch):
    """A hung skill filesystem cannot hold the gateway's serving boundary."""
    scan_started = threading.Event()
    release_scan = threading.Event()

    def hung_sync(*, quiet):
        assert quiet is True
        scan_started.set()
        release_scan.wait()
        return {}

    monkeypatch.setattr("tools.skills_sync.sync_skills", hung_sync)

    started_at = time.monotonic()
    thread = gateway_run._start_post_readiness_skill_sync()

    try:
        assert scan_started.wait(timeout=5.0)
        assert time.monotonic() - started_at < 5.0
        assert thread.is_alive()
    finally:
        release_scan.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_gateway_reaches_ready_and_cron_with_hung_skill_filesystem(
    tmp_path, monkeypatch
):
    """Gateway startup never launches a scan against a configured hung root."""
    import tools.skills_sync as skills_sync_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    hung_external = tmp_path / "hung-external"
    (tmp_path / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {hung_external}\n",
        encoding="utf-8",
    )
    order = []
    ready = asyncio.Event()
    shutdown = asyncio.Event()
    cron_started = threading.Event()
    housekeeping_started = threading.Event()
    scan_started = threading.Event()

    class ReadyRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self._draining = False
            self._external_drain_active = False
            self._restart_via_service = False
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None

        async def start(self):
            order.append("runner")
            return True

        async def wait_for_shutdown(self):
            await shutdown.wait()

        def _start_systemd_watchdog(self):
            order.append("ready")
            ready.set()
            return True

    class TestCronProvider:
        name = "test"

        def start(self, stop_event, **kwargs):
            order.append("cron")
            cron_started.set()
            stop_event.wait()

    class DisabledControlServer:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            return False

    def run_housekeeping(stop_event, **kwargs):
        order.append("housekeeping")
        housekeeping_started.set()
        stop_event.wait()

    real_sync = skills_sync_module.sync_skills

    def gateway_sync(*, quiet):
        assert quiet is True
        order.append("skill-sync")
        scan_started.set()
        return real_sync(quiet=quiet)

    original_resolve = Path.resolve
    original_is_dir = Path.is_dir

    def guarded_resolve(path, *args, **kwargs):
        if str(path).startswith(str(hung_external)):
            raise AssertionError("gateway resolved the configured hung root")
        return original_resolve(path, *args, **kwargs)

    def guarded_is_dir(path):
        if str(path).startswith(str(hung_external)):
            raise AssertionError("gateway stated the configured hung root")
        return original_is_dir(path)

    async def no_mcp_discovery(config):
        return None

    async def no_mcp_shutdown():
        return None

    monkeypatch.setattr(
        "hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None
    )
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.security_audit_startup.log_startup_security_warnings",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(gateway_run, "GatewayRunner", ReadyRunner)
    monkeypatch.setattr(gateway_run, "_enable_multiplex_log_routing", lambda config: None)
    monkeypatch.setattr(gateway_run, "_run_planned_stop_watcher", lambda *args: None)
    monkeypatch.setattr(
        "gateway.control_socket.GatewayControlServer", DisabledControlServer
    )
    monkeypatch.setattr("gateway.lifecycle_ledger.record_startup", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None
    )
    monkeypatch.setattr(
        "hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None
    )
    monkeypatch.setattr(gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr(gateway_run, "_discover_gateway_mcp_tools", no_mcp_discovery)
    monkeypatch.setattr("gateway.shutdown_flush.recover_pending_to_db", lambda: 0)
    provider = TestCronProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: provider)
    monkeypatch.setattr(
        "cron.scheduler_provider.scheduler_for_profile_mode",
        lambda resolved, **kwargs: resolved,
    )
    monkeypatch.setattr(gateway_run, "_start_gateway_housekeeping", run_housekeeping)
    monkeypatch.setattr(gateway_run, "_stop_cron_provider", lambda provider: None)
    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", no_mcp_shutdown)
    monkeypatch.setattr(gateway_run, "_shutdown_gateway_health_export", lambda runner: None)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    monkeypatch.setattr("tools.skills_sync.sync_skills", gateway_sync)
    run_scan = MagicMock(side_effect=AssertionError("gateway launched scanner child"))
    monkeypatch.setattr(
        "tools.skills_sync._run_external_scan_subprocess",
        run_scan,
    )

    started_at = time.monotonic()
    task = asyncio.create_task(
        gateway_run.start_gateway(
            config=GatewayConfig(), replace=False, verbosity=None
        )
    )

    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        assert time.monotonic() - started_at < 5.0
        assert await asyncio.to_thread(cron_started.wait, 5.0)
        assert await asyncio.to_thread(housekeeping_started.wait, 5.0)
        assert await asyncio.to_thread(scan_started.wait, 5.0)
        assert order.index("ready") < order.index("skill-sync")
    finally:
        shutdown.set()

    assert await asyncio.wait_for(task, timeout=10.0) is True
    run_scan.assert_not_called()


@pytest.mark.asyncio
async def test_real_adapter_startup_reads_only_materialized_external_snapshot(
    tmp_path, monkeypatch
):
    """A real GatewayRunner adapter connect cannot resolve the live root."""
    from agent import skill_utils

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "skills").mkdir()
    live_external = tmp_path / "network-external"
    live_external.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {live_external}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    skill_utils._external_dirs_cache_clear()
    roots, _timeout = skill_utils.get_external_skills_scan_settings()
    fingerprint = skill_utils.external_skills_roots_fingerprint(roots)
    relative_root = f"{fingerprint}/generation/root-0000"
    materialized = skill_utils.external_skills_snapshot_dir() / relative_root
    package = materialized / "adapter-skill"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: adapter-skill\n---\n",
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
                "names": ["adapter-skill"],
                "materialized_complete": True,
                "materialized_roots": [relative_root],
            }
        ),
        encoding="utf-8",
    )

    original_resolve = Path.resolve
    original_is_dir = Path.is_dir

    def guarded_resolve(path, *args, **kwargs):
        if str(path).startswith(str(live_external)):
            raise AssertionError("adapter startup resolved the live external root")
        return original_resolve(path, *args, **kwargs)

    def guarded_is_dir(path):
        if str(path).startswith(str(live_external)):
            raise AssertionError("adapter startup stated the live external root")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    observed = []

    def inspect_skills_on_connect():
        observed.extend(skill_utils.get_external_skills_dirs())

    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=hermes_home / "sessions",
    )
    runner = gateway_run.GatewayRunner(config)
    adapter = StartupRaceAdapter(
        Platform.TELEGRAM,
        on_connect=inspect_skills_on_connect,
    )
    monkeypatch.setattr(runner, "_create_adapter", lambda *args: adapter)
    monkeypatch.setattr(runner, "_start_secondary_profile_adapters", lambda: 0)

    assert await asyncio.wait_for(runner.start(), timeout=30) is True
    assert adapter.connected is True
    assert observed == [materialized]


@pytest.mark.asyncio
async def test_startup_aborts_when_restart_begins_during_platform_connect(tmp_path, monkeypatch):
    patch_startup_side_effects(monkeypatch, tmp_path)

    runner = make_startup_runner(tmp_path)
    first_disconnected = asyncio.Event()
    telegram = StartupRaceAdapter(
        Platform.TELEGRAM,
        on_connect=lambda: runner.request_restart(detached=False, via_service=True),
    )
    slack = StartupRaceAdapter(Platform.SLACK, wait_for_disconnect=first_disconnected)

    async def disconnect_and_release():
        telegram.disconnected = True
        first_disconnected.set()

    telegram.disconnect = disconnect_and_release
    runner._create_adapter = MagicMock(side_effect=[telegram, slack])

    result = await asyncio.wait_for(runner.start(), timeout=30)

    assert result is True
    assert telegram.disconnected is True
    assert telegram.background_cancelled is True
    assert slack.connected is False
    assert runner._running is False
    assert runner.adapters == {}
    assert runner._update_runtime_status.call_args_list[-1].args[0] == "stopped"
    assert not any(
        call.args[:1] == ("running",)
        for call in runner._update_runtime_status.call_args_list
    )
    assert not any(
        call.args[:2] == (Platform.SLACK.value, "connected")
        for call in runner._update_platform_runtime_status.call_args_list
    )


@pytest.mark.asyncio
async def test_start_gateway_does_not_start_cron_after_aborted_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron_started = False
    skill_sync_started = False
    export_shutdown_calls = 0

    class ExportRuntime:
        def shutdown(self):
            nonlocal export_shutdown_calls
            export_shutdown_calls += 1

    class AbortedStartupRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = False
            self.should_exit_cleanly = True
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = GATEWAY_SERVICE_RESTART_EXIT_CODE
            self._gateway_health_export_runtime = ExportRuntime()

        async def start(self):
            return True

        async def wait_for_shutdown(self):
            return None

    def fail_if_cron_starts(*args, **kwargs):
        nonlocal cron_started
        cron_started = True

    def fail_if_skill_sync_starts(*args, **kwargs):
        nonlocal skill_sync_started
        skill_sync_started = True

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", AbortedStartupRunner)
    monkeypatch.setattr("gateway.run._start_cron_ticker", fail_if_cron_starts)
    monkeypatch.setattr(
        "gateway.run._start_post_readiness_skill_sync",
        fail_if_skill_sync_starts,
    )
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)

    with pytest.raises(SystemExit) as exc:
        await gateway_run.start_gateway(config=GatewayConfig(), replace=False, verbosity=None)

    assert exc.value.code == GATEWAY_SERVICE_RESTART_EXIT_CODE
    assert cron_started is False
    assert skill_sync_started is False
    assert export_shutdown_calls == 1
