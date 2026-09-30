"""Monitor-Bot: alles, was ohne laufenden Mumble-Server pruefbar ist.

Der Schwerpunkt liegt auf :class:`~intercom.monitor.stats.LossTracker`. Die
Zaehler in ``UserStats`` sind kumulativ; sie ungerechnet als Prozentwert
anzuzeigen waere schlicht falsch, und der Fehler faellt im Betrieb erst auf,
wenn er schon eine Fehlentscheidung ausgeloest hat. Deshalb steht hier jeder
Sonderfall: Erstabruf, Zaehlerneustart, Session-Wiederverwendung, Intervall
ohne Pakete.

Was einen Server braucht -- Anmeldung, Kanalwechsel, echte Antwortzeiten --
steht nicht hier. Statt dessen ist alles, was sich isolieren laesst, isoliert:
die Rahmenerzeugung fuer ``UserStats`` gegen einen Socket-Ersatz, die
Kanalpfad-Aufloesung gegen ein Woerterbuch, das Backoff gegen einen festen
Zufallsgenerator.
"""

from __future__ import annotations

import ipaddress
import itertools
import ssl
import struct
import threading
from pathlib import Path

import pytest

from intercom.monitor import bot as bot_module
from intercom.monitor.stats import IntervalLoss, LossTracker, UserStatsSample, sha1_cert_hash

needs_pymumble = pytest.mark.skipif(
    not bot_module.PYMUMBLE_AVAILABLE,
    reason="pymumble ist nicht installiert (pip install pymumble).",
)

try:
    import cryptography  # noqa: F401

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    CRYPTOGRAPHY_AVAILABLE = False

needs_cryptography = pytest.mark.skipif(
    not CRYPTOGRAPHY_AVAILABLE,
    reason="cryptography fehlt -- ohne das kann kein Zertifikat entstehen.",
)


# --------------------------------------------------------------------------- #
#  Hilfen
# --------------------------------------------------------------------------- #


def sample(session: int = 7, ts: float = 0.0, **counters: object) -> UserStatsSample:
    """Momentaufnahme mit Nullwerten, ueberschrieben durch ``counters``."""
    return UserStatsSample(ts=ts, session=session, **counters)  # type: ignore[arg-type]


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """Eine :class:`~intercom.config.Settings` aus kontrollierter Umgebung."""
    from intercom.config import Settings

    monkeypatch.setenv("ICE_SECRET", "test-secret")
    monkeypatch.setenv("SESSION_SECRET", "test-session")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONITOR_BOT_ENABLED", "true")
    monkeypatch.setenv("MONITOR_BOT_NAME", "monitor")
    monkeypatch.setenv("MONITOR_BOT_CHANNEL", "Intercom/Regie")
    monkeypatch.setenv("MONITOR_BOT_CERT", str(tmp_path / "monitor-cert.pem"))
    monkeypatch.setenv("MONITOR_STATS_INTERVAL_MS", "500")
    # Ein Port, auf dem sicher nichts lauscht: der Verbindungsversuch soll
    # sofort scheitern, nicht in einen Zeitablauf laufen.
    monkeypatch.setenv("MUMBLE_PORT", "64999")
    monkeypatch.delenv("MONITOR_BOT_PASSWORD", raising=False)
    return Settings.load()


# --------------------------------------------------------------------------- #
#  UserStatsSample -- abgeleitete Werte
# --------------------------------------------------------------------------- #


def test_verlust_zaehlt_late_nicht_als_verlust():
    """Ein verspaetetes Paket ist angekommen -- Nenner, nicht Zaehler."""
    s = sample(from_client_good=90, from_client_late=5, from_client_lost=5)
    assert s.loss_pct_from_client == 5.0


def test_verlust_ohne_pakete_ist_unbekannt_nicht_null():
    """Ein TCP-getunnelter Client hat gar keine Krypto-Zaehler."""
    s = sample(tcp_packets=100)
    assert s.loss_pct_from_client is None
    assert s.loss_pct_to_client is None
    assert s.loss_pct is None


def test_loss_pct_nimmt_die_schlechtere_richtung():
    s = sample(
        from_client_good=1000,
        from_client_lost=0,
        from_server_good=900,
        from_server_lost=100,
    )
    assert s.loss_pct_from_client == 0.0
    assert s.loss_pct_to_client == 10.0
    assert s.loss_pct == 10.0


def test_loss_pct_nutzt_die_bekannte_richtung_wenn_nur_eine_zaehlt():
    s = sample(from_client_good=99, from_client_lost=1)
    assert s.loss_pct_to_client is None
    assert s.loss_pct == 1.0


