"""Ruftasten: Speicher, Vererbung, Abdeckung und Enforcer.

Was hier geprueft wird, ist die Logik um ``redirectWhisperGroup`` herum. Dass
der Mechanismus selbst traegt, steht in test_integration_real_server.py --
gemessen mit echten Clients, die wirklich Ton senden und empfangen.
"""

from __future__ import annotations

import pytest

from intercom import ruftasten as R
from intercom.ice import wirkung as W
from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup, MumbleChannel
from intercom.store.db import Store, ruf_gruppe
from tests.conftest import needs_ice

# --------------------------------------------------------------------------- #
#  Speicher
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "h.sqlite")
    s.connect()
    s.migrate()
    yield s
    s.close()


def test_taste_belegen_und_freigeben(store):
    store.set_ruftaste("Kampfgerichte", 1, "zeitmessung", "admin")
    store.set_ruftaste("Kampfgerichte", 2, "technik")
    assert store.ruftasten() == {"Kampfgerichte": {1: "zeitmessung", 2: "technik"}}
    store.set_ruftaste("Kampfgerichte", 1, None)
    assert store.ruftasten() == {"Kampfgerichte": {2: "technik"}}


def test_es_gibt_nur_vier_tasten(store):
    with pytest.raises(ValueError, match="Taste 5"):
        store.set_ruftaste("Technik", 5, "leitung")


def test_umbenennen_zieht_die_belegung_mit(store):
    store.set_ruftaste("Wettkampf/Technik", 1, "leitung")
    store.set_ruftaste("WettkampfBuero", 1, "leitung")
    store.ruftaste_umschreiben("Wettkampf", "Meeting")
    assert set(store.ruftasten()) == {"Meeting/Technik", "WettkampfBuero"}


def test_geloeschter_platz_nimmt_seine_belegung_mit(store):
    store.set_ruftaste("Kampfgerichte", 1, "zeitmessung")
    store.set_ruftaste("Kampfgerichte/Kampfgericht 3", 2, "technik")
    store.set_ruftaste("Technik", 1, "leitung")
    assert store.ruftaste_vergessen("Kampfgerichte") == 2
    assert store.ruftasten() == {"Technik": {1: "leitung"}}


def test_platzhalterzeichen_im_namen_ziehen_keine_fremden_plaetze_mit(store):
    """``_`` und ``%`` sind bei LIKE Platzhalter -- hier duerfen sie es nicht sein."""
    store.set_ruftaste("KG_1/Tisch", 1, "leitung")
    store.set_ruftaste("KGA1/Tisch", 1, "technik")
    store.set_ruftaste("100%/Tisch", 2, "leitung")
    store.set_ruftaste("1000/Tisch", 2, "technik")
    store.ruftaste_umschreiben("KG_1", "KG 1")
    assert set(store.ruftasten()) == {"KG 1/Tisch", "KGA1/Tisch", "100%/Tisch", "1000/Tisch"}
    assert store.ruftaste_vergessen("100%") == 1
    assert "1000/Tisch" in store.ruftasten()

    store.set_verbindung("hoert", "KG_1/Tisch", "Technik", an=True)
    store.set_verbindung("hoert", "KGA1/Tisch", "Technik", an=True)
    store.verbindung_umschreiben("KG_1", "KG 1")
    assert set(store.verbindungen()["hoert"]) == {"KG 1/Tisch", "KGA1/Tisch"}
    store.verbindung_vergessen("KG 1")
    assert set(store.verbindungen()["hoert"]) == {"KGA1/Tisch"}

    store.set_wunsch("platz", 7, ["KG_1/Tisch"], "admin")
    store.set_wunsch("platz", 8, ["KGA1/Tisch"], "admin")
    store.wunsch_umschreiben("KG_1", "KG 1")
    assert store.alle_wuensche()["platz"] == {7: ["KG 1/Tisch"], 8: ["KGA1/Tisch"]}


def test_feste_rufgruppen():
    """Genau diesen Namen tippt man im Client ein -- er darf sich nie aendern."""
    assert [ruf_gruppe(t) for t in (1, 2, 3, 4)] == ["ruf1", "ruf2", "ruf3", "ruf4"]


# --------------------------------------------------------------------------- #
#  Vererbung und Abdeckung
# --------------------------------------------------------------------------- #


def _baum():
    k = {0: MumbleChannel(id=0, name="Root", parent=-1)}
    for kid, name, parent in (
        (1, "Kampfgerichte", 0), (2, "Kampfgericht 1", 1), (3, "Kampfgericht 2", 1),
        (4, "Wettkampf", 0), (5, "Zeitmessung", 4),
    ):
        k[kid] = MumbleChannel(id=kid, name=name, parent=parent)
    return k


def _acls(fluestern_bei_zeitmessung: bool = True):
    def e(gruppe, allow=0, deny=0):
        return ACLEntry(apply_here=True, apply_subs=True, allow=allow, deny=deny, group=gruppe)

    return {
        0: ChannelACL(0, acls=[e("all", deny=W.SPEAK | W.WHISPER)], groups=[
            ChannelGroup(name="kampfgericht", add=[1]),
            ChannelGroup(name="zeitmessung", add=[2]),
        ]),
        1: ChannelACL(1, acls=[e("kampfgericht", allow=W.SPEAK)]),
        2: ChannelACL(2),
        3: ChannelACL(3),
        4: ChannelACL(4),
        5: ChannelACL(5, acls=[e("zeitmessung", allow=W.SPEAK)] + (
            [e("kampfgericht", allow=W.WHISPER)] if fluestern_bei_zeitmessung else []
        )),
    }


