"""SQLite fuer Metrik-Verlauf, Audit-Log und Notizen.

Eine Datei (``settings.db_path``, im Container ``/data/history.sqlite``) mit drei
Bestaenden:

* ``samples`` -- Messreihen je Client, die der Monitor-Bot im Sekundentakt fuellt.
* ``audit``   -- wer hat wann was geaendert und ob es geklappt hat.
* ``notes``   -- freie Notizen an Nutzern, Kanaelen und Geraeten.

Nebenlaeufigkeit
----------------
Zwei Seiten greifen gleichzeitig zu: der Monitor-Bot schreibt aus seinem eigenen
Thread, der Web-Layer liest aus dem asyncio-Loop. Daraus folgen drei
Entscheidungen, die zusammengehoeren:

1. **WAL** (``journal_mode=WAL``). Im voreingestellten Rollback-Journal sperrt
   ein Schreiber die ganze Datei: jede Kachel im Cockpit wartet dann auf den
   Bot und der Bot auf die Kachel. Mit WAL liest der Web-Layer auf einem
   Schnappschuss weiter, waehrend geschrieben wird.

2. **Eine Verbindung je Thread** (``threading.local``) statt einer gemeinsamen.
   Eine geteilte Verbindung haette den Gewinn aus (1) wieder aufgezehrt, denn
   sie traegt genau *einen* Transaktionszustand: ein ``BEGIN`` des Bots zoege
   die SELECTs des Web-Layers in seine Transaktion. Ein Lock muesste deshalb
   auch die Lesezugriffe umschliessen -- dann wartet wieder alles auf alles.
   Die Alternative (gemeinsame Verbindung, Lock nur um Schreib-Transaktionen)
   ist genau deswegen verworfen.

3. **Ein prozessweites Schreib-Lock**. SQLite laesst auch in WAL nur einen
   Schreiber zu; ohne Lock bekaeme der zweite ``SQLITE_BUSY``, im Klartext
   "database is locked". Das Lock macht daraus ein geordnetes Warten im
   Prozess. ``timeout`` beim Verbindungsaufbau (= ``busy_timeout``) deckt
   zusaetzlich den Fall ab, dass ein *anderer* Prozess schreibt -- die CLI oder
   ein ``sqlite3`` von Hand.

Weil ``close()`` beim Herunterfahren auch die Verbindungen bereits beendeter
Threads abbauen muss, werden alle Verbindungen mit ``check_same_thread=False``
geoeffnet und in einer Liste mitgefuehrt.

Transaktionen
-------------
Die Verbindungen laufen mit ``isolation_level=None``, also ohne das implizite
``BEGIN`` des sqlite3-Moduls. Jede Schreib-Transaktion beginnt ausdruecklich mit
``BEGIN IMMEDIATE``: so faellt ein belegter Schreib-Slot sofort am Anfang auf und
nicht erst beim ``COMMIT``, wo ein Rollback die bereits geschriebenen Zeilen
kosten wuerde.
"""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "SPARKLINE_FIELDS",
    "AuditEntry",
    "Note",
    "Sample",
    "Store",
    "StoreClosed",
]

#: Wie lange auf einen fremden Schreiber gewartet wird, bevor SQLite aufgibt.
#: Der Wert deckt einen laufenden ``prune`` auf einer grossen Datei ab.
_BUSY_TIMEOUT_S: Final[float] = 5.0

#: Spalten, die :meth:`Store.sparkline` zeichnen kann. Der Feldname wandert in
#: den SQL-Text, deshalb ist die Liste eine Weissliste und keine Pruefung "ist
#: das ein Feld von Sample".
SPARKLINE_FIELDS: Final[frozenset[str]] = frozenset(
    {"ping_ms", "loss_pct", "bandwidth_bps"}
)

