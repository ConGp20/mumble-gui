"""Eingebaute Intercom-Vorlagen fuer den ACL-Editor.

Dieselben Muster, die der Provisioner aus ``speak`` / ``whisper_in`` /
``listen_for`` erzeugt -- hier als Ein-Klick-Aktion fuer einen einzelnen Kanal.
Wer im GUI eine Vorlage anwendet, bekommt genau die ACL-Liste, die auch aus der
YAML entstanden waere. Das ist Absicht: sonst haette man zwei Wahrheiten, und
der naechste ``apply`` wuerde die Handarbeit wieder einkassieren.

Jede Vorlage bekommt die Gruppen, auf die sie wirken soll, und liefert eine
fertige ACL-Liste. Der Editor zeigt sie als Diff, bevor etwas geschrieben wird.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..ice.permissions import BY_NAME
from ..ice.types import ACLEntry

__all__ = ["Template", "TEMPLATES", "apply_template"]

SPEAK = BY_NAME["Speak"].bit
WHISPER = BY_NAME["Whisper"].bit
LISTEN = BY_NAME["Listen"].bit
TRAVERSE = BY_NAME["Traverse"].bit
ENTER = BY_NAME["Enter"].bit
TEXT = BY_NAME["TextMessage"].bit
MOVE = BY_NAME["Move"].bit
MUTE = BY_NAME["MuteDeafen"].bit


def _entry(group: str, allow: int, deny: int, *, subs: bool = False) -> ACLEntry:
    return ACLEntry(
        apply_here=True, apply_subs=subs, allow=allow, deny=deny, group=group, userid=-1
    )


@dataclass(frozen=True, slots=True)
class Template:
    """Eine benannte Vorlage."""

    key: str
    label: str
    description: str
    #: Wie viele Gruppen die Vorlage sinnvoll braucht (0 = keine).
    wants_groups: bool
    build: Callable[[list[str]], list[ACLEntry]] = field(repr=False, default=lambda g: [])

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "wants_groups": self.wants_groups,
        }


def _ring(groups: list[str]) -> list[ACLEntry]:
    """Geschlossener Ring: nur die genannten Gruppen sehen und hoeren den Kanal."""
    entries = [_entry("all", 0, TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT)]
    for group in sorted(set(groups)):
        entries.append(
            _entry(group, TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT, 0)
        )
    return entries


def _listen_only(groups: list[str]) -> list[ACLEntry]:
    """Alle duerfen zuhoeren, nur die genannten Gruppen sprechen."""
    entries = [_entry("all", TRAVERSE | ENTER | LISTEN, SPEAK | WHISPER)]
    for group in sorted(set(groups)):
        entries.append(_entry(group, TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN, 0))
    return entries


def _broadcast(groups: list[str]) -> list[ACLEntry]:
    """Sammelruf: jeder hoert mit, nur die genannten Gruppen senden."""
    entries = [_entry("all", TRAVERSE | ENTER | LISTEN | TEXT, SPEAK)]
    for group in sorted(set(groups)):
        entries.append(
            _entry(group, TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT, 0)
        )
    return entries


def _control(groups: list[str]) -> list[ACLEntry]:
    """Regie-Rechte: sprechen, fluestern, verschieben, stummschalten -- auch
    in allen Unterkanaelen."""
    entries: list[ACLEntry] = []
    for group in sorted(set(groups)):
        entries.append(
            _entry(
                group,
                TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT | MOVE | MUTE,
                0,
                subs=True,
            )
        )
    return entries


def _emergency(groups: list[str]) -> list[ACLEntry]:
    """Notfallkanal: jeder darf alles. Die uebergebenen Gruppen bekommen
    zusaetzlich das Recht, andere zu verschieben und stummzuschalten."""
    entries = [_entry("all", TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT, 0)]
    for group in sorted(set(groups)):
        entries.append(
            _entry(group, TRAVERSE | ENTER | SPEAK | WHISPER | LISTEN | TEXT | MOVE | MUTE, 0)
        )
    return entries


TEMPLATES: dict[str, Template] = {
    template.key: template
    for template in (
        Template(
            key="ring",
            label="Ring anlegen",
            description=(
                "Geschlossener Ring: ausser den gewaehlten Gruppen sieht niemand "
                "den Kanal, betritt ihn oder hoert mit."
            ),
            wants_groups=True,
            build=_ring,
        ),
        Template(
            key="nur-hoeren",
            label="Nur-Hoeren",
            description=(
                "Alle duerfen betreten und mithoeren, aber nur die gewaehlten "
                "Gruppen duerfen sprechen und fluestern."
            ),
            wants_groups=True,
            build=_listen_only,
        ),
        Template(
            key="sammelruf",
            label="Sammelruf-Rechte",
            description=(
                "Jeder hoert mit, nur die gewaehlten Gruppen senden. Fuer Ansagen "
                "an die ganze Produktion."
            ),
            wants_groups=True,
            build=_broadcast,
        ),
        Template(
            key="regie",
            label="Regie-Rechte",
            description=(
                "Die gewaehlten Gruppen duerfen hier und in allen Unterkanaelen "
                "sprechen, fluestern, verschieben und stummschalten."
            ),
            wants_groups=True,
            build=_control,
        ),
        Template(
            key="notfall",
            label="Notfall-Kanal",
            description=(
                "Jeder darf sprechen und mithoeren. Die gewaehlten Gruppen duerfen "
                "zusaetzlich verschieben und stummschalten."
            ),
            wants_groups=True,
            build=_emergency,
        ),
    )
}


def apply_template(key: str, groups: list[str]) -> list[ACLEntry]:
    """Baut die ACL-Liste einer Vorlage.

    Wirft ``KeyError`` bei unbekanntem Schluessel -- die Route uebersetzt das in
    einen 400er mit der Liste der gueltigen Schluessel.
    """
    return TEMPLATES[key].build(groups)
