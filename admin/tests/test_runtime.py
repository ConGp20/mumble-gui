"""Laufzeit-Abgleich: Priority Speaker und Listener.

Beides ist Sitzungszustand und ueberlebt keinen Reconnect -- diese Tests
belegen, dass der Enforcer das auffaengt.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from tests.conftest import needs_ice

pytestmark = needs_ice

CONFIG = """
version: 1
groups: [regie, kamera, leitung]
channels:
  - name: Intercom
    children:
      - name: Regie
        speak: [regie, leitung]
        listen_for: [regie, leitung]
        priority: [regie]
        listen_to: ["Intercom/Kameras", "Intercom/Zeitnahme"]
      - name: Kameras
        speak: [kamera, regie]
        listen_for: [kamera, regie]
      - name: Zeitnahme
        speak: [regie]
        listen_for: [regie]
policies:
  guests_listen_only: true
users:
  regie-1: { groups: [regie] }
  kam-1:   { groups: [kamera] }
"""


@pytest.fixture()
def armed(ice_client):
    """Server provisioniert, Enforcer geladen."""
    from intercom.provision.acl_map import build_desired_state
    from intercom.provision.planner import reconcile
    from intercom.provision.schema import parse_config
    from intercom.runtime import Enforcer

    config = parse_config(yaml.safe_load(textwrap.dedent(CONFIG)))
    ids = {}
    for name in config.users:
        ids[name] = ice_client.register_user(name, cert_hash=f"{name}-hash")

    plan = reconcile(ice_client, config, dry_run=False)
    assert not plan.failed, [c.error for c in plan.failed]

    enforcer = Enforcer(ice_client)
    desired = build_desired_state(config, ice_client.get_user_ids(list(config.users)))
    enforcer.load(desired, ice_client.get_channels())
    enforcer.refresh_membership()
    return enforcer, ids


def _channel(client, path: str) -> int:
    from intercom.runtime import build_paths

    return build_paths(client.get_channels())[path]


def test_priority_speaker_wird_gesetzt(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )

    user = ice_client.get_state(session)
    assert user.priority_speaker is False

    deviations = enforcer.enforce_user(user)
    assert len(deviations) >= 1
    priority = [d for d in deviations if d.kind == "priority_speaker"]
    assert priority and priority[0].corrected
    assert ice_client.get_state(session).priority_speaker is True


def test_priority_speaker_wird_wiederhergestellt(ice_client, fake_murmur, armed):
    """Der eigentliche Punkt: manche Clients setzen das Flag beim Verbinden zurueck."""
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )
    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is True

    # Client dreht es zurueck.
    ice_client.set_user_state(session, priority_speaker=False)
    assert ice_client.get_state(session).priority_speaker is False

    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is True


def test_kein_priority_ohne_gruppenmitgliedschaft(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user(
        "kam-1", userid=ids["kam-1"], channel=regie
    )
    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is False


def test_kein_priority_in_einem_kanal_ohne_anspruch(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    kameras = _channel(ice_client, "Intercom/Kameras")
    session = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=kameras
    )
    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is False


def test_ein_von_hand_gesetztes_flag_wird_nicht_weggenommen(ice_client, fake_murmur, armed):
    """Ein Automatismus, der eine bewusste Entscheidung zurueckdreht, waere
    im Betrieb schlimmer als ein Flag zuviel."""
    enforcer, ids = armed
    kameras = _channel(ice_client, "Intercom/Kameras")
    session = fake_murmur.server.connect_user(
        "kam-1", userid=ids["kam-1"], channel=kameras
    )
    ice_client.set_user_state(session, priority_speaker=True)

    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is True


def test_listener_werden_gesetzt(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    kameras = _channel(ice_client, "Intercom/Kameras")
    zeitnahme = _channel(ice_client, "Intercom/Zeitnahme")
    session = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )

    assert ice_client.get_listening_channels(session) == []
    deviations = enforcer.enforce_user(ice_client.get_state(session))
    listener = [d for d in deviations if d.kind == "listener"]
    assert listener and listener[0].corrected
    assert sorted(ice_client.get_listening_channels(session)) == sorted([kameras, zeitnahme])


def test_listener_ueberleben_reconnect_nicht_und_werden_neu_gesetzt(
    ice_client, fake_murmur, armed
):
    """Die dokumentierte Einschraenkung -- und ihre Abhilfe."""
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    first = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )
    enforcer.enforce_user(ice_client.get_state(first))
    assert ice_client.get_listening_channels(first)

    fake_murmur.server.disconnect_user(first)
    second = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )
    assert ice_client.get_listening_channels(second) == [], "Listener haetten ueberlebt"

    enforcer.enforce_user(ice_client.get_state(second))
    assert len(ice_client.get_listening_channels(second)) == 2


def test_listener_nur_fuer_speak_gruppen(ice_client, fake_murmur, armed):
    """kam-1 darf in Regie nicht sprechen und bekommt darum keine Listener."""
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user(
        "kam-1", userid=ids["kam-1"], channel=regie
    )
    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_listening_channels(session) == []


def test_enforce_all_sammelt_abweichungen(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    fake_murmur.server.connect_user("regie-1", userid=ids["regie-1"], channel=regie)

    deviations = enforcer.enforce_all(ice_client.get_users().values())
    kinds = {d.kind for d in deviations}
    assert kinds == {"priority_speaker", "listener"}
    assert enforcer.deviations == deviations

    # Zweiter Durchlauf: alles sitzt, keine Abweichung mehr.
    assert enforcer.enforce_all(ice_client.get_users().values()) == []


def test_soll_anzeige_fuer_das_detailpanel(ice_client, fake_murmur, armed):
    enforcer, ids = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user(
        "regie-1", userid=ids["regie-1"], channel=regie
    )
    user = ice_client.get_state(session)
    assert enforcer.expects_priority(user) is True
    assert len(enforcer.expected_listeners(user)) == 2


def test_unregistrierter_gast_bekommt_nichts(ice_client, fake_murmur, armed):
    enforcer, _ = armed
    regie = _channel(ice_client, "Intercom/Regie")
    session = fake_murmur.server.connect_user("gast", userid=-1, channel=regie)
    enforcer.enforce_user(ice_client.get_state(session))
    assert ice_client.get_state(session).priority_speaker is False
    assert ice_client.get_listening_channels(session) == []


def test_provision_on_start_wird_nachgeholt(fake_murmur):
    """murmur ist beim Start oft noch nicht da -- das darf nichts verschlucken.

    Im Compose starten beide Container gleichzeitig; bis murmur die
    Ice-Schnittstelle geoeffnet hat, vergehen Sekunden. Der erste
    Verbindungsversuch scheitert also regelmaessig. Haenge man
    ``PROVISION_ON_START`` an genau diesen Versuch, wird im Normalfall nie
    provisioniert -- und niemand merkt es.
    """
    import asyncio

    from intercom.ice.errors import IceConnectionLost
    from intercom.web.context import AppContext

    ctx = AppContext(fake_murmur.settings(provision_on_start=True))
    # Nur "nicht None" ist hier relevant; provision() ist ersetzt.
    ctx.config = object()  # type: ignore[assignment]

    erreichbar = False
    provisioniert: list[str] = []
    scharf: list[int] = []

    async def fake_run(func, *args, **kwargs):
        if not erreichbar:
            raise IceConnectionLost("murmur ist noch nicht oben")
        return None

    async def fake_refresh(full=False):
        return None

    async def fake_provision(dry_run=False, actor="") -> None:
        provisioniert.append(actor)

    async def fake_arm() -> None:
        scharf.append(1)

    ctx.ice.run = fake_run  # type: ignore[method-assign]
    ctx._refresh = fake_refresh  # type: ignore[method-assign]
    ctx.provision = fake_provision  # type: ignore[method-assign]
    ctx._arm_enforcer = fake_arm  # type: ignore[method-assign]

    async def ablauf() -> None:
        nonlocal erreichbar
        assert await ctx._try_connect(first=True) is False
        assert provisioniert == [], "ohne Verbindung darf nichts laufen"

        erreichbar = True
        assert await ctx._try_connect() is True

    asyncio.run(ablauf())

    assert provisioniert == ["start"], "PROVISION_ON_START wurde verschluckt"


def test_provision_on_start_laeuft_nur_einmal(fake_murmur):
    """Ein Reconnect im Betrieb darf nicht jedes Mal neu provisionieren."""
    import asyncio

    from intercom.web.context import AppContext

    ctx = AppContext(fake_murmur.settings(provision_on_start=True))
    ctx.config = object()  # type: ignore[assignment]

    provisioniert: list[str] = []

    async def fake_run(func, *args, **kwargs):
        return None

    async def fake_refresh(full=False):
        return None

    async def fake_provision(dry_run=False, actor="") -> None:
        provisioniert.append(actor)

    async def fake_arm() -> None:
        return None

    ctx.ice.run = fake_run  # type: ignore[method-assign]
    ctx._refresh = fake_refresh  # type: ignore[method-assign]
    ctx.provision = fake_provision  # type: ignore[method-assign]
    ctx._arm_enforcer = fake_arm  # type: ignore[method-assign]

    async def ablauf() -> None:
        await ctx._try_connect(first=True)
        await ctx._try_connect()
        await ctx._try_connect()

    asyncio.run(ablauf())

    assert provisioniert == ["start"]


def test_provision_on_start_meldet_kaputte_konfiguration(fake_murmur):
    """Ohne brauchbare intercom.yaml wird nichts angelegt -- aber sichtbar."""
    import asyncio

    from intercom.web.context import AppContext

    ctx = AppContext(fake_murmur.settings(provision_on_start=True))
    ctx.config = None
    ctx.config_error = "Zeile 12: kaputt"

    async def fake_run(func, *args, **kwargs):
        return None

    async def fake_refresh(full=False):
        return None

    async def fake_arm() -> None:
        return None

    ctx.ice.run = fake_run  # type: ignore[method-assign]
    ctx._refresh = fake_refresh  # type: ignore[method-assign]
    ctx._arm_enforcer = fake_arm  # type: ignore[method-assign]

    asyncio.run(ctx._try_connect(first=True))

    assert any("PROVISION_ON_START" in b for b in ctx.banners)
