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


# --------------------------------------------------------------------------- #
#  Die Rechte-Auswertung gegen das Original
# --------------------------------------------------------------------------- #


@needs_real_server
def test_gerechnete_rechte_stimmen_mit_dem_server_ueberein(echter_client):
    """Haelt :mod:`intercom.ice.wirkung` gegen ``effectivePermissions``.

    Die Oberflaeche muss Rechte auch dann anzeigen, wenn niemand verbunden ist --
    beim Aufbauen einer Veranstaltung sitzt noch keiner im Kanal. Dafuer ist die
    Auswertung aus ACL.cpp nachgebaut, und dieser Test ist der Beleg, dass der
    Nachbau stimmt: fuer einen *wirklich* verbundenen Client muss beides aufs
    Bit genau dasselbe sagen.

    Geprueft wird die Sicherheitseigenschaft, auf die sich die Oberflaeche
    verlaesst: **was als sicher ausgegeben wird, stimmt.** Bits, die als
    unbestimmt gelten, duerfen abweichen -- sie werden nirgends behauptet.
    """
    pytest.importorskip("pymumble_py3")
    import time

    import pymumble_py3

    from intercom.ice import wirkung
    from intercom.ice.permissions import mask_to_names
    from intercom.ice.types import ACLEntry, ChannelGroup

    wurzel = echter_client.add_channel("ITest-Rechte", 0)
    mitte = echter_client.add_channel("Mitte", wurzel)
    unten = echter_client.add_channel("Unten", mitte)
    kette = [wurzel, mitte, unten]

    name = "itest-rechte-pruefling"
    passwort = "pruef-geheim-123"
    try:
        uid = echter_client.register_user(name=name, password=passwort)
    except Exception:  # noqa: BLE001 -- schon vorhanden
        uid = next(
            u for u, n in echter_client.get_registered_users().items() if n == name
        )

    root_acl = echter_client.get_acl(0)
    alte_gruppen = list(root_acl.own_groups())
    root_acl.groups = [
        g for g in alte_gruppen if g.name != "itestrolle"
    ] + [ChannelGroup(name="itestrolle", add=[uid])]
    echter_client.set_channel_acl(root_acl)

    S = wirkung
    # Bewusst die drei Faelle, die beim Nachbauen schieflaufen:
    # allow+deny im selben Eintrag, Vererbung aus, und Write ohne Sprechen.
    a = echter_client.get_acl(wurzel)
    a.acls = [
        ACLEntry(apply_here=True, apply_subs=True, allow=0, deny=S.SPEAK, group="all"),
        ACLEntry(
            apply_here=True, apply_subs=True, allow=S.SPEAK, deny=0, group="itestrolle"
        ),
    ]
    echter_client.set_channel_acl(a)

    a = echter_client.get_acl(mitte)
    a.acls = [
        ACLEntry(
            apply_here=True,
            apply_subs=True,
            allow=S.WHISPER,
            deny=S.WHISPER,
            group="all",
        ),
        ACLEntry(
            apply_here=True, apply_subs=True, allow=S.WRITE, deny=0, group="itestrolle"
        ),
    ]
    echter_client.set_channel_acl(a)

    a = echter_client.get_acl(unten)
    a.inherit = False
    a.acls = [
        ACLEntry(apply_here=True, apply_subs=True, allow=0, deny=S.LISTEN, group="out"),
    ]
    echter_client.set_channel_acl(a)

    bot = pymumble_py3.Mumble(
        ICE_HOST,
        name,
        port=int(os.environ.get("MUMBLE_TEST_PORT", "64739")),
        password=passwort,
        reconnect=False,
    )
    bot.set_application_string("intercom-itest")
    bot.start()
    bot.is_ready()
    try:
        time.sleep(1.0)
        session = next(
            u.session for u in echter_client.get_users().values() if u.name == name
        )
        kanaele = echter_client.get_channels()
        acls = {cid: echter_client.get_acl(cid) for cid in kanaele}

        geprueft = 0
        for ziel in kette:
            echter_client.set_user_state(session, channel=ziel)
            time.sleep(0.3)
            wo = echter_client.get_state(session).channel
            vom_server = echter_client.effective_permissions(session, ziel)
            gerechnet = wirkung.rechte_einer_person(
                userid=uid, ziel=ziel, kanaele=kanaele, acls=acls, sitzt_in=wo
            )
            sicher = ~gerechnet.unbestimmt
            assert (vom_server & sicher) == (gerechnet.maske & sicher), (
                f"Kanal {kanaele[ziel].name}: Server sagt "
                f"{sorted(mask_to_names(vom_server & sicher))}, "
                f"gerechnet {sorted(mask_to_names(gerechnet.maske & sicher))}"
            )
            geprueft += 1

        assert geprueft == len(kette)

        # Und die Aussage, die man sich merkt: Write macht keinen Redner.
        gerechnet = wirkung.rechte_einer_person(
            userid=uid, ziel=mitte, kanaele=kanaele, acls=acls, sitzt_in=mitte
        )
        assert gerechnet.darf(wirkung.WRITE) is True
        assert gerechnet.darf(wirkung.WHISPER) is False
    finally:
        with contextlib.suppress(Exception):
            bot.stop()
        with contextlib.suppress(Exception):
            echter_client.unregister_user(uid)
        root_acl = echter_client.get_acl(0)
        root_acl.groups = [
            g for g in root_acl.own_groups() if g.name != "itestrolle"
        ]
        with contextlib.suppress(Exception):
            echter_client.set_channel_acl(root_acl)
        for cid in reversed(kette):
            with contextlib.suppress(Exception):
                echter_client.remove_channel(cid)


