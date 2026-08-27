"""Fehlerklassen fuer die Ice-Anbindung.

Der Rest der Anwendung faengt ausschliesslich diese Typen. Ice-eigene
Ausnahmen (``Ice.ConnectionRefusedException`` und Verwandte) werden in
``client.py`` uebersetzt, damit Web-Layer und Provisioner ohne Ice-Import
auskommen.
"""

from __future__ import annotations

__all__ = [
    "IceError",
    "IceNotConnected",
    "IceConnectionLost",
    "IceAuthError",
    "IceCallFailed",
    "SliceMismatch",
]


class IceError(RuntimeError):
    """Basis aller Ice-bezogenen Fehler dieser Anwendung."""


class IceNotConnected(IceError):
    """Es wurde ein Aufruf gemacht, bevor die Verbindung stand."""


class IceConnectionLost(IceError):
    """Die Verbindung zu murmur ist weg (Neustart, Netz, Absturz)."""


class IceAuthError(IceError):
    """murmur hat das Secret abgelehnt.

    Praktisch immer: ``ICE_SECRET`` in der ``.env`` passt nicht zu
    ``icesecretwrite`` in der Serverkonfiguration.
    """


class IceCallFailed(IceError):
    """Ein Aufruf ist fachlich fehlgeschlagen (ungueltiger Kanal, Session, ...).

    ``slice_exception`` traegt den Namen der urspruenglichen Slice-Ausnahme,
    damit das GUI eine sinnvolle deutsche Meldung zeigen kann.
    """

    def __init__(self, message: str, slice_exception: str = "") -> None:
        super().__init__(message)
        self.slice_exception = slice_exception


class SliceMismatch(IceError):
    """Einkompilierte Slice und laufender Server passen nicht zusammen.

    Wird nie geworfen, sondern nur als Warnung gefuehrt -- ein Versionsunter-
    schied macht die meisten Aufrufe noch nicht kaputt, und ein toter Container
    hilft im Stadion niemandem.
    """