#: Schema-Schritte. Jeder Schritt laeuft genau einmal; die Nummer landet in
#: ``schema_version``. Neue Versionen werden hier angehaengt, nie geaendert --
#: eine Datenbank im Feld hat den alten Schritt bereits ausgefuehrt.
_MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts            INTEGER NOT NULL,
                session       INTEGER NOT NULL,
                name          TEXT    NOT NULL,
                userid        INTEGER NOT NULL,
                channel_id    INTEGER NOT NULL,
                address       TEXT    NOT NULL DEFAULT '',
                ping_ms       REAL    NOT NULL DEFAULT 0,
                loss_pct      REAL    NOT NULL DEFAULT 0,
                bandwidth_bps INTEGER NOT NULL DEFAULT 0,
                tcp_only      INTEGER NOT NULL DEFAULT 0
            )
            """,
            # prune() laeuft im Betrieb regelmaessig ueber ts.
            "CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts)",
            # history() und sparkline() fragen immer (name, Zeitfenster).
            "CREATE INDEX IF NOT EXISTS samples_name_ts ON samples (name, ts)",
            """
            CREATE TABLE IF NOT EXISTS audit (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     INTEGER NOT NULL,
                actor  TEXT    NOT NULL,
                action TEXT    NOT NULL,
                target TEXT    NOT NULL DEFAULT '',
                before TEXT    NOT NULL DEFAULT '',
                after  TEXT    NOT NULL DEFAULT '',
                ok     INTEGER NOT NULL DEFAULT 1,
                error  TEXT    NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS audit_actor ON audit (actor)",
            "CREATE INDEX IF NOT EXISTS audit_action ON audit (action)",
            """
            CREATE TABLE IF NOT EXISTS notes (
                kind       TEXT    NOT NULL,
                key        TEXT    NOT NULL,
                text       TEXT    NOT NULL DEFAULT '',
                author     TEXT    NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (kind, key)
            )
            """,
        ),
    ),
    (
        2,
        (
            # loss_pct wird nullbar.
            #
            # "nicht gemessen" und "kein Verlust" sind zwei verschiedene
            # Aussagen, und die zweite ist eine Entwarnung. Ohne Monitor-Bot --
            # oder ohne dessen Ban-Recht am Wurzelkanal -- gibt es ueberhaupt
            # keine Verlustzahlen; als 0 gespeichert zeichnete die Sparkline
            # daraus eine makellose Nulllinie. sparkline() laesst Luecken
            # bewusst als None stehen und app.js zeichnet sie als
            # Unterbrechung; NOT NULL nahm beiden die Grundlage.
            #
            # SQLite kann NOT NULL nicht per ALTER entfernen -- deshalb der
            # Umbau ueber eine Zwischentabelle. Reihenfolge: erst die alte
            # Tabelle samt ihrer Indizes wegwerfen, dann die Indizes neu
            # anlegen. Ein Index behaelt beim RENAME seinen Namen und haengt
            # weiter an der alten Tabelle; CREATE INDEX IF NOT EXISTS waere
            # sonst still ein Nichtstun.
            "ALTER TABLE samples RENAME TO samples_alt",
            """
            CREATE TABLE samples (
                ts            INTEGER NOT NULL,
                session       INTEGER NOT NULL,
                name          TEXT    NOT NULL,
                userid        INTEGER NOT NULL,
                channel_id    INTEGER NOT NULL,
                address       TEXT    NOT NULL DEFAULT '',
                ping_ms       REAL    NOT NULL DEFAULT 0,
                loss_pct      REAL,
                bandwidth_bps INTEGER NOT NULL DEFAULT 0,
                tcp_only      INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            INSERT INTO samples (
                ts, session, name, userid, channel_id, address,
                ping_ms, loss_pct, bandwidth_bps, tcp_only
            )
            SELECT ts, session, name, userid, channel_id, address,
                   ping_ms, loss_pct, bandwidth_bps, tcp_only
            FROM samples_alt
            """,
            "DROP TABLE samples_alt",
            "CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts)",
            "CREATE INDEX IF NOT EXISTS samples_name_ts ON samples (name, ts)",
        ),
    ),
    (
        3,
        (
            # Der Wunschzustand fuer das, was murmur selbst nicht behaelt.
            #
            # Drei Dinge ueberleben in Mumble keine Verbindung: der Platz, auf
            # dem jemand landen soll (``enum UserInfo`` hat kein Kanalfeld),
            # dauerhaftes Mithoeren (``startListening`` nimmt eine Sitzung) und
            # Priority Speaker (ein Flag am verbundenen Client). Wer das
            # trotzdem verlaesslich haben will, muss es selbst hinterlegen und
            # nach jedem Verbinden neu setzen -- das tut der Enforcer.
            #
            # Das Ziel steht als **Pfad**, nicht als Kanal-ID: IDs vergibt
            # murmur neu, sobald ein Kanal geloescht und wieder angelegt wird.
            # Nach dem Einspielen einer Sicherung zeigte eine gespeicherte ID
            # sonst auf den falschen Platz oder ins Leere.
            """
            CREATE TABLE IF NOT EXISTS wunsch (
                art        TEXT    NOT NULL,
                userid     INTEGER NOT NULL,
                ziel       TEXT    NOT NULL DEFAULT '',
                author     TEXT    NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (art, userid, ziel)
            )
            """,
            "CREATE INDEX IF NOT EXISTS wunsch_art ON wunsch (art)",
        ),
    ),
    (
        4,
        (
            # Verbindungen zwischen zwei Plaetzen.
            #
            # Das ist die Frage, die eine Intercom stellt: "Kampfgericht 1 soll
            # die Zeitmessung hoeren." Mumble kennt sie nicht als solche -- dort
            # zerfaellt sie in ein Recht am *Ziel* und, beim Mithoeren,
            # zusaetzlich in ein startListening je Sitzung, das kein Trennen
            # ueberlebt. Die Absicht selbst hat dort keinen Ort, also hier.
            #
            # Wieder als Pfad, nicht als ID: siehe die Tabelle wunsch.
            """
            CREATE TABLE IF NOT EXISTS verbindung (
                art        TEXT    NOT NULL,
                von        TEXT    NOT NULL,
                nach       TEXT    NOT NULL,
                author     TEXT    NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (art, von, nach)
            )
            """,
            "CREATE INDEX IF NOT EXISTS verbindung_von ON verbindung (art, von)",
        ),
    ),
    (
        5,
        (
            # Netzsegmente.
            #
            # Sie standen als `networks:` in der intercom.yaml. Seit der Server
            # die Wahrheit haelt, gibt es die Datei im Normalfall nicht mehr --
            # und damit blieb die Spalte "Segment" im Cockpit immer leer und
            # der Hinweis darunter zeigte auf etwas, das nicht existiert.
            #
            # Die Reihenfolge entscheidet: die erste passende Maske gewinnt, wie
            # in einer Routingtabelle. Deshalb eine Spalte dafuer.
            """
            CREATE TABLE IF NOT EXISTS netz (
                name       TEXT    NOT NULL PRIMARY KEY,
                cidr       TEXT    NOT NULL,
                notiz      TEXT    NOT NULL DEFAULT '',
                rang       INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """,
        ),
    ),
    (
        6,
        (
            # Ruftasten: zentral belegte Tasten je Platz.
            #
            # Profisysteme legen in der Konfigurationssoftware fest, was Taste 1
            # bis 4 am Beltpack tut. Mumble kennt das nicht -- die Tasten stehen
            # im Client. Der Umweg: jeder Client ruft mit seiner Taste n immer
            # dieselbe feste Gruppe ``rufn``, und der Server leitet diese Gruppe
            # je Sitzung per ``redirectWhisperGroup`` auf die Rolle um, die an
            # dem Platz gerade gerufen werden soll. Gegen murmur v1.5.735
            # gemessen: 7 von 7 Faellen wie erwartet (DECISIONS D-032).
            #
            # Die Umleitung ueberlebt kein Trennen; die Belegung selbst wohnt
            # deshalb hier, als Pfad wie alles andere.
            """
            CREATE TABLE IF NOT EXISTS ruftaste (
                platz      TEXT    NOT NULL,
                taste      INTEGER NOT NULL,
                rolle      TEXT    NOT NULL,
                author     TEXT    NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (platz, taste)
            )
            """,
        ),
    ),
    (
        7,
        (
            # Shows: mehrere benannte Aufbauten statt einer einzigen Sicherung.
            #
            # Eine Show ist genau das, was eine Sicherung ist -- der Text, den
            # der Export liefert, samt der Abschnitte aus dieser Oberflaeche
            # (feste Plaetze, Verbindungen, Ruftasten, Netze). Gespeichert wird
            # der Text, nicht eine zerlegte Form: so laedt eine Show genau
            # denselben Weg wie eine hochgeladene Datei, und was man
            # herunterlaedt, ist Byte fuer Byte das, was geladen wuerde.
            #
            # ``geladen`` ist nur eine Auskunft ("zuletzt geladen am ..."), kein
            # Zustand: der Server bleibt die Wahrheit, und wer nach dem Laden
            # etwas aendert, hat eben etwas geaendert.
            """
            CREATE TABLE IF NOT EXISTS show (
                name       TEXT    NOT NULL PRIMARY KEY,
                notiz      TEXT    NOT NULL DEFAULT '',
                yaml_text  TEXT    NOT NULL,
                author     TEXT    NOT NULL DEFAULT '',
                erstellt   INTEGER NOT NULL,
                geaendert  INTEGER NOT NULL,
                geladen    INTEGER,
                geladen_von TEXT   NOT NULL DEFAULT ''
            )
            """,
        ),
    ),
)

