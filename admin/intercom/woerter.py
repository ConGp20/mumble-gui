"""Die Woerter, in denen diese Oberflaeche spricht.

Mumble ist fuer Spieleabende gebaut. Seine Begriffe -- Kanal, Gruppe,
registrierter Nutzer, ACL -- beschreiben zwar genau dieselben Dinge, die eine
Eventintercom braucht, aber niemand am Kampfgericht denkt in ihnen. Vier Namen
fuer dieselbe Sache ueber vier Seiten verteilt war der Hauptgrund, warum die
Oberflaeche sich unuebersichtlich anfuehlte.

Deshalb steht hier **eine** Uebersetzung, und alles haelt sich daran:

==================  ==================  ====================================
In der Intercom     Bei Mumble          Warum
==================  ==================  ====================================
Platz               Kanal               Ein Ort, an dem jemand arbeitet.
Rolle               Gruppe              Eine Schublade voller Leute.
Person              Registrierter       Jemand, den der Server wiedererkennt.
                    Nutzer
Regel               ACL-Eintrag         Eine Zeile, die etwas erlaubt/verbietet.
Verbindung          Client / Session    Ein gerade angemeldetes Geraet.
Ueberall            Wurzelkanal         Siehe unten.
==================  ==================  ====================================

Zum Wurzelkanal im Besonderen: "Wurzel" ist ein Wort aus der Informatik und
sagt vor Ort niemandem etwas -- es stand an einem Dutzend Stellen in der
Oberflaeche und war jedes Mal erklaerungsbeduerftig. Fachlich ist der
Wurzelkanal der Platz, unter dem alle anderen haengen; eine Regel dort gilt
darum ueberall. Genau das ist der Name: **Ueberall**.

Die Mumble-Woerter werden nicht versteckt. Wo es beim Nachschlagen in fremder
Dokumentation hilft, stehen sie in Klammern dahinter -- aber sie sind nie das
Hauptwort.
"""

from __future__ import annotations

from typing import Final

#: Anzeigename des Wurzelkanals. Steht ueberall dort, wo murmur einen Kanal
#: ohne Namen liefert (``getChannels()[0].name`` ist die leere Zeichenkette).
UEBERALL: Final = "Überall"

#: Der Zusatz fuer Stellen, an denen Platz fuer eine Erklaerung ist.
UEBERALL_LANG: Final = "Überall (gilt für alle Plätze)"

#: Kurze Erlaeuterung fuer Titel-Attribute und Hilfetexte.
UEBERALL_HILFE: Final = (
    "Der oberste Platz, unter dem alle anderen hängen. Was hier gilt, "
    "gilt überall – Mumble nennt ihn Wurzelkanal."
)


def platzname(name: str | None, pfad: str | None = None) -> str:
    """Anzeigename eines Platzes. Ohne Namen ist es der oberste.

    ``pfad`` hat Vorrang, wenn er gesetzt ist -- in Listen ist der vollstaendige
    Pfad die brauchbarere Auskunft als der blosse Name, weil es acht Plaetze
    namens "Kampfgericht n" gibt, aber nur einen "Wettkampf/Technik".
    """
    if pfad:
        return pfad
    return name or UEBERALL


__all__ = ["UEBERALL", "UEBERALL_HILFE", "UEBERALL_LANG", "platzname"]
