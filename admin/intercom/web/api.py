"""JSON-Schnittstelle des Cockpits.

Aufteilung der Rechte: lesende Routen haengen an :func:`require_user`,
schreibende an :func:`require_admin`. Damit kann der Nur-Lese-Zugang alles
sehen und nichts anfassen.

Jede schreibende Route schreibt einen Audit-Eintrag mit Vorher/Nachher. Das ist
kein Beiwerk: wenn waehrend eines Wettkampfs jemand im falschen Kanal landet,
muss nachvollziehbar sein, wer ihn verschoben hat.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..ice.errors import IceError
from ..ice.permissions import BY_NAME, PERMISSIONS, mask_to_names, names_to_mask
from ..ice.types import ACLEntry, BanEntry, ChannelACL, ChannelGroup, MumbleChannel
from ..provision.templates import TEMPLATES, apply_template
from ..woerter import UEBERALL
from .auth import Account, require_admin, require_user

router = APIRouter(prefix="/api")

__all__ = ["metrics_text", "router"]


def ctx(request: Request) -> Any:
    return request.app.state.ctx


def _fail(exc: Exception) -> HTTPException:
    """Ice-Fehler -> HTTP mit lesbarem Text."""
    return HTTPException(status_code=502, detail=str(exc))


# --------------------------------------------------------------------------- #
#  Zustand
# --------------------------------------------------------------------------- #


@router.get("/me")
def me(request: Request, account: Account = Depends(require_user)) -> dict[str, Any]:
    return {"account": account.to_json(), "csrf": account.csrf}


@router.get("/state")
async def state(request: Request, account: Account = Depends(require_user)) -> dict[str, Any]:
    """Bewusst ``async``: siehe :func:`metrics` -- LiveState gehoert dem Loop."""
    context = ctx(request)
    snapshot = context.live.snapshot(context.enforcer.deviations)
    snapshot["health"] = context.health()
    return snapshot


@router.get("/events")
async def events(request: Request, account: Account = Depends(require_user)) -> StreamingResponse:
    """Server-Sent Events fuer die Live-Aktualisierung.

    ``X-Accel-Buffering: no`` ist fuer nginx-artige Reverse Proxies noetig -- ohne
    das puffert nginx den Strom und im Browser kommt minutenlang nichts an.
    """
    context = ctx(request)

    async def stream() -> AsyncIterator[str]:
        # Sofort den aktuellen Stand schicken, damit die Seite nicht leer bleibt,
        # bis das erste Ereignis eintrifft.
        first = json.dumps(
            context.live.snapshot(context.enforcer.deviations),
            ensure_ascii=False,
            default=str,
        )
        yield f"event: state\ndata: {first}\n\n"
        async for frame in context.live.hub.subscribe():
            if await request.is_disconnected():
                break
            yield frame

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/permissions")
def permissions(account: Account = Depends(require_user)) -> list[dict[str, Any]]:
    """Die Rechte-Tabelle fuer die ACL-Matrix im GUI."""
    return [
        {
            "name": p.name,
            "bit": p.bit,
            "label": p.label,
            "description": p.description,
            "root_only": p.root_only,
            "in_slice": p.in_slice,
        }
        for p in PERMISSIONS
    ]


# --------------------------------------------------------------------------- #
#  Clients
# --------------------------------------------------------------------------- #


@router.get("/users/{session}")
async def user_detail(
    session: int, request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    context = ctx(request)
    try:
        user = await context.ice.get_state(session)
        certificates = await context.ice.get_certificate_list(session)
        listening = await context.ice.get_listening_channels(session)
    except IceError as exc:
        raise _fail(exc) from exc

    row = next(
        (r for r in context.live.user_rows(context.enforcer.deviations) if r["session"] == session),
        user.to_json(),
    )
    expected_listeners = context.enforcer.expected_listeners(user)
    registration: dict[str, Any] | None = None
    if user.registered:
        try:
            registration = (await context.ice.get_registration(user.userid)).to_json()
        except IceError:
            registration = None

    history: dict[str, list[float | None]] = {}
    if context.store is not None:
        try:
            history = {
                "ping": context.store.sparkline(user.name, "ping_ms"),
                "loss": context.store.sparkline(user.name, "loss_pct"),
            }
        except Exception:  # noqa: BLE001 - Verlauf ist Beiwerk, nie ein Fehler
            history = {}

    note = None
    if context.store is not None:
        try:
            stored = context.store.get_note("user", user.name)
            note = stored.text if stored else None
        except Exception:  # noqa: BLE001
            note = None

    device = (context.config.devices.get(user.name) if context.config else None) or {}

    return {
        "user": row,
        "certificates": [_certificate_info(der) for der in certificates],
        "listening": [
            {"id": cid, "name": context.live.channel_name(cid)} for cid in listening
        ],
        "expected_listeners": [
            {"id": cid, "name": context.live.channel_name(cid)} for cid in expected_listeners
        ],
        "listeners_ok": set(listening) >= set(expected_listeners),
        "expects_priority": context.enforcer.expects_priority(user),
        "registration": registration,
        "history": history,
        "note": note,
        "device": device,
    }


def _certificate_info(der: bytes) -> dict[str, Any]:
    """Kurzinfo zu einem Zertifikat der Kette.

    Ohne zusaetzliche Abhaengigkeit: der SHA-1-Fingerabdruck genuegt, denn genau
    den benutzt murmur intern als ``UserHash`` beim Registrieren. Fuer Aussteller
    und Laufzeit waere ein X.509-Parser noetig -- wenn ``cryptography`` ohnehin
    installiert ist (der Monitor-Bot braucht es), nutzen wir es zusaetzlich.
    """
    import hashlib

    info: dict[str, Any] = {
        "sha1": hashlib.sha1(der).hexdigest(),
        "sha256": hashlib.sha256(der).hexdigest(),
        "bytes": len(der),
    }
    try:
        from cryptography import x509

        certificate = x509.load_der_x509_certificate(der)
        info["subject"] = certificate.subject.rfc4514_string()
        info["issuer"] = certificate.issuer.rfc4514_string()
        # not_valid_*_utc gibt es erst ab cryptography 42; die aelteren
        # Eigenschaften liefern dasselbe, nur ohne Zeitzone. Beide Wege
        # bedienen, damit eine aeltere Umgebung nicht die ganze Kette verliert.
        nicht_vor = getattr(certificate, "not_valid_before_utc", None) or (
            certificate.not_valid_before
        )
        nicht_nach = getattr(certificate, "not_valid_after_utc", None) or (
            certificate.not_valid_after
        )
        info["not_before"] = nicht_vor.isoformat()
        info["not_after"] = nicht_nach.isoformat()
        info["serial"] = f"{certificate.serial_number:x}"
    except Exception:  # noqa: BLE001 - ohne cryptography bleibt es beim Hash
        pass
    return info


class UserAction(BaseModel):
    action: str = Field(
        description="move | mute | deaf | suppress | priority | kick | message | comment"
    )
    channel: int | None = None
    value: bool | None = None
    text: str = ""


@router.post("/users/{session}/action")
async def user_action(
    session: int,
    request: Request,
    body: UserAction,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        before = await context.ice.get_state(session)
    except IceError as exc:
        raise _fail(exc) from exc

    try:
        if body.action == "move":
            if body.channel is None:
                raise HTTPException(400, "channel fehlt.")
            await context.ice.set_user_state(session, channel=body.channel)
        elif body.action == "mute":
            await context.ice.set_user_state(session, mute=bool(body.value))
        elif body.action == "deaf":
            await context.ice.set_user_state(session, deaf=bool(body.value))
        elif body.action == "suppress":
            await context.ice.set_user_state(session, suppress=bool(body.value))
        elif body.action == "priority":
            await context.ice.set_user_state(session, priority_speaker=bool(body.value))
        elif body.action == "comment":
            await context.ice.set_user_state(session, comment=body.text)
        elif body.action == "kick":
            await context.ice.kick_user(session, body.text or "Vom Admin getrennt")
        elif body.action == "message":
            if not body.text:
                raise HTTPException(400, "text fehlt.")
            await context.ice.send_message(session, body.text)
        else:
            raise HTTPException(400, f"Unbekannte Aktion {body.action!r}.")
    except IceError as exc:
        context.audit(
            account.name, f"user.{body.action}", before.name, ok=False, error=str(exc)
        )
        raise _fail(exc) from exc

    after: dict[str, Any] = {}
    if body.action not in {"kick", "message"}:
        try:
            after = (await context.ice.get_state(session)).to_json()
        except IceError:
            after = {}
    context.audit(
        account.name,
        f"user.{body.action}",
        before.name,
        before=json.dumps(before.to_json(), ensure_ascii=False, default=str),
        after=json.dumps(after, ensure_ascii=False, default=str) if after else body.text,
    )
    return {"ok": True}


class RegisterRequest(BaseModel):
    """Registrierung eines verbundenen Clients mit dessen aktuellem Zertifikat."""

    name: str = ""
    password: str = ""


@router.post("/users/{session}/register")
async def register_connected(
    session: int,
    request: Request,
    body: RegisterRequest,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    """Registriert einen verbundenen Client.

    Nimmt den Hash des Zertifikats, mit dem der Client gerade verbunden ist --
    genau den benutzt murmur intern zum Wiedererkennen. Hat der Client kein
    Zertifikat, muss ein Passwort mitgegeben werden.
    """
    import hashlib

    context = ctx(request)
    try:
        user = await context.ice.get_state(session)
        certificates = await context.ice.get_certificate_list(session)
    except IceError as exc:
        raise _fail(exc) from exc

    if user.registered:
        raise HTTPException(400, f"{user.name} ist bereits registriert.")

    name = body.name.strip() or user.name
    cert_hash = hashlib.sha1(certificates[0]).hexdigest() if certificates else None
    if not cert_hash and not body.password:
        raise HTTPException(
            400,
            "Der Client hat kein Zertifikat vorgelegt. Ohne Zertifikat wird ein "
            "Passwort gebraucht.",
        )

    try:
        userid = await context.ice.register_user(
            name, cert_hash=cert_hash, password=body.password or None
        )
    except IceError as exc:
        context.audit(account.name, "user.register", name, ok=False, error=str(exc))
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "user.register",
        name,
        after=json.dumps({"userid": userid, "cert_hash": cert_hash}, ensure_ascii=False),
    )
    return {"ok": True, "userid": userid, "cert_hash": cert_hash}


# --------------------------------------------------------------------------- #
#  Kanaele
# --------------------------------------------------------------------------- #


class ChannelCreate(BaseModel):
    name: str
    parent: int = 0
    description: str = ""
    position: int = 0


@router.post("/channels")
async def channel_create(
    request: Request, body: ChannelCreate, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    try:
        channel_id = await context.ice.add_channel(body.name, body.parent)
        if body.description or body.position:
            channel = await context.ice.get_channel_state(channel_id)
            channel.description = body.description
            channel.position = body.position
            await context.ice.set_channel_state(channel)
    except IceError as exc:
        context.audit(account.name, "channel.create", body.name, ok=False, error=str(exc))
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "channel.create",
        body.name,
        after=json.dumps(body.model_dump(), ensure_ascii=False),
    )
    await _refresh(context)
    return {"ok": True, "id": channel_id}


class ChannelUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    position: int | None = None
    parent: int | None = None
    links: list[int] | None = None


@router.patch("/channels/{channel_id}")
async def channel_update(
    channel_id: int,
    request: Request,
    body: ChannelUpdate,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        current = await context.ice.get_channel_state(channel_id)
    except IceError as exc:
        raise _fail(exc) from exc

    before = current.to_json()
    updated = MumbleChannel(
        id=channel_id,
        name=body.name if body.name is not None else current.name,
        parent=body.parent if body.parent is not None else current.parent,
        description=body.description if body.description is not None else current.description,
        temporary=current.temporary,
        position=body.position if body.position is not None else current.position,
        links=body.links if body.links is not None else list(current.links),
    )
    # Der Pfad vor der Aenderung -- er steht so im hinterlegten Wunschzustand.
    alter_pfad = context.live.path_of.get(channel_id)

    try:
        await context.ice.set_channel_state(updated)
    except IceError as exc:
        context.audit(
            account.name, "channel.update", current.name, ok=False, error=str(exc)
        )
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "channel.update",
        current.name,
        before=json.dumps(before, ensure_ascii=False),
        after=json.dumps(updated.to_json(), ensure_ascii=False),
    )
    await _refresh(context)

    # Umbenennen und Verschieben aendern den Pfad. Der Wunschzustand merkt sich
    # Plaetze als Pfad -- ohne Nachziehen zeigte er ins Leere, und die
    # Oberflaeche behauptete einen festen Platz, den es nicht mehr gibt.
    neuer_pfad = context.live.path_of.get(channel_id)
    if context.store is not None and alter_pfad and neuer_pfad and alter_pfad != neuer_pfad:
        geaendert = context.store.wunsch_umschreiben(alter_pfad, neuer_pfad)
        geaendert += context.store.verbindung_umschreiben(alter_pfad, neuer_pfad)
        geaendert += context.store.ruftaste_umschreiben(alter_pfad, neuer_pfad)
        if geaendert:
            channels = await context.ice.get_channels()
            context.enforcer.lade_wuensche(context.store.alle_wuensche(), channels)
            context.enforcer.lade_verbindungen(context.store.verbindungen(), channels)
            context.enforcer.lade_ruftasten(context.store.ruftasten(), channels)
    return {"ok": True}


@router.delete("/channels/{channel_id}")
async def channel_delete(
    channel_id: int, request: Request, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    if channel_id == 0:
        raise HTTPException(
            400, "Der oberste Platz lässt sich nicht löschen."
        )
    pfad = context.live.path_of.get(channel_id)
    try:
        current = await context.ice.get_channel_state(channel_id)
        await context.ice.remove_channel(channel_id)
    except IceError as exc:
        raise _fail(exc) from exc

    # Verbindungen auf einen Platz, den es nicht mehr gibt, waeren eine Anzeige
    # ohne Gegenstand. Der gemerkte Platz einer Person bleibt dagegen stehen:
    # ein gleichnamiger Platz kann wiederkommen, etwa aus einer Sicherung.
    if context.store is not None and pfad:
        context.store.verbindung_vergessen(pfad)
        context.store.ruftaste_vergessen(pfad)

    context.audit(
        account.name,
        "channel.delete",
        current.name,
        before=json.dumps(current.to_json(), ensure_ascii=False),
    )
    await _refresh(context)
    return {"ok": True}


class ChannelMessage(BaseModel):
    text: str
    tree: bool = False


@router.post("/channels/{channel_id}/message")
async def channel_message(
    channel_id: int,
    request: Request,
    body: ChannelMessage,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    if not body.text.strip():
        raise HTTPException(400, "Der Text ist leer.")
    try:
        await context.ice.send_message_channel(channel_id, body.tree, body.text)
    except IceError as exc:
        raise _fail(exc) from exc
    context.audit(
        account.name,
        "channel.message",
        context.live.channel_name(channel_id),
        after=body.text,
    )
    return {"ok": True}


async def _refresh(context: Any) -> None:
    from ..ice.errors import IceError as _IceError

    try:
        context.live.set_channels(await context.ice.get_channels())
        context.live.set_users(await context.ice.get_users())
    except _IceError:
        return
    context.live.hub.publish("state", context.live.snapshot(context.enforcer.deviations))


# --------------------------------------------------------------------------- #
#  ACL
# --------------------------------------------------------------------------- #


class ACLEntryModel(BaseModel):
    group: str = ""
    userid: int = -1
    apply_here: bool = True
    apply_subs: bool = False
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    def to_entry(self) -> ACLEntry:
        return ACLEntry(
            apply_here=self.apply_here,
            apply_subs=self.apply_subs,
            allow=names_to_mask(self.allow),
            deny=names_to_mask(self.deny),
            group=self.group,
            userid=self.userid,
        )


class GroupModel(BaseModel):
    name: str
    inherit: bool = True
    inheritable: bool = True
    add: list[int] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)

    def to_group(self) -> ChannelGroup:
        return ChannelGroup(
            name=self.name,
            inherit=self.inherit,
            inheritable=self.inheritable,
            add=sorted(set(self.add)),
            remove=sorted(set(self.remove)),
        )


class ACLPayload(BaseModel):
    acls: list[ACLEntryModel] = Field(default_factory=list)
    groups: list[GroupModel] = Field(default_factory=list)
    inherit: bool = True


@router.get("/channels/{channel_id}/acl")
async def acl_read(
    channel_id: int, request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    context = ctx(request)
    try:
        acl = await context.ice.get_acl(channel_id)
        registered = await context.ice.get_registered_users()
    except IceError as exc:
        raise _fail(exc) from exc

    data = acl.to_json()
    data["channel_name"] = context.live.channel_name(channel_id)
    data["is_root"] = channel_id == 0
    data["registered"] = registered
    data["templates"] = [t.to_json() for t in TEMPLATES.values()]
    data["dangling"] = [a.to_json() for a in acl.own_acls() if a.dangling]
    return data


def _diff_lines(current: list[ACLEntry], wanted: list[ACLEntry]) -> list[dict[str, str]]:
    def line(entry: ACLEntry) -> str:
        scope = "+".join(
            part
            for part, flag in (("hier", entry.apply_here), ("Unterkanaele", entry.apply_subs))
            if flag
        )
        who = f"@{entry.group}" if entry.is_group else f"Nutzer {entry.userid}"
        return (
            f"{who} [{scope or 'nirgends'}] "
            f"erlaubt: {', '.join(mask_to_names(entry.allow)) or '-'} | "
            f"verboten: {', '.join(mask_to_names(entry.deny)) or '-'}"
        )

    rows: list[dict[str, str]] = []
    for index in range(max(len(current), len(wanted))):
        old = line(current[index]) if index < len(current) else ""
        new = line(wanted[index]) if index < len(wanted) else ""
        rows.append({"before": old, "after": new, "changed": str(old != new).lower()})
    return rows


@router.post("/channels/{channel_id}/acl/preview")
async def acl_preview(
    channel_id: int,
    request: Request,
    body: ACLPayload,
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    """Diff-Ansicht vor dem Speichern.

    POST nur wegen des Rumpfs -- die Route **schreibt nichts** und steht darum
    auch dem Nur-Lese-Konto offen. Abgesichert durch
    ``test_lesende_post_routen_schreiben_wirklich_nicht``.
    """
    context = ctx(request)
    try:
        current = await context.ice.get_acl(channel_id)
    except IceError as exc:
        raise _fail(exc) from exc
    try:
        wanted = [entry.to_entry() for entry in body.acls]
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    warnings: list[str] = []
    for entry in wanted:
        if entry.dangling:
            warnings.append(
                f"@{entry.group or entry.userid}: weder 'hier' noch 'Unterkanaele' -- "
                "der Eintrag wirkt nirgends."
            )
        if channel_id != 0:
            root_only = [
                BY_NAME[name].label
                for name in mask_to_names(entry.allow | entry.deny)
                if name in BY_NAME and BY_NAME[name].root_only
            ]
            if root_only:
                warnings.append(
                    f"@{entry.group or entry.userid}: {', '.join(root_only)} wertet "
                    "murmur nur ganz oben aus -- an diesem Platz bleibt es wirkungslos."
                )
    return {"diff": _diff_lines(current.own_acls(), wanted), "warnings": warnings}


@router.post("/channels/{channel_id}/acl/template")
def acl_template(
    channel_id: int,
    body: dict[str, Any] = Body(...),
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    """Baut die ACL-Liste einer Vorlage, ohne sie zu schreiben.

    Reine Funktion, kein Serverzugriff. POST nur wegen des Rumpfs.
    """
    key = str(body.get("key", ""))
    groups = [str(g) for g in body.get("groups", [])]
    try:
        entries = apply_template(key, groups)
    except KeyError:
        raise HTTPException(
            400, f"Unbekannte Vorlage {key!r}. Bekannt: {', '.join(TEMPLATES)}"
        ) from None
    return {
        "acls": [
            {
                "group": e.group,
                "userid": e.userid,
                "apply_here": e.apply_here,
                "apply_subs": e.apply_subs,
                "allow": mask_to_names(e.allow),
                "deny": mask_to_names(e.deny),
            }
            for e in entries
        ]
    }


@router.put("/channels/{channel_id}/acl")
async def acl_write(
    channel_id: int,
    request: Request,
    body: ACLPayload,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        current = await context.ice.get_acl(channel_id)
    except IceError as exc:
        raise _fail(exc) from exc

    try:
        wanted = ChannelACL(
            channel_id=channel_id,
            acls=[entry.to_entry() for entry in body.acls],
            groups=[group.to_group() for group in body.groups],
            inherit=body.inherit,
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    for entry in wanted.acls:
        if entry.dangling:
            raise HTTPException(
                400,
                "Ein Eintrag gilt weder hier noch für Unterkanäle. Solche "
                "Leichen legt der Editor nicht an -- bitte einen Geltungsbereich "
                "wählen oder den Eintrag entfernen.",
            )

    try:
        await context.ice.set_channel_acl(wanted)
    except IceError as exc:
        context.audit(
            account.name,
            "acl.write",
            context.live.channel_name(channel_id),
            ok=False,
            error=str(exc),
        )
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "acl.write",
        context.live.channel_name(channel_id),
        before=json.dumps(current.to_json(), ensure_ascii=False),
        after=json.dumps(wanted.to_json(), ensure_ascii=False),
    )
    return {"ok": True}


@router.get("/channels/{channel_id}/effective")
async def effective_permissions(
    channel_id: int,
    request: Request,
    session: int = Query(..., description="Sitzung des Clients"),
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    """Was darf dieser Client in diesem Kanal wirklich?

    Fragt murmur selbst (``effectivePermissions``) statt die ACLs nachzurechnen --
    die Auswertung in ACL.cpp kennt Token und Kontextgruppen, die wir hier nicht
    nachbilden wuerden.
    """
    context = ctx(request)
    try:
        mask = await context.ice.effective_permissions(session, channel_id)
    except IceError as exc:
        raise _fail(exc) from exc
    granted = set(mask_to_names(mask))
    return {
        "mask": mask,
        "channel": context.live.channel_name(channel_id),
        "permissions": [
            {"name": p.name, "label": p.label, "granted": p.name in granted}
            for p in PERMISSIONS
        ],
    }


# --------------------------------------------------------------------------- #
#  Registrierte Nutzer
# --------------------------------------------------------------------------- #


@router.get("/registered")
async def registered_list(
    request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    context = ctx(request)
    try:
        registered = await context.ice.get_registered_users()
        root = await context.ice.get_acl(0)
    except IceError as exc:
        raise _fail(exc) from exc

    membership: dict[int, list[str]] = {}
    for group in root.own_groups():
        for userid in group.add:
            membership.setdefault(userid, []).append(group.name)

    online = {user.userid: user.session for user in context.live.users.values() if user.registered}
    entries = []
    for userid, name in sorted(registered.items(), key=lambda item: item[1].lower()):
        try:
            detail = (await context.ice.get_registration(userid)).to_json()
        except IceError:
            detail = {"userid": userid, "name": name}
        detail["groups"] = sorted(membership.get(userid, []))
        detail["online_session"] = online.get(userid)
        entries.append(detail)

    return {
        "users": entries,
        "groups": sorted(g.name for g in root.own_groups()),
    }


class RegisteredCreate(BaseModel):
    name: str
    password: str = ""
    cert_hash: str = ""
    email: str = ""
    comment: str = ""


@router.post("/registered")
async def registered_create(
    request: Request, body: RegisteredCreate, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    if not body.password and not body.cert_hash:
        raise HTTPException(
            400,
            "Ohne Passwort und ohne Zertifikatshash kann sich der Nutzer nicht "
            "anmelden. Eines von beidem wird gebraucht.",
        )
    try:
        userid = await context.ice.register_user(
            body.name,
            password=body.password or None,
            cert_hash=body.cert_hash or None,
            email=body.email or None,
            comment=body.comment or None,
        )
    except IceError as exc:
        raise _fail(exc) from exc
    context.audit(account.name, "registered.create", body.name, after=str(userid))
    return {"ok": True, "userid": userid}


class RegisteredUpdate(BaseModel):
    name: str | None = None
    password: str | None = None
    cert_hash: str | None = None
    email: str | None = None
    comment: str | None = None


@router.patch("/registered/{userid}")
async def registered_update(
    userid: int,
    request: Request,
    body: RegisteredUpdate,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        before = (await context.ice.get_registration(userid)).to_json()
        await context.ice.update_registration(
            userid,
            name=body.name,
            password=body.password,
            cert_hash=body.cert_hash,
            email=body.email,
            comment=body.comment,
        )
    except IceError as exc:
        raise _fail(exc) from exc

    # Das Passwort taucht bewusst nicht im Audit-Log auf.
    changed = body.model_dump(exclude_none=True)
    if "password" in changed:
        changed["password"] = "(gesetzt)"
    context.audit(
        account.name,
        "registered.update",
        str(before.get("name", userid)),
        before=json.dumps(before, ensure_ascii=False),
        after=json.dumps(changed, ensure_ascii=False),
    )
    return {"ok": True}


@router.delete("/registered/{userid}")
async def registered_delete(
    userid: int, request: Request, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    try:
        before = (await context.ice.get_registration(userid)).to_json()
        await context.ice.unregister_user(userid)
    except IceError as exc:
        raise _fail(exc) from exc
    # Was wir uns fuer diese Person gemerkt haben, gilt jetzt niemandem mehr.
    # murmur vergibt Nutzer-IDs aufsteigend weiter -- bliebe der Wunsch stehen,
    # erbte ihn irgendwann die naechste Person mit derselben ID.
    if context.store is not None:
        context.store.wunsch_vergessen(userid)

    context.audit(
        account.name,
        "registered.delete",
        str(before.get("name", userid)),
        before=json.dumps(before, ensure_ascii=False),
    )
    return {"ok": True}


class GroupMatrix(BaseModel):
    """Vollstaendige Zuordnung Gruppe -> Nutzer-IDs, wie die Checkbox-Matrix sie liefert."""

    groups: dict[str, list[int]]


@router.put("/registered/groups")
async def registered_groups(
    request: Request, body: GroupMatrix, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Schreibt die Rollenzugehoerigkeit ganz oben (am Wurzelkanal).

    Nur ``setACL`` macht Mitgliedschaft dauerhaft; ``addUserToGroup`` waere
    temporaer und ueberlebte keinen Serverneustart (siehe DECISIONS D-005).
    Gruppen, die nicht in der Matrix stehen, bleiben unangetastet -- sonst
    loeschte ein Speichern im GUI alles, was die YAML nicht kennt.
    """
    context = ctx(request)
    try:
        root = await context.ice.get_acl(0)
    except IceError as exc:
        raise _fail(exc) from exc

    before = json.dumps(
        {g.name: g.add for g in root.own_groups()}, ensure_ascii=False, sort_keys=True
    )
    existing = {g.name: g for g in root.own_groups()}
    for name, members in body.groups.items():
        group = existing.get(name)
        if group is None:
            existing[name] = ChannelGroup(name=name, add=sorted(set(members)))
        else:
            group.add = sorted(set(members))

    root.groups = list(existing.values())
    try:
        await context.ice.set_channel_acl(root)
    except IceError as exc:
        raise _fail(exc) from exc

    context.audit(
        account.name,
        "registered.groups",
        UEBERALL,
        before=before,
        after=json.dumps(body.groups, ensure_ascii=False, sort_keys=True),
    )
    await context.ice.run(context.enforcer.refresh_membership)
    return {"ok": True}