def test_jitter_ist_die_wurzel_der_varianz_und_bevorzugt_udp():
    s = sample(udp_packets=100, udp_ping_var=9.0, tcp_ping_var=400.0)
    assert s.jitter_ms == 3.0
    ohne_udp = sample(udp_packets=0, tcp_ping_var=4.0)
    assert ohne_udp.jitter_ms == 2.0
    assert sample().jitter_ms == 0.0


def test_ping_faellt_ohne_udp_auf_tcp_zurueck():
    mit_udp = sample(udp_packets=10, udp_ping_avg_ms=21.5, tcp_ping_avg_ms=40.0)
    assert mit_udp.ping_ms == 21.5
    assert mit_udp.udp_active is True
    ohne_udp = sample(udp_packets=0, tcp_ping_avg_ms=40.0)
    assert ohne_udp.ping_ms == 40.0
    assert ohne_udp.udp_active is False


def test_to_json_enthaelt_die_abgeleiteten_werte():
    data = sample(
        name="Kamera 1", udp_packets=10, udp_ping_var=4.0, from_client_good=100
    ).to_json()
    assert data["name"] == "Kamera 1"
    assert data["jitter_ms"] == 2.0
    assert data["loss_pct"] == 0.0
    assert data["udp_active"] is True


def test_leerer_zertifikatshash_bleibt_leer():
    assert sha1_cert_hash(b"") == ""


# --------------------------------------------------------------------------- #
#  LossTracker -- der eigentliche Kern
# --------------------------------------------------------------------------- #


def test_erster_abruf_liefert_keine_rate():
    """Ohne Vorgaenger gibt es keine Differenz -- und keinen Prozentwert."""
    tracker = LossTracker()
    result = tracker.update(sample(ts=0.0, from_client_good=1000, from_client_lost=50))
    assert result.has_rate is False
    assert result.restarted is False
    assert result.loss_pct is None
    assert result.packets_from_client == 0


def test_rate_im_intervall_statt_seit_verbindungsbeginn():
    """Der Fall, um den es geht: kumulativ harmlos, im Intervall katastrophal.

    Der Client hat in einer Stunde 100 000 Pakete sauber uebertragen und
    verliert jetzt jedes fuenfte. Kumulativ ergibt das 0,08 % -- weit unter
    jeder Alarmschwelle. Im Intervall sind es 20 %.
    """
    tracker = LossTracker()
    tracker.update(sample(ts=0.0, from_client_good=100_000, from_client_lost=80))
    result = tracker.update(
        sample(ts=5.0, from_client_good=100_400, from_client_lost=180)
    )

    assert result.has_rate is True
    assert result.seconds == 5.0
    assert result.from_client_good == 400
    assert result.from_client_lost == 100
    assert result.packets_from_client == 500
    assert result.loss_pct_from_client == 20.0

    kumulativ = sample(from_client_good=100_400, from_client_lost=180)
    assert kumulativ.loss_pct_from_client == pytest.approx(0.179, abs=0.001)


def test_beide_richtungen_und_resync_werden_differenziert():
    tracker = LossTracker()
    tracker.update(
        sample(
            ts=0.0,
            from_client_good=100,
            from_client_late=1,
            from_client_lost=2,
            from_client_resync=3,
            from_server_good=200,
            from_server_late=4,
            from_server_lost=5,
            from_server_resync=6,
        )
    )
    result = tracker.update(
        sample(
            ts=2.0,
            from_client_good=190,
            from_client_late=2,
            from_client_lost=10,
            from_client_resync=4,
            from_server_good=400,
            from_server_late=4,
            from_server_lost=5,
            from_server_resync=6,
        )
    )
    assert result.from_client_good == 90
    assert result.from_client_late == 1
    assert result.from_client_lost == 8
    assert result.from_client_resync == 1
    assert result.from_server_good == 200
    assert result.from_server_resync == 0
    assert result.loss_pct_from_client == pytest.approx(8.081, abs=0.001)
    assert result.loss_pct_to_client == 0.0
    assert result.loss_pct == pytest.approx(8.081, abs=0.001)


def test_intervall_ohne_pakete_teilt_nicht_durch_null():
    """Zwei Abrufe, dazwischen kein einziges Paket -- kein ZeroDivisionError."""
    tracker = LossTracker()
    tracker.update(sample(ts=0.0, from_client_good=500, from_client_lost=5))
    result = tracker.update(sample(ts=5.0, from_client_good=500, from_client_lost=5))
    assert result.has_rate is True
    assert result.packets_from_client == 0
    assert result.loss_pct_from_client is None
    assert result.loss_pct is None


