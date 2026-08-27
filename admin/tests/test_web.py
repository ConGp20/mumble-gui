"""Web-Schicht von aussen: Anmeldung, Rechte, CSRF, API, SSE.

Die Anwendung laeuft dabei vollstaendig -- inklusive Lebenszyklus, Ice-Verbindung
zum murmur-Doppel, SQLite und Hintergrundaufgaben. Nur der Mumble-Server ist ein
Doppel, alles andere ist echt.
"""

from __future__ import annotations

import pytest

from tests.conftest import needs_ice

pytestmark = needs_ice


@pytest.fixture()
def app_client(fake_murmur, tmp_path):
    """Gestartete Anwendung mit TestClient."""
    from fastapi.testclient import TestClient

    from intercom.web.app import create_app

    config = tmp_path / "intercom.yaml"
    config.write_text(
        """
version: 1
groups: [regie, kamera]
channels:
  - name: Intercom
    children:
      - name: Regie
        speak: [regie]
        listen_for: [regie]
        priority: [regie]
      - name: Kameras
        speak: [kamera, regie]
        listen_for: [kamera, regie]
policies:
  guests_listen_only: true
users:
  regie-1: { groups: [regie], channel: "Intercom/Regie" }
networks:
  - name: "Kabel Regie"
    cidr: "10.20.10.0/24"
""",
        encoding="utf-8",
    )

    settings = fake_murmur.settings(
        intercom_config=config,
        data_dir=tmp_path,
        provision_on_start=True,
        admin_user="admin",
        admin_password="geheim",
        readonly_user="viewer",
        readonly_password="lesen",
        poll_interval_ms=250,
    )
    with TestClient(create_app(settings)) as client:
        yield client, fake_murmur


def _anmelden(client, benutzer="admin", passwort="geheim"):
    antwort = client.post(
        "/login",
        data={"benutzer": benutzer, "passwort": passwort},
        follow_redirects=False,
    )
    return antwort


def _csrf(client) -> str:
    return client.get("/api/me").json()["csrf"]


def _warte_auf_client(client, name: str, sekunden: float = 5.0) -> None:
    """Wartet, bis ein Client im Zustand auftaucht.

    Ein ``userConnected`` kommt aus einem Ice-Thread und wird per
    ``call_soon_threadsafe`` in den Loop der Anwendung gehoben. Der Test laeuft
    in einem anderen Thread und muss diesen Weg abwarten, statt ihn
    vorauszusetzen.
    """
    import time

    frist = time.monotonic() + sekunden
    while time.monotonic() < frist:
        daten = client.get("/api/state").json()
        if any(u["name"] == name for u in daten["users"]):
            return
        time.sleep(0.05)
    raise AssertionError(f"Client {name!r} ist nicht im Zustand aufgetaucht.")


# --------------------------------------------------------------------------- #
#  Anmeldung und Rechte
# --------------------------------------------------------------------------- #


def test_ohne_anmeldung_zur_anmeldeseite(app_client):
    """Seitenaufrufe leiten um, API-Aufrufe antworten mit 401-JSON."""
    client, _ = app_client
    seite = client.get("/", follow_redirects=False)
    assert seite.status_code == 303
    assert seite.headers["location"] == "/login"

    api = client.get("/api/state")
    assert api.status_code == 401
    assert api.json()["detail"]


def test_healthz_und_metrics_brauchen_keine_anmeldung(app_client):
    """Der Docker-Healthcheck und Prometheus koennen sich nicht anmelden."""
    client, _ = app_client
    gesundheit = client.get("/healthz")
    assert gesundheit.status_code == 200
    assert gesundheit.json()["ice"]["connected"] is True

    metriken = client.get("/metrics")
    assert metriken.status_code == 200
    assert "intercom_up 1" in metriken.text
    assert "intercom_clients" in metriken.text


def test_falsches_passwort(app_client):
    client, _ = app_client
    antwort = _anmelden(client, passwort="falsch")
    assert antwort.status_code == 401
    assert "stimmt nicht" in antwort.text


def test_anmeldung_und_cockpit(app_client):
    client, _ = app_client
    assert _anmelden(client).status_code == 303
    seite = client.get("/")
    assert seite.status_code == 200
    assert "Cockpit" in seite.text
    assert "Stadion-Intercom" in seite.text


def test_secrets_stehen_nicht_im_html(app_client):
    """Das Ice-Secret darf nirgends im Frontend landen."""
    client, fake = app_client
    _anmelden(client)
    for pfad in ("/", "/kanaele", "/acl", "/nutzer", "/server", "/provisioning", "/audit"):
        text = client.get(pfad).text
        assert fake.secret not in text, f"Secret steht in {pfad}"
        assert "geheim" not in text, f"Admin-Passwort steht in {pfad}"
    assert fake.secret not in client.get("/healthz").text