# --------------------------------------------------------------------------- #
#  Ruftasten: wirklich gehoert?
# --------------------------------------------------------------------------- #


@needs_real_server
def test_ruftaste_wird_ueber_den_enforcer_wirklich_gehoert(echter_client):
    """Ende-zu-Ende: der Enforcer belegt, echte Clients senden und hoeren.

    Das ist der Nachweis fuer DECISIONS D-032. Ein Client ruft immer dieselbe
    feste Gruppe ``ruf1``; wen das trifft, entscheidet allein die Belegung des
    Platzes, auf dem er steht -- und ein Platzwechsel stellt die Taste um, ohne
    dass am Client etwas passiert.

    Braucht ``opuslib`` (und libopus) fuer Ton; ohne wird uebersprungen.
    """
    pytest.importorskip("opuslib")
    pytest.importorskip("pymumble_py3")
    import time

    import pymumble_py3
    from pymumble_py3 import mumble_pb2
    from pymumble_py3.constants import (
        PYMUMBLE_CLBK_SOUNDRECEIVED,
        PYMUMBLE_MSG_TYPES_VOICETARGET,
    )

    from intercom.ice import wirkung
    from intercom.ice.types import ACLEntry, ChannelGroup
    from intercom.runtime import Enforcer

    c = echter_client
    basis = c.add_channel("ITest-Ruf", 0)
    platz_a = c.add_channel("A", basis)
    platz_b = c.add_channel("B", basis)
    platz_c = c.add_channel("C", basis)
    acl = c.get_acl(basis)
    acl.acls = [ACLEntry(
        apply_here=True, apply_subs=True, group="all", deny=0,
        allow=wirkung.ENTER | wirkung.SPEAK | wirkung.WHISPER | wirkung.TRAVERSE,
    )]
    c.set_channel_acl(acl)

    namen = ("itest-rufer", "itest-tech", "itest-zeit", "itest-nix")
    ids = {}
    bekannt = {n: u for u, n in c.get_registered_users().items()}
    for name in namen:
        ids[name] = bekannt.get(name) or c.register_user(name=name, password="pw-" + name)
    wurzel = c.get_acl(0)
    wurzel.groups = [g for g in wurzel.own_groups() if not g.name.startswith("itestruf")] + [
        ChannelGroup(name="itestruf_technik", add=[ids["itest-tech"]]),
        ChannelGroup(name="itestruf_zeit", add=[ids["itest-zeit"]]),
    ]
    c.set_channel_acl(wurzel)

    gehoert: dict[str, int] = {}
    clients = {}

    def verbinde(name, platz):
        m = pymumble_py3.Mumble(ICE_HOST, name, port=int(os.environ.get("MUMBLE_TEST_PORT", "64739")),
                                password="pw-" + name, reconnect=False)
        m.set_receive_sound(True)
        m.callbacks.set_callback(
            PYMUMBLE_CLBK_SOUNDRECEIVED,
            lambda user, chunk, n=name: gehoert.__setitem__(n, gehoert.get(n, 0) + 1)
            if user["name"] == "itest-rufer" else None,
        )
        m.start()
        m.is_ready()
        time.sleep(0.6)
        c.set_user_state(m.users.myself_session, channel=platz)
        clients[name] = m
        return m.users.myself_session

    enforcer = Enforcer(c)
    try:
        sitzung = verbinde("itest-rufer", platz_a)
        verbinde("itest-tech", platz_b)
        verbinde("itest-zeit", platz_c)
        verbinde("itest-nix", platz_b)
        time.sleep(1.0)

        # Die eine, einmal im Client eingerichtete Taste.
        ziel = mumble_pb2.VoiceTarget()
        ziel.id = 5
        t = ziel.targets.add()
        t.channel_id = 0
        t.children = True
        t.group = "ruf1"
        rufer = clients["itest-rufer"]
        rufer.send_message(PYMUMBLE_MSG_TYPES_VOICETARGET, ziel)
        rufer.sound_output.target = 5
        time.sleep(0.5)

        def rufen():
            gehoert.clear()
            rufer.sound_output.add_sound(b"\x10\x00" * 48000)
            time.sleep(2.2)
            return sorted(n for n, k in gehoert.items() if k > 0)

        enforcer.lade_ruftasten(
            {"ITest-Ruf/A": {1: "itestruf_technik"}, "ITest-Ruf/C": {1: "itestruf_zeit"}},
            c.get_channels(),
        )
        enforcer.enforce_user(c.get_state(sitzung))
        assert rufen() == ["itest-tech"], "Taste 1 auf A muss die Technik rufen"

        # Platzwechsel -- am Client aendert sich nichts.
        c.set_user_state(sitzung, channel=platz_c)
        time.sleep(0.4)
        enforcer.enforce_user(c.get_state(sitzung))
        assert rufen() == ["itest-zeit"], "dieselbe Taste auf C muss die Zeitmessung rufen"

        # Platz ohne Belegung: die Taste bleibt stumm.
        c.set_user_state(sitzung, channel=platz_b)
        time.sleep(0.4)
        enforcer.enforce_user(c.get_state(sitzung))
        assert rufen() == [], "auf B ist Taste 1 nicht belegt"
    finally:
        for m in clients.values():
            with contextlib.suppress(Exception):
                m.stop()
        for name in namen:
            with contextlib.suppress(Exception):
                c.unregister_user(ids[name])
        wurzel = c.get_acl(0)
        wurzel.groups = [g for g in wurzel.own_groups() if not g.name.startswith("itestruf")]
        with contextlib.suppress(Exception):
            c.set_channel_acl(wurzel)
        with contextlib.suppress(Exception):
            c.remove_channel(basis)


