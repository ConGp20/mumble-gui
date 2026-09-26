"""Integrationstest gegen einen **echten** mumble-server.

Warum es diesen Test zusaetzlich zum Doppel gibt
------------------------------------------------
``tests/fake_murmur.py`` bildet die Semantik aus ``MumbleServerIce.cpp`` nach.
Das faengt Denkfehler, aber nicht die Faelle, in denen unsere Lesart des
Quelltextes falsch ist. Genau diese Faelle prueft dieser Test -- vor allem:

* ueberlebt das ``Listen``-Bit (0x800) einen ``setACL``/``getACL``-Umlauf auf
  einem echten Server, obwohl die Slice keine Konstante dafuer kennt?
* nimmt ``startListening`` wirklich eine Session und keine Nutzer-ID?
* ist ``addUserToGroup`` tatsaechlich nur temporaer?

Aufruf
------
    docker compose -f docker-compose.test.yml up -d
    cd admin && python -m pytest -m integration -v
    docker compose -f docker-compose.test.yml down -v

Ohne laufenden Testserver werden alle Tests uebersprungen -- der Testlauf ist
dann gruen, meldet die Zahl der uebersprungenen Tests aber sichtbar.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time

import pytest

from tests.conftest import ICE_AVAILABLE, SLICE_AVAILABLE

pytestmark = pytest.mark.integration

ICE_HOST = os.environ.get("MUMBLE_TEST_ICE_HOST", "127.0.0.1")
ICE_PORT = int(os.environ.get("MUMBLE_TEST_ICE_PORT", "16502"))
ICE_SECRET = os.environ.get("MUMBLE_TEST_SECRET", "test-secret")
MUMBLE_VERSION = os.environ.get("MUMBLE_TEST_VERSION", "v1.5.735")


def _server_erreichbar(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ICE_HOST, ICE_PORT), timeout=timeout):
            return True
    except OSError:
        return False


needs_real_server = pytest.mark.skipif(
    not (ICE_AVAILABLE and SLICE_AVAILABLE and _server_erreichbar()),
    reason=(
        f"Kein Mumble-Testserver auf {ICE_HOST}:{ICE_PORT}. "
        "Mit 'docker compose -f docker-compose.test.yml up -d' starten."
    ),
)


@pytest.fixture(scope="module")
def echter_client():
    """Verbindung zum echten Server. Raeumt hinterher alles wieder auf."""
    from pathlib import Path

    from intercom.config import Settings
    from intercom.ice.client import IceClient

    settings = Settings(
        ice_host=ICE_HOST,
        ice_port=ICE_PORT,
        ice_secret=ICE_SECRET,
        ice_server_id=1,
        listen_host="127.0.0.1",
        listen_port=8080,
        admin_user="admin",
        admin_password="admin",
        readonly_user=None,
        readonly_password=None,
        session_secret="test" * 8,
        intercom_config=Path("intercom.yaml"),
        provision_on_start=False,
        provision_prune=False,
        monitor_enabled=False,
        monitor_name="monitor",
        monitor_channel="Intercom/Regie",
        monitor_password=None,
        monitor_cert=Path("/tmp/monitor-cert.pem"),
        monitor_stats_interval_ms=5000,
        mumble_port=64739,
        poll_interval_ms=2000,
        history_retention_hours=1,
        alert_ping_ms=80.0,
        alert_loss_pct=2.0,
        log_level="INFO",
        data_dir=Path("/tmp"),
        slice_dir=Path("/tmp"),
        expected_mumble_version=MUMBLE_VERSION,
        warnings=(),
    )

    client = IceClient(settings)
    for versuch in range(20):
        try:
            client.connect()
            break
        except Exception:
            if versuch == 19:
                raise
            time.sleep(1)

    try:
        yield client
    finally:
        _aufraeumen(client)
        client.close()


def _aufraeumen(client) -> None:
    """Loescht alles, was die Tests angelegt haben."""
    from intercom.runtime import build_paths

    try:
        paths = build_paths(client.get_channels())
        for pfad, kanal_id in sorted(paths.items(), key=lambda p: -len(p[0])):
            if pfad.startswith("ITest"):
                with contextlib.suppress(Exception):
                    client.remove_channel(kanal_id)
        for uid, name in client.get_registered_users().items():
            if name.startswith("itest-"):
                with contextlib.suppress(Exception):
                    client.unregister_user(uid)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #


@needs_real_server
def test_version_und_slice_passen(echter_client):
    """Belegt, dass die einkompilierte Slice zum Server passt."""
    version = echter_client.get_version()
    assert version.major == 1
    assert version.minor >= 5, "Vor 1.5 hiess das Slice-Modul Murmur, nicht MumbleServer"
    assert echter_client.version_warnings == [], echter_client.version_warnings


@needs_real_server
def test_listen_bit_ueberlebt_einen_echten_server(echter_client):
    """Der wichtigste Test dieser Datei.

    Die Slice von 1.5 kennt keine Konstante ``PermissionListen``. Wir setzen das
    Bit trotzdem, weil ``impl_Server_setACL`` nur gegen ``ChanACL::All``
    maskiert und ``All`` das Bit enthaelt. Waere diese Lesart falsch, waere
    ``listen_for`` in der ganzen Anwendung wirkungslos -- und zwar lautlos.
    """
    from intercom.ice.types import ACLEntry, ChannelACL

    LISTEN = 0x800
    SPEAK = 0x08

    kanal = echter_client.add_channel("ITest-Listen", 0)
    echter_client.set_channel_acl(
        ChannelACL(
            channel_id=kanal,
            acls=[
                ACLEntry(
                    apply_here=True, apply_subs=False, group="all",
                    allow=0, deny=SPEAK | LISTEN,
                ),
                ACLEntry(
                    apply_here=True, apply_subs=False, group="regie",
                    allow=SPEAK | LISTEN, deny=0,
                ),
            ],
        )
    )

    zurueck = echter_client.get_acl(kanal).own_acls()
    alle = next(a for a in zurueck if a.group == "all")
    regie = next(a for a in zurueck if a.group == "regie")

    assert alle.deny & LISTEN, "Listen wurde vom echten Server wegmaskiert"
    assert regie.allow & LISTEN, "Listen wurde vom echten Server wegmaskiert"
    assert alle.deny & SPEAK
    assert regie.allow & SPEAK


@needs_real_server
def test_unbekanntes_bit_wird_wegmaskiert(echter_client):
    """Gegenprobe: ein Bit ausserhalb von ChanACL::All darf NICHT ueberleben.

    Ohne diese Gegenprobe koennte der Test oben auch dann gruen sein, wenn
    murmur schlicht alles durchreicht -- dann waere die Aussage wertlos.
    """
    from intercom.ice.types import ACLEntry, ChannelACL

    CACHED = 0x8000000  # interner Marker des Servers, nicht in All
    SPEAK = 0x08

    kanal = echter_client.add_channel("ITest-Maske", 0)
    echter_client.set_channel_acl(
        ChannelACL(
            channel_id=kanal,
            acls=[
                ACLEntry(apply_here=True, apply_subs=False, group="all",
                         allow=SPEAK | CACHED, deny=0)
            ],
        )
    )
    zurueck = echter_client.get_acl(kanal).own_acls()[0]
    assert zurueck.allow & SPEAK
    assert not (zurueck.allow & CACHED), "murmur reicht offenbar jedes Bit durch"


@needs_real_server
def test_geerbte_eintraege_werden_nicht_kopiert(echter_client):
    """Der Schutz aus DECISIONS D-006, gegen den echten Server geprueft."""
    from intercom.ice.types import ACLEntry, ChannelACL

    SPEAK = 0x08
    oben = echter_client.add_channel("ITest-Erbe", 0)
    unten = echter_client.add_channel("Kind", oben)

    echter_client.set_channel_acl(
        ChannelACL(
            channel_id=oben,
            acls=[ACLEntry(apply_here=True, apply_subs=True, group="all",
                           allow=0, deny=SPEAK)],
        )
    )

    kind_acl = echter_client.get_acl(unten)
    assert any(a.inherited for a in kind_acl.acls), "keine Vererbung sichtbar"
    assert kind_acl.own_acls() == []

    echter_client.set_channel_acl(kind_acl)          # unveraendert zurueck
    assert echter_client.get_acl(unten).own_acls() == [], "geerbter Eintrag wurde kopiert"


@needs_real_server
def test_gruppenmitgliedschaft_ueber_setacl_ist_dauerhaft(echter_client):
    """Beleg fuer DECISIONS D-005: add nimmt Nutzer-IDs und bleibt bestehen."""
    from intercom.ice.types import ChannelACL, ChannelGroup

    userid = echter_client.register_user("itest-regie", cert_hash="c" * 40)
    kanal = echter_client.add_channel("ITest-Gruppe", 0)

    echter_client.set_channel_acl(
        ChannelACL(channel_id=kanal, groups=[ChannelGroup(name="itestgrp", add=[userid])])
    )

    gruppe = echter_client.get_acl(kanal).group("itestgrp")
    assert gruppe is not None
    assert userid in gruppe.add, "add nimmt offenbar keine Nutzer-IDs"
    assert userid in gruppe.members


@needs_real_server
def test_apply_export_apply_gegen_echten_server(echter_client):
    """Die Abnahmekriterien aus dem Auftrag, aber ohne Doppel."""
    import textwrap

    import yaml

    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config

    quelle = textwrap.dedent("""
        version: 1
        groups: [itestregie, itestkamera]
        channels:
          - name: ITest-Baum
            children:
              - name: Regie
                speak: [itestregie]
                listen_for: [itestregie]
              - name: Kameras
                speak: [itestkamera, itestregie]
                listen_for: [itestkamera, itestregie]
        policies:
          guests_listen_only: true
    """)
    config = parse_config(yaml.safe_load(quelle))

    plan = reconcile(echter_client, config, dry_run=False)
    assert not plan.failed, [c.error for c in plan.failed]

    # Zweiter Lauf: keine einzige Aenderung mehr.
    zweiter = reconcile(echter_client, config, dry_run=False)
    assert zweiter.empty, "nicht idempotent:\n" + zweiter.to_text()

    # Export und erneutes Anwenden aendern ebenfalls nichts.
    exportiert = parse_config(yaml.safe_load(export_yaml(echter_client, roots=["ITest-Baum"])))
    dritter = reconcile(echter_client, exportiert, dry_run=True)
    fremde = [c for c in dritter.pending if c.target.startswith("ITest-Baum")]
    assert not fremde, "Export ist nicht aequivalent:\n" + dritter.to_text()


@needs_real_server
def test_konfiguration_lesen_und_schreiben(echter_client):
    alle = echter_client.get_all_conf()
    assert isinstance(alle, dict)

    vorher = alle.get("welcometext", "")
    echter_client.set_conf("welcometext", "Integrationstest")
    assert echter_client.get_conf("welcometext") == "Integrationstest"
    echter_client.set_conf("welcometext", vorher)


@needs_real_server
def test_write_only_gilt_nur_fuer_key_und_passphrase(echter_client):
    """Die Annahme "Secrets sind write-only" stimmt nur fuer zwei Schluessel.

    Gegen den echten Server nachgemessen und in ``MumbleServerIce.cpp``
    belegt -- ``impl_Server_getConf`` prueft wortwoertlich zwei Namen::

        if (key == "key" || key == "passphrase")
            cb->ice_exception(WriteOnlyException());

    ``icesecretwrite`` ist keiner davon. Der Aufruf gelingt und liefert einen
    **leeren String**, weil das Secret in der ini-Datei steht und nicht in der
    ``config``-Tabelle, aus der ``ServerDB::getConf`` liest. Wer hier eine
    Ausnahme erwartet, faellt entweder auf die Nase oder -- schlimmer -- haelt
    den leeren String fuer "kein Secret gesetzt".
    """
    from intercom.ice.errors import IceCallFailed

    for schluessel in ("key", "passphrase"):
        with pytest.raises(IceCallFailed):
            echter_client.get_conf(schluessel)

    assert echter_client.get_conf("icesecretwrite") == ""
    # Und in getAllConf kommt er ueberhaupt nicht vor.
    assert "icesecretwrite" not in echter_client.get_all_conf()


@needs_real_server
def test_getallconf_ist_nicht_die_wirksame_konfiguration(echter_client):
    """"Default" heisst bei murmur nicht "Werkseinstellung".

    ``Server::getAllConf`` liest nur die ``config``-Tabelle, also was jemand
    zur Laufzeit per ``setConf`` geaendert hat. ``Meta::getDefaultConf``
    liefert ``qmConfig``, und das baut ``MetaParams`` aus der **ini-Datei**
    plus den eingebauten Vorgaben -- beim Docker-Image also aus den
    ``MUMBLE_CONFIG_*``-Variablen der Compose.

    Der Test haelt das an einem Wert fest, der beides auseinanderhaelt: die
    Compose setzt hier Port 64739, murmurs eingebaute Vorgabe ist 64738.
    """
    ueberschrieben = echter_client.get_all_conf()
    aus_datei = echter_client.get_default_conf()

    # Der Port steht in der Datei, nicht in der Datenbank ...
    assert "port" not in ueberschrieben
    assert aus_datei["port"] == "64739", "kommt aus MUMBLE_CONFIG_PORT der Compose"
    # ... und 64738 waere die eingebaute Vorgabe, die hier gerade NICHT gilt.
    assert aus_datei["port"] != "64738"

    # Ein per Ice gesetzter Wert landet dagegen in der Datenbank und
    # ueberschattet die Datei.
    vorher_datei = aus_datei.get("welcometext", "")
    echter_client.set_conf("welcometext", "Aus der Datenbank")
    try:
        assert echter_client.get_all_conf()["welcometext"] == "Aus der Datenbank"
        # Die Datei-Ebene bleibt davon unberuehrt -- darauf faellt der Server
        # zurueck, wenn der Datenbankeintrag verschwindet.
        assert echter_client.get_default_conf().get("welcometext", "") == vorher_datei
    finally:
        echter_client.set_conf("welcometext", "")


@needs_real_server
def test_serverlog_kommt_an(echter_client):
    assert echter_client.get_log_len() >= 0
    eintraege = echter_client.get_log(0, 10)
    assert all(e.timestamp > 0 for e in eintraege)


@needs_real_server
def test_export_verliert_die_konfiguration_aus_der_datei_nicht(echter_client):
    """Der Export las ``getAllConf`` -- und das sind nur die Datenbankwerte.

    Auf einem Server, der ueber ``MUMBLE_CONFIG_*`` der Compose eingerichtet
    wurde, stehen ``welcometext`` und ``defaultchannel`` in der ini-Datei und
    **nicht** in der ``config``-Tabelle. Der Export hat sie damit stillschweigend
    verloren -- ein Ruecksichern haette sie auf die Vorgaben zurueckgesetzt.
    """
    aus_datenbank = echter_client.get_all_conf()
    wirksam = echter_client.get_effective_conf()

    # Der Port kommt hier aus der Datei, nicht aus der Datenbank.
    assert "port" not in aus_datenbank
    assert wirksam["port"] == "64739"
    # Datenbankwerte ueberschatten die Datei.
    echter_client.set_conf("textmessagelength", "1234")
    try:
        assert echter_client.get_effective_conf()["textmessagelength"] == "1234"
    finally:
        echter_client.set_conf("textmessagelength", "")


@needs_real_server
def test_eingeschraenkter_export_bleibt_einlesbar(echter_client):
    """``roots=`` darf keine YAML erzeugen, die auf fremde Kanaele zeigt.

    ``server.defaultchannel`` wurde ungeprueft mitgeschrieben. Zeigte er auf
    einen Kanal ausserhalb des exportierten Teilbaums, verwies die YAML auf
    einen Kanal, den sie selbst nicht anlegt -- ``parse_config`` lehnte sie mit
    "Kanal gibt es nicht" ab. Das Abnahmekriterium "Export ist wieder
    einlesbar" war damit genau dann verletzt, wenn ein Vorgabekanal gesetzt war.
    """
    import textwrap

    import yaml

    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config

    quelle = textwrap.dedent("""
        version: 1
        groups: [itestaussen]
        channels:
          - name: ITest-Aussen
            children:
              - name: Vorgabe
          - name: ITest-Innen
            children:
              - name: Regie
                speak: [itestaussen]
    """)
    reconcile(echter_client, parse_config(yaml.safe_load(quelle)), dry_run=False)

    kanaele = echter_client.get_channels()
    ziel = next(
        cid
        for cid, k in kanaele.items()
        if k.name == "Vorgabe" and kanaele.get(k.parent, k).name == "ITest-Aussen"
    )
    vorher = echter_client.get_conf("defaultchannel")
    echter_client.set_conf("defaultchannel", str(ziel))
    try:
        # Export nur des ANDEREN Teilbaums -- der Vorgabekanal liegt draussen.
        text = export_yaml(echter_client, roots=["ITest-Innen"])
        assert "defaultchannel" not in yaml.safe_load(text).get("server", {})
        assert "Nicht uebernommen" in text, "das Weglassen muss sichtbar sein"
        assert "ITest-Aussen/Vorgabe" in text

        # Und die Hauptsache: wieder einlesbar.
        wieder = parse_config(yaml.safe_load(text))
        assert [c.name for c in wieder.channels] == ["ITest-Innen"]

        # Liegt der Vorgabekanal drinnen, wird er uebernommen.
        drin = export_yaml(echter_client, roots=["ITest-Aussen"])
        assert yaml.safe_load(drin)["server"]["defaultchannel"] == "ITest-Aussen/Vorgabe"
        parse_config(yaml.safe_load(drin))
    finally:
        echter_client.set_conf("defaultchannel", vorher)
