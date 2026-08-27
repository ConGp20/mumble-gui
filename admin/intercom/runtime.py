"""Laufzeit-Abgleich: Priority Speaker und Channel Listener.

Zwei Dinge aus ``intercom.yaml`` lassen sich nicht provisionieren, weil sie
kein dauerhafter Serverzustand sind:

``priority``
    Priority Speaker ist **kein ACL-Recht**, sondern ein Feld im ``User``-Struct
    (``prioritySpeaker``). Es haengt an der Sitzung und ist weg, sobald der
    Client sich neu verbindet. Manche Clients setzen es beim Verbinden auch
    aktiv zurueck.

``listen_to``
    ``startListening`` nimmt entgegen der Slice-Dokumentation eine **Session**,
    keine Nutzer-ID -- belegt in ``impl_Server_startListening``
    (MumbleServerIce.cpp), das ueber ``NEED_PLAYER`` aufloest. Listener sind
    damit ebenfalls Sitzungszustand.

Beides wird deshalb hier zur Laufzeit nachgezogen: bei ``userConnected`` und
``userStateChanged``, und zusaetzlich bei jedem Polling-Durchlauf als Netz mit
doppeltem Boden, falls ein Callback verloren geht.

Der Abgleich ist absichtlich **anzeigend und korrigierend zugleich**: jede
Abweichung landet in :attr:`Enforcer.deviations`, damit das Cockpit sie zeigen
kann, auch wenn sie im selben Moment behoben wird. Wer nur einen roten Punkt
sieht, ohne zu erfahren, dass staendig korrigiert werden muss, sucht den Fehler
an der falschen Stelle.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .ice.errors import IceError
from .ice.types import MumbleChannel, MumbleUser

if TYPE_CHECKING:
    from .ice.client import IceClient
    from .provision.acl_map import DesiredState

log = logging.getLogger(__name__)

__all__ = ["Deviation", "Enforcer", "GroupMembership", "build_paths"]


def build_paths(channels: dict[int, MumbleChannel]) -> dict[str, int]:
    """Kanalbaum -> ``{"Intercom/Regie": 7, ...}``. Die Wurzel hat den leeren Pfad."""
    paths: dict[str, int] = {"": 0}

    def path_of(channel_id: int, guard: int = 0) -> str | None:
        if guard > 64:
            return None
        channel = channels.get(channel_id)
        if channel is None:
            return None
        if channel.parent < 0:
            return ""
        parent = path_of(channel.parent, guard + 1)
        if parent is None:
            return None
        return f"{parent}/{channel.name}" if parent else channel.name

    for channel_id in channels:
        resolved = path_of(channel_id)
        if resolved is not None:
            paths[resolved] = channel_id
    return paths


@dataclass(frozen=True, slots=True)
class Deviation:
    """Eine Abweichung zwischen Soll und Ist im laufenden Betrieb."""

    kind: str
    """``priority_speaker`` oder ``listener``."""

    session: int
    user: str
    channel: str
    detail: str
    corrected: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "session": self.session,
            "user": self.user,
            "channel": self.channel,
            "detail": self.detail,
            "corrected": self.corrected,
        }


@dataclass(slots=True)
class GroupMembership:
    """Wer ist in welcher Gruppe.

    Quelle sind die ``add``-Listen der Gruppen am Wurzelkanal -- dort legt der
    Provisioner die dauerhafte Mitgliedschaft ab (siehe DECISIONS D-005). Die
    von murmur eingebauten Gruppen (``all``, ``auth`` ...) stehen nicht darin;
    ``includes`` behandelt sie gesondert.
    """

    by_user: dict[int, set[str]] = field(default_factory=dict)

    @classmethod
    def from_root_acl(cls, groups: Iterable[Any]) -> GroupMembership:
        by_user: dict[int, set[str]] = {}
        for group in groups:
            # `members` enthaelt auch geerbte Mitglieder; an der Wurzel ist das
            # dasselbe wie `add`, aber wir nehmen die groessere Menge.
            for userid in set(group.add) | set(group.members):
                by_user.setdefault(userid, set()).add(group.name)
        return cls(by_user=by_user)

    def includes(self, user: MumbleUser, wanted: Iterable[str]) -> bool:
        """Gehoert ``user`` zu einer der genannten Gruppen?"""
        names = set(wanted)
        if not names:
            return False
        if "all" in names:
            return True
        if "auth" in names and user.registered:
            return True
        if not user.registered:
            return False
        return bool(self.by_user.get(user.userid, set()) & names)


class Enforcer:
    """Haelt Priority Speaker und Listener am Wunschzustand.

    Der Abgleich ist zustandslos gegenueber dem Server: es wird immer erst
    gelesen und nur bei echter Abweichung geschrieben. Damit kann ein
    ``userStateChanged``, das unsere eigene Aenderung meldet, keine Schleife
    ausloesen -- beim zweiten Durchlauf stimmt der Zustand bereits.
    """

    def __init__(self, client: IceClient) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._desired: DesiredState | None = None
        self._membership = GroupMembership()
        self._paths: dict[str, int] = {}
        self._channel_of_path: dict[int, str] = {}
        #: Kanal-ID -> Gruppen mit Priority Speaker
        self._priority: dict[int, list[str]] = {}
        #: Kanal-ID -> (Gruppen, die es betrifft, Ziel-Kanal-IDs)
        self._listen: dict[int, tuple[list[str], list[int]]] = {}
        self._deviations: list[Deviation] = []
        #: True, sobald ein Wunschzustand geladen wurde.
        self.armed = False

    # ------------------------------------------------------------------ #

    @property
    def deviations(self) -> list[Deviation]:
        with self._lock:
            return list(self._deviations)

    def load(self, desired: DesiredState, channels: dict[int, MumbleChannel]) -> None:
        """Uebernimmt den Wunschzustand und loest Kanalpfade in IDs auf.

        Wird nach jedem Provisioning und nach jeder Kanalaenderung aufgerufen --
        ohne aufgeloeste IDs waere jeder Abgleich eine Suche im Baum.
        """
        with self._lock:
            self._desired = desired
            self._paths = build_paths(channels)
            self._channel_of_path = {cid: path for path, cid in self._paths.items()}
            self._priority = {}
            self._listen = {}

            for channel in desired.channels:
                channel_id = self._paths.get(channel.path)
                if channel_id is None:
                    # Kanal noch nicht angelegt -- beim naechsten Lauf.
                    continue
                if channel.priority_groups:
                    self._priority[channel_id] = list(channel.priority_groups)
                if channel.listen_to:
                    targets = [
                        self._paths[target]
                        for target in channel.listen_to
                        if target in self._paths
                    ]
                    if targets:
                        # Wer die Listener bekommen soll: die speak-Gruppen des
                        # Kanals. Ohne speak-Angabe gilt es fuer alle, die drin
                        # sitzen -- sonst bekaeme niemand die Listener.
                        source = next(
                            (
                                c
                                for c in desired.channels
                                if c.path == channel.path
                            ),
                            None,
                        )
                        groups = _speak_groups(source) if source else ["all"]
                        self._listen[channel_id] = (groups, targets)
            self.armed = True

    def refresh_membership(self) -> None:
        """Liest die Gruppen am Wurzelkanal neu ein."""
        try:
            root = self._client.get_acl(0)
        except IceError:
            log.debug("Gruppen am Wurzelkanal nicht lesbar", exc_info=True)
            return
        with self._lock:
            self._membership = GroupMembership.from_root_acl(root.groups)

    # ------------------------------------------------------------------ #

    def enforce_user(self, user: MumbleUser) -> list[Deviation]:
        """Gleicht einen einzelnen Client ab. Gibt die gefundenen Abweichungen zurueck."""
        if not self.armed:
            return []
        found: list[Deviation] = []
        found.extend(self._enforce_priority(user))
        found.extend(self._enforce_listeners(user))
        return found

    def enforce_all(self, users: Iterable[MumbleUser]) -> list[Deviation]:
        """Vollstaendiger Durchlauf. Ersetzt die Abweichungsliste."""
        if not self.armed:
            return []
        found: list[Deviation] = []
        for user in users:
            found.extend(self.enforce_user(user))
        with self._lock:
            self._deviations = found
        return found

    # ------------------------------------------------------------------ #

    def _channel_name(self, channel_id: int) -> str:
        return self._channel_of_path.get(channel_id, f"#{channel_id}") or "(Wurzel)"

    def _enforce_priority(self, user: MumbleUser) -> list[Deviation]:
        with self._lock:
            groups = self._priority.get(user.channel)
            membership = self._membership

        if not groups:
            # Kein Anspruch in diesem Kanal. Ein anderswo gesetztes Flag nehmen
            # wir NICHT weg: es kann von Hand gesetzt worden sein, und ein
            # Automatismus, der eine bewusste Entscheidung zurueckdreht, ist im
            # Betrieb schlimmer als ein Flag zuviel.
            return []

        should = membership.includes(user, groups)
        if should == user.priority_speaker:
            return []
        if not should:
            return []

        deviation = Deviation(
            kind="priority_speaker",
            session=user.session,
            user=user.name,
            channel=self._channel_name(user.channel),
            detail=(
                f"Priority Speaker fehlt (Gruppen: {', '.join(groups)}). "
                "Der Client hat ihn vermutlich beim Verbinden zurueckgesetzt."
            ),
        )
        try:
            self._client.set_user_state(user.session, priority_speaker=True)
            deviation = replace(deviation, corrected=True)
        except IceError as exc:
            log.warning(
                "Priority Speaker fuer %s konnte nicht gesetzt werden: %s", user.name, exc
            )
        return [deviation]

    def _enforce_listeners(self, user: MumbleUser) -> list[Deviation]:
        with self._lock:
            entry = self._listen.get(user.channel)
            membership = self._membership

        if entry is None:
            return []
        groups, targets = entry
        if not membership.includes(user, groups or []):
            return []

        try:
            current = set(self._client.get_listening_channels(user.session))
        except IceError:
            return []

        missing = [target for target in targets if target not in current]
        if not missing:
            return []

        names = ", ".join(self._channel_name(target) for target in missing)
        corrected = True
        for target in missing:
            try:
                self._client.start_listening(user.session, target)
            except IceError as exc:
                corrected = False
                log.warning(
                    "Listener %s -> %s konnte nicht gesetzt werden: %s",
                    user.name,
                    self._channel_name(target),
                    exc,
                )
        return [
            Deviation(
                kind="listener",
                session=user.session,
                user=user.name,
                channel=self._channel_name(user.channel),
                detail=f"Mithoeren fehlt fuer: {names}",
                corrected=corrected,
            )
        ]

    # ------------------------------------------------------------------ #

    def expected_listeners(self, user: MumbleUser) -> list[int]:
        """Soll-Listener eines Clients -- fuer die Anzeige im Detailpanel."""
        with self._lock:
            entry = self._listen.get(user.channel)
            membership = self._membership
        if entry is None:
            return []
        groups, targets = entry
        return list(targets) if membership.includes(user, groups) else []

    def expects_priority(self, user: MumbleUser) -> bool:
        with self._lock:
            groups = self._priority.get(user.channel)
            membership = self._membership
        return bool(groups) and membership.includes(user, groups or [])


def _speak_groups(channel: Any) -> list[str]:
    """Die Gruppen, die in einem Kanal sprechen duerfen.

    Wird aus den erzeugten ACLs zurueckgelesen statt aus der YAML, damit auch
    ein ``acl:``-Block oder eine Vorlage beruecksichtigt wird.
    """
    from .provision.acl_map import SPEAK

    all_entry = next(
        (e for e in channel.acls if e.group == "all" and e.apply_here), None
    )
    if all_entry is not None and all_entry.allow & SPEAK:
        return ["all"]
    groups = [
        entry.group
        for entry in channel.acls
        if entry.is_group and entry.group != "all" and entry.allow & SPEAK
    ]
    return groups or ["all"]