def test_belegung_am_ordner_gilt_fuer_alle_darunter():
    belegung = {"Kampfgerichte": {1: "zeitmessung"}}
    for kid in (2, 3):
        wirk = R.wirksame_belegung(kid, _baum(), belegung)
        assert wirk[1].rolle == "zeitmessung"
        assert wirk[1].von == "Kampfgerichte"


def test_naeherer_platz_ueberschreibt():
    belegung = {"Kampfgerichte": {1: "zeitmessung"}, "Kampfgerichte/Kampfgericht 2": {1: "technik"}}
    assert R.wirksame_belegung(2, _baum(), belegung)[1].rolle == "zeitmessung"
    assert R.wirksame_belegung(3, _baum(), belegung)[1].rolle == "technik"


def test_oben_belegt_gilt_ueberall():
    belegung = {"": {4: "all"}}
    assert R.wirksame_belegung(5, _baum(), belegung)[4].rolle == "all"


def test_geltungsbereich_spart_ueberschriebene_plaetze_aus():
    belegung = {"Kampfgerichte": {1: "zeitmessung"}, "Kampfgerichte/Kampfgericht 2": {1: "technik"}}
    assert R.geltungsbereich(1, 1, _baum(), belegung) == [1, 2]


def test_abdeckung_findet_fehlendes_fluesterrecht():
    """Der Ruf kommt nur an, wo der Rufende reinschalten darf."""
    belegung = {"Kampfgerichte": {1: "zeitmessung"}}
    mit = R.abdeckung(1, 1, "zeitmessung", _baum(), _acls(True), ["kampfgericht", "zeitmessung"], belegung)
    ohne = R.abdeckung(1, 1, "zeitmessung", _baum(), _acls(False), ["kampfgericht", "zeitmessung"], belegung)
    assert mit.rufende == ["kampfgericht"]
    assert mit.zielplaetze == [5]
    assert mit.fehlt == []
    assert ohne.fehlt == [("kampfgericht", 5)]


def test_wer_nicht_sprechen_darf_ruft_nicht():
    """murmur verwirft die Sprache Unterdrueckter ganz -- auch das Fluestern."""
    belegung = {"Wettkampf": {1: "kampfgericht"}}
    ab = R.abdeckung(4, 1, "kampfgericht", _baum(), _acls(), ["kampfgericht", "zeitmessung"], belegung)
    # Auf "Wettkampf" selbst darf niemand sprechen, auf "Zeitmessung" darunter
    # schon -- also ruft die Zeitmessung, sonst niemand.
    assert ab.rufende == ["zeitmessung"]


# --------------------------------------------------------------------------- #
#  Enforcer
# --------------------------------------------------------------------------- #


@needs_ice
def test_enforcer_leitet_beim_verbinden_und_beim_platzwechsel_um(ice_client, fake_murmur):
    from intercom.runtime import Enforcer

    a = ice_client.add_channel("Kampfgericht 1", 0)
    b = ice_client.add_channel("Technik", 0)
    enforcer = Enforcer(ice_client)
    enforcer.lade_ruftasten(
        {"Kampfgericht 1": {1: "zeitmessung", 2: "technik"}, "Technik": {1: "leitung"}},
        ice_client.get_channels(),
    )

    session = fake_murmur.server.connect_user("kg", channel=a)
    enforcer.enforce_user(ice_client.get_state(session))
    assert fake_murmur.server.whisper_redirects[session] == {
        "ruf1": "zeitmessung", "ruf2": "technik"
    }

    # Platzwechsel: Taste 1 ruft jetzt die Leitung, Taste 2 ist dort frei.
    ice_client.set_user_state(session, channel=b)
    enforcer.enforce_user(ice_client.get_state(session))
    assert fake_murmur.server.whisper_redirects[session] == {"ruf1": "leitung"}


@needs_ice
def test_enforcer_schreibt_nur_was_sich_aendert(ice_client, fake_murmur, monkeypatch):
    """Sonst loeste das UserState-Echo der eigenen Aenderung eine Schleife aus."""
    from intercom.runtime import Enforcer

    a = ice_client.add_channel("Kampfgericht 1", 0)
    enforcer = Enforcer(ice_client)
    enforcer.lade_ruftasten({"Kampfgericht 1": {1: "zeitmessung"}}, ice_client.get_channels())
    session = fake_murmur.server.connect_user("kg", channel=a)

    aufrufe = []
    original = ice_client.redirect_whisper_group
    monkeypatch.setattr(
        ice_client, "redirect_whisper_group",
        lambda *args: (aufrufe.append(args), original(*args))[1],
    )
    user = ice_client.get_state(session)
    enforcer.enforce_user(user)
    enforcer.enforce_user(user)
    enforcer.enforce_user(user)
    assert len(aufrufe) == 1


@needs_ice
def test_nach_dem_trennen_wird_neu_gesetzt(ice_client, fake_murmur):
    """Die Umleitung haengt am ServerUser und stirbt mit ihm."""
    from intercom.runtime import Enforcer

    a = ice_client.add_channel("Kampfgericht 1", 0)
    enforcer = Enforcer(ice_client)
    enforcer.lade_ruftasten({"Kampfgericht 1": {1: "zeitmessung"}}, ice_client.get_channels())

    erste = fake_murmur.server.connect_user("kg", channel=a)
    enforcer.enforce_user(ice_client.get_state(erste))
    fake_murmur.server.disconnect_user(erste)
    enforcer.vergiss_sitzung(erste)

    zweite = fake_murmur.server.connect_user("kg", channel=a)
    enforcer.enforce_user(ice_client.get_state(zweite))
    assert fake_murmur.server.whisper_redirects[zweite] == {"ruf1": "zeitmessung"}