def test_rueckwaerts_laufende_zaehler_setzen_zurueck():
    """Negative Differenzen sind sinnlos; ein Intervall wird geopfert."""
    tracker = LossTracker()
    tracker.update(sample(ts=0.0, from_client_good=1000, from_client_lost=10))
    result = tracker.update(sample(ts=5.0, from_client_good=20, from_client_lost=0))
    assert result.has_rate is False
    assert result.restarted is True
    assert result.loss_pct is None

    # Danach zaehlt die neue Verbindung normal weiter.
    weiter = tracker.update(sample(ts=10.0, from_client_good=100, from_client_lost=10))
    assert weiter.has_rate is True
    assert weiter.from_client_good == 80
    assert weiter.from_client_lost == 10
    # 10 verloren von 90 zugestellten plus verlorenen Paketen. Der Nenner ist
    # good + late + lost -- dieselbe Formel, die Mumble im Client anzeigt
    # (src/mumble/UserInformation.cpp: lost * 100.0 / (good + late + lost)),
    # damit die Zahl im Cockpit zu der im Client passt.
    assert weiter.loss_pct_from_client == pytest.approx(11.11, abs=0.01)


def test_wiederverwendete_session_wird_an_onlinesecs_erkannt():
    """murmur vergibt Session-IDs erneut. Die Zaehler duerfen nicht mitwandern.

    Der Extremfall: die neue Verbindung hat zufaellig hoehere Zaehlerstaende
    als die alte, also faellt der Neustart an keiner einzigen Zaehlerdifferenz
    auf. Verraten wird er nur durch ``onlinesecs``, das zurueckspringt.
    """
    tracker = LossTracker()
    tracker.update(sample(ts=0.0, onlinesecs=3600, from_client_good=1000))
    result = tracker.update(sample(ts=5.0, onlinesecs=3, from_client_good=1200))
    assert result.has_rate is False
    assert result.restarted is True


def test_andere_session_hat_keinen_vorgaenger():
    tracker = LossTracker()
    tracker.update(sample(session=7, ts=0.0, from_client_good=1000))
    result = tracker.update(sample(session=8, ts=1.0, from_client_good=50))
    assert result.session == 8
    assert result.has_rate is False
    assert result.restarted is False
    assert len(tracker) == 2


def test_forget_prune_und_reset():
    tracker = LossTracker()
    for session in (1, 2, 3):
        tracker.update(sample(session=session, ts=0.0))
    assert len(tracker) == 3

    tracker.forget(2)
    assert 2 not in tracker
    assert len(tracker) == 2

    tracker.prune([1])
    assert len(tracker) == 1
    assert 1 in tracker

    tracker.reset()
    assert len(tracker) == 0
    # Nach reset() gibt es wieder keinen Vorgaenger.
    assert tracker.update(sample(session=1, ts=1.0)).has_rate is False


def test_intervall_json_ist_vollstaendig():
    data = IntervalLoss(
        session=7,
        seconds=5.0,
        from_client_good=95,
        from_client_lost=5,
        has_rate=True,
    ).to_json()
    assert data["packets_from_client"] == 100
    assert data["loss_pct_from_client"] == 5.0
    assert data["loss_pct"] == 5.0
    assert data["loss_pct_to_client"] is None


# --------------------------------------------------------------------------- #
#  Parsen einer echten UserStats-Nachricht
# --------------------------------------------------------------------------- #


@needs_pymumble
def test_userstats_wird_vollstaendig_uebernommen():
    from pymumble_py3 import mumble_pb2

    message = mumble_pb2.UserStats()
    message.session = 7
    message.tcp_ping_avg = 24.5
    message.tcp_ping_var = 30.25
    message.tcp_packets = 100
    message.udp_ping_avg = 21.5
    message.udp_ping_var = 4.0
    message.udp_packets = 900
    message.from_client.good = 1000
    message.from_client.late = 3
    message.from_client.lost = 7
    message.from_client.resync = 1
    message.from_server.good = 990
    message.from_server.lost = 10
    message.bandwidth = 48000
    message.onlinesecs = 3600
    message.idlesecs = 12
    message.opus = True
    message.address = ipaddress.IPv6Address("::ffff:10.20.30.40").packed
    message.version.version_v2 = (1 << 32) | (5 << 16) | 735
    message.certificates.append(b"nicht-wirklich-DER")

    s = UserStatsSample.from_protobuf(message, name="Kamera 1", ts=1000.0)

    assert s.ts == 1000.0
    assert s.session == 7
    assert s.name == "Kamera 1"
    assert (s.tcp_ping_avg_ms, s.tcp_ping_var, s.tcp_packets) == (24.5, 30.25, 100)
    assert (s.udp_ping_avg_ms, s.udp_ping_var, s.udp_packets) == (21.5, 4.0, 900)
    assert (s.from_client_good, s.from_client_late) == (1000, 3)
    assert (s.from_client_lost, s.from_client_resync) == (7, 1)
    assert (s.from_server_good, s.from_server_lost) == (990, 10)
    assert (s.bandwidth_bps, s.onlinesecs, s.idlesecs) == (48000, 3600, 12)
    assert s.opus is True
    assert s.certificate_count == 1
    assert s.cert_hash == sha1_cert_hash(b"nicht-wirklich-DER")
    # IPv4-mapped IPv6 wird als IPv4 gezeigt, sonst ist die Netzsicht unlesbar.
    assert s.address == "10.20.30.40"
    assert s.version == "1.5.735"
    assert s.jitter_ms == 2.0
    assert s.loss_pct_to_client == 1.0


