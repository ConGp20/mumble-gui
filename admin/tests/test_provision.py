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
    guest = next(a for a in root if a.group == "all")
    assert guest.apply_subs is True, "gilt sonst nur am Wurzelkanal selbst"
    assert guest.deny & BY_NAME["Speak"].bit
    assert guest.deny & BY_NAME["Whisper"].bit


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
