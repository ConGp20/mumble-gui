"""SQLite-Schicht gegen eine echte Datei unter ``tmp_path``.

Kein Ice und kein Mock: SQLite ist die zu testende Sache. Ein Mock wuerde genau
das wegabstrahieren, worum es hier geht -- Transaktionen, Sperren und die Frage,
ob zwei Threads sich in die Quere kommen.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from intercom.store import SCHEMA_VERSION, Sample, Store, StoreClosed
from intercom.store.db import _MIGRATIONS


@pytest.fixture()
def store(tmp_path):
    """Ein migrierter Store auf einer frischen Datei."""
    with Store(tmp_path / "history.sqlite") as opened:
        opened.migrate()
        yield opened


def _sample(ts: int, name: str = "kam-1", session: int = 11, **felder) -> Sample:
    werte = {
        "userid": 5,
        "channel_id": 3,
        "address": "10.20.30.40",
        "ping_ms": 12.5,
        "loss_pct": 0.0,
        "bandwidth_bps": 48000,
        "tcp_only": False,
    }
    werte.update(felder)
    return Sample(ts=ts, session=session, name=name, **werte)


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #


def test_migrate_ist_idempotent(tmp_path):
    pfad = tmp_path / "history.sqlite"
    with Store(pfad) as store:
        assert store.schema_version() == 0
        assert store.migrate() == SCHEMA_VERSION
        store.record_samples([_sample(int(time.time()))])
        assert store.migrate() == SCHEMA_VERSION
        assert store.schema_version() == SCHEMA_VERSION
        # Der zweite Lauf darf weder den Schritt noch die Daten wiederholen.
        with sqlite3.connect(pfad) as roh:
            versionen = roh.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
            assert versionen == [(v,) for v in range(1, SCHEMA_VERSION + 1)]
            assert roh.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_wal_ist_eingeschaltet(store, tmp_path):
    with sqlite3.connect(tmp_path / "history.sqlite") as roh:
        assert roh.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_geschlossener_store_meldet_sich_klar(tmp_path):
    store = Store(tmp_path / "history.sqlite")
    store.migrate()
    store.close()
    with pytest.raises(StoreClosed):
        store.history("kam-1")


# --------------------------------------------------------------------------- #
#  Metrik-Verlauf
# --------------------------------------------------------------------------- #


def test_record_und_history_umlauf(store):
    now = int(time.time())
    store.record_samples(
        [
            _sample(now - 120, ping_ms=11.0, loss_pct=0.5, tcp_only=True),
            _sample(now - 60, ping_ms=22.0, bandwidth_bps=64000),
        ]
    )
    verlauf = store.history("kam-1", minutes=60)

    assert [s.ts for s in verlauf] == [now - 120, now - 60]
    erste = verlauf[0]
    assert erste.name == "kam-1"
    assert erste.session == 11
    assert erste.userid == 5
    assert erste.channel_id == 3
    assert erste.address == "10.20.30.40"
    assert erste.ping_ms == 11.0
    assert erste.loss_pct == 0.5
    assert erste.bandwidth_bps == 48000
    assert erste.tcp_only is True
    assert verlauf[1].bandwidth_bps == 64000
    assert verlauf[1].tcp_only is False


def test_leerer_durchlauf_kostet_nichts(store):
    store.record_samples([])
    assert store.history("kam-1") == []


def test_history_trennt_die_clients(store):
    now = int(time.time())
    store.record_samples([_sample(now, name="kam-1"), _sample(now, name="regie-1")])
    assert [s.name for s in store.history("regie-1")] == ["regie-1"]


def test_history_folgt_dem_namen_ueber_den_sessionwechsel(store):
    """Ein Reconnect vergibt eine neue Session -- der Verlauf darf nicht abreissen."""
    now = int(time.time())
    store.record_samples(
        [
            _sample(now - 300, session=11, ping_ms=10.0),
            _sample(now - 240, session=11, ping_ms=12.0),
            # Hier bricht das WLAN ab, der Client kommt mit neuer Session wieder.
            _sample(now - 60, session=987, ping_ms=40.0),
        ]
    )
    verlauf = store.history("kam-1", minutes=60)

    assert [s.session for s in verlauf] == [11, 11, 987]
    assert [s.ping_ms for s in verlauf] == [10.0, 12.0, 40.0]


def test_history_beachtet_das_zeitfenster(store):
    now = int(time.time())
    store.record_samples([_sample(now - 7200), _sample(now - 60)])
    assert len(store.history("kam-1", minutes=60)) == 1
    assert len(store.history("kam-1", minutes=180)) == 2


def test_sparkline_rastert_und_mittelt(store):
    now = int(time.time())
    store.record_samples(
        [
            _sample(now - 40, ping_ms=10.0),
            _sample(now - 20, ping_ms=30.0),
            _sample(now - 90, ping_ms=50.0),
        ]
    )
    reihe = store.sparkline("kam-1", "ping_ms", points=10, minutes=10)

    assert len(reihe) == 10
    assert reihe[9] == 20.0  # Mittel aus 10 und 30
    assert reihe[8] == 50.0


def test_sparkline_laesst_luecken_offen(store):
    now = int(time.time())
    store.record_samples([_sample(now - 20, ping_ms=10.0)])
    reihe = store.sparkline("kam-1", "ping_ms", points=10, minutes=10)

    assert reihe[9] == 10.0
    assert all(wert is None for wert in reihe[:9])


def test_sparkline_ohne_daten_ist_voll_leer(store):
    assert store.sparkline("gibt-es-nicht", "loss_pct", points=5, minutes=5) == [None] * 5


def test_sparkline_lehnt_fremde_spalten_ab(store):
    with pytest.raises(ValueError, match="zeichenbare Spalte"):
        store.sparkline("kam-1", "name; DROP TABLE samples")
    with pytest.raises(ValueError):
        store.sparkline("kam-1", "ping_ms", points=0)
    with pytest.raises(ValueError):
        store.sparkline("kam-1", "ping_ms", minutes=0)


def test_prune_loescht_nur_altes_und_zaehlt_richtig(store):
    now = int(time.time())
    alt = [_sample(now - 10 * 3600 - i) for i in range(3)]
    neu = [_sample(now - 3600), _sample(now - 60)]
    store.record_samples(alt + neu)

    assert store.prune(retention_hours=5) == 3
    verlauf = store.history("kam-1", minutes=24 * 60)
    assert [s.ts for s in verlauf] == [now - 3600, now - 60]
    # Zweiter Lauf findet nichts mehr.
    assert store.prune(retention_hours=5) == 0


# --------------------------------------------------------------------------- #
#  Audit-Log
# --------------------------------------------------------------------------- #


def test_audit_haelt_alle_felder(store):
    neue_id = store.audit(
        actor="admin",
        action="channel_update",
        target="Intercom/Regie",
        before='{"position": 10}',
        after='{"position": 20}',
        ok=False,
        error="murmur hat abgelehnt",
    )
    assert neue_id > 0

    eintrag = store.audit_entries()[0]
    assert eintrag.id == neue_id
    assert eintrag.actor == "admin"
    assert eintrag.action == "channel_update"
    assert eintrag.target == "Intercom/Regie"
    assert eintrag.before == '{"position": 10}'
    assert eintrag.after == '{"position": 20}'
    assert eintrag.ok is False
    assert eintrag.error == "murmur hat abgelehnt"
    assert eintrag.ts > 0


def test_audit_neueste_zuerst_und_blaetterei(store):
    for i in range(5):
        store.audit("admin", "move_user", f"ziel-{i}")

    seite1 = store.audit_entries(limit=2)
    seite2 = store.audit_entries(limit=2, offset=2)
    seite3 = store.audit_entries(limit=2, offset=4)

    assert [e.target for e in seite1] == ["ziel-4", "ziel-3"]
    assert [e.target for e in seite2] == ["ziel-2", "ziel-1"]
    assert [e.target for e in seite3] == ["ziel-0"]
    assert store.audit_entries(limit=2, offset=99) == []


def test_audit_filter(store):
    store.audit("admin", "kick", "kam-1", after='{"grund": "test"}')
    store.audit("admin", "move_user", "kam-2")
    store.audit("regie", "kick", "kam-3", ok=False, error="keine Rechte")

    assert len(store.audit_entries(actor="admin")) == 2
    assert len(store.audit_entries(action="kick")) == 2
    assert len(store.audit_entries(actor="regie", action="kick")) == 1
    assert [e.target for e in store.audit_entries(search="kam-2")] == ["kam-2"]
    assert [e.target for e in store.audit_entries(search="grund")] == ["kam-1"]
    assert [e.target for e in store.audit_entries(search="keine Rechte")] == ["kam-3"]
    assert store.audit_entries(search="gibt es nicht") == []


def test_audit_count_benutzt_dieselben_filter(store):
    for i in range(7):
        store.audit("admin" if i % 2 else "regie", "kick", f"ziel-{i}")

    assert store.audit_count() == 7
    assert store.audit_count(actor="admin") == 3
    assert store.audit_count(action="kick") == 7
    assert store.audit_count(actor="admin", action="move_user") == 0
    assert store.audit_count(search="ziel-3") == 1
    # Blaetterei und Gesamtzahl muessen zusammenpassen.
    assert len(store.audit_entries(limit=100, actor="admin")) == store.audit_count(
        actor="admin"
    )


def test_audit_suche_nimmt_platzhalter_woertlich(store):
    store.audit("admin", "note", "kam_1")
    store.audit("admin", "note", "kamX1")

    assert [e.target for e in store.audit_entries(search="kam_1")] == ["kam_1"]
    assert store.audit_entries(search="%") == []


def test_audit_auswahllisten(store):
    store.audit("regie", "kick", "kam-1")
    store.audit("admin", "move_user", "kam-2")
    store.audit("admin", "kick", "kam-3")

    assert store.audit_actors() == ["admin", "regie"]
    assert store.audit_actions() == ["kick", "move_user"]


def test_prune_laesst_das_audit_log_stehen(store):
    store.audit("admin", "kick", "kam-1")
    store.record_samples([_sample(int(time.time()) - 10 * 3600)])

    assert store.prune(retention_hours=1) == 1
    assert store.audit_count() == 1


# --------------------------------------------------------------------------- #
#  Notizen
# --------------------------------------------------------------------------- #


def test_notiz_upsert_ueberschreibt(store):
    store.set_note("device", "headset-3", "kratzt", "admin")
    erste = store.get_note("device", "headset-3")
    assert erste is not None
    assert erste.text == "kratzt"
    assert erste.author == "admin"

    store.set_note("device", "headset-3", "getauscht", "regie")
    zweite = store.get_note("device", "headset-3")
    assert zweite is not None
    assert zweite.text == "getauscht"
    assert zweite.author == "regie"
    assert zweite.updated_at >= erste.updated_at
    assert len(store.notes("device")) == 1


def test_notiz_unbekannt_ist_none(store):
    assert store.get_note("user", "gibt-es-nicht") is None
    assert store.notes("user") == {}


def test_notizen_trennen_die_arten(store):
    store.set_note("user", "kam-1", "neu im Team", "admin")
    store.set_note("channel", "kam-1", "nur fuer Kameras", "admin")

    nutzer = store.notes("user")
    kanaele = store.notes("channel")
    assert set(nutzer) == {"kam-1"}
    assert nutzer["kam-1"].text == "neu im Team"
    assert kanaele["kam-1"].text == "nur fuer Kameras"


# --------------------------------------------------------------------------- #
#  Nebenlaeufigkeit
# --------------------------------------------------------------------------- #


def test_lesen_geht_waehrend_ein_fremder_schreiber_offen_ist(store, tmp_path):
    """Ein fremder Schreiber mit offener Transaktion haelt das Lesen nicht an.

    Die zweite Verbindung steht fuer einen anderen Prozess -- die CLI oder ein
    ``sqlite3`` von Hand. Dessen noch nicht festgeschriebene Zeile darf im
    Cockpit weder auftauchen noch den Lesepfad blockieren.
    """
    fremd = sqlite3.connect(tmp_path / "history.sqlite", isolation_level=None)
    try:
        fremd.execute("BEGIN IMMEDIATE")
        fremd.execute(
            "INSERT INTO samples (ts, session, name, userid, channel_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), 11, "kam-1", 5, 3),
        )
        # Noch nicht festgeschrieben: sichtbar ist nichts, blockieren darf es
        # trotzdem nicht.
        assert store.history("kam-1") == []
        assert store.audit_count() == 0
        fremd.execute("ROLLBACK")
    finally:
        fremd.close()


def test_schreibender_thread_stoert_den_lesenden_nicht(store):
    """Der Monitor-Bot schreibt aus seinem Thread, das Cockpit liest weiter.

    Faellt der Test mit "database is locked", stimmt entweder der WAL-Modus
    nicht oder das Schreib-Lock deckt nicht die ganze Transaktion ab.
    """
    runden = 150
    fertig = threading.Event()
    fehler: list[BaseException] = []

    def schreiben() -> None:
        try:
            for i in range(runden):
                now = int(time.time())
                store.record_samples(
                    [_sample(now, name=f"kam-{n}", ping_ms=float(i)) for n in range(5)]
                )
                store.audit("bot", "sample", f"runde-{i}")
        except BaseException as exc:  # noqa: BLE001 - im Hauptthread ausgewertet
            fehler.append(exc)
        finally:
            fertig.set()

    schreiber = threading.Thread(target=schreiben, name="test-monitor")
    schreiber.start()

    lesevorgaenge = 0
    try:
        while not fertig.wait(timeout=0.002):
            store.history("kam-1", minutes=60)
            store.sparkline("kam-2", "ping_ms", points=30, minutes=30)
            store.audit_count(actor="bot")
            store.notes("device")
            lesevorgaenge += 1
    finally:
        schreiber.join(timeout=30)

    assert not fehler, f"Schreiber ist gescheitert: {fehler[0]!r}"
    assert not schreiber.is_alive()
    assert lesevorgaenge > 0, "Der Lesepfad kam gar nicht zum Zug."
    assert store.audit_count(actor="bot") == runden
    assert len(store.history("kam-1", minutes=60)) == runden


def test_zwei_threads_schreiben_ohne_verlust(store):
    """Zwei Schreiber gleichzeitig: SQLite laesst nur einen zu, keiner faellt raus."""
    pro_thread = 60
    fehler: list[BaseException] = []

    def schreiben(kennung: str) -> None:
        try:
            for i in range(pro_thread):
                store.audit(kennung, "test", f"{kennung}-{i}")
        except BaseException as exc:  # noqa: BLE001 - im Hauptthread ausgewertet
            fehler.append(exc)

    threads = [
        threading.Thread(target=schreiben, args=(name,), name=f"test-{name}")
        for name in ("bot", "web")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not fehler, f"Schreiber ist gescheitert: {fehler[0]!r}"
    assert store.audit_count(actor="bot") == pro_thread
    assert store.audit_count(actor="web") == pro_thread


def test_close_raeumt_auch_fremde_threads_ab(tmp_path):
    """close() muss die Verbindung eines beendeten Threads mit abbauen."""
    store = Store(tmp_path / "history.sqlite")
    store.migrate()

    def schreiben() -> None:
        store.record_samples([_sample(int(time.time()))])

    thread = threading.Thread(target=schreiben, name="test-kurz")
    thread.start()
    thread.join(timeout=10)

    assert len(store._connections) == 2
    store.close()
    assert store._connections == []


def test_nicht_gemessener_verlust_bleibt_leer(store):
    """``None`` heisst "nicht gemessen", ``0.0`` heisst "kein Verlust".

    Ohne Monitor-Bot -- oder ohne dessen Ban-Recht am Wurzelkanal -- gibt es
    ueberhaupt keine Verlustzahlen. Als 0 gespeichert zeichnete die Sparkline
    daraus eine makellose Nulllinie, also eine Entwarnung, die niemand gemessen
    hat.
    """
    jetzt = int(time.time())
    store.record_samples([_sample(jetzt, loss_pct=None)])

    verlauf = store.history("kam-1")
    assert len(verlauf) == 1
    assert verlauf[0].loss_pct is None


def test_sparkline_zeichnet_die_luecke_nicht_zu(store):
    """Ein Fach ohne Messung bleibt ``None``, auch wenn Ping-Werte da sind."""
    jetzt = int(time.time())
    store.record_samples(
        [
            _sample(jetzt - 90, loss_pct=None),
            _sample(jetzt - 60, loss_pct=None),
            _sample(jetzt - 30, loss_pct=3.5),
        ]
    )

    reihe = store.sparkline("kam-1", "loss_pct", points=4, minutes=2)
    gemessen = [w for w in reihe if w is not None]
    assert gemessen == [3.5], reihe
    # Der Ping wurde durchgehend gemessen -- die Luecke betrifft nur den Verlust.
    assert len([w for w in store.sparkline("kam-1", "ping_ms", points=4, minutes=2)
                if w is not None]) >= 2


def test_alte_datenbank_wird_auf_nullbaren_verlust_gehoben(tmp_path):
    """Migration 2 baut ``samples`` um -- vorhandene Zeilen muessen bleiben."""
    pfad = tmp_path / "history.sqlite"
    jetzt = int(time.time())

    # Stand 1 herstellen: nur den ersten Schritt ausfuehren.
    with sqlite3.connect(pfad) as roh:
        roh.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        for anweisung in _MIGRATIONS[0][1]:
            roh.execute(anweisung)
        roh.execute("INSERT INTO schema_version VALUES (1, ?)", (jetzt,))
        roh.execute(
            "INSERT INTO samples (ts, session, name, userid, channel_id, address, "
            "ping_ms, loss_pct, bandwidth_bps, tcp_only) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (jetzt, 11, "kam-1", 5, 3, "10.20.30.40", 12.5, 2.5, 48000, 0),
        )

    with Store(pfad) as store:
        assert store.schema_version() == 1
        assert store.migrate() == SCHEMA_VERSION

        # Die alte Zeile ist noch da ...
        verlauf = store.history("kam-1")
        assert [(s.name, s.loss_pct) for s in verlauf] == [("kam-1", 2.5)]

        # ... und neue Zeilen duerfen jetzt None sein.
        store.record_samples([_sample(jetzt + 1, loss_pct=None)])
        assert store.history("kam-1")[-1].loss_pct is None

    # Die Indizes haengen an der neuen Tabelle, nicht an einer Zwischentabelle.
    with sqlite3.connect(pfad) as roh:
        indizes = {
            name
            for (name,) in roh.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'samples'"
            )
        }
        assert {"samples_ts", "samples_name_ts"} <= indizes
        tabellen = {
            name for (name,) in roh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "samples_alt" not in tabellen


# --------------------------------------------------------------------------- #
#  Wunschzustand
# --------------------------------------------------------------------------- #


def test_wunsch_setzen_und_lesen(tmp_path):
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("platz", 7, ["Wettkampf/Technik"], "admin")
        store.set_wunsch("mithoeren", 7, ["Wettkampf", "Wettkampf/Zeitmessung"], "admin")
        assert store.wuensche("platz") == {7: ["Wettkampf/Technik"]}
        assert store.alle_wuensche()["mithoeren"] == {
            7: ["Wettkampf", "Wettkampf/Zeitmessung"]
        }
        # Jede Art hat ihren Platz, auch wenn nichts drinsteht.
        assert store.alle_wuensche()["vorrang"] == {}


def test_wunsch_setzen_ersetzt_statt_anzuhaengen(tmp_path):
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("mithoeren", 7, ["A", "B"])
        store.set_wunsch("mithoeren", 7, ["C"])
        assert store.wuensche("mithoeren") == {7: ["C"]}


def test_leere_liste_loescht_den_wunsch(tmp_path):
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("platz", 7, ["Wettkampf"])
        store.set_wunsch("platz", 7, [])
        assert store.wuensche("platz") == {}


def test_unbekannte_wunschart_wird_abgelehnt(tmp_path):
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        with pytest.raises(ValueError, match="Wunschart"):
            store.set_wunsch("farbe", 7, ["blau"])


def test_umbenennen_zieht_auch_die_unterpfade_mit(tmp_path):
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("platz", 7, ["Wettkampf/Technik"])
        store.set_wunsch("platz", 8, ["Wettkampf"])
        assert store.wunsch_umschreiben("Wettkampf", "Meeting") == 2
        assert store.wuensche("platz") == {7: ["Meeting/Technik"], 8: ["Meeting"]}


def test_umbenennen_trifft_keine_namensverwandten(tmp_path):
    """``Wettkampf`` darf nicht ``Wettkampfbuero`` mitnehmen."""
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("platz", 7, ["Wettkampfbuero"])
        assert store.wunsch_umschreiben("Wettkampf", "Meeting") == 0
        assert store.wuensche("platz") == {7: ["Wettkampfbuero"]}


def test_wunsch_vergessen_raeumt_alle_arten(tmp_path):
    """murmur vergibt Nutzer-IDs weiter -- ein Rest erbte sonst die naechste Person."""
    with Store(tmp_path / "h.sqlite") as store:
        store.migrate()
        store.set_wunsch("platz", 7, ["A"])
        store.set_wunsch("mithoeren", 7, ["B"])
        store.set_wunsch("platz", 8, ["C"])
        assert store.wunsch_vergessen(7) == 2
        assert store.alle_wuensche() == {
            "platz": {8: ["C"]},
            "mithoeren": {},
            "vorrang": {},
        }