@needs_pymumble
def test_leere_userstats_ergibt_lauter_nullwerte():
    """Ohne ``Ban`` am Wurzelkanal laesst murmur die meisten Felder weg."""
    from pymumble_py3 import mumble_pb2

    message = mumble_pb2.UserStats()
    message.session = 3
    message.tcp_ping_avg = 12.0
    message.onlinesecs = 60

    s = UserStatsSample.from_protobuf(message, ts=1.0)
    assert s.session == 3
    assert s.name == ""
    assert s.certificate_count == 0
    assert s.cert_hash == ""
    assert s.address == ""
    assert s.version == ""
    assert s.loss_pct is None
    assert s.ping_ms == 12.0


@needs_pymumble
def test_lossracker_ueber_echte_protobuf_nachrichten():
    """Der ganze Weg: zwei Serverantworten, ein brauchbarer Prozentwert."""
    from pymumble_py3 import mumble_pb2

    def antwort(good: int, lost: int) -> object:
        message = mumble_pb2.UserStats()
        message.session = 42
        message.from_client.good = good
        message.from_client.lost = lost
        return message

    tracker = LossTracker()
    tracker.update(UserStatsSample.from_protobuf(antwort(10_000, 10), ts=0.0))
    result = tracker.update(UserStatsSample.from_protobuf(antwort(10_090, 20), ts=5.0))
    assert result.loss_pct_from_client == 10.0


# --------------------------------------------------------------------------- #
#  Backoff
# --------------------------------------------------------------------------- #


def test_backoff_verdoppelt_und_deckelt():
    ohne_zufall = {"rng": lambda: 0.0}
    werte = [bot_module.backoff_delay(n, **ohne_zufall) for n in range(1, 9)]
    assert werte == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_backoff_waechst_nie_und_bleibt_unter_dem_deckel():
    voriger = 0.0
    for n in range(1, 40):
        wert = bot_module.backoff_delay(n, rng=lambda: 0.0)
        assert wert >= voriger
        assert wert <= 60.0
        voriger = wert


def test_backoff_jitter_liegt_im_erwarteten_band():
    """Der Zufall schlaegt nur oben drauf, er verkuerzt nie."""
    assert bot_module.backoff_delay(3, rng=lambda: 0.0) == 4.0
    assert bot_module.backoff_delay(3, rng=lambda: 1.0) == 5.0
    assert bot_module.backoff_delay(3, rng=lambda: 0.5) == 4.5


def test_backoff_haelt_auch_unsinnige_versuchszaehler_aus():
    assert bot_module.backoff_delay(0, rng=lambda: 0.0) == 1.0
    assert bot_module.backoff_delay(-5, rng=lambda: 0.0) == 1.0
    # Ohne Deckel auf dem Exponenten waere 2**1000 eine Ganzzahl mit 302 Stellen.
    assert bot_module.backoff_delay(1000, rng=lambda: 0.0) == 60.0


def test_backoff_respektiert_eigene_grenzen():
    assert bot_module.backoff_delay(1, base=0.5, cap=4.0, jitter=0.0, rng=lambda: 1.0) == 0.5
    assert bot_module.backoff_delay(9, base=0.5, cap=4.0, jitter=0.0, rng=lambda: 1.0) == 4.0


# --------------------------------------------------------------------------- #
#  Kanalpfad
# --------------------------------------------------------------------------- #

#: Kanalbaum wie ihn pymumble haelt: Wurzel ohne ``parent``.
BAUM = {
    0: {"channel_id": 0, "name": "Root"},
    1: {"channel_id": 1, "name": "Intercom", "parent": 0},
    2: {"channel_id": 2, "name": "Regie", "parent": 1},
    3: {"channel_id": 3, "name": "Technik", "parent": 2},
    4: {"channel_id": 4, "name": "Presse", "parent": 0},
    5: {"channel_id": 5, "name": "Technik", "parent": 4},
}


def test_pfad_zerlegen():
    assert bot_module.split_channel_path("Intercom/Regie") == ("Intercom", "Regie")
    assert bot_module.split_channel_path("/Intercom//Regie/") == ("Intercom", "Regie")
    assert bot_module.split_channel_path("") == ()
    assert bot_module.split_channel_path("   ") == ()