def test_nur_lese_konto_darf_nicht_schreiben(app_client):
    client, _ = app_client
    _anmelden(client, "viewer", "lesen")
    assert client.get("/api/state").status_code == 200

    csrf = _csrf(client)
    antwort = client.post(
        "/api/channels",
        json={"name": "Verboten", "parent": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert antwort.status_code == 403
    assert "nur lesen" in antwort.json()["detail"]


def test_ohne_csrf_token_kein_schreiben(app_client):
    client, _ = app_client
    _anmelden(client)
    antwort = client.post("/api/channels", json={"name": "Ohne", "parent": 0})
    assert antwort.status_code == 403
    assert "CSRF" in antwort.json()["detail"]


def test_falsches_csrf_token_wird_abgewiesen(app_client):
    client, _ = app_client
    _anmelden(client)
    antwort = client.post(
        "/api/channels",
        json={"name": "Ohne", "parent": 0},
        headers={"X-CSRF-Token": "erfunden"},
    )
    assert antwort.status_code == 403


# --------------------------------------------------------------------------- #
#  Provisioning beim Start
# --------------------------------------------------------------------------- #


def test_provision_on_start_hat_gewirkt(app_client):
    client, fake = app_client
    _anmelden(client)
    namen = {c.name for c in fake.server.channels.values()}
    assert {"Intercom", "Regie", "Kameras"} <= namen

    gesundheit = client.get("/healthz").json()
    assert gesundheit["provision"]["last_at"] is not None
    assert gesundheit["provision"]["failed"] == 0


def test_zweiter_plan_ist_leer(app_client):
    """Nach dem Start-Provisioning gibt es nichts mehr zu tun."""
    client, _ = app_client
    _anmelden(client)
    plan = client.post("/api/provision/plan", headers={"X-CSRF-Token": _csrf(client)})
    assert plan.status_code == 200
    assert plan.json()["empty"] is True, plan.json()["changes"]


def test_export_liefert_yaml(app_client):
    import yaml

    client, _ = app_client
    _anmelden(client)
    antwort = client.get("/api/provision/export")
    assert antwort.status_code == 200
    daten = yaml.safe_load(antwort.text)
    assert any(c["name"] == "Intercom" for c in daten["channels"])


# --------------------------------------------------------------------------- #
#  Zustand und Clients
# --------------------------------------------------------------------------- #


def test_state_enthaelt_clients_und_segmente(app_client):
    client, fake = app_client
    _anmelden(client)
    fake.server.connect_user("regie-1", userid=1, address="10.20.10.7")
    fake.server.connect_user("gast", userid=-1, address="192.168.9.9")

    daten = client.get("/api/state").json()
    namen = {u["name"] for u in daten["users"]}
    assert namen == {"regie-1", "gast"}

    segmente = {s["segment"] for s in daten["segments"]}
    assert "Kabel Regie" in segmente     # 10.20.10.7 passt ins CIDR
    assert "sonstige" in segmente        # 192.168.9.9 passt nirgends

    assert daten["channels"], "Kanalbaum fehlt"
    assert daten["counts"]["users"] == 2


def test_detail_eines_clients(app_client):
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("regie-1", userid=1, address="10.20.10.7")

    daten = client.get(f"/api/users/{session}").json()
    assert daten["user"]["name"] == "regie-1"
    assert daten["user"]["segment"] == "Kabel Regie"
    assert "certificates" in daten
    assert "expected_listeners" in daten


def test_client_verschieben_wird_auditiert(app_client):
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("regie-1", userid=1)
    ziel = next(c.id for c in fake.server.channels.values() if c.name == "Kameras")

    csrf = _csrf(client)
    antwort = client.post(
        f"/api/users/{session}/action",
        json={"action": "move", "channel": ziel},
        headers={"X-CSRF-Token": csrf},
    )
    assert antwort.status_code == 200
    assert fake.server.users[session].channel == ziel

    audit = client.get("/api/audit?action=user.move").json()
    assert audit["total"] >= 1
    assert audit["entries"][0]["actor"] == "admin"
    assert "regie-1" in audit["entries"][0]["target"]


def test_kanal_anlegen_und_loeschen(app_client):
    client, fake = app_client
    _anmelden(client)
    csrf = _csrf(client)

    neu = client.post(
        "/api/channels",
        json={"name": "Testkanal", "parent": 0, "description": "nur ein Test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert neu.status_code == 200
    kanal_id = neu.json()["id"]
    assert fake.server.channels[kanal_id].description == "nur ein Test"

    weg = client.request(
        "DELETE", f"/api/channels/{kanal_id}", headers={"X-CSRF-Token": csrf}
    )
    assert weg.status_code == 200
    assert kanal_id not in fake.server.channels


# --------------------------------------------------------------------------- #
#  ACL-Editor
# --------------------------------------------------------------------------- #


def test_acl_lesen_und_vorschau(app_client):
    client, fake = app_client
    _anmelden(client)
    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")

    acl = client.get(f"/api/channels/{regie}/acl").json()
    assert acl["channel_name"].endswith("Regie")
    assert any(t["key"] == "nur-hoeren" for t in acl["templates"])

    vorschau = client.post(
        f"/api/channels/{regie}/acl/preview",
        json={
            "acls": [
                {"group": "all", "apply_here": True, "apply_subs": False,
                 "allow": ["Traverse"], "deny": ["Speak"]}
            ],
            "groups": [],
            "inherit": True,
        },
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert vorschau.status_code == 200
    assert vorschau.json()["diff"]


def test_acl_vorschau_warnt_vor_root_only_rechten(app_client):
    """Kick an einem Unterkanal ist wirkungslos -- das muss dastehen."""
    client, fake = app_client
    _anmelden(client)
    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")

    antwort = client.post(
        f"/api/channels/{regie}/acl/preview",
        json={
            "acls": [{"group": "regie", "apply_here": True, "allow": ["Kick"], "deny": []}],
            "groups": [],
            "inherit": True,
        },
        headers={"X-CSRF-Token": _csrf(client)},
    )
    warnungen = " ".join(antwort.json()["warnings"])
    assert "Wurzelkanal" in warnungen


def test_acl_lehnt_wirkungslose_eintraege_ab(app_client):
    client, fake = app_client
    _anmelden(client)
    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")

    antwort = client.put(
        f"/api/channels/{regie}/acl",
        json={
            "acls": [{"group": "regie", "apply_here": False, "apply_subs": False,
                      "allow": ["Speak"], "deny": []}],
            "groups": [],
            "inherit": True,
        },
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert antwort.status_code == 400
    assert "weder hier noch" in antwort.json()["detail"]


def test_acl_vorlage(app_client):
    client, fake = app_client
    _anmelden(client)
    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")

    antwort = client.post(
        f"/api/channels/{regie}/acl/template",
        json={"key": "nur-hoeren", "groups": ["regie"]},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    acls = antwort.json()["acls"]
    alle = next(a for a in acls if a["group"] == "all")
    assert "Speak" in alle["deny"]
    assert "Listen" in alle["allow"]


def test_effektive_rechte(app_client):
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("regie-1", userid=1)
    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")

    antwort = client.get(f"/api/channels/{regie}/effective?session={session}")
    assert antwort.status_code == 200
    namen = {p["name"] for p in antwort.json()["permissions"]}
    assert "Speak" in namen


def test_permissions_tabelle_kennzeichnet_listen(app_client):
    """Listen fehlt in der Slice und muss im GUI erkennbar bleiben."""
    client, _ = app_client
    _anmelden(client)
    rechte = client.get("/api/permissions").json()
    listen = next(p for p in rechte if p["name"] == "Listen")
    assert listen["bit"] == 0x800
    assert listen["in_slice"] is False
    kick = next(p for p in rechte if p["name"] == "Kick")
    assert kick["root_only"] is True


# --------------------------------------------------------------------------- #
#  Nutzer, Bans, Konfiguration
# --------------------------------------------------------------------------- #


def test_registrierte_nutzer_und_gruppenmatrix(app_client):
    client, _fake = app_client
    _anmelden(client)
    csrf = _csrf(client)

    angelegt = client.post(
        "/api/registered",
        json={"name": "kam-9", "cert_hash": "b" * 40},
        headers={"X-CSRF-Token": csrf},
    )
    assert angelegt.status_code == 200
    userid = angelegt.json()["userid"]

    gespeichert = client.put(
        "/api/registered/groups",
        json={"groups": {"kamera": [userid]}},
        headers={"X-CSRF-Token": csrf},
    )
    assert gespeichert.status_code == 200

    liste = client.get("/api/registered").json()
    eintrag = next(u for u in liste["users"] if u["userid"] == userid)
    assert "kamera" in eintrag["groups"]
    # Die andere verwaltete Gruppe darf dabei nicht verschwinden.
    assert "regie" in liste["groups"]


def test_registrierung_ohne_passwort_und_hash_wird_abgelehnt(app_client):
    client, _ = app_client
    _anmelden(client)
    antwort = client.post(
        "/api/registered", json={"name": "leer"}, headers={"X-CSRF-Token": _csrf(client)}
    )
    assert antwort.status_code == 400
    assert "anmelden" in antwort.json()["detail"]


def test_bans_umlauf(app_client):
    client, _ = app_client
    _anmelden(client)
    csrf = _csrf(client)

    client.put(
        "/api/bans",
        json=[{"address": "10.20.30.99", "bits": 32, "reason": "Test"}],
        headers={"X-CSRF-Token": csrf},
    )
    bans = client.get("/api/bans").json()
    assert len(bans) == 1
    assert bans[0]["address"] == "10.20.30.99"


def test_conf_zeigt_neustart_pflicht(app_client):
    client, _ = app_client
    _anmelden(client)
    daten = client.get("/api/conf").json()
    schluessel = {z["key"]: z for z in daten["rows"]}
    assert schluessel["port"]["restart_required"] is True
    assert schluessel["welcometext"]["restart_required"] is False


def test_ice_secret_kann_nicht_ueber_conf_geaendert_werden(app_client):
    """Das waere ein Fusstritt: danach ist die eigene Verbindung tot."""
    client, _ = app_client
    _anmelden(client)
    antwort = client.put(
        "/api/conf",
        json={"key": "icesecretwrite", "value": "neu"},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert antwort.status_code == 400
    assert ".env" in antwort.json()["detail"]


def test_serverlog(app_client):
    client, fake = app_client
    _anmelden(client)
    fake.server.connect_user("regie-1", userid=1)

    daten = client.get("/api/log?first=0&count=50").json()
    assert daten["total"] >= 1
    assert any("regie-1" in e["text"] for e in daten["entries"])

    gefiltert = client.get("/api/log?first=0&count=50&pattern=regie").json()
    assert all("regie" in e["text"].lower() for e in gefiltert["entries"])

    kaputt = client.get("/api/log?pattern=%5B")
    assert kaputt.status_code == 400


def test_log_download(app_client):
    client, fake = app_client
    _anmelden(client)
    fake.server.connect_user("regie-1", userid=1)
    antwort = client.get("/api/log/download")
    assert antwort.status_code == 200
    assert "attachment" in antwort.headers["content-disposition"]


# --------------------------------------------------------------------------- #
#  Live-Strom
# --------------------------------------------------------------------------- #


def test_sse_endpunkt_setzt_die_richtigen_koepfe(app_client):
    """Der Live-Strom wird direkt gepruft, nicht ueber den TestClient.

    Starlettes TestClient wartet, bis eine Antwort vollstaendig ist. Ein
    SSE-Strom endet nie von allein, ein ``client.stream("/api/events")`` wuerde
    also fuer immer haengen -- eine Eigenheit des Testwerkzeugs, kein Fehler der
    Anwendung. Deshalb wird die Route hier direkt aufgerufen und nur der erste
    Rahmen abgeholt.
    """
    import asyncio
    import json

    from intercom.web.api import events

    client, fake = app_client
    _anmelden(client)
    fake.server.connect_user("regie-1", userid=1)
    _warte_auf_client(client, "regie-1")
    kontext = client.app.state.ctx

    class _Request:
        """Das Minimum, das die Route anfasst."""

        def __init__(self, app):
            self.app = app

        async def is_disconnected(self):
            return False

    async def hole_ersten_rahmen():
        antwort = await events(_Request(client.app), account=None)
        assert antwort.media_type == "text/event-stream"
        # Ohne diesen Kopf puffert der Synology-Reverse-Proxy den Strom und im
        # Browser kommt minutenlang nichts an.
        assert antwort.headers["x-accel-buffering"] == "no"
        assert "no-cache" in antwort.headers["cache-control"]

        strom = antwort.body_iterator
        try:
            return await asyncio.wait_for(strom.__anext__(), timeout=5.0)
        finally:
            await strom.aclose()

    rahmen = asyncio.run(hole_ersten_rahmen())
    assert rahmen.startswith("event: state\n")
    nutzlast = json.loads(rahmen.split("data: ", 1)[1].strip())
    assert any(u["name"] == "regie-1" for u in nutzlast["users"])
    assert kontext.live.hub.subscriber_count == 0, "Abonnent wurde nicht abgeraeumt"


def test_event_hub_verteilt_und_wirft_alte_rahmen_weg():
    """Ein eingefrorener Browser darf das Cockpit nicht anhalten.

    Die Warteschlange je Abonnent ist begrenzt; laeuft sie voll, fliegt der
    AELTESTE Rahmen raus. Das ist unbedenklich, weil jeder Rahmen den
    vollstaendigen Zustand traegt und nicht nur eine Differenz.
    """
    import asyncio

    from intercom.web.state import EventHub

    async def lauf():
        hub = EventHub(queue_size=3)
        assert hub.subscriber_count == 0

        strom = hub.subscribe()
        erster = await strom.__anext__()
        assert erster.startswith(": ")          # Kommentarrahmen zur Begruessung
        assert hub.subscriber_count == 1

        for i in range(6):
            hub.publish("state", {"n": i})

        gesehen = []
        for _ in range(3):
            gesehen.append(await asyncio.wait_for(strom.__anext__(), timeout=2.0))
        await strom.aclose()

        assert hub.dropped == 3, "es haetten drei Rahmen entfallen muessen"
        # Uebrig bleiben die JUENGSTEN drei.
        assert '"n": 3' in gesehen[0]
        assert '"n": 5' in gesehen[2]
        assert hub.subscriber_count == 0

    asyncio.run(lauf())


def test_event_hub_ohne_abonnenten_serialisiert_nichts():
    """Ohne offenes Cockpit soll das Polling keine Arbeit verschwenden."""
    import asyncio

    from intercom.web.state import EventHub

    class Unsserialisierbar:
        def __repr__(self):
            raise AssertionError("haette nicht serialisiert werden duerfen")

    async def lauf():
        hub = EventHub()
        hub.publish("state", {"x": Unsserialisierbar()})   # darf nicht werfen
        assert hub.dropped == 0

    asyncio.run(lauf())


# --------------------------------------------------------------------------- #
#  Absicherung der Routen
# --------------------------------------------------------------------------- #

#: POST-Routen, die absichtlich nichts veraendern und darum dem Nur-Lese-Konto
#: offenstehen. Sie benutzen POST nur, weil sie einen Rumpf entgegennehmen.
#: Wer hier etwas eintraegt, muss belegen koennen, dass die Route wirklich
#: nichts schreibt.
LESENDE_POST_ROUTEN = {
    "/api/channels/{channel_id}/acl/preview",   # rechnet nur den Diff aus
    "/api/channels/{channel_id}/acl/template",  # reine Funktion, kein Serverzugriff
    "/api/provision/plan",                      # Trockenlauf, dry_run=True
}


def test_jede_schreibende_route_verlangt_admin():
    """Wache gegen die stille Luecke.

    Eine neue Route, bei der jemand ``require_admin`` vergisst, waere im Betrieb
    nicht zu bemerken: der Nur-Lese-Zugang koennte sie benutzen, und niemand
    wuerde es sehen. Dieser Test geht alle Routen durch, statt sich auf
    Aufmerksamkeit beim Nachlesen zu verlassen.
    """
    from intercom.web.api import router

    ungeschuetzt: list[tuple[str, str, str]] = []
    for route in router.routes:
        methoden = set(getattr(route, "methods", []) or [])
        wachen = {
            getattr(abhaengigkeit.call, "__name__", "")
            for abhaengigkeit in route.dependant.dependencies
        }
        wache = (
            "require_admin"
            if "require_admin" in wachen
            else ("require_user" if "require_user" in wachen else "keine")
        )

        veraendernd = bool(methoden & {"POST", "PUT", "PATCH", "DELETE"})
        if veraendernd and route.path in LESENDE_POST_ROUTEN:
            veraendernd = False

        erwartet = "require_admin" if veraendernd else "require_user"
        if wache != erwartet and not (wache == "require_admin" and not veraendernd):
            ungeschuetzt.append((",".join(sorted(methoden)), route.path, wache))

    assert not ungeschuetzt, "Routen ohne passende Wache: " + repr(ungeschuetzt)


def test_lesende_post_routen_schreiben_wirklich_nicht(app_client):
    """Belegt die Ausnahmeliste oben, statt sie zu behaupten."""
    client, fake = app_client
    _anmelden(client, "viewer", "lesen")
    csrf = _csrf(client)

    vorher = {
        "kanaele": {c.id: (c.name, c.description, c.position) for c in fake.server.channels.values()},
        "acls": {c.id: [(a.group, a.allow, a.deny) for a in c.acls] for c in fake.server.channels.values()},
        "conf": dict(fake.server.conf),
        "registriert": dict(fake.server.registered),
    }

    regie = next(c.id for c in fake.server.channels.values() if c.name == "Regie")
    assert client.post(
        f"/api/channels/{regie}/acl/preview",
        json={"acls": [], "groups": [], "inherit": True},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.post(
        f"/api/channels/{regie}/acl/template",
        json={"key": "ring", "groups": ["regie"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.post(
        "/api/provision/plan", headers={"X-CSRF-Token": csrf}
    ).status_code == 200

    nachher = {
        "kanaele": {c.id: (c.name, c.description, c.position) for c in fake.server.channels.values()},
        "acls": {c.id: [(a.group, a.allow, a.deny) for a in c.acls] for c in fake.server.channels.values()},
        "conf": dict(fake.server.conf),
        "registriert": dict(fake.server.registered),
    }
    assert nachher == vorher, "eine als lesend gefuehrte Route hat geschrieben"


# --------------------------------------------------------------------------- #
#  Serverneustart im laufenden Betrieb
# --------------------------------------------------------------------------- #


def test_ueberlebt_einen_serverneustart_und_verbindet_neu(tmp_path):
    """Der wahrscheinlichste Zwischenfall ueberhaupt.

    Faellt murmur weg -- Containerneustart, kurzer Netzaussetzer --, muss das
    Cockpit stehen bleiben und sich von selbst wieder fangen. Insbesondere darf
    ``/healthz`` weiter 200 liefern: der Docker-Healthcheck haengt daran, und
    ein Admin-Container, der wegen eines fremden Dienstes neu startet, wuerde
    mit murmur um die Wette kreisen.

    Ebenso wichtig ist, was **nicht** passiert: nach dem Wiederverbinden wird
    nicht erneut provisioniert. ``PROVISION_ON_START`` heisst Containerstart,
    nicht Serverneustart. Ein Netzaussetzer waehrend des Wettkampfs darf nicht
    dazu fuehren, dass die ACLs neu geschrieben werden und dabei eine bewusste
    Aenderung von vor fuenf Minuten verlorengeht.
    """
    import socket
    import time

    from fastapi.testclient import TestClient

    from intercom.web.app import create_app
    from tests.fake_murmur import FakeMurmur

    sonde = socket.socket()
    sonde.bind(("127.0.0.1", 0))
    port = sonde.getsockname()[1]
    sonde.close()

    config = tmp_path / "intercom.yaml"
    config.write_text(
        "version: 1\ngroups: [regie]\nchannels:\n  - name: Intercom\n", encoding="utf-8"
    )

    erster = FakeMurmur(port=port)
    erster.start()
    try:
        settings = erster.settings(
            intercom_config=config,
            data_dir=tmp_path,
            provision_on_start=True,
            admin_password="geheim",
            poll_interval_ms=250,
        )
        with TestClient(create_app(settings)) as client:
            _anmelden(client)
            assert client.get("/healthz").json()["ice"]["connected"] is True
            assert any(c.name == "Intercom" for c in erster.server.channels.values())

            # --- murmur faellt weg -------------------------------------------
            erster.stop()
            frist = time.monotonic() + 10
            while time.monotonic() < frist:
                if not client.get("/healthz").json()["ice"]["connected"]:
                    break
                time.sleep(0.1)

            gesundheit = client.get("/healthz")
            assert gesundheit.status_code == 200, "Healthcheck darf nicht kippen"
            assert gesundheit.json()["ice"]["connected"] is False
            assert "mumble-server" in gesundheit.json()["ice"]["error"]
            # Das Cockpit bleibt bedienbar und zeigt den letzten bekannten Stand.
            assert client.get("/").status_code == 200
            assert client.get("/api/state").status_code == 200

            # --- murmur kommt zurueck, mit leerer Datenbank -------------------
            zweiter = FakeMurmur(port=port)
            zweiter.start()
            try:
                frist = time.monotonic() + 20
                while time.monotonic() < frist:
                    if client.get("/healthz").json()["ice"]["connected"]:
                        break
                    time.sleep(0.2)
                assert client.get("/healthz").json()["ice"]["connected"] is True, (
                    "Wiederverbinden ist gescheitert"
                )

                # Nicht erneut provisioniert: der frische Server hat nur die Wurzel.
                assert set(zweiter.server.channels) == {0}, (
                    "nach dem Wiederverbinden wurde ungefragt provisioniert"
                )
                # Aber der Plan zeigt die Abweichung sofort an -- eine Klick, und
                # der Betreiber holt sie zurueck.
                plan = client.post(
                    "/api/provision/plan", headers={"X-CSRF-Token": _csrf(client)}
                ).json()
                assert plan["empty"] is False
                assert any(c["kind"] == "channel_create" for c in plan["changes"])
            finally:
                zweiter.stop()
    finally:
        erster.stop()


# --------------------------------------------------------------------------- #
#  Verlustmessung: Intervall statt Sitzungsmittel
# --------------------------------------------------------------------------- #


def _stats(session: int, ts: float, gut: int, verloren: int):
    """Eine UserStats-Momentaufnahme mit kumulativen Zaehlern."""
    from intercom.monitor.stats import UserStatsSample

    return UserStatsSample(
        ts=ts,
        session=session,
        name="kam-1",
        from_client_good=gut,
        from_client_lost=verloren,
        udp_ping_avg_ms=20.0,
        udp_packets=gut,
    )


def test_verlust_wird_als_intervallrate_gemeldet_nicht_als_sitzungsmittel(app_client):
    """Der Fall, um den es geht.

    Die Paketzaehler in ``UserStats`` sind kumulativ seit Verbindungsbeginn.
    Wer sie direkt anzeigt, zeigt den Mittelwert der ganzen Sitzung: nach zwei
    Stunden sauberem Betrieb verschwindet ein akuter Ausfall darin restlos --
    zwei Drittel der Sprache weg, und die Alarmschwelle bleibt still.
    """
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("kam-1", userid=1, address="10.20.10.7")
    _warte_auf_client(client, "kam-1")

    kontext = client.app.state.ctx
    from intercom.monitor.stats import LossTracker

    kontext._loss = LossTracker()

    # Zwei Stunden sauber: 360 000 Pakete, 20 verloren.
    kontext._handle_stats(_stats(session, 0.0, 360_000, 20))
    # Naechstes Intervall: 300 Pakete erwartet, 200 davon verloren.
    kontext._handle_stats(_stats(session, 5.0, 360_100, 220))

    zeile = next(r for r in kontext.live.user_rows() if r["session"] == session)
    assert zeile["loss_pct"] is not None
    assert zeile["loss_pct"] > 50, (
        f"Es wird der Sitzungsmittelwert angezeigt ({zeile['loss_pct']} %) "
        "statt der Rate im Intervall."
    )
    stufen = [a.level for a in kontext.live.alarms() if a.kind == "verlust"]
    assert "kritisch" in stufen, "ein Ausfall dieser Groesse muss alarmieren"


def test_erster_messwert_erzeugt_noch_keine_rate(app_client):
    """Ohne Vorgaenger gibt es keine Differenz -- und darum keine Falschmeldung."""
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("kam-1", userid=1)
    _warte_auf_client(client, "kam-1")

    kontext = client.app.state.ctx
    from intercom.monitor.stats import LossTracker

    kontext._loss = LossTracker()
    kontext._handle_stats(_stats(session, 0.0, 100_000, 5_000))

    zeile = next(r for r in kontext.live.user_rows() if r["session"] == session)
    assert zeile["loss_pct"] is None, "der erste Abruf darf keine Rate liefern"
    assert not [a for a in kontext.live.alarms() if a.kind == "verlust"]


def test_alter_messwert_alarmiert_nicht_mehr(app_client):
    """Stirbt der Bot, darf sein letzter Wert nicht unbegrenzt weiteralarmieren.

    Ein Verlust von vor zehn Minuten ist keine Aussage ueber jetzt. Er sieht
    aber aus wie eine Messung -- das ist schlimmer als gar kein Wert.
    """
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("kam-1", userid=1)
    _warte_auf_client(client, "kam-1")

    live = client.app.state.ctx.live
    live.note_stats(session, 40.0, 5.0)
    assert next(r for r in live.user_rows() if r["session"] == session)["loss_pct"] == 40.0
    assert [a for a in live.alarms() if a.kind == "verlust"]

    # Messung kuenstlich altern lassen.
    live.stats_seen[session] -= live.STATS_MAX_AGE_S + 1

    zeile = next(r for r in live.user_rows() if r["session"] == session)
    assert zeile["loss_pct"] is None, "veralteter Wert wird weiter angezeigt"
    assert zeile["jitter_ms"] is None
    assert not [a for a in live.alarms() if a.kind == "verlust"]


def test_bot_verliert_verbindung_und_messwerte_verschwinden(app_client):
    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("kam-1", userid=1)
    _warte_auf_client(client, "kam-1")

    kontext = client.app.state.ctx
    kontext.live.note_stats(session, 40.0, 5.0)
    assert kontext.live.loss

    kontext._handle_monitor_state("wartet")
    assert not kontext.live.loss, "Messwerte haetten verworfen werden muessen"
    assert not [a for a in kontext.live.alarms() if a.kind == "verlust"]


# --------------------------------------------------------------------------- #
#  Anmeldung: Haertung
# --------------------------------------------------------------------------- #


def test_umlaute_in_der_anmeldung_ergeben_401_statt_500(app_client):
    """``hmac.compare_digest`` wirft bei Nicht-ASCII einen TypeError.

    Ungefangen wurde daraus eine 500 -- und weil nur *bestehende* Konten
    ueberhaupt bis zum Passwortvergleich kamen, verriet der Statuscode, welcher
    Benutzername existiert. Ein Tippfehler mit Umlaut genuegte.
    """
    for benutzer, passwort in (
        ("admin", "Käse123"),      # richtiger Name, Umlaut im Passwort
        ("ädmin", "geheim"),       # Umlaut im Namen
        ("viewer", "Käse"),        # zweites Konto
        ("nixda", "Käse"),         # unbekannter Name
        ("ページ", "パスワード"),      # gar kein Latin-1
    ):
        antwort = client_login(app_client, benutzer, passwort)
        assert antwort.status_code == 401, (
            f"{benutzer!r}/{passwort!r} ergab {antwort.status_code} statt 401"
        )


def client_login(app_client, benutzer: str, passwort: str):
    client, _ = app_client
    return client.post(
        "/login",
        data={"benutzer": benutzer, "passwort": passwort},
        follow_redirects=False,
    )


def test_umlaute_im_passwort_funktionieren_trotzdem(fake_murmur, tmp_path):
    """Gekappt werden darf nur der Fehlerfall -- ein Umlaut-Passwort muss gehen."""
    from fastapi.testclient import TestClient

    from intercom.web.app import create_app

    config = tmp_path / "intercom.yaml"
    config.write_text("version: 1\nchannels:\n  - name: Intercom\n", encoding="utf-8")
    settings = fake_murmur.settings(
        intercom_config=config,
        data_dir=tmp_path,
        provision_on_start=False,
        admin_user="tönchef",
        admin_password="Käse-Straße-42",
    )
    with TestClient(create_app(settings)) as client:
        antwort = client.post(
            "/login",
            data={"benutzer": "tönchef", "passwort": "Käse-Straße-42"},
            follow_redirects=False,
        )
        assert antwort.status_code == 303, "Umlaute duerfen die Anmeldung nicht blockieren"


def test_audit_log_laesst_sich_nicht_von_aussen_vollschreiben(app_client):
    """``/login`` schreibt vor jeder Authentisierung eine Audit-Zeile.

    Ohne Laengengrenze konnte jeder ohne Anmeldung die Datenbank fluten -- und
    es ist dieselbe Datei wie der Metrik-Verlauf. Laeuft sie voll, sind
    waehrend der Veranstaltung Verlaufsgrafik und Protokoll tot.
    """
    from intercom.store.db import MAX_KURZFELD

    client, _ = app_client
    riese = "X" * 200_000

    antwort = client_login(app_client, riese, "egal")
    assert antwort.status_code in (401, 429)

    _anmelden(client)
    eintraege = client.get("/api/audit?action=auth.login").json()["entries"]
    assert eintraege, "der Fehlversuch haette protokolliert werden muessen"
    assert all(len(e["actor"]) <= MAX_KURZFELD for e in eintraege), (
        "der Benutzername wurde ungekappt gespeichert"
    )


def test_anmeldebremse_greift_auch_bei_wechselnder_quelladresse(app_client):
    """Die Je-IP-Bremse allein reicht nicht.

    Hinter einem Reverse-Proxy stammt die Quell-IP aus einem Kopf, den der
    Client mitschickt. Wer ihn faelschen kann, haette ohne Gesamtbremse gar
    keine Bremse.
    """
    client, _ = app_client
    kontext = client.app.state.ctx
    sitzungen = kontext.sessions

    for nummer in range(sitzungen.MAX_GESAMT + 5):
        sitzungen.note_failure(f"10.0.0.{nummer % 250}")

    assert sitzungen.blocked_for("10.99.99.99") > 0, (
        "eine bisher unbekannte Adresse muesste jetzt trotzdem gebremst werden"
    )
    # Eine erfolgreiche Anmeldung raeumt beides ab.
    sitzungen.clear_failures("10.99.99.99")
    assert sitzungen.blocked_for("10.99.99.99") == 0


def test_quellentabelle_waechst_nicht_unbegrenzt(app_client):
    client, _ = app_client
    sitzungen = client.app.state.ctx.sessions
    for nummer in range(sitzungen.MAX_QUELLEN + 500):
        sitzungen.note_failure(f"10.{nummer // 65536}.{nummer // 256 % 256}.{nummer % 256}")
    assert len(sitzungen._failures) <= sitzungen.MAX_QUELLEN


def test_vox_verdacht_wird_auch_ohne_monitor_aufgeraeumt(fake_murmur):
    """Das Aufraeumen hing an ``self.loss`` -- die falsche Liste.

    Der VOX-Verdacht entsteht fuer *jeden* Client, der Paketverlust dagegen nur,
    wenn der Monitor-Bot laeuft. Ist er aus (``MONITOR_BOT_ENABLED=false``),
    bleibt ``self.loss`` leer, und mit ihr als Mass wurde nie etwas geloescht.
    murmur vergibt Sitzungsnummern aufsteigend: ein Eintrag pro Verbindung,
    fuer die gesamte Laufzeit des Containers.
    """
    from intercom.ice.types import MumbleUser
    from intercom.web.state import LiveState

    live = LiveState(fake_murmur.settings(monitor_enabled=False))

    def klient(session: int) -> MumbleUser:
        return MumbleUser(
            session=session, userid=session, name=f"kam-{session}", channel=0
        )

    for runde in range(1, 21):
        live.set_users({runde: klient(runde)})
        live.note_activity()

    assert set(live._vox) == {20}, "der VOX-Speicher waechst mit jeder Verbindung"


def test_vox_verdacht_ueberlebt_solange_der_client_verbunden_ist(fake_murmur):
    """Gegenprobe: aufgeraeumt wird nur, was wirklich weg ist."""
    from intercom.ice.types import MumbleUser
    from intercom.web.state import LiveState

    live = LiveState(fake_murmur.settings(monitor_enabled=False))
    bleibt = MumbleUser(session=7, userid=7, name="regie-1", channel=0)
    geht = MumbleUser(session=8, userid=8, name="kam-1", channel=0)

    live.set_users({7: bleibt, 8: geht})
    live.note_activity()
    assert set(live._vox) == {7, 8}

    live.set_users({7: bleibt})
    assert set(live._vox) == {7}


def test_lesende_endpunkte_laufen_im_loop(app_client):
    """Wer ``LiveState`` liest, muss eine Koroutine sein.

    FastAPI schiebt eine synchrone Pfadfunktion in einen Threadpool. Dort
    laeuft sie neben dem asyncio-Loop -- und der Loop traegt gerade Clients
    in ``live.users`` ein oder aus. Eine Schleife darueber aus einem fremden
    Thread endet in "dictionary changed size during iteration", also einem
    500er, und zwar bevorzugt bei Betrieb.
    """
    import inspect

    from intercom.web import api

    for funktion in (api.state, api.provision_reload):
        assert inspect.iscoroutinefunction(funktion), funktion.__name__

    client, _fake = app_client
    for pfad in ("/metrics", "/healthz"):
        route = next(r for r in client.app.routes if getattr(r, "path", "") == pfad)
        assert inspect.iscoroutinefunction(route.endpoint), pfad


def test_metriken_holen_die_laufzeit_nicht_frisch(app_client, monkeypatch):
    """``/metrics`` darf keinen Ice-Aufruf machen.

    Der haengt am selben Threadpool wie alles andere -- ausgerechnet der
    Endpunkt, der eine Ueberlastung melden soll, waere dann der erste, der
    daran haengenbleibt.
    """
    from intercom.web.api import metrics_text

    client, _fake = app_client
    kontext = client.app.state.ctx

    def verboten() -> int:
        raise AssertionError("/metrics hat einen Ice-Aufruf gemacht")

    monkeypatch.setattr(kontext.ice.sync, "get_uptime", verboten)
    kontext._server_uptime = 4711

    text = metrics_text(kontext)
    assert "intercom_server_uptime_seconds 4711" in text


def test_metriken_melden_keinen_alten_verlust(app_client):
    """Ein Wert von vor zehn Minuten waere in Prometheus eine gerade Linie."""
    from intercom.web.api import metrics_text

    client, fake = app_client
    _anmelden(client)
    session = fake.server.connect_user("kam-1", userid=1)
    _warte_auf_client(client, "kam-1")

    live = client.app.state.ctx.live
    live.note_stats(session, 40.0, 5.0)
    assert "intercom_client_loss_percent" in metrics_text(client.app.state.ctx)
    assert "40.0" in metrics_text(client.app.state.ctx)

    live.stats_seen[session] -= live.STATS_MAX_AGE_S + 1
    text = metrics_text(client.app.state.ctx)
    assert not [z for z in text.splitlines() if z.startswith("intercom_client_loss_percent{")]


def test_herunterfahren_blockiert_den_loop_nicht(app_client, monkeypatch):
    """``monitor.stop()`` und ``ice.shutdown()`` warten auf Threads.

    Im Loop ausgefuehrt steht dabei alles -- auch das, was uvicorn noch
    erledigen will. Docker schickt nach zehn Sekunden SIGKILL.
    """
    import asyncio
    import time as zeit

    client, _fake = app_client
    kontext = client.app.state.ctx

    monkeypatch.setattr(kontext.ice, "shutdown", lambda: zeit.sleep(1.0))

    async def messen() -> float:
        laeuft = True
        takte = 0

        async def uhr() -> None:
            nonlocal takte
            while laeuft:
                takte += 1
                await asyncio.sleep(0.02)

        aufgabe = asyncio.create_task(uhr())
        await asyncio.sleep(0.05)
        await kontext._abbau_der_threads()
        laeuft = False
        await aufgabe
        return takte

    takte = asyncio.run(messen())
    # Ohne to_thread stuende die Uhr eine Sekunde lang -- sie kaeme auf die
    # zwei, drei Takte vor dem Abbau.
    assert takte > 20, f"der Loop stand still ({takte} Takte)"


def test_herunterfahren_gibt_nach_der_frist_auf(app_client, monkeypatch):
    """Ein haengender Abbau darf das Herunterfahren nicht aufhalten."""
    import asyncio
    import time as zeit

    from intercom.web import context as context_modul

    client, _fake = app_client
    kontext = client.app.state.ctx

    monkeypatch.setattr(context_modul, "SHUTDOWN_TIMEOUT_S", 0.2)
    monkeypatch.setattr(kontext.ice, "shutdown", lambda: zeit.sleep(5.0))

    begonnen = zeit.monotonic()
    asyncio.run(kontext._abbau_der_threads())
    assert zeit.monotonic() - begonnen < 2.0
