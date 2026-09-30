"""Provisioner: Planen, Anwenden, Exportieren.

Alles laeuft ueber echtes Ice gegen das murmur-Doppel aus ``fake_murmur.py``.
Die Abnahmekriterien aus dem Auftrag stehen hier:

* leerer Server -> apply -> export ergibt Aequivalenz
* zweites apply = keine Aenderungen
* ACL-Abbildung fuer speak, whisper_in, listen_for, guests_listen_only
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from tests.conftest import needs_ice

pytestmark = needs_ice


# --------------------------------------------------------------------------- #
#  Hilfen
# --------------------------------------------------------------------------- #


def _load(text: str):
    from intercom.provision.schema import parse_config

    return parse_config(yaml.safe_load(textwrap.dedent(text)))


def _snapshot(client) -> dict:
    """Vergleichbarer Abzug des Serverzustands.

    Enthaelt alles, was der Provisioner schreibt: Kanalbaum, eigene ACLs je
    Kanal und die Gruppen am Wurzelkanal -- als Namen statt IDs, damit zwei
    Server mit unterschiedlicher ID-Vergabe vergleichbar bleiben.
    """
    from intercom.provision.planner import Reconciler

    channels = client.get_channels()
    paths = Reconciler._build_paths(channels)
    registered = client.get_registered_users()

    result: dict = {"channels": {}, "groups": {}}
    for path, channel_id in sorted(paths.items()):
        if not path:
            continue
        channel = channels[channel_id]
        acl = client.get_acl(channel_id)
        result["channels"][path] = {
            "description": channel.description,
            "position": channel.position,
            "acls": [
                (a.group, a.userid, a.apply_here, a.apply_subs, a.allow, a.deny)
                for a in acl.own_acls()
            ],
            # Eigene Gruppen des Kanals gehoeren in den Abzug: sonst faellt beim
            # Aequivalenztest nicht auf, wenn der Export sie verliert.
            "groups": {
                g.name: (
                    g.inherit,
                    g.inheritable,
                    sorted(registered.get(uid, f"?{uid}") for uid in g.add),
                )
                for g in acl.own_groups()
            },
        }
    for group in client.get_acl(0).own_groups():
        result["groups"][group.name] = sorted(
            registered.get(uid, f"?{uid}") for uid in group.add
        )
    return result


CONFIG = """
    version: 1
    server:
      defaultchannel: "Intercom/Sammelruf"
      welcometext: "Stadion-Intercom"
    groups: [regie, kamera, technik, leitung]
    acl_templates:
      nur-hoeren:
        - group: all
          apply_here: true
          apply_sub: false
          allow: [Traverse, Enter, Listen]
          deny: [Speak, Whisper]
    channels:
      - name: Intercom
        description: "Wurzel des Intercoms"
        children:
          - name: Regie
            position: 10
            acl_template: nur-hoeren
            speak: [regie, leitung]
            whisper_in: [regie, technik]
            listen_for: [regie, leitung]
            priority: [regie]
            listen_to: ["Intercom/Kameras"]
          - name: Kameras
            position: 20
            speak: [kamera, regie]
            listen_for: [kamera, regie]
          - name: Sammelruf
            position: 30
            speak: [all]
            listen_for: [all]
    policies:
      whisper_anywhere: [regie, leitung]
      move_users: [leitung]
      kick: [leitung]
      ban: [leitung]
      guests_listen_only: true
    users:
      regie-1: { groups: [regie] }
      kam-1:   { groups: [kamera] }
      chef:    { groups: [leitung, regie] }
