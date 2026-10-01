"""Shows: benannte Aufbauten ablegen, vergleichen, laden.

Eine Show ist dieselbe Datei wie eine Sicherung, nur mit Namen abgelegt.
Geprueft wird vor allem, dass Laden *alles* wiederherstellt -- auch das, was
Mumble selbst nicht kennt (Ruftasten, Verbindungen, feste Plaetze) -- und dass
ein Testlauf nichts schreibt.
"""

from __future__ import annotations

import yaml

# Das Fixture ``app_client`` steht in test_web.py und wird hier eingezogen;
# pytest findet es ueber den Namen, ruff haelt die Parameter fuer Schatten.
# ruff: noqa: F811
from tests.test_web import (  # noqa: F401
    _anmelden,
    _csrf,
    _kanal_id,
    _person_anlegen,
    _schreibe,
    _warte_auf_client,
    app_client,
)


def _loeschen(client, name):
    return client.request(
        "DELETE", "/api/shows", params={"name": name},
        headers={"X-CSRF-Token": _csrf(client)},
    )


def _kanal_anlegen(client, name, parent):
    antwort = _schreibe(client, "post", "/api/channels", {"name": name, "parent": parent})
    assert antwort.status_code == 200, antwort.text
    return antwort.json()


def _shows(client):
    return {s["name"]: s for s in client.get("/api/shows").json()["shows"]}


def test_aktuellen_stand_als_show_ablegen(app_client):
    client, _ = app_client
    _anmelden(client)
    antwort = _schreibe(client, "post", "/api/shows",
                        {"name": "  Landesfinale   Halle 1 ", "notiz": "mit Kampfgerichten"})
    assert antwort.status_code == 200, antwort.text
    # Leerraum wird zusammengezogen -- zwei Shows, die gleich aussehen, waeren
    # sonst zwei verschiedene.
    assert antwort.json()["name"] == "Landesfinale Halle 1"

    show = _shows(client)["Landesfinale Halle 1"]
    assert show["notiz"] == "mit Kampfgerichten"
    assert show["author"] == "admin"
    assert show["geladen"] is None
    assert show["inhalt"]["lesbar"] is True
    assert show["inhalt"]["plaetze"] >= 3  # Intercom, Regie, Kameras
    assert show["inhalt"]["rollen"] == 2


def test_gleicher_name_wird_nicht_still_ueberschrieben(app_client):
    client, _ = app_client
    _anmelden(client)
    assert _schreibe(client, "post", "/api/shows", {"name": "Training"}).status_code == 200
    doppelt = _schreibe(client, "post", "/api/shows", {"name": "Training"})
    assert doppelt.status_code == 409
    assert "Überschreiben" in doppelt.json()["detail"]
    erlaubt = _schreibe(client, "post", "/api/shows",
                        {"name": "Training", "ueberschreiben": True, "notiz": "neu"})
    assert erlaubt.status_code == 200
    assert _shows(client)["Training"]["notiz"] == "neu"


def test_testlauf_schreibt_nichts_und_laden_stellt_her(app_client):
    client, fake = app_client
    _anmelden(client)
    intercom = _kanal_id(client, "Intercom")
    _schreibe(client, "post", "/api/shows", {"name": "Grundaufbau"})

    # Danach wird umgebaut: ein Platz kommt dazu.
    _kanal_anlegen(client, "Presse", intercom)
    assert any(c.name == "Presse" for c in fake.server.channels.values())

    plan = _schreibe(client, "post", "/api/shows/plan", {"name": "Grundaufbau"})
    assert plan.status_code == 200, plan.text
    loeschen = [c for c in plan.json()["changes"] if c["kind"] == "channel_delete"]
    assert [c["target"] for c in loeschen] == ["Intercom/Presse"]
    # Der Testlauf hat nichts angefasst.
    assert any(c.name == "Presse" for c in fake.server.channels.values())
    assert _shows(client)["Grundaufbau"]["geladen"] is None

    geladen = _schreibe(client, "post", "/api/shows/laden", {"name": "Grundaufbau"})
    assert geladen.status_code == 200, geladen.text
    assert geladen.json()["fehlgeschlagen"] == 0
    assert not any(c.name == "Presse" for c in fake.server.channels.values())

    liste = client.get("/api/shows").json()
    assert liste["zuletzt_geladen"] == "Grundaufbau"
    assert liste["shows"][0]["geladen_von"] == "admin"


