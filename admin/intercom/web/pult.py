"""Das Pult: Plaetze, Rollen und Personen auf einer Flaeche.

Warum es diese Seite zusaetzlich zu ``/kanaele``, ``/nutzer`` und ``/acl``
gibt: jene drei zeigen Mumble so, wie Mumble gebaut ist -- Kanaele, registrierte
Nutzer, eine Rechtematrix aus siebzehn Bits. Wer eine Veranstaltung aufbaut,
denkt aber nicht in Bits, sondern in "wer sitzt wo und darf wohin reden".

Diese Schnittstelle liefert genau das, und zwar **nur was der Server wirklich
haelt**. Drei Regeln halten das durch:

1. Die Rechte werden nicht aus unseren eigenen Vorgaben abgeleitet, sondern aus
   den ACLs des Servers ausgerechnet -- mit :mod:`intercom.ice.wirkung`, das
   gegen ``effectivePermissions`` geprueft ist.
2. Was sich ohne verbundene Sitzung nicht entscheiden laesst, wird als
   "kommt drauf an" geliefert und nicht geraten.
3. Eine Zelle, die das Raster nicht verlustfrei schreiben kann, wird als nicht
   bearbeitbar geliefert -- mit Begruendung und Verweis auf die Expertensicht.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..ice.errors import IceError
from ..ice.types import ACLEntry, ChannelACL, ChannelGroup, MumbleChannel
from ..ice.wirkung import (
    ENTER,
    LISTEN,
    SPEAK,
    TEXT_MESSAGE,
    WHISPER,
    Wirkung,
    rechte_einer_person,
    rechte_einer_rolle,
)
from ..ruftasten import EINGEBAUTE_ZIELE, abdeckung, wirksame_belegung
from ..ruftasten import pfade as pfade_der_plaetze
from ..store.db import RUFTASTEN, VERBINDUNGSARTEN, WUNSCH_ARTEN, ruf_gruppe
from ..woerter import UEBERALL
from .auth import Account, require_admin, require_user

router = APIRouter(prefix="/api/pult")

__all__ = ["router"]


def ctx(request: Request) -> Any:
    return request.app.state.ctx


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


#: Die fuenf Rechte, in denen eine Eventintercom gedacht wird. Die restlichen
#: zwoelf sind Verwaltungsrechte und bleiben der Expertensicht vorbehalten.
RECHTE: tuple[tuple[str, int, str, str], ...] = (
    (
        "betreten",
        ENTER,
        "Betreten",
        "Darf auf diesen Platz wechseln.",
    ),
    (
        "sprechen",
        SPEAK,
        "Sprechen",
        "Darf hier senden, wenn er auf dem Platz ist.",
    ),
    (
        "hoeren",
        LISTEN,
        "Mithören",
        "Darf diesen Platz mithören, ohne darauf zu sein.",
    ),
    (
        "reinschalten",
        WHISPER,
        "Reinschalten",
        "Darf von woanders hier hineinsprechen (Flüstern).",
    ),
    (
        "schreiben",
        TEXT_MESSAGE,
        "Schreiben",
        "Darf hier Textnachrichten senden.",
    ),
)

#: Die eingebauten Gruppen, die als Zeile sinnvoll sind. ``in``/``out``/``sub``
#: haengen am Aufenthaltsort und ergeben als feste Zeile keinen Sinn.
EINGEBAUTE_ROLLEN: tuple[tuple[str, str], ...] = (
    ("all", "Alle"),
    ("auth", "Angemeldete"),
)


def _bit(schluessel: str) -> int:
    for name, bit, _, _ in RECHTE:
        if name == schluessel:
            return bit
    raise HTTPException(400, f"Unbekanntes Recht {schluessel!r}.")


def _antwort(w: Wirkung) -> dict[str, bool | None]:
    """Die fuenf Rechte als Ja / Nein / kommt drauf an."""
    return {name: w.darf(bit) for name, bit, _, _ in RECHTE}


def _eigene_eintraege(acl: ChannelACL, rolle: str) -> list[ACLEntry]:
    """Die Eintraege, die *dieser* Kanal fuer die Rolle selbst haelt."""
    return [e for e in acl.own_acls() if e.group == rolle and e.userid == -1]


def _eigener_stand(acl: ChannelACL, rolle: str) -> dict[str, str]:
    """Was der Kanal selbst setzt -- unabhaengig davon, was dabei herauskommt."""
    eintraege = _eigene_eintraege(acl, rolle)
    stand: dict[str, str] = {}
    for name, bit, _, _ in RECHTE:
        wert = "offen"
        for eintrag in eintraege:
            if eintrag.allow & bit:
                wert = "erlaubt"
            if eintrag.deny & bit:
                wert = "verboten"
        stand[name] = wert
    return stand


def _bearbeitbar(acl: ChannelACL, rolle: str) -> tuple[bool, str]:
    """Kann das Raster diese Zelle schreiben, ohne etwas kaputtzumachen?

    Nein, sobald der Kanal mehrere eigene Eintraege fuer dieselbe Rolle haelt:
    welcher davon gewinnt, haengt an der Reihenfolge, und ein Raster mit drei
    Zustaenden je Zelle kann das nicht abbilden. Statt zu raten wird die Zelle
    gesperrt und auf die Expertensicht verwiesen.
    """
    eintraege = _eigene_eintraege(acl, rolle)
    if len(eintraege) > 1:
        return False, (
            f"An diesem Platz stehen {len(eintraege)} eigene Regeln fuer diese Rolle. "
            "Welche gewinnt, haengt an ihrer Reihenfolge -- das kann ein Raster "
            "mit drei Zustaenden nicht abbilden. Unter 'Rechte im Original' "
            "bearbeiten."
        )
    if eintraege and not eintraege[0].apply_here:
        return False, (
            "Die vorhandene Regel gilt nur fuer Plaetze darunter, nicht fuer diesen. "
            "Unter 'Rechte im Original' bearbeiten."
        )
    return True, ""


def _baum(kanaele: dict[int, MumbleChannel]) -> list[dict[str, Any]]:
    """Kanaele in Anzeigereihenfolge, mit Tiefe und Pfad."""
    kinder: dict[int, list[MumbleChannel]] = {}
    for kanal in kanaele.values():
        if kanal.id != 0:
            kinder.setdefault(kanal.parent, []).append(kanal)
    for liste in kinder.values():
        liste.sort(key=lambda k: (k.position, k.name.lower()))

    reihen: list[dict[str, Any]] = []

    def lauf(kanal_id: int, tiefe: int, pfad: str) -> None:
        kanal = kanaele.get(kanal_id)
        if kanal is None:
            return
        # Der oberste Platz heisst hier immer "Ueberall", egal wie murmur ihn
        # nennt: frisch aufgesetzte Server tragen dort "Root" ein, aeltere
        # nichts. Beides sagt vor Ort niemandem etwas.
        name = UEBERALL if kanal_id == 0 else (kanal.name or UEBERALL)
        voll = name if not pfad else f"{pfad}/{name}"
        reihen.append(
            {
                "id": kanal_id,
                "name": name,
                "parent": kanal.parent,
                "tiefe": tiefe,
                "pfad": voll,
                "wurzel": kanal_id == 0,
            }
        )
        for kind in kinder.get(kanal_id, []):
            lauf(kind.id, tiefe + 1, voll if kanal_id != 0 else "")

    lauf(0, 0, "")
    return reihen


async def _alles_lesen(context: Any) -> tuple[
    dict[int, MumbleChannel], dict[int, ChannelACL]
]:
    kanaele = await context.ice.get_channels()
    acls: dict[int, ChannelACL] = {}
    for kanal_id in kanaele:
        acls[kanal_id] = await context.ice.get_acl(kanal_id)
    return kanaele, acls


@router.get("")
async def pult(request: Request, account: Account = Depends(require_user)) -> dict[str, Any]:
    """Alles, was die Pult-Oberflaeche braucht, in einem Zug."""
    context = ctx(request)
    try:
        kanaele, acls = await _alles_lesen(context)
        registriert = await context.ice.get_registered_users()
    except IceError as exc:
        raise _fail(exc) from exc

    wurzel = acls.get(0)
    rollen: list[dict[str, str]] = [
        {"name": name, "titel": titel, "eingebaut": "ja"}
        for name, titel in EINGEBAUTE_ROLLEN
    ]
    mitglieder: dict[str, list[int]] = {}
    if wurzel is not None:
        for gruppe in sorted(wurzel.own_groups(), key=lambda g: g.name.lower()):
            rollen.append({"name": gruppe.name, "titel": gruppe.name, "eingebaut": ""})
            mitglieder[gruppe.name] = sorted(gruppe.add)

    # Verbundene Clients
    verbunden: dict[int, list[dict[str, Any]]] = {}
    online_von: dict[int, int] = {}
    for user in context.live.users.values():
        verbunden.setdefault(user.channel, []).append(
            {
                "session": user.session,
                "name": user.name,
                "userid": user.userid,
                "registriert": user.registered,
                "stumm": bool(getattr(user, "mute", False) or getattr(user, "self_mute", False)),
                "taub": bool(getattr(user, "deaf", False) or getattr(user, "self_deaf", False)),
            }
        )
        if user.registered and user.userid >= 0:
            online_von[user.userid] = user.session

    personen = [
        {
            "userid": userid,
            "name": name,
            "rollen": sorted(r for r, m in mitglieder.items() if userid in m),
            "session": online_von.get(userid),
        }
        for userid, name in sorted(registriert.items(), key=lambda p: p[1].lower())
    ]

    rechte: dict[str, dict[str, Any]] = {}
    for kanal_id in kanaele:
        je_rolle: dict[str, Any] = {}
        acl = acls.get(kanal_id)
        for rolle in rollen:
            name = rolle["name"]
            w = rechte_einer_rolle(
                rolle=name, ziel=kanal_id, kanaele=kanaele, acls=acls
            )
            frei, grund = _bearbeitbar(acl, name) if acl else (False, "ACL nicht lesbar.")
            je_rolle[name] = {
                "wirkung": _antwort(w),
                "eigen": _eigener_stand(acl, name) if acl else {},
                "bearbeitbar": frei,
                "grund": grund,
                "gruende": list(w.gruende),
            }
        rechte[str(kanal_id)] = je_rolle

    # Verbindungen, gleich nach Kanal-ID aufgeloest -- die Uebersicht soll sie
    # zeigen koennen, ohne je Platz noch einmal nachzufragen.
    baum = _baum(kanaele)
    nach_pfad = {k["pfad"]: k["id"] for k in baum if k["id"] != 0}
    roh = context.store.verbindungen() if context.store is not None else {}
    verbindungen: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for art, je_platz in roh.items():
        eintraege: dict[str, list[dict[str, Any]]] = {}
        for von, zielen in je_platz.items():
            quelle = nach_pfad.get(von)
            if quelle is None:
                continue
            eintraege[str(quelle)] = [
                {
                    "kanal": nach_pfad.get(z),
                    "pfad": z,
                    "name": z.rsplit("/", 1)[-1],
                }
                for z in zielen
            ]
        verbindungen[art] = eintraege

    # Ruftasten je Platz, mit Vererbung aufgeloest. "eigen" sagt, ob sie hier
    # gesetzt sind oder von weiter oben kommen -- in der Uebersicht soll man
    # sehen, wo man sie aendern muss.
    belegung = context.store.ruftasten() if context.store is not None else {}
    nach_pfad_alle = pfade_der_plaetze(kanaele)
    ruftasten_je_platz: dict[str, list[dict[str, Any]]] = {}
    for kid in kanaele:
        wirk = wirksame_belegung(kid, kanaele, belegung)
        if wirk:
            ruftasten_je_platz[str(kid)] = [
                {
                    "taste": b.taste,
                    "rolle": b.rolle,
                    "eigen": b.von == nach_pfad_alle.get(kid, ""),
                }
                for b in sorted(wirk.values(), key=lambda b: b.taste)
            ]

    return {
        "kanaele": baum,
        "rollen": rollen,
        "personen": personen,
        "verbindungen": verbindungen,
        "ruftasten": ruftasten_je_platz,
        "verbunden": {str(k): v for k, v in verbunden.items()},
        "rechte": rechte,
        "spalten": [
            {"name": n, "titel": t, "hilfe": h} for n, _, t, h in RECHTE
        ],
    }


@router.get("/person/{userid}")
async def person(
    userid: int, request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    """Was darf diese Person wo? Die Probe aufs Exempel."""
    context = ctx(request)
    try:
        kanaele, acls = await _alles_lesen(context)
        registriert = await context.ice.get_registered_users()
    except IceError as exc:
        raise _fail(exc) from exc

    if userid not in registriert:
        raise HTTPException(404, "Diese Person ist nicht registriert.")

    sitzt_in = next(
        (
            u.channel
            for u in context.live.users.values()
            if u.registered and u.userid == userid
        ),
        None,
    )

    zeilen = []
    for kanal in _baum(kanaele):
        w = rechte_einer_person(
            userid=userid,
            ziel=kanal["id"],
            kanaele=kanaele,
            acls=acls,
            sitzt_in=sitzt_in if sitzt_in is not None else kanal["id"],
        )
        zeilen.append({**kanal, "wirkung": _antwort(w), "gruende": list(w.gruende)})

    return {
        "userid": userid,
        "name": registriert[userid],
        "online": sitzt_in is not None,
        "sitzt_in": sitzt_in,
        "angenommen": sitzt_in is None,
        "kanaele": zeilen,
        "spalten": [{"name": n, "titel": t, "hilfe": h} for n, _, t, h in RECHTE],
    }


class RechtBody(BaseModel):
    kanal: int
    rolle: str
    recht: str
    wert: str  # erlaubt | verboten | offen


@router.put("/recht")
async def recht_setzen(
    request: Request, body: RechtBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Setzt ein einzelnes Recht einer Rolle an einem Platz.

    Geschrieben wird immer nur der **eigene** Eintrag dieses Kanals. Was dabei
    herauskommt, entscheidet der Server -- deshalb wird danach neu gerechnet und
    das Ergebnis zurueckgegeben, statt die Absicht zu bestaetigen. Klickt jemand
    "erlaubt" und ein Eintrag weiter unten verbietet es trotzdem, zeigt die
    Oberflaeche das Verbot.
    """
    if body.wert not in {"erlaubt", "verboten", "offen"}:
        raise HTTPException(400, f"Unbekannter Wert {body.wert!r}.")
    bit = _bit(body.recht)
    context = ctx(request)

    try:
        acl = await context.ice.get_acl(body.kanal)
    except IceError as exc:
        raise _fail(exc) from exc

    frei, grund = _bearbeitbar(acl, body.rolle)
    if not frei:
        raise HTTPException(409, grund)

    vorher = _eigener_stand(acl, body.rolle)
    eintraege = _eigene_eintraege(acl, body.rolle)
    if eintraege:
        eintrag = eintraege[0]
    else:
        if body.wert == "offen":
            # Nichts zu tun -- und vor allem kein leerer Eintrag, der nur
            # Verwirrung stiftet.
            return {"ok": True, "unveraendert": True}
        eintrag = ACLEntry(
            apply_here=True, apply_subs=True, allow=0, deny=0, group=body.rolle
        )
        acl.acls.append(eintrag)

    eintrag.allow &= ~bit
    eintrag.deny &= ~bit
    if body.wert == "erlaubt":
        eintrag.allow |= bit
    elif body.wert == "verboten":
        eintrag.deny |= bit

    if not eintrag.allow and not eintrag.deny:
        # Ein Eintrag, der nichts erlaubt und nichts verbietet, tut nichts --
        # er bliebe als Leiche stehen und wuerde spaeter jemanden ratlos machen.
        acl.acls = [e for e in acl.acls if e is not eintrag]

    try:
        await context.ice.set_channel_acl(acl)
        kanaele = await context.ice.get_channels()
        acls = {cid: await context.ice.get_acl(cid) for cid in kanaele}
    except IceError as exc:
        context.audit(
            account.name,
            "pult.recht",
            f"{context.live.channel_name(body.kanal)}/{body.rolle}",
            ok=False,
            error=str(exc),
        )
        raise _fail(exc) from exc

    neu = acls.get(body.kanal)
    context.audit(
        account.name,
        "pult.recht",
        f"{context.live.channel_name(body.kanal)}/{body.rolle}",
        before=f"{body.recht}={vorher.get(body.recht, 'offen')}",
        after=f"{body.recht}={body.wert}",
    )

    w = rechte_einer_rolle(
        rolle=body.rolle, ziel=body.kanal, kanaele=kanaele, acls=acls
    )
    frei, grund = _bearbeitbar(neu, body.rolle) if neu else (False, "ACL nicht lesbar.")
    return {
        "ok": True,
        "wirkung": _antwort(w),
        "eigen": _eigener_stand(neu, body.rolle) if neu else {},
        "bearbeitbar": frei,
        "grund": grund,
        "gruende": list(w.gruende),
    }


