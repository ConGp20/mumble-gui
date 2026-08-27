"""Passiver Mumble-Client, der ``UserStats`` fuer alle Clients abfragt.

Warum es diesen Bot ueberhaupt gibt
-----------------------------------
Die Ice-Schnittstelle von murmur liefert pro Client nur ``udpPing``, ``tcpPing``,
``bytespersec`` und ``tcponly`` -- **keinen Paketverlust**. Ein ``getUserStats``
existiert in der Slice nicht (geprueft gegen ``MumbleServer.ice``). Die Zaehler
``good/late/lost/resync`` je Richtung stehen ausschliesslich in der
``UserStats``-Nachricht des Mumble-Protokolls, und die beantwortet murmur nur
gegenueber einem angemeldeten Client. Deshalb haengt hier ein zusaetzlicher,
stummer und tauber Client im Server und fragt sie ab.

pymumble kann das von Haus aus nicht
------------------------------------
``PYMUMBLE_MSG_TYPES_USERSTATS = 22`` ist in pymumble zwar definiert, aber die
einzige Fundstelle im Code ist der eingehende Zweig in
``Mumble.dispatch_control_message``, der die Nachricht parst und **verwirft**.
Es gibt keinen Rueckruf, keine ``messages.Cmd``-Klasse und keinen Zweig in
``treat_command``. Beides wird hier nachgeruestet: :class:`_UserStatsCmd` als
Kommando und :class:`_MonitorMumble` mit je einem eigenen Zweig fuer Ein- und
Ausgang. Der Ausgang laeuft ueber die Kommandowarteschlange, weil
``control_socket.send()`` in pymumble durch keine Sperre geschuetzt ist -- wer
aus einem Fremdthread schreibt, schiebt seine Bytes mitten in den Ping-Frame
des pymumble-Threads und zerstoert den Rahmenstrom.

Rechte
------
``Server::msgUserStats`` gibt ``from_client``/``from_server`` nur heraus, wenn
der Anfragende im selben Kanal steht **oder** ``Ban`` am Wurzelkanal besitzt.
Der Bot muss also entweder in der Gruppe ``admin`` am Wurzelkanal stehen oder
er sieht Paketverlust nur fuer die Clients seines eigenen Kanals. Ohne Rechte
antwortet murmur mit ``PermissionDenied``; das landet in :attr:`MonitorBot.last_error`.

Abhaengigkeiten
---------------
Neben ``pymumble`` (Fassung 1.7 vom Zweig ``pymumble_py3``; die PyPI-Fassung
1.6.1 importiert ``opuslib`` beim Modulimport und braucht damit ``libopus``,
das wir nicht wollen) benoetigt dieses Modul **``cryptography``**, und zwar
ausschliesslich zum Erzeugen des selbstsignierten Client-Zertifikats in
:func:`ensure_certificate`. Die Standardbibliothek kann kein X.509 ausstellen --
``ssl`` liest Zertifikate, schreibt aber keine. Gehoert also in die
Abhaengigkeiten der ``pyproject.toml``:
``pymumble @ git+https://github.com/azlux/pymumble@pymumble_py3``,
``protobuf==3.20.3`` (die mitgelieferte ``mumble_pb2.py`` ist vor-3.19-Code) und
``cryptography``.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import random
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any

from ..config import Settings
from .stats import UserStatsSample

log = logging.getLogger(__name__)

__all__ = [
    "PYMUMBLE_AVAILABLE",
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_DISABLED",
    "STATE_FAILED",
    "STATE_STOPPED",
    "STATE_WAITING",
    "MonitorBot",
    "backoff_delay",
    "certificate_fingerprint",
    "ensure_certificate",
    "resolve_channel_path",
    "split_channel_path",
]


#: Zustaende, die :attr:`MonitorBot.state` annehmen kann. Sichtbarer Text.
STATE_STOPPED = "gestoppt"
STATE_DISABLED = "aus"
STATE_CONNECTING = "verbindet"
STATE_CONNECTED = "verbunden"
STATE_WAITING = "wartet"
STATE_FAILED = "fehler"

#: Name unseres zusaetzlichen Rueckrufs in ``mumble.callbacks``.
_CLBK_USERSTATS = "user_stats_received"

#: Name unseres zusaetzlichen Kommandos in der Warteschlange.
_CMD_USERSTATS = "request_user_stats"

#: ``connected == 2`` heisst verbunden (``PYMUMBLE_CONN_STATE_CONNECTED``).
_CONN_CONNECTED = 2

#: Schleifentakt des pymumble-Threads. 10 ms sind fuer 20-ms-Audiopakete
#: gedacht; ohne Audio reichen 50 ms und sparen 80 % der Aufwachvorgaenge.
_LOOP_RATE_S = 0.05

#: Untergrenze fuer das Abfrageintervall. Darunter erzeugt das Abfragen mehr
#: Last als Erkenntnis -- dieselbe Begruendung wie bei ``POLL_INTERVAL_MS``.
_MIN_INTERVAL_S = 0.25

#: So lange muss eine Verbindung getragen haben, bevor der Backoff-Zaehler
#: zurueckgesetzt wird.
#:
#: Der haeufigste Dauerfehler ist einer, bei dem die Anmeldung *gelingt* und
#: murmur den Bot gleich danach wieder loswird -- Name schon vergeben,
#: Zertifikat abgelehnt, Ban. Wird der Zaehler direkt nach dem Verbinden
#: genullt, wartet der naechste Versuch wieder nur die Grundzeit, und der Bot
#: haemmert im Sekundentakt gegen den Server, statt sich zurueckzuziehen.
_STABIL_S = 30.0

try:
    import pymumble_py3
    from pymumble_py3 import messages, mumble_pb2
    from pymumble_py3.constants import (
        PYMUMBLE_CLBK_PERMISSIONDENIED,
        PYMUMBLE_MSG_TYPES_UDPTUNNEL,
        PYMUMBLE_MSG_TYPES_USERSTATS,
    )

    PYMUMBLE_AVAILABLE = True
except ImportError:  # pragma: no cover - haengt an der Installation, nicht am Code
    PYMUMBLE_AVAILABLE = False


# --------------------------------------------------------------------------- #
#  Reine Funktionen -- ohne pymumble testbar
# --------------------------------------------------------------------------- #


def backoff_delay(
    attempt: int,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: float = 0.25,
    rng: Callable[[], float] = random.random,
) -> float:
    """Wartezeit vor dem ``attempt``-ten Verbindungsversuch, in Sekunden.

    Verdopplung ab ``base``, gedeckelt bei ``cap``, plus bis zu ``jitter``-fach
    Zufall obendrauf. Der Deckel verhindert, dass der Bot nach einer langen
    Serverwartung stundenlang schweigt; der Zufall verhindert, dass Bot und
    Cockpit nach einem gemeinsamen Ausfall im Gleichschritt auf den gerade erst
    hochfahrenden murmur einschlagen.

    ``rng`` ist herausgezogen, damit die Berechnung im Test ohne Zufall
    nachrechenbar ist.
    """
    steps = max(int(attempt), 1) - 1
    # Ohne Deckel auf dem Exponenten wird 2**steps fuer grosse Zaehler zu einer
    # riesigen Ganzzahl, bevor min() sie wieder wegwirft.
    delay = cap if steps > 32 else min(base * (2.0**steps), cap)
    return delay + delay * jitter * max(0.0, min(1.0, rng()))


def split_channel_path(path: str) -> tuple[str, ...]:
    """``"Intercom/Regie"`` -> ``("Intercom", "Regie")``.

    Leere Segmente fallen weg, damit ``"/Intercom//Regie/"`` dasselbe ergibt.
    Ein leerer Pfad bedeutet den Wurzelkanal.
    """
    return tuple(part for part in path.replace("\\", "/").split("/") if part.strip())


def resolve_channel_path(channels: Mapping[int, Any], path: str) -> int | None:
    """Kanalpfad -> Kanal-ID, oder ``None``, wenn es ihn nicht gibt.

    ``channels`` ist eine Abbildung ``id -> Objekt mit den Schluesseln
    "channel_id", "name" und "parent"`` -- also genau ``mumble.channels``, aber
    auch jedes Woerterbuch mit denselben Schluesseln, damit die Aufloesung ohne
    laufenden Server testbar bleibt.

    Aufgeloest wird **nach Pfad**, nicht nach blossem Namen. Das ist kein
    Detail: in einem Stadion-Setup heissen Unterkanaele zwangslaeufig mehrfach
    gleich (``Intercom/Regie/Technik`` und ``Presse/Technik``), und pymumbles
    ``find_by_name`` liefert davon irgendeinen. Zusaetzlich stolpert pymumbles
    ``find_by_tree`` ueber Zeichenketten: es iteriert sie Zeichen fuer Zeichen,
    weil seine Listenpruefung ein No-Op ist.

    Gross-/Kleinschreibung: erst exakt, dann als zweiter Versuch ohne Ruecksicht
    darauf. murmur unterscheidet Kanalnamen zwar, aber ein ``intercom/regie`` in
    der ``.env`` ist ein Tippfehler und kein Grund, den Bot heimatlos zu lassen.
    """
    segments = split_channel_path(path)
    current = 0
    for segment in segments:
        children = [
            channel
            for channel in channels.values()
            if _channel_field(channel, "parent") == current
            and _channel_field(channel, "channel_id") != current
        ]
        match = _pick_child(children, segment)
        if match is None:
            return None
        current = int(_channel_field(match, "channel_id"))
    return current


def _pick_child(children: list[Any], name: str) -> Any | None:
    for channel in children:
        if _channel_field(channel, "name") == name:
            return channel
    lowered = name.casefold()
    for channel in children:
        if str(_channel_field(channel, "name") or "").casefold() == lowered:
            return channel
    return None


def _channel_field(channel: Any, key: str) -> Any:
    """Liest ein Feld aus einem pymumble-``Channel`` oder einem Woerterbuch.

    Der Wurzelkanal hat in pymumble kein ``parent``; ``get`` liefert dort
    ``None``, was nie mit einer Kanal-ID uebereinstimmt -- genau richtig, denn
    die Wurzel ist Kind von niemandem.
    """
    if isinstance(channel, Mapping):
        return channel.get(key)
    return getattr(channel, key, None)


# --------------------------------------------------------------------------- #
#  Zertifikat
# --------------------------------------------------------------------------- #

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def _first_certificate_block(text: str) -> str:
    """Schneidet das erste PEM-Zertifikat aus einer Datei heraus.

    Unsere Datei enthaelt Zertifikat **und** privaten Schluessel;
    ``ssl.PEM_cert_to_DER_cert`` verlangt aber eine Zeichenkette, die mit dem
    Footer endet. Also erst zuschneiden.
    """
    start = text.find(_PEM_BEGIN)
    end = text.find(_PEM_END, start + 1)
    if start < 0 or end < 0:
        raise ValueError("Die Zertifikatsdatei enthaelt kein PEM-Zertifikat.")
    return text[start : end + len(_PEM_END)] + "\n"


def certificate_fingerprint(path: Path) -> str:
    """SHA-1 des Blattzertifikats in DER-Form, hexadezimal.

    Das ist exakt der Wert, unter dem murmur den Client kennt (``UserHash`` in
    der Registrierung). Der Betreiber traegt ihn im Cockpit ein, um den Bot als
    registrierten Nutzer anzulegen und ihm die Gruppe ``admin`` zu geben.
    """
    der = ssl.PEM_cert_to_DER_cert(_first_certificate_block(path.read_text()))
    return hashlib.sha1(der).hexdigest()


def ensure_certificate(path: Path, common_name: str) -> str:
    """Sorgt dafuer, dass unter ``path`` ein Client-Zertifikat liegt.

    Existiert die Datei, wird sie unveraendert benutzt -- der Betreiber hat den
    Bot dann bereits unter diesem Hash registriert, und ein neues Zertifikat
    wuerde ihm die Rechte unter den Fuessen wegziehen. Andernfalls entsteht ein
    selbstsigniertes Zertifikat mit zehn Jahren Laufzeit.

    Zehn Jahre, weil ein abgelaufenes Bot-Zertifikat mitten in der Saison den
    Handschlag scheitern liesse und der Fehler (TLS-Fehler sind in pymumble von
    Netzfehlern nicht unterscheidbar) kaum zu finden waere. Eine eigene CA gibt
    es bewusst nicht: murmur prueft Client-Zertifikate nicht gegen eine
    Vertrauenskette, es merkt sich nur ihren SHA-1.

    Braucht ``cryptography`` -- siehe Modul-Docstring. Rueckgabe ist der
    Fingerabdruck.
    """
    if path.exists() and path.stat().st_size > 0:
        return certificate_fingerprint(path)

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - Abhaengigkeit fehlt
        raise RuntimeError(
            "Das Paket 'cryptography' fehlt. Es wird gebraucht, um dem "
            "Monitor-Bot ein Zertifikat auszustellen; die Standardbibliothek "
            "kann kein X.509 erzeugen."
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = time.time()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Eine Stunde Vorlauf: Container starten oft, bevor NTP die Uhr
        # gerichtet hat, und ein Zertifikat aus der Zukunft ist ungueltig.
        .not_valid_before(_utc(now - 3600))
        .not_valid_after(_utc(now + 10 * 365 * 24 * 3600))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    pem = certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Zertifikat zuerst: OpenSSL erwartet das Blattzertifikat am Dateianfang.
    path.write_bytes(pem)
    path.chmod(0o600)
    log.info("Zertifikat fuer den Monitor-Bot erzeugt: %s", path)
    return certificate_fingerprint(path)


def _utc(timestamp: float) -> Any:
    from datetime import datetime

    # naiv in UTC: aeltere cryptography-Fassungen verweigern bewusste Zeitzonen.
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
#  pymumble-Erweiterung
# --------------------------------------------------------------------------- #

if PYMUMBLE_AVAILABLE:

    # pymumble bringt keine Typinformationen mit; Cmd ist fuer mypy Any.
    class _UserStatsCmd(messages.Cmd):  # type: ignore[misc]
        """Kommando "frag ``UserStats`` fuer diese Session ab".

        pymumble bringt so etwas nicht mit. Der Umweg ueber die
        Kommandowarteschlange ist Pflicht, nicht Geschmack: nur der
        pymumble-Thread darf auf ``control_socket`` schreiben.
        """

        def __init__(self, session: int, stats_only: bool = False) -> None:
            messages.Cmd.__init__(self)
            self.cmd = _CMD_USERSTATS
            self.parameters = {"session": session, "stats_only": stats_only}

    class _MonitorMumble(pymumble_py3.Mumble):  # type: ignore[misc]
        """``Mumble`` mit einem Ein- und einem Ausgang fuer ``UserStats``.

        Zusaetzlich raeumt der Konstruktor den ``StreamHandler`` weg, den
        pymumble bedingungslos an den globalen Logger ``"PyMumble"`` haengt.
        Ohne das sammelt sich bei unserer eigenen Reconnect-Schleife pro
        Verbindungsversuch ein weiterer Handler an, und jede Meldung erscheint
        irgendwann vielfach -- an der Logging-Konfiguration der Anwendung vorbei.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            for handler in list(self.Log.handlers):
                self.Log.removeHandler(handler)
            self.callbacks[_CLBK_USERSTATS] = None

        def dispatch_control_message(self, type: int, message: bytes) -> None:
            """Eingehend, laeuft im pymumble-Thread."""
            if type == PYMUMBLE_MSG_TYPES_USERSTATS:
                stats = mumble_pb2.UserStats()
                stats.ParseFromString(message)
                self.callbacks(_CLBK_USERSTATS, stats)
                return
            super().dispatch_control_message(type, message)

        def treat_command(self, cmd: Any) -> None:
            """Ausgehend, laeuft im pymumble-Thread."""
            if cmd.cmd == _CMD_USERSTATS:
                request = mumble_pb2.UserStats()
                request.session = cmd.parameters["session"]
                request.stats_only = cmd.parameters["stats_only"]
                self.send_message(PYMUMBLE_MSG_TYPES_USERSTATS, request)
                cmd.response = True
                # Pflicht: ohne answer() bleibt ein blockierender Aufrufer
                # fuer immer an cmd.lock haengen.
                self.commands.answer(cmd)
                return
            super().treat_command(cmd)

        def send_message(self, type: int, message: Any) -> None:
            """Zweite Sperre gegen Audio -- siehe :class:`MonitorBot`.

            Jeder Audioframe verlaesst einen Mumble-Client als ``UDPTunnel``.
            Hier fliegt er raus, statt gesendet zu werden. Der Bot hat keinen
            Grund, jemals Audio zu schicken; wenn es doch passiert, ist das ein
            Programmfehler und soll laut sein.
            """
            if type == PYMUMBLE_MSG_TYPES_UDPTUNNEL:
                raise RuntimeError(
                    "Der Monitor-Bot darf kein Audio senden. Ein UDPTunnel-Frame "
                    "auf dem Weg nach draussen ist ein Programmfehler."
                )
            super().send_message(type, message)


