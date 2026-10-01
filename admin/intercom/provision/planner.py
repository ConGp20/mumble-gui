"""Abgleich Ist-Zustand <-> Wunschzustand.

Plan und Anwendung laufen durch **denselben** Code. ``Reconciler.run()`` kennt
nur einen Schalter: ``dry_run``. Im Trockenlauf werden Aenderungen
aufgeschrieben statt ausgefuehrt. Damit kann ein Plan nicht von dem abweichen,
was ein ``apply`` anschliessend tut -- der klassische Fehler bei zwei getrennten
Implementierungen.

Idempotenz
----------
Geschrieben wird nur, was sich unterscheidet. Ein zweites ``apply`` direkt nach
dem ersten erzeugt keine einzige Aenderung. Der Test
``test_zweites_apply_ist_leer`` haelt das fest.

Geltungsbereich
---------------
Verwaltet wird nur der Teilbaum unter den in ``channels:`` genannten
Kanaelen der obersten Ebene, dazu Gruppen und Richtlinien am Wurzelkanal.
Ein Kanal, den jemand von Hand neben ``Intercom`` angelegt hat, wird nie
angefasst -- auch nicht mit ``PROVISION_PRUNE``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from ..ice.permissions import describe_mask
from ..ice.types import ACLEntry, ChannelACL, ChannelGroup, MumbleChannel
from ..woerter import UEBERALL
from .acl_map import DesiredChannel, DesiredState, build_desired_state
from .schema import META_GROUPS, IntercomConfig, Issue

if TYPE_CHECKING:
    from ..ice.client import IceClient

log = logging.getLogger(__name__)

__all__ = ["Change", "Plan", "Reconciler", "reconcile"]

ChangeKind = Literal[
    "channel_create",
    "channel_update",
    "channel_delete",
    "channel_link",
    "acl_update",
    "group_update",
    "conf_set",
    "note",
]


@dataclass(slots=True)
class Change:
    """Eine einzelne Abweichung zwischen Ist und Soll."""

    kind: ChangeKind
    target: str
    summary: str
    #: Zeilenweise Gegenueberstellung fuer die Diff-Ansicht.
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    #: True, wenn die Aenderung etwas entfernt.
    destructive: bool = False
    #: True, wenn sie nur mit ``PROVISION_PRUNE=true`` ausgefuehrt wird.
    needs_prune: bool = False
    #: True, wenn sie tatsaechlich ausgefuehrt wurde (nur bei apply).
    applied: bool = False
    #: Fehlermeldung, falls die Ausfuehrung scheiterte.
    error: str = ""

    @property
    def marker(self) -> str:
        if self.needs_prune:
            return "-!"
        if self.kind == "channel_create":
            return "+"
        if self.kind == "channel_delete":
            return "-"
        return "~"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "summary": self.summary,
            "before": self.before,
            "after": self.after,
            "destructive": self.destructive,
            "needs_prune": self.needs_prune,
            "applied": self.applied,
            "error": self.error,
            "marker": self.marker,
        }


@dataclass(slots=True)
class Plan:
    """Ergebnis eines Abgleichs."""

    changes: list[Change] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    dry_run: bool = True
    prune: bool = False
    #: Nutzernamen aus ``users:``, die nicht registriert sind.
    unknown_users: list[str] = field(default_factory=list)
    #: Kanalpfad -> Gruppen, die dort Priority Speaker sein sollen.
    priority_map: dict[str, list[str]] = field(default_factory=dict)
    #: Kanalpfad -> Kanalpfade, die dessen Sprecher mithoeren sollen.
    listen_map: dict[str, list[str]] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def pending(self) -> list[Change]:
        """Aenderungen, die tatsaechlich ausgefuehrt wuerden."""
        return [c for c in self.changes if not c.needs_prune or self.prune]

    @property
    def empty(self) -> bool:
        return not self.pending

    @property
    def failed(self) -> list[Change]:
        return [c for c in self.changes if c.error]

    def summary(self) -> str:
        """Eine Zeile fuer Oberflaeche und CLI.

        Nach dem Anwenden in der Vergangenheit -- "13 anzulegen" ueber einem
        Ergebnis, das schon angelegt ist, las sich wie ein Plan, der noch
        aussteht. Was nur mit Aufraeumen passiert, heisst auch so: so steht der
        Haken in der Oberflaeche, und ``PROVISION_PRUNE`` ist nur der Name
        derselben Sache in der Umgebung.
        """
        skipped = sum(1 for c in self.changes if c.needs_prune and not self.prune)
        if self.empty:
            if skipped:
                # "steht schon so" waere hier falsch: es gibt etwas, das nur
                # die Datei nicht hat -- und ohne Aufraeumen bleibt es.
                return (
                    f"Nichts zu ergänzen oder zu ändern – {skipped} nur mit "
                    "Aufräumen (PROVISION_PRUNE)"
                )
            return "Keine Änderungen – der Server steht schon so."
        # Nach dem Anwenden zaehlt, was wirklich passiert ist -- ein
        # gescheiterter Schritt ist nicht "angelegt".
        zaehlen = self.pending if self.dry_run else [c for c in self.pending if c.applied]
        created = sum(1 for c in zaehlen if c.kind == "channel_create")
        deleted = sum(1 for c in zaehlen if c.kind == "channel_delete")
        changed = len(zaehlen) - created - deleted
        if self.dry_run:
            parts = [f"{created} anzulegen", f"{changed} zu ändern", f"{deleted} zu löschen"]
        else:
            parts = [f"{created} angelegt", f"{changed} geändert", f"{deleted} gelöscht"]
            if self.failed:
                parts.append(f"{len(self.failed)} fehlgeschlagen")
        if skipped:
            parts.append(f"{skipped} nur mit Aufräumen (PROVISION_PRUNE)")
        return ", ".join(parts)

    def to_text(self, colour: bool = False) -> str:
        """Terraform-artige Ausgabe fuer die CLI."""

        def paint(text: str, code: str) -> str:
            return f"\033[{code}m{text}\033[0m" if colour else text

        lines: list[str] = []
        for change in self.changes:
            if change.needs_prune and not self.prune:
                marker = paint("-!", "33")
                suffix = "   (nur mit PROVISION_PRUNE=true)"
            elif change.marker == "+":
                marker = paint("+", "32")
                suffix = ""
            elif change.marker == "-":
                marker = paint("-", "31")
                suffix = ""
            else:
                marker = paint("~", "36")
                suffix = ""
            status = ""
            if change.error:
                status = paint(f"  FEHLER: {change.error}", "31")
            elif change.applied:
                status = paint("  [angewendet]", "32")
            lines.append(f"  {marker} {change.target}: {change.summary}{suffix}{status}")
            for line in change.before:
                lines.append(paint(f"        - {line}", "31"))
            for line in change.after:
                lines.append(paint(f"        + {line}", "32"))
        if self.unknown_users:
            lines.append("")
            lines.append(
                paint(
                    "  Nicht registrierte Nutzer aus users: "
                    + ", ".join(self.unknown_users),
                    "33",
                )
            )
            lines.append(
                "    Ohne Registrierung gibt es keine Nutzer-ID, und ohne "
                "Nutzer-ID keine Gruppenmitgliedschaft."
            )
        for issue in self.issues:
            lines.append(f"  {issue}")
        lines.append("")
        lines.append(f"  {self.summary()}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "prune": self.prune,
            "empty": self.empty,
            "summary": self.summary(),
            "changes": [c.to_json() for c in self.changes],
            "issues": [
                {"level": i.level, "path": i.path, "message": i.message}
                for i in self.issues
            ],
            "unknown_users": self.unknown_users,
            "duration_ms": self.duration_ms,
        }


def _acl_line(entry: ACLEntry) -> str:
    scope = []
    if entry.apply_here:
        scope.append("hier")
    if entry.apply_subs:
        scope.append("darunter")
    who = f"@{entry.group}" if entry.is_group else f"Nutzer {entry.userid}"
    return (
        f"{who} [{'+'.join(scope) or 'nirgends'}] "
        f"erlaubt: {describe_mask(entry.allow)} | "
        f"verboten: {describe_mask(entry.deny)}"
    )


def _group_line(group: ChannelGroup) -> str:
    return (
        f"@{group.name} Mitglieder={group.add or '[]'} "
        f"ausgenommen={group.remove or '[]'} "
        f"erbt={'ja' if group.inherit else 'nein'} "
        f"vererbbar={'ja' if group.inheritable else 'nein'}"
    )


def _acl_key(entry: ACLEntry) -> tuple[str, int, bool, bool, int, int]:
    return (
        entry.group,
        entry.userid,
        entry.apply_here,
        entry.apply_subs,
        entry.allow,
        entry.deny,
    )


def _group_key(group: ChannelGroup) -> tuple[str, bool, bool, tuple[int, ...], tuple[int, ...]]:
    return (
        group.name,
        group.inherit,
        group.inheritable,
        tuple(sorted(group.add)),
        tuple(sorted(group.remove)),
    )


class Reconciler:
    """Bringt den Server in den Wunschzustand -- oder sagt nur, was noetig waere."""

    def __init__(
        self,
        client: IceClient,
        config: IntercomConfig,
        *,
        prune: bool = False,
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.config = config
        self.prune = prune
        self.dry_run = dry_run
        self.plan = Plan(dry_run=dry_run, prune=prune, issues=list(config.issues))
        #: Kanalpfad -> ID. Im Trockenlauf bekommen geplante Kanaele negative
        #: Platzhalter-IDs, damit Kinder trotzdem zugeordnet werden koennen.
        self._path_to_id: dict[str, int] = {}
        self._next_virtual_id = -1000

    # ------------------------------------------------------------------ #

    def run(self) -> Plan:
        import time

        started = time.monotonic()

        live_channels = self.client.get_channels()
        self._path_to_id = self._build_paths(live_channels)

        # Alle Namen einsammeln, die aufgeloest werden muessen: aus users: UND
        # aus channels[].groups[]. Wer das vergisst, legt die Gruppe an, aber
        # ohne Mitglieder -- und zwar lautlos.
        namen: list[str] = list(self.config.users)
        for kanal in self.config.all_channels():
            for gruppe in kanal.groups:
                namen.extend(gruppe.add)
                namen.extend(gruppe.remove)

        desired = build_desired_state(
            self.config, self.client.get_user_ids(sorted(set(namen)))
        )
        self.plan.unknown_users = desired.unknown_users
        self.plan.priority_map = {
            c.path: c.priority_groups for c in desired.channels if c.priority_groups
        }
        self.plan.listen_map = {
            c.path: c.listen_to for c in desired.channels if c.listen_to
        }

        self._reconcile_channels(desired, live_channels)
        self._reconcile_links(desired)
        self._reconcile_root(desired)
        self._reconcile_channel_acls(desired)
        self._prune_channels(desired, live_channels)
        self._reconcile_conf(desired)

        self.plan.duration_ms = int((time.monotonic() - started) * 1000)
        return self.plan

    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_paths(channels: dict[int, MumbleChannel]) -> dict[str, int]:
        """Kanal-IDs -> Pfade wie ``Intercom/Kameras/Kamera 1``.

        Der Wurzelkanal (ID 0) hat den leeren Pfad; alles andere haengt darunter.
        """
        paths: dict[str, int] = {"": 0}

        def path_of(channel_id: int, guard: int = 0) -> str | None:
            if guard > 64:  # kaputte Elternkette
                return None
            channel = channels.get(channel_id)
            if channel is None:
                return None
            if channel.parent < 0:
                return ""
            parent_path = path_of(channel.parent, guard + 1)
            if parent_path is None:
                return None
            return f"{parent_path}/{channel.name}" if parent_path else channel.name

        for channel_id in channels:
            resolved = path_of(channel_id)
            if resolved is not None:
                paths[resolved] = channel_id
        return paths

    def _record(self, change: Change) -> None:
        self.plan.changes.append(change)

    def _should_execute(self, change: Change) -> bool:
        if self.dry_run:
            return False
        return not (change.needs_prune and not self.prune)

    def _execute(
        self,
        change: Change,
        action: Callable[[], None],
        mitziehen: Sequence[Change] = (),
    ) -> None:
        """Fuehrt die Aenderung aus, wenn wir nicht im Trockenlauf sind.

        ``mitziehen`` sind bereits gemeldete Aenderungen, die derselbe Aufruf
        miterledigt -- typisch das Loeschen einer fremden Gruppe, das nur
        dadurch geschieht, dass sie in der Liste fehlt, die ``setACL``
        schreibt. Sie bekommen dasselbe Ergebnis wie ``change``: angewendet
        nur, wenn der Aufruf durchging, sonst denselben Fehlertext.

        Vorher trugen diese Aenderungen ``applied=not dry_run`` schon bei der
        Meldung. Das war eine Aussage ueber einen Aufruf, der noch gar nicht
        gelaufen war -- schlug ``setACL`` fehl, stand im Bericht trotzdem
        ``[angewendet]``, waehrend die Gruppe unveraendert im Server steht.
        """
        self._record(change)
        if not self._should_execute(change):
            return
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - im Report sichtbar machen
            change.error = str(exc)
            for weitere in mitziehen:
                weitere.error = str(exc)
            log.error(
                "Provisioning: %s (%s) fehlgeschlagen: %s",
                change.summary,
                change.target,
                exc,
            )
            return
        change.applied = True
        for weitere in mitziehen:
            if self._should_execute(weitere):
                weitere.applied = True

    # ------------------------------------------------------------------ #
    #  Kanaele
    # ------------------------------------------------------------------ #

    def _reconcile_channels(
        self, desired: DesiredState, live: dict[int, MumbleChannel]
    ) -> None:
        for want in desired.channels:
            parent_id = self._path_to_id.get(want.parent_path or "")
            if parent_id is None:
                # Kann nur passieren, wenn der Elternkanal gerade erst geplant
                # wurde und im Trockenlauf keine echte ID hat.
                self._record(
                    Change(
                        kind="note",
                        target=want.path,
                        summary=(
                            f"Der Platz darüber ({want.parent_path!r}) "
                            "fehlt – übersprungen."
                        ),
                    )
                )
                continue

            existing_id = self._path_to_id.get(want.path)
            if existing_id is None:
                change = Change(
                    kind="channel_create",
                    target=want.path,
                    summary="Platz anlegen",
                    after=[
                        f"Name: {want.name}",
                        f"Beschreibung: {want.description or '(keine)'}",
                        f"Position: {want.position}",
                    ],
                )
                created: dict[str, int] = {}

                def action(w: Any = want, p: Any = parent_id, out: Any = created) -> None:
                    out["id"] = self.client.add_channel(w.name, p)

                self._execute(change, action)
                if change.applied and "id" in created:
                    channel_id = created["id"]
                    self._path_to_id[want.path] = channel_id
                    # Beschreibung und Position sind bei addChannel nicht dabei.
                    self._apply_channel_details(want, channel_id, is_new=True)
                else:
                    self._path_to_id[want.path] = self._next_virtual_id
                    self._next_virtual_id -= 1
                    if self.dry_run:
                        self._record(
                            Change(
                                kind="channel_update",
                                target=want.path,
                                summary="Beschreibung und Position setzen",
                                after=[
                                    f"Beschreibung: {want.description or '(keine)'}",
                                    f"Position: {want.position}",
                                ],
                            )
                        )
                continue

            current = live.get(existing_id)
            if current is not None:
                self._apply_channel_details(want, existing_id, current=current)

    def _apply_channel_details(
        self,
        want: DesiredChannel,
        channel_id: int,
        *,
        current: MumbleChannel | None = None,
        is_new: bool = False,
    ) -> None:
        """Setzt Beschreibung und Position, wenn sie abweichen."""
        if current is None:
            try:
                current = self.client.get_channel_state(channel_id)
            except Exception as exc:  # noqa: BLE001
                self._record(
                    Change(
                        kind="channel_update",
                        target=want.path,
                        summary="Platz nicht lesbar",
                        error=str(exc),
                    )
                )
                return

        differences: list[tuple[str, object, object]] = []
        if current.description != want.description:
            differences.append(("Beschreibung", current.description, want.description))
        if current.position != want.position:
            differences.append(("Position", current.position, want.position))
        if current.name != want.name:
            differences.append(("Name", current.name, want.name))
        if not differences:
            return

        change = Change(
            kind="channel_update",
            target=want.path,
            summary="Platz ändern",
            before=[f"{label}: {old!r}" for label, old, _ in differences],
            after=[f"{label}: {new!r}" for label, _, new in differences],
        )

        def action(w: Any = want, cur: Any = current, cid: Any = channel_id) -> None:
            updated = MumbleChannel(
                id=cid,
                name=w.name,
                parent=cur.parent,
                description=w.description,
                temporary=cur.temporary,
                position=w.position,
                links=list(cur.links),
            )
            self.client.set_channel_state(updated)

        if is_new:
            # Bei einem frisch angelegten Kanal ist das keine Aenderung, sondern
            # Teil des Anlegens -- ausfuehren, aber nicht doppelt melden.
            if not self.dry_run:
                try:
                    action()
                except Exception as exc:  # noqa: BLE001
                    self._record(
                        Change(
                            kind="channel_update",
                            target=want.path,
                            summary="Beschreibung/Position setzen",
                            error=str(exc),
                        )
                    )
            return

        self._execute(change, action)

    def _reconcile_links(self, desired: DesiredState) -> None:
        """Kanalverlinkungen. Verlinkung ist wechselseitig, murmur speichert sie
        aber je Kanal -- wir schreiben nur die in der YAML genannte Richtung und
        verlassen uns darauf, dass murmur die Gegenrichtung mitfuehrt."""
        for want in desired.channels:
            if not want.links:
                continue
            channel_id = self._path_to_id.get(want.path)
            if channel_id is None or channel_id < 0:
                continue
            wanted_ids = sorted(
                {
                    self._path_to_id[target]
                    for target in want.links
                    if self._path_to_id.get(target, -1) >= 0
                }
            )
            try:
                current = self.client.get_channel_state(channel_id)
            except Exception:  # noqa: BLE001
                continue
            if sorted(current.links) == wanted_ids:
                continue
            change = Change(
                kind="channel_link",
                target=want.path,
                summary="Verlinkungen ändern",
                before=[f"verlinkt mit IDs {sorted(current.links)}"],
                after=[f"verlinkt mit {want.links}"],
            )

            def action(cid: Any = channel_id, cur: Any = current, ids: Any = wanted_ids) -> None:
                cur.links = ids
                self.client.set_channel_state(cur)

            self._execute(change, action)

    def _prune_channels(
        self, desired: DesiredState, live: dict[int, MumbleChannel]
    ) -> None:
        """Kanaele im verwalteten Teilbaum, die nicht in der YAML stehen."""
        managed_roots = [c.path for c in desired.channels if c.parent_path is None]
        wanted_paths = {c.path for c in desired.channels}

        for path, channel_id in sorted(self._path_to_id.items()):
            if channel_id <= 0 or path in wanted_paths:
                continue
            if not any(
                path == root or path.startswith(f"{root}/") for root in managed_roots
            ):
                # Ausserhalb des verwalteten Teilbaums -- nie anfassen.
                continue
            if channel_id not in live:
                continue
            change = Change(
                kind="channel_delete",
                target=path,
                summary="Platz löschen (steht nicht in der Show bzw. Datei)",
                before=[f"Kanal-ID {channel_id}"],
                destructive=True,
                needs_prune=True,
            )

            def entfernen(cid: int = channel_id) -> None:
                self.client.remove_channel(cid)

            self._execute(change, entfernen)

    # ------------------------------------------------------------------ #
    #  ACLs und Gruppen
    # ------------------------------------------------------------------ #

    def _reconcile_root(self, desired: DesiredState) -> None:
        """Gruppen und Richtlinien am Wurzelkanal."""
        try:
            current = self.client.get_acl(0)
        except Exception as exc:  # noqa: BLE001
            self._record(
                Change(
                    kind="acl_update",
                    target=UEBERALL,
                    summary="Regeln nicht lesbar",
                    error=str(exc),
                )
            )
            return

        current_groups = current.own_groups()
        managed_names = {g.name for g in desired.root_groups}

        # Gruppen, die wir nicht verwalten, bleiben erhalten -- setACL wuerde
        # sie sonst mitloeschen, obwohl PROVISION_PRUNE aus ist.
        preserved: list[ChannelGroup] = []
        # Aenderungen, die erst das setACL am Ende wirklich ausfuehrt.
        mitziehen: list[Change] = []
        for group in current_groups:
            if group.name in managed_names:
                continue
            if group.name not in META_GROUPS:
                geloescht = Change(
                    kind="group_update",
                    target=f"{UEBERALL} @{group.name}",
                    summary="Rolle löschen (steht nicht in der Show bzw. Datei)",
                    before=[_group_line(group)],
                    destructive=True,
                    needs_prune=True,
                )
                self._record(geloescht)
                if self.prune:
                    mitziehen.append(geloescht)
                    continue
            preserved.append(group)

        wanted_groups = list(desired.root_groups) + preserved

        # Dasselbe fuer die ACLs: setACL ersetzt auch sie vollstaendig. Ein
        # Eintrag, den jemand von Hand angelegt hat -- etwa ein Recht fuer einen
        # einzelnen Nutzer --, wuerde sonst beim ersten Lauf verschwinden.
        # Er bleibt hinter den verwalteten stehen, behaelt damit das letzte
        # Wort, und der Plan meldet ihn als loeschbar.
        verwaltete_keys = {entry.key() for entry in desired.root_acls}
        fremde_acls = [
            entry for entry in current.own_acls() if entry.key() not in verwaltete_keys
        ]
        for entry in fremde_acls:
            geloescht = Change(
                kind="acl_update",
                target=f"{UEBERALL} {_acl_line(entry)}",
                summary="Regel löschen (steht nicht in der Show bzw. Datei)",
                before=[_acl_line(entry)],
                destructive=True,
                needs_prune=True,
            )
            self._record(geloescht)
            if self.prune:
                mitziehen.append(geloescht)
        if self.prune:
            fremde_acls = []

        wanted_acls = list(desired.root_acls) + fremde_acls

        group_diff = self._diff_groups(current_groups, wanted_groups)
        acl_diff = self._diff_acls(current.own_acls(), wanted_acls)

        if not group_diff and not acl_diff:
            return

        change = Change(
            kind="acl_update",
            target=UEBERALL,
            summary="Rollen und Richtlinien setzen, die überall gelten",
            before=[line for line, _ in group_diff] + [line for line, _ in acl_diff],
            after=[line for _, line in group_diff] + [line for _, line in acl_diff],
        )

        def action() -> None:
            self.client.set_channel_acl(
                ChannelACL(
                    channel_id=0,
                    acls=wanted_acls,
                    groups=wanted_groups,
                    inherit=current.inherit,
                )
            )

        self._execute(change, action, mitziehen)

    def _reconcile_channel_acls(self, desired: DesiredState) -> None:
        for want in desired.channels:
            channel_id = self._path_to_id.get(want.path)
            if channel_id is None:
                continue
            if channel_id < 0:
                # Kanal wird erst angelegt -- im Trockenlauf gibt es nichts zu
                # vergleichen, wir melden die vollstaendige Neuvergabe.
                if want.acls:
                    self._record(
                        Change(
                            kind="acl_update",
                            target=want.path,
                            summary="Regeln setzen",
                            after=[_acl_line(a) for a in want.acls],
                        )
                    )
                continue
            try:
                current = self.client.get_acl(channel_id)
            except Exception as exc:  # noqa: BLE001
                self._record(
                    Change(
                        kind="acl_update",
                        target=want.path,
                        summary="Regeln nicht lesbar",
                        error=str(exc),
                    )
                )
                continue

            # Gruppen, die der Kanal selbst fuehrt und die NICHT in der YAML
            # stehen, bleiben erhalten -- setACL ersetzt alle Gruppen und
            # wuerde sie sonst auch ohne PROVISION_PRUNE mitloeschen.
            verwaltet = {g.name for g in want.groups}
            bewahrt = [g for g in current.own_groups() if g.name not in verwaltet]
            # Auch hier gilt: geloescht wird eine Gruppe erst dadurch, dass sie
            # im setACL am Ende fehlt. Bis dahin ist nichts angewendet.
            mitziehen = []
            for group in bewahrt:
                geloescht = Change(
                    kind="group_update",
                    target=f"{want.path} @{group.name}",
                    summary="Rolle löschen (steht nicht in der Show bzw. Datei)",
                    before=[_group_line(group)],
                    destructive=True,
                    needs_prune=True,
                )
                self._record(geloescht)
                if self.prune:
                    mitziehen.append(geloescht)
            if self.prune:
                bewahrt = []

            keep_groups = list(want.groups) + bewahrt
            acl_diff = self._diff_acls(current.own_acls(), want.acls)
            group_diff = self._diff_groups(current.own_groups(), keep_groups)
            if not acl_diff and not group_diff:
                continue

            change = Change(
                kind="acl_update",
                target=want.path,
                summary="Regeln ändern" if acl_diff else "Rollen ändern",
                before=[line for line, _ in acl_diff if line]
                + [line for line, _ in group_diff if line],
                after=[line for _, line in acl_diff if line]
                + [line for _, line in group_diff if line],
            )

            def action(
                cid: Any = channel_id,
                acls: Any = want.acls,
                groups: Any = keep_groups,
                inherit: Any = current.inherit,
            ) -> None:
                self.client.set_channel_acl(
                    ChannelACL(
                        channel_id=cid,
                        acls=list(acls),
                        groups=list(groups),
                        inherit=inherit,
                    )
                )

            self._execute(change, action, mitziehen)

    @staticmethod
    def _diff_acls(
        current: list[ACLEntry], wanted: list[ACLEntry]
    ) -> list[tuple[str, str]]:
        """Gegenueberstellung Ist/Soll. Leere Liste heisst: gleich.

        Verglichen wird die Liste **in Reihenfolge**: bei ACLs ist die
        Reihenfolge Semantik, nicht Kosmetik.
        """
        if [_acl_key(a) for a in current] == [_acl_key(a) for a in wanted]:
            return []
        rows: list[tuple[str, str]] = []
        for index in range(max(len(current), len(wanted))):
            old = _acl_line(current[index]) if index < len(current) else ""
            new = _acl_line(wanted[index]) if index < len(wanted) else ""
            if old != new:
                rows.append((old, new))
        return rows

    @staticmethod
    def _diff_groups(
        current: list[ChannelGroup], wanted: list[ChannelGroup]
    ) -> list[tuple[str, str]]:
        current_map = {g.name: g for g in current}
        wanted_map = {g.name: g for g in wanted}
        rows: list[tuple[str, str]] = []
        for name in sorted(set(current_map) | set(wanted_map)):
            old = current_map.get(name)
            new = wanted_map.get(name)
            if old is not None and new is not None and _group_key(old) == _group_key(new):
                continue
            rows.append(
                (_group_line(old) if old else "", _group_line(new) if new else "")
            )
        return rows

    # ------------------------------------------------------------------ #
    #  Serverkonfiguration
    # ------------------------------------------------------------------ #

    #: murmur benennt beim Schreiben um: `setConf("serverpassword", ...)` landet
    #: in der Datenbank als `password` (src/murmur/ServerDB.cpp, Z. 2589-2592),
    #: waehrend `getAllConf` die Tabelle roh liest. Ein Vergleich gegen den
    #: urspruenglichen Namen findet also nie etwas -- der Plan stuende dauerhaft
    #: auf rot, obwohl der Wert laengst gesetzt ist. Beide Namen wirken gleich
    #: (Server::setLiveConf), wir benutzen deshalb durchgaengig den, unter dem
    #: der Server ihn auch wieder herausgibt.
    CONF_ALIASE: ClassVar[dict[str, str]] = {"serverpassword": "password"}

    def _reconcile_conf(self, desired: DesiredState) -> None:
        wanted = {
            self.CONF_ALIASE.get(key, key): value for key, value in desired.conf.items()
        }

        if desired.default_channel_path:
            channel_id = self._path_to_id.get(desired.default_channel_path.strip("/"))
            if channel_id is None or channel_id < 0:
                self._record(
                    Change(
                        kind="note",
                        target="server.defaultchannel",
                        summary=(
                            f"Platz {desired.default_channel_path!r} gibt es noch "
                            "nicht – der Startplatz wird beim nächsten Lauf gesetzt."
                        ),
                    )
                )
            else:
                wanted["defaultchannel"] = str(channel_id)

        if not wanted:
            return
        # get_effective_conf, nicht get_all_conf: verglichen wird gegen das, was
        # der Server TATSAECHLICH benutzt -- Datenbankeintrag, sonst Dateiwert
        # bzw. eingebaute Vorgabe.
        #
        # Mit get_all_conf (nur die Datenbank) war eine Sicherungsrunde nicht
        # neutral: der Export liest den wirksamen Wert, und beim Einspielen galt
        # jede Vorgabe als "nicht gesetzt" und wurde in die Datenbank
        # geschrieben. Eine zurueckgespielte Sicherung haette damit ein Dutzend
        # murmur-Vorgaben festgenagelt, die vorher frei waren -- und die
        # naechste Aenderung an der Compose waere wirkungslos geblieben, ohne
        # dass jemand sieht warum.
        try:
            current = self.client.get_effective_conf()
        except Exception as exc:  # noqa: BLE001
            self._record(
                Change(
                    kind="conf_set",
                    target="(Konfiguration)",
                    summary="nicht lesbar",
                    error=str(exc),
                )
            )
            return

        for key, value in sorted(wanted.items()):
            if current.get(key, "") == value:
                continue
            change = Change(
                kind="conf_set",
                target=f"server.{key}",
                summary="Konfigurationswert setzen",
                before=[current.get(key, "(nicht gesetzt)")],
                after=[value],
            )

            def schreiben(k: str = key, v: str = value) -> None:
                self.client.set_conf(k, v)

            self._execute(change, schreiben)


def reconcile(
    client: IceClient,
    config: IntercomConfig,
    *,
    prune: bool = False,
    dry_run: bool = True,
) -> Plan:
    """Bequemlichkeitsfunktion um :class:`Reconciler`."""
    return Reconciler(client, config, prune=prune, dry_run=dry_run).run()