def test_pfad_wird_als_pfad_aufgeloest_nicht_als_name():
    """Zwei Kanaele heissen ``Technik``. Nur der Pfad entscheidet."""
    assert bot_module.resolve_channel_path(BAUM, "Intercom/Regie/Technik") == 3
    assert bot_module.resolve_channel_path(BAUM, "Presse/Technik") == 5


def test_leerer_pfad_ist_der_wurzelkanal():
    assert bot_module.resolve_channel_path(BAUM, "") == 0
    assert bot_module.resolve_channel_path(BAUM, "/") == 0


def test_unbekannter_pfad_ergibt_none():
    assert bot_module.resolve_channel_path(BAUM, "Intercom/Gibtsnicht") is None
    assert bot_module.resolve_channel_path(BAUM, "Gibtsnicht") is None
    # Ein Kanal, den es gibt -- aber nicht an dieser Stelle im Baum.
    assert bot_module.resolve_channel_path(BAUM, "Regie") is None


def test_schreibweise_wird_zweitrangig_nachgesehen():
    assert bot_module.resolve_channel_path(BAUM, "intercom/REGIE") == 2


def test_exakte_schreibweise_hat_vorrang():
    baum = {
        0: {"channel_id": 0, "name": "Root"},
        1: {"channel_id": 1, "name": "regie", "parent": 0},
        2: {"channel_id": 2, "name": "Regie", "parent": 0},
    }
    assert bot_module.resolve_channel_path(baum, "Regie") == 2
    assert bot_module.resolve_channel_path(baum, "regie") == 1


# --------------------------------------------------------------------------- #
#  Zertifikat
# --------------------------------------------------------------------------- #


@needs_cryptography
def test_zertifikat_wird_erzeugt_und_bleibt_dann_gleich(tmp_path: Path):
    """Ein zweiter Aufruf darf den Hash nicht aendern -- daran haengt die Registrierung."""
    pfad = tmp_path / "unterverzeichnis" / "monitor-cert.pem"
    erster = bot_module.ensure_certificate(pfad, "monitor")
    assert pfad.exists()
    assert len(erster) == 40
    assert bot_module.ensure_certificate(pfad, "monitor") == erster


@needs_cryptography
def test_zertifikat_ist_nur_fuer_den_eigentuemer_lesbar(tmp_path: Path):
    """Die Datei enthaelt den privaten Schluessel."""
    pfad = tmp_path / "monitor-cert.pem"
    bot_module.ensure_certificate(pfad, "monitor")
    assert pfad.stat().st_mode & 0o077 == 0


@needs_cryptography
def test_zertifikat_ist_fuer_ssl_benutzbar(tmp_path: Path):
    """Genau so laedt pymumble die Datei: certfile und keyfile derselbe Pfad."""
    pfad = tmp_path / "monitor-cert.pem"
    bot_module.ensure_certificate(pfad, "monitor")
    context = ssl.create_default_context()
    context.load_cert_chain(str(pfad), str(pfad))


@needs_cryptography
def test_fingerabdruck_ist_der_sha1_des_der_blattzertifikats(tmp_path: Path):
    """Derselbe Wert, den murmur in ``UserHash`` fuehrt."""
    pfad = tmp_path / "monitor-cert.pem"
    erwartet = bot_module.ensure_certificate(pfad, "monitor")
    block = pfad.read_text().split("-----END CERTIFICATE-----")[0]
    der = ssl.PEM_cert_to_DER_cert(block + "-----END CERTIFICATE-----\n")
    assert erwartet == sha1_cert_hash(der)


def test_datei_ohne_zertifikat_wird_klar_abgelehnt(tmp_path: Path):
    pfad = tmp_path / "kaputt.pem"
    pfad.write_text("nur Text, kein PEM\n")
    with pytest.raises(ValueError):
        bot_module.certificate_fingerprint(pfad)


# --------------------------------------------------------------------------- #
#  Die pymumble-Erweiterung
# --------------------------------------------------------------------------- #


class _SocketErsatz:
    """Nimmt Bytes entgegen, statt sie zu senden."""

    def __init__(self) -> None:
        self.data = bytearray()

    def send(self, packet: bytes) -> int:
        self.data.extend(packet)
        return len(packet)

    def close(self) -> None:
        return None


def _client():
    """Ein ``_MonitorMumble`` ohne Verbindung.

    ``Mumble.__init__`` fasst das Netz nicht an -- das passiert erst in
    ``start()``. Damit laesst sich die Rahmenerzeugung ohne Server pruefen.
    """
    from pymumble_py3 import commands

    client = bot_module._MonitorMumble(host="127.0.0.1", user="monitor", port=64999)
    client.control_socket = _SocketErsatz()
    client.commands = commands.Commands()
    return client