def test_nur_ergaenzen_laesst_neues_stehen(app_client):
    client, fake = app_client
    _anmelden(client)
    intercom = _kanal_id(client, "Intercom")
    _schreibe(client, "post", "/api/shows", {"name": "Grundaufbau"})
    _kanal_anlegen(client, "Presse", intercom)

    antwort = _schreibe(client, "post", "/api/shows/laden",
                        {"name": "Grundaufbau", "aufraeumen": False})
    assert antwort.status_code == 200, antwort.text
    assert any(c.name == "Presse" for c in fake.server.channels.values())


def test_laden_stellt_ruftasten_und_verbindungen_genau_her(app_client):
    """Das kennt Mumble nicht -- ohne Show waere es nach dem Umbau weg."""
    client, fake = app_client
    _anmelden(client)
    kameras = _kanal_id(client, "Kameras")
    intercom = _kanal_id(client, "Intercom")
    session = fake.server.connect_user("kam-tablet", channel=kameras)
    _warte_auf_client(client, "kam-tablet")

    _schreibe(client, "put", "/api/pult/ruftaste", {"kanal": kameras, "taste": 1, "rolle": "regie"})
    _schreibe(client, "put", "/api/pult/verbindung",
              {"art": "hoert", "von": kameras, "nach": _kanal_id(client, "Regie"), "an": True})
    gespeichert = _schreibe(client, "post", "/api/shows", {"name": "Show A"})
    assert gespeichert.json()["inhalt"]["ruftasten"] == 1
    assert gespeichert.json()["inhalt"]["verbindungen"] == 1

    # Umbau: andere Belegung, eine zusaetzliche, Verbindung weg.
    _schreibe(client, "put", "/api/pult/ruftaste", {"kanal": kameras, "taste": 1, "rolle": "kamera"})
    _schreibe(client, "put", "/api/pult/ruftaste", {"kanal": intercom, "taste": 3, "rolle": "regie"})
    _schreibe(client, "put", "/api/pult/verbindung",
              {"art": "hoert", "von": kameras, "nach": _kanal_id(client, "Regie"), "an": False})
    assert fake.server.whisper_redirects[session] == {"ruf1": "kamera", "ruf3": "regie"}

    plan = _schreibe(client, "post", "/api/shows/plan", {"name": "Show A"}).json()
    assert plan["ruftasten"] == {"uebernommen": 1, "geaendert": 1, "entfernt": 1}
    assert plan["verbindungen"]["neu"] == 1
    assert any("Ruftasten" in z and "würde freigegeben" in z for z in plan["zusatz"])

    _schreibe(client, "post", "/api/shows/laden", {"name": "Show A"})
    blatt = client.get(f"/api/pult/platz/{kameras}").json()
    belegt = {t["taste"]: t["wirksam"] for t in blatt["ruftasten"] if t["wirksam"]}
    assert belegt == {1: "regie"}
    # Die verbundene Sitzung folgt sofort, nicht erst beim naechsten Abgleich.
    assert fake.server.whisper_redirects[session] == {"ruf1": "regie"}
    # Ein zweiter Testlauf findet nichts mehr zu tun: alles steht wie in der Show.
    danach = _schreibe(client, "post", "/api/shows/plan", {"name": "Show A"}).json()
    assert danach["ruftasten"] == {"uebernommen": 1, "geaendert": 0, "entfernt": 0}
    assert danach["verbindungen"] == {"uebernommen": 1, "neu": 0, "entfernt": 0}


