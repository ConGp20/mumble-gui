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
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..ice.client import AsyncIceClient
from ..ice.errors import IceError
from ..provision.acl_map import build_desired_state
from ..provision.planner import Plan, reconcile
from ..provision.schema import ConfigInvalid, IntercomConfig, load_config
from ..runtime import Enforcer
from ..store.db import StoreClosed
from .auth import SessionManager
from .state import LiveState, NetworkMap

log = logging.getLogger(__name__)

#: Obergrenze fuer den blockierenden Teil des Herunterfahrens.
#:
#: Docker wartet nach ``SIGTERM`` voreingestellt zehn Sekunden auf das Ende des
#: Prozesses und schickt dann ``SIGKILL``. Was danach noch offen ist, wird nie
#: mehr erledigt -- deshalb liegt die Grenze darunter, damit hinterher noch
#: Zeit bleibt, die SQLite-Datei sauber zu schliessen.
SHUTDOWN_TIMEOUT_S = 8.0

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
        #: Rechnet die kumulativen Paketzaehler in die Rate je Intervall um.
        #: Er gehoert hierher und nicht in den Bot: er ist nicht thread-sicher,
        #: und hier laeuft alles im asyncio-Loop. Siehe MonitorBot-Docstring.
        self._loss: Any = None

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
        #: Kurzlebige Aufgaben aus Callbacks. Ohne festgehaltene Referenz
        #: kann der Garbage Collector eine laufende Task einsammeln --
        #: asyncio haelt selbst nur eine schwache Referenz darauf.
        self._nebenaufgaben: set[asyncio.Task[None]] = set()
        self._provision_lock = asyncio.Lock()
        #: PROVISION_ON_START ist noch offen. Bewusst kein "erster Versuch":
        #: im Compose starten murmur und die Oberflaeche gleichzeitig, und
        #: murmur braucht ein paar Sekunden, bis die Ice-Schnittstelle steht.
        #: Der erste Verbindungsversuch scheitert also regelmaessig -- an ihn
        #: darf das Provisionieren nicht gebunden sein.
        self._provision_offen = settings.provision_on_start

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
        for task in [*self._tasks, *self._nebenaufgaben]:
            task.cancel()
        for task in [*self._tasks, *self._nebenaufgaben]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._nebenaufgaben.clear()

        await self._abbau_der_threads()

        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.close()

    async def _abbau_der_threads(self) -> None:
        """Beendet Monitor-Bot und Ice-Verbindung, ohne den Loop einzufrieren.

        Beides blockiert: ``MonitorBot.stop`` wartet auf zwei Thread-Joins (bis
        zu 7 s), ``IceClient.close`` auf ``removeCallback`` und
        ``communicator.destroy()``. Direkt im Loop ausgefuehrt steht damit
        alles still, auch das, was uvicorn beim Herunterfahren noch erledigen
        will -- offene SSE-Verbindungen schliessen zum Beispiel. Im
        schlechtesten Fall summiert es sich auf mehr als die zehn Sekunden, die
        Docker vor dem SIGKILL wartet; dann kommt der Store nie zum Schliessen.
        """

        fertig = threading.Event()

        def abbau() -> None:
            try:
                if self.monitor is not None:
                    with contextlib.suppress(Exception):
                        self.monitor.stop()
                with contextlib.suppress(Exception):
                    self.ice.shutdown()
            finally:
                fertig.set()

        # Ein eigener Daemon-Thread, nicht ``asyncio.to_thread``: der laeuft im
        # Standard-Executor, und den wartet der Loop beim Schliessen ab
        # (``loop.shutdown_default_executor``). Die Frist unten waere damit nur
        # scheinbar eine -- gewartet wuerde trotzdem, nur eine Ebene tiefer.
        threading.Thread(target=abbau, name="abbau", daemon=True).start()

        frist = time.monotonic() + SHUTDOWN_TIMEOUT_S
        while not fertig.is_set() and time.monotonic() < frist:
            await asyncio.sleep(0.05)
        if not fertig.is_set():
            log.warning(
                "Monitor-Bot und Ice-Verbindung waren nach %.0f s nicht abgebaut. "
                "Der Prozess faehrt trotzdem herunter.",
                SHUTDOWN_TIMEOUT_S,
            )

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
        except Exception as exc:
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
            from ..monitor.stats import LossTracker

            self._loss = LossTracker()
            self.monitor = MonitorBot(
                self.settings, on_stats=self._on_stats, on_state=self._on_monitor_state
            )
            self.monitor.start()
        except Exception as exc:
            self.monitor = None
            self.banners.append(f"Monitor-Bot konnte nicht starten: {exc}")
            log.exception("Monitor-Bot")

    def reload_config(self) -> None:
        """Liest ``intercom.yaml`` ein, falls es eine gibt.

        **Keine Datei zu haben ist der Normalfall**, kein Fehler. Der Server
        ist die Wahrheit: was in der Oberflaeche angelegt wird, steht im
        Server und bleibt dort. Die YAML ist nur noch das Format fuer
        Sicherungen -- wer keine eingespielt hat, hat auch keine Datei, und
        das darf weder ein Banner noch einen Log-Eintrag ausloesen.

        Eine Datei, die *da* ist, aber nicht gelesen werden kann, bleibt ein
        Fehler: dann wollte jemand etwas und es ging schief.
        """
        if not Path(self.settings.intercom_config).exists():
            self.config = None
            self.config_error = ""
            self.live.apply_config([], {})
            self.netze_laden()
            return
        try:
            self.config = load_config(self.settings.intercom_config)
            self.config_error = ""
            self.live.apply_config(self.config.networks, self.config.user_channels)
        except ConfigInvalid as exc:
            self.config = None
            self.config_error = str(exc)
            log.error("intercom.yaml ist unbrauchbar: %s", exc)
        except Exception as exc:
            self.config = None
            self.config_error = str(exc)
            log.exception("intercom.yaml")
        # Zuletzt, damit der Store eine Vorgabedatei ueberstimmt: gepflegt wird
        # in der Oberflaeche.
        self.netze_laden()

    # ------------------------------------------------------------------ #
    #  Verbindung
    # ------------------------------------------------------------------ #

    def netze_laden(self) -> None:
        """Holt die Netzsegmente aus dem Store in die Netzsicht.

        Sie liegen dort, seit die intercom.yaml nicht mehr die Wahrheit haelt.
        Eine Vorgabedatei darf sie weiterhin mitbringen; steht in beiden etwas,
        gewinnt der Store -- er ist das, was in der Oberflaeche gepflegt wird.
        """
        if self.store is None:
            return
        try:
            segmente = self.store.netze()
        except StoreClosed:
            return
        if segmente:
            self.live.networks = NetworkMap(segmente)

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
        if self._provision_offen and self.config is not None:
            self._provision_offen = False
            if not first:
                log.info(
                    "murmur ist jetzt erreichbar -- PROVISION_ON_START wird "
                    "nachgeholt."
                )
            await self.provision(dry_run=False, actor="start")
        else:
            if self._provision_offen:
                # config is None: intercom.yaml ist unbrauchbar. Nicht still
                # weglassen -- sonst laeuft der Server ohne die Kanaele, die
                # das Stadion erwartet.
                self._provision_offen = False
                self.banners.append(
                    "PROVISION_ON_START war gesetzt, aber intercom.yaml ist "
                    "unbrauchbar -- es wurde nichts angelegt. Datei korrigieren "
                    "und im Bereich Provisionierung von Hand anwenden."
                )
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
                self._spawn(self._enforce(payload))
        elif event == "user_disconnected":
            self.live.drop_user(payload.session)
            self.enforcer.vergiss_sitzung(payload.session)
            if self._loss is not None:
                self._loss.forget(payload.session)
        elif event in {"channel_created", "channel_removed", "channel_state_changed"}:
            self._spawn(self._refresh(full=True))
            return
        self.live.hub.publish("state", self.live.snapshot(self.enforcer.deviations))

    def _spawn(self, coro: Any) -> None:
        """Startet eine Nebenaufgabe und haelt sie am Leben.

        ``asyncio`` haelt auf laufende Tasks nur eine schwache Referenz. Wer das
        Ergebnis von ``create_task`` wegwirft, riskiert, dass die Aufgabe
        mittendrin eingesammelt wird -- der Abgleich waere dann manchmal da und
        manchmal nicht, und zwar ohne jede Fehlermeldung.
        """
        task = asyncio.create_task(coro)
        self._nebenaufgaben.add(task)
        task.add_done_callback(self._nebenaufgaben.discard)

    def _on_stats(self, sample: Any) -> None:
        """Wird aus dem pymumble-Thread aufgerufen."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_stats, sample)

    def _on_monitor_state(self, state: str) -> None:
        """Zustandswechsel des Bots -- kommt aus dessen Aufseher-Thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_monitor_state, state)

    def _handle_monitor_state(self, state: str) -> None:
        if state == "verbunden" and self._loss is not None:
            # Waehrend der Trennung sind die Zaehler der Clients weitergelaufen
            # und Session-IDs koennen neu vergeben sein. Jede gemerkte
            # Momentaufnahme ist damit wertlos.
            self._loss.reset()
        elif state in {"wartet", "fehler", "gestoppt"}:
            # Ohne Bot gibt es keine frischen Messwerte. Die alten stehen zu
            # lassen hiesse, mit einem Wert von vor zehn Minuten zu alarmieren.
            self.live.clear_stats()

    def _handle_stats(self, sample: Any) -> None:
        """Kumulative Zaehler -> Rate im Intervall.

        ``sample.loss_pct`` waere der Mittelwert der GANZEN Sitzung. Ein Client,
        der in der ersten Minute 30 % verloren hat und seitdem sauber laeuft,
        stuende nach drei Stunden immer noch im Alarm -- und ein akuter Ausfall
        verschwaende im Mittel einer langen Sitzung. Fuer die Anzeige zaehlt
        allein die Differenz zum letzten Abruf.
        """
        if self._loss is None:
            return
        intervall = self._loss.update(sample)
        if not intervall.has_rate:
            # Erster Abruf oder Zaehler zurueckgesprungen: ein Intervall wird
            # geopfert, damit der naechste Wert stimmt.
            return
        verlust = intervall.loss_pct
        if verlust is None:
            # In diesem Intervall lief kein einziges Paket -- typisch fuer einen
            # Client, der auf TCP zurueckgefallen ist. "unbekannt" ist die
            # richtige Aussage, nicht "0 %".
            return
        self.live.note_stats(sample.session, verlust, sample.jitter_ms)

    async def _enforce(self, user: Any) -> None:
        try:
            await self.ice.run(self.enforcer.enforce_user, user)
        except IceError:
            log.debug("Laufzeit-Abgleich fehlgeschlagen", exc_info=True)

    async def _arm_enforcer(self) -> None:
        """Laedt den Wunschzustand in den Enforcer.

        Zwei Quellen: die Vorgabedatei, sofern es eine gibt, und der in der
        Oberflaeche gepflegte Wunsch je Person aus dem Store. Die zweite ist der
        Normalfall -- seit der Server die Wahrheit haelt, gibt es meist keine
        Vorgabedatei mehr.
        """
        if not self.connected:
            return
        try:
            channels = await self.ice.get_channels()
            if self.config is not None:
                user_ids = await self.ice.get_user_ids(list(self.config.users))
                desired = build_desired_state(self.config, user_ids)
                self.enforcer.load(desired, channels)
            if self.store is not None:
                self.enforcer.lade_wuensche(self.store.alle_wuensche(), channels)
                self.enforcer.lade_verbindungen(self.store.verbindungen(), channels)
                self.enforcer.lade_ruftasten(self.store.ruftasten(), channels)
            await self.ice.run(self.enforcer.refresh_membership)
        except IceError as exc:
            log.warning("Laufzeit-Abgleich nicht scharf: %s", exc)
        except StoreClosed:
            log.debug("Wunschzustand nicht lesbar -- Store geschlossen")

    async def wunschzustand_neu_laden(self) -> None:
        """Nach dem Laden einer Show: Enforcer neu laden, Sitzungen sofort nachziehen.

        Ohne den zweiten Schritt folgten verbundene Sitzungen erst beim
        naechsten Abgleich -- feste Plaetze und Ruftasten stuenden dann ein paar
        Sekunden auf dem alten Stand.
        """
        await self._arm_enforcer()
        if not self.connected:
            return
        try:
            await self.ice.run(self.enforcer.enforce_all, list(self.live.users.values()))
        except IceError as exc:
            log.warning("Sitzungen nicht nachgezogen: %s", exc)

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
            except Exception:
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
                # loss_pct(), nicht live.loss: ohne frische Messung wird
                # None gespeichert. Eine 0 waere eine Entwarnung, die niemand
                # gemessen hat -- und in der Sparkline eine makellose Linie
                # ueber genau die Zeit, in der nichts gemessen wurde.
                loss_pct=self.live.loss_pct(user.session),
                bandwidth_bps=user.bytes_per_sec * 8,
                tcp_only=user.tcp_only,
            )
            for user in self.live.users.values()
        ]
        if rows:
            try:
                self.store.record_samples(rows)
            except Exception:
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
                except Exception:
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

    async def anwenden(
        self,
        config: IntercomConfig,
        *,
        dry_run: bool,
        actor: str,
        quelle: str,
        prune: bool | None = None,
    ) -> Plan:
        """Gleicht den Server gegen eine **uebergebene** Konfiguration ab.

        Der Weg fuer Vorlagen und eingespielte Sicherungen: beide sind
        einmalige Aktionen mit einer Konfiguration in der Hand, keine laufende
        Bindung an eine Datei. ``quelle`` landet im Audit-Log, damit spaeter
        nachvollziehbar ist, was den Server veraendert hat.
        """
        if not self.ice.sync.connected:
            raise RuntimeError("Keine Verbindung zu murmur.")

        async with self._provision_lock:
            effective_prune = self.settings.provision_prune if prune is None else prune
            plan = await self.ice.run(
                reconcile,
                self.ice.sync,
                config,
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
                    quelle,
                    after=json.dumps(plan.to_json(), ensure_ascii=False),
                    ok=not plan.failed,
                    error="; ".join(c.error for c in plan.failed),
                )
            self.live.hub.publish("provision", plan.to_json())
            return plan

    async def provision(
        self, *, dry_run: bool, actor: str, prune: bool | None = None
    ) -> Plan:
        """Plan oder Anwendung gegen die geladene ``intercom.yaml``.

        Nur noch fuer den ausdruecklichen Weg ueber eine Datei -- die
        Oberflaeche benutzt :meth:`anwenden`.
        """
        if self.config is None:
            raise RuntimeError(
                self.config_error
                or "Es ist keine intercom.yaml geladen. Der Server ist die Wahrheit; "
                "eine Datei brauchst du nur zum Einspielen einer Sicherung."
            )
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
        except Exception:
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