"""


@pytest.fixture()
def config():
    return _load(CONFIG)


def _register_all(client, config) -> None:
    for name in config.users:
        client.register_user(name, cert_hash=f"{name}-hash")


# --------------------------------------------------------------------------- #
#  Plan
# --------------------------------------------------------------------------- #


def test_plan_auf_leerem_server_aendert_nichts(ice_client, config):
    """Ein Plan ist ein Trockenlauf und darf den Server nicht anfassen."""
    from intercom.provision.planner import reconcile

    before = _snapshot(ice_client)
    plan = reconcile(ice_client, config, dry_run=True)

    assert not plan.empty
    assert any(c.kind == "channel_create" for c in plan.changes)
    assert _snapshot(ice_client) == before, "plan hat geschrieben"


def test_plan_meldet_nicht_registrierte_nutzer(ice_client, config):
    from intercom.provision.planner import reconcile

    plan = reconcile(ice_client, config, dry_run=True)
    assert sorted(plan.unknown_users) == ["chef", "kam-1", "regie-1"]
    assert "regie-1" in plan.to_text()


# --------------------------------------------------------------------------- #
#  Apply
# --------------------------------------------------------------------------- #


def test_apply_legt_den_baum_an(ice_client, config):
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    plan = reconcile(ice_client, config, dry_run=False)

    assert not plan.failed, [c.error for c in plan.failed]
    snapshot = _snapshot(ice_client)
    assert set(snapshot["channels"]) == {
        "Intercom",
        "Intercom/Regie",
        "Intercom/Kameras",
        "Intercom/Sammelruf",
    }
    assert snapshot["channels"]["Intercom/Regie"]["position"] == 10
    assert snapshot["channels"]["Intercom"]["description"] == "Wurzel des Intercoms"
    assert snapshot["groups"]["regie"] == ["chef", "regie-1"]
    assert snapshot["groups"]["kamera"] == ["kam-1"]


def test_zweites_apply_ist_leer(ice_client, config):
    """Idempotenz -- das wichtigste Kriterium ueberhaupt."""
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    second = reconcile(ice_client, config, dry_run=False)
    assert second.empty, "zweiter Lauf haette geschrieben:\n" + second.to_text()

    # Auch ein Plan danach muss leer sein.
    third = reconcile(ice_client, config, dry_run=True)
    assert third.empty, third.to_text()


def test_apply_repariert_eine_haendische_aenderung(ice_client, config):
    """Nach einem Eingriff im Client zieht der naechste Lauf ihn zurueck."""
    from intercom.provision.planner import Reconciler, reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    paths = Reconciler._build_paths(ice_client.get_channels())
    regie = paths["Intercom/Regie"]
    channel = ice_client.get_channel_state(regie)
    channel.description = "von Hand verstellt"
    ice_client.set_channel_state(channel)

    plan = reconcile(ice_client, config, dry_run=True)
    assert not plan.empty
    assert any(c.target == "Intercom/Regie" for c in plan.changes)

    reconcile(ice_client, config, dry_run=False)
    assert ice_client.get_channel_state(regie).description == ""


# --------------------------------------------------------------------------- #
#  ACL-Abbildung
# --------------------------------------------------------------------------- #


def _acls(client, path) -> dict[str, tuple[int, int]]:
    from intercom.provision.planner import Reconciler

    paths = Reconciler._build_paths(client.get_channels())
    acl = client.get_acl(paths[path])
    return {a.group: (a.allow, a.deny) for a in acl.own_acls() if a.is_group}


def test_acl_abbildung_speak(ice_client, config):
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    speak = BY_NAME["Speak"].bit

    entries = _acls(ice_client, "Intercom/Regie")
    # @all bekommt Speak verboten ...
    assert entries["all"][1] & speak
    assert not entries["all"][0] & speak
    # ... und die genannten Gruppen wieder erlaubt.
    assert entries["regie"][0] & speak
    assert entries["leitung"][0] & speak
    # kamera steht nicht in speak: und bekommt es nicht.
    assert "kamera" not in entries


def test_acl_abbildung_speak_all(ice_client, config):
    """``speak: [all]`` erlaubt @all das Sprechen, statt es zu verbieten."""
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    speak = BY_NAME["Speak"].bit

    entries = _acls(ice_client, "Intercom/Sammelruf")
    assert entries["all"][0] & speak
    assert not entries["all"][1] & speak


def test_acl_abbildung_whisper_in(ice_client, config):
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    whisper = BY_NAME["Whisper"].bit
    enter = BY_NAME["Enter"].bit

    entries = _acls(ice_client, "Intercom/Regie")
    assert entries["all"][1] & whisper
    assert entries["regie"][0] & whisper
    assert entries["technik"][0] & whisper
    # technik darf hineinfluestern, aber nicht betreten -- es steht nicht in speak:.
    assert not entries["technik"][0] & enter


def test_acl_abbildung_listen_for(ice_client, config):
    """Listen (0x800) fehlt in der Slice, wirkt aber -- siehe DECISIONS D-003."""
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    listen = 0x800

    entries = _acls(ice_client, "Intercom/Regie")
    assert entries["all"][1] & listen, "Listen wurde nicht verboten"
    assert entries["regie"][0] & listen, "Listen wurde nicht erlaubt"

    offen = _acls(ice_client, "Intercom/Sammelruf")
    assert offen["all"][0] & listen
    assert not offen["all"][1] & listen


def test_guests_listen_only_haengt_an_der_wurzel(ice_client, config):
    """Muss am Wurzelkanal mit apply_sub stehen, sonst gilt es nicht ueberall."""
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    root = ice_client.get_acl(0).own_acls()
    # Es gibt mehrere @all-Eintraege an der Wurzel: murmurs Vorgabe
    # (SelfRegister) und unsere Gastregel. Gemeint ist die, die etwas verbietet.
    guest = next(a for a in root if a.group == "all" and a.deny)
    assert guest.apply_subs is True, "gilt sonst nur am Wurzelkanal selbst"
    assert guest.deny & BY_NAME["Speak"].bit
    assert guest.deny & BY_NAME["Whisper"].bit

    # murmurs Vorgaben duerfen dabei nicht verlorengehen -- setACL ersetzt alles.
    vorgaben = {
        (a.group, a.allow) for a in root
    }
    assert ("admin", BY_NAME["Write"].bit) in vorgaben
    assert ("auth", BY_NAME["MakeTempChannel"].bit) in vorgaben
    assert ("all", BY_NAME["SelfRegister"].bit) in vorgaben


def test_root_only_rechte_stehen_an_der_wurzel_ohne_apply_sub(ice_client, config):
    """Kick/Ban/Register wertet murmur nur an der Wurzel und nur mit
    applyFromSelf aus -- apply_sub waere hier wirkungslos."""
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    root = ice_client.get_acl(0).own_acls()
    kick_entries = [a for a in root if a.allow & BY_NAME["Kick"].bit]
    assert kick_entries, "Kick-Recht fehlt"
    for entry in kick_entries:
        assert entry.apply_here is True
        assert entry.apply_subs is False
        assert entry.allow & BY_NAME["Ban"].bit, "Ban wurde nicht mit zusammengefasst"


def test_richtlinien_im_ganzen_baum_haben_apply_sub(ice_client, config):
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    root = ice_client.get_acl(0).own_acls()
    move = [a for a in root if a.allow & BY_NAME["Move"].bit]
    assert move and all(a.apply_subs for a in move)


# --------------------------------------------------------------------------- #
#  Export
# --------------------------------------------------------------------------- #


def test_export_ergibt_aequivalenz(ice_client, config, fake_murmur):
    """leerer Server -> apply -> export -> apply auf frischem Server -> gleich.

    Das ist die belastbare Fassung von "Aequivalenz": nicht der YAML-Text muss
    identisch sein, sondern der Serverzustand, den er erzeugt.
    """
    from intercom.ice.client import IceClient
    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config
    from tests.fake_murmur import FakeMurmur

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    original = _snapshot(ice_client)

    exported_text = export_yaml(ice_client)
    exported = parse_config(yaml.safe_load(exported_text))

    with FakeMurmur() as second_server:
        second = IceClient(second_server.settings())
        second.connect()
        try:
            for name in config.users:
                second.register_user(name, cert_hash=f"{name}-hash")
            plan = reconcile(second, exported, dry_run=False)
            assert not plan.failed, [c.error for c in plan.failed]
            assert _snapshot(second) == original, (
                "Export ist nicht aequivalent.\n" + exported_text
            )
        finally:
            second.close()


def test_export_bleibt_lesbar(ice_client, config):
    """Fuer Konfigurationen aus dieser Datei kommt die lesbare Form heraus.

    Der Name einer ``acl_template`` laesst sich nicht exportieren -- der Server
    speichert nur das Ergebnis, nicht die Herkunft. Was die Vorlage
    beigesteuert hat (hier Traverse und Enter fuer @all), erscheint deshalb als
    ein einzelner expliziter ``acl:``-Eintrag neben den lesbaren Regeln.
    """
    from intercom.provision.exporter import export_state
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    document = export_state(ice_client)
    intercom = next(c for c in document["channels"] if c["name"] == "Intercom")
    regie = next(c for c in intercom["children"] if c["name"] == "Regie")

    assert regie["speak"] == ["leitung", "regie"]
    assert regie["whisper_in"] == ["regie", "technik"]
    assert regie["listen_for"] == ["leitung", "regie"]
    # Nur der Rest aus der Vorlage, nicht die ganze ACL-Liste.
    assert [e["allow"] for e in regie["acl"]] == [["Traverse", "Enter"]]

    # Kameras hat keine Vorlage -- dort bleibt es vollstaendig lesbar.
    kameras = next(c for c in intercom["children"] if c["name"] == "Kameras")
    assert kameras["speak"] == ["kamera", "regie"]
    assert "acl" not in kameras

    assert document["policies"]["guests_listen_only"] is True
    assert document["users"]["chef"]["groups"] == ["leitung", "regie"]


def test_export_faengt_handgeklickte_acls_verlustfrei(ice_client):
    """Ein Muster, das der Generator nicht erzeugen kann, muss roh exportiert
    werden -- sonst geht beim Sichern etwas verloren."""
    from intercom.ice.types import ACLEntry, ChannelACL
    from intercom.provision.exporter import export_state

    channel_id = ice_client.add_channel("Handarbeit", 0)
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=channel_id,
            acls=[
                # Kein Muster des Generators: userid-ACL plus krumme Bits.
                ACLEntry(apply_here=True, apply_subs=True, userid=42, allow=0x20, deny=0x10),
            ],
        )
    )

    document = export_state(ice_client)
    node = next(c for c in document["channels"] if c["name"] == "Handarbeit")
    assert "acl" in node, "handgeklickte ACL wurde verschluckt"
    assert node["acl"][0]["userid"] == 42
    assert node["acl"][0]["allow"] == ["Move"]
    assert node["acl"][0]["deny"] == ["MuteDeafen"]


# --------------------------------------------------------------------------- #
#  Prune
# --------------------------------------------------------------------------- #


def test_prune_zeigt_aber_loescht_nicht(ice_client, config):
    from intercom.provision.planner import Reconciler, reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    paths = Reconciler._build_paths(ice_client.get_channels())
    ice_client.add_channel("Altlast", paths["Intercom"])

    plan = reconcile(ice_client, config, prune=False, dry_run=False)
    doomed = [c for c in plan.changes if c.kind == "channel_delete"]
    assert doomed, "der ueberzaehlige Kanal wurde nicht gemeldet"
    assert doomed[0].needs_prune
    assert not doomed[0].applied
    assert "Intercom/Altlast" in Reconciler._build_paths(ice_client.get_channels())


def test_prune_loescht_mit_schalter(ice_client, config):
    from intercom.provision.planner import Reconciler, reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    paths = Reconciler._build_paths(ice_client.get_channels())
    ice_client.add_channel("Altlast", paths["Intercom"])

    reconcile(ice_client, config, prune=True, dry_run=False)
    assert "Intercom/Altlast" not in Reconciler._build_paths(ice_client.get_channels())


def test_prune_fasst_kanaele_ausserhalb_des_baums_nicht_an(ice_client, config):
    """Ein Kanal neben Intercom gehoert dem Betreiber, nicht uns."""
    from intercom.provision.planner import Reconciler, reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)
    ice_client.add_channel("Musik", 0)

    reconcile(ice_client, config, prune=True, dry_run=False)
    assert "Musik" in Reconciler._build_paths(ice_client.get_channels())


def test_fremde_wurzelgruppe_ueberlebt_ohne_prune(ice_client, config):
    """setACL ersetzt alle Gruppen -- eine nicht verwaltete darf trotzdem
    nicht nebenbei verschwinden."""
    from intercom.ice.types import ChannelGroup
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    root = ice_client.get_acl(0)
    root.groups.append(ChannelGroup(name="haustechnik", add=[1]))
    ice_client.set_channel_acl(root)

    reconcile(ice_client, config, prune=False, dry_run=False)
    names = {g.name for g in ice_client.get_acl(0).own_groups()}
    assert "haustechnik" in names, "fremde Gruppe wurde mitgeloescht"
    assert "regie" in names


def test_kanalgruppen_ueberleben_den_export(ice_client, fake_murmur):
    """Regression: eine Gruppe an einem UNTERKANAL darf nicht verlorengehen.

    Der Exporter sammelte Gruppen frueher nur am Wurzelkanal. Ein von Hand am
    Unterkanal angelegter Ring verschwand damit aus der Sicherung -- die
    ACL-Eintraege zeigten nach dem Wiedereinspielen auf eine Gruppe, die es
    nicht mehr gab. Aufgefallen ist das nicht beim Anwenden auf denselben
    Server (dort bleiben unbekannte Gruppen stehen), sondern erst beim
    Einspielen auf einen frischen.
    """
    from intercom.ice.client import IceClient
    from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup
    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config
    from tests.fake_murmur import FakeMurmur

    uid = ice_client.register_user("kam-7", cert_hash="d" * 40)
    top = ice_client.add_channel("Intercom", 0)
    sub = ice_client.add_channel("Kameras", top)
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=sub,
            acls=[
                ACLEntry(
                    apply_here=True, apply_subs=False, group="kamera-lokal",
                    allow=0x08, deny=0,
                )
            ],
            groups=[ChannelGroup(name="kamera-lokal", add=[uid])],
        )
    )
    original = _snapshot(ice_client)
    assert original["channels"]["Intercom/Kameras"]["groups"], "Aufbau misslungen"

    exported = export_yaml(ice_client)
    assert "kamera-lokal" in exported, "Gruppe fehlt im YAML-Text"

    with FakeMurmur() as zweiter_server:
        zweiter = IceClient(zweiter_server.settings())
        zweiter.connect()
        try:
            zweiter.register_user("kam-7", cert_hash="d" * 40)
            plan = reconcile(zweiter, parse_config(yaml.safe_load(exported)), dry_run=False)
            assert not plan.failed, [c.error for c in plan.failed]
            assert _snapshot(zweiter) == original, (
                "Kanalgruppe hat den Umlauf nicht ueberlebt:\n" + exported
            )
        finally:
            zweiter.close()


def test_fremde_kanalgruppe_ueberlebt_ohne_prune(ice_client, config):
    """Wie an der Wurzel: setACL ersetzt alle Gruppen eines Kanals."""
    from intercom.ice.types import ChannelGroup
    from intercom.provision.planner import Reconciler, reconcile

    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    paths = Reconciler._build_paths(ice_client.get_channels())
    regie = paths["Intercom/Regie"]
    acl = ice_client.get_acl(regie)
    acl.groups.append(ChannelGroup(name="handarbeit", add=[]))
    ice_client.set_channel_acl(acl)

    plan = reconcile(ice_client, config, prune=False, dry_run=False)
    namen = {g.name for g in ice_client.get_acl(regie).own_groups()}
    assert "handarbeit" in namen, "fremde Kanalgruppe wurde mitgeloescht"
    assert any(c.needs_prune and "handarbeit" in c.target for c in plan.changes), (
        "der Plan haette sie als loeschbar melden muessen"
    )

    reconcile(ice_client, config, prune=True, dry_run=False)
    namen = {g.name for g in ice_client.get_acl(regie).own_groups()}
    assert "handarbeit" not in namen, "mit --prune haette sie weg sein muessen"


# --------------------------------------------------------------------------- #
#  murmurs Vorgabezustand
# --------------------------------------------------------------------------- #


def _murmur_vorgabe(client) -> int:
    """Stellt den Zustand her, den murmur bei einem frischen Server anlegt.

    ``src/murmur/ServerDB.cpp``, Z. 1039-1087: genau drei ACLs am Wurzelkanal
    und die Gruppe ``admin``. Das Doppel startet mit leerem Wurzelkanal --
    ohne diesen Aufbau sieht kein Test, was ein echter Server mitbringt.
    """
    from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup

    # Bewusst ein Name, der NICHT in der Testkonfiguration steht: dieser Nutzer
    # steht fuer jemanden, den der Betreiber vor dem ersten apply angelegt hat.
    chef = client.register_user("alt-admin", cert_hash="e" * 40)
    client.set_channel_acl(
        ChannelACL(
            channel_id=0,
            acls=[
                ACLEntry(apply_here=True, apply_subs=True, group="admin", allow=0x01, deny=0),
                ACLEntry(apply_here=True, apply_subs=True, group="auth", allow=0x400, deny=0),
                ACLEntry(apply_here=True, apply_subs=False, group="all", allow=0x80000, deny=0),
            ],
            groups=[ChannelGroup(name="admin", add=[chef])],
        )
    )
    return chef


def test_apply_nimmt_murmurs_vorgaben_nicht_weg(ice_client, config):
    """setACL ersetzt alles -- was murmur mitbringt, muss mitgeschrieben werden.

    Sonst verlieren angemeldete Nutzer beim allerersten ``apply`` das Anlegen
    temporaerer Kanaele und alle die Selbstregistrierung.
    """
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    chef = _murmur_vorgabe(ice_client)
    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    root = ice_client.get_acl(0)
    vorhanden = {(a.group, a.allow, a.apply_subs) for a in root.own_acls()}
    assert ("admin", BY_NAME["Write"].bit, True) in vorhanden
    assert ("auth", BY_NAME["MakeTempChannel"].bit, True) in vorhanden
    assert ("all", BY_NAME["SelfRegister"].bit, False) in vorhanden

    admin_gruppe = root.group("admin")
    assert admin_gruppe is not None, "murmurs admin-Gruppe wurde geloescht"
    assert chef in admin_gruppe.add, "die Mitgliedschaft ging verloren"


def test_export_vom_echten_vorgabezustand_ist_wieder_einlesbar(ice_client, config):
    """`intercom export > neu.yaml && intercom apply -c neu.yaml` muss gehen.

    Der Exporter schrieb die Wurzelgruppe ``admin`` nach ``groups:`` und ihre
    Mitglieder nach ``users:``; die Validierung wies beides als "eingebaut"
    zurueck. Der dokumentierte Sicherungsweg endete damit auf jedem echten
    Server mit Rueckgabewert 2. ``admin`` ist aber keine Meta-Gruppe, sondern
    eine echte Zeile in der Datenbank, deren Mitgliedschaft der Betreiber
    fuehrt -- sie gehoert in die Sicherung.
    """
    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config

    _murmur_vorgabe(ice_client)
    _register_all(ice_client, config)
    reconcile(ice_client, config, dry_run=False)

    text = export_yaml(ice_client)
    wieder = parse_config(yaml.safe_load(text))          # darf nicht werfen
    assert "admin" in wieder.groups
    assert "alt-admin" in wieder.users
    assert "admin" in wieder.users["alt-admin"]


def test_meta_gruppen_bleiben_verboten(ice_client):
    """`all` und Verwandte haben keine Mitgliederliste -- das muss auffallen."""
    from intercom.provision.schema import ConfigInvalid

    for name in ("all", "auth", "sub"):
        with pytest.raises(ConfigInvalid) as excinfo:
            _load(f"""
                version: 1
                groups: [{name}]
                channels:
                  - name: Intercom
            """)
        assert "Meta-Gruppe" in str(excinfo.value)


def test_fremder_wurzel_acl_ueberlebt_ohne_prune(ice_client, config):
    """Ein von Hand gesetztes Recht fuer einen einzelnen Nutzer bleibt stehen."""
    from intercom.ice.types import ACLEntry
    from intercom.provision.planner import reconcile

    chef = _murmur_vorgabe(ice_client)
    _register_all(ice_client, config)

    root = ice_client.get_acl(0)
    root.acls.append(
        ACLEntry(apply_here=True, apply_subs=False, userid=chef, allow=0x10, deny=0)
    )
    ice_client.set_channel_acl(root)

    plan = reconcile(ice_client, config, prune=False, dry_run=False)
    danach = ice_client.get_acl(0).own_acls()
    assert any(a.userid == chef for a in danach), "fremder Eintrag wurde mitgeloescht"
    assert any(c.needs_prune for c in plan.changes), "der Plan haette ihn melden muessen"

    reconcile(ice_client, config, prune=True, dry_run=False)
    danach = ice_client.get_acl(0).own_acls()
    assert not any(a.userid == chef for a in danach), "mit --prune haette er weg sein muessen"


# --------------------------------------------------------------------------- #
#  Zusammenfassen von ACL-Eintraegen
# --------------------------------------------------------------------------- #


def _sequenziell(start: int, eintraege) -> int:
    """Wertet Eintraege so aus wie ChanACL::effectivePermissions (ACL.cpp 222-225)."""
    granted = start
    for eintrag in eintraege:
        granted = (granted | eintrag.allow) & ~eintrag.deny
    return granted


def test_zusammenfassen_verhaelt_sich_wie_die_folge():
    """Eigenschaftstest: verschmolzen == nacheinander, fuer jeden Ausgangszustand.

    murmur wertet mehrere Eintraege derselben Gruppe der Reihe nach aus, der
    spaetere gewinnt. Wer sie mit 'allow |= ...; deny |= ...; allow &= ~deny'
    zusammenfasst, dreht das um und verschluckt lautlos genau die Rechte, die
    der spaetere Eintrag zurueckgeben sollte.
    """
    import random

    from intercom.ice.types import ACLEntry
    from intercom.provision.acl_map import merge_entries

    random.seed(20260827)
    for anzahl in (2, 3, 4):
        for _ in range(2000):
            eintraege = [
                ACLEntry(
                    apply_here=True,
                    apply_subs=False,
                    group="x",
                    allow=random.getrandbits(21),
                    deny=random.getrandbits(21),
                )
                for _ in range(anzahl)
            ]
            verschmolzen = merge_entries(eintraege)
            assert len(verschmolzen) == 1
            for start in (0, 0x1FFFFF, random.getrandbits(21)):
                assert _sequenziell(start, eintraege) == _sequenziell(start, verschmolzen), (
                    f"Abweichung bei {anzahl} Eintraegen, Start {start:#x}"
                )


def test_vorlage_und_regel_widersprechen_sich_die_regel_gewinnt(ice_client):
    """`whisper_in: [all]` gegen eine Vorlage, die Whisper verbietet.

    Die Regel steht spaeter und muss gewinnen -- sonst verschwindet sie
    spurlos, und im Kanal darf niemand hineinfluestern, obwohl es dasteht.
    """
    from intercom.ice.permissions import BY_NAME
    from intercom.provision.planner import reconcile

    config = _load("""
        version: 1
        groups: [regie]
        acl_templates:
          nur-hoeren:
            - group: all
              apply_here: true
              apply_sub: false
              allow: [Traverse, Enter, Listen]
              deny: [Speak, Whisper]
        channels:
          - name: Intercom
            children:
              - name: Ansage
                acl_template: nur-hoeren
                speak: [regie]
                whisper_in: [all]
    """)
    ice_client.register_user("regie-1", cert_hash="a" * 40)
    reconcile(ice_client, config, dry_run=False)

    entries = _acls(ice_client, "Intercom/Ansage")
    allow, deny = entries["all"]
    assert allow & BY_NAME["Whisper"].bit, "whisper_in: [all] ist verschwunden"
    assert not deny & BY_NAME["Whisper"].bit
    assert deny & BY_NAME["Speak"].bit, "speak: [regie] haette @all Speak nehmen muessen"


def test_rohexport_mit_widerspruechlichen_eintraegen(ice_client):
    """Handgeklickt: erst verbieten, dann erlauben. Der Umlauf darf das nicht drehen."""
    from intercom.ice.types import ACLEntry, ChannelACL
    from intercom.provision.exporter import export_yaml
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config

    SPEAK = 0x08
    kanal = ice_client.add_channel("Handarbeit", 0)
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=kanal,
            acls=[
                ACLEntry(apply_here=True, apply_subs=False, group="all", allow=0, deny=SPEAK),
                ACLEntry(apply_here=True, apply_subs=False, group="all", allow=SPEAK, deny=0),
            ],
        )
    )
    vorher = _snapshot(ice_client)["channels"]["Handarbeit"]["acls"]
    assert _sequenziell(0, ice_client.get_acl(kanal).own_acls()) & SPEAK, "Aufbau misslungen"

    exportiert = parse_config(yaml.safe_load(export_yaml(ice_client)))
    reconcile(ice_client, exportiert, dry_run=False)

    danach = ice_client.get_acl(kanal).own_acls()
    assert _sequenziell(0, danach) & SPEAK, (
        f"Speak wurde beim Umlauf verschluckt.\nvorher: {vorher}\n"
        f"danach: {[(a.group, hex(a.allow), hex(a.deny)) for a in danach]}"
    )


def test_verlinkungen_und_serverpasswort_konvergieren(ice_client):
    """Zwei Faelle, in denen der Server anders zurueckgibt als geschrieben wurde.

    * murmur spiegelt Verlinkungen. Wer nur die selbst angegebene Richtung
      schreibt, reisst im selben Lauf die Gegenrichtung ab, die der
      Partnerkanal gerade gesetzt hat.
    * ``setConf("serverpassword", ...)`` landet als ``password`` in der
      Datenbank, ``getAllConf`` liefert die Tabelle roh.

    Beides fuehrte dazu, dass ``apply`` bei JEDEM Lauf dieselbe Aenderung
    schrieb und der Plan dauerhaft auf rot stand.
    """
    from intercom.provision.planner import Reconciler, reconcile

    config = _load("""
        version: 1
        groups: [regie]
        server:
          serverpassword: stadion2026
        channels:
          - name: Intercom
            children:
              - name: Regie
                links: ["Intercom/Kameras"]
              - name: Kameras
              - name: Technik
                links: ["Intercom/Regie"]
    """)

    erster = reconcile(ice_client, config, dry_run=False)
    assert not erster.failed, [c.error for c in erster.failed]

    for lauf in (2, 3):
        weiterer = reconcile(ice_client, config, dry_run=False)
        assert weiterer.empty, f"Lauf {lauf} schreibt erneut:\n{weiterer.to_text()}"

    # Die Verlinkung ist die symmetrische Huelle der Angaben.
    paths = Reconciler._build_paths(ice_client.get_channels())
    umgekehrt = {cid: pfad for pfad, cid in paths.items()}
    verlinkt = {
        umgekehrt[cid]: sorted(umgekehrt[x] for x in kanal.links)
        for cid, kanal in ice_client.get_channels().items()
        if kanal.links
    }
    assert verlinkt["Intercom/Regie"] == ["Intercom/Kameras", "Intercom/Technik"]
    assert verlinkt["Intercom/Kameras"] == ["Intercom/Regie"]
    assert verlinkt["Intercom/Technik"] == ["Intercom/Regie"]

    # Das Passwort steht unter dem Namen, unter dem murmur es auch herausgibt.
    assert ice_client.get_all_conf()["password"] == "stadion2026"


def test_gescheitertes_setacl_meldet_nichts_als_angewendet(ice_client, config, monkeypatch):
    """``[angewendet]`` war frueher eine Behauptung ueber einen Aufruf, der noch lief.

    Eine fremde Gruppe verschwindet nicht durch einen eigenen Aufruf, sondern
    dadurch, dass sie in der Liste fehlt, die ``setACL`` schreibt. Schlaegt
    dieses ``setACL`` fehl, steht die Gruppe unveraendert im Server -- der
    Bericht meldete sie trotzdem als geloescht.
    """
    from intercom.ice.types import ChannelGroup
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    # Einmal sauber durchlaufen, damit nur noch die ACL-Aenderung offen ist.
    reconcile(ice_client, config, prune=False, dry_run=False)
    root = ice_client.get_acl(0)
    root.groups.append(ChannelGroup(name="haustechnik", add=[1]))
    ice_client.set_channel_acl(root)

    def kaputt(acl):
        raise RuntimeError("murmur mag nicht")

    monkeypatch.setattr(ice_client, "set_channel_acl", kaputt)

    plan = reconcile(ice_client, config, prune=True, dry_run=False)
    geloescht = [c for c in plan.changes if c.target == "(Wurzel) @haustechnik"]
    assert len(geloescht) == 1
    assert geloescht[0].applied is False, "Loeschung wurde faelschlich gemeldet"
    assert "murmur mag nicht" in geloescht[0].error
    assert not [c for c in plan.changes if c.applied], plan.to_text()
    assert "[angewendet]" not in plan.to_text()

    # Gegenprobe am Server: die Gruppe steht noch da.
    monkeypatch.undo()
    assert "haustechnik" in {g.name for g in ice_client.get_acl(0).own_groups()}


def test_erfolgreiches_prune_wird_als_angewendet_gemeldet(ice_client, config):
    """Gegenprobe: geht das setACL durch, ist die Meldung berechtigt."""
    from intercom.ice.types import ChannelGroup
    from intercom.provision.planner import reconcile

    _register_all(ice_client, config)
    root = ice_client.get_acl(0)
    root.groups.append(ChannelGroup(name="haustechnik", add=[1]))
    ice_client.set_channel_acl(root)

    plan = reconcile(ice_client, config, prune=True, dry_run=False)
    geloescht = next(c for c in plan.changes if c.target == "(Wurzel) @haustechnik")
    assert geloescht.applied is True
    assert geloescht.error == ""
    assert "haustechnik" not in {g.name for g in ice_client.get_acl(0).own_groups()}


# --------------------------------------------------------------------------- #
#  Eingebaute Vorlagen
# --------------------------------------------------------------------------- #


def test_alle_vorlagen_sind_gueltig():
    """Eine kaputte Vorlage darf nicht erst beim Anwenden auffallen."""
    from intercom.provision.vorlagen import VORLAGEN, vorlage_laden

    assert VORLAGEN, "es gibt keine einzige Vorlage"
    for vorlage in VORLAGEN:
        config = vorlage_laden(vorlage.schluessel)
        fehler = [i for i in config.issues if i.level == "error"]
        assert not fehler, f"{vorlage.schluessel}: {[i.message for i in fehler]}"
        assert config.channels, f"{vorlage.schluessel} legt keinen Kanal an"
        # Die Klartextangaben fuer die Oberflaeche duerfen nicht fehlen.
        assert vorlage.titel and vorlage.beschreibung and vorlage.legt_an


def test_unbekannte_vorlage_faellt_auf():
    """Ein Tippfehler im Schluessel darf nicht stillschweigend nichts tun."""
    import pytest as _pytest

    from intercom.provision.vorlagen import vorlage_laden

    with _pytest.raises(KeyError):
        vorlage_laden("gibtsnicht")


def test_vorlage_leichtathletik_legt_die_plaetze_an(ice_client):
    """Acht getrennte Kampfgerichte, dazu Zeitmessung, Buero und Technik."""
    from intercom.provision.planner import reconcile
    from intercom.provision.vorlagen import vorlage_laden

    config = vorlage_laden("leichtathletik")
    plan = reconcile(ice_client, config, dry_run=False)
    assert not plan.failed, [c.error for c in plan.failed]

    namen = {k.name for k in ice_client.get_channels().values()}
    for nummer in range(1, 9):
        assert f"Kampfgericht {nummer}" in namen
    for platz in ("Zeitmessung", "Wettkampfbüro", "Technik"):
        assert platz in namen

    # Die Kampfgerichte liegen in eigenen Kanaelen -- genau das ist der Sinn.
    kanaele = ice_client.get_channels()
    kg = [k for k in kanaele.values() if k.name.startswith("Kampfgericht ")]
    assert len({k.id for k in kg}) == 8


def test_vorlage_zweimal_anwenden_aendert_nichts(ice_client):
    """Eine Vorlage ist ein Startschuss, keine laufende Bindung."""
    from intercom.provision.planner import reconcile
    from intercom.provision.vorlagen import vorlage_laden

    config = vorlage_laden("klein")
    reconcile(ice_client, config, dry_run=False)
    zweiter = reconcile(ice_client, config, dry_run=False)
    assert zweiter.empty, "nicht idempotent:\n" + zweiter.to_text()


def test_vorlage_bindet_den_server_nicht(ice_client):
    """Nach dem Umbenennen darf nichts die Vorlage wieder herstellen.

    Das war der eigentliche Fehler der bisherigen Bauart: PROVISION_ON_START
    schrieb bei jedem Start die Datei zurueck, und was in der Oberflaeche
    geaendert wurde, war weg. Eine Vorlage wird einmal angewendet und ist dann
    fertig -- das haelt dieser Test fest.
    """
    from intercom.ice.types import MumbleChannel
    from intercom.provision.planner import reconcile
    from intercom.provision.vorlagen import vorlage_laden

    config = vorlage_laden("klein")
    reconcile(ice_client, config, dry_run=False)

    kanal = next(k for k in ice_client.get_channels().values() if k.name == "Technik")
    ice_client.set_channel_state(
        MumbleChannel(id=kanal.id, name="Tonregie", parent=kanal.parent)
    )
    namen = {k.name for k in ice_client.get_channels().values()}
    assert "Tonregie" in namen and "Technik" not in namen

    # Nichts im laufenden Betrieb stellt die Vorlage wieder her.
    namen_danach = {k.name for k in ice_client.get_channels().values()}
    assert namen_danach == namen


def test_vorlagen_beschreiben_genau_was_sie_anlegen(ice_client):
    """Die Oberfläche darf keine Zahl nennen, die hinterher nicht stimmt.

    Regel für dieses Projekt: nie etwas anzeigen, das auf dem Server nicht so
    steht. Für eine Vorlage heisst das: was unter „legt an" steht, muss der
    Wirklichkeit nach dem Anwenden entsprechen.
    """
    import re

    from intercom.provision.planner import reconcile
    from intercom.provision.vorlagen import VORLAGEN, vorlage_laden

    for vorlage in VORLAGEN:
        # Jede Vorlage auf einem frischen Server -- daher je Durchlauf pruefen,
        # was DIESE Vorlage anlegt.
        vorher = set(ice_client.get_channels())
        reconcile(ice_client, vorlage_laden(vorlage.schluessel), dry_run=False)
        neu = set(ice_client.get_channels()) - vorher

        text = " ".join(vorlage.legt_an)
        zahlen = [int(z) for z in re.findall(r"\b(\d+)\s+Kanäle", text)]
        assert zahlen, f"{vorlage.schluessel}: nennt keine Kanalzahl"
        assert len(neu) == zahlen[-1], (
            f"{vorlage.schluessel}: angekuendigt {zahlen[-1]}, angelegt {len(neu)}"
        )
