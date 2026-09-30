"""Eingebaute Baukästen für einen frischen Server.

Warum es die gibt
-----------------
Der Server ist die Wahrheit: was in der Oberfläche angelegt wird, steht im
Server und bleibt dort. Eine Konfigurationsdatei zu pflegen, um überhaupt
anzufangen, ist genau der Umweg, den niemand will.

Eine Vorlage ist deshalb *keine* laufende Bindung, sondern ein einmaliger
Startschuss: sie legt Kanäle und Gruppen an, und danach ist sie fertig. Alles
Weitere passiert in der Oberfläche. Wer eine Vorlage anwendet und danach einen
Kanal umbenennt, hat einen umbenannten Kanal -- und keine Datei, die ihm beim
nächsten Neustart widerspricht.

Aufbau
------
Jede Vorlage ist der Text einer ``intercom.yaml``. Das ist Absicht und keine
Bequemlichkeit: so läuft sie durch dieselbe Prüfung und denselben Abgleich wie
eine eingespielte Sicherung. Was hier steht, kann also nichts, was eine
Sicherung nicht auch könnte -- und umgekehrt fällt ein Fehler in einer Vorlage
schon beim Testlauf auf, nicht erst beim Anwenden.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .schema import IntercomConfig, parse_config

__all__ = ["VORLAGEN", "Vorlage", "vorlage_laden", "vorlagen_liste"]


@dataclass(frozen=True)
class Vorlage:
    """Ein Baukasten mit Namen, Beschreibung und Inhalt."""

    schluessel: str
    titel: str
    beschreibung: str
    #: Was sie anlegt, in Klartext für die Oberfläche.
    legt_an: tuple[str, ...]
    yaml_text: str

    def laden(self) -> IntercomConfig:
        """Wandelt den Text in eine geprüfte Konfiguration."""
        return parse_config(yaml.safe_load(self.yaml_text), source=f"Vorlage {self.schluessel}")


# --------------------------------------------------------------------------- #
#  Leichtathletik-Wettkampf
# --------------------------------------------------------------------------- #
#
# Aufgebaut wie eine Event-Intercom, nicht wie ein Mumble-Server: eine Stelle
# je Platz, und die Technik hört überall mit. Die acht Kampfgerichte bekommen
# je einen eigenen Kanal, damit sie sich nicht gegenseitig zuhören -- genau das
# ist der Sinn getrennter Plätze. Wer sie doch zusammenlegen will, löscht in
# der Oberfläche sieben davon.

_LEICHTATHLETIK = """
version: 1

groups:
  - leitung
  - technik
  - kampfgericht
  - zeitmessung
  - wettkampfbuero

policies:
  # Wer darf jemanden vom Server werfen. Bewusst knapp gehalten.
  kick: [leitung]
  ban: [leitung, technik]
  # Gäste ohne Anmeldung dürfen zuhören, aber nicht sprechen.
  guests_listen_only: true

channels:
  - name: Wettkampf
    description: Dach über allem. Hier spricht niemand, hier wird sortiert.
    children:
      - name: Wettkampfbüro
        description: Meldestelle, Ergebnisse, Zeitplan.
        position: 10
        speak: [wettkampfbuero, leitung, technik]
        listen_for: [wettkampfbuero, leitung, technik]

      - name: Zeitmessung
        description: Zielkamera, Zeitnahme, Windmessung.
        position: 20
        speak: [zeitmessung, leitung, technik]
        listen_for: [zeitmessung, leitung, technik]

      - name: Technik
        description: Ton, Anlage, Netz. Hört überall mit.
        position: 30
        speak: [technik, leitung]
        listen_for: [technik, leitung]

  - name: Kampfgerichte
    description: Ein Kanal je Kampfgericht – sie hören sich gegenseitig nicht.
    children:
      - name: Kampfgericht 1
        position: 10
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 2
        position: 20
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 3
        position: 30
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 4
        position: 40
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 5
        position: 50
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 6
        position: 60
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 7
        position: 70
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
      - name: Kampfgericht 8
        position: 80
        speak: [kampfgericht, leitung, technik]
        listen_for: [kampfgericht, leitung, technik]
"""


# --------------------------------------------------------------------------- #
#  Kleine Veranstaltung
# --------------------------------------------------------------------------- #
#
# Für alles, was kein Stadion ist: ein Kanal für die Leitung, einer für die
# Technik, einer als Sammelruf. Drei Plätze reichen erstaunlich weit.

_KLEIN = """
version: 1

groups:
  - leitung
  - technik

policies:
  kick: [leitung]
  ban: [leitung]
  guests_listen_only: true

channels:
  - name: Leitung
    description: Wer die Veranstaltung führt.
    position: 10
    speak: [leitung, technik]
    listen_for: [leitung, technik]

  - name: Technik
    description: Ton, Licht, Aufbau.
    position: 20
    speak: [technik, leitung]
    listen_for: [technik, leitung]

  - name: Sammelruf
    description: Hier hören alle mit. Für Ansagen an das ganze Team.
    position: 30
    speak: [leitung, technik]
    listen_for: [leitung, technik]
"""


VORLAGEN: tuple[Vorlage, ...] = (
    Vorlage(
        schluessel="leichtathletik",
        titel="Leichtathletik-Wettkampf",
        beschreibung=(
            "Acht getrennte Kampfgerichte, dazu Zeitmessung, Wettkampfbüro und "
            "Technik. Die Kampfgerichte hören sich gegenseitig nicht – Leitung "
            "und Technik erreichen alle."
        ),
        legt_an=(
            "11 Plätze: Wettkampfbüro, Zeitmessung, Technik und Kampfgericht 1 bis 8",
            "2 Ordner darüber: „Wettkampf“ und „Kampfgerichte“ – dort spricht niemand, "
            "sie sortieren nur (zusammen also 13 Kanäle)",
            "5 Gruppen: leitung, technik, kampfgericht, zeitmessung, wettkampfbuero",
            "Gäste ohne Anmeldung dürfen zuhören, aber nicht sprechen",
        ),
        yaml_text=_LEICHTATHLETIK,
    ),
    Vorlage(
        schluessel="klein",
        titel="Kleine Veranstaltung",
        beschreibung=(
            "Drei Plätze: Leitung, Technik und ein Sammelruf für Ansagen an alle. "
            "Guter Anfang, wenn noch nicht feststeht, wie groß es wird."
        ),
        legt_an=(
            "3 Kanäle: Leitung, Technik, Sammelruf – keine Ordner darüber",
            "2 Gruppen: leitung, technik",
            "Gäste ohne Anmeldung dürfen zuhören, aber nicht sprechen",
        ),
        yaml_text=_KLEIN,
    ),
)


def vorlagen_liste() -> list[dict[str, object]]:
    """Alle Vorlagen als Datensätze für die Oberfläche."""
    return [
        {
            "schluessel": v.schluessel,
            "titel": v.titel,
            "beschreibung": v.beschreibung,
            "legt_an": list(v.legt_an),
        }
        for v in VORLAGEN
    ]


def vorlage_laden(schluessel: str) -> IntercomConfig:
    """Vorlage nach Schlüssel, geprüft und geparst.

    Wirft ``KeyError``, wenn es sie nicht gibt -- der Web-Layer macht daraus
    einen 404. Ein unbekannter Schlüssel darf nie stillschweigend nichts tun.
    """
    for v in VORLAGEN:
        if v.schluessel == schluessel:
            return v.laden()
    raise KeyError(schluessel)
