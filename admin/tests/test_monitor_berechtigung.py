"""Die Anwendung berechtigt ihren Monitor-Bot selbst (DECISIONS D-035).

Ohne ``Register`` am obersten Platz liefert murmur Paketzaehler nur fuer
Clients auf dem Platz des Bots. Gemessen gegen den echten Server in
``test_monitor_braucht_register_am_obersten_platz_nicht_ban``; hier geht es
darum, dass die Regel sauber gesetzt wird und dass Planer, Export und
"Aufraeumen" sie nicht wieder wegnehmen.
"""

from __future__ import annotations

import textwrap

import yaml

from intercom.ice import wirkung
from intercom.ice.types import ACLEntry, ChannelGroup
from intercom.monitor.berechtigung import gruppe, ist_regel, sicherstellen
from tests.conftest import needs_ice

FP = "AB12" * 10  # 40 Hex-Zeichen, wie ein SHA-1


def test_gruppe_ist_der_hash_klein_geschrieben():
    """murmur vergleicht mit ``qsHash`` -- und das ist Hex in Kleinbuchstaben."""
    assert gruppe(FP) == "$" + FP.lower()


def test_unvollstaendige_regel_zaehlt_nicht():
    halb = ACLEntry(apply_here=True, apply_subs=False, group=gruppe(FP),
                    allow=wirkung.BAN, deny=0)
    verboten = ACLEntry(apply_here=True, apply_subs=False, group=gruppe(FP),
                        allow=wirkung.REGISTER, deny=wirkung.REGISTER)
    voll = ACLEntry(apply_here=True, apply_subs=False, group=gruppe(FP),
                    allow=wirkung.REGISTER, deny=0)
    assert not ist_regel(halb, FP)
    assert not ist_regel(verboten, FP)
    assert ist_regel(voll, FP)


@needs_ice
def test_setzt_einmal_und_laesst_alles_andere_stehen(ice_client):
    vorher = ice_client.get_acl(0)
    vorher.acls = [*vorher.own_acls(), ACLEntry(apply_here=True, apply_subs=True,
                                                group="leitung", allow=wirkung.KICK, deny=0)]
    vorher.groups = [*vorher.own_groups(), ChannelGroup(name="leitung", add=[])]
    ice_client.set_channel_acl(vorher)

    assert sicherstellen(ice_client, FP) is True
    assert sicherstellen(ice_client, FP) is False, "zweiter Aufruf darf nichts schreiben"

    nachher = ice_client.get_acl(0)
    eigene = nachher.own_acls()
    assert ist_regel(eigene[-1], FP), "die Regel steht am Ende, damit nichts sie ueberstimmt"
    assert not eigene[-1].apply_subs
    assert any(e.group == "leitung" and e.allow & wirkung.KICK for e in eigene)
    assert any(g.name == "leitung" for g in nachher.own_groups())


@needs_ice
def test_ersetzt_eine_verbogene_fassung_statt_zu_verdoppeln(ice_client):
    acl = ice_client.get_acl(0)
    acl.acls = [*acl.own_acls(), ACLEntry(apply_here=True, apply_subs=False,
                                          group=gruppe(FP), allow=wirkung.BAN, deny=0)]
    acl.groups = acl.own_groups()
    ice_client.set_channel_acl(acl)

    assert sicherstellen(ice_client, FP) is True
    treffer = [e for e in ice_client.get_acl(0).own_acls() if e.group == gruppe(FP)]
    assert len(treffer) == 1 and ist_regel(treffer[0], FP)


def _config(text: str):
    from intercom.provision.schema import parse_config

    return parse_config(yaml.safe_load(textwrap.dedent(text)))


SHOW = """
version: 1
groups: [regie]
channels:
  - name: Intercom
    children:
      - name: Regie
        speak: [regie]
policies:
  kick: [regie]
"""


@needs_ice
def test_aufraeumen_nimmt_dem_bot_die_regel_nicht(ice_client):
    from intercom.provision.planner import reconcile

    sicherstellen(ice_client, FP)
    fremd = ice_client.get_acl(0)
    fremd.acls = [*fremd.own_acls(), ACLEntry(apply_here=True, apply_subs=False,
                                              group="fremd", allow=wirkung.MOVE, deny=0)]
    fremd.groups = fremd.own_groups()
    ice_client.set_channel_acl(fremd)

    plan = reconcile(ice_client, _config(SHOW), prune=True, dry_run=False,
                     geschuetzt=frozenset({gruppe(FP)}))
    assert not plan.failed
    # Keine eigene Loeschzeile fuer die Bot-Regel -- sie darf hoechstens im
    # Vorher/Nachher der Gesamtaenderung stehen, und dann auch nachher.
    assert not [c for c in plan.changes if gruppe(FP) in c.target]
    for c in plan.changes:
        if any(gruppe(FP) in z for z in c.before):
            assert any(gruppe(FP) in z for z in c.after), c.summary

    eigene = ice_client.get_acl(0).own_acls()
    assert any(ist_regel(e, FP) for e in eigene), "Aufraeumen hat die Bot-Regel geloescht"
    assert not any(e.group == "fremd" for e in eigene), "fremde Regeln raeumt es weiter weg"

    zweiter = reconcile(ice_client, _config(SHOW), prune=True, dry_run=True,
                        geschuetzt=frozenset({gruppe(FP)}))
    assert zweiter.empty, [c.summary for c in zweiter.changes]


@needs_ice
def test_ohne_schutz_waere_sie_weg(ice_client):
    """Gegenprobe: genau das passierte vorher bei jeder Show mit Aufraeumen."""
    from intercom.provision.planner import reconcile

    sicherstellen(ice_client, FP)
    reconcile(ice_client, _config(SHOW), prune=True, dry_run=False)
    assert not any(ist_regel(e, FP) for e in ice_client.get_acl(0).own_acls())


@needs_ice
def test_export_traegt_den_hash_nicht_in_die_datei(ice_client):
    from intercom.provision.exporter import export_yaml

    sicherstellen(ice_client, FP)
    mit = export_yaml(ice_client)
    ohne = export_yaml(ice_client, ohne_gruppen=frozenset({gruppe(FP)}))
    assert gruppe(FP) in mit, "Gegenprobe: ungeschuetzt landet er als Richtlinie in der Datei"
    assert gruppe(FP) not in ohne
