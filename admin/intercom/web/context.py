"""Der Laufzeitkontext: alles, was der Prozess besitzt und am Leben haelt.

Ein Prozess, ein asyncio-Loop, mehrere Nebenlaeufer:

* **Ice** bringt eigene Threads mit. Aufrufe gehen ueber
  :class:`~intercom.ice.client.AsyncIceClient` in einen kleinen Threadpool,
  Rueckrufe kommen per ``call_soon_threadsafe`` in den Loop.
* **Der Monitor-Bot** ist ein pymumble-Thread. Seine Messwerte nehmen denselben
  Weg in den Loop.
* **Polling, Verlaufs-Aufraeumen und der Verbindungswaechter** sind asyncio-Tasks.

Warum kein ``supervisord``: der Monitor-Bot schreibt in dieselbe SQLite wie das
GUI und seine Werte sollen sofort im Cockpit stehen. Zwei Prozesse haetten dafuer
IPC gebraucht -- Aufwand ohne Gegenwert. Dass ein Absturz des Bots das GUI nicht
mitreisst, loest der Waechter-Task mit Backoff. Siehe DECISIONS D-009.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from ..config import Settings
from ..ice.client import AsyncIceClient
from ..ice.errors import IceError
from ..provision.acl_map import build_desired_state
from ..provision.planner import Plan, reconcile
from ..provision.schema import ConfigInvalid, IntercomConfig, load_config
from ..runtime import Enforcer
from .auth import SessionManager
from .state import LiveState

log = logging.getLogger(__name__)

__all__ = ["AppContext"]


class AppContext:
    """Besitzt Verbindungen, Zustand und Hintergrundaufgaben."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ice = AsyncIceClient(settings)
        self.live = LiveState(settings)
        self.sessions = SessionManager(settings)
        self.enforcer = Enforcer(self.ice.sync)

        self.store: Any = None
        self.monitor: Any = None

        self.config: IntercomConfig | None = None
        self.config_error: str = ""
        self.last_plan: Plan | None = None
        self.last_provision_at: float = 0.0

        self.started_at = time.time()
        self._server_uptime = 0
        self.connected = False
        self.connection_error = ""
        #: Nicht toedliche Hinweise fuer das Banner im Cockpit.
        self.banners: list[str] = list(settings.warnings)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._provision_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  Start und Ende
    # ------------------------------------------------------------------ #

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()

        self._open_store()
        self.reload_config()

        # Rueckrufe registrieren, bevor die Verbindung steht: der Adapter wird
        # beim Verbinden aufgebaut und die Handler haengen schon daran.
        self.ice.sync.callbacks.subscribe(self._on_ice_event)

        await self._try_connect(first=True)

        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="polling"),
            asyncio.create_task(self._reconnect_loop(), name="reconnect"),
            asyncio.create_task(self._prune_loop(), name="aufraeumen"),
        ]
        if self.settings.monitor_enabled:
            self._start_monitor()

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        if self.monitor is not None:
            with contextlib.suppress(Exception):
                self.monitor.stop()
        self.ice.shutdown()
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.close()

    # ------------------------------------------------------------------ #
    #  Aufbau der Einzelteile
    # ------------------------------------------------------------------ #

    def _open_store(self) -> None:
        try:
            from ..store.db import Store
        except ImportError as exc:  # pragma: no cover
            self.banners.append(f"Verlauf und Audit-Log sind aus: {exc}")
            return
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            self.store = Store(self.settings.db_path)
            self.store.connect()
            self.store.migrate()
        except Exception as exc:  # noqa: BLE001
            self.store = None
            self.banners.append(
                f"{self.settings.db_path} ist nicht benutzbar ({exc}). Verlauf und "
                "Audit-Log sind aus; das Cockpit laeuft weiter. Auf dem NAS hilft "
                "meist ein chown auf das Verzeichnis admin-data."
            )
            log.exception("SQLite nicht benutzbar")

    def _start_monitor(self) -> None:
        try:
            from ..monitor.bot import MonitorBot
        except ImportError as exc:  # pragma: no cover
            self.banners.append(
                f"Monitor-Bot ist aus ({exc}). Ohne ihn gibt es keinen Paketverlust -- "
                "die Ice-Schnittstelle liefert ihn nicht."
            )
            return
        try:
            self.monitor = MonitorBot(self.settings, on_stats=self._on_stats)
            self.monitor.start()
        except Exception as exc:  # noqa: BLE001
            self.monitor = None
            self.banners.append(f"Monitor-Bot konnte nicht starten: {exc}")
            log.exception("Monitor-Bot")

    def reload_config(self) -> None:
        """Liest ``intercom.yaml`` neu ein."""
        try:
            self.config = load_config(self.settings.intercom_config)
            self.config_error = ""
            self.live.apply_config(self.config.networks, self.config.user_channels)
        except ConfigInvalid as exc:
            self.config = None
            self.config_error = str(exc)
            log.error("intercom.yaml ist unbrauchbar: %s", exc)
        except Exception as exc:  # noqa: BLE001
            self.config = None
            self.config_error = str(exc)
            log.exception("intercom.yaml")

    # ------------------------------------------------------------------ #
    #  Verbindung
    # ------------------------------------------------------------------ #

    async def _try_connect(self, first: bool = False) -> bool:
        try:
            await self.ice.run(self.ice.sync.connect)
        except IceError as exc:
            self.connected = False
            self.connection_error = str(exc)
            if first:
                log.error("Keine Verbindung zu murmur: %s", exc)
            return False

        self.connected = True
        self.connection_error = ""
        for warning in self.ice.sync.version_warnings:
            if warning not in self.banners:
                self.banners.append(warning)

        await self._refresh(full=True)
        if first and self.settings.provision_on_start and self.config is not None:
            await self.provision(dry_run=False, actor="start")
        else:
            await self._arm_enforcer()
        return True

    async def _reconnect_loop(self) -> None:
        """Baut die Verbindung wieder auf, wenn sie abreisst."""
        delay = 2.0
        while True:
            await asyncio.sleep(delay)
            if self.connected and self.ice.sync.connected:
                delay = 2.0
                continue
            if not self.ice.sync.connected:
                self.connected = False
            log.info("Versuche erneut, murmur zu erreichen ...")
            if await self._try_connect():
                log.info("Verbindung zu murmur steht wieder.")
                delay = 2.0
            else:
                delay = min(30.0, delay * 2)

    # ------------------------------------------------------------------ #
    #  Ereignisse aus Fremdthreads
    # ------------------------------------------------------------------ #

    def _on_ice_event(self, event: str, payload: Any) -> None:
        """Wird aus einem Ice-Thread aufgerufen -- nur weiterreichen, nichts tun."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_ice_event, event, payload)

    def _handle_ice_event(self, event: str, payload: Any) -> None:
        """Laeuft im asyncio-Loop."""
        if event in {"user_connected", "user_state_changed"}:
            self.live.note_user(payload)
            if event == "user_connected" or payload.priority_speaker is False:
                # Nur wenn es sich lohnen kann: der Abgleich liest sonst bei
                # jeder Lautstaerkeaenderung den Server ab.
                asyncio.create_task(self._enforce(payload))
        elif event == "user_disconnected":
            self.live.drop_user(payload.session)
        elif event in {"channel_created", "channel_removed", "channel_state_changed"}:
            asyncio.create_task(self._refresh(full=True))
            return
        self.live.hub.publish("state", self.live.snapshot(self.enforcer.deviations))

    def _on_stats(self, sample: Any) -> None:
        """Wird aus dem pymumble-Thread aufgerufen."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_stats, sample)

    def _handle_stats(self, sample: Any) -> None:
        self.live.note_stats(sample.session, sample.loss_pct, sample.jitter_ms)

    async def _enforce(self, user: Any) -> None:
        try:
            await self.ice.run(self.enforcer.enforce_user, user)
        except IceError:
            log.debug("Laufzeit-Abgleich fehlgeschlagen", exc_info=True)

    async def _arm_enforcer(self) -> None:
        """Laedt den Wunschzustand in den Enforcer."""
        if self.config is None or not self.connected:
            return
        try:
            channels = await self.ice.get_channels()
            user_ids = await self.ice.get_user_ids(list(self.config.users))
            desired = build_desired_state(self.config, user_ids)
            self.enforcer.load(desired, channels)
            await self.ice.run(self.enforcer.refresh_membership)
        except IceError as exc:
            log.warning("Laufzeit-Abgleich nicht scharf: %s", exc)

    # ------------------------------------------------------------------ #
    #  Hintergrundaufgaben
    # ------------------------------------------------------------------ #

    async def _refresh(self, full: bool = False) -> None:
        """Holt Nutzer (und bei Bedarf Kanaele) vom Server."""
        if not self.ice.sync.connected:
            return
        try:
            if full:
                self.live.set_channels(await self.ice.get_channels())
                self._server_uptime = await self.ice.get_uptime()
            self.live.set_users(await self.ice.get_users())
        except IceError as exc:
            self.connected = False
            self.connection_error = str(exc)
            return
        if full:
            await self._arm_enforcer()

    async def _poll_loop(self) -> None:
        """Statistiken einsammeln, Abgleich nachfuehren, Verlauf schreiben."""
        interval = self.settings.poll_interval_ms / 1000.0
        history_every = max(1, int(5.0 / interval))
        tick = 0
        while True:
            await asyncio.sleep(interval)
            tick += 1
            if not self.ice.sync.connected:
                continue
            try:
                await self._refresh(full=False)
                self.live.note_activity()
                deviations = await self.ice.run(
                    self.enforcer.enforce_all, list(self.live.users.values())
                )
                if tick % history_every == 0:
                    self._write_history()
                self.live.hub.publish("state", self.live.snapshot(deviations))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - der Task darf nie sterben
                log.exception("Polling-Durchlauf fehlgeschlagen")

    def _write_history(self) -> None:
        if self.store is None:
            return
        try:
            from ..store.db import Sample
        except ImportError:  # pragma: no cover
            return
        now = int(time.time())
        rows = [
            Sample(
                ts=now,
                session=user.session,
                name=user.name,
                userid=user.userid,
                channel_id=user.channel,
                address=user.address,
                ping_ms=user.ping,
                loss_pct=self.live.loss.get(user.session, 0.0),
                bandwidth_bps=user.bytes_per_sec * 8,
                tcp_only=user.tcp_only,
            )
            for user in self.live.users.values()
        ]
        if rows:
            try:
                self.store.record_samples(rows)
            except Exception:  # noqa: BLE001
                log.exception("Verlauf konnte nicht geschrieben werden")

    async def _prune_loop(self) -> None:
        """Raeumt den Verlauf auf und haelt den Monitor-Bot am Leben."""
        while True:
            await asyncio.sleep(600)
            if self.store is not None:
                try:
                    removed = self.store.prune(self.settings.history_retention_hours)
                    if removed:
                        log.info("Verlauf aufgeraeumt: %d Zeilen entfernt", removed)
                except Exception:  # noqa: BLE001
                    log.exception("Aufraeumen fehlgeschlagen")
            if (
                self.settings.monitor_enabled
                and self.monitor is not None
                and not self.monitor.connected
            ):
                log.info("Monitor-Bot ist nicht verbunden -- der Bot regelt das selbst.")

    # ------------------------------------------------------------------ #
    #  Provisioning
    # ------------------------------------------------------------------ #

    async def provision(
        self, *, dry_run: bool, actor: str, prune: bool | None = None
    ) -> Plan:
        """Plan oder Anwendung. Immer nur einer gleichzeitig."""
        if self.config is None:
            raise RuntimeError(self.config_error or "intercom.yaml ist nicht geladen.")
        if not self.ice.sync.connected:
            raise RuntimeError("Keine Verbindung zu murmur.")

        async with self._provision_lock:
            effective_prune = (
                self.settings.provision_prune if prune is None else prune
            )
            plan = await self.ice.run(
                reconcile,
                self.ice.sync,
                self.config,
                prune=effective_prune,
                dry_run=dry_run,
            )
            if not dry_run:
                self.last_plan = plan
                self.last_provision_at = time.time()
                await self._refresh(full=True)
                self.audit(
                    actor,
                    "provision.apply",
                    str(self.settings.intercom_config),
                    after=json.dumps(plan.to_json(), ensure_ascii=False),
                    ok=not plan.failed,
                    error="; ".join(c.error for c in plan.failed),
                )
            elif self.last_plan is None:
                self.last_plan = plan
            self.live.hub.publish("provision", plan.to_json())
            return plan

    # ------------------------------------------------------------------ #
    #  Audit
    # ------------------------------------------------------------------ #

    def audit(
        self,
        actor: str,
        action: str,
        target: str,
        *,
        before: str = "",
        after: str = "",
        ok: bool = True,
        error: str = "",
    ) -> None:
        """Schreibt einen Audit-Eintrag. Faellt der Speicher aus, wird geloggt."""
        if self.store is None:
            log.info("AUDIT %s %s %s (ohne Speicher)", actor, action, target)
            return
        try:
            self.store.audit(
                actor=actor,
                action=action,
                target=target,
                before=before,
                after=after,
                ok=ok,
                error=error,
            )
        except Exception:  # noqa: BLE001
            log.exception("Audit-Eintrag konnte nicht geschrieben werden")

    # ------------------------------------------------------------------ #
    #  Zustandsbericht
    # ------------------------------------------------------------------ #

    def health(self) -> dict[str, Any]:
        version = self.ice.sync.server_version
        # Die Laufzeit des virtuellen Servers kostet einen Ice-Aufruf; sie wird
        # beim Polling mitgenommen und hier nur ausgelesen.
        return {
            "server_uptime_s": self._server_uptime,
            "ok": self.connected,
            "ice": {
                "connected": self.connected,
                "error": self.connection_error,
                "server_version": version.short if version else None,
                "server_version_text": version.text if version else None,
                "expected_version": self.settings.expected_mumble_version,
                "warnings": self.ice.sync.version_warnings,
            },
            "config": {
                "path": str(self.settings.intercom_config),
                "loaded": self.config is not None,
                "error": self.config_error,
            },
            "store": self.store is not None,
            "monitor": {
                "enabled": self.settings.monitor_enabled,
                "connected": bool(self.monitor and self.monitor.connected),
                "ping_ms": getattr(self.monitor, "own_ping_ms", None),
                "error": getattr(self.monitor, "last_error", ""),
            },
            "provision": {
                "last_at": self.last_provision_at or None,
                "summary": self.last_plan.summary() if self.last_plan else None,
                "failed": len(self.last_plan.failed) if self.last_plan else 0,
            },
            "uptime_s": int(time.time() - self.started_at),
            "subscribers": self.live.hub.subscriber_count,
            "banners": self.banners,
        }
