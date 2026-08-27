"""Anmeldung, Sitzungen und CSRF-Schutz.

Bewusst klein gehalten: zwei Konten aus der Umgebung, ein signiertes Cookie,
ein CSRF-Token. Keine Nutzerverwaltung, keine Datenbank -- wer mehr braucht,
setzt den Synology-Reverse-Proxy davor.

Rollen
------
``admin``   darf alles (``ADMIN_USER`` / ``ADMIN_PASSWORD``)
``viewer``  darf nur lesen (``ADMIN_READONLY_USER`` / ``ADMIN_READONLY_PASSWORD``)

Der Nur-Lese-Zugang ist optional und faellt aus, wenn kein Passwort gesetzt ist.

HTTPS macht der Reverse-Proxy davor. Deshalb ist das Cookie **nicht** mit
``secure`` markiert -- sonst kaeme es beim Direktzugriff auf ``http://nas:8080``
gar nicht erst an. ``httponly`` und ``samesite=lax`` sind gesetzt.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import Settings

log = logging.getLogger(__name__)

__all__ = [
    "Account",
    "SessionManager",
    "COOKIE_NAME",
    "CSRF_FIELD",
    "CSRF_HEADER",
    "require_user",
    "require_admin",
    "current_user",
]

COOKIE_NAME: Final[str] = "intercom_session"
CSRF_FIELD: Final[str] = "csrf_token"
CSRF_HEADER: Final[str] = "X-CSRF-Token"
#: Nach dieser Zeit ohne Neuanmeldung ist Schluss.
SESSION_MAX_AGE: Final[int] = 12 * 3600
#: Methoden, die etwas veraendern und deshalb ein CSRF-Token brauchen.
UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Account:
    """Der angemeldete Benutzer."""

    name: str
    role: str
    csrf: str

    @property
    def can_write(self) -> bool:
        return self.role == "admin"

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "role": self.role, "can_write": self.can_write}


class SessionManager:
    """Signiert und prueft Sitzungs-Cookies."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(
            settings.session_secret, salt="intercom-session"
        )
        #: Fehlversuche je Quell-IP, gegen stumpfes Durchprobieren.
        self._failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ #

    def authenticate(self, username: str, password: str) -> Account | None:
        """Prueft die Zugangsdaten in konstanter Zeit.

        ``hmac.compare_digest`` statt ``==``, damit die Laufzeit nichts ueber
        das Passwort verraet. Beide Konten werden immer geprueft, damit auch
        die Anzahl der Vergleiche nicht verraet, welcher Name existiert.
        """
        settings = self._settings
        admin_ok = hmac.compare_digest(
            username, settings.admin_user
        ) and hmac.compare_digest(password, settings.admin_password)

        viewer_ok = False
        if settings.has_readonly_account:
            viewer_ok = hmac.compare_digest(
                username, settings.readonly_user or ""
            ) and hmac.compare_digest(password, settings.readonly_password or "")

        if admin_ok:
            return Account(name=settings.admin_user, role="admin", csrf=secrets.token_urlsafe(32))
        if viewer_ok:
            return Account(
                name=settings.readonly_user or "viewer",
                role="viewer",
                csrf=secrets.token_urlsafe(32),
            )
        return None

    # -- Bremse gegen Durchprobieren ---------------------------------------

    def note_failure(self, client_ip: str) -> None:
        now = time.monotonic()
        attempts = [t for t in self._failures.get(client_ip, []) if now - t < 300]
        attempts.append(now)
        self._failures[client_ip] = attempts

    def blocked_for(self, client_ip: str) -> float:
        """Wie lange diese IP noch warten muss, in Sekunden.

        Ab dem fuenften Fehlversuch in fuenf Minuten wird gebremst. Kein
        dauerhaftes Sperren: im Stadion waere ein ausgesperrter Techniker
        schlimmer als ein langsamer Angreifer.
        """
        now = time.monotonic()
        attempts = [t for t in self._failures.get(client_ip, []) if now - t < 300]
        self._failures[client_ip] = attempts
        if len(attempts) < 5:
            return 0.0
        delay = min(30.0, 2.0 ** (len(attempts) - 5))
        remaining = delay - (now - attempts[-1])
        return max(0.0, remaining)

    def clear_failures(self, client_ip: str) -> None:
        self._failures.pop(client_ip, None)

    # -- Cookie -------------------------------------------------------------

    def dump(self, account: Account) -> str:
        return self._serializer.dumps(
            {"name": account.name, "role": account.role, "csrf": account.csrf}
        )

    def load(self, token: str) -> Account | None:
        try:
            data = self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except SignatureExpired:
            return None
        except BadSignature:
            log.warning("Sitzungs-Cookie mit ungueltiger Signatur abgewiesen.")
            return None
        if not isinstance(data, dict):
            return None
        name = str(data.get("name", ""))
        role = str(data.get("role", ""))
        csrf = str(data.get("csrf", ""))
        if role not in {"admin", "viewer"} or not name or not csrf:
            return None
        return Account(name=name, role=role, csrf=csrf)


# --------------------------------------------------------------------------- #
#  Abhaengigkeiten fuer die Routen
# --------------------------------------------------------------------------- #


def _sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def current_user(request: Request) -> Account | None:
    """Der angemeldete Benutzer, oder ``None``. Wirft nicht."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return _sessions(request).load(token)


async def require_user(request: Request) -> Account:
    """Erzwingt eine Anmeldung und prueft bei schreibenden Methoden das CSRF-Token."""
    account = current_user(request)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht angemeldet.",
            headers={"X-Intercom-Login": "/login"},
        )

    if request.method in UNSAFE_METHODS:
        await _check_csrf(request, account)
    return account


async def require_admin(account: Account = Depends(require_user)) -> Account:
    """Wie :func:`require_user`, verlangt aber Schreibrechte."""
    if not account.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dieser Zugang darf nur lesen.",
        )
    return account


async def _check_csrf(request: Request, account: Account) -> None:
    """Token aus Header oder Formularfeld gegen das Cookie pruefen.

    Das Token steckt im signierten Cookie und muss zusaetzlich im Request
    auftauchen. Ein fremder Ursprung kann das Cookie zwar mitschicken lassen,
    seinen Inhalt aber nicht lesen und den Wert daher nicht wiederholen.
    """
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            supplied = str(form.get(CSRF_FIELD, ""))

    if not supplied or not hmac.compare_digest(supplied, account.csrf):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "CSRF-Token fehlt oder passt nicht. Seite neu laden und "
                "erneut anmelden."
            ),
        )


def client_ip(request: Request) -> str:
    """Beste verfuegbare Quell-IP.

    Hinter dem Synology-Reverse-Proxy steht die echte Adresse in
    ``X-Forwarded-For``. Wir nehmen den ersten Eintrag -- weiter vorne stehende
    Werte kann ein Client selbst setzen, aber fuer eine Anmeldebremse reicht das;
    fuer eine Zugriffsentscheidung wuerde es nicht reichen.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"
