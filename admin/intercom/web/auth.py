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
    "COOKIE_NAME",
    "CSRF_FIELD",
    "CSRF_HEADER",
    "Account",
    "SessionManager",
    "current_user",
    "require_admin",
    "require_user",
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
        #: Fehlversuche insgesamt, unabhaengig von der Quelle. Zweite
        #: Verteidigungslinie: die Quell-IP stammt hinter einem Reverse-Proxy
        #: aus einem Kopf, den der Client mitschickt. Wer sie faelschen kann,
        #: haette mit einer reinen Je-IP-Bremse gar keine.
        self._failures_gesamt: list[float] = []

    # ------------------------------------------------------------------ #

    @staticmethod
    def _gleich(links: str, rechts: str) -> bool:
        """Zeitkonstanter Vergleich, der auch Umlaute vertraegt.

        ``hmac.compare_digest`` wirft bei Zeichenketten mit Nicht-ASCII einen
        ``TypeError`` ("comparing strings with non-ASCII characters is not
        supported"). Genau das passiert bei einem Tippfehler mit Umlaut im
        Benutzernamen oder bei einem Passwort aus der .env, das jemand von Hand
        gesetzt hat. Ungefangen wird daraus eine 500 statt einer Anmeldeseite --
        und weil nur *bestehende* Konten ueberhaupt bis zum Passwortvergleich
        kommen, verraet der Statuscode, welcher Benutzername existiert.
        Auf Bytes verglichen gibt es das Problem nicht.
        """
        return hmac.compare_digest(links.encode("utf-8"), rechts.encode("utf-8"))

    def authenticate(self, username: str, password: str) -> Account | None:
        """Prueft die Zugangsdaten in konstanter Zeit.

        Zeitkonstant heisst hier auch: **ohne Kurzschluss**. Ein
        ``name_ok and passwort_ok`` haette den Passwortvergleich uebersprungen,
        sobald der Name nicht passt -- und damit ueber die Laufzeit verraten,
        welche Benutzernamen es gibt. Beide Vergleiche laufen deshalb immer,
        und beide Konten werden immer geprueft.
        """
        settings = self._settings
        gleich = self._gleich

        admin_name_ok = gleich(username, settings.admin_user)
        admin_pass_ok = gleich(password, settings.admin_password)
        admin_ok = admin_name_ok & admin_pass_ok

        viewer_name_ok = gleich(username, settings.readonly_user or "")
        viewer_pass_ok = gleich(password, settings.readonly_password or "")
        viewer_ok = bool(
            settings.has_readonly_account & (viewer_name_ok & viewer_pass_ok)
        )

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

    #: Mehr verschiedene Quellen als das merken wir uns nicht. Ohne Deckel
    #: waechst die Tabelle mit jeder erfundenen Adresse -- und erfinden kann
    #: sie jeder, der den Weiterleitungskopf setzt.
    MAX_QUELLEN = 4096
    #: So viele Fehlversuche insgesamt in fuenf Minuten, dann bremst es fuer
    #: alle. Grosszuegig genug, dass ein Techniker mit Zahlendreher nicht
    #: ausgesperrt wird, eng genug gegen stumpfes Durchprobieren.
    MAX_GESAMT = 60

    def note_failure(self, client_ip: str) -> None:
        now = time.monotonic()
        attempts = [t for t in self._failures.get(client_ip, []) if now - t < 300]
        attempts.append(now)

        if client_ip not in self._failures and len(self._failures) >= self.MAX_QUELLEN:
            # Aeltesten Eintrag verdraengen, statt unbegrenzt zu wachsen.
            aeltester = min(
                self._failures, key=lambda ip: self._failures[ip][-1] if self._failures[ip] else 0
            )
            self._failures.pop(aeltester, None)
        self._failures[client_ip] = attempts

        self._failures_gesamt = [t for t in self._failures_gesamt if now - t < 300]
        self._failures_gesamt.append(now)

    def blocked_for(self, client_ip: str) -> float:
        """Wie lange diese IP noch warten muss, in Sekunden.

        Ab dem fuenften Fehlversuch in fuenf Minuten wird gebremst. Kein
        dauerhaftes Sperren: im Stadion waere ein ausgesperrter Techniker
        schlimmer als ein langsamer Angreifer.
        """
        now = time.monotonic()
        attempts = [t for t in self._failures.get(client_ip, []) if now - t < 300]
        if attempts:
            self._failures[client_ip] = attempts
        else:
            self._failures.pop(client_ip, None)

        self._failures_gesamt = [t for t in self._failures_gesamt if now - t < 300]
        if len(self._failures_gesamt) >= self.MAX_GESAMT:
            # Greift auch dann, wenn jemand die Quell-IP je Anfrage wechselt.
            return max(0.0, 30.0 - (now - self._failures_gesamt[-1]))

        if len(attempts) < 5:
            return 0.0
        delay = min(30.0, 2.0 ** (len(attempts) - 5))
        remaining = delay - (now - attempts[-1])
        return max(0.0, remaining)

    def clear_failures(self, client_ip: str) -> None:
        self._failures.pop(client_ip, None)
        self._failures_gesamt.clear()

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
    """Beste verfuegbare Quell-IP fuer die Anmeldebremse.

    ``X-Forwarded-For`` ist ein Kopf, den der Client mitschickt. uvicorn traegt
    ihn nur dann in ``request.client`` ein, wenn der unmittelbare Absender in
    ``forwarded_allow_ips`` steht -- deshalb steht dort der Loopback und nicht
    ``*`` (siehe ``intercom.web.app.main``). Wer sich direkt mit dem Port
    verbindet, kann seine Adresse damit nicht mehr frei waehlen.

    Vollstaendig verlassen darf man sich darauf trotzdem nicht: laeuft der
    Reverse-Proxy auf demselben Host, kommt jede Anfrage vom Loopback, und der
    Proxy reicht durch, was der Client geschickt hat. Genau dafuer gibt es
    zusaetzlich die Gesamtbremse in :class:`SessionManager`.
    """
    return request.client.host if request.client else "?"
