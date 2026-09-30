"""Rechnet aus, was eine Rolle oder eine Person in einem Kanal wirklich darf.

Warum ueberhaupt nachrechnen? ``Server::effectivePermissions`` beantwortet
genau diese Frage -- aber nur fuer eine **verbundene Sitzung**. Die Oberflaeche
muss die Rechte aber auch dann zeigen, wenn niemand online ist: beim Aufbauen
einer Veranstaltung sitzt noch keiner im Kanal.

Deshalb ist das hier eine Portierung von ``ChanACL::effectivePermissions``
(src/ACL.cpp) und ``Group::appliesToUser`` (src/Group.cpp), Stand v1.5.735.
Drei Details daraus widersprechen dem, was man ueblicherweise annimmt, und
genau die machen den Unterschied zwischen einer richtigen und einer huebschen
Anzeige:

1. Die Kette laeuft **immer** bis zur Wurzel. ``bInheritACL`` bricht sie nicht
   ab, sondern setzt beim Abarbeiten die bis dahin gesammelten Rechte auf die
   Grundausstattung zurueck (``if (!ch->bInheritACL) granted = def;``).
2. Innerhalb eines Eintrags gilt erst ``allow``, dann ``deny`` -- ein Eintrag,
   der dasselbe Recht erlaubt und verbietet, **verbietet** es.
3. ``Traverse`` und ``Write`` werden unabhaengig von ``applyHere``/``applySubs``
   mitgefuehrt. Faellt ``Traverse`` unterwegs weg und ``Write`` ist nicht
   gesetzt, sind ab dort alle Rechte weg -- auch die des Zielkanals.

Was hier **nicht** nachgebildet wird, ist alles, was vom Zustand einer echten
Verbindung abhaengt: ``strong`` (Zertifikat geprueft), ``#token`` (Zugangswort)
und ``$hash`` (bestimmtes Zertifikat). Diese Angaben werden nicht geraten,
sondern als unbestimmt gefuehrt -- siehe :class:`Wirkung`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from .permissions import PERMISSIONS
from .types import ACLEntry, ChannelACL, MumbleChannel

# --------------------------------------------------------------------------- #
#  Konstanten aus ACL.h / ACL.cpp
# --------------------------------------------------------------------------- #

WRITE: Final = 0x1
TRAVERSE: Final = 0x2
ENTER: Final = 0x4
SPEAK: Final = 0x8
MUTE_DEAFEN: Final = 0x10
MOVE: Final = 0x20
MAKE_CHANNEL: Final = 0x40
LINK_CHANNEL: Final = 0x80
WHISPER: Final = 0x100
TEXT_MESSAGE: Final = 0x200
MAKE_TEMP_CHANNEL: Final = 0x400
LISTEN: Final = 0x800

KICK: Final = 0x10000
BAN: Final = 0x20000
REGISTER: Final = 0x40000
SELF_REGISTER: Final = 0x80000
RESET_USER_CONTENT: Final = 0x100000

CACHED: Final = 0x8000000

#: Grundausstattung, die jeder ohne jeden ACL-Eintrag hat (``Permissions def``).
GRUNDRECHTE: Final = TRAVERSE | ENTER | SPEAK | WHISPER | TEXT_MESSAGE | LISTEN

#: Rechte, die nur am Wurzelkanal vergeben werden und deshalb aus dem normalen
#: ``granted |= allow`` herausmaskiert sind.
NUR_WURZEL: Final = KICK | BAN | REGISTER | SELF_REGISTER | RESET_USER_CONTENT

#: Was ``Write`` mitbringt. Bemerkenswert: **weder Sprechen noch Fluestern**.
#: Ein Admin darf alles verwalten, aber nicht ungefragt ueberall reinreden.
WRITE_IMPLIZIERT: Final = (
    TRAVERSE
    | ENTER
    | MUTE_DEAFEN
    | MOVE
    | MAKE_CHANNEL
    | LINK_CHANNEL
    | TEXT_MESSAGE
    | MAKE_TEMP_CHANNEL
    | LISTEN
)

#: Jedes Bit, das ueberhaupt vorkommt -- Obergrenze fuer die Einklammerung.
ALLE_BITS: Final = (
    WRITE
    | TRAVERSE
    | ENTER
    | SPEAK
    | MUTE_DEAFEN
    | MOVE
    | MAKE_CHANNEL
    | LINK_CHANNEL
    | WHISPER
    | TEXT_MESSAGE
    | MAKE_TEMP_CHANNEL
    | LISTEN
    | KICK
    | BAN
    | REGISTER
    | SELF_REGISTER
    | RESET_USER_CONTENT
)

#: Gruppenangaben, deren Zutreffen sich ohne verbundene Sitzung nicht
#: entscheiden laesst: ``strong`` haengt am geprueften Zertifikat, ``#wort`` am
#: Zugangswort des Clients, ``$hash`` an dessen Zertifikatsabdruck. Sie werden
#: nicht geraten, sondern eingeklammert -- siehe :func:`wirksame_rechte`.
UNBESTIMMTE_ANGABEN: Final[frozenset[str]] = frozenset({"strong"})

#: Wie viele verschiedene unbestimmte Angaben eine Kette haben darf, bevor die
#: vollstaendige Einklammerung (2^n Durchlaeufe) durch die grobe ersetzt wird.
#: In der Praxis ist n null oder eins; zehn ist eine Reissleine, keine Grenze.
MAX_UNBESTIMMTE: Final = 10


def _zerlege(angabe: str) -> tuple[str, bool, bool, bool, bool]:
    """Spaltet eine Gruppenangabe in ihre Bestandteile.

    Gibt ``(name, invertiert, am_acl_kanal, ist_token, ist_hash)`` zurueck --
    dieselbe Schleife wie am Kopf von ``Group::appliesToUser``.
    """
    invertiert = am_acl_kanal = ist_token = ist_hash = False
    while angabe:
        if angabe.startswith("!"):
            invertiert = True
            angabe = angabe[1:]
        elif angabe.startswith("~"):
            am_acl_kanal = True
            angabe = angabe[1:]
        elif angabe.startswith("#"):
            ist_token = True
            angabe = angabe[1:]
        elif angabe.startswith("$"):
            ist_hash = True
            angabe = angabe[1:]
        else:
            break
    return angabe, invertiert, am_acl_kanal, ist_token, ist_hash


class Zugehoerigkeit(Protocol):
    """Beantwortet, ob der gedachte Nutzer zu einer benannten Gruppe gehoert."""

    def registriert(self) -> bool:
        ...

    def in_gruppe(self, name: str, kontext_kanal: int) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class Rolle:
    """Ein gedachter Traeger genau einer Rolle.

    Fuer die Rechtematrix: "wer diese Rolle hat, darf hier was?". Der Traeger
    ist registriert (eine Rolle bekommt man nur als registrierter Nutzer) und
    gehoert zu genau der einen Gruppe.
    """

    name: str

    def registriert(self) -> bool:
        return True

    def in_gruppe(self, name: str, kontext_kanal: int) -> bool:
        return name == self.name


@dataclass(frozen=True, slots=True)
class Person:
    """Ein echter registrierter Nutzer, aufgeloest ueber die Gruppen am Server.

    Die Aufloesung folgt ``Group::appliesToUser``: vom Kontextkanal nach oben,
    ``inheritable`` bricht die Kette von unten, ``inherit`` von oben ab, und
    beim Abarbeiten von aussen nach innen gewinnt der innerste Eintrag.
    """

    userid: int
    acls: Mapping[int, ChannelACL]
    kanaele: Mapping[int, MumbleChannel]

    def registriert(self) -> bool:
        return self.userid >= 0

    def in_gruppe(self, name: str, kontext_kanal: int) -> bool:
        if self.userid < 0:
            return False
        stapel: list[tuple[list[int], list[int]]] = []
        kanal: int | None = kontext_kanal
        erster = True
        while kanal is not None:
            acl = self.acls.get(kanal)
            gruppe = None
            if acl is not None:
                # Nur die *eigenen* Gruppen des Kanals -- murmur laeuft in
                # ``Group::appliesToUser`` ueber ``channel->qhGroups``. Die
                # geerbten Eintraege aus ``getACL`` haben laut
                # ``impl_Server_getACL`` eine leere ``add``-Liste (gefuellt ist
                # dort nur ``members``); wer sie mitnimmt, haelt jede geerbte
                # Rolle faelschlich fuer unbesetzt.
                gruppe = next((g for g in acl.own_groups() if g.name == name), None)
            if gruppe is not None:
                if not erster and not gruppe.inheritable:
                    break
                stapel.append((list(gruppe.add), list(gruppe.remove)))
                if not gruppe.inherit:
                    break
            erster = False
            kanal = _eltern(kanal, self.kanaele)

        drin = False
        for add, remove in reversed(stapel):
            if self.userid in add:
                drin = True
            if self.userid in remove:
                drin = False
        return drin


def _kette_von_wurzel(
    kanal_id: int, kanaele: Mapping[int, MumbleChannel]
) -> list[int]:
    """Kanalkette von der Wurzel bis ``kanal_id`` einschliesslich."""
    kette: list[int] = []
    kanal: int | None = kanal_id
    schutz = 1000
    while kanal is not None and schutz > 0:
        schutz -= 1
        kette.append(kanal)
        kanal = _eltern(kanal, kanaele)
    kette.reverse()
    return kette


def _ganzzahl(text: str) -> int:
    """``QString::toInt()``: was keine Zahl ist, ist null."""
    try:
        return int(text)
    except ValueError:
        return 0


def _sub_trifft_zu(
    rest: str,
    *,
    ziel: int,
    kontext: int,
    sitzt_in: int,
    kanaele: Mapping[int, MumbleChannel],
) -> bool:
    """Die ``sub``-Metagruppe aus ``Group::appliesToUser``.

    ``sub,<versatz>,<min>,<max>`` trifft zu, wenn der Nutzer tief genug unter
    einem bestimmten Kanal der Zielkette steht. ``rest`` ist alles nach dem
    ``sub`` -- murmur schneidet dort vier Zeichen ab, ``sub`` allein wird damit
    zur leeren Zeichenkette.
    """
    args = rest.split(",") if rest else []
    versatz = _ganzzahl(args[0]) if len(args) >= 1 and args[0] else 0
    min_tiefe = _ganzzahl(args[1]) if len(args) >= 2 and args[1] else 1
    max_tiefe = _ganzzahl(args[2]) if len(args) >= 3 and args[2] else 1000

    heimatkette = _kette_von_wurzel(sitzt_in, kanaele)
    zielkette = _kette_von_wurzel(ziel, kanaele)

    if kontext not in zielkette:
        return False
    index = zielkette.index(kontext) + versatz
    if index >= len(zielkette):
        return False
    index = max(index, 0)

    if zielkette[index] not in heimatkette:
        return False

    gesamttiefe = len(heimatkette) - 1
    return index + min_tiefe <= gesamttiefe <= index + max_tiefe


def _eltern(kanal_id: int, kanaele: Mapping[int, MumbleChannel]) -> int | None:
    """Elternkanal oder ``None`` an der Wurzel."""
    if kanal_id == 0:
        return None
    kanal = kanaele.get(kanal_id)
    if kanal is None or kanal.parent < 0 or kanal.parent == kanal_id:
        return None
    return kanal.parent


@dataclass(frozen=True, slots=True)
class Wirkung:
    """Das Ergebnis einer Auswertung.

    ``maske`` sind die Rechte, die sicher gelten. ``unbestimmt`` sind die Bits,
    bei denen das Ergebnis davon abhaengt, ob eine nicht entscheidbare
    Gruppenangabe (``strong``, ``#token``, ``$hash``, ``sub``) zutrifft. Die
    Oberflaeche zeigt solche Bits als "kommt drauf an" statt als Ja oder Nein --
    ein geratenes Ja waere genau die Sorte Anzeige, die im Betrieb schadet.
    """

    maske: int
    unbestimmt: int = 0
    gruende: tuple[str, ...] = field(default_factory=tuple)

    def darf(self, bit: int) -> bool | None:
        """``True``/``False``, oder ``None`` wenn es nicht entscheidbar ist."""
        if self.unbestimmt & bit:
            return None
        return bool(self.maske & bit)

    def to_json(self) -> dict[str, object]:
        return {
            "maske": self.maske,
            "unbestimmt": self.unbestimmt,
            "rechte": {
                p.name: self.darf(p.bit)
                for p in PERMISSIONS
            },
            "gruende": list(self.gruende),
        }


def _einmal(
    *,
    ziel: int,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
    wer: Zugehoerigkeit,
    userid: int,
    sitzt_in: int,
    annahmen: Mapping[str, bool],
    gesehen: set[str],
) -> int:
    """Ein Durchlauf von ``ChanACL::effectivePermissions``."""
    if userid == 0:  # SuperUser
        return ALLE_BITS & ~(SPEAK | WHISPER)

    # Kette bis zur Wurzel -- ohne Ruecksicht auf bInheritACL, das wirkt erst
    # beim Abarbeiten. ``_kette_von_wurzel`` liefert sie bereits in der
    # Reihenfolge, in der murmur den Stapel abraeumt: aussen zuerst.
    kette = _kette_von_wurzel(ziel, kanaele)

    granted = GRUNDRECHTE
    traverse = True
    write = False

    for kanal_id in kette:
        acl = acls.get(kanal_id)
        if acl is None:
            # Kanal ohne gelesene ACL: er kann nichts erlauben und nichts
            # verbieten, aber die Abbruchpruefung unten gilt trotzdem.
            if not traverse and not write:
                return 0
            continue
        if not acl.inherit:
            granted = GRUNDRECHTE

        for eintrag in acl.own_acls():
            if not _trifft_zu(
                eintrag,
                wer=wer,
                userid=userid,
                ziel=ziel,
                acl_kanal=kanal_id,
                sitzt_in=sitzt_in,
                kanaele=kanaele,
                annahmen=annahmen,
                gesehen=gesehen,
            ):
                continue

            if eintrag.allow & TRAVERSE:
                traverse = True
            if eintrag.deny & TRAVERSE:
                traverse = False
            if eintrag.allow & WRITE:
                write = True
            if eintrag.deny & WRITE:
                write = False

            if kanal_id == 0 and ziel == 0 and eintrag.apply_here:
                granted |= eintrag.allow & NUR_WURZEL

            hier = kanal_id == ziel and eintrag.apply_here
            drunter = kanal_id != ziel and eintrag.apply_subs
            if hier or drunter:
                granted |= eintrag.allow & ~(NUR_WURZEL | CACHED)
                granted &= ~eintrag.deny

        if not traverse and not write:
            return 0

    if granted & WRITE:
        granted |= WRITE_IMPLIZIERT
        if ziel == 0:
            granted |= NUR_WURZEL

    return granted


def _trifft_zu(
    eintrag: ACLEntry,
    *,
    wer: Zugehoerigkeit,
    userid: int,
    ziel: int,
    acl_kanal: int,
    sitzt_in: int,
    kanaele: Mapping[int, MumbleChannel],
    annahmen: Mapping[str, bool],
    gesehen: set[str],
) -> bool:
    """``matchUser || matchGroup`` aus ``effectivePermissions``."""
    if eintrag.userid != -1 and eintrag.userid == userid:
        return True
    if not eintrag.group:
        return False

    name, invertiert, am_acl_kanal, ist_token, ist_hash = _zerlege(eintrag.group)
    if not name:
        return False

    kontext = acl_kanal if am_acl_kanal else ziel

    if ist_token or ist_hash or name in UNBESTIMMTE_ANGABEN:
        # Der Schluessel ist die Angabe *ohne* Verneinung: ``strong`` und
        # ``!strong`` sind dieselbe Frage mit entgegengesetzter Antwort. Wer
        # beide unabhaengig annimmt, klammert Faelle ein, die es nicht gibt.
        schluessel = ("#" if ist_token else "$" if ist_hash else "") + name
        gesehen.add(schluessel)
        trifft = annahmen.get(schluessel, False)
    elif name == "none":
        trifft = False
    elif name == "all":
        trifft = True
    elif name == "auth":
        trifft = wer.registriert()
    elif name == "in":
        trifft = sitzt_in == kontext
    elif name == "out":
        trifft = sitzt_in != kontext
    elif name == "sub" or name.startswith("sub,"):
        return _sub_trifft_zu(
            name[4:],
            ziel=ziel,
            kontext=kontext,
            sitzt_in=sitzt_in,
            kanaele=kanaele,
        ) != invertiert
    else:
        trifft = wer.in_gruppe(name, kontext)

    return not trifft if invertiert else trifft


def wirksame_rechte(
    *,
    ziel: int,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
    wer: Zugehoerigkeit,
    userid: int = -1,
    sitzt_in: int | None = None,
) -> Wirkung:
    """Was darf ``wer`` im Kanal ``ziel``?

    ``sitzt_in`` ist der Kanal, in dem der gedachte Nutzer gerade steht -- er
    entscheidet ueber die Gruppen ``in`` und ``out``. Ohne Angabe wird
    angenommen, dass er im Zielkanal sitzt; das ist die Frage, die man an eine
    Rechtematrix ueblicherweise stellt ("wer hier drin ist, darf was?").

    Die Unbestimmtheit wird eingeklammert: es wird zweimal gerechnet, einmal mit
    "die nicht entscheidbaren Angaben treffen zu" und einmal mit "sie treffen
    nicht zu". Bits, die in beiden Durchlaeufen gleich sind, stehen fest; der
    Rest ist unbestimmt. Das ist exakt -- es wird nichts geschaetzt.
    """
    if sitzt_in is None:
        sitzt_in = ziel

    def lauf(annahmen: Mapping[str, bool], gesehen: set[str]) -> int:
        return _einmal(
            ziel=ziel,
            kanaele=kanaele,
            acls=acls,
            wer=wer,
            userid=userid,
            sitzt_in=sitzt_in,
            annahmen=annahmen,
            gesehen=gesehen,
        )

    gesehen: set[str] = set()
    erster = lauf({}, gesehen)
    if not gesehen:
        return Wirkung(maske=erster)

    angaben = sorted(gesehen)
    if len(angaben) > MAX_UNBESTIMMTE:
        return _grob(
            erster=erster,
            lauf=lauf,
            angaben=angaben,
            ziel=ziel,
            kanaele=kanaele,
            acls=acls,
        )

    # Vollstaendige Einklammerung: jede Kombination einmal durchrechnen. Ein
    # Bit steht nur fest, wenn *alle* Durchlaeufe es gleich beantworten -- zwei
    # Extremlaeufe genuegen dafuer nicht, weil sich Angaben gegenseitig
    # aufheben koennen (eine trifft zu, die andere nicht).
    immer = ALLE_BITS
    jemals = 0
    for muster in range(1 << len(angaben)):
        annahmen = {
            name: bool(muster & (1 << i)) for i, name in enumerate(angaben)
        }
        ergebnis = lauf(annahmen, set())
        immer &= ergebnis
        jemals |= ergebnis

    return Wirkung(
        maske=immer,
        unbestimmt=jemals & ~immer,
        gruende=tuple(angaben),
    )


def _grob(
    *,
    erster: int,
    lauf: Callable[[Mapping[str, bool], set[str]], int],
    angaben: list[str],
    ziel: int,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
) -> Wirkung:
    """Reissleine fuer absurd viele unbestimmte Angaben.

    Statt 2^n Durchlaeufen wird hier grob abgeschaetzt: unbestimmt ist alles,
    was ein Eintrag mit unbestimmter Angabe ueberhaupt anfasst. Beruehrt einer
    davon ``Traverse`` oder ``Write``, ist alles unbestimmt -- ueber diese
    beiden Bits kann die Auswertung die gesamte Kette abbrechen.
    """
    alle_ja = lauf(dict.fromkeys(angaben, True), set())
    beruehrt = 0
    for kanal_id in _kette_von_wurzel(ziel, kanaele):
        acl = acls.get(kanal_id)
        if acl is None:
            continue
        for eintrag in acl.own_acls():
            name, _, _, ist_token, ist_hash = _zerlege(eintrag.group)
            if ist_token or ist_hash or name in UNBESTIMMTE_ANGABEN:
                beruehrt |= eintrag.allow | eintrag.deny
    if beruehrt & (TRAVERSE | WRITE):
        beruehrt = ALLE_BITS
    unbestimmt = (erster ^ alle_ja) | beruehrt
    return Wirkung(
        maske=erster & ~unbestimmt,
        unbestimmt=unbestimmt,
        gruende=tuple(angaben),
    )


def rechte_einer_rolle(
    *,
    rolle: str,
    ziel: int,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
) -> Wirkung:
    """Rechtematrix-Zeile: was darf, wer diese Rolle traegt?"""
    return wirksame_rechte(
        ziel=ziel,
        kanaele=kanaele,
        acls=acls,
        wer=Rolle(rolle),
        userid=-1,
        sitzt_in=ziel,
    )


def rechte_einer_person(
    *,
    userid: int,
    ziel: int,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
    sitzt_in: int | None = None,
) -> Wirkung:
    """Was darf dieser registrierte Nutzer in diesem Kanal?"""
    return wirksame_rechte(
        ziel=ziel,
        kanaele=kanaele,
        acls=acls,
        wer=Person(userid=userid, acls=acls, kanaele=kanaele),
        userid=userid,
        sitzt_in=sitzt_in,
    )


def rollen_am_server(acls: Mapping[int, ChannelACL]) -> list[str]:
    """Alle benannten Gruppen, die irgendwo am Server definiert sind."""
    namen: set[str] = set()
    for acl in acls.values():
        for gruppe in acl.groups:
            if gruppe.name:
                namen.add(gruppe.name)
    return sorted(namen)


__all__ = [
    "GRUNDRECHTE",
    "Person",
    "Rolle",
    "Wirkung",
    "rechte_einer_person",
    "rechte_einer_rolle",
    "rollen_am_server",
    "wirksame_rechte",
]
