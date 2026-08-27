"""Zentrale Tabelle der Mumble-Berechtigungen.

Warum diese Datei existiert
---------------------------
Die Slice-Datei ``MumbleServer.ice`` deklariert *nicht alle* Rechte, die der
Server tatsaechlich kennt. Konkret fehlt ``Listen`` (0x800). Der Server kennt
das Bit aber sehr wohl -- siehe ``src/ACL.h`` in mumble-voip/mumble::

    enum Perm {
        ...
        MakeTempChannel = 0x400,
        Listen          = 0x800,
        ...
        Cached = 0x8000000,
        All = Write + Traverse + ... + Listen + ... + ResetUserContent
    };

und ``impl_Server_setACL`` in ``src/murmur/MumbleServerIce.cpp`` maskiert die
per Ice uebergebenen Bits lediglich gegen ``ChanACL::All``::

    acl->pDeny  = static_cast<ChanACL::Permissions>(ai.deny)  & ChanACL::All;
    acl->pAllow = static_cast<ChanACL::Permissions>(ai.allow) & ChanACL::All;

``ChanACL::All`` enthaelt ``Listen``. Das Bit ueberlebt die Maskierung also und
laesst sich ueber Ice setzen, obwohl die Slice keine Konstante dafuer anbietet.
Genau deshalb definieren wir die Bits hier selbst und verlassen uns nicht auf
``MumbleServer.Permission*``. ``verify_against_slice()`` prueft beim Start, dass
unsere Werte mit denen der einkompilierten Slice uebereinstimmen, soweit die
Slice sie kennt -- eine stille Abweichung nach einem Mumble-Update faellt damit
sofort auf.

Achtung bei den Namen: die Slice heisst ``PermissionRegisterSelf`` (nicht
SelfRegister) und ``ResetUserContent`` (ohne ``Permission``-Praefix).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALL_MASK",
    "BY_BIT",
    "BY_NAME",
    "CACHED_BIT",
    "PERMISSIONS",
    "Permission",
    "describe_mask",
    "mask_to_names",
    "names_to_mask",
    "verify_against_slice",
]


@dataclass(frozen=True, slots=True)
class Permission:
    """Ein einzelnes Rechte-Bit."""

    bit: int
    """Numerischer Wert, wie ihn ``ACL.allow`` / ``ACL.deny`` erwartet."""

    name: str
    """Kanonischer Name, so wie er in ``intercom.yaml`` geschrieben wird."""

    label: str
    """Kurzer deutscher Name fuer das GUI."""

    description: str
    """Was das Recht im Betrieb bedeutet."""

    root_only: bool = False
    """True, wenn das Recht laut ACL.h nur am Wurzelkanal ausgewertet wird."""

    in_slice: bool = True
    """False, wenn die Slice keine Konstante dafuer definiert (nur ``Listen``)."""

    slice_const: str | None = None
    """Name der Slice-Konstanten, falls abweichend vom kanonischen Namen."""

    @property
    def effective_slice_const(self) -> str | None:
        if not self.in_slice:
            return None
        return self.slice_const or f"Permission{self.name}"


#: Reihenfolge = Anzeigereihenfolge in der ACL-Matrix des GUI.
PERMISSIONS: Final[tuple[Permission, ...]] = (
    Permission(
        bit=0x01,
        name="Write",
        label="Schreiben",
        description=(
            "Vollzugriff auf den Kanal. Impliziert alle anderen Rechte ausser "
            "Sprechen. Am Wurzelkanal gesetzt gilt es serverweit -- damit ist "
            "der Nutzer faktisch Admin."
        ),
    ),
    Permission(
        bit=0x02,
        name="Traverse",
        label="Durchqueren",
        description=(
            "Der Kanal darf durchquert werden, um Unterkanaele zu erreichen. "
            "Ohne dieses Recht sind alle Unterkanaele unerreichbar, egal welche "
            "Rechte dort gelten."
        ),
    ),
    Permission(
        bit=0x04,
        name="Enter",
        label="Betreten",
        description="Der Nutzer darf den Kanal betreten.",
    ),
    Permission(
        bit=0x08,
        name="Speak",
        label="Sprechen",
        description="Der Nutzer darf in diesem Kanal senden (PTT).",
    ),
    Permission(
        bit=0x10,
        name="MuteDeafen",
        label="Stummschalten",
        description="Andere Nutzer in diesem Kanal stumm oder taub schalten.",
    ),
    Permission(
        bit=0x20,
        name="Move",
        label="Verschieben",
        description=(
            "Nutzer aus diesem Kanal verschieben. Zum Verschieben wird das Recht "
            "in Quell- UND Zielkanal benoetigt."
        ),
    ),
    Permission(
        bit=0x40,
        name="MakeChannel",
        label="Kanal anlegen",
        description="Unterkanaele anlegen.",
    ),
    Permission(
        bit=0x80,
        name="LinkChannel",
        label="Kanal verlinken",
        description=(
            "Diesen Kanal verlinken. Zum Verlinken wird das Recht in beiden "
            "Kanaelen benoetigt, zum Trennen genuegt einer."
        ),
    ),
    Permission(
        bit=0x100,
        name="Whisper",
        label="Fluestern",
        description=(
            "In diesen Kanal fluestern, ohne ihn zu betreten. Fuer ein Intercom "
            "das wichtigste Recht neben Sprechen."
        ),
    ),
    Permission(
        bit=0x200,
        name="TextMessage",
        label="Textnachricht",
        description="Textnachrichten in diesen Kanal senden.",
    ),
    Permission(
        bit=0x400,
        name="MakeTempChannel",
        label="Temp-Kanal anlegen",
        description="Temporaere Unterkanaele anlegen.",
    ),
    Permission(
        bit=0x800,
        name="Listen",
        label="Mithoeren",
        description=(
            "Den Kanal mithoeren, ohne ihn zu betreten (Channel Listener, ab "
            "Mumble 1.4). ACHTUNG: Die Slice-Datei definiert dafuer KEINE "
            "Konstante; das Bit wird trotzdem vom Server ausgewertet."
        ),
        in_slice=False,
    ),
    Permission(
        bit=0x10000,
        name="Kick",
        label="Kicken",
        description="Nutzer vom Server werfen. Nur am Wurzelkanal wirksam.",
        root_only=True,
    ),
    Permission(
        bit=0x20000,
        name="Ban",
        label="Bannen",
        description="Nutzer vom Server bannen. Nur am Wurzelkanal wirksam.",
        root_only=True,
    ),
    Permission(
        bit=0x40000,
        name="Register",
        label="Registrieren",
        description=(
            "Andere Nutzer registrieren und deren Registrierung loeschen. "
            "Nur am Wurzelkanal wirksam."
        ),
        root_only=True,
    ),
    Permission(
        bit=0x80000,
        name="SelfRegister",
        label="Selbst registrieren",
        description=(
            "Sich selbst registrieren. Nur am Wurzelkanal wirksam. "
            "Heisst in der Slice PermissionRegisterSelf."
        ),
        root_only=True,
        slice_const="PermissionRegisterSelf",
    ),
    Permission(
        bit=0x100000,
        name="ResetUserContent",
        label="Inhalte zuruecksetzen",
        description=(
            "Kommentar oder Avatar eines Nutzers zuruecksetzen. Nur am "
            "Wurzelkanal wirksam. Heisst in der Slice ResetUserContent."
        ),
        root_only=True,
        slice_const="ResetUserContent",
    ),
)

BY_NAME: Final[dict[str, Permission]] = {p.name: p for p in PERMISSIONS}
BY_BIT: Final[dict[int, Permission]] = {p.bit: p for p in PERMISSIONS}

def _alle_bits() -> int:
    mask = 0
    for perm in PERMISSIONS:
        mask |= perm.bit
    return mask


#: Alle gueltigen Bits zusammen -- entspricht ``ChanACL::All`` in ACL.h.
ALL_MASK: Final[int] = _alle_bits()

#: Internes Cache-Flag des Servers. Wird von setACL wegmaskiert, taucht aber in
#: ``effectivePermissions()`` auf und muss dort ausgeblendet werden.
CACHED_BIT: Final[int] = 0x8000000

def _lookup_tabelle() -> dict[str, str]:
    tabelle: dict[str, str] = {}
    for perm in PERMISSIONS:
        tabelle[perm.name.lower()] = perm.name
        tabelle[perm.label.lower()] = perm.name
    return tabelle


#: Kleinschreibung -> kanonischer Name, damit die YAML tolerant sein kann.
_LOOKUP: Final[dict[str, str]] = _lookup_tabelle()
# Haeufige Schreibweisen aus aelteren Mumble-Dokus und der Slice.
_LOOKUP.update(
    {
        "registerself": "SelfRegister",
        "permissionregisterself": "SelfRegister",
        "selfregister": "SelfRegister",
        "resetusercontent": "ResetUserContent",
        "permissionresetusercontent": "ResetUserContent",
        "mutedeafen": "MuteDeafen",
        "maketempchannel": "MakeTempChannel",
        "makechannel": "MakeChannel",
        "linkchannel": "LinkChannel",
        "textmessage": "TextMessage",
    }
)


def mask_to_names(mask: int) -> list[str]:
    """Bitmaske -> sortierte Liste kanonischer Rechtenamen.

    Unbekannte Bits werden als ``0x<hex>`` zurueckgegeben, damit ein
    unerwarteter Wert im GUI sichtbar wird statt lautlos zu verschwinden.
    """
    names: list[str] = []
    remaining = mask & ~CACHED_BIT
    for perm in PERMISSIONS:
        if remaining & perm.bit:
            names.append(perm.name)
            remaining &= ~perm.bit
    if remaining:
        names.append(f"0x{remaining:x}")
    return names


def names_to_mask(names: Iterable[str]) -> int:
    """Rechtenamen -> Bitmaske.

    Akzeptiert kanonische Namen, deutsche Labels und die Slice-Schreibweisen.
    Wirft ``KeyError`` bei einem unbekannten Namen -- die YAML-Validierung
    faengt das ab und meldet es mit Zeilenbezug.
    """
    mask = 0
    for raw in names:
        key = str(raw).strip().lower()
        if not key:
            continue
        try:
            canonical = _LOOKUP[key]
        except KeyError:
            raise KeyError(
                f"Unbekanntes Recht {raw!r}. Erlaubt: "
                + ", ".join(p.name for p in PERMISSIONS)
            ) from None
        mask |= BY_NAME[canonical].bit
    return mask


def describe_mask(mask: int) -> str:
    """Bitmaske -> lesbare deutsche Aufzaehlung fuer Log und Audit."""
    names = mask_to_names(mask)
    if not names:
        return "(keine)"
    return ", ".join(BY_NAME[n].label if n in BY_NAME else n for n in names)


def verify_against_slice(module: object) -> list[str]:
    """Vergleicht unsere Bitwerte mit der einkompilierten Slice.

    Gibt eine Liste von Klartext-Abweichungen zurueck; leer = alles konsistent.
    Wird beim Start aufgerufen. Ein Treffer bedeutet, dass sich Mumble geaendert
    hat und diese Tabelle nachgezogen werden muss -- er wird als Banner im GUI
    und als Warnung im Log ausgegeben, statt den Start zu verhindern.
    """
    problems: list[str] = []
    for perm in PERMISSIONS:
        const = perm.effective_slice_const
        if const is None:
            # Nur Listen: die Slice kennt es nicht, der Server schon.
            if hasattr(module, f"Permission{perm.name}"):
                problems.append(
                    f"{perm.name}: Slice definiert jetzt Permission{perm.name} -- "
                    "permissions.py kann auf die Slice-Konstante umgestellt werden."
                )
            continue
        actual = getattr(module, const, None)
        if actual is None:
            problems.append(
                f"{perm.name}: Slice-Konstante {const} fehlt "
                "(Mumble-Version zu alt oder umbenannt)."
            )
        elif actual != perm.bit:
            problems.append(
                f"{perm.name}: Slice sagt {const}={actual:#x}, "
                f"permissions.py sagt {perm.bit:#x}."
            )
    return problems