@needs_pymumble
def test_userstats_anfrage_hat_den_richtigen_rahmen():
    """Typ 22, Laenge als big-endian ``!HL``, danach das Protobuf."""
    from pymumble_py3 import mumble_pb2

    client = _client()
    cmd = bot_module._UserStatsCmd(7)
    client.commands.new_cmd(cmd)
    client.treat_command(cmd)

    rohdaten = bytes(client.control_socket.data)
    typ, laenge = struct.unpack("!HL", rohdaten[:6])
    assert typ == 22
    nutzlast = rohdaten[6:]
    assert len(nutzlast) == laenge

    angefragt = mumble_pb2.UserStats()
    angefragt.ParseFromString(nutzlast)
    assert angefragt.session == 7
    assert angefragt.stats_only is False


@needs_pymumble
def test_kommando_wird_beantwortet_sonst_haengt_der_aufrufer():
    """``commands.answer`` ist Pflicht: sonst wartet ``execute_command`` ewig."""
    client = _client()
    cmd = bot_module._UserStatsCmd(7)
    sperre = client.commands.new_cmd(cmd)
    assert sperre.locked() is True

    client.treat_command(cmd)
    assert cmd.response is True
    assert sperre.locked() is False


@needs_pymumble
def test_eingehende_userstats_landen_im_rueckruf():
    from pymumble_py3 import mumble_pb2

    client = _client()
    empfangen: list[object] = []
    client.callbacks.add_callback(bot_module._CLBK_USERSTATS, empfangen.append)

    nachricht = mumble_pb2.UserStats()
    nachricht.session = 11
    nachricht.from_client.lost = 4
    client.dispatch_control_message(22, nachricht.SerializeToString())

    assert len(empfangen) == 1
    assert empfangen[0].session == 11
    assert empfangen[0].from_client.lost == 4


@needs_pymumble
def test_andere_nachrichten_gehen_weiter_an_pymumble():
    """Unser Zweig darf den Rest der Protokollbehandlung nicht verschlucken."""
    from pymumble_py3 import mumble_pb2

    client = _client()
    version = mumble_pb2.Version()
    version.release = "1.5.735"
    # Wuerde unser Zweig alles abfangen, kaeme hier eine Ausnahme oder nichts.
    client.dispatch_control_message(0, version.SerializeToString())


@needs_pymumble
def test_audio_kann_strukturell_nicht_gesendet_werden():
    """Jeder Audioframe waere ein UDPTunnel. Der fliegt raus."""
    from pymumble_py3 import mumble_pb2

    client = _client()
    with pytest.raises(RuntimeError, match="kein Audio"):
        client.send_message(1, mumble_pb2.UserStats())
    assert bytes(client.control_socket.data) == b""


@needs_pymumble
def test_ohne_empfangston_entsteht_kein_soundoutput():
    """Ohne ``sound_output`` gibt es kein Objekt, das senden koennte."""
    client = _client()
    client.set_receive_sound(False)
    client.init_connection()
    assert client.sound_output is None


@needs_pymumble
def test_pymumbles_streamhandler_wird_entfernt():
    """Sonst sammelt sich pro Verbindungsversuch ein weiterer Handler an."""
    client = _client()
    assert client.Log.handlers == []


# --------------------------------------------------------------------------- #
#  MonitorBot
# --------------------------------------------------------------------------- #


def test_abgeschalteter_bot_startet_nicht(settings, monkeypatch):
    from intercom.config import Settings

    monkeypatch.setenv("MONITOR_BOT_ENABLED", "false")
    aus = Settings.load()

    bot = bot_module.MonitorBot(aus)
    assert bot.state == bot_module.STATE_STOPPED
    bot.start()
    assert bot.state == bot_module.STATE_DISABLED
    assert bot.connected is False
    assert bot.own_ping_ms == 0.0
    bot.stop()
    # stop() darf einen abgeschalteten Bot nicht auf "gestoppt" umschreiben --
    # "aus" ist die genauere Aussage.
    assert bot.state == bot_module.STATE_DISABLED


def test_stop_ohne_start_ist_harmlos(settings):
    bot = bot_module.MonitorBot(settings)
    bot.stop()
    assert bot.state == bot_module.STATE_STOPPED
    assert bot.connected is False
    assert bot.last_error == ""


