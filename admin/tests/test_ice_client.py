"""Ice-Anbindung gegen das murmur-Doppel.

Diese Tests laufen ueber echtes Ice: Proxy, Serialisierung, Objektadapter und
Rueckrufe sind dieselben wie im Betrieb. Nur der Server dahinter ist ein Doppel.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests.conftest import needs_ice

pytestmark = needs_ice


def test_verbindung_und_version(ice_client, fake_murmur):
    version = ice_client.get_version()
    assert version.short == "1.5.735"
    assert ice_client.connected
    # Gleiche Slice auf beiden Seiten -> keine Versionswarnung.
    assert ice_client.version_warnings == []


def test_version_abweichung_wird_gemeldet(fake_murmur):
    from intercom.ice.client import IceClient

    client = IceClient(fake_murmur.settings(expected_mumble_version="v1.4.287"))
    client.connect()
    try:
        assert any("1.4.287" in w for w in client.version_warnings)
    finally:
        client.close()


def test_kanaele_anlegen_und_lesen(ice_client):
    channel_id = ice_client.add_channel("Intercom", 0)
    assert channel_id > 0
    channels = ice_client.get_channels()
    assert channels[channel_id].name == "Intercom"
    assert channels[channel_id].parent == 0
    assert channels[0].is_root


def test_doppelter_kanalname_scheitert(ice_client):
    from intercom.ice.errors import IceCallFailed

    ice_client.add_channel("Regie", 0)
    with pytest.raises(IceCallFailed):
        ice_client.add_channel("Regie", 0)


def test_nutzer_werden_vollstaendig_uebersetzt(ice_client, fake_murmur):
    session = fake_murmur.server.connect_user(
        "kam-1", userid=7, address="10.20.30.44", tcp_only=True
    )
    users = ice_client.get_users()
    user = users[session]
    assert user.name == "kam-1"
    assert user.address == "10.20.30.44"      # IPv4-mapped korrekt zurueckgerechnet
    assert user.tcp_only is True
    assert user.registered is True
    assert user.version == "1.5.735"          # aus version2
    assert user.ping == pytest.approx(12.5)


def test_acl_schreiben_und_lesen(ice_client):
    from intercom.ice.permissions import names_to_mask
    from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup

    top = ice_client.add_channel("Intercom", 0)
    acl = ChannelACL(
        channel_id=top,
        inherit=True,
        acls=[
            ACLEntry(
                apply_here=True,
                apply_subs=False,
                group="all",
                allow=names_to_mask(["Traverse", "Enter", "Listen"]),
                deny=names_to_mask(["Speak", "Whisper"]),
            )
        ],
        groups=[ChannelGroup(name="regie", add=[7, 9])],
    )
    ice_client.set_channel_acl(acl)

    read_back = ice_client.get_acl(top)
    own = read_back.own_acls()
    assert len(own) == 1
    # Listen (0x800) ist in der Slice nicht deklariert, ueberlebt aber setACL.
    assert own[0].allow & 0x800
    assert own[0].deny & 0x8
    group = read_back.group("regie")
    assert group is not None
    assert group.add == [7, 9]


def test_geerbte_eintraege_werden_beim_schreiben_gefiltert(ice_client):
    """Der Kern des ACL-Editors.

    ``getACL`` liefert geerbte Eintraege mit. Wer sie unveraendert
    zurueckschreibt, kopiert sie in den Unterkanal und friert die Vererbung ein.
    ``set_channel_acl`` muss sie herauswerfen.
    """
    from intercom.ice.permissions import names_to_mask
    from intercom.ice.types import ACLEntry, ChannelACL, ChannelGroup

    top = ice_client.add_channel("Intercom", 0)
    child = ice_client.add_channel("Regie", top)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=top,
            acls=[
                ACLEntry(
                    apply_here=True,
                    apply_subs=True,          # vererbt sich nach unten
                    group="all",
                    allow=0,
                    deny=names_to_mask(["Speak"]),
                )
            ],
            groups=[ChannelGroup(name="regie", add=[7])],
        )
    )

    child_acl = ice_client.get_acl(child)
    assert any(a.inherited for a in child_acl.acls), "Vererbung nicht sichtbar"
    assert child_acl.own_acls() == []

    # Unveraendert zurueckschreiben darf nichts in den Kanal kopieren.
    ice_client.set_channel_acl(child_acl)
    after = ice_client.get_acl(child)
    assert after.own_acls() == [], "geerbter Eintrag wurde in den Kanal kopiert"
    assert after.own_groups() == [], "geerbte Gruppe wurde in den Kanal kopiert"


def test_listener_haengen_an_der_session(ice_client, fake_murmur):
    """Belegt die Einschraenkung, die im README dokumentiert ist."""
    top = ice_client.add_channel("Intercom", 0)
    other = ice_client.add_channel("Zeitnahme", top)
    session = fake_murmur.server.connect_user("regie-1", userid=3)

    ice_client.start_listening(session, other)
    assert ice_client.get_listening_channels(session) == [other]
    assert ice_client.get_listening_users(other) == [session]
    assert ice_client.is_listening(session, other)

    # Reconnect: neue Session, Listener weg.
    fake_murmur.server.disconnect_user(session)
    new_session = fake_murmur.server.connect_user("regie-1", userid=3)
    assert new_session != session
    assert ice_client.get_listening_channels(new_session) == []


def test_nutzerzustand_aendert_nur_das_uebergebene_feld(ice_client, fake_murmur):
    top = ice_client.add_channel("Intercom", 0)
    session = fake_murmur.server.connect_user("regie-1", userid=3)

    ice_client.set_user_state(session, comment="Regiewagen")
    ice_client.set_user_state(session, priority_speaker=True)
    ice_client.set_user_state(session, channel=top)

    user = ice_client.get_state(session)
    assert user.comment == "Regiewagen"     # nicht vom zweiten Aufruf geloescht
    assert user.priority_speaker is True
    assert user.channel == top


def test_callbacks_kommen_an(ice_client, fake_murmur):
    """murmur ruft aus eigenen Threads zurueck -- der Adapter muss stehen."""
    seen: list[tuple[str, object]] = []
    arrived = threading.Event()

    def handler(event: str, payload: object) -> None:
        seen.append((event, payload))
        arrived.set()

    ice_client.callbacks.subscribe(handler)
    fake_murmur.server.connect_user("kam-2", userid=8)

    assert arrived.wait(timeout=5.0), "userConnected kam nicht an"
    events = [event for event, _ in seen]
    assert "user_connected" in events
    user = dict(seen)["user_connected"]
    assert user.name == "kam-2"


def test_kaputter_handler_meldet_den_callback_nicht_ab(ice_client, fake_murmur):
    """murmur wirft Callbacks weg, die eine Ausnahme werfen. Wir fangen alles."""
    good: list[str] = []

    ice_client.callbacks.subscribe(lambda e, p: (_ for _ in ()).throw(RuntimeError("boom")))
    ice_client.callbacks.subscribe(lambda e, p: good.append(e))

    fake_murmur.server.connect_user("kam-3", userid=9)
    deadline = time.time() + 5.0
    while time.time() < deadline and not good:
        time.sleep(0.05)
    assert good, "der zweite Handler wurde nicht mehr bedient"
    assert len(fake_murmur.server.callbacks) == 1, "murmur haette abgemeldet"


def test_registrierung(ice_client):
    userid = ice_client.register_user("kam-1", cert_hash="a" * 40)
    assert userid > 0
    registered = ice_client.get_registered_users()
    assert registered[userid] == "kam-1"
    entry = ice_client.get_registration(userid)
    assert entry.name == "kam-1"
    assert entry.short_hash == "a" * 12
    assert ice_client.get_user_ids(["kam-1", "gibtsnicht"]) == {"kam-1": userid, "gibtsnicht": -1}


def test_bans_roundtrip(ice_client):
    from intercom.ice.types import BanEntry

    ice_client.set_bans([BanEntry(address="10.20.30.99", bits=32, reason="Testbann")])
    bans = ice_client.get_bans()
    assert len(bans) == 1
    assert bans[0].address == "10.20.30.99"   # ueber 16-Byte-Form und zurueck
    assert bans[0].permanent is True


def test_falsches_secret_wird_klar_gemeldet(fake_murmur):
    """Die haeufigste Fehlkonfiguration ueberhaupt."""
    from intercom.ice.client import IceClient
    from intercom.ice.errors import IceCallFailed

    # Das Doppel prueft kein Secret; wir pruefen die Uebersetzung eines
    # falschen ICE_SERVER_ID, der zweithaeufigsten Fehlkonfiguration.
    client = IceClient(fake_murmur.settings(ice_server_id=99))
    with pytest.raises(IceCallFailed) as excinfo:
        client.connect()
    assert "ICE_SERVER_ID" in str(excinfo.value)


def test_ohne_verbindung_klare_meldung(fake_murmur):
    from intercom.ice.client import IceClient
    from intercom.ice.errors import IceNotConnected

    client = IceClient(fake_murmur.settings())
    with pytest.raises(IceNotConnected):
        client.get_users()


def test_haengender_aufruf_wird_abgebrochen(fake_murmur, monkeypatch):
    """Ein murmur, der nicht antwortet, darf keinen Thread festhalten.

    Das ist der Grund fuer ``ice_invocationTimeout``: ``Ice.Override.Timeout``
    ist ein Endpunkt-Timeout und greift hier nachweislich nicht -- ein Aufruf
    lief damit gemessen 12 s durch, obwohl 3 s eingestellt waren.
    """
    from intercom.ice import client as client_modul
    from intercom.ice.errors import IceConnectionLost

    monkeypatch.setattr(client_modul, "INVOCATION_TIMEOUT_MS", 800)
    client = client_modul.IceClient(fake_murmur.settings())
    client.connect()
    try:
        fake_murmur.server.haenge_getChannels_s = 5.0
        begonnen = time.monotonic()
        with pytest.raises(IceConnectionLost) as excinfo:
            client.get_channels()
        gedauert = time.monotonic() - begonnen

        assert gedauert < 3.0, f"Aufruf lief {gedauert:.1f} s statt 0,8 s"
        assert "0 s" in str(excinfo.value) or "nicht" in str(excinfo.value)
        # Der tote Proxy ist weg, der Reconnect-Task kann uebernehmen.
        assert not client.connected
    finally:
        fake_murmur.server.haenge_getChannels_s = 0.0
        client.close()


def test_abbau_haelt_die_sperre_nicht(fake_murmur, monkeypatch):
    """``communicator.destroy()`` darf ``_lock`` nicht ueber Sekunden halten.

    ``destroy()`` wartet auf laufende Aufrufe. Blockiert es mit gehaltener
    Sperre, steht genau in dem Moment auch der Reconnect, der die Verbindung
    retten soll.
    """
    from intercom.ice.client import IceClient

    client = IceClient(fake_murmur.settings())
    client.connect()

    im_abbau = threading.Event()
    weiter = threading.Event()
    echt = IceClient._destroy_communicator

    def langsam(communicator):
        im_abbau.set()
        assert weiter.wait(5.0), "Test haengt"
        echt(communicator)

    monkeypatch.setattr(IceClient, "_destroy_communicator", staticmethod(langsam))

    schliesser = threading.Thread(target=client.close, daemon=True)
    schliesser.start()
    assert im_abbau.wait(5.0), "close() kam nicht bis zum Abbau"

    # Waehrend der Abbau haengt, muss die Sperre frei sein.
    frei = client._lock.acquire(timeout=1.0)
    if frei:
        client._lock.release()
    weiter.set()
    schliesser.join(5.0)
    assert frei, "_lock wurde ueber communicator.destroy() hinweg gehalten"


def test_kanal_ohne_vererbung_sieht_trotzdem_den_elternteil(ice_client, fake_murmur):
    """murmur zeigt bei ``inherit=False`` weiter die Eintraege des Elternteils.

    ``impl_Server_getACL`` steigt **immer** mindestens bis zum direkten
    Elternteil auf -- ``if ((p == channel) || (p->bInheritACL))``. Erst dessen
    ``bInheritACL`` entscheidet, ob es weiter nach oben geht. Der Haken selbst
    kommt getrennt als drittes Feld zurueck; der ACL-Editor im Mumble-Client
    zeigt damit an, was bei eingeschalteter Vererbung gelten wuerde.

    Das ist nicht dasselbe wie ``ChanACL::effectivePermissions`` -- die prueft
    ``bInheritACL`` auch am Kanal selbst und bricht dort ab.
    """
    from intercom.ice.types import ACLEntry, ChannelACL

    eltern = ice_client.add_channel("Intercom", 0)
    kind = ice_client.add_channel("Regie", eltern)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=eltern,
            acls=[ACLEntry(apply_here=True, apply_subs=True, allow=0, deny=0x2, group="all")],
            groups=[],
            inherit=True,
        )
    )
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=kind,
            acls=[ACLEntry(apply_here=True, apply_subs=False, allow=0x2, deny=0, group="regie")],
            groups=[],
            inherit=False,
        )
    )

    acl = ice_client.get_acl(kind)
    assert acl.inherit is False
    geerbt = [e for e in acl.acls if e.inherited]
    assert [e.group for e in geerbt] == ["all"], acl.acls
    assert [e.group for e in acl.own_acls()] == ["regie"]


def test_vererbung_bricht_am_elternteil_ab(ice_client, fake_murmur):
    """Steht der Haken beim *Elternteil* nicht, endet der Aufstieg dort."""
    from intercom.ice.types import ACLEntry, ChannelACL

    opa = ice_client.add_channel("Haus", 0)
    eltern = ice_client.add_channel("Intercom", opa)
    kind = ice_client.add_channel("Regie", eltern)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=opa,
            acls=[ACLEntry(apply_here=True, apply_subs=True, allow=0x1, deny=0, group="haus")],
            groups=[],
            inherit=True,
        )
    )
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=eltern,
            acls=[ACLEntry(apply_here=True, apply_subs=True, allow=0, deny=0x2, group="all")],
            groups=[],
            inherit=False,
        )
    )

    gruppen = {e.group for e in ice_client.get_acl(kind).acls if e.inherited}
    assert gruppen == {"all"}, "der Aufstieg lief ueber den Elternteil hinaus"


def test_gruppen_haengen_nicht_am_vererbungshaken(ice_client, fake_murmur):
    """``bInheritACL`` steuert die ACLs, nicht die Gruppen.

    murmur sammelt die Gruppen ueber ``Group::groupNames`` und
    ``Group::getGroup``; die kennen nur ``inheritable`` und ``inherit`` der
    Gruppe selbst. Eine vererbbare Gruppe des Elternteils bleibt deshalb auch
    dann sichtbar, wenn der Kanal die ACL-Vererbung abgeschaltet hat.
    """
    from intercom.ice.types import ChannelACL, ChannelGroup

    eltern = ice_client.add_channel("Intercom", 0)
    kind = ice_client.add_channel("Regie", eltern)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=eltern,
            acls=[],
            groups=[ChannelGroup(name="technik", inherit=True, inheritable=True, add=[7])],
            inherit=True,
        )
    )
    ice_client.set_channel_acl(
        ChannelACL(channel_id=kind, acls=[], groups=[], inherit=False)
    )

    gruppen = {g.name: g for g in ice_client.get_acl(kind).groups}
    assert "technik" in gruppen, "vererbbare Gruppe verschwand mit dem Haken"
    assert gruppen["technik"].inherited is True
    assert gruppen["technik"].members == [7]


def test_nicht_vererbbare_gruppe_bleibt_beim_elternteil(ice_client, fake_murmur):
    from intercom.ice.types import ChannelACL, ChannelGroup

    eltern = ice_client.add_channel("Intercom", 0)
    kind = ice_client.add_channel("Regie", eltern)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=eltern,
            acls=[],
            groups=[ChannelGroup(name="intern", inherit=True, inheritable=False, add=[7])],
            inherit=True,
        )
    )

    assert "intern" not in {g.name for g in ice_client.get_acl(kind).groups}
    assert "intern" in {g.name for g in ice_client.get_acl(eltern).groups}


def test_eigene_gruppe_ergaenzt_die_geerbten_mitglieder(ice_client, fake_murmur):
    """``add`` und ``remove`` wirken auf die Mitglieder des Elternteils."""
    from intercom.ice.types import ChannelACL, ChannelGroup

    eltern = ice_client.add_channel("Intercom", 0)
    kind = ice_client.add_channel("Regie", eltern)

    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=eltern,
            acls=[],
            groups=[ChannelGroup(name="technik", inherit=True, inheritable=True, add=[7, 8])],
            inherit=True,
        )
    )
    ice_client.set_channel_acl(
        ChannelACL(
            channel_id=kind,
            acls=[],
            groups=[
                ChannelGroup(name="technik", inherit=True, inheritable=True, add=[9], remove=[8])
            ],
            inherit=True,
        )
    )

    gruppe = next(g for g in ice_client.get_acl(kind).groups if g.name == "technik")
    assert gruppe.inherited is False
    assert gruppe.members == [7, 9]
    assert gruppe.add == [9]
    assert gruppe.remove == [8]