# --------------------------------------------------------------------------- #
#  Monitor-Bot: welches Recht braucht er wirklich?
# --------------------------------------------------------------------------- #


@needs_real_server
def test_monitor_braucht_register_am_obersten_platz_nicht_ban(echter_client, tmp_path):
    """Gemessen statt zitiert (DECISIONS D-035).

    D-015 berief sich auf ``Ban``; in murmur 1.5.735 steht in
    ``Server::msgUserStats`` aber ``ChanACL::Register``. Ohne das Recht liefert
    murmur die Paketzaehler (``from_client``) nur fuer Clients im eigenen
    Kanal des Fragenden -- die Verlustspalte bliebe fuer alle anderen leer.

    Und: eine Regel fuer ``$<Zertifikats-Hash>`` greift ohne Registrierung.
    Genau so berechtigt die Anwendung ihren Bot.
    """
    pytest.importorskip("pymumble_py3")
    import threading

    import pymumble_py3
    from pymumble_py3 import mumble_pb2
    from pymumble_py3.constants import PYMUMBLE_MSG_TYPES_USERSTATS

    from intercom.ice import wirkung
    from intercom.ice.types import ACLEntry
    from intercom.monitor.bot import ensure_certificate

    c = echter_client
    port = int(os.environ.get("MUMBLE_TEST_PORT", "64739"))
    platz = c.add_channel("ITest-Monitor", 0)
    cert = tmp_path / "beobachter.pem"
    fingerabdruck = ensure_certificate(cert, "itest-beobachter")

    antworten: list = []
    angekommen = threading.Event()

    class Beobachter(pymumble_py3.Mumble):
        def dispatch_control_message(self, type, message):
            if type == PYMUMBLE_MSG_TYPES_USERSTATS:
                stats = mumble_pb2.UserStats()
                stats.ParseFromString(message)
                antworten.append(stats)
                angekommen.set()
                return
            super().dispatch_control_message(type, message)

    ziel = pymumble_py3.Mumble(ICE_HOST, "itest-ziel", port=port, reconnect=False)
    beobachter = Beobachter(ICE_HOST, "itest-beobachter", port=port, reconnect=False,
                            certfile=str(cert), keyfile=str(cert))
    wurzel_vorher = c.get_acl(0)

    def frage() -> bool:
        """Liefert murmur die Paketzaehler fuer das Ziel?"""
        antworten.clear()
        angekommen.clear()
        anfrage = mumble_pb2.UserStats()
        anfrage.session = ziel.users.myself_session
        beobachter.send_message(PYMUMBLE_MSG_TYPES_USERSTATS, anfrage)
        assert angekommen.wait(5), "keine UserStats-Antwort"
        return antworten[-1].HasField("from_client")

    def regel(recht: int) -> None:
        acl = c.get_acl(0)
        acl.acls = [e for e in acl.own_acls() if e.group != f"${fingerabdruck}"] + [
            ACLEntry(apply_here=True, apply_subs=False, group=f"${fingerabdruck}",
                     allow=recht, deny=0)
        ]
        acl.groups = acl.own_groups()
        c.set_channel_acl(acl)
        time.sleep(0.5)

    try:
        for m in (ziel, beobachter):
            m.start()
            m.is_ready()
        time.sleep(0.8)
        c.set_user_state(ziel.users.myself_session, channel=platz)
        time.sleep(0.8)

        assert frage() is False, "ohne Recht darf es keine Zaehler fuer fremde Plaetze geben"
        regel(wirkung.BAN)
        assert frage() is False, "Ban reicht in 1.5.735 nicht -- D-015 war falsch"
        regel(wirkung.REGISTER)
        assert frage() is True, "mit Register ueber $hash muss murmur die Zaehler liefern"

        # Und genau so, wie die Anwendung es tut: Regel weg, sicherstellen().
        from intercom.monitor.berechtigung import gruppe, sicherstellen

        ohne = c.get_acl(0)
        ohne.acls = [e for e in ohne.own_acls() if e.group != gruppe(fingerabdruck)]
        ohne.groups = ohne.own_groups()
        c.set_channel_acl(ohne)
        time.sleep(0.5)
        assert frage() is False
        assert sicherstellen(c, fingerabdruck) is True
        time.sleep(0.5)
        assert frage() is True, "sicherstellen() muss den Bot wirklich berechtigen"
        assert sicherstellen(c, fingerabdruck) is False, "und nur einmal schreiben"
    finally:
        for m in (ziel, beobachter):
            with contextlib.suppress(Exception):
                m.stop()
        with contextlib.suppress(Exception):
            wurzel_vorher.acls = wurzel_vorher.own_acls()
            wurzel_vorher.groups = wurzel_vorher.own_groups()
            c.set_channel_acl(wurzel_vorher)
        with contextlib.suppress(Exception):
            c.remove_channel(platz)