@needs_pymumble
@needs_cryptography
def test_gescheiterte_verbindung_reisst_niemanden_mit(settings, monkeypatch):
    """Kein Server auf dem Port. Der Bot muss das melden und weiterleben.

    pymumble wirft seinen ``ConnectionRejectedError`` im eigenen Thread; der
    voreingestellte ``threading.excepthook`` wuerde einen Stacktrace in die
    Testausgabe drucken, der nach einem Fehler aussieht, aber genau das
    erwartete Verhalten ist. Deshalb wird er hier stillgelegt.
    """
    monkeypatch.setattr(threading, "excepthook", lambda args: None)

    zustaende: list[str] = []
    bot = bot_module.MonitorBot(settings, on_state=zustaende.append)
    bot.start()
    try:
        frist = threading.Event()
        # Warten, bis der Bot einmal durch ist: verbinden, scheitern, warten.
        for _ in range(100):
            if bot_module.STATE_WAITING in zustaende:
                break
            frist.wait(0.1)
    finally:
        bot.stop()

    assert bot_module.STATE_CONNECTING in zustaende
    assert bot_module.STATE_FAILED in zustaende
    assert bot.connected is False
    assert "64999" in bot.last_error or "fehlgeschlagen" in bot.last_error
    # Das Zertifikat ist trotzdem entstanden -- der Betreiber kann den Bot
    # registrieren, bevor er das erste Mal erfolgreich verbindet.
    assert settings.monitor_cert.exists()
    assert len(bot.cert_hash) == 40


def test_zustandsrueckruf_darf_ausnahmen_werfen(settings):
    """Ein kaputter Melder darf den Bot nicht anhalten."""

    def kaputt(state: str) -> None:
        raise RuntimeError("absichtlich")

    bot = bot_module.MonitorBot(settings, on_state=kaputt)
    bot._set_state(bot_module.STATE_CONNECTING)
    assert bot.state == bot_module.STATE_CONNECTING


# --------------------------------------------------------------------------- #
#  Verbindungsschleife und UserStats-Sparsamkeit
# --------------------------------------------------------------------------- #


def test_backoff_faellt_erst_wenn_die_verbindung_getragen_hat(settings, monkeypatch):
    """Anmeldung gelingt, murmur wirft sofort wieder raus -- der haeufigste Fall.

    Name schon vergeben, Zertifikat abgelehnt, Ban: die Anmeldung *klappt*, die
    Verbindung endet Sekundenbruchteile spaeter. Wurde der Zaehler direkt nach
    dem Verbinden genullt, wartete der naechste Versuch wieder die Grundzeit --
    der Bot haemmert im Sekundentakt gegen den Server.
    """
    bot = bot_module.MonitorBot(settings)
    gefragt: list[int] = []

    def fake_backoff(attempt, **kwargs):
        gefragt.append(attempt)
        return 0.0

    monkeypatch.setattr(bot_module, "backoff_delay", fake_backoff)

    runden = 0

    def fake_connect():
        nonlocal runden
        runden += 1
        if runden >= 4:
            bot._stop.set()
        return object()

    monkeypatch.setattr(bot, "_connect", fake_connect)
    monkeypatch.setattr(bot, "_prepare", lambda mumble: None)
    monkeypatch.setattr(bot, "_poll_loop", lambda mumble: None)
    monkeypatch.setattr(bot, "_close_client", lambda mumble: None)

    bot._run()

    assert gefragt == [1, 2, 3], "der Zaehler wurde zu frueh zurueckgesetzt"


def test_backoff_faellt_nach_einer_stabilen_verbindung(settings, monkeypatch):
    """Gegenprobe: hat die Verbindung getragen, faengt das Warten wieder klein an."""
    bot = bot_module.MonitorBot(settings)
    gefragt: list[int] = []

    def fake_backoff(attempt, **kwargs):
        gefragt.append(attempt)
        return 0.0

    monkeypatch.setattr(bot_module, "backoff_delay", fake_backoff)

    # Die Uhr laeuft schneller als der Test: jede Runde altert um 100 s,
    # deutlich mehr als _STABIL_S.
    uhr = itertools.count(0.0, 100.0)
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: next(uhr))

    runden = 0

    def fake_connect():
        nonlocal runden
        runden += 1
        if runden >= 3:
            bot._stop.set()
        return object()

    monkeypatch.setattr(bot, "_connect", fake_connect)
    monkeypatch.setattr(bot, "_prepare", lambda mumble: None)
    monkeypatch.setattr(bot, "_poll_loop", lambda mumble: None)
    monkeypatch.setattr(bot, "_close_client", lambda mumble: None)

    bot._run()

    # Zweite Runde wartet wieder die Grundzeit statt das Doppelte.
    assert gefragt == [1, 1]


class _KanalErsatz(dict):
    """Kanal, wie pymumble ihn haelt -- plus die Methoden, die wir nicht wollen."""

    def move_in(self, session=None):
        raise AssertionError("move_in() blockiert ohne Zeitlimit")


class _IchErsatz(dict):
    def get_property(self, key):
        return self.get(key)

    def mute(self):
        raise AssertionError("mute() blockiert ohne Zeitlimit")

    def deafen(self):
        raise AssertionError("deafen() blockiert ohne Zeitlimit")


class _UserErsatz(dict):
    myself = None