def test_feste_plaetze_werden_ueber_den_namen_zugeordnet(app_client):
    client, _ = app_client
    _anmelden(client)
    userid = _person_anlegen(client, "zeit-1")
    regie = _kanal_id(client, "Regie")
    _schreibe(client, "put", "/api/pult/wunsch", {"art": "platz", "userid": userid, "kanaele": [regie]})
    _schreibe(client, "post", "/api/shows", {"name": "Mit Personen"})
    _schreibe(client, "put", "/api/pult/wunsch", {"art": "platz", "userid": userid, "kanaele": []})

    antwort = _schreibe(client, "post", "/api/shows/laden", {"name": "Mit Personen"}).json()
    assert antwort["wunsch"]["uebernommen"] == 1
    wunsch = client.get("/api/pult/wunsch").json()["wunsch"]
    assert wunsch["platz"][str(userid)][0]["pfad"] == "Intercom/Regie"


def test_hochladen_prueft_vorher(app_client):
    client, _ = app_client
    _anmelden(client)
    kaputt = _schreibe(client, "post", "/api/shows/hochladen",
                       {"name": "Kaputt", "yaml_text": "channels: [:"})
    assert kaputt.status_code == 400
    falsch = _schreibe(client, "post", "/api/shows/hochladen",
                       {"name": "Falsch", "yaml_text": "groups: [all]\n"})
    assert falsch.status_code == 400, "Meta-Gruppen als Rolle muss die Pruefung ablehnen"
    assert "Kaputt" not in _shows(client) and "Falsch" not in _shows(client)

    text = client.get("/api/provision/export").text
    gut = _schreibe(client, "post", "/api/shows/hochladen", {"name": "Aus Datei", "yaml_text": text})
    assert gut.status_code == 200, gut.text
    assert _shows(client)["Aus Datei"]["inhalt"]["plaetze"] >= 3


def test_herunterladen_liefert_genau_den_text(app_client):
    client, _ = app_client
    _anmelden(client)
    text = client.get("/api/provision/export").text + "\n# eigene Notiz\n"
    _schreibe(client, "post", "/api/shows/hochladen", {"name": "Halle / Probe", "yaml_text": text})
    datei = client.get("/api/shows/datei", params={"name": "Halle / Probe"})
    assert datei.status_code == 200
    assert datei.text == text
    assert "attachment" in datei.headers["content-disposition"]
    assert client.get("/api/shows/datei", params={"name": "Gibt es nicht"}).status_code == 404


def test_umbenennen_notiz_und_loeschen(app_client):
    client, fake = app_client
    _anmelden(client)
    _schreibe(client, "post", "/api/shows", {"name": "Alt"})
    _schreibe(client, "post", "/api/shows", {"name": "Andere"})
    kanaele_vorher = len(fake.server.channels)

    assert _schreibe(client, "patch", "/api/shows",
                     {"name": "Alt", "neuer_name": "Andere"}).status_code == 409
    umbenannt = _schreibe(client, "patch", "/api/shows",
                          {"name": "Alt", "neuer_name": "Neu", "notiz": "Halle 2"})
    assert umbenannt.status_code == 200, umbenannt.text
    assert _shows(client)["Neu"]["notiz"] == "Halle 2"
    assert "Alt" not in _shows(client)

    assert _loeschen(client, "Neu").status_code == 200
    assert _loeschen(client, "Neu").status_code == 404
    # Eine Show zu loeschen aendert am Server nichts.
    assert len(fake.server.channels) == kanaele_vorher