#: Stand, den :meth:`Store.migrate` herstellt. Abgeleitet statt gepflegt -- eine
#: von Hand nachgezogene Zahl laeuft frueher oder spaeter aus dem Tritt.
SCHEMA_VERSION: Final[int] = max(nummer for nummer, _ in _MIGRATIONS)

_INSERT_SAMPLE: Final[str] = """
    INSERT INTO samples (
        ts, session, name, userid, channel_id, address,
        ping_ms, loss_pct, bandwidth_bps, tcp_only
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SAMPLE_COLUMNS: Final[str] = (
    "ts, session, name, userid, channel_id, address, "
    "ping_ms, loss_pct, bandwidth_bps, tcp_only"
)

_AUDIT_COLUMNS: Final[str] = "id, ts, actor, action, target, before, after, ok, error"


#: Laengengrenzen fuer das Audit-Log. Kurzfelder sind Name, Aktion und Ziel;
#: Langfelder das Vorher/Nachher als JSON. Grosszuegig genug, dass eine echte
#: ACL-Aenderung vollstaendig hineinpasst.
MAX_KURZFELD = 200
MAX_LANGFELD = 20_000
#: Obergrenze fuer den Text einer Show. Ein Stadion mit 200 Plaetzen und
#: 500 Personen liegt bei gut 100 kB; 2 MB sind reichlich und halten trotzdem
#: einen versehentlich hochgeladenen Mitschnitt aus der Datenbank.
MAX_SHOWTEXT = 2_000_000
MAX_SHOWNAME = 80

#: Die drei Dinge, die murmur selbst nicht behaelt und die deshalb hier wohnen.
#: ``platz``: wo die Person nach dem Verbinden landen soll.
#: ``mithoeren``: welche Plaetze sie dauerhaft mithoeren soll.
#: ``vorrang``: auf welchen Plaetzen sie Priority Speaker sein soll.
WUNSCH_ARTEN: Final[tuple[str, ...]] = ("platz", "mithoeren", "vorrang")

#: Arten von Verbindungen zwischen zwei Plaetzen.
#: ``hoert``: wer auf dem einen Platz sitzt, hoert den anderen mit.
#: ``reinschalten``: wer auf dem einen Platz sitzt, darf in den anderen
#: hineinsprechen, ohne ihn zu betreten.
VERBINDUNGSARTEN: Final[tuple[str, ...]] = ("hoert", "reinschalten")

#: Wie viele Ruftasten es gibt. Vier, wie die Kanaltasten an einem Beltpack --
#: mehr ist am Geraet ohnehin nicht zu greifen.
RUFTASTEN: Final[tuple[int, ...]] = (1, 2, 3, 4)


def ruf_gruppe(taste: int) -> str:
    """Der feste Gruppenname, auf den Taste ``taste`` im Client ruft."""
    return f"ruf{taste}"


def _unterhalb(pfad: str) -> tuple[int, str]:
    """Parameter fuer "liegt unter diesem Pfad": ``substr(spalte, 1, n) = praefix``.

    Bewusst nicht ``LIKE pfad || '/%'``: dort sind ``_`` und ``%`` Platzhalter,
    und ein Umbenennen von "KG_1" haette auch "KGA1/..." mitgezogen.
    """
    praefix = pfad + "/"
    return len(praefix), praefix


def _kappen(text: str, grenze: int) -> str:
    """Kuerzt zu lange Werte und macht die Kuerzung sichtbar."""
    if not text:
        return ""
    if len(text) <= grenze:
        return text
    return text[: grenze - 15] + "\u2026 [gekuerzt]"


class StoreClosed(RuntimeError):
    """Zugriff auf einen bereits geschlossenen Store.

    Ein geschlossener Store wird nicht wiederbelebt: der Prozess baut beim Start
    genau einen und schliesst ihn beim Herunterfahren.
    """


# --------------------------------------------------------------------------- #
#  Modelle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Sample:
    """Eine Messung fuer einen Client zu einem Zeitpunkt.

    ``ts`` sind Unix-Sekunden. Der Aufrufer setzt sie fuer alle Zeilen eines
    Durchlaufs auf denselben Wert -- so gehoeren die Messungen eines Rasters
    sichtbar zusammen, auch wenn das Einsammeln ueber alle Clients eine Sekunde
    dauert.
    """

    ts: int
    session: int
    name: str
    userid: int
    channel_id: int
    address: str = ""
    ping_ms: float = 0.0
    #: ``None`` heisst "nicht gemessen" -- nicht "kein Verlust". Ohne
    #: Monitor-Bot gibt es diese Zahl gar nicht.
    loss_pct: float | None = None
    bandwidth_bps: int = 0
    tcp_only: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Eine Zeile des Audit-Logs.

    ``before`` und ``after`` halten **JSON-Text**, den der Aufrufer selbst
    serialisiert (``json.dumps``). Der Store schaut nicht hinein: was ein
    sinnvoller Vorher/Nachher-Vergleich ist, weiss nur die aufrufende Stelle --
    bei einer ACL-Aenderung die Rechteliste, beim Verschieben eines Nutzers der
    Kanalname. Leerer String heisst "nichts festgehalten".

    Passwoerter, ``ICE_SECRET`` und ``SESSION_SECRET`` gehoeren **nicht** in
    diese Felder. Der Aufrufer entfernt sie, bevor er serialisiert.
    """

    id: int
    ts: int
    actor: str
    action: str
    target: str = ""
    before: str = ""
    after: str = ""
    ok: bool = True
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Note:
    """Eine freie Notiz, z. B. "Headset kratzt" an einem Geraet."""

    kind: str
    key: str
    text: str
    author: str
    updated_at: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
#  Hilfsfunktionen
# --------------------------------------------------------------------------- #