class _MumbleErsatz:
    def __init__(self, channel_id=0):
        self.users = _UserErsatz()
        self.users.myself = _IchErsatz(session=42, channel_id=channel_id)
        self.channels = {
            0: _KanalErsatz({"channel_id": 0, "name": "Root"}),
            1: _KanalErsatz({"channel_id": 1, "name": "Intercom", "parent": 0}),
            2: _KanalErsatz({"channel_id": 2, "name": "Regie", "parent": 1}),
        }
        self.kommandos = []

    def execute_command(self, cmd, blocking=True):
        self.kommandos.append((cmd, blocking))
        return None


@needs_pymumble
def test_prepare_wartet_auf_nichts(settings):
    """``execute_command(blocking=True)`` wartet ohne Zeitlimit auf den pymumble-Thread.

    Stirbt der, haengt der Aufseher-Thread fuer immer -- und mit ihm ``stop()``
    und das Herunterfahren des Containers. ``_prepare`` laeuft direkt nach dem
    Verbindungsaufbau, also genau dann, wenn der Thread am ehesten stirbt.
    """
    bot = bot_module.MonitorBot(settings)
    mumble = _MumbleErsatz(channel_id=0)

    bot._prepare(mumble)

    assert [blocking for _, blocking in mumble.kommandos] == [False, False]
    stumm, umzug = (cmd for cmd, _ in mumble.kommandos)
    assert stumm.parameters == {"session": 42, "self_mute": True, "self_deaf": True}
    assert umzug.parameters == {"session": 42, "channel_id": 2}


@needs_pymumble
def test_prepare_zieht_nicht_um_wenn_der_bot_schon_richtig_steht(settings):
    bot = bot_module.MonitorBot(settings)
    mumble = _MumbleErsatz(channel_id=2)

    bot._prepare(mumble)

    assert len(mumble.kommandos) == 1


def _probe(session=5, **felder):
    from intercom.monitor.stats import UserStatsSample

    grund = {
        "ts": 1000.0,
        "session": session,
        "certificate_count": 0,
        "cert_hash": "",
        "address": "",
        "version": "",
        "opus": False,
    }
    grund.update(felder)
    return UserStatsSample(**grund)


def test_unveraenderliche_felder_werden_nur_einmal_geholt(settings):
    """Die Zertifikatskette bei jeder Abfrage mitzuschicken ist reine Last."""
    bot = bot_module.MonitorBot(settings)

    voll = bot._merge_merkmale(
        _probe(certificate_count=2, cert_hash="ab12", address="10.20.10.5", version="1.5.735")
    )
    assert voll.cert_hash == "ab12"
    assert 5 in bot._merkmale

    # Ab jetzt fragt der Bot nur noch die Zahlen ab ...
    mumble = _MumbleErsatz()
    bot._request_stats(mumble, 5)
    cmd, blocking = mumble.kommandos[0]
    assert blocking is False
    assert cmd.parameters["stats_only"] is True

    # ... und die Antwort ohne Zertifikat wird wieder vervollstaendigt.
    knapp = bot._merge_merkmale(_probe(tcp_ping_avg_ms=12.0))
    assert knapp.cert_hash == "ab12"
    assert knapp.certificate_count == 2
    assert knapp.address == "10.20.10.5"
    assert knapp.version == "1.5.735"
    assert knapp.tcp_ping_avg_ms == 12.0


def test_ohne_ban_recht_wird_weiter_voll_gefragt(settings):
    """Fehlt dem Bot ``Ban`` am Wurzelkanal, liefert murmur die Felder nie.

    Dann darf sich der Bot nicht auf eine leere Antwort einschwoeren -- sonst
    bekaeme er die Daten auch nach dem Nachruesten des Rechts nie zu sehen.
    """
    bot = bot_module.MonitorBot(settings)

    bot._merge_merkmale(_probe(tcp_ping_avg_ms=9.0))
    assert bot._merkmale == {}

    mumble = _MumbleErsatz()
    bot._request_stats(mumble, 5)
    assert mumble.kommandos[0][0].parameters["stats_only"] is False


def test_ohne_vorgegebenen_platz_bleibt_der_bot_wo_er_ist(monkeypatch):
    """Die Vorgabe zeigte auf „Intercom/Regie“ – einen Platz aus der alten YAML.

    Nach einem Baukasten gibt es den nicht, und der Bot meldete bei jedem Start
    einen Fehler für etwas, das gar nicht eingestellt war. Leer heißt jetzt:
    bleib oben und miss von dort.
    """
    from intercom.config import Settings

    monkeypatch.delenv("MONITOR_BOT_CHANNEL", raising=False)
    for pflicht, wert in (
        ("ICE_SECRET", "x" * 16),
        ("ADMIN_PASSWORD", "y" * 12),
        ("SESSION_SECRET", "z" * 32),
    ):
        monkeypatch.setenv(pflicht, wert)
    einstellungen = Settings.load()
    assert einstellungen.monitor_channel == ""
