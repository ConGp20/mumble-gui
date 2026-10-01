"""Shows und Sicherung: ganze Aufbauten ablegen, vergleichen, laden.

Eine **Sicherung** ist eine Datei zum Herunterladen und Wegheften. Eine
**Show** ist genau dieselbe Datei, nur unter einem Namen im Admin-Container
abgelegt -- "Landesfinale Halle 1", "Training", "Probe Mittwoch". Wer zwischen
zwei Veranstaltungen umbaut, laedt die andere Show, statt Plaetze und Rollen von
Hand umzustellen.

Laden geht fuer beide denselben Weg, und jeder Schritt hat einen Testlauf, der
nichts schreibt und dieselben Zahlen liefert:

1. **Plaetze, Rollen, Regeln** -- der Planner gleicht den Server gegen die
   Datei ab. Mit "Aufraeumen" loescht er auch, was in der Datei fehlt.
2. **Was nur diese Oberflaeche kennt** -- feste Plaetze, Mithoeren und Vorrang
   je Person, Verbindungen zwischen Plaetzen, Ruftasten, Netzsegmente. Mumble
   merkt sich nichts davon; ohne diesen Schritt fehlte es nach dem Laden, ohne
   dass es jemandem auffiele.
3. **Verbundene Sitzungen folgen sofort** -- der Enforcer wird neu geladen.

Registrierte Personen legt ein Laden nie an und loescht sie nie: Mumble
registriert eine Person an ihrem Zertifikat, und das bringt nur ihr eigenes
Geraet mit. Namen aus der Datei, die der Server nicht kennt, nennt das Ergebnis.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..ice.errors import IceError
from ..store.db import (
    MAX_SHOWNAME,
    RUFTASTEN,
    VERBINDUNGSARTEN,
    WUNSCH_ARTEN,
)
from .auth import Account, require_admin, require_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

__all__ = ["router", "stand_als_yaml"]


def ctx(request: Request) -> Any:
    return request.app.state.ctx


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def _store(context: Any) -> Any:
    if context.store is None:
        raise HTTPException(503, "Der Speicher ist nicht verfügbar.")
    return context.store


# --------------------------------------------------------------------------- #
#  Lesen und Schreiben des ganzen Stands
# --------------------------------------------------------------------------- #


async def stand_als_yaml(context: Any) -> str:
    """Der aktuelle Stand als Datei -- fuer Sicherung und "als Show speichern".

    Der Server liefert Plaetze, Rollen und Regeln; der Store den Rest. Ohne den
    Rest waere die Datei unvollstaendig, und nach dem Laden fehlten feste
    Plaetze, Verbindungen und Ruftasten.
    """
    from ..provision.exporter import export_yaml

    store = context.store
    wunsch = store.alle_wuensche() if store is not None else None
    verbindungen = store.verbindungen() if store is not None else None
    netze = store.netze() if store is not None else None
    ruftasten = store.ruftasten() if store is not None else None
    return str(
        await context.ice.run(
            lambda client: export_yaml(
                client,
                wunsch=wunsch,
                verbindungen=verbindungen,
                netze=netze,
                ruftasten=ruftasten,
            ),
            context.ice.sync,
        )
    )


def _lesen(yaml_text: str, quelle: str) -> tuple[Any, dict[str, Any]]:
    """YAML-Text -> (gepruefte Konfiguration, Rohdaten). Fehler als HTTP 400."""
    from ..provision.schema import ConfigInvalid, parse_config

    try:
        roh = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"Das ist kein gültiges YAML: {exc}") from exc
    if not isinstance(roh, dict):
        raise HTTPException(400, "Die Datei enthält keinen Aufbau.")
    try:
        return parse_config(roh, source=quelle), roh
    except ConfigInvalid as exc:
        raise HTTPException(400, str(exc)) from exc


def _zahl(n: int, eins: str, viele: str) -> str:
    return f"{n} {eins if n == 1 else viele}"


async def _wunsch(
    context: Any, roh: dict[str, Any], *, aufraeumen: bool, dry_run: bool, actor: str
) -> dict[str, Any]:
    """Fester Platz, dauerhaftes Mithoeren, Vorrang -- nach Namen aufgeloest.

    In der Datei steht der Nutzername, nicht die ID: murmur vergibt IDs beim
    Wiederanlegen neu. Wen der Server nicht kennt, wird gemeldet, nicht
    stillschweigend uebergangen.
    """
    store = context.store
    abschnitt = roh.get("wunsch")
    if not isinstance(abschnitt, dict):
        abschnitt = {}
    if not abschnitt and not aufraeumen:
        return {"uebernommen": 0, "fehlend": [], "entfernt": 0}

    try:
        registriert = await context.ice.get_registered_users()
    except IceError:
        return {"uebernommen": 0, "fehlend": [], "entfernt": 0,
                "fehler": "Personen am Server nicht lesbar."}
    nach_name = {name: userid for userid, name in registriert.items()}

    neu: dict[tuple[str, int], list[str]] = {}
    fehlend: set[str] = set()
    for art, je_person in abschnitt.items():
        if art not in WUNSCH_ARTEN or not isinstance(je_person, dict):
            continue
        for name, pfade in je_person.items():
            userid = nach_name.get(str(name))
            if userid is None:
                fehlend.add(str(name))
                continue
            ziele = [str(p) for p in (pfade or []) if p]
            if ziele:
                neu[(str(art), userid)] = ziele

    vorher = {
        (art, userid)
        for art, je_person in store.alle_wuensche().items()
        for userid, ziele in je_person.items()
        if ziele
    }
    entfernt = len(vorher - set(neu)) if aufraeumen else 0
    if not dry_run:
        if aufraeumen:
            store.wuensche_leeren()
        for (art, userid), ziele in neu.items():
            store.set_wunsch(art, userid, ziele, actor)
    return {"uebernommen": len(neu), "fehlend": sorted(fehlend), "entfernt": entfernt}


def _verbindungen(
    context: Any, roh: dict[str, Any], *, aufraeumen: bool, dry_run: bool, actor: str
) -> dict[str, Any]:
    """Verbindungen zwischen Plaetzen. Pfade, die es (noch) nicht gibt, bleiben
    stehen -- sie greifen, sobald der Platz da ist."""
    store = context.store
    abschnitt = roh.get("verbindungen")
    paare: set[tuple[str, str, str]] = set()
    if isinstance(abschnitt, dict):
        for art, je_platz in abschnitt.items():
            if art not in VERBINDUNGSARTEN or not isinstance(je_platz, dict):
                continue
            for von, zielen in je_platz.items():
                for nach in zielen or []:
                    if von and nach and str(von) != str(nach):
                        paare.add((str(art), str(von), str(nach)))
    if not paare and not aufraeumen:
        return {"uebernommen": 0, "neu": 0, "entfernt": 0}

    vorher = {
        (art, von, nach)
        for art, je_platz in store.verbindungen().items()
        for von, ziele in je_platz.items()
        for nach in ziele
    }
    ergebnis = {
        "uebernommen": len(paare),
        "neu": len(paare - vorher),
        "entfernt": len(vorher - paare) if aufraeumen else 0,
    }
    if not dry_run:
        if aufraeumen:
            store.verbindungen_leeren()
        for art, von, nach in sorted(paare):
            store.set_verbindung(art, von, nach, an=True, author=actor)
    return ergebnis


def _ruftasten(
    context: Any, roh: dict[str, Any], *, aufraeumen: bool, dry_run: bool, actor: str
) -> dict[str, Any]:
    """Ruftasten je Platz. In der Datei steht der oberste Platz als "/"."""
    store = context.store
    abschnitt = roh.get("ruftasten")
    belegung: dict[tuple[str, int], str] = {}
    if isinstance(abschnitt, dict):
        for pfad, je_taste in abschnitt.items():
            if not isinstance(je_taste, dict):
                continue
            platz = "" if str(pfad) in ("", "/") else str(pfad).strip("/")
            for taste, rolle in je_taste.items():
                try:
                    nummer = int(taste)
                except (TypeError, ValueError):
                    continue
                if nummer in RUFTASTEN and rolle:
                    belegung[(platz, nummer)] = str(rolle)
    if not belegung and not aufraeumen:
        return {"uebernommen": 0, "geaendert": 0, "entfernt": 0}

    vorher = {
        (platz, taste): rolle
        for platz, je_taste in store.ruftasten().items()
        for taste, rolle in je_taste.items()
    }
    ergebnis = {
        "uebernommen": len(belegung),
        "geaendert": sum(1 for k, r in belegung.items() if vorher.get(k) != r),
        "entfernt": len(set(vorher) - set(belegung)) if aufraeumen else 0,
    }
    if not dry_run:
        if aufraeumen:
            neu: dict[str, dict[int, str]] = {}
            for (platz, taste), rolle in belegung.items():
                neu.setdefault(platz, {})[taste] = rolle
            store.ruftasten_ersetzen(neu)
        else:
            for (platz, taste), rolle in belegung.items():
                store.set_ruftaste(platz, taste, rolle, actor)
    return ergebnis


def _netze(
    context: Any, config: Any, *, aufraeumen: bool, dry_run: bool
) -> dict[str, Any]:
    """Netzsegmente aus ``networks:`` der Datei.

    Ohne Aufraeumen kommen die Segmente der Datei vorne hin und die uebrigen
    bleiben dahinter stehen -- die Reihenfolge entscheidet, welches Segment
    einen Client bekommt, und ohne Haken soll nichts verschwinden.
    """
    store = context.store
    aus_datei = [
        {"name": n.name, "cidr": n.cidr, "notiz": n.note}
        for n in getattr(config, "networks", []) or []
    ]
    vorher = store.netze()
    if not aus_datei and not aufraeumen:
        return {"uebernommen": 0, "entfernt": 0}
    namen = {s["name"] for s in aus_datei}
    bleiben = [] if aufraeumen else [n for n in vorher if n["name"] not in namen]
    ergebnis = {
        "uebernommen": len(aus_datei),
        "entfernt": sum(1 for n in vorher if n["name"] not in namen) if aufraeumen else 0,
    }
    if not dry_run:
        try:
            store.set_netze(aus_datei + bleiben)
        except ValueError as exc:
            return {"uebernommen": 0, "entfernt": 0, "fehler": str(exc)}
        context.netze_laden()
    return ergebnis


def _zeilen(teile: dict[str, dict[str, Any]], dry_run: bool) -> list[str]:
    """Die Zusatzschritte in Saetzen -- genau das, was die Oberflaeche zeigt."""

    def verb(n: int) -> str:
        if dry_run:
            return "würde" if n == 1 else "würden"
        return "wurde" if n == 1 else "wurden"

    zeilen: list[str] = []
    w = teile["wunsch"]
    if w.get("uebernommen"):
        n = w["uebernommen"]
        zeilen.append(
            f"Feste Plätze, Mithören, Vorrang: {_zahl(n, 'Eintrag', 'Einträge')} "
            f"{verb(n)} übernommen."
        )
    if w.get("entfernt"):
        n = w["entfernt"]
        zeilen.append(
            f"Feste Plätze, Mithören, Vorrang: {_zahl(n, 'Eintrag', 'Einträge')} "
            f"{verb(n)} entfernt."
        )
    if w.get("fehlend"):
        zeilen.append(
            "Nicht registriert, deshalb ohne festen Platz, Mithören und Vorrang: "
            + ", ".join(w["fehlend"])
            + ". Sobald sie unter „Personen“ registriert sind, noch einmal laden."
        )

    v = teile["verbindungen"]
    if v.get("uebernommen"):
        zeilen.append(
            f"Verbindungen: {_zahl(v['uebernommen'], 'Verbindung', 'Verbindungen')} "
            f"in der Datei, davon {v['neu']} neu."
        )
    if v.get("entfernt"):
        n = v["entfernt"]
        zeilen.append(f"Verbindungen: {n} {verb(n)} aufgehoben.")

    r = teile["ruftasten"]
    if r.get("uebernommen"):
        zeilen.append(
            f"Ruftasten: {_zahl(r['uebernommen'], 'Belegung', 'Belegungen')} in der "
            f"Datei, davon {r['geaendert']} anders als jetzt."
        )
    if r.get("entfernt"):
        n = r["entfernt"]
        zeilen.append(f"Ruftasten: {n} {verb(n)} freigegeben.")

    nz = teile["netze"]
    if nz.get("uebernommen"):
        zeilen.append(
            f"Netzsegmente: {_zahl(nz['uebernommen'], 'Segment', 'Segmente')} in der Datei."
        )
    if nz.get("entfernt"):
        n = nz["entfernt"]
        zeilen.append(f"Netzsegmente: {n} {verb(n)} entfernt.")

    namen = {"wunsch": "feste Plätze", "verbindungen": "Verbindungen",
             "ruftasten": "Ruftasten", "netze": "Netzsegmente"}
    for schluessel, teil in teile.items():
        if teil.get("fehler"):
            zeilen.append(f"Nicht möglich ({namen[schluessel]}): {teil['fehler']}")
    return zeilen


async def einspielen(
    context: Any,
    yaml_text: str,
    *,
    aufraeumen: bool,
    dry_run: bool,
    actor: str,
    quelle: str,
) -> dict[str, Any]:
    """Der eine Weg fuer Sicherung und Show. Mit ``dry_run`` wird nichts geschrieben."""
    config, roh = _lesen(yaml_text, quelle)
    try:
        plan = await context.anwenden(
            config, dry_run=dry_run, actor=actor, quelle=quelle, prune=aufraeumen
        )
    except (RuntimeError, IceError) as exc:
        raise HTTPException(400, str(exc)) from exc

    antwort: dict[str, Any] = plan.to_json()
    antwort["fehlgeschlagen"] = len(plan.failed)
    if context.store is None:
        antwort["zusatz"] = ["Nicht möglich: der Speicher ist nicht verfügbar – "
                             "feste Plätze, Verbindungen und Ruftasten bleiben, wie sie sind."]
        return antwort

    teile = {
        "wunsch": await _wunsch(
            context, roh, aufraeumen=aufraeumen, dry_run=dry_run, actor=actor
        ),
        "verbindungen": _verbindungen(
            context, roh, aufraeumen=aufraeumen, dry_run=dry_run, actor=actor
        ),
        "ruftasten": _ruftasten(
            context, roh, aufraeumen=aufraeumen, dry_run=dry_run, actor=actor
        ),
        "netze": _netze(context, config, aufraeumen=aufraeumen, dry_run=dry_run),
    }
    antwort.update(teile)
    antwort["zusatz"] = _zeilen(teile, dry_run)

    if not dry_run:
        await context.wunschzustand_neu_laden()
        if antwort["zusatz"]:
            context.audit(actor, "einspielen.zusatz", quelle, after=" ".join(antwort["zusatz"]))
    return antwort


# --------------------------------------------------------------------------- #
#  Sicherung: eine Datei
# --------------------------------------------------------------------------- #


class SicherungBody(BaseModel):
    """Der Inhalt einer Datei, so wie er hochgeladen wurde."""

    yaml_text: str = Field(max_length=2_000_000)
    #: Auch loeschen, was in der Datei fehlt. Aus per Vorgabe -- eine Sicherung
    #: einzuspielen soll nichts wegraeumen, was jemand seither angelegt hat,
    #: solange er es nicht ausdruecklich will.
    aufraeumen: bool = False


@router.get("/provision/export", response_class=PlainTextResponse)
async def provision_export(
    request: Request, account: Account = Depends(require_user)
) -> PlainTextResponse:
    """Der aktuelle Stand als Datei zum Herunterladen."""
    try:
        text = await stand_als_yaml(ctx(request))
    except IceError as exc:
        raise _fail(exc) from exc
    return PlainTextResponse(
        text,
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="intercom-sicherung.yaml"'},
    )


@router.post("/sicherung/plan")
async def sicherung_plan(
    request: Request, body: SicherungBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Testlauf fuer eine hochgeladene Datei. Schreibt nichts."""
    return await einspielen(
        ctx(request), body.yaml_text, aufraeumen=body.aufraeumen,
        dry_run=True, actor=account.name, quelle="Sicherung",
    )


