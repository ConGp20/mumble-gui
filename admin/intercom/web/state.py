"""Zentraler Laufzeitzustand des Admin-Prozesses.

Haelt zusammen, was das Cockpit braucht:

* eine gepflegte Kopie von Nutzern und Kanaelen (aus Ice-Callbacks und Polling),
* die Netzsicht (Zuordnung IP -> Segment aus ``intercom.yaml``),
* die Alarmleiste,
* einen Verteiler fuer Live-Aktualisierungen (SSE).

Nebenlaeufigkeit
----------------
Drei Quellen schreiben hier hinein und keine davon ist der asyncio-Loop:

* Ice ruft Callbacks aus **eigenen Threads** auf,
* der Monitor-Bot liefert Messwerte aus dem **pymumble-Thread**,
* das Polling laeuft als asyncio-Task.

Alle Fremdthread-Ereignisse werden mit ``call_soon_threadsafe`` in den Loop
gehoben und erst dort verarbeitet. Der Zustand selbst wird damit ausschliesslich
aus dem Loop veraendert -- das erspart Sperren an jeder Lesestelle.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..ice.types import MumbleChannel, MumbleUser
from ..provision.schema import NetworkSpec
from ..runtime import build_paths
from ..woerter import UEBERALL

log = logging.getLogger(__name__)

__all__ = ["Alarm", "EventHub", "LiveState", "NetworkMap"]


# --------------------------------------------------------------------------- #
#  Verteiler fuer Live-Aktualisierungen
# --------------------------------------------------------------------------- #


class EventHub:
    """Verteilt Ereignisse an alle offenen SSE-Verbindungen.

    Jeder Abonnent bekommt eine eigene, **begrenzte** Warteschlange. Ist sie
    voll, wird der aelteste Eintrag verworfen statt zu blockieren: ein Browser,
    der im Hintergrund eingefroren ist, darf nicht das ganze Cockpit anhalten.
    Der Verlust faellt nicht auf, weil jedes Ereignis den vollstaendigen
    Zustandsausschnitt traegt, keine Differenz.
    """

    def __init__(self, queue_size: int = 32) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._queue_size = queue_size
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped(self) -> int:
        return self._dropped

    def publish(self, event: str, data: Any) -> None:
        """Aus dem asyncio-Loop aufrufen."""
        if not self._subscribers:
            return
        try:
            payload = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            log.exception("Ereignis %s liess sich nicht serialisieren", event)
            return
        frame = f"event: {event}\ndata: {payload}\n\n"
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                    self._dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped += 1

    async def subscribe(self) -> AsyncIterator[str]:
        """Liefert SSE-Rahmen, bis der Aufrufer abbricht.

        Alle 15 Sekunden geht ein Kommentar-Rahmen raus. Er haelt die Verbindung
        durch einen etwaigen Reverse Proxy offen, der sonst nach einer Weile ohne
        Daten dichtmacht.
        """
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield ": verbunden\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subscribers.discard(queue)


# --------------------------------------------------------------------------- #
#  Netzsicht
# --------------------------------------------------------------------------- #


class NetworkMap:
    """Ordnet Client-IPs den Segmenten aus ``intercom.yaml`` zu.

    Reihenfolge zaehlt: der erste passende Eintrag gewinnt. So laesst sich ein
    kleineres Netz vor ein groesseres stellen, ohne Praefixlaengen zu vergleichen.
    """

    def __init__(self, segments: list[NetworkSpec] | None = None) -> None:
        self._segments: list[tuple[str, Any, str]] = []
        for spec in segments or []:
            try:
                network = ipaddress.ip_network(spec.cidr, strict=False)
            except ValueError:
                log.warning("Netzsegment %s hat ein ungueltiges CIDR %s", spec.name, spec.cidr)
                continue
            self._segments.append((spec.name, network, spec.note))

    def segment_for(self, address: str) -> str:
        if not address or address == "(anonymisiert)":
            return "unbekannt"
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return "unbekannt"
        for name, network, _ in self._segments:
            if parsed.version == network.version and parsed in network:
                return name
        return "sonstige"

    def note_for(self, name: str) -> str:
        for segment_name, _, note in self._segments:
            if segment_name == name:
                return note
        return ""

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self._segments]


# --------------------------------------------------------------------------- #
#  Alarme
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Alarm:
    """Ein Eintrag in der Alarmleiste."""

    level: str
    """``kritisch`` oder ``warnung``."""

    kind: str
    subject: str
    message: str
    session: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "level": self.level,
            "kind": self.kind,
            "subject": self.subject,
            "message": self.message,
            "session": self.session,
        }


@dataclass(slots=True)
class _VoxSuspicion:
    """Zaehlt Hinweise auf Dauersenden statt PTT.

    Mumble verraet den Sendemodus nicht -- weder ueber Ice noch im Protokoll.
    Erkennbar ist er nur indirekt: ein PTT-Nutzer hat zwischen den Durchsagen
    Leerlauf, ein VOX- oder Dauersender praktisch nie. Wir zaehlen aufeinander
    folgende Messungen mit Datenfluss und ohne Leerlauf; erst nach laengerer
    Zeit gibt es eine Warnung, damit eine lange Ansage keinen Fehlalarm ausloest.
    """

    streak: int = 0
    last_seen: float = 0.0


class LiveState:
    """Gepflegte Kopie des Serverzustands plus abgeleitete Sichten."""

    #: So viele aufeinander folgende Messungen ohne Leerlauf gelten als Dauersenden.
    VOX_STREAK = 30

    #: Aelter als das darf ein Messwert des Monitor-Bots nicht sein, um noch
    #: angezeigt zu werden oder einen Alarm auszuloesen. Stirbt der Bot, stuende
    #: sonst der letzte Wert unbegrenzt und alarmierte weiter -- ein Verlust von
    #: vor zehn Minuten ist keine Aussage ueber jetzt.
    STATS_MAX_AGE_S = 60.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hub = EventHub()
        self.networks = NetworkMap()

        self.users: dict[int, MumbleUser] = {}
        self.channels: dict[int, MumbleChannel] = {}
        self.paths: dict[str, int] = {}
        self.path_of: dict[int, str] = {}

        #: Session -> zuletzt vom Monitor-Bot gemessener Paketverlust in Prozent.
        self.loss: dict[int, float] = {}
        #: Session -> Jitter in Millisekunden.
        self.jitter: dict[int, float] = {}
        #: Session -> Zeitpunkt der letzten Messung.
        self.stats_seen: dict[int, float] = {}

        #: Soll-Kanal je Nutzername, aus intercom.yaml.
        self.expected_channels: dict[str, str] = {}

        self._vox: dict[int, _VoxSuspicion] = {}
        self._alarms: list[Alarm] = []
        self.last_update: float = 0.0

    # ------------------------------------------------------------------ #
    #  Aktualisierung
    # ------------------------------------------------------------------ #

    def set_channels(self, channels: dict[int, MumbleChannel]) -> None:
        self.channels = channels
        self.paths = build_paths(channels)
        self.path_of = {cid: path for path, cid in self.paths.items()}

    def set_users(self, users: dict[int, MumbleUser]) -> None:
        self.users = users
        # Ueber alle Sitzungs-Woerterbuecher aufraeumen, nicht nur ueber
        # self.loss: der VOX-Verdacht entsteht in note_activity fuer *jeden*
        # Client, auch ohne Monitor-Bot. Ist der Bot aus (MONITOR_ENABLED=false)
        # oder liefert er nichts, bleibt self.loss leer -- und mit ihr als Mass
        # wurde nie etwas geloescht. murmur vergibt Sitzungsnummern
        # aufsteigend, also waere das ein Eintrag pro Verbindung, fuer immer.
        bekannt = set(self.loss) | set(self.jitter) | set(self.stats_seen) | set(self._vox)
        for session in bekannt - set(users):
            self.loss.pop(session, None)
            self.jitter.pop(session, None)
            self.stats_seen.pop(session, None)
            self._vox.pop(session, None)
        self.last_update = time.time()

    def apply_config(self, networks: list[NetworkSpec], expected: dict[str, str]) -> None:
        self.networks = NetworkMap(networks)
        self.expected_channels = dict(expected)

    def note_user(self, user: MumbleUser) -> None:
        """Einzelner Client aus einem Ice-Callback."""
        self.users[user.session] = user
        self.last_update = time.time()

    def drop_user(self, session: int) -> None:
        self.users.pop(session, None)
        self.loss.pop(session, None)
        self.jitter.pop(session, None)
        self.stats_seen.pop(session, None)
        self._vox.pop(session, None)
        self.last_update = time.time()

    def note_stats(self, session: int, loss_pct: float, jitter_ms: float) -> None:
        """Messwert vom Monitor-Bot. Nur aus dem asyncio-Loop aufrufen."""
        self.loss[session] = loss_pct
        self.jitter[session] = jitter_ms
        self.stats_seen[session] = time.time()

    def clear_stats(self) -> None:
        """Alle Messwerte des Bots verwerfen -- wenn er die Verbindung verliert."""
        self.loss.clear()
        self.jitter.clear()
        self.stats_seen.clear()

    def loss_pct(self, session: int) -> float | None:
        """Verlust, sofern die Messung noch aktuell genug ist -- sonst ``None``.

        Ein alter Wert ist schlimmer als gar keiner: er sieht aus wie eine
        Messung, ist aber eine Erinnerung. Nach :attr:`STATS_MAX_AGE_S` gilt er
        als unbekannt, das Cockpit zeigt wieder ``–`` und die Alarmschwelle
        schlaegt nicht mehr darauf an.
        """
        gesehen = self.stats_seen.get(session)
        if gesehen is None or time.time() - gesehen > self.STATS_MAX_AGE_S:
            return None
        return self.loss.get(session)

    def note_activity(self) -> None:
        """Zaehlt die VOX-Verdachtsreihen fort. Einmal je Polling-Durchlauf."""
        now = time.time()
        for session, user in self.users.items():
            suspicion = self._vox.setdefault(session, _VoxSuspicion())
            suspicion.last_seen = now
            if user.bytes_per_sec > 0 and user.idle_secs == 0:
                suspicion.streak += 1
            else:
                suspicion.streak = 0

    def vox_suspects(self) -> set[int]:
        return {
            session
            for session, suspicion in self._vox.items()
            if suspicion.streak >= self.VOX_STREAK
        }

    # ------------------------------------------------------------------ #
    #  Abgeleitete Sichten
    # ------------------------------------------------------------------ #

    def channel_name(self, channel_id: int) -> str:
        """Anzeigename eines Platzes.

        Der oberste heisst immer :data:`~intercom.woerter.UEBERALL` -- murmur
        traegt dort je nach Alter des Servers "Root" oder gar nichts ein, und
        beides ist vor Ort keine Auskunft.
        """
        if channel_id == 0:
            return UEBERALL
        return self.path_of.get(channel_id) or UEBERALL

    def user_rows(self, deviations: list[Any] | None = None) -> list[dict[str, Any]]:
        """Eine Zeile je verbundenem Client -- die Datengrundlage der Tabelle."""
        deviation_map: dict[int, list[str]] = {}
        for deviation in deviations or []:
            deviation_map.setdefault(deviation.session, []).append(deviation.kind)

        vox = self.vox_suspects()
        rows: list[dict[str, Any]] = []
        for session, user in self.users.items():
            row = user.to_json()
            row["channel_name"] = self.channel_name(user.channel)
            row["channel_path"] = self.path_of.get(user.channel, "")
            row["segment"] = self.networks.segment_for(user.address)
            row["loss_pct"] = self.loss_pct(session)
            row["jitter_ms"] = (
                self.jitter.get(session) if row["loss_pct"] is not None else None
            )
            row["stats_age"] = (
                round(time.time() - self.stats_seen[session], 1)
                if session in self.stats_seen
                else None
            )
            expected = self.expected_channels.get(user.name)
            row["expected_channel"] = expected
            row["channel_ok"] = expected is None or expected == row["channel_path"]
            row["vox_suspect"] = session in vox
            row["deviations"] = deviation_map.get(session, [])
            row["alert"] = self._user_alert_level(user, self.loss_pct(session))
            rows.append(row)
        rows.sort(key=lambda r: str(r["name"]).lower())
        return rows

    def _user_alert_level(self, user: MumbleUser, loss: float | None) -> str:
        ping = user.ping
        if ping >= self.settings.alert_ping_ms * 2:
            return "kritisch"
        if loss is not None and loss >= self.settings.alert_loss_pct * 2:
            return "kritisch"
        if ping >= self.settings.alert_ping_ms:
            return "warnung"
        if loss is not None and loss >= self.settings.alert_loss_pct:
            return "warnung"
        if user.tcp_only:
            return "warnung"
        return "ok"

    def segment_rows(self) -> list[dict[str, Any]]:
        """Netzsicht: je Segment Median-Ping, groesster Ping und Verlust."""
        buckets: dict[str, list[tuple[float, float | None]]] = {}
        for session, user in self.users.items():
            segment = self.networks.segment_for(user.address)
            buckets.setdefault(segment, []).append(
                (user.ping, self.loss_pct(session))
            )

        rows: list[dict[str, Any]] = []
        for segment, values in buckets.items():
            pings = sorted(ping for ping, _ in values if ping > 0)
            losses = [loss for _, loss in values if loss is not None]
            median = pings[len(pings) // 2] if pings else 0.0
            rows.append(
                {
                    "segment": segment,
                    "note": self.networks.note_for(segment),
                    "clients": len(values),
                    "ping_median": round(median, 1),
                    "ping_max": round(max(pings), 1) if pings else 0.0,
                    "loss_max": round(max(losses), 2) if losses else None,
                    "loss_avg": round(sum(losses) / len(losses), 2) if losses else None,
                    "level": self._segment_level(median, max(losses) if losses else None),
                }
            )
        rows.sort(
            key=lambda r: (r["level"] != "kritisch", r["level"] != "warnung", r["segment"])
        )
        return rows

    def _segment_level(self, ping: float, loss: float | None) -> str:
        if ping >= self.settings.alert_ping_ms * 2 or (
            loss is not None and loss >= self.settings.alert_loss_pct * 2
        ):
            return "kritisch"
        if ping >= self.settings.alert_ping_ms or (
            loss is not None and loss >= self.settings.alert_loss_pct
        ):
            return "warnung"
        return "ok"

    def channel_tree(self) -> list[dict[str, Any]]:
        """Kanalbaum mit Nutzern, fuer die Baumansicht im Cockpit."""
        children: dict[int, list[int]] = {}
        for channel_id, channel in self.channels.items():
            if channel.parent >= 0:
                children.setdefault(channel.parent, []).append(channel_id)

        rows = {row["session"]: row for row in self.user_rows()}
        users_by_channel: dict[int, list[dict[str, Any]]] = {}
        for user in self.users.values():
            users_by_channel.setdefault(user.channel, []).append(rows[user.session])

        def build(channel_id: int, depth: int) -> dict[str, Any]:
            channel = self.channels[channel_id]
            node = channel.to_json()
            node["path"] = self.path_of.get(channel_id, "")
            node["depth"] = depth
            node["users"] = sorted(
                users_by_channel.get(channel_id, []), key=lambda u: str(u["name"]).lower()
            )
            node["speaking"] = sum(1 for u in node["users"] if u.get("bytes_per_sec", 0) > 0)
            node["link_names"] = [self.channel_name(link) for link in channel.links]
            node["children"] = [
                build(child, depth + 1)
                for child in sorted(
                    children.get(channel_id, []),
                    key=lambda c: (self.channels[c].position, self.channels[c].name),
                )
            ]
            node["total_users"] = len(node["users"]) + sum(
                child["total_users"] for child in node["children"]
            )
            return node

        return [build(0, 0)] if 0 in self.channels else []

    def alarms(self, deviations: list[Any] | None = None) -> list[Alarm]:
        """Die Alarmleiste."""
        found: list[Alarm] = []
        settings = self.settings
        vox = self.vox_suspects()

        for session, user in self.users.items():
            ping = user.ping
            loss = self.loss_pct(session)

            if ping >= settings.alert_ping_ms:
                found.append(
                    Alarm(
                        level="kritisch" if ping >= settings.alert_ping_ms * 2 else "warnung",
                        kind="ping",
                        subject=user.name,
                        message=f"Ping {ping:.0f} ms (Schwelle {settings.alert_ping_ms:.0f} ms)",
                        session=session,
                    )
                )
            if loss is not None and loss >= settings.alert_loss_pct:
                found.append(
                    Alarm(
                        level="kritisch" if loss >= settings.alert_loss_pct * 2 else "warnung",
                        kind="verlust",
                        subject=user.name,
                        message=(
                            f"Paketverlust {loss:.1f} % "
                            f"(Schwelle {settings.alert_loss_pct:.1f} %)"
                        ),
                        session=session,
                    )
                )
            if user.tcp_only:
                found.append(
                    Alarm(
                        level="warnung",
                        kind="tcp",
                        subject=user.name,
                        message=(
                            "Nur TCP -- UDP ist blockiert. Sprache laeuft ueber die "
                            "Steuerverbindung und wird bei Last spuerbar traeger."
                        ),
                        session=session,
                    )
                )
            if session in vox:
                found.append(
                    Alarm(
                        level="warnung",
                        kind="vox",
                        subject=user.name,
                        message=(
                            "Sendet dauerhaft ohne Leerlauf -- vermutlich VOX statt PTT. "
                            "(Heuristik: Mumble meldet den Sendemodus nicht.)"
                        ),
                        session=session,
                    )
                )
            expected = self.expected_channels.get(user.name)
            if expected is not None and expected != self.path_of.get(user.channel, ""):
                found.append(
                    Alarm(
                        level="warnung",
                        kind="kanal",
                        subject=user.name,
                        message=(
                            f"Steht in {self.channel_name(user.channel)}, "
                            f"erwartet waere {expected}."
                        ),
                        session=session,
                    )
                )

        for deviation in deviations or []:
            found.append(
                Alarm(
                    level="warnung",
                    kind=deviation.kind,
                    subject=deviation.user,
                    message=deviation.detail
                    + (" (wurde nachgesetzt)" if deviation.corrected else ""),
                    session=deviation.session,
                )
            )

        order = {"kritisch": 0, "warnung": 1}
        found.sort(key=lambda a: (order.get(a.level, 2), a.kind, a.subject))
        self._alarms = found
        return found

    @property
    def current_alarms(self) -> list[Alarm]:
        return list(self._alarms)

    def snapshot(self, deviations: list[Any] | None = None) -> dict[str, Any]:
        """Alles, was das Cockpit fuer eine Aktualisierung braucht."""
        return {
            "ts": time.time(),
            "users": self.user_rows(deviations),
            "channels": self.channel_tree(),
            "segments": self.segment_rows(),
            "alarms": [a.to_json() for a in self.alarms(deviations)],
            "counts": {
                "users": len(self.users),
                "channels": len(self.channels),
                "bandwidth_bps": sum(u.bytes_per_sec for u in self.users.values()) * 8,
                "speaking": sum(1 for u in self.users.values() if u.bytes_per_sec > 0),
                # Wie viele der Verbundenen sind Personen, die der Server
                # wiedererkennt? Der Rest sind Gaeste und bekommt nur, was fuer
                # alle gilt -- im Betrieb ist das der haeufigste Grund dafuer,
                # dass jemand nicht senden darf.
                "registered": sum(1 for u in self.users.values() if u.registered),
            },
        }