def test_nur_lesen_darf_ansehen_aber_nichts_aendern(app_client):
    client, _ = app_client
    _anmelden(client)
    _schreibe(client, "post", "/api/shows", {"name": "Training"})
    client.post("/logout", headers={"X-CSRF-Token": _csrf(client)})
    _anmelden(client, "viewer", "lesen")

    assert "Training" in _shows(client)
    for methode, pfad, koerper in (
        ("post", "/api/shows", {"name": "X"}),
        ("post", "/api/shows/laden", {"name": "Training"}),
        ("post", "/api/shows/plan", {"name": "Training"}),
        ("patch", "/api/shows", {"name": "Training", "notiz": "x"}),
    ):
        assert _schreibe(client, methode, pfad, koerper).status_code == 403, pfad
    assert _loeschen(client, "Training").status_code == 403


def test_ohne_csrf_wird_nichts_abgelegt(app_client):
    client, _ = app_client
    _anmelden(client)
    antwort = client.post("/api/shows", json={"name": "Ohne Token"})
    assert antwort.status_code == 403
    assert "Ohne Token" not in _shows(client)


def test_export_enthaelt_ruftasten_mit_schraegstrich_fuer_ueberall(app_client):
    client, _ = app_client
    _anmelden(client)
    _schreibe(client, "put", "/api/pult/ruftaste", {"kanal": 0, "taste": 4, "rolle": "all"})
    roh = yaml.safe_load(client.get("/api/provision/export").text)
    assert roh["ruftasten"] == {"/": {4: "all"}}


def test_einrichten_seite_zeigt_shows(app_client):
    client, _ = app_client
    _anmelden(client)
    text = client.get("/einrichten").text
    assert "Shows" in text
    assert "/api/shows" in text


def test_zusammenfassung_spricht_nach_dem_laden_in_der_vergangenheit(app_client):
    """"13 anzulegen" ueber einem fertigen Ergebnis las sich wie ein offener Plan."""
    client, _ = app_client
    _anmelden(client)
    intercom = _kanal_id(client, "Intercom")
    _schreibe(client, "post", "/api/shows", {"name": "Grundaufbau"})
    _kanal_anlegen(client, "Presse", intercom)

    plan = _schreibe(client, "post", "/api/shows/plan", {"name": "Grundaufbau"}).json()
    assert "zu löschen" in plan["summary"]
    ohne = _schreibe(client, "post", "/api/shows/plan",
                     {"name": "Grundaufbau", "aufraeumen": False}).json()
    assert "nur mit Aufräumen" in ohne["summary"]
    geladen = _schreibe(client, "post", "/api/shows/laden", {"name": "Grundaufbau"}).json()
    assert "1 gelöscht" in geladen["summary"]
    assert "zu löschen" not in geladen["summary"]


def test_show_laden_laesst_die_regel_des_monitor_bots_stehen(app_client):
    """Sonst naehme jede Show mit Aufraeumen dem Cockpit den Paketverlust (D-035)."""
    from intercom.monitor.berechtigung import gruppe

    client, _ = app_client
    _anmelden(client)
    fp = "cd34" * 10
    client.app.state.ctx.monitor_fingerabdruck = fp

    _schreibe(client, "post", "/api/shows", {"name": "Grundaufbau"})
    datei = client.get("/api/shows/datei", params={"name": "Grundaufbau"}).text
    assert gruppe(fp) not in datei, "der Hash dieser Installation gehoert nicht in die Show"

    geladen = _schreibe(client, "post", "/api/shows/laden", {"name": "Grundaufbau"})
    assert geladen.status_code == 200, geladen.text
    acl = client.get("/api/channels/0/acl").json()
    regeln = [a for a in acl["acls"] if a.get("group") == gruppe(fp) and not a.get("inherited")]
    assert len(regeln) == 1, acl["acls"]
    assert acl["bezeichnungen"][gruppe(fp)].startswith("Monitor-Bot")

    plan = _schreibe(client, "post", "/api/shows/plan", {"name": "Grundaufbau"}).json()
    assert not [c for c in plan["changes"] if gruppe(fp) in c["target"]]
    assert plan["summary"].startswith("Keine Änderungen"), plan["summary"]

    health = client.get("/healthz").json()
    assert health["monitor"]["alle_plaetze"] is True