def _escape_like(term: str) -> str:
    """Macht ``%`` und ``_`` in einem Suchbegriff harmlos.

    Ohne das wuerde die Suche nach ``foo_bar`` auch ``fooXbar`` finden.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_filter(
    actor: str | None, action: str | None, search: str | None
) -> tuple[str, list[Any]]:
    """Baut die WHERE-Klausel fuer Liste und Zaehlung -- eine Quelle fuer beide.

    Sonst laufen Blaetterei und Gesamtzahl frueher oder spaeter auseinander.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if search:
        pattern = f"%{_escape_like(search)}%"
        clauses.append(
            "(target LIKE ? ESCAPE '\\' OR before LIKE ? ESCAPE '\\'"
            " OR after LIKE ? ESCAPE '\\' OR error LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _sample_from_row(row: sqlite3.Row) -> Sample:
    return Sample(
        ts=row["ts"],
        session=row["session"],
        name=row["name"],
        userid=row["userid"],
        channel_id=row["channel_id"],
        address=row["address"],
        ping_ms=row["ping_ms"],
        loss_pct=row["loss_pct"],
        bandwidth_bps=row["bandwidth_bps"],
        tcp_only=bool(row["tcp_only"]),
    )


def _audit_from_row(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        ts=row["ts"],
        actor=row["actor"],
        action=row["action"],
        target=row["target"],
        before=row["before"],
        after=row["after"],
        ok=bool(row["ok"]),
        error=row["error"],
    )


# --------------------------------------------------------------------------- #
#  Store
# --------------------------------------------------------------------------- #


class Store:
    """Zugriff auf die SQLite-Datei. Thread-sicher, siehe Modul-Kopf."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        #: Verbindung des jeweiligen Threads.
        self._local = threading.local()
        #: Alle offenen Verbindungen, damit close() sie auch dann abbauen kann,
        #: wenn der erzeugende Thread schon beendet ist.
        self._connections: list[sqlite3.Connection] = []
        self._registry_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ #
    #  Verbindung
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> None:
        """Oeffnet die Verbindung des aufrufenden Threads.

        Idempotent. Der Aufruf ist optional -- jeder Zugriff oeffnet die
        Verbindung seines Threads bei Bedarf selbst. Ausdruecklich aufgerufen
        wird er beim Start, damit ein unbrauchbarer Pfad (fehlendes ``/data``,
        keine Schreibrechte) sofort auffaellt und nicht erst beim ersten
        Messwert.
        """
        self._connection()

    def close(self) -> None:
        """Schliesst alle Verbindungen, auch die fremder Threads.

        Wartet auf eine laufende Schreib-Transaktion, damit kein Durchlauf halb
        geschrieben abgeschnitten wird. Auf einen laufenden *Lesevorgang* wird
        nicht gewartet -- Leser halten kein Lock. Der Aufrufer beendet deshalb
        erst den Monitor-Bot und die Web-Tasks und dann den Store; wer danach
        noch zugreift, bekommt :class:`StoreClosed`.
        """
        with self._write_lock:
            with self._registry_lock:
                self._closed = True
                connections = list(self._connections)
                self._connections.clear()
            for conn in connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    log.debug("Verbindung liess sich nicht schliessen", exc_info=True)
            self._local = threading.local()

    def __enter__(self) -> Store:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreClosed(f"Der Store zu {self._path} ist bereits geschlossen.")
        existing: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if existing is not None:
            return existing

        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            # Nicht zum Teilen der Verbindung zwischen Threads -- jeder Thread
            # hat seine eigene -- sondern damit close() sie am Ende auch aus
            # einem anderen Thread heraus abbauen darf.
            check_same_thread=False,
            # Kein implizites BEGIN des sqlite3-Moduls, siehe Modul-Kopf.
            isolation_level=None,
            timeout=_BUSY_TIMEOUT_S,
        )
        conn.row_factory = sqlite3.Row
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        # NORMAL statt FULL: bei WAL kostet ein Absturz hoechstens die letzten
        # Transaktionen seit dem letzten Checkpoint. Fuer Messreihen und ein
        # Audit-Log ist das der richtige Tausch gegen ein fsync pro Commit.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        with self._registry_lock:
            if self._closed:
                conn.close()
                raise StoreClosed(f"Der Store zu {self._path} ist bereits geschlossen.")
            first = not self._connections
            self._connections.append(conn)
        self._local.conn = conn

        if first and str(journal).lower() != "wal":
            log.warning(
                "SQLite laeuft im Journal-Modus %r statt WAL. Auf einer "
                "Netzfreigabe geht WAL nicht; Lesen im Cockpit und Schreiben "
                "des Monitor-Bots blockieren sich dann gegenseitig. %s liegt "
                "besser auf einem lokalen Datentraeger.",
                journal,
                self._path,
            )
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Eine Schreib-Transaktion, prozessweit serialisiert."""
        # Die Verbindung wird vor dem Lock geholt: die erste eines Threads
        # aufzubauen kostet Datei-I/O und PRAGMAs, und das haette jeden anderen
        # Schreiber mit aufgehalten.
        conn = self._connection()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    # ------------------------------------------------------------------ #
    #  Schema
    # ------------------------------------------------------------------ #

    def migrate(self) -> int:
        """Bringt die Datei auf :data:`SCHEMA_VERSION` und gibt den Stand zurueck.

        Idempotent: ein zweiter Aufruf findet die Schritte in ``schema_version``
        wieder und tut nichts. Alles laeuft in einer Transaktion -- SQLite kann
        auch DDL zuruecknehmen, ein Abbruch mitten im Schritt hinterlaesst also
        keine halbe Tabelle.
        """
        with self._transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                )
                """
            )
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            now = int(time.time())
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, now),
                )
                log.info("Schema-Schritt %d angewendet (%s)", version, self._path)
                current = version
        return current

    def schema_version(self) -> int:
        """Stand der Datei. 0 heisst: noch nie migriert."""
        conn = self._connection()
        known = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if known is None:
            return 0
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] or 0

    # ------------------------------------------------------------------ #
    #  Metrik-Verlauf
    # ------------------------------------------------------------------ #

    def record_samples(self, rows: Sequence[Sample]) -> None:
        """Schreibt einen ganzen Messdurchlauf in **einer** Transaktion.

        Der Monitor-Bot ruft das alle paar Sekunden fuer alle Clients auf. Ein
        Commit je Zeile waere ein fsync je Zeile; ``executemany`` in einer
        Transaktion macht daraus einen. Eine leere Liste kostet nichts.
        """
        if not rows:
            return
        payload = [
            (
                int(row.ts),
                int(row.session),
                row.name,
                int(row.userid),
                int(row.channel_id),
                row.address,
                float(row.ping_ms),
                None if row.loss_pct is None else float(row.loss_pct),
                int(row.bandwidth_bps),
                int(row.tcp_only),
            )
            for row in rows
        ]
        with self._transaction() as conn:
            conn.executemany(_INSERT_SAMPLE, payload)

    def history(self, name: str, minutes: int = 60) -> list[Sample]:
        """Verlauf eines Clients, aelteste Messung zuerst.

        Gesucht wird ueber den **Namen**, nicht ueber die Session. Eine Session
        ist die laufende Nummer einer Verbindung und wird bei jedem Reconnect
        neu vergeben -- genau dann, wenn es interessant wird. Wer nach Session
        fragte, saehe den Verlauf beim WLAN-Abriss der Kamera 2 abreissen und
        gleich daneben als neue Reihe wieder beginnen. Der Name ist auf dem
        Server eindeutig (murmur laesst keine zwei gleichen Namen zu) und
        ueberlebt den Reconnect.
        """
        cutoff = int(time.time()) - max(0, minutes) * 60
        conn = self._connection()
        rows = conn.execute(
            f"SELECT {_SAMPLE_COLUMNS} FROM samples "
            "WHERE name = ? AND ts >= ? ORDER BY ts, rowid",
            (name, cutoff),
        ).fetchall()
        return [_sample_from_row(row) for row in rows]

    def sparkline(
        self,
        name: str,
        field: str,
        points: int = 60,
        minutes: int = 60,
    ) -> list[float | None]:
        """Gleichmaessig gerasterte Reihe fuer die Sparkline im Cockpit.

        Das Fenster sind die letzten ``minutes`` Minuten bis **jetzt**, geteilt
        in ``points`` gleich breite Faecher; je Fach der Mittelwert von
        ``field``. Das Ergebnis hat immer genau ``points`` Werte, aeltester
        zuerst -- so ist die x-Achse zweier Sparklines nebeneinander vergleichbar.

        **Luecken bleiben ``None``, es wird nicht interpoliert.** Ein Fach ohne
        Messung heisst: der Client war nicht da. Eine durchgezogene Linie ueber
        diese Luecke wuerde einen Ausfall zu einer gesunden Reihe glaetten --
        also genau das verstecken, wonach in der Halbzeitpause gesucht wird. Der
        Aufrufer zeichnet die Luecke als Unterbrechung.

        ``field`` muss in :data:`SPARKLINE_FIELDS` stehen; der Name wandert in
        den SQL-Text und darf deshalb nicht vom Anwender kommen.
        """
        if field not in SPARKLINE_FIELDS:
            raise ValueError(
                f"{field!r} ist keine zeichenbare Spalte. Moeglich: "
                + ", ".join(sorted(SPARKLINE_FIELDS))
            )
        if points < 1:
            raise ValueError(f"points={points} -- eine Sparkline braucht mindestens 1 Fach.")
        if minutes < 1:
            raise ValueError(f"minutes={minutes} -- das Fenster waere leer.")

        end = int(time.time())
        span = minutes * 60
        start = end - span
        conn = self._connection()
        rows = conn.execute(
            f"""
            SELECT MIN(CAST((ts - ?) * ? / ? AS INTEGER), ?) AS fach,
                   AVG({field}) AS wert
              FROM samples
             WHERE name = ? AND ts >= ? AND ts <= ?
             GROUP BY fach
            """,
            (start, points, span, points - 1, name, start, end),
        ).fetchall()
        series: list[float | None] = [None] * points
        for row in rows:
            wert = row["wert"]
            # AVG() ueber lauter NULL ist NULL: in dem Fach gibt es Messungen,
            # aber keine dieser Spalte -- typisch fuer loss_pct ohne
            # Monitor-Bot. Das Fach bleibt eine Luecke.
            if wert is None:
                continue
            series[row["fach"]] = float(wert)
        return series

    def prune(self, retention_hours: int) -> int:
        """Loescht Messungen aelter als ``retention_hours`` und zaehlt sie.

        Betrifft nur ``samples``. Das Audit-Log wird nie automatisch beschnitten
        -- ein Protokoll, das sich selbst aufraeumt, ist keins; es waechst
        langsam genug (eine Zeile je Bedienhandlung).

        Kein ``VACUUM``: das sperrt die ganze Datei fuer die Dauer des Umbaus,
        und der Aufruf kommt aus einem Hintergrund-Task waehrend das Cockpit
        laeuft. Die frei gewordenen Seiten benutzt SQLite von selbst wieder; die
        Datei bleibt auf dem Hoechststand statt zu schrumpfen. Das ist bei einem
        gleitenden Fenster genau richtig.
        """
        cutoff = int(time.time()) - max(0, retention_hours) * 3600
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            removed = cursor.rowcount
        if removed:
            log.info("%d Messwerte aelter als %d h geloescht.", removed, retention_hours)
        return removed

    # ------------------------------------------------------------------ #
    #  Audit-Log
    # ------------------------------------------------------------------ #

    def audit(
        self,
        actor: str,
        action: str,
        target: str,
        before: str = "",
        after: str = "",
        ok: bool = True,
        error: str = "",
    ) -> int:
        """Haelt eine Bedienhandlung fest und gibt die neue Zeilennummer zurueck.

        ``before``/``after`` sind JSON-Text, den der Aufrufer serialisiert --
        siehe :class:`AuditEntry`. Auch gescheiterte Versuche werden
        geschrieben (``ok=False`` mit ``error``): dass jemand etwas *versucht*
        hat, ist fuer die Nachschau so wichtig wie der Erfolg.

        Alle Felder werden gekappt. Ein Teil davon stammt aus Eingaben, die
        **vor** jeder Anmeldung entstehen -- der Benutzername eines
        fehlgeschlagenen Logins etwa. Ohne Deckel liesse sich diese Datei von
        aussen vollschreiben, und sie ist dieselbe wie der Metrik-Verlauf:
        laeuft sie voll, sind waehrend der Veranstaltung Verlaufsgrafik **und**
        Protokoll tot.
        """
        actor = _kappen(actor, MAX_KURZFELD)
        action = _kappen(action, MAX_KURZFELD)
        target = _kappen(target, MAX_KURZFELD)
        before = _kappen(before, MAX_LANGFELD)
        after = _kappen(after, MAX_LANGFELD)
        error = _kappen(error, MAX_KURZFELD)

        now = int(time.time())
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO audit (ts, actor, action, target, before, after, ok, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, actor, action, target, before, after, int(ok), error),
            )
            new_id = cursor.lastrowid
        return int(new_id or 0)

    def audit_entries(
        self,
        limit: int = 200,
        offset: int = 0,
        actor: str | None = None,
        action: str | None = None,
        search: str | None = None,
    ) -> list[AuditEntry]:
        """Eine Seite des Audit-Logs, neueste zuerst.

        ``search`` sucht in ``target``, ``before``, ``after`` und ``error`` --
        den freien Feldern. ``actor`` und ``action`` sind exakte Filter, weil
        das Cockpit sie als Auswahlliste anbietet.

        Sortiert wird nach ``ts`` und bei Gleichstand nach ``id``. Innerhalb
        einer Sekunde koennen mehrere Handlungen liegen; ohne den zweiten
        Schluessel waere die Reihenfolge beliebig und dieselbe Seite zweimal
        abgerufen saehe anders aus.
        """
        where, params = _audit_filter(actor, action, search)
        conn = self._connection()
        rows = conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM audit{where} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*params, max(0, limit), max(0, offset)),
        ).fetchall()
        return [_audit_from_row(row) for row in rows]

    def audit_count(
        self,
        actor: str | None = None,
        action: str | None = None,
        search: str | None = None,
    ) -> int:
        """Zahl der Treffer zu denselben Filtern -- fuer die Blaetterei."""
        where, params = _audit_filter(actor, action, search)
        conn = self._connection()
        row = conn.execute(f"SELECT COUNT(*) FROM audit{where}", tuple(params)).fetchone()
        return int(row[0])

    def audit_actors(self) -> list[str]:
        """Alle vorkommenden Bediener, fuer die Auswahlliste des Filters."""
        conn = self._connection()
        rows = conn.execute("SELECT DISTINCT actor FROM audit ORDER BY actor").fetchall()
        return [row[0] for row in rows]

    def audit_actions(self) -> list[str]:
        """Alle vorkommenden Handlungen, fuer die Auswahlliste des Filters."""
        conn = self._connection()
        rows = conn.execute("SELECT DISTINCT action FROM audit ORDER BY action").fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------ #
    #  Notizen
    # ------------------------------------------------------------------ #

    def set_note(self, kind: str, key: str, text: str, author: str) -> None:
        """Legt eine Notiz an oder ersetzt sie.

        ``kind`` ist die Art des Bezugs (``"user"``, ``"channel"``,
        ``"device"``), ``key`` der Name oder die ID darin. Beides zusammen ist
        der Schluessel -- eine Notiz je Bezug, keine Historie: das Cockpit zeigt
        einen Merkzettel, kein zweites Protokoll. Wer wissen will, wer sie
        zuletzt geaendert hat, findet ``author`` und ``updated_at`` daneben.
        """
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO notes (kind, key, text, author, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (kind, key) DO UPDATE SET
                    text = excluded.text,
                    author = excluded.author,
                    updated_at = excluded.updated_at
                """,
                (kind, key, text, author, int(time.time())),
            )

    def get_note(self, kind: str, key: str) -> Note | None:
        """Eine einzelne Notiz oder ``None``."""
        conn = self._connection()
        row = conn.execute(
            "SELECT kind, key, text, author, updated_at FROM notes "
            "WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if row is None:
            return None
        return Note(
            kind=row["kind"],
            key=row["key"],
            text=row["text"],
            author=row["author"],
            updated_at=row["updated_at"],
        )

    def notes(self, kind: str) -> dict[str, Note]:
        """Alle Notizen einer Art, nach ``key`` geschluesselt.

        Die Nutzertabelle im Cockpit holt sich damit alle Notizen in einem
        Aufruf statt einer Abfrage je Zeile.
        """
        conn = self._connection()
        rows = conn.execute(
            "SELECT kind, key, text, author, updated_at FROM notes WHERE kind = ?",
            (kind,),
        ).fetchall()
        return {
            row["key"]: Note(
                kind=row["kind"],
                key=row["key"],
                text=row["text"],
                author=row["author"],
                updated_at=row["updated_at"],
            )
            for row in rows
        }

    # ------------------------------------------------------------------ #
    #  Wunschzustand
    # ------------------------------------------------------------------ #

    def set_wunsch(
        self, art: str, userid: int, ziele: Sequence[str], author: str = ""
    ) -> None:
        """Setzt den Wunsch einer Person fuer eine Art -- und ersetzt den alten.

        ``ziele`` sind Kanalpfade. Eine leere Liste loescht den Wunsch; das ist
        der Weg, einen festen Platz wieder aufzuheben.
        """
        if art not in WUNSCH_ARTEN:
            raise ValueError(f"Unbekannte Wunschart {art!r}.")
        jetzt = int(time.time())
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM wunsch WHERE art = ? AND userid = ?", (art, userid)
            )
            conn.executemany(
                "INSERT INTO wunsch (art, userid, ziel, author, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (art, userid, ziel, _kappen(author, MAX_KURZFELD), jetzt)
                    for ziel in dict.fromkeys(ziele)
                    if ziel
                ],
            )

    def wuensche(self, art: str) -> dict[int, list[str]]:
        """Alle Wuensche einer Art, nach Nutzer-ID geschluesselt."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT userid, ziel FROM wunsch WHERE art = ? ORDER BY userid, ziel",
            (art,),
        ).fetchall()
        gesammelt: dict[int, list[str]] = {}
        for row in rows:
            gesammelt.setdefault(row["userid"], []).append(row["ziel"])
        return gesammelt

    def alle_wuensche(self) -> dict[str, dict[int, list[str]]]:
        """Der vollstaendige Wunschzustand -- so geht er in die Sicherung."""
        return {art: self.wuensche(art) for art in WUNSCH_ARTEN}

    def wuensche_leeren(self) -> int:
        """Loescht den ganzen Wunschzustand -- fuer "Laden mit Aufraeumen"."""
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM wunsch")
            return int(cur.rowcount or 0)

    def wunsch_vergessen(self, userid: int) -> int:
        """Loescht alle Wuensche einer Person. Nach dem Abmelden faellig."""
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM wunsch WHERE userid = ?", (userid,))
            return int(cur.rowcount or 0)

    def wunsch_umschreiben(self, alt: str, neu: str) -> int:
        """Zieht Wuensche mit, wenn ein Kanal umbenannt oder verschoben wird.

        Ohne das zeigte der gespeicherte Pfad nach jedem Umbenennen ins Leere --
        und die Oberflaeche behauptete einen festen Platz, den es nicht gibt.
        Unterpfade wandern mit: wer ``Wettkampf`` nach ``Meeting`` umbenennt,
        verschiebt auch ``Wettkampf/Technik``.
        """
        if not alt or alt == neu:
            return 0
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE wunsch SET ziel = ? || substr(ziel, ?) "
                "WHERE ziel = ? OR substr(ziel, 1, ?) = ?",
                (neu, len(alt) + 1, alt, *_unterhalb(alt)),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    #  Verbindungen zwischen Plaetzen
    # ------------------------------------------------------------------ #

    def set_verbindung(
        self, art: str, von: str, nach: str, *, an: bool, author: str = ""
    ) -> None:
        """Legt eine Verbindung an oder hebt sie auf."""
        if art not in VERBINDUNGSARTEN:
            raise ValueError(f"Unbekannte Verbindungsart {art!r}.")
        if not von or not nach:
            raise ValueError("Verbindungen brauchen zwei Plaetze.")
        if von == nach:
            raise ValueError("Ein Platz kann sich nicht mit sich selbst verbinden.")
        with self._transaction() as conn:
            if an:
                conn.execute(
                    "INSERT INTO verbindung (art, von, nach, author, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (art, von, nach) DO UPDATE SET "
                    "  author = excluded.author, updated_at = excluded.updated_at",
                    (art, von, nach, _kappen(author, MAX_KURZFELD), int(time.time())),
                )
            else:
                conn.execute(
                    "DELETE FROM verbindung WHERE art = ? AND von = ? AND nach = ?",
                    (art, von, nach),
                )

    def verbindungen(self, art: str | None = None) -> dict[str, dict[str, list[str]]]:
        """Alle Verbindungen, nach Art und Ausgangsplatz geschluesselt."""
        conn = self._connection()
        if art is None:
            rows = conn.execute(
                "SELECT art, von, nach FROM verbindung ORDER BY art, von, nach"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT art, von, nach FROM verbindung WHERE art = ? ORDER BY von, nach",
                (art,),
            ).fetchall()
        gesammelt: dict[str, dict[str, list[str]]] = {a: {} for a in VERBINDUNGSARTEN}
        for row in rows:
            gesammelt.setdefault(row["art"], {}).setdefault(row["von"], []).append(
                row["nach"]
            )
        return gesammelt

    def verbindungen_leeren(self) -> int:
        """Loescht alle Verbindungen -- fuer "Laden mit Aufraeumen"."""
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM verbindung")
            return int(cur.rowcount or 0)

    def verbindung_umschreiben(self, alt: str, neu: str) -> int:
        """Zieht Verbindungen mit, wenn ein Platz umbenannt oder verschoben wird."""
        if not alt or alt == neu:
            return 0
        geaendert = 0
        with self._transaction() as conn:
            for spalte in ("von", "nach"):
                cur = conn.execute(
                    f"UPDATE OR REPLACE verbindung SET {spalte} = ? || substr({spalte}, ?) "
                    f"WHERE {spalte} = ? OR substr({spalte}, 1, ?) = ?",
                    (neu, len(alt) + 1, alt, *_unterhalb(alt)),
                )
                geaendert += int(cur.rowcount or 0)
        return geaendert

    def verbindung_vergessen(self, pfad: str) -> int:
        """Loescht alle Verbindungen eines Platzes. Nach dem Loeschen faellig."""
        if not pfad:
            return 0
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM verbindung WHERE von = ? OR nach = ? "
                "OR substr(von, 1, ?) = ? OR substr(nach, 1, ?) = ?",
                (pfad, pfad, *_unterhalb(pfad), *_unterhalb(pfad)),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    #  Netzsegmente
    # ------------------------------------------------------------------ #

    def netze(self) -> list[dict[str, Any]]:
        """Alle Segmente in Auswertungsreihenfolge -- die erste Maske gewinnt."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT name, cidr, notiz, rang FROM netz ORDER BY rang, name"
        ).fetchall()
        return [
            {
                "name": row["name"],
                "cidr": row["cidr"],
                "notiz": row["notiz"],
                "rang": row["rang"],
            }
            for row in rows
        ]

    def set_netze(self, segmente: Sequence[Mapping[str, Any]]) -> None:
        """Ersetzt die Segmentliste vollstaendig.

        Ganz ersetzen statt einzeln pflegen, weil die **Reihenfolge** Teil der
        Aussage ist: ein Client landet im ersten passenden Segment. Wer einzelne
        Zeilen anlegte und loeschte, muesste die Reihenfolge trotzdem im Ganzen
        schreiben -- dann kann es gleich ein Vorgang sein.
        """
        jetzt = int(time.time())
        zeilen = []
        for rang, eintrag in enumerate(segmente):
            name = str(eintrag.get("name", "")).strip()
            cidr = str(eintrag.get("cidr", "")).strip()
            if not name or not cidr:
                continue
            # Sprechender Fehler statt einer stillen Zeile, die nie trifft.
            ipaddress.ip_network(cidr, strict=False)
            zeilen.append(
                (name, cidr, str(eintrag.get("notiz", "")).strip(), rang, jetzt)
            )
        with self._transaction() as conn:
            conn.execute("DELETE FROM netz")
            conn.executemany(
                "INSERT INTO netz (name, cidr, notiz, rang, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                zeilen,
            )

    # ------------------------------------------------------------------ #
    #  Ruftasten
    # ------------------------------------------------------------------ #

    def set_ruftaste(
        self, platz: str, taste: int, rolle: str | None, author: str = ""
    ) -> None:
        """Belegt eine Taste an einem Platz -- oder gibt sie frei (``rolle=None``).

        ``platz`` ist ein Pfad; der leere Pfad ist der oberste Platz
        ("Ueberall"), eine Belegung dort gilt fuer alle Plaetze, die selbst
        nichts anderes festlegen.
        """
        if taste not in RUFTASTEN:
            raise ValueError(f"Es gibt keine Taste {taste}.")
        with self._transaction() as conn:
            if rolle:
                conn.execute(
                    "INSERT INTO ruftaste (platz, taste, rolle, author, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (platz, taste) DO UPDATE SET "
                    "rolle = excluded.rolle, author = excluded.author, "
                    "updated_at = excluded.updated_at",
                    (platz, taste, rolle, _kappen(author, MAX_KURZFELD), int(time.time())),
                )
            else:
                conn.execute(
                    "DELETE FROM ruftaste WHERE platz = ? AND taste = ?", (platz, taste)
                )

    def ruftasten(self) -> dict[str, dict[int, str]]:
        """Alle Belegungen: Platzpfad -> {Taste: Rolle}."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT platz, taste, rolle FROM ruftaste ORDER BY platz, taste"
        ).fetchall()
        gesammelt: dict[str, dict[int, str]] = {}
        for row in rows:
            gesammelt.setdefault(row["platz"], {})[int(row["taste"])] = row["rolle"]
        return gesammelt

    def ruftasten_ersetzen(self, belegung: Mapping[str, Mapping[int, str]]) -> None:
        """Ersetzt alle Belegungen -- fuer Sicherung und Shows."""
        jetzt = int(time.time())
        zeilen = [
            (platz, int(taste), rolle, "", jetzt)
            for platz, je_taste in belegung.items()
            for taste, rolle in je_taste.items()
            if int(taste) in RUFTASTEN and rolle
        ]
        with self._transaction() as conn:
            conn.execute("DELETE FROM ruftaste")
            conn.executemany(
                "INSERT INTO ruftaste (platz, taste, rolle, author, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                zeilen,
            )

    def ruftaste_umschreiben(self, alt: str, neu: str) -> int:
        """Zieht Belegungen mit, wenn ein Platz umbenannt oder verschoben wird."""
        if not alt or alt == neu:
            return 0
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE OR REPLACE ruftaste SET platz = ? || substr(platz, ?) "
                "WHERE platz = ? OR substr(platz, 1, ?) = ?",
                (neu, len(alt) + 1, alt, *_unterhalb(alt)),
            )
            return int(cur.rowcount or 0)

    def ruftaste_vergessen(self, pfad: str) -> int:
        """Loescht die Belegungen eines geloeschten Platzes samt allem darunter."""
        if not pfad:
            return 0
        with self._transaction() as conn:
            cur = conn.execute(
                "DELETE FROM ruftaste WHERE platz = ? OR substr(platz, 1, ?) = ?",
                (pfad, *_unterhalb(pfad)),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ #
    #  Shows
    # ------------------------------------------------------------------ #

    @staticmethod
    def _showname(name: str) -> str:
        sauber = " ".join(str(name).split())
        if not sauber:
            raise ValueError("Eine Show braucht einen Namen.")
        if len(sauber) > MAX_SHOWNAME:
            raise ValueError(f"Der Name ist zu lang (hoechstens {MAX_SHOWNAME} Zeichen).")
        return sauber

    def show_speichern(
        self,
        name: str,
        yaml_text: str,
        *,
        notiz: str = "",
        author: str = "",
        ueberschreiben: bool = False,
    ) -> str:
        """Legt eine Show an oder ersetzt ihren Inhalt. Gibt den Namen zurueck.

        Ohne ``ueberschreiben`` ist ein vorhandener Name ein Fehler
        (:class:`FileExistsError`) -- eine Show versehentlich durch eine
        gleichnamige zu ersetzen, waere ein stiller Verlust.
        """
        sauber = self._showname(name)
        if len(yaml_text.encode("utf-8")) > MAX_SHOWTEXT:
            raise ValueError("Die Show ist zu gross.")
        jetzt = int(time.time())
        with self._transaction() as conn:
            da = conn.execute(
                "SELECT 1 FROM show WHERE name = ?", (sauber,)
            ).fetchone()
            if da and not ueberschreiben:
                raise FileExistsError(sauber)
            if da:
                conn.execute(
                    "UPDATE show SET yaml_text = ?, notiz = ?, author = ?, geaendert = ? "
                    "WHERE name = ?",
                    (yaml_text, _kappen(notiz, MAX_LANGFELD),
                     _kappen(author, MAX_KURZFELD), jetzt, sauber),
                )
            else:
                conn.execute(
                    "INSERT INTO show (name, notiz, yaml_text, author, erstellt, geaendert) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sauber, _kappen(notiz, MAX_LANGFELD), yaml_text,
                     _kappen(author, MAX_KURZFELD), jetzt, jetzt),
                )
        return sauber

    def shows(self) -> list[dict[str, Any]]:
        """Alle Shows ohne ihren Text, zuletzt geaenderte zuerst."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT name, notiz, author, erstellt, geaendert, geladen, geladen_von, "
            "length(yaml_text) AS groesse FROM show ORDER BY geaendert DESC, name"
        ).fetchall()
        return [dict(row) for row in rows]

    def show(self, name: str) -> dict[str, Any] | None:
        """Eine Show samt Text -- oder ``None``."""
        conn = self._connection()
        row = conn.execute(
            "SELECT name, notiz, yaml_text, author, erstellt, geaendert, geladen, "
            "geladen_von FROM show WHERE name = ?",
            (" ".join(str(name).split()),),
        ).fetchone()
        return dict(row) if row else None

    def show_geladen(self, name: str, von: str) -> None:
        """Vermerkt, dass eine Show gerade geladen wurde."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE show SET geladen = ?, geladen_von = ? WHERE name = ?",
                (int(time.time()), _kappen(von, MAX_KURZFELD), name),
            )

    def show_aendern(
        self, name: str, *, neuer_name: str | None = None, notiz: str | None = None
    ) -> str:
        """Benennt eine Show um und/oder aendert ihre Notiz."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM show WHERE name = ?", (name,)).fetchone() is None:
                raise KeyError(name)
            ziel = name
            if neuer_name is not None:
                ziel = self._showname(neuer_name)
                if ziel != name and conn.execute(
                    "SELECT 1 FROM show WHERE name = ?", (ziel,)
                ).fetchone():
                    raise FileExistsError(ziel)
                conn.execute("UPDATE show SET name = ? WHERE name = ?", (ziel, name))
            if notiz is not None:
                conn.execute(
                    "UPDATE show SET notiz = ?, geaendert = ? WHERE name = ?",
                    (_kappen(notiz, MAX_LANGFELD), int(time.time()), ziel),
                )
            return ziel

    def show_loeschen(self, name: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM show WHERE name = ?", (name,))
            return bool(cur.rowcount)
