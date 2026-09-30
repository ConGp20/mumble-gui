"""FastAPI-Anwendung: Seiten, Anmeldung, Gesundheitspruefung, Metriken.

Die eigentliche Arbeit steckt in :mod:`intercom.web.api` (JSON) und
:mod:`intercom.web.context` (Zustand und Hintergrundaufgaben). Hier wird nur
zusammengesteckt.

TLS macht bei Bedarf ein Reverse Proxy davor -- die Anwendung liefert bewusst
einfaches HTTP auf ``LISTEN_PORT``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import ConfigError, Settings
from .api import metrics_text
from .api import router as api_router
from .auth import COOKIE_NAME, SESSION_MAX_AGE, Account, client_ip, current_user, require_user
from .context import AppContext
from .pult import router as pult_router

log = logging.getLogger(__name__)

__all__ = ["create_app"]

PACKAGE_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path("/opt/intercom/static")
TEMPLATE_DIR = Path("/opt/intercom/templates")

# In der Entwicklung liegen die Dateien im Quellbaum, im Image unter /opt.
if not STATIC_DIR.exists():
    STATIC_DIR = PACKAGE_DIR / "static"
if not TEMPLATE_DIR.exists():
    TEMPLATE_DIR = PACKAGE_DIR / "templates"


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Ice ist gespraechig und schreibt in seine eigenen Kanaele.
    logging.getLogger("Ice").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context = AppContext(settings)
        app.state.ctx = context
        app.state.settings = settings
        app.state.sessions = context.sessions
        await context.startup()
        log.info(
            "Cockpit hoert auf %s:%s (Konfiguration: %s)",
            settings.listen_host,
            settings.listen_port,
            settings.intercom_config,
        )
        for key, value in settings.redacted().items():
            log.debug("  %-24s %s", key, value)
        try:
            yield
        finally:
            await context.shutdown()

    app = FastAPI(
        title="Stadion-Intercom – Administration",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals["settings"] = settings
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    else:  # pragma: no cover - nur bei kaputtem Image
        log.error("Statische Dateien fehlen unter %s", STATIC_DIR)

    app.include_router(api_router)
    app.include_router(pult_router)

    # ------------------------------------------------------------------ #
    #  Gesundheit und Metriken -- bewusst ohne Anmeldung.
    #
    #  /healthz braucht der Docker-Healthcheck, /metrics ein Prometheus im
    #  Homelab. Beide haengen nur auf dem Host-Netz hinter dem Reverse-Proxy
    #  und geben keine Secrets preis.
    # ------------------------------------------------------------------ #

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        context: AppContext = request.app.state.ctx
        report = context.health()
        # Der Healthcheck soll gruen sein, sobald der Prozess antwortet -- ein
        # kurz nicht erreichbarer murmur darf den Container nicht neu starten
        # lassen, sonst kreisen beide.
        return report

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        """Bewusst ``async``, nicht ``def``.

        FastAPI schiebt eine *synchrone* Pfadfunktion in einen Threadpool.
        Dort laeuft sie neben dem asyncio-Loop -- und ``LiveState`` gehoert dem
        Loop: die Rueckrufe aus Ice heben ihre Ereignisse mit
        ``call_soon_threadsafe`` genau dorthin. Wer aus einem fremden Thread
        ueber ``live.users`` laeuft, waehrend der Loop einen Client eintraegt
        oder entfernt, faengt sich ein "dictionary changed size during
        iteration" -- also einen 500er, und zwar bevorzugt dann, wenn viel
        los ist. Als Koroutine laeuft die Funktion im Loop, und dazwischen
        kommt nichts.

        Voraussetzung dafuer ist, dass hier nichts blockiert: darum die
        Laufzeit aus dem Polling statt frisch per Ice.
        """
        context: AppContext = request.app.state.ctx
        return PlainTextResponse(
            metrics_text(context), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    # ------------------------------------------------------------------ #
    #  Anmeldung
    # ------------------------------------------------------------------ #

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, fehler: str = "") -> Response:
        if current_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"fehler": fehler, "titel": "Anmeldung"}
        )

    @app.post("/login")
    async def login(
        request: Request,
        benutzer: str = Form(...),
        passwort: str = Form(...),
    ) -> Response:
        context: AppContext = request.app.state.ctx
        source = client_ip(request)

        wait = context.sessions.blocked_for(source)
        if wait > 0:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "fehler": f"Zu viele Fehlversuche. Bitte {wait:.0f} Sekunden warten.",
                    "titel": "Anmeldung",
                },
                status_code=429,
            )

        account = context.sessions.authenticate(benutzer, passwort)
        if account is None:
            context.sessions.note_failure(source)
            log.warning("Fehlgeschlagene Anmeldung von %s als %r", source, benutzer)
            context.audit(benutzer, "auth.login", source, ok=False, error="falsche Zugangsdaten")
            return templates.TemplateResponse(
                request,
                "login.html",
                {"fehler": "Benutzer oder Passwort stimmt nicht.", "titel": "Anmeldung"},
                status_code=401,
            )

        context.sessions.clear_failures(source)
        context.audit(account.name, "auth.login", source)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            context.sessions.dump(account),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            # Kein secure=True: der Reverse-Proxy terminiert TLS, aber der
            # Direktzugriff auf http://nas:8080 muss weiter funktionieren.
        )
        return response

    @app.post("/logout")
    def logout(request: Request) -> Response:
        account = current_user(request)
        if account is not None:
            request.app.state.ctx.audit(account.name, "auth.logout", client_ip(request))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # ------------------------------------------------------------------ #
    #  Seiten
    # ------------------------------------------------------------------ #

    def page(name: str, titel: str) -> Callable[..., Response]:
        def render(request: Request, account: Account = Depends(require_user)) -> Response:
            context: AppContext = request.app.state.ctx
            return templates.TemplateResponse(
                request,
                name,
                {
                    "titel": titel,
                    "account": account,
                    "csrf": account.csrf,
                    "health": context.health(),
                    "seite": name.removesuffix(".html"),
                },
            )

        return render

    app.get("/", response_class=HTMLResponse)(page("cockpit.html", "Cockpit"))
    app.get("/pult", response_class=HTMLResponse)(page("pult.html", "Pult"))
    app.get("/kanaele", response_class=HTMLResponse)(page("kanaele.html", "Kanaele"))
    app.get("/acl", response_class=HTMLResponse)(page("acl.html", "ACL-Editor"))
    app.get("/nutzer", response_class=HTMLResponse)(page("nutzer.html", "Nutzer"))
    app.get("/server", response_class=HTMLResponse)(page("server.html", "Server"))
    app.get("/anleitung", response_class=HTMLResponse)(
        page("anleitung.html", "Anleitung")
    )
    app.get("/einrichten", response_class=HTMLResponse)(
        page("einrichten.html", "Einrichten")
    )
    app.get("/audit", response_class=HTMLResponse)(page("audit.html", "Audit-Log"))

    # ------------------------------------------------------------------ #
    #  Fehlerbehandlung
    # ------------------------------------------------------------------ #

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        """Nicht angemeldete Seitenaufrufe landen auf der Anmeldeseite.

        Entschieden wird am Pfad, nicht am ``Accept``-Kopf: dieser Kopf ist
        beliebig setzbar, und ein Browser, der aus irgendeinem Grund nur
        ``*/*`` schickt, bekaeme sonst rohes JSON statt eines Anmeldeformulars.
        Unter ``/api/`` ist JSON dagegen immer richtig -- dort ruft niemand von
        Hand auf.
        """
        from fastapi.responses import JSONResponse

        ist_api = request.url.path.startswith("/api/")
        if exc.status_code == 401 and not ist_api:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    return app


def main() -> int:
    """Einstiegspunkt fuer ``python -m intercom.web.app``."""
    import uvicorn

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}")
        return 2

    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG",
        # Ein Reverse Proxy setzt X-Forwarded-For. Vertraut wird der
        # Angabe aber nur, wenn sie vom Loopback kommt -- der Proxy laeuft auf
        # demselben NAS. Mit "*" wuerde uvicorn den Kopf JEDES Absenders
        # uebernehmen, und ein Angreifer koennte sich mit jeder Anfrage eine
        # neue Adresse geben und damit die Anmeldebremse aushebeln.
        # Steht der Proxy auf einem anderen Host, gehoert dessen Adresse hier
        # hinein (FORWARDED_ALLOW_IPS).
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
