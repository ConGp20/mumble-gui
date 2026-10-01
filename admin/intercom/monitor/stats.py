"""Auswertung der ``UserStats``-Nachricht des Mumble-Protokolls.

Warum es dieses Modul gibt
--------------------------
Die Ice-Schnittstelle von murmur liefert pro verbundenem Client nur ``udpPing``,
``tcpPing``, ``bytespersec`` und ``tcponly`` -- **keinen Paketverlust**. Ein
``getUserStats`` gibt es in der Slice nicht (geprueft gegen ``MumbleServer.ice``).
Die Zaehler ``good/late/lost/resync`` je Richtung stehen ausschliesslich in der
``UserStats``-Nachricht des Mumble-Protokolls, die nur ein angemeldeter Client
abfragen kann. Genau dafuer haengt der Monitor-Bot im Server; dieses Modul
uebersetzt seine Antworten in ein Modell, mit dem der Rest der Anwendung
arbeitet -- so wie ``intercom.ice.types`` das fuer Ice tut.

Die Falle: kumulative Zaehler
-----------------------------
``good``, ``late``, ``lost`` und ``resync`` zaehlen **seit Verbindungsbeginn**
hoch und werden nie zurueckgesetzt. Wer sie direkt als Prozentwert anzeigt,
zeigt den Durchschnitt der gesamten Sitzung: ein Client, der in der ersten
Minute 20 % verloren hat und seitdem sauber laeuft, steht nach drei Stunden
immer noch bei einem alarmierenden Wert -- und umgekehrt verschwindet ein
akuter Ausfall im Mittel einer langen Sitzung. Fuer eine Anzeige, die dem
Regie-Personal waehrend des Spiels etwas nuetzt, zaehlt allein die **Differenz
zum letzten Abruf**. Die bildet :class:`LossTracker`.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from ..ice.types import decode_address, decode_version

__all__ = [
    "IntervalLoss",
    "LossTracker",
    "UserStatsSample",
    "sha1_cert_hash",
]


def sha1_cert_hash(der: bytes) -> str:
    """Zertifikatshash so, wie murmur ihn fuehrt.

    murmur speichert in ``ServerUser::qsHash`` den **SHA-1 des Blattzertifikats
    in DER-Form** als Hex (``Server.cpp``, ``cert.digest(QCryptographicHash::Sha1)``).
    Genau dieser Wert steht in der Nutzerregistrierung (``UserHash``), also muss
    er hier identisch berechnet werden -- sonst findet das Cockpit den Bot in
    der Registrierung nicht wieder.
    """
    if not der:
        return ""
    return hashlib.sha1(der).hexdigest()


def _loss_pct(good: int, late: int, lost: int) -> float | None:
    """Verlust in Prozent, oder ``None``, wenn in dem Zeitraum nichts lief.

    ``late`` steht im Nenner, nicht im Zaehler: ein verspaetetes Paket ist
    angekommen. Es verschlechtert die Sprachqualitaet (der Jitterpuffer muss es
    wegwerfen oder wachsen), aber es ist kein Verlust -- Mumble selbst rechnet
    an dieser Stelle genauso.

    ``None`` statt ``0.0``, wenn ueberhaupt keine Pakete gezaehlt wurden. Das
    ist kein Sonderfall, sondern der Normalfall fuer jeden Client, der auf TCP
    zurueckgefallen ist: dessen UDP-Krypto-Zaehler bleiben fuer immer auf 0.
    ``0.0 %`` waere dort schlicht gelogen -- "unbekannt" ist die richtige
    Aussage, und die Alarmschwelle darf darauf nicht anschlagen.
    """
    total = good + late + lost
    if total <= 0:
        return None
    return round(100.0 * lost / total, 3)


# --------------------------------------------------------------------------- #
#  Momentaufnahme
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class UserStatsSample:
    """Eine ``UserStats``-Antwort des Servers, entpackt.

    Die Zaehlerfelder sind **kumulativ seit Verbindungsbeginn** -- siehe
    Modul-Docstring. Die Eigenschaften :attr:`loss_pct_from_client`,
    :attr:`loss_pct_to_client` und :attr:`loss_pct` geben deshalb den Mittelwert
    der ganzen Sitzung. Fuer die Anzeige im Cockpit ist :class:`LossTracker`
    zustaendig.

    Was der Server tatsaechlich fuellt, haengt von den Rechten des Bots ab
    (``Server::msgUserStats``):

    * immer: ``session``, Ping- und Paketzaehler, ``onlinesecs``
    * nur wenn der Bot im selben Kanal steht **oder** ``Register`` am
      Wurzelkanal hat: ``from_client``/``from_server``, ``bandwidth``, ``idlesecs``
    * nur bei ``Register`` am Wurzelkanal: Zertifikate, ``version``, ``opus``,
      ``address``

    Fehlende Felder kommen als 0 bzw. leerer String an. Genau deshalb braucht
    der Bot ``Register`` am Wurzelkanal -- ohne das gibt es keinen Paketverlust.
    In murmur 1.5.735 gemessen; aeltere Fassungen fragten ``Ban`` ab (D-035).
    """

    ts: float
    session: int
    name: str = ""

    tcp_ping_avg_ms: float = 0.0
    tcp_ping_var: float = 0.0
    tcp_packets: int = 0

    udp_ping_avg_ms: float = 0.0
    udp_ping_var: float = 0.0
    udp_packets: int = 0

    # Client -> Server, vom Server selbst gemessen.
    from_client_good: int = 0
    from_client_late: int = 0
    from_client_lost: int = 0
    from_client_resync: int = 0

    # Server -> Client, aus der Ping-Nachricht des Clients uebernommen.
    from_server_good: int = 0
    from_server_late: int = 0
    from_server_lost: int = 0
    from_server_resync: int = 0

    bandwidth_bps: int = 0
    onlinesecs: int = 0
    idlesecs: int = 0
    opus: bool = False
    certificate_count: int = 0
    cert_hash: str = ""
    address: str = ""
    version: str = ""

    # ------------------------------------------------------------------ #

    @property
    def loss_pct_from_client(self) -> float | None:
        """Verlust auf dem Weg Client -> Server, seit Verbindungsbeginn."""
        return _loss_pct(self.from_client_good, self.from_client_late, self.from_client_lost)

    @property
    def loss_pct_to_client(self) -> float | None:
        """Verlust auf dem Weg Server -> Client, seit Verbindungsbeginn."""
        return _loss_pct(self.from_server_good, self.from_server_late, self.from_server_lost)

    @property
    def loss_pct(self) -> float | None:
        """Der **schlechtere** der beiden Werte.

        Ein Intercom ist bidirektional. Ein Kameramann, den die Regie
        einwandfrei hoert, der aber selbst nichts versteht, ist genauso
        arbeitsunfaehig wie umgekehrt. Der Mittelwert wuerde so einen Ausfall
        halbieren und unter die Alarmschwelle druecken; deshalb zaehlt die
        schlechtere Richtung.
        """
        values = [v for v in (self.loss_pct_from_client, self.loss_pct_to_client) if v is not None]
        if not values:
            return None
        return max(values)

    @property
    def jitter_ms(self) -> float:
        """Standardabweichung des Pings in ms -- das, was Mumble "Jitter" nennt.

        ``*_ping_var`` ist die Varianz, die der Client selbst gemeldet hat; die
        Wurzel daraus ist die Groesse, die zum Ping vergleichbar ist. UDP hat
        Vorrang, weil darueber die Sprache laeuft; ohne UDP-Pakete bleibt nur
        der TCP-Wert.

        Achtung fuer den Bot selbst: pymumble berechnet seine ``var`` mit einer
        Formel, die keine Varianz ist und nie abklingt (``mumble.py``,
        ``ping_response``). Der Jitter des Bots ist damit unbrauchbar -- sein
        Ping (:attr:`MonitorBot.own_ping_ms`) dagegen nicht.
        """
        variance = self.udp_ping_var if self.udp_packets > 0 else self.tcp_ping_var
        if variance <= 0.0:
            return 0.0
        return round(math.sqrt(variance), 2)

    @property
    def ping_ms(self) -> float:
        """Der aussagekraeftigere Ping -- UDP, sonst TCP.

        Gleiche Begruendung wie in :attr:`intercom.ice.types.MumbleUser.ping`:
        ein Client ohne UDP ist ohnehin ein Fall fuer die Fehlersuche.
        """
        return self.udp_ping_avg_ms if self.udp_packets > 0 else self.tcp_ping_avg_ms

    @property
    def udp_active(self) -> bool:
        """Laeuft die Sprache ueber UDP oder tunnelt der Client durch TCP?"""
        return self.udp_packets > 0

    # ------------------------------------------------------------------ #

    @classmethod
    def from_protobuf(
        cls, message: Any, name: str = "", ts: float | None = None
    ) -> UserStatsSample:
        """``mumble_pb2.UserStats`` -> :class:`UserStatsSample`.

        ``message`` wird nur ueber Attributzugriffe gelesen, damit diese
        Funktion ohne installiertes pymumble testbar bleibt. Nicht gesetzte
        Protobuf-Felder liefern die Nullwerte ihres Typs, verschachtelte
        Nachrichten ein leeres Exemplar -- eine Fallunterscheidung ist deshalb
        nirgends noetig.

        ``name`` liefert der Aufrufer, weil ``UserStats`` keinen Namen enthaelt;
        der steht nur in der Nutzerliste des Clients.
        """
        from_client = message.from_client
        from_server = message.from_server
        certificates = list(message.certificates)
        version = message.version

        return cls(
            ts=time.time() if ts is None else ts,
            session=int(message.session),
            name=name,
            tcp_ping_avg_ms=round(float(message.tcp_ping_avg), 2),
            tcp_ping_var=float(message.tcp_ping_var),
            tcp_packets=int(message.tcp_packets),
            udp_ping_avg_ms=round(float(message.udp_ping_avg), 2),
            udp_ping_var=float(message.udp_ping_var),
            udp_packets=int(message.udp_packets),
            from_client_good=int(from_client.good),
            from_client_late=int(from_client.late),
            from_client_lost=int(from_client.lost),
            from_client_resync=int(from_client.resync),
            from_server_good=int(from_server.good),
            from_server_late=int(from_server.late),
            from_server_lost=int(from_server.lost),
            from_server_resync=int(from_server.resync),
            bandwidth_bps=int(message.bandwidth),
            onlinesecs=int(message.onlinesecs),
            idlesecs=int(message.idlesecs),
            opus=bool(message.opus),
            certificate_count=len(certificates),
            # Nur das Blattzertifikat zaehlt -- murmur hasht ausschliesslich das.
            cert_hash=sha1_cert_hash(certificates[0]) if certificates else "",
            address=decode_address(message.address),
            version=decode_version(
                int(getattr(version, "version_v1", 0) or 0),
                int(getattr(version, "version_v2", 0) or 0),
            ),
        )

    def to_json(self) -> dict[str, Any]:
        """Fassung fuer den Web-Layer."""
        data = asdict(self)
        data["loss_pct_from_client"] = self.loss_pct_from_client
        data["loss_pct_to_client"] = self.loss_pct_to_client
        data["loss_pct"] = self.loss_pct
        data["jitter_ms"] = self.jitter_ms
        data["ping_ms"] = self.ping_ms
        data["udp_active"] = self.udp_active
        return data


# --------------------------------------------------------------------------- #
#  Differenz zweier Momentaufnahmen
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntervalLoss:
    """Was zwischen zwei Abrufen passiert ist -- der Wert, den das Cockpit zeigt.

    Alle Zaehlerfelder sind **Differenzen**, keine Absolutwerte. ``has_rate``
    ist ``False``, solange es keinen brauchbaren Vorgaenger gibt (erster Abruf
    nach dem Verbinden, oder Neustart der Zaehler); dann sind alle Differenzen
    0 und die Prozentwerte ``None``.
    """

    session: int
    seconds: float

    from_client_good: int = 0
    from_client_late: int = 0
    from_client_lost: int = 0
    from_client_resync: int = 0

    from_server_good: int = 0
    from_server_late: int = 0
    from_server_lost: int = 0
    from_server_resync: int = 0

    has_rate: bool = False
    #: Die Zaehler sind zurueckgesprungen -- neue Verbindung unter gleicher Session.
    restarted: bool = False

    # ------------------------------------------------------------------ #

    @property
    def packets_from_client(self) -> int:
        return self.from_client_good + self.from_client_late + self.from_client_lost

    @property
    def packets_to_client(self) -> int:
        return self.from_server_good + self.from_server_late + self.from_server_lost

    @property
    def loss_pct_from_client(self) -> float | None:
        if not self.has_rate:
            return None
        return _loss_pct(self.from_client_good, self.from_client_late, self.from_client_lost)

    @property
    def loss_pct_to_client(self) -> float | None:
        if not self.has_rate:
            return None
        return _loss_pct(self.from_server_good, self.from_server_late, self.from_server_lost)

    @property
    def loss_pct(self) -> float | None:
        """Schlechtere Richtung -- Begruendung wie bei :attr:`UserStatsSample.loss_pct`."""
        values = [v for v in (self.loss_pct_from_client, self.loss_pct_to_client) if v is not None]
        if not values:
            return None
        return max(values)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["packets_from_client"] = self.packets_from_client
        data["packets_to_client"] = self.packets_to_client
        data["loss_pct_from_client"] = self.loss_pct_from_client
        data["loss_pct_to_client"] = self.loss_pct_to_client
        data["loss_pct"] = self.loss_pct
        return data


class LossTracker:
    """Haelt je Session die vorigen Zaehler und liefert die Rate im Intervall.

    Nicht thread-sicher. Der Monitor-Bot ruft :meth:`update` ausschliesslich aus
    dem pymumble-Thread auf; wer ihn von mehreren Threads fuettert, muss selbst
    sperren. Das ist Absicht -- eine Sperre je Messwert waere Verschwendung, und
    der Aufrufer haelt ohnehin schon eine.

    Zurueckgesetzt wird in zwei Faellen:

    * **Session-Wechsel.** murmur vergibt Session-IDs wieder. Verbindet sich ein
      Client neu und bekommt dieselbe ID, faengt er bei 0 an. Erkennbar an
      ``onlinesecs``, das zurueckspringt.
    * **Rueckwaerts laufende Zaehler.** Dasselbe Symptom aus anderer Ursache
      (Krypto-Resync, Serverneustart). In beiden Faellen ist die Differenz
      negativ und damit sinnlos; ein Intervall wird geopfert, dafuer stimmt der
      naechste Wert.
    """

    def __init__(self) -> None:
        self._previous: dict[int, UserStatsSample] = {}

    # ------------------------------------------------------------------ #

    def update(self, sample: UserStatsSample) -> IntervalLoss:
        """Nimmt eine Momentaufnahme auf und liefert die Differenz zur vorigen."""
        previous = self._previous.get(sample.session)
        self._previous[sample.session] = sample

        if previous is None:
            return IntervalLoss(session=sample.session, seconds=0.0, has_rate=False)

        if _counters_restarted(previous, sample):
            return IntervalLoss(
                session=sample.session, seconds=0.0, has_rate=False, restarted=True
            )

        return IntervalLoss(
            session=sample.session,
            seconds=round(max(sample.ts - previous.ts, 0.0), 3),
            from_client_good=sample.from_client_good - previous.from_client_good,
            from_client_late=sample.from_client_late - previous.from_client_late,
            from_client_lost=sample.from_client_lost - previous.from_client_lost,
            from_client_resync=sample.from_client_resync - previous.from_client_resync,
            from_server_good=sample.from_server_good - previous.from_server_good,
            from_server_late=sample.from_server_late - previous.from_server_late,
            from_server_lost=sample.from_server_lost - previous.from_server_lost,
            from_server_resync=sample.from_server_resync - previous.from_server_resync,
            has_rate=True,
        )

    def forget(self, session: int) -> None:
        """Vergisst eine Session -- aufzurufen, wenn ein Client sich trennt."""
        self._previous.pop(session, None)

    def prune(self, active: Iterable[int]) -> None:
        """Wirft alles weg, was nicht mehr verbunden ist.

        Ohne das waechst der Speicher ueber ein langes Spiel mit jedem
        Verbindungsabbruch, weil murmur fuer jede neue Verbindung eine neue
        Session-ID vergibt.
        """
        alive = set(active)
        for session in [s for s in self._previous if s not in alive]:
            del self._previous[session]

    def reset(self) -> None:
        """Alles vergessen -- nach einem Verbindungsabbruch des Bots.

        Nach einem Reconnect ist jede fruehere Session-ID bedeutungslos, und die
        Zaehler der Clients sind weitergelaufen, waehrend niemand hingesehen hat.
        """
        self._previous.clear()

    def __len__(self) -> int:
        return len(self._previous)

    def __contains__(self, session: object) -> bool:
        return session in self._previous


def _counters_restarted(previous: UserStatsSample, current: UserStatsSample) -> bool:
    """Sind die Zaehler zurueckgesprungen?"""
    if current.onlinesecs < previous.onlinesecs:
        return True
    return (
        current.from_client_good < previous.from_client_good
        or current.from_client_late < previous.from_client_late
        or current.from_client_lost < previous.from_client_lost
        or current.from_client_resync < previous.from_client_resync
        or current.from_server_good < previous.from_server_good
        or current.from_server_late < previous.from_server_late
        or current.from_server_lost < previous.from_server_lost
        or current.from_server_resync < previous.from_server_resync
    )