# --------------------------------------------------------------------------- #
#  Ziehen und Ablegen
# --------------------------------------------------------------------------- #


class RolleBody(BaseModel):
    userid: int
    rolle: str
    drin: bool


@router.put("/rolle")
async def rolle_setzen(
    request: Request, body: RolleBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Nimmt eine Person in eine Rolle auf oder heraus.

    Geschrieben wird die Gruppe am Wurzelkanal per ``setACL`` -- nur das ist
    dauerhaft. ``addUserToGroup`` waere temporaer und ueberlebte keinen
    Serverneustart (siehe DECISIONS D-005).
    """
    if body.rolle in {name for name, _ in EINGEBAUTE_ROLLEN}:
        raise HTTPException(
            400,
            f"{body.rolle!r} ist eine eingebaute Rolle von Mumble. Wer dazugehört, "
            "ergibt sich von selbst und lässt sich nicht von Hand setzen.",
        )
    context = ctx(request)
    try:
        wurzel = await context.ice.get_acl(0)
    except IceError as exc:
        raise _fail(exc) from exc

    gruppen = list(wurzel.own_groups())
    gruppe = next((g for g in gruppen if g.name == body.rolle), None)
    if gruppe is None:
        if not body.drin:
            return {"ok": True, "unveraendert": True}
        gruppe = ChannelGroup(name=body.rolle, add=[])
        gruppen.append(gruppe)

    vorher = sorted(gruppe.add)
    mitglieder = set(gruppe.add)
    if body.drin:
        mitglieder.add(body.userid)
    else:
        mitglieder.discard(body.userid)
    gruppe.add = sorted(mitglieder)
    if vorher == gruppe.add:
        return {"ok": True, "unveraendert": True}

    wurzel.groups = gruppen
    try:
        await context.ice.set_channel_acl(wurzel)
    except IceError as exc:
        context.audit(
            account.name, "pult.rolle", body.rolle, ok=False, error=str(exc)
        )
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "pult.rolle",
        f"{body.rolle}/{body.userid}",
        before=",".join(str(u) for u in vorher),
        after=",".join(str(u) for u in gruppe.add),
    )
    await context.ice.run(context.enforcer.refresh_membership)
    return {"ok": True, "mitglieder": gruppe.add}


class PlatzBody(BaseModel):
    userid: int | None = None
    session: int | None = None
    kanal: int
    #: True: den Platz zusaetzlich als festen Platz merken.
    merken: bool = False


@router.post("/platz")
async def platz_setzen(
    request: Request, body: PlatzBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Setzt jemanden auf einen Platz.

    Zwei verschiedene Dinge, die die Oberflaeche auseinanderhalten muss:

    * Ist die Person **verbunden**, verschiebt ``setState`` sie sofort. Das ist
      echt und sofort sichtbar -- aber nur fuer diese Verbindung.
    * Ein **fester Platz** ist etwas anderes: Mumble kennt ihn nicht
      (``enum UserInfo`` hat kein Kanalfeld). Er wird bei uns hinterlegt und
      nach jedem Verbinden einmal hergestellt.

    Wer nur zieht, verschiebt. Wer ``merken`` setzt, legt zusaetzlich den festen
    Platz fest.
    """
    context = ctx(request)
    kanaele = None
    ergebnis: dict[str, Any] = {"ok": True, "verschoben": False, "gemerkt": False}

    session = body.session
    if session is None and body.userid is not None:
        session = next(
            (
                u.session
                for u in context.live.users.values()
                if u.registered and u.userid == body.userid
            ),
            None,
        )

    if session is not None:
        try:
            vorher = await context.ice.get_state(session)
            await context.ice.set_user_state(session, channel=body.kanal)
        except IceError as exc:
            context.audit(
                account.name, "pult.platz", str(session), ok=False, error=str(exc)
            )
            raise _fail(exc) from exc
        context.audit(
            account.name,
            "pult.platz",
            vorher.name,
            before=context.live.channel_name(vorher.channel),
            after=context.live.channel_name(body.kanal),
        )
        ergebnis["verschoben"] = True

    if body.merken:
        if body.userid is None:
            raise HTTPException(
                400,
                "Ein fester Platz braucht eine registrierte Person -- ohne "
                "Registrierung erkennt der Server sie beim nächsten Verbinden "
                "nicht wieder. Anzulegen unter Personen.",
            )
        try:
            kanaele = await context.ice.get_channels()
        except IceError as exc:
            raise _fail(exc) from exc
        pfad = _pfad_von(kanaele, body.kanal)
        if pfad is None:
            raise HTTPException(404, "Diesen Platz gibt es nicht.")
        _store(context).set_wunsch("platz", body.userid, [pfad], account.name)
        context.enforcer.lade_wuensche(_store(context).alle_wuensche(), kanaele)
        context.audit(account.name, "pult.fester-platz", f"{body.userid}", after=pfad)
        ergebnis["gemerkt"] = True
        ergebnis["pfad"] = pfad

    if not ergebnis["verschoben"] and not ergebnis["gemerkt"]:
        raise HTTPException(
            409,
            "Diese Person ist nicht verbunden. Ein Platz lässt sich nur "
            "verschieben, solange jemand online ist -- oder als fester Platz "
            "merken.",
        )
    return ergebnis


class WunschBody(BaseModel):
    art: str
    userid: int
    kanaele: list[int]


@router.put("/wunsch")
async def wunsch_setzen(
    request: Request, body: WunschBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Legt fest, was nach jedem Verbinden wiederhergestellt werden soll.

    Die drei Arten sind genau die, die Mumble selbst vergisst: der feste Platz,
    dauerhaftes Mithoeren und der Vorrang beim Sprechen. Alles andere braucht
    das nicht -- es steht am Server.
    """
    if body.art not in WUNSCH_ARTEN:
        raise HTTPException(400, f"Unbekannte Art {body.art!r}.")
    context = ctx(request)
    try:
        kanaele = await context.ice.get_channels()
    except IceError as exc:
        raise _fail(exc) from exc

    pfade: list[str] = []
    for kanal_id in body.kanaele:
        pfad = _pfad_von(kanaele, kanal_id)
        if pfad is None:
            raise HTTPException(404, f"Platz {kanal_id} gibt es nicht.")
        pfade.append(pfad)
    if body.art == "platz" and len(pfade) > 1:
        raise HTTPException(400, "Ein fester Platz, nicht mehrere.")

    store = _store(context)
    store.set_wunsch(body.art, body.userid, pfade, account.name)
    context.enforcer.lade_wuensche(store.alle_wuensche(), kanaele)
    context.audit(
        account.name, f"pult.wunsch.{body.art}", str(body.userid), after=", ".join(pfade)
    )
    return {"ok": True, "pfade": pfade}


@router.get("/wunsch")
async def wunsch_lesen(
    request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    """Der hinterlegte Wunschzustand, mit aufgeloesten Kanal-IDs.

    Ein Pfad, den es nicht mehr gibt, wird als solcher geliefert -- die
    Oberflaeche zeigt ihn dann als offenen Posten an, statt ihn zu verschweigen.
    """
    context = ctx(request)
    try:
        kanaele = await context.ice.get_channels()
    except IceError as exc:
        raise _fail(exc) from exc
    nach_pfad = {
        pfad: kanal_id for kanal_id, pfad in _alle_pfade(kanaele).items()
    }

    store = _store(context)
    ergebnis: dict[str, Any] = {}
    for art, je_person in store.alle_wuensche().items():
        ergebnis[art] = {
            str(userid): [
                {"pfad": pfad, "kanal": nach_pfad.get(pfad)} for pfad in pfade
            ]
            for userid, pfade in je_person.items()
        }
    return {"wunsch": ergebnis, "arten": list(WUNSCH_ARTEN)}


def _store(context: Any) -> Any:
    if context.store is None:
        raise HTTPException(
            503,
            "Der Verlaufsspeicher ist nicht offen -- ohne ihn lässt sich kein "
            "Wunschzustand hinterlegen.",
        )
    return context.store


def _alle_pfade(kanaele: dict[int, MumbleChannel]) -> dict[int, str]:
    """Kanal-ID -> Pfad ohne den Wurzelnamen."""
    return {kanal["id"]: kanal["pfad"] for kanal in _baum(kanaele) if kanal["id"] != 0}


def _pfad_von(kanaele: dict[int, MumbleChannel], kanal_id: int) -> str | None:
    if kanal_id == 0:
        return ""
    return _alle_pfade(kanaele).get(kanal_id)


# --------------------------------------------------------------------------- #
#  Der einzelne Platz: die Sicht, in der eine Intercom gedacht wird
# --------------------------------------------------------------------------- #


def _rollen_die_sprechen(
    kanal_id: int,
    kanaele: dict[int, MumbleChannel],
    acls: dict[int, ChannelACL],
    rollen: list[str],
) -> list[str]:
    """Wer darf an diesem Platz senden?

    Das ist die Menge, um die es bei einer Verbindung geht: wenn Kampfgericht 1
    die Zeitmessung hoeren soll, dann brauchen **die** das Recht -- nicht
    irgendwer.
    """
    duerfen = []
    for rolle in rollen:
        w = rechte_einer_rolle(
            rolle=rolle, ziel=kanal_id, kanaele=kanaele, acls=acls
        )
        if w.darf(SPEAK) is True:
            duerfen.append(rolle)
    return duerfen


def _eigene_rollen(acls: dict[int, ChannelACL]) -> list[str]:
    """Die selbst angelegten Rollen -- ohne Mumbles eingebaute."""
    wurzel = acls.get(0)
    if wurzel is None:
        return []
    return sorted((g.name for g in wurzel.own_groups()), key=str.lower)


@router.get("/platz/{kanal_id}")
async def platz(
    kanal_id: int, request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    """Alles ueber einen Platz -- aus der Sicht des Platzes.

    Beantwortet die beiden Fragen, die man vor Ort stellt:

    * **Wer ist hier?** Welche Rollen duerfen betreten, sprechen, schreiben.
    * **Mit wem?** Welche anderen Plaetze hoert man von hier, in welche darf
      man von hier reinschalten -- und umgekehrt, wer hoert diesen Platz und
      wer darf hier hineinsprechen.
    """
    context = ctx(request)
    try:
        kanaele, acls = await _alles_lesen(context)
    except IceError as exc:
        raise _fail(exc) from exc
    if kanal_id not in kanaele:
        raise HTTPException(404, "Diesen Platz gibt es nicht.")

    baum = _baum(kanaele)
    nach_id = {k["id"]: k for k in baum}
    pfade = {k["id"]: k["pfad"] for k in baum}
    nach_pfad = {pfad: kid for kid, pfad in pfade.items()}

    rollen = _eigene_rollen(acls)
    eingebaut = [name for name, _ in EINGEBAUTE_ROLLEN]

    hier = []
    for rolle in eingebaut + rollen:
        w = rechte_einer_rolle(
            rolle=rolle, ziel=kanal_id, kanaele=kanaele, acls=acls
        )
        acl = acls.get(kanal_id)
        frei, grund = _bearbeitbar(acl, rolle) if acl else (False, "Nicht lesbar.")
        hier.append(
            {
                "name": rolle,
                "titel": dict(EINGEBAUTE_ROLLEN).get(rolle, rolle),
                "eingebaut": rolle in eingebaut,
                "wirkung": _antwort(w),
                "eigen": _eigener_stand(acl, rolle) if acl else {},
                "bearbeitbar": frei,
                "grund": grund,
            }
        )

    store = context.store
    roh = store.verbindungen() if store is not None else {}
    eigener_pfad = pfade.get(kanal_id, "")

    def ziele(art: str) -> list[dict[str, Any]]:
        return [
            {"kanal": nach_pfad.get(p), "pfad": p, "name": _kurzname(p)}
            for p in roh.get(art, {}).get(eigener_pfad, [])
        ]

    def quellen(art: str) -> list[dict[str, Any]]:
        gefunden = []
        for von, zielliste in roh.get(art, {}).items():
            if eigener_pfad in zielliste:
                gefunden.append(
                    {"kanal": nach_pfad.get(von), "pfad": von, "name": _kurzname(von)}
                )
        return gefunden

    return {
        "platz": nach_id.get(kanal_id),
        "kanaele": baum,
        "hier": hier,
        "spricht_hier": _rollen_die_sprechen(kanal_id, kanaele, acls, rollen),
        "hoert": ziele("hoert"),
        "reinschalten": ziele("reinschalten"),
        "wird_gehoert_von": quellen("hoert"),
        "reinschalten_von": quellen("reinschalten"),
        "ruftasten": _ruftasten_blatt(kanal_id, kanaele, acls, rollen, store),
        "rufziele": [
            {"name": n, "titel": t} for n, t in EINGEBAUTE_ZIELE.items()
        ] + [{"name": r, "titel": r} for r in rollen],
        "spalten": [{"name": n, "titel": t, "hilfe": h} for n, _, t, h in RECHTE],
    }


def _ruftasten_blatt(
    kanal_id: int,
    kanaele: dict[int, MumbleChannel],
    acls: dict[int, ChannelACL],
    rollen: list[str],
    store: Any,
) -> list[dict[str, Any]]:
    """Die vier Tasten dieses Platzes: belegt, geerbt oder frei -- und ob es ankommt."""
    belegung = store.ruftasten() if store is not None else {}
    eigener_pfad = pfade_der_plaetze(kanaele).get(kanal_id, "")
    wirk = wirksame_belegung(kanal_id, kanaele, belegung)
    namen = {k["id"]: k["pfad"] for k in _baum(kanaele)}

    zeilen = []
    for taste in RUFTASTEN:
        b = wirk.get(taste)
        zeile: dict[str, Any] = {
            "taste": taste,
            "gruppe": ruf_gruppe(taste),
            "eigen": b.rolle if b is not None and b.von == eigener_pfad else None,
            "geerbt": (
                {"rolle": b.rolle, "von": _kurzname(b.von)}
                if b is not None and b.von != eigener_pfad
                else None
            ),
            "wirksam": b.rolle if b is not None else None,
            "rufende": [],
            "kommt_nicht_an": [],
        }
        if b is not None:
            von_id = next(
                (k for k, p in pfade_der_plaetze(kanaele).items() if p == b.von), kanal_id
            )
            ab = abdeckung(von_id, taste, b.rolle, kanaele, acls, rollen, belegung)
            zeile["rufende"] = ab.rufende
            zeile["kommt_nicht_an"] = sorted(
                {f"{namen.get(platz, platz)} (fuer {rufer})" for rufer, platz in ab.fehlt}
            )
        zeilen.append(zeile)
    return zeilen


def _erlauben(
    acl: ChannelACL, rollen: list[str], bit: int
) -> tuple[list[str], list[str]]:
    """Erlaubt ``bit`` fuer jede Rolle im *eigenen* Eintrag dieses Platzes.

    Gibt zurueck, fuer wen es gesetzt wurde und wer uebersprungen wurde -- eine
    Zelle mit mehreren eigenen Eintraegen derselben Rolle wird nicht angefasst,
    aus demselben Grund wie im Raster (siehe :func:`_bearbeitbar`).
    Geschrieben wird hier nicht; das macht der Aufrufer, einmal je Platz.
    """
    gesetzt: list[str] = []
    uebersprungen: list[str] = []
    for rolle in rollen:
        frei, grund = _bearbeitbar(acl, rolle)
        if not frei:
            uebersprungen.append(f"{rolle}: {grund}")
            continue
        eintraege = _eigene_eintraege(acl, rolle)
        if eintraege:
            eintrag = eintraege[0]
        else:
            eintrag = ACLEntry(
                apply_here=True, apply_subs=True, allow=0, deny=0, group=rolle
            )
            acl.acls.append(eintrag)
        eintrag.deny &= ~bit
        eintrag.allow |= bit
        gesetzt.append(rolle)
    return gesetzt, uebersprungen


def _kurzname(pfad: str) -> str:
    return pfad.rsplit("/", 1)[-1] if pfad else UEBERALL


class VerbindungBody(BaseModel):
    art: str
    von: int
    nach: int
    an: bool = True


@router.put("/verbindung")
async def verbindung_setzen(
    request: Request, body: VerbindungBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Verbindet zwei Plaetze -- oder loest die Verbindung wieder.

    Was dabei wirklich passiert, ist zweierlei, und die Oberflaeche bekommt es
    zurueckgemeldet, statt es zu verschweigen:

    * Am **Zielplatz** wird das noetige Recht fuer die Rollen gesetzt, die am
      Ausgangsplatz sprechen duerfen -- ``Mithoeren`` bzw. ``Reinschalten``.
    * Beim Mithoeren wird die Verbindung zusaetzlich hinterlegt, weil
      ``startListening`` an der Sitzung haengt und kein Trennen ueberlebt. Der
      Enforcer zieht sie nach.

    Wer eine Verbindung aufloest, verliert das Recht am Ziel nicht automatisch:
    es koennte von Hand oder fuer etwas anderes gesetzt worden sein, und ein
    Automatismus, der eine bewusste Entscheidung zurueckdreht, ist schlimmer als
    ein Recht zuviel. Das Ergebnis sagt, was stehen geblieben ist.
    """
    if body.art not in VERBINDUNGSARTEN:
        raise HTTPException(400, f"Unbekannte Verbindungsart {body.art!r}.")
    if body.von == body.nach:
        raise HTTPException(400, "Ein Platz kann sich nicht mit sich selbst verbinden.")

    context = ctx(request)
    try:
        kanaele, acls = await _alles_lesen(context)
    except IceError as exc:
        raise _fail(exc) from exc
    for kid in (body.von, body.nach):
        if kid not in kanaele:
            raise HTTPException(404, f"Platz {kid} gibt es nicht.")

    pfade = {k["id"]: k["pfad"] for k in _baum(kanaele)}
    von_pfad, nach_pfad = pfade.get(body.von, ""), pfade.get(body.nach, "")
    rollen = _rollen_die_sprechen(
        body.von, kanaele, acls, _eigene_rollen(acls)
    )
    recht = "hoeren" if body.art == "hoert" else "reinschalten"
    bit = _bit(recht)

    gesetzt: list[str] = []
    uebersprungen: list[str] = []
    if body.an:
        if not rollen:
            raise HTTPException(
                409,
                f"An {_kurzname(von_pfad)!r} darf zurzeit keine eigene Rolle "
                "sprechen. Ohne das gibt es niemanden, dem die Verbindung "
                "etwas nützen würde -- erst dort das Sprechen erlauben.",
            )
        ziel_acl = acls[body.nach]
        gesetzt, uebersprungen = _erlauben(ziel_acl, rollen, bit)
        if gesetzt:
            try:
                await context.ice.set_channel_acl(ziel_acl)
            except IceError as exc:
                raise _fail(exc) from exc

    store = _store(context)
    try:
        store.set_verbindung(
            body.art, von_pfad, nach_pfad, an=body.an, author=account.name
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        kanaele = await context.ice.get_channels()
        context.enforcer.lade_verbindungen(store.verbindungen(), kanaele)
    except IceError:
        pass

    context.audit(
        account.name,
        f"pult.verbindung.{body.art}",
        f"{von_pfad} -> {nach_pfad}",
        after="verbunden" if body.an else "getrennt",
    )
    return {
        "ok": True,
        "an": body.an,
        "rollen": gesetzt,
        "uebersprungen": uebersprungen,
        "hinweis": (
            ""
            if body.an
            else "Die Verbindung ist weg. Das Recht am Zielplatz bleibt stehen – "
            "es koennte fuer etwas anderes gesetzt worden sein."
        ),
    }


# --------------------------------------------------------------------------- #
#  Ruftasten
# --------------------------------------------------------------------------- #


class RuftasteBody(BaseModel):
    kanal: int
    taste: int
    #: Rolle, die gerufen werden soll. ``None`` gibt die Taste an diesem Platz
    #: frei -- dann gilt wieder, was weiter oben festgelegt ist.
    rolle: str | None = None


@router.put("/ruftaste")
async def ruftaste_setzen(
    request: Request, body: RuftasteBody, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Belegt eine Ruftaste an einem Platz.

    Drei Dinge passieren, und alle drei kommen in der Antwort zurueck:

    1. Die Belegung wird gespeichert.
    2. Damit der Ruf ankommt, bekommen die Rufenden das Fluesterrecht an den
       Plaetzen, an denen die gerufene Rolle sprechen darf -- murmur prueft es
       je Zielplatz. Wo das Raster eine Zelle nicht verlustfrei schreiben kann,
       wird sie uebersprungen und genannt.
    3. Alle Verbundenen werden sofort umgeleitet, nicht erst beim naechsten
       Verbinden.
    """
    if body.taste not in RUFTASTEN:
        raise HTTPException(400, f"Es gibt Taste 1 bis {len(RUFTASTEN)}, keine {body.taste}.")
    context = ctx(request)
    try:
        kanaele, acls = await _alles_lesen(context)
    except IceError as exc:
        raise _fail(exc) from exc
    if body.kanal not in kanaele:
        raise HTTPException(404, "Diesen Platz gibt es nicht.")

    rollen = _eigene_rollen(acls)
    if body.rolle and body.rolle not in rollen and body.rolle not in EINGEBAUTE_ZIELE:
        raise HTTPException(404, f"Die Rolle {body.rolle!r} gibt es nicht.")

    store = _store(context)
    pfad = pfade_der_plaetze(kanaele).get(body.kanal, "")
    try:
        store.set_ruftaste(pfad, body.taste, body.rolle, account.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    belegung = store.ruftasten()

    erlaubt: dict[str, list[str]] = {}
    uebersprungen: list[str] = []
    wirk = wirksame_belegung(body.kanal, kanaele, belegung).get(body.taste)
    if wirk is not None:
        ab = abdeckung(body.kanal, body.taste, wirk.rolle, kanaele, acls, rollen, belegung)
        je_platz: dict[int, list[str]] = {}
        for rufer, platz in ab.fehlt:
            je_platz.setdefault(platz, []).append(rufer)
        namen = {k["id"]: k["pfad"] for k in _baum(kanaele)}
        for platz, rufende in sorted(je_platz.items()):
            acl = acls[platz]
            gesetzt, nicht = _erlauben(acl, rufende, WHISPER)
            uebersprungen.extend(f"{namen.get(platz, platz)}: {n}" for n in nicht)
            if gesetzt:
                try:
                    await context.ice.set_channel_acl(acl)
                except IceError as exc:
                    raise _fail(exc) from exc
                for rolle in gesetzt:
                    erlaubt.setdefault(rolle, []).append(namen.get(platz, str(platz)))

    try:
        kanaele = await context.ice.get_channels()
    except IceError as exc:
        raise _fail(exc) from exc
    context.enforcer.lade_ruftasten(belegung, kanaele)
    await context.ice.run(context.enforcer.enforce_all, list(context.live.users.values()))

    context.audit(
        account.name,
        "pult.ruftaste",
        f"{pfad or UEBERALL} / Taste {body.taste}",
        after=body.rolle or "frei",
    )
    return {
        "ok": True,
        "taste": body.taste,
        "gruppe": ruf_gruppe(body.taste),
        "rolle": body.rolle,
        "reinschalten_erlaubt": erlaubt,
        "uebersprungen": uebersprungen,
    }