class SuperuserPassword(BaseModel):
    password: str


@router.post("/superuser-password")
async def superuser_password(
    request: Request, body: SuperuserPassword, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    if len(body.password) < 8:
        raise HTTPException(400, "Mindestens 8 Zeichen.")
    try:
        await context.ice.set_superuser_password(body.password)
    except IceError as exc:
        raise _fail(exc) from exc
    # Das Passwort selbst wird nirgends festgehalten.
    context.audit(account.name, "server.superuser_password", "SuperUser", after="(geaendert)")
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Bans
# --------------------------------------------------------------------------- #


class BanModel(BaseModel):
    address: str = ""
    bits: int = 32
    name: str = ""
    hash: str = ""
    reason: str = ""
    start: int = 0
    duration: int = 0

    def to_ban(self) -> BanEntry:
        return BanEntry(
            address=self.address,
            bits=self.bits,
            name=self.name,
            hash=self.hash,
            reason=self.reason,
            start=self.start or int(time.time()),
            duration=self.duration,
        )


@router.get("/bans")
async def bans_read(
    request: Request, account: Account = Depends(require_user)
) -> list[dict[str, Any]]:
    context = ctx(request)
    try:
        return [b.to_json() for b in await context.ice.get_bans()]
    except IceError as exc:
        raise _fail(exc) from exc


@router.put("/bans")
async def bans_write(
    request: Request,
    body: list[BanModel] = Body(...),
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    """Ersetzt die gesamte Bannliste -- ``setBans`` kennt nichts anderes."""
    context = ctx(request)
    try:
        before = [b.to_json() for b in await context.ice.get_bans()]
        await context.ice.set_bans([entry.to_ban() for entry in body])
    except (IceError, ValueError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 502, str(exc)) from exc
    context.audit(
        account.name,
        "bans.write",
        f"{len(body)} Eintraege",
        before=json.dumps(before, ensure_ascii=False),
        after=json.dumps([b.model_dump() for b in body], ensure_ascii=False),
    )
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Serverkonfiguration und Log
# --------------------------------------------------------------------------- #

#: Schluessel, die murmur erst nach einem Neustart uebernimmt.
#: Zusammengetragen aus Server::readParams / Meta::reloadSSLSettings -- alles,
#: was Sockets, Datenbank oder die Ice-Schnittstelle betrifft.
RESTART_REQUIRED: frozenset[str] = frozenset(
    {
        "host",
        "port",
        "database",
        "dbDriver",
        "dbUsername",
        "dbPassword",
        "dbHost",
        "dbPort",
        "dbPrefix",
        "ice",
        "icesecretread",
        "icesecretwrite",
        "grpc",
        "logfile",
        "pidfile",
        "uname",
        "sslCert",
        "sslKey",
        "sslCA",
        "sslDHParams",
        "sslCiphers",
    }
)


def _normalisiere_conf_name(name: str) -> str:
    """Wie der Einstiegspunkt des mumble-server-Images Namen vergleicht.

    Dort: ``uppercase="${1^^}"; echo "${uppercase//_/}"`` -- Grossbuchstaben,
    Unterstriche weg. Noetig, weil murmur dieselbe Einstellung an drei Stellen
    unterschiedlich schreibt: ``Meta::getDefaultConf`` liefert ``registername``,
    die ``bare_config.ini`` des Images nennt sie ``registerName``, und in der
    Compose steht ``MUMBLE_CONFIG_REGISTERNAME``.
    """
    return name.upper().replace("_", "")


@router.get("/conf")
async def conf_read(request: Request, account: Account = Depends(require_user)) -> dict[str, Any]:
    """Wirksame Werte, ihre Herkunft und die Abweichungen zur Compose.

    Die Namen der beiden Ice-Aufrufe fuehren in die Irre, deshalb hier
    ausgeschrieben -- nachgemessen an murmur 1.5.735 und belegt in
    ``MumbleServerIce.cpp`` und ``Meta.cpp``:

    * ``Server::getAllConf`` liest ``SELECT key, value FROM config WHERE
      server_id = ?`` -- **nur** was jemand zur Laufzeit per ``setConf``
      geaendert hat. Auf einem frisch aufgesetzten Server steht dort ausser dem
      selbst erzeugten Zertifikat nichts.
    * ``Meta::getDefaultConf`` liefert ``qmConfig``, und das baut ``MetaParams``
      aus der **ini-Datei** plus den eingebauten Vorgaben. Beim Docker-Image ist
      das genau der Stand, den die ``MUMBLE_CONFIG_*``-Variablen der Compose
      geschrieben haben. "Default" heisst hier also *nicht* "murmurs Werkseinstellung".

    Der wirksame Wert ist damit: Datenbank, wenn dort ein Eintrag steht, sonst
    Datei. Wer stattdessen ``getAllConf`` als Ist-Wert anzeigt, behauptet fuer
    jede Einstellung aus der Compose "nicht gesetzt" -- und die Warnung
    "Compose und Live laufen auseinander" kann strukturell nie ausloesen,
    weil der Ist-Wert fuer genau diese Schluessel immer leer ist.
    """
    context = ctx(request)
    try:
        ueberschrieben = await context.ice.get_all_conf()
        aus_datei = await context.ice.get_default_conf()
    except IceError as exc:
        raise _fail(exc) from exc

    import os

    # MUMBLE_CONFIG_* der Compose, normalisiert nachschlagbar.
    aus_compose = {
        _normalisiere_conf_name(name[len("MUMBLE_CONFIG_") :]): (name, wert)
        for name, wert in os.environ.items()
        if name.startswith("MUMBLE_CONFIG_")
    }

    rows = []
    for key in sorted(set(ueberschrieben) | set(aus_datei)):
        live = key in ueberschrieben
        value = ueberschrieben[key] if live else aus_datei.get(key, "")
        env_key, env_value = aus_compose.get(_normalisiere_conf_name(key), ("", None))
        rows.append(
            {
                "key": key,
                "value": value,
                "source": "datenbank" if live else "datei",
                # Der Wert, auf den der Server zurueckfaellt, wenn man den
                # Datenbankeintrag loescht.
                "default": aus_datei.get(key, ""),
                # Zur Laufzeit ueberschrieben: folgt der Compose nicht mehr.
                "overridden": live and value != aus_datei.get(key, ""),
                "restart_required": key in RESTART_REQUIRED,
                "env_key": env_key or f"MUMBLE_CONFIG_{key.upper()}",
                "env_value": env_value,
                # Die Compose sagt etwas anderes als der Server tatsaechlich
                # benutzt -- fast immer eine spaetere Aenderung von Hand, die
                # den naechsten `up -d` ueberlebt und niemandem auffaellt.
                "env_mismatch": env_value is not None and env_value.strip('"') != value,
            }
        )
    return {"rows": rows, "restart_required": sorted(RESTART_REQUIRED)}


class ConfSet(BaseModel):
    key: str
    value: str


@router.put("/conf")
async def conf_write(
    request: Request, body: ConfSet, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    if body.key.lower().startswith("icesecret"):
        raise HTTPException(
            400,
            "Das Ice-Secret lässt sich hier nicht ändern -- danach wäre die "
            "Verbindung des Admin-Prozesses sofort tot. In der .env ändern und "
            "beide Container neu starten.",
        )
    try:
        before = await context.ice.get_conf(body.key)
    except IceError:
        before = ""
    try:
        await context.ice.set_conf(body.key, body.value)
    except IceError as exc:
        raise _fail(exc) from exc
    context.audit(
        account.name, "server.conf", body.key, before=before, after=body.value
    )
    return {"ok": True, "restart_required": body.key in RESTART_REQUIRED}


class NetzEintrag(BaseModel):
    name: str
    cidr: str
    notiz: str = ""


class NetzListe(BaseModel):
    netze: list[NetzEintrag]


@router.get("/netze")
async def netze_lesen(
    request: Request, account: Account = Depends(require_user)
) -> dict[str, Any]:
    """Die Netzsegmente und was sie gerade auffangen."""
    context = ctx(request)
    segmente = context.store.netze() if context.store is not None else []
    # Wie viele Clients haengen gerade in welchem Segment? Das ist die Probe,
    # ob eine Maske ueberhaupt etwas trifft -- eine Zeile, die nie greift, ist
    # schlimmer als keine.
    zaehler: dict[str, int] = {}
    for user in context.live.users.values():
        name = context.live.networks.segment_for(user.address)
        zaehler[name] = zaehler.get(name, 0) + 1
    return {
        "netze": [{**n, "clients": zaehler.get(n["name"], 0)} for n in segmente],
        "sonstige": zaehler.get("sonstige", 0),
        "unbekannt": zaehler.get("unbekannt", 0),
    }


@router.put("/netze")
async def netze_schreiben(
    request: Request, body: NetzListe, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Ersetzt die Segmentliste.

    Im Ganzen statt zeilenweise, weil die Reihenfolge Teil der Aussage ist: ein
    Client landet im **ersten** passenden Segment.
    """
    context = ctx(request)
    if context.store is None:
        raise HTTPException(503, "Der Verlaufsspeicher ist nicht offen.")
    namen = [e.name.strip() for e in body.netze if e.name.strip()]
    if len(namen) != len(set(namen)):
        raise HTTPException(400, "Zwei Segmente mit demselben Namen.")
    try:
        context.store.set_netze([e.model_dump() for e in body.netze])
    except ValueError as exc:
        raise HTTPException(400, f"Ungültige Netzmaske: {exc}") from exc
    context.netze_laden()
    context.audit(
        account.name,
        "netze",
        f"{len(namen)} Segmente",
        after=", ".join(f"{e.name}={e.cidr}" for e in body.netze),
    )
    return {"ok": True, "netze": context.store.netze()}


@router.get("/log")
async def server_log(
    request: Request,
    first: int = Query(0, ge=0),
    count: int = Query(200, ge=1, le=2000),
    pattern: str = Query("", description="Regulaerer Ausdruck als Filter"),
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    """Serverlog. ``first=0`` ist der neueste Eintrag."""
    import re

    context = ctx(request)
    try:
        total = await context.ice.get_log_len()
        entries = await context.ice.get_log(first, first + count)
    except IceError as exc:
        raise _fail(exc) from exc

    rows = [e.to_json() for e in entries]
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(400, f"Ungültiger regulärer Ausdruck: {exc}") from exc
        rows = [row for row in rows if matcher.search(row["text"])]
    return {"total": total, "first": first, "entries": rows}


@router.get("/log/download", response_class=PlainTextResponse)
async def server_log_download(
    request: Request,
    count: int = Query(5000, ge=1, le=50000),
    account: Account = Depends(require_user),
) -> PlainTextResponse:
    context = ctx(request)
    try:
        entries = await context.ice.get_log(0, count)
    except IceError as exc:
        raise _fail(exc) from exc
    lines = [
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.timestamp))}  {e.text}"
        for e in reversed(entries)
    ]
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": 'attachment; filename="mumble-server.log"'},
    )


# --------------------------------------------------------------------------- #
#  Provisioning
# --------------------------------------------------------------------------- #


@router.post("/provision/plan")
async def provision_plan(
    request: Request,
    prune: bool | None = Query(None),
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        plan = await context.provision(dry_run=True, actor=account.name, prune=prune)
    except (RuntimeError, IceError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return plan.to_json()


@router.post("/provision/apply")
async def provision_apply(
    request: Request,
    prune: bool | None = Query(None),
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    try:
        plan = await context.provision(dry_run=False, actor=account.name, prune=prune)
    except (RuntimeError, IceError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return plan.to_json()


# --------------------------------------------------------------------------- #
#  Vorlagen und Sicherungen
#
#  Beides sind EINMALIGE Aktionen mit einer Konfiguration in der Hand, keine
#  laufende Bindung an eine Datei. Der Server bleibt die Wahrheit.
# --------------------------------------------------------------------------- #


@router.get("/vorlagen")
async def vorlagen_auflisten(account: Account = Depends(require_user)) -> dict[str, Any]:
    """Die eingebauten Baukaesten, mit Klartext, was sie anlegen."""
    from ..provision.vorlagen import vorlagen_liste

    return {"vorlagen": vorlagen_liste()}


@router.post("/vorlagen/{schluessel}/plan")
async def vorlage_plan(
    schluessel: str, request: Request, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Testlauf: was wuerde diese Vorlage aendern?

    Nichts wird geschrieben. Die Oberflaeche zeigt das Ergebnis, bevor
    irgendjemand auf Anwenden drueckt -- niemand soll raten muessen, was
    gleich passiert.
    """
    from ..provision.vorlagen import vorlage_laden

    context = ctx(request)
    try:
        config = vorlage_laden(schluessel)
    except KeyError:
        raise HTTPException(404, f"Vorlage {schluessel!r} gibt es nicht.") from None
    try:
        plan = await context.anwenden(
            config, dry_run=True, actor=account.name, quelle=f"Vorlage {schluessel}"
        )
    except (RuntimeError, IceError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return plan.to_json()


@router.post("/vorlagen/{schluessel}/anwenden")
async def vorlage_anwenden(
    schluessel: str, request: Request, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    """Wendet die Vorlage einmalig an. Danach ist sie fertig.

    Es entsteht keine Bindung: wer danach einen Kanal umbenennt, hat einen
    umbenannten Kanal. Kein Neustart schreibt die Vorlage erneut.
    """
    from ..provision.vorlagen import vorlage_laden

    context = ctx(request)
    try:
        config = vorlage_laden(schluessel)
    except KeyError:
        raise HTTPException(404, f"Vorlage {schluessel!r} gibt es nicht.") from None
    try:
        plan = await context.anwenden(
            config, dry_run=False, actor=account.name, quelle=f"Vorlage {schluessel}"
        )
    except (RuntimeError, IceError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return plan.to_json()


@router.post("/provision/reload")
async def provision_reload(
    request: Request, account: Account = Depends(require_admin)
) -> dict[str, Any]:
    context = ctx(request)
    context.reload_config()
    context.audit(account.name, "provision.reload", str(context.settings.intercom_config))
    return {
        "ok": context.config is not None,
        "error": context.config_error,
        "issues": [
            {"level": i.level, "path": i.path, "message": i.message}
            for i in (context.config.issues if context.config else [])
        ],
    }


@router.get("/provision/last")
def provision_last(request: Request, account: Account = Depends(require_user)) -> dict[str, Any]:
    context = ctx(request)
    return {
        "at": context.last_provision_at or None,
        "plan": context.last_plan.to_json() if context.last_plan else None,
    }


# --------------------------------------------------------------------------- #
#  Verlauf, Audit, Notizen
# --------------------------------------------------------------------------- #


@router.get("/history/{name}")
def history(
    name: str,
    request: Request,
    minutes: int = Query(60, ge=1, le=1440),
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    context = ctx(request)
    if context.store is None:
        return {"ping": [], "loss": [], "available": False}
    try:
        return {
            "available": True,
            "ping": context.store.sparkline(name, "ping_ms", minutes=minutes),
            "loss": context.store.sparkline(name, "loss_pct", minutes=minutes),
            "bandwidth": context.store.sparkline(name, "bandwidth_bps", minutes=minutes),
        }
    except Exception as exc:
        raise HTTPException(500, f"Verlauf nicht lesbar: {exc}") from exc


@router.get("/audit")
def audit(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor: str | None = None,
    action: str | None = None,
    search: str | None = None,
    account: Account = Depends(require_user),
) -> dict[str, Any]:
    context = ctx(request)
    if context.store is None:
        return {"entries": [], "total": 0, "available": False}
    entries = context.store.audit_entries(
        limit=limit, offset=offset, actor=actor, action=action, search=search
    )
    total = context.store.audit_count(actor=actor, action=action, search=search)
    return {
        "available": True,
        "total": total,
        "entries": [
            e.__dict__ if not hasattr(e, "to_json") else e.to_json() for e in entries
        ],
    }


class NoteBody(BaseModel):
    text: str


@router.put("/notes/{kind}/{key}")
def note_write(
    kind: str,
    key: str,
    request: Request,
    body: NoteBody,
    account: Account = Depends(require_admin),
) -> dict[str, Any]:
    context = ctx(request)
    if context.store is None:
        raise HTTPException(503, "Ohne SQLite gibt es keine Notizen.")
    context.store.set_note(kind, key, body.text, account.name)
    context.audit(account.name, "note.write", f"{kind}/{key}", after=body.text)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Prometheus
# --------------------------------------------------------------------------- #


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics_text(context: Any) -> str:
    """Metriken im Prometheus-Textformat.

    Bewusst ohne ``prometheus_client``: es sind ein Dutzend Zeilen, und eine
    weitere Abhaengigkeit im Image, die ein Wheel braucht, waere hier reine Last.
    """
    live = context.live
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    metric("intercom_up", "gauge", "1 wenn die Ice-Verbindung zu murmur steht.")
    lines.append(f"intercom_up {1 if context.connected else 0}")

    metric("intercom_server_uptime_seconds", "gauge", "Laufzeit des virtuellen Servers.")
    # Aus dem Polling, nicht frisch geholt: ein Ice-Aufruf an dieser Stelle
    # haengt am selben Threadpool wie alles andere. Genau das machte /metrics
    # bei ausgelastetem Pool zu einem Endpunkt mit halber Minute Antwortzeit --
    # ausgerechnet der, der die Ueberlastung melden soll. health() liest die
    # Laufzeit aus demselben Grund schon immer nur aus.
    lines.append(f"intercom_server_uptime_seconds {context._server_uptime}")

    metric("intercom_admin_uptime_seconds", "gauge", "Laufzeit des Admin-Prozesses.")
    lines.append(f"intercom_admin_uptime_seconds {int(time.time() - context.started_at)}")

    metric("intercom_clients", "gauge", "Anzahl verbundener Clients.")
    lines.append(f"intercom_clients {len(live.users)}")

    metric("intercom_channels", "gauge", "Anzahl Kanaele.")
    lines.append(f"intercom_channels {len(live.channels)}")

    metric("intercom_monitor_up", "gauge", "1 wenn der Monitor-Bot verbunden ist.")
    lines.append(
        f"intercom_monitor_up {1 if (context.monitor and context.monitor.connected) else 0}"
    )

    metric("intercom_client_ping_ms", "gauge", "Ping je Client in Millisekunden.")
    metric("intercom_client_loss_percent", "gauge", "Paketverlust je Client in Prozent.")
    metric("intercom_client_bandwidth_bps", "gauge", "Bandbreite je Client in Bit/s.")
    metric("intercom_client_online_seconds", "gauge", "Verbindungsdauer je Client.")
    metric("intercom_client_tcp_only", "gauge", "1 wenn der Client nur ueber TCP laeuft.")

    for session, user in sorted(live.users.items(), key=lambda item: item[1].name):
        labels = (
            f'client="{_escape(user.name)}",'
            f'channel="{_escape(live.channel_name(user.channel))}",'
            f'segment="{_escape(live.networks.segment_for(user.address))}"'
        )
        lines.append(f"intercom_client_ping_ms{{{labels}}} {user.ping}")
        # loss_pct(), nicht live.loss: ein Wert von vor zehn Minuten waere in
        # Prometheus eine gerade Linie, die aussieht wie eine Messung.
        loss = live.loss_pct(session)
        if loss is not None:
            lines.append(f"intercom_client_loss_percent{{{labels}}} {loss}")
        lines.append(f"intercom_client_bandwidth_bps{{{labels}}} {user.bytes_per_sec * 8}")
        lines.append(f"intercom_client_online_seconds{{{labels}}} {user.online_secs}")
        lines.append(f"intercom_client_tcp_only{{{labels}}} {1 if user.tcp_only else 0}")

    metric("intercom_channel_users", "gauge", "Nutzer je Kanal.")
    counts: dict[int, int] = {}
    for user in live.users.values():
        counts[user.channel] = counts.get(user.channel, 0) + 1
    for channel_id, _channel in sorted(live.channels.items()):
        labels = f'channel="{_escape(live.channel_name(channel_id))}"'
        lines.append(f"intercom_channel_users{{{labels}}} {counts.get(channel_id, 0)}")

    metric("intercom_alarms", "gauge", "Offene Alarme nach Stufe.")
    for level in ("kritisch", "warnung"):
        count = sum(1 for a in live.current_alarms if a.level == level)
        lines.append(f'intercom_alarms{{level="{level}"}} {count}')

    return "\n".join(lines) + "\n"
