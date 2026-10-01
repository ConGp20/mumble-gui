"""Ruftasten: zentral belegte Tasten je Platz.

Profi-Intercoms (GreenGo, Riedel, Clear-Com) legen in ihrer
Konfigurationssoftware fest, was Taste 1 bis 4 an einem Beltpack tut. Mumble
kennt das nicht: welche Taste wohin flüstert, steht im Client, und die Slice
hat keine Methode, um das von aussen zu setzen.

Der Umweg, gegen murmur v1.5.735 gemessen (DECISIONS D-032):

* Jeder Client richtet **einmal** Taste n ein als "Rufen an den obersten
  Platz samt Unterplaetzen, beschraenkt auf Gruppe ``rufn``".
* Eine Gruppe ``rufn`` gibt es nicht -- ohne Umleitung hoert niemand etwas.
* Der Server leitet ``rufn`` je Sitzung per ``redirectWhisperGroup`` auf die
  Rolle um, die an diesem Platz gerufen werden soll.

Zwei Bedingungen aus Server.cpp, die die Oberflaeche anzeigen muss, weil sonst
eine Taste belegt aussieht und trotzdem stumm bleibt:

1. **Der Rufende muss auf seinem eigenen Platz sprechen duerfen.** Wer dort
   unterdrueckt ist, dessen Sprache verwirft murmur ganz -- auch das Fluestern
   (Server.cpp, ``if (... u->bSuppress ...) return``).
2. **Er braucht das Fluesterrecht am Platz jedes Empfaengers.**
   ``createWhisperTargetCacheFor`` prueft ``ChanACL::Whisper`` je Zielplatz.

Die Belegung gilt fuer einen Platz und alles darunter, solange ein Platz
darunter nicht selbst etwas anderes festlegt -- so belegt man die acht
Kampfgerichte einmal am Ordner "Kampfgerichte".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .ice.types import ChannelACL, MumbleChannel
from .ice.wirkung import SPEAK, WHISPER, rechte_einer_rolle
from .store.db import RUFTASTEN

#: Rollen, die Mumble selbst kennt und die als Rufziel taugen. "Alle" ist die
#: Durchsage an jeden, der verbunden ist.
EINGEBAUTE_ZIELE: dict[str, str] = {"all": "Alle", "auth": "Alle Angemeldeten"}


def pfade(kanaele: Mapping[int, MumbleChannel]) -> dict[int, str]:
    """Kanal-ID -> Pfad. Der oberste Platz hat den leeren Pfad."""
    ergebnis: dict[int, str] = {}

    def pfad(kid: int, tiefe: int = 0) -> str:
        if kid in ergebnis:
            return ergebnis[kid]
        if kid == 0 or tiefe > 64:
            ergebnis[kid] = ""
            return ""
        kanal = kanaele.get(kid)
        if kanal is None:
            return ""
        oben = pfad(kanal.parent, tiefe + 1) if kanal.parent >= 0 else ""
        ergebnis[kid] = f"{oben}/{kanal.name}" if oben else kanal.name
        return ergebnis[kid]

    for kid in kanaele:
        pfad(kid)
    return ergebnis


def _kette(kid: int, kanaele: Mapping[int, MumbleChannel]) -> list[int]:
    """Der Platz selbst, dann alle darueber bis zum obersten."""
    kette: list[int] = []
    aktuell: int | None = kid
    while aktuell is not None and aktuell not in kette and len(kette) < 64:
        kette.append(aktuell)
        if aktuell == 0:
            break
        kanal = kanaele.get(aktuell)
        aktuell = kanal.parent if kanal is not None and kanal.parent >= 0 else 0
    return kette


@dataclass(frozen=True)
class Belegt:
    """Wie eine Taste an einem Platz belegt ist -- und woher das kommt."""

    taste: int
    rolle: str
    #: Pfad des Platzes, an dem die Belegung steht. Gleich dem eigenen Pfad,
    #: wenn sie hier gesetzt ist; sonst geerbt von weiter oben.
    von: str


def wirksame_belegung(
    kid: int,
    kanaele: Mapping[int, MumbleChannel],
    belegung: Mapping[str, Mapping[int, str]],
) -> dict[int, Belegt]:
    """Welche Taste ruft an diesem Platz wen? Der naechstgelegene Eintrag gewinnt."""
    nach_pfad = pfade(kanaele)
    ergebnis: dict[int, Belegt] = {}
    for stufe in _kette(kid, kanaele):
        pfad = nach_pfad.get(stufe, "")
        for taste, rolle in belegung.get(pfad, {}).items():
            if taste in RUFTASTEN and taste not in ergebnis and rolle:
                ergebnis[taste] = Belegt(taste=taste, rolle=rolle, von=pfad)
    return ergebnis


def geltungsbereich(
    kid: int,
    taste: int,
    kanaele: Mapping[int, MumbleChannel],
    belegung: Mapping[str, Mapping[int, str]],
) -> list[int]:
    """Alle Plaetze, an denen die Belegung von Taste ``taste`` an ``kid`` greift.

    Das ist der Platz selbst und alles darunter, ausser wo weiter unten dieselbe
    Taste anders belegt ist.
    """
    nach_pfad = pfade(kanaele)
    eigener = nach_pfad.get(kid, "")
    bereich = []
    for anderer in kanaele:
        wirk = wirksame_belegung(anderer, kanaele, belegung).get(taste)
        if wirk is not None and wirk.von == eigener:
            bereich.append(anderer)
    return sorted(bereich)


def sprechende_rollen(
    plaetze: list[int],
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
    rollen: list[str],
) -> list[str]:
    """Rollen, die an mindestens einem dieser Plaetze sprechen duerfen.

    Das sind die Rufenden: wer nicht sprechen darf, wird von murmur ganz
    verworfen und kann auch nicht rufen.
    """
    gefunden = []
    for rolle in rollen:
        for kid in plaetze:
            if rechte_einer_rolle(
                rolle=rolle, ziel=kid, kanaele=kanaele, acls=acls
            ).darf(SPEAK) is True:
                gefunden.append(rolle)
                break
    return gefunden


def plaetze_der_rolle(
    rolle: str,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
) -> list[int]:
    """Wo sitzen die Gerufenen? Dort, wo ihre Rolle sprechen darf.

    Fuer "Alle" und "Alle Angemeldeten" ist das jeder Platz ausser dem
    obersten -- eine Durchsage soll ueberall ankommen.
    """
    if rolle in EINGEBAUTE_ZIELE:
        return sorted(k for k in kanaele if k != 0)
    return sorted(
        kid
        for kid in kanaele
        if kid != 0
        and rechte_einer_rolle(rolle=rolle, ziel=kid, kanaele=kanaele, acls=acls).darf(
            SPEAK
        )
        is True
    )


@dataclass
class Abdeckung:
    """Kommt ein Ruf an? Und wenn nicht: wo nicht, und fuer wen."""

    rufende: list[str] = field(default_factory=list)
    zielplaetze: list[int] = field(default_factory=list)
    #: (Rolle des Rufenden, Platz-ID) -- dort fehlt das Fluesterrecht.
    fehlt: list[tuple[str, int]] = field(default_factory=list)


def abdeckung(
    kid: int,
    taste: int,
    rolle: str,
    kanaele: Mapping[int, MumbleChannel],
    acls: Mapping[int, ChannelACL],
    eigene_rollen: list[str],
    belegung: Mapping[str, Mapping[int, str]],
) -> Abdeckung:
    """Rechnet mit :mod:`intercom.ice.wirkung` nach, wo der Ruf ankommt."""
    bereich = geltungsbereich(kid, taste, kanaele, belegung) or [kid]
    rufende = sprechende_rollen(bereich, kanaele, acls, eigene_rollen)
    ziele = plaetze_der_rolle(rolle, kanaele, acls)
    fehlt = [
        (rufer, ziel)
        for rufer in rufende
        for ziel in ziele
        if rechte_einer_rolle(rolle=rufer, ziel=ziel, kanaele=kanaele, acls=acls).darf(
            WHISPER
        )
        is not True
    ]
    return Abdeckung(rufende=rufende, zielplaetze=ziele, fehlt=fehlt)


def je_kanal(
    kanaele: Mapping[int, MumbleChannel],
    belegung: Mapping[str, Mapping[int, str]],
) -> dict[int, dict[int, str]]:
    """Fuer den Enforcer: Kanal-ID -> {Taste: Rolle}, mit Vererbung aufgeloest."""
    return {
        kid: {t: b.rolle for t, b in wirksame_belegung(kid, kanaele, belegung).items()}
        for kid in kanaele
    }


__all__ = [
    "EINGEBAUTE_ZIELE",
    "Abdeckung",
    "Belegt",
    "abdeckung",
    "geltungsbereich",
    "je_kanal",
    "pfade",
    "plaetze_der_rolle",
    "sprechende_rollen",
    "wirksame_belegung",
]