# --------------------------------------------------------------------------- #
#  Der Bot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Merkmale:
    """Was sich waehrend einer Sitzung nicht mehr aendert."""

    certificate_count: int
    cert_hash: str
    address: str
    version: str
    opus: bool


class MonitorBot:
    """Haelt einen stummen Mumble-Client im Server und liefert ``UserStats``.

    Der Bot laeuft in einem eigenen Thread (dem "Aufseher"), der die
    Verbindung aufbaut, den pymumble-Thread startet, das Abfragen taktet und
    nach einem Abbruch mit exponentiellem Backoff neu verbindet. Ein
    Verbindungsabbruch bleibt vollstaendig in diesem Thread; er reisst den
    Prozess nicht mit.

    Kein Audio -- strukturell
    -------------------------
    Es reicht nicht, einfach nichts zu senden. Zwei Vorkehrungen:

    1. ``set_receive_sound(False)`` **vor** ``start()``. pymumble baut dann
       ``sound_output`` gar nicht erst (``init_connection``), importiert
       ``soundoutput``/``soundqueue`` nie und braucht folglich kein ``libopus``.
       Das ist die entscheidende Massnahme: der einzige Schreibzugriff auf den
       Socket ausserhalb von ``send_message`` steckt in ``SoundOutput`` -- ein
       Objekt, das nicht existiert, kann nichts senden. Die Hauptschleife ruft
       ``sound_output.send_audio()`` nur unter ``if self.sound_output``.
    2. :meth:`_MonitorMumble.send_message` weist ``UDPTunnel``-Frames ab. Damit
       ist auch der verbleibende Weg -- irgendjemand ruft ``send_message``
       direkt auf -- versperrt.

    Zusaetzlich setzt sich der Bot auf ``self_mute`` und ``self_deaf``. Das ist
    aber nur Hoeflichkeit gegenueber den anderen Clients (er erscheint als
    stumm und taub, und der Server schickt ihm keine Sprache mehr); die
    Garantie liefern die beiden Punkte oben.

    Threading
    ---------
    ``on_stats`` wird **aus dem pymumble-Thread** aufgerufen, ``on_state`` aus
    dem Aufseher-Thread. Beide muessen kurz sein und duerfen nicht blockieren --
    ein haengender Rueckruf haelt den ganzen Client an, inklusive Ping, und der
    Server wirft den Bot nach 60 Sekunden raus. Wer in einem asyncio-Loop
    arbeitet, hebt den Wert mit ``loop.call_soon_threadsafe(...)`` hinueber und
    macht dort weiter.

    Der :class:`~intercom.monitor.stats.LossTracker` gehoert bewusst **nicht**
    hierher, sondern zum Aufrufer. Er ist nicht thread-sicher; im Bot muesste
    er zwischen pymumble-Thread und Aufseher-Thread gesperrt werden, beim
    Aufrufer im asyncio-Loop dagegen gar nicht. Der Aufrufer setzt ihn zurueck,
    sobald ``on_state`` den Zustand ``"verbunden"`` meldet -- waehrend der
    Trennung sind die Clients weitergelaufen und Session-IDs koennen neu
    vergeben sein.
    """

    def __init__(
        self,
        settings: Settings,
        on_stats: Callable[[UserStatsSample], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        """``on_stats`` laeuft im pymumble-Thread, ``on_state`` im Aufseher-Thread.

        Siehe Abschnitt "Threading" in der Klassendokumentation -- beide
        Rueckrufe muessen thread-sicher und kurz sein.
        """
        self._settings = settings
        self._on_stats = on_stats
        self._on_state = on_state

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mumble: Any = None

        self._state = STATE_STOPPED
        self._last_error = ""
        self._cert_hash = ""
        self._attempt = 0
        self._random = random.Random()
        #: Session -> die Felder, die nur die Vollantwort enthaelt.
        #: Siehe :meth:`_merge_merkmale`.
        self._merkmale: dict[int, _Merkmale] = {}

    # ------------------------------------------------------------------ #
    #  Zustand
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def connected(self) -> bool:
        mumble = self._mumble
        if mumble is None:
            return False
        return bool(mumble.is_alive() and mumble.connected == _CONN_CONNECTED)

    @property
    def own_ping_ms(self) -> float:
        """Ping des Bots selbst -- die Referenz "liegt es am Server?".

        Ist dieser Wert gut und der eines Clients schlecht, liegt das Problem
        beim Client oder auf seinem Weg, nicht am Server. Ist er schlecht,
        haben alle ein Problem.

        Achtung: der Wert stammt aus pymumbles eigener Mittelwertbildung
        (``ping_stats['avg']``) und ist ein gleitender Mittelwert ueber die
        gesamte Verbindung, kein Momentanwert. Fuer die Aussage "Server-Seite
        ok?" reicht das; als Sekundenanzeige taugt er nicht.
        """
        mumble = self._mumble
        if mumble is None or not self.connected:
            return 0.0
        try:
            return round(float(mumble.ping_stats["avg"]), 2)
        except (AttributeError, KeyError, TypeError, ValueError):
            return 0.0

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def cert_hash(self) -> str:
        """SHA-1 des Bot-Zertifikats -- damit registriert der Betreiber ihn."""
        with self._lock:
            return self._cert_hash

    # ------------------------------------------------------------------ #
    #  Lebenszyklus
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Startet den Aufseher-Thread. Kehrt sofort zurueck."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._settings.monitor_enabled:
                self._set_state_locked(STATE_DISABLED)
                return
            if not PYMUMBLE_AVAILABLE:
                # Kein harter Abbruch: ohne Paketverlust ist das Cockpit
                # aermer, aber vollstaendig benutzbar. Ein toter Prozess waere
                # der schlechtere Tausch.
                self._last_error = (
                    "Das Paket 'pymumble' ist nicht installiert. Ohne den "
                    "Monitor-Bot gibt es keinen Paketverlust je Client."
                )
                log.error("%s", self._last_error)
                self._set_state_locked(STATE_FAILED)
                return
            self._stop.clear()
            self._attempt = 0
            self._thread = threading.Thread(
                target=self._run, name="monitor-bot", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Beendet den Bot und wartet hoechstens ``timeout`` Sekunden darauf."""
        self._stop.set()
        self._close_client(self._mumble)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self._thread = None
            if self._state != STATE_DISABLED:
                self._set_state_locked(STATE_STOPPED)

    # ------------------------------------------------------------------ #
    #  Aufseher-Thread
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop.is_set():
            self._attempt += 1
            begonnen = time.monotonic()
            try:
                self._set_state(STATE_CONNECTING)
                mumble = self._connect()
                self._set_state(STATE_CONNECTED)
                self._prepare(mumble)
                self._poll_loop(mumble)
            except Exception as exc:  # noqa: BLE001 - der Thread muss ueberleben
                self._fail(exc)
            finally:
                # ``self._mumble``, nicht ``mumble``: scheitert ``_connect``
                # nach ``start()``, ist die lokale Variable noch None, der
                # pymumble-Thread laeuft aber schon.
                self._close_client(self._mumble)
                self._mumble = None
                # Nach einem Reconnect vergibt murmur neue Sitzungsnummern;
                # die alten Merkmale gehoeren zu niemandem mehr.
                with self._lock:
                    self._merkmale.clear()

            # Erst jetzt, und nur wenn die Verbindung wirklich getragen hat --
            # siehe _STABIL_S.
            if time.monotonic() - begonnen >= _STABIL_S:
                self._attempt = 0

            if self._stop.is_set():
                break
            delay = backoff_delay(max(self._attempt, 1), rng=self._random.random)
            self._set_state(STATE_WAITING)
            self._stop.wait(delay)

        self._set_state(STATE_STOPPED)

    def _connect(self) -> Any:
        settings = self._settings
        fingerprint = ensure_certificate(settings.monitor_cert, settings.monitor_name)
        with self._lock:
            self._cert_hash = fingerprint

        mumble = _MonitorMumble(
            host="127.0.0.1",
            user=settings.monitor_name,
            port=settings.mumble_port,
            password=settings.monitor_password or "",
            certfile=str(settings.monitor_cert),
            # Zertifikat und Schluessel liegen in derselben Datei.
            keyfile=str(settings.monitor_cert),
            # Eigenes Backoff statt pymumbles starrer 10-Sekunden-Schleife:
            # das Mumble-Objekt ist danach einmalig und wird neu gebaut.
            reconnect=False,
            # client_type=1 markiert uns als Bot. murmur zaehlt uns dann nicht
            # als Zuhoerer und Clients zeigen uns als Bot an.
            client_type=1,
        )
        mumble.daemon = True
        # Vor start(): beides wird erst in init_connection() gelesen.
        mumble.set_receive_sound(False)
        mumble.set_loop_rate(_LOOP_RATE_S)
        mumble.set_application_string("stadion-intercom-monitor")
        mumble.callbacks.add_callback(_CLBK_USERSTATS, self._handle_stats)
        mumble.callbacks.add_callback(
            PYMUMBLE_CLBK_PERMISSIONDENIED, self._handle_permission_denied
        )

        self._mumble = mumble
        mumble.start()
        # is_ready() kehrt auch im Fehlerfall zurueck -- dieselbe Sperre wird
        # auf dem Scheiterpfad freigegeben. Der Zustand ist die Wahrheit.
        mumble.is_ready()
        if mumble.connected != _CONN_CONNECTED:
            raise ConnectionError(
                f"Anmeldung als {settings.monitor_name!r} an 127.0.0.1:"
                f"{settings.mumble_port} fehlgeschlagen. Laeuft murmur, ist der "
                "Name frei und stimmt MONITOR_BOT_PASSWORD?"
            )
        with self._lock:
            self._last_error = ""
        log.info(
            "Monitor-Bot verbunden als %s (Zertifikatshash %s)",
            settings.monitor_name,
            fingerprint[:12],
        )
        return mumble

    def _prepare(self, mumble: Any) -> None:
        """Stumm, taub, und in den vorgesehenen Kanal.

        Die Kommandos gehen bewusst nicht ueber ``User.mute()``,
        ``User.deafen()`` und ``Channel.move_in()``: die rufen
        ``execute_command`` mit ``blocking=True`` auf, und das wartet
        **ohne Zeitlimit** auf eine Sperre, die nur der pymumble-Thread
        loesen kann (``Mumble.execute_command`` -> ``lock.acquire()``,
        freigegeben erst in ``commands.answer()``). Stirbt der Thread
        dazwischen -- und genau dann sind wir hier, naemlich direkt nach dem
        Verbindungsaufbau -- haengt der Aufseher-Thread fuer immer, und mit
        ihm ``stop()`` und das Herunterfahren des Containers. Dieselbe
        Begruendung wie bei :meth:`_request_stats`.

        Stumm und taub in *einer* Nachricht: murmur wertet beide Felder aus
        demselben ``UserState`` aus, zwei Nachrichten sind nur zwei Chancen,
        dass eine davon unterwegs verlorengeht.
        """
        myself = mumble.users.myself
        if myself is None:
            raise ConnectionError(
                "Der Server hat kein ServerSync geschickt -- eigene Session unbekannt."
            )
        session = int(myself["session"])
        mumble.execute_command(
            messages.ModUserState(
                session,
                {"session": session, "self_mute": True, "self_deaf": True},
            ),
            blocking=False,
        )

        path = self._settings.monitor_channel
        channel_id = resolve_channel_path(mumble.channels, path)
        if channel_id is None:
            # Kein Abbruch: ausserhalb seines Zielkanals sieht der Bot immer
            # noch Ping und Paketzahlen, und bei Ban-Recht am Wurzelkanal sogar
            # alles. Ein Bot, der wegen eines Tippfehlers gar nicht laeuft,
            # waere schlechter.
            message = (
                f"Kanal {path!r} nicht gefunden. Der Monitor-Bot bleibt im "
                "Wurzelkanal; MONITOR_BOT_CHANNEL pruefen."
            )
            log.warning("%s", message)
            with self._lock:
                self._last_error = message
            return
        if myself.get_property("channel_id") != channel_id:
            mumble.execute_command(
                messages.MoveCmd(session, channel_id), blocking=False
            )

    def _poll_loop(self, mumble: Any) -> None:
        interval = max(self._settings.monitor_stats_interval_ms / 1000.0, _MIN_INTERVAL_S)
        while (
            not self._stop.is_set()
            and mumble.is_alive()
            and mumble.connected == _CONN_CONNECTED
        ):
            sessions = self._sessions(mumble)
            # Getrennte Clients aus dem Merkmalsspeicher werfen. murmur vergibt
            # Sitzungsnummern aufsteigend; ohne das waere es ein Eintrag pro
            # Verbindung, solange der Bot laeuft.
            with self._lock:
                for veraltet in set(self._merkmale) - set(sessions):
                    self._merkmale.pop(veraltet, None)
            if not sessions:
                self._stop.wait(interval)
                continue
            # Verteilt statt in einem Schwall: murmur beantwortet UserStats im
            # selben Thread, der die Sprache verteilt. Dreissig Anfragen auf
            # einmal erzeugen dort eine Spitze und bei uns einen Schwall
            # Antworten, den der pymumble-Thread abarbeiten muss, waehrend er
            # eigentlich Pakete lesen soll. Ueber das Intervall gestreckt
            # bleibt beides unmerklich -- und die Anzeige aktualisiert sich
            # gleichmaessig statt sprunghaft.
            slot = interval / len(sessions)
            for session in sessions:
                if self._stop.is_set() or mumble.connected != _CONN_CONNECTED:
                    return
                self._request_stats(mumble, session)
                self._stop.wait(slot)

    def _sessions(self, mumble: Any) -> list[int]:
        """Momentaufnahme der verbundenen Sessions.

        ``dict(...)`` kopiert auf C-Ebene und kann deshalb nicht mitten in einer
        Aenderung durch den pymumble-Thread stolpern -- eine Schleife ueber
        ``keys()`` koennte das.
        """
        try:
            return sorted(dict(mumble.users))
        except (AttributeError, RuntimeError, TypeError):
            return []

    def _request_stats(self, mumble: Any, session: int) -> None:
        # Beim ersten Mal die Vollantwort, danach nur noch die Zahlen --
        # siehe _merge_merkmale.
        with self._lock:
            nur_zahlen = session in self._merkmale
        try:
            # blocking=False mit Absicht: die blockierende Fassung wartet ohne
            # Zeitlimit auf eine Sperre, die nur der pymumble-Thread loesen
            # kann. Stirbt der zwischen Pruefung und Aufruf, haengt der
            # Aufseher-Thread fuer immer -- und mit ihm stop(). Die Antwort
            # kommt ohnehin asynchron ueber den Rueckruf.
            mumble.execute_command(
                _UserStatsCmd(session, stats_only=nur_zahlen), blocking=False
            )
        except Exception as exc:  # noqa: BLE001 - eine tote Session ist kein Grund aufzugeben
            log.debug("UserStats fuer Session %s nicht angefragt: %s", session, exc)

    # ------------------------------------------------------------------ #
    #  Rueckrufe -- laufen im pymumble-Thread
    # ------------------------------------------------------------------ #

    def _handle_stats(self, message: Any) -> None:
        """Antwort des Servers -> :class:`UserStatsSample` -> ``on_stats``.

        Laeuft im pymumble-Thread. Hier darf nichts nach oben durchschlagen:
        eine Ausnahme in einem Rueckruf beendet die Hauptschleife des Clients
        und damit die Verbindung.
        """
        try:
            sample = self._merge_merkmale(
                UserStatsSample.from_protobuf(
                    message, name=self._name_of(int(message.session))
                )
            )
        except Exception:
            log.exception("UserStats-Antwort nicht lesbar")
            return
        callback = self._on_stats
        if callback is None:
            return
        try:
            callback(sample)
        except Exception:
            log.exception("on_stats hat eine Ausnahme geworfen")

    def _merge_merkmale(self, sample: UserStatsSample) -> UserStatsSample:
        """Ergaenzt die Felder, die nur in der Vollantwort stehen.

        Zertifikatskette, Adresse, Clientversion und Codec stehen fuer eine
        Sitzung fest, sobald der Client verbunden ist. Sie bei *jeder* Abfrage
        mitzuschicken kostet die vollstaendige DER-Kette je Client und
        Intervall -- bei dreissig Clients alle fuenf Sekunden der mit Abstand
        groesste Posten der ganzen Ueberwachung, fuer Daten, die sich nicht
        aendern. Deshalb: einmal voll fragen, merken, danach ``stats_only``.

        Selbstheilend: gemerkt wird nur, was auch angekommen ist. Fehlt dem Bot
        das Recht ``Ban`` am Wurzelkanal, liefert murmur diese Felder gar
        nicht -- dann bleibt das Woerterbuch leer und weiter voll gefragt.
        Bekommt der Bot das Recht spaeter, greift es sofort.

        Laeuft im pymumble-Thread, ``_request_stats`` im Aufseher-Thread --
        daher die Sperre.
        """
        neu = _Merkmale(
            certificate_count=sample.certificate_count,
            cert_hash=sample.cert_hash,
            address=sample.address,
            version=sample.version,
            opus=sample.opus,
        )
        if neu.certificate_count or neu.address or neu.version:
            with self._lock:
                self._merkmale[sample.session] = neu
            return sample

        with self._lock:
            gemerkt = self._merkmale.get(sample.session)
        if gemerkt is None:
            return sample
        return replace(
            sample,
            certificate_count=gemerkt.certificate_count,
            cert_hash=gemerkt.cert_hash,
            address=gemerkt.address,
            version=gemerkt.version,
            opus=gemerkt.opus,
        )

    def _handle_permission_denied(self, message: Any) -> None:
        """murmur verweigert etwas -- fast immer fehlende Rechte fuer UserStats."""
        mumble = self._mumble
        try:
            kind = mumble.denial_type(message.type) if mumble is not None else message.type
        except Exception:  # noqa: BLE001
            kind = message.type
        text = (
            f"murmur hat abgelehnt ({kind}): {message.reason or 'ohne Begruendung'}. "
            "Fuer Paketverlust fremder Clients braucht der Monitor-Bot das "
            "Recht 'Ban' am Wurzelkanal -- sonst sieht er nur seinen eigenen Kanal."
        )
        log.warning("%s", text)
        with self._lock:
            self._last_error = text

    def _name_of(self, session: int) -> str:
        mumble = self._mumble
        if mumble is None:
            return ""
        try:
            return str(mumble.users[session]["name"])
        except (AttributeError, KeyError, TypeError):
            return ""

    # ------------------------------------------------------------------ #
    #  Kleinkram
    # ------------------------------------------------------------------ #

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._set_state_locked(state)

    def _set_state_locked(self, state: str) -> None:
        if self._state == state:
            return
        self._state = state
        callback = self._on_state
        if callback is None:
            return
        try:
            callback(state)
        except Exception:
            log.exception("on_state hat eine Ausnahme geworfen")

    def _fail(self, exc: BaseException) -> None:
        text = str(exc) or type(exc).__name__
        with self._lock:
            self._last_error = text
        log.warning("Monitor-Bot: %s", text)
        self._set_state(STATE_FAILED)

    def _close_client(self, mumble: Any) -> None:
        """Beendet den pymumble-Thread, ohne sich an seinen Eigenheiten zu stoeren.

        ``Mumble.stop()`` fasst ``control_socket`` an. Vor dem ersten
        ``init_connection`` gibt es das Attribut nicht, danach kann es ``None``
        sein -- beides wirft ``AttributeError``, und beides bedeutet nur, dass
        ohnehin nichts offen ist.
        """
        if mumble is None:
            return
        with contextlib.suppress(AttributeError, OSError):
            mumble.stop()
        if mumble.is_alive() and mumble is not threading.current_thread():
            mumble.join(2.0)