@router.post("/sicherung/einspielen")
async def sicherung_einspielen(
    request: Request, body: SicherungBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Spielt eine hochgeladene Datei ein."""
    return await einspielen(
        ctx(request), body.yaml_text, aufraeumen=body.aufraeumen,
        dry_run=False, actor=account.name, quelle="Sicherung",
    )


# --------------------------------------------------------------------------- #
#  Shows: benannte Aufbauten
# --------------------------------------------------------------------------- #


def _inhalt(yaml_text: str) -> dict[str, Any]:
    """Was steckt in einer Show? Gezaehlt aus dem Text, ohne den Server."""
    try:
        roh = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {"lesbar": False}
    if not isinstance(roh, dict):
        return {"lesbar": False}

    def plaetze(knoten: Any) -> int:
        if not isinstance(knoten, list):
            return 0
        return sum(
            1 + plaetze(k.get("children")) for k in knoten if isinstance(k, dict)
        )

    def summe(abschnitt: Any) -> int:
        if not isinstance(abschnitt, dict):
            return 0
        return sum(
            len(v) if isinstance(v, (list, dict)) else 0
            for je in abschnitt.values()
            if isinstance(je, dict)
            for v in je.values()
        )

    ruftasten = roh.get("ruftasten")
    return {
        "lesbar": True,
        "plaetze": plaetze(roh.get("channels")),
        "rollen": len(roh.get("groups") or []),
        "personen": len(roh.get("users") or {}),
        "verbindungen": summe(roh.get("verbindungen")),
        "ruftasten": sum(
            len(v) for v in ruftasten.values() if isinstance(v, dict)
        ) if isinstance(ruftasten, dict) else 0,
        "netze": len(roh.get("networks") or []),
    }


class ShowSpeichernBody(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_SHOWNAME + 40)
    notiz: str = Field(default="", max_length=2000)
    #: Einen vorhandenen Namen ersetzen. Ohne ist er ein Fehler (409) --
    #: eine Show soll nicht versehentlich durch eine gleichnamige verschwinden.
    ueberschreiben: bool = False


class ShowHochladenBody(ShowSpeichernBody):
    yaml_text: str = Field(max_length=2_000_000)


class ShowLadenBody(BaseModel):
    name: str
    #: Fuer eine Show der Normalfall: der Aufbau soll danach genau so sein.
    aufraeumen: bool = True


class ShowAendernBody(BaseModel):
    name: str
    neuer_name: str | None = Field(default=None, max_length=MAX_SHOWNAME + 40)
    notiz: str | None = Field(default=None, max_length=2000)


def _show_oder_404(store: Any, name: str) -> dict[str, Any]:
    show = store.show(name)
    if show is None:
        raise HTTPException(404, f"Eine Show {name!r} gibt es nicht.")
    return dict(show)


def _ablegen(
    context: Any, body: ShowSpeichernBody, text: str, actor: str, art: str
) -> dict[str, Any]:
    store = _store(context)
    try:
        name = store.show_speichern(
            body.name, text, notiz=body.notiz, author=actor,
            ueberschreiben=body.ueberschreiben,
        )
    except FileExistsError:
        raise HTTPException(
            409, f"Es gibt schon eine Show {body.name.strip()!r}. Überschreiben?"
        ) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    context.audit(actor, art, name, after=body.notiz)
    return {"ok": True, "name": name, "inhalt": _inhalt(text)}


@router.get("/shows")
async def shows_auflisten(
    request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    """Alle Shows mit dem, was in ihnen steckt. Der Text selbst bleibt hier weg."""
    store = _store(ctx(request))
    liste = []
    for eintrag in store.shows():
        show = store.show(eintrag["name"]) or {}
        liste.append({**eintrag, "inhalt": _inhalt(show.get("yaml_text", ""))})
    zuletzt = max(
        (s for s in liste if s.get("geladen")), key=lambda s: s["geladen"], default=None
    )
    return {"shows": liste, "zuletzt_geladen": zuletzt["name"] if zuletzt else None}


@router.post("/shows")
async def show_speichern(
    request: Request, body: ShowSpeichernBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Legt den aktuellen Stand als Show ab."""
    context = ctx(request)
    try:
        text = await stand_als_yaml(context)
    except IceError as exc:
        raise _fail(exc) from exc
    return _ablegen(context, body, text, account.name, "show.speichern")


@router.post("/shows/hochladen")
async def show_hochladen(
    request: Request, body: ShowHochladenBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Legt eine Datei als Show ab -- erst nach Pruefung, nie ungelesen."""
    _lesen(body.yaml_text, f"Show {body.name}")
    return _ablegen(ctx(request), body, body.yaml_text, account.name, "show.hochladen")


@router.get("/shows/datei", response_class=PlainTextResponse)
async def show_datei(
    request: Request,
    name: str = Query(..., max_length=MAX_SHOWNAME + 40),
    account: Account = Depends(require_user),
) -> PlainTextResponse:
    """Die Show als Datei -- genau der Text, der beim Laden verwendet wird."""
    show = _show_oder_404(_store(ctx(request)), name)
    dateiname = "".join(c if c.isalnum() or c in "-_" else "-" for c in show["name"])
    return PlainTextResponse(
        show["yaml_text"],
        media_type="text/yaml; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"show-{dateiname.strip('-') or 'export'}.yaml\"; "
                f"filename*=UTF-8''{quote('show-' + show['name'] + '.yaml')}"
            )
        },
    )


@router.post("/shows/plan")
async def show_plan(
    request: Request, body: ShowLadenBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Testlauf: was wuerde sich aendern, wenn diese Show jetzt geladen wird?"""
    context = ctx(request)
    show = _show_oder_404(_store(context), body.name)
    return await einspielen(
        context, show["yaml_text"], aufraeumen=body.aufraeumen,
        dry_run=True, actor=account.name, quelle=f"Show {show['name']}",
    )


@router.post("/shows/laden")
async def show_laden(
    request: Request, body: ShowLadenBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Laedt eine Show: Server und Oberflaeche stehen danach auf ihrem Stand."""
    context = ctx(request)
    store = _store(context)
    show = _show_oder_404(store, body.name)
    antwort = await einspielen(
        context, show["yaml_text"], aufraeumen=body.aufraeumen,
        dry_run=False, actor=account.name, quelle=f"Show {show['name']}",
    )
    if not antwort["fehlgeschlagen"]:
        store.show_geladen(show["name"], account.name)
    context.audit(
        account.name, "show.laden", show["name"],
        after="mit Aufräumen" if body.aufraeumen else "nur ergänzen",
        ok=not antwort["fehlgeschlagen"],
        error=f"{antwort['fehlgeschlagen']} Schritte fehlgeschlagen"
        if antwort["fehlgeschlagen"] else "",
    )
    return antwort


@router.patch("/shows")
async def show_aendern(
    request: Request, body: ShowAendernBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Umbenennen oder Notiz aendern. Der Inhalt bleibt, wie er ist."""
    context = ctx(request)
    store = _store(context)
    try:
        name = store.show_aendern(body.name, neuer_name=body.neuer_name, notiz=body.notiz)
    except KeyError:
        raise HTTPException(404, f"Eine Show {body.name!r} gibt es nicht.") from None
    except FileExistsError as exc:
        raise HTTPException(409, f"Es gibt schon eine Show {exc.args[0]!r}.") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    context.audit(account.name, "show.aendern", body.name, after=name)
    return {"ok": True, "name": name}


@router.delete("/shows")
async def show_loeschen(
    request: Request,
    name: str = Query(..., max_length=MAX_SHOWNAME + 40),
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    """Loescht nur die abgelegte Show -- am Server aendert das nichts."""
    context = ctx(request)
    if not _store(context).show_loeschen(name):
        raise HTTPException(404, f"Eine Show {name!r} gibt es nicht.")
    context.audit(account.name, "show.loeschen", name)
    return {"ok": True}
