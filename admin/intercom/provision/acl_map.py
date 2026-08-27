"""Abbildung ``intercom.yaml`` -> ACL-Eintraege und Gruppen.

Das ist die Stelle, an der aus "Regie darf hier sprechen" die Bitmasken werden,
die murmur versteht. Sie ist bewusst frei von Ice und von Netzwerkzugriff:
Eingabe ist die Konfiguration plus eine Namen-zu-ID-Tabelle, Ausgabe sind reine
Datenklassen. Damit ist sie ohne laufenden Server testbar.

Reihenfolge ist Semantik
------------------------
``ChanACL::effectivePermissions`` in ``src/ACL.cpp`` laeuft die Kanalkette von
der Wurzel abwaerts und innerhalb eines Kanals die ACL-Liste **der Reihe nach**
durch::

    granted |= acl->pAllow;
    granted &= ~acl->pDeny;

Ein spaeterer Eintrag gewinnt also gegen einen frueheren. Wir erzeugen deshalb
pro Kanal immer dieselbe Reihenfolge:

1. Eintraege aus ``acl_template`` -- unveraendert, in Vorlagenreihenfolge.
2. Ein zusammengefasster ``@all``-Eintrag aus ``speak`` / ``whisper_in`` /
   ``listen_for``. Er ueberschreibt die Vorlage.
3. Ein Eintrag je Gruppe, nach Gruppenname sortiert. Er ueberschreibt ``@all``.

Ausgangslage: murmur gewaehrt ohne jede ACL bereits
``Traverse | Enter | Speak | Whisper | TextMessage | Listen`` (ACL.cpp, ``def``).
Ein ``deny`` ist also nicht Zierde, sondern noetig, um etwas wegzunehmen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ice.permissions import BY_NAME
from ..ice.types import ACLEntry, ChannelGroup
from .schema import (
    POLICY_PERMISSIONS,
    PREDEFINED_GROUPS,
    ROOT_ONLY_POLICIES,
    ChannelSpec,
    IntercomConfig,
)

__all__ = [
    "DesiredChannel",
    "DesiredState",
    "build_desired_state",
    "merge_entries",
]

SPEAK = BY_NAME["Speak"].bit
WHISPER = BY_NAME["Whisper"].bit
LISTEN = BY_NAME["Listen"].bit
TRAVERSE = BY_NAME["Traverse"].bit
ENTER = BY_NAME["Enter"].bit
WRITE = BY_NAME["Write"].bit


@dataclass(slots=True)
class DesiredChannel:
    """Wunschzustand eines Kanals, noch ohne Kanal-IDs."""

    path: str
    name: str
    parent_path: str | None
    description: str = ""
    position: int = 0
    links: list[str] = field(default_factory=list)
    acls: list[ACLEntry] = field(default_factory=list)
    groups: list[ChannelGroup] = field(default_factory=list)
    inherit: bool = True
    #: Gruppen, deren Mitglieder hier Priority Speaker sein sollen. Kein ACL --
    #: Priority Speaker ist Nutzerzustand und wird zur Laufzeit gesetzt.
    priority_groups: list[str] = field(default_factory=list)
    #: Kanaele, die Mitglieder der speak-Gruppen zusaetzlich mithoeren sollen.
    #: Ebenfalls Laufzeit: Listener haengen an der Sitzung (DECISIONS D-004).
    listen_to: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DesiredState:
    """Der komplette Wunschzustand, wie ihn der Planer vergleicht."""

    #: Kanaele in Anlege-Reihenfolge -- Eltern immer vor Kindern.
    channels: list[DesiredChannel] = field(default_factory=list)
    #: ACLs am Wurzelkanal (ID 0): Richtlinien und die Gastregel.
    root_acls: list[ACLEntry] = field(default_factory=list)
    #: Gruppen am Wurzelkanal.
    root_groups: list[ChannelGroup] = field(default_factory=list)
    #: Schluessel fuer ``setConf``.
    conf: dict[str, str] = field(default_factory=dict)
    #: ``server.defaultchannel`` als Pfad; die ID kennt erst der Planer.
    default_channel_path: str | None = None
    #: Namen aus ``users:``, die nicht registriert sind.
    unknown_users: list[str] = field(default_factory=list)

    def channel(self, path: str) -> DesiredChannel | None:
        for channel in self.channels:
            if channel.path == path:
                return channel
        return None


def _entry(
    group: str,
    allow: int,
    deny: int,
    *,
    apply_here: bool = True,
    apply_subs: bool = False,
    userid: int = -1,
) -> ACLEntry:
    return ACLEntry(
        apply_here=apply_here,
        apply_subs=apply_subs,
        allow=allow,
        deny=deny,
        group=group,
        userid=userid,
    )


def merge_entries(entries: list[ACLEntry]) -> list[ACLEntry]:
    """Fasst Eintraege mit gleichem Schluessel zusammen.

    Nur *gleiche* Schluessel (Gruppe, Nutzer, Geltungsbereich) werden
    verschmolzen, und die urspruengliche Reihenfolge bleibt erhalten -- sonst
    aenderte sich die Auswertungsreihenfolge und damit die Bedeutung.
    """
    merged: list[ACLEntry] = []
    index: dict[tuple[str, int, bool, bool], ACLEntry] = {}
    for entry in entries:
        key = entry.key()
        existing = index.get(key)
        if existing is None:
            copy = ACLEntry(
                apply_here=entry.apply_here,
                apply_subs=entry.apply_subs,
                allow=entry.allow,
                deny=entry.deny,
                group=entry.group,
                userid=entry.userid,
            )
            index[key] = copy
            merged.append(copy)
        else:
            existing.allow |= entry.allow
            existing.deny |= entry.deny
            # Innerhalb eines Eintrags schlaegt deny das allow -- murmur wertet
            # 'granted |= allow; granted &= ~deny' in genau dieser Folge aus.
            existing.allow &= ~existing.deny
    return merged


def _channel_acls(spec: ChannelSpec, config: IntercomConfig) -> list[ACLEntry]:
    """Baut die ACL-Liste eines Kanals aus Vorlage plus speak/whisper/listen."""
    entries: list[ACLEntry] = []

    # -- 1. Vorlage ---------------------------------------------------------
    if spec.acl_template:
        for template_entry in config.acl_templates.get(spec.acl_template, []):
            entries.append(
                ACLEntry(
                    apply_here=template_entry.apply_here,
                    apply_subs=template_entry.apply_sub,
                    allow=template_entry.allow_mask,
                    deny=template_entry.deny_mask,
                    group=template_entry.group,
                    userid=template_entry.userid,
                )
            )

    # -- 1b. Rohe Eintraege aus channels[].acl -------------------------------
    for raw_entry in spec.acl:
        entries.append(
            ACLEntry(
                apply_here=raw_entry.apply_here,
                apply_subs=raw_entry.apply_sub,
                allow=raw_entry.allow_mask,
                deny=raw_entry.deny_mask,
                group=raw_entry.group,
                userid=raw_entry.userid,
            )
        )

    # -- 2. @all-Grundlage aus speak / whisper_in / listen_for ---------------
    all_allow = 0
    all_deny = 0
    per_group: dict[str, int] = {}

    def apply_rule(members: list[str], bit: int, needs_enter: bool) -> None:
        """Eine Regel auswerten.

        ``[all]`` heisst: alle duerfen es. Sonst wird es fuer ``@all`` verboten
        und den genannten Gruppen einzeln erlaubt.

        Leere Liste heisst: die Regel sagt zu diesem Recht nichts. Dann wird
        weder erlaubt noch verboten und die Vererbung bleibt unangetastet.
        """
        nonlocal all_allow, all_deny
        if not members:
            return
        if "all" in members:
            all_allow |= bit
            all_deny &= ~bit
            return
        all_deny |= bit
        all_allow &= ~bit
        for group_name in members:
            # Wer sprechen soll, muss den Kanal betreten koennen; wer mithoeren
            # oder hineinfluestern soll, muss ihn wenigstens sehen. Ohne das
            # ergaeben restriktive Vorlagen wie "geschlossen" Kanaele, die
            # niemand benutzen kann. Siehe README, Abschnitt Schema.
            extra = (TRAVERSE | ENTER) if needs_enter else TRAVERSE
            per_group[group_name] = per_group.get(group_name, 0) | bit | extra

    apply_rule(spec.speak, SPEAK, needs_enter=True)
    apply_rule(spec.whisper_in, WHISPER, needs_enter=False)
    apply_rule(spec.listen_for, LISTEN, needs_enter=False)

    if all_allow or all_deny:
        entries.append(_entry("all", all_allow, all_deny))

    # -- 3. Gruppen, deterministisch sortiert -------------------------------
    for group_name in sorted(per_group):
        entries.append(_entry(group_name, per_group[group_name], 0))

    return merge_entries(entries)


def _root_acls(config: IntercomConfig) -> list[ACLEntry]:
    """ACLs am Wurzelkanal: Richtlinien und die Gastregel.

    Warum die Wurzel und nicht der oberste konfigurierte Kanal:
    ``Kick``, ``Ban`` und ``Register`` wertet murmur ausschliesslich dort aus --
    ``src/ACL.cpp``::

        if (ch->iId == 0 && applyFromSelf) {
            if (acl->pAllow & Kick) granted |= Kick;

    Ein solcher Eintrag an ``Intercom`` waere wirkungslos. Damit alle
    Richtlinien an derselben Stelle stehen und dieselben Gruppen sehen, liegen
    Richtlinien und Gruppen an der Wurzel.
    """
    entries: list[ACLEntry] = []

    # murmur legt bei einem frischen Server '@admin -> Write' an der Wurzel an.
    # setACL ersetzt alles, also schreiben wir es bewusst mit, sonst verliert
    # der Betreiber seinen manuellen Admin-Weg.
    entries.append(_entry("admin", WRITE, 0, apply_here=True, apply_subs=True))

    # Gastregel: @all darf im ganzen Baum weder sprechen noch fluestern; die
    # Kanaele geben es je Gruppe wieder frei.
    if config.policies.guests_listen_only:
        entries.append(
            _entry("all", 0, SPEAK | WHISPER, apply_here=True, apply_subs=True)
        )

    # Richtlinien, die im ganzen Baum gelten.
    for policy_name, permission in sorted(POLICY_PERMISSIONS.items()):
        if policy_name in ROOT_ONLY_POLICIES:
            continue
        for group_name in sorted(set(config.policies.groups_for(policy_name))):
            entries.append(
                _entry(
                    group_name,
                    BY_NAME[permission].bit,
                    0,
                    apply_here=True,
                    apply_subs=True,
                )
            )

    # Richtlinien, die murmur nur an der Wurzel auswertet -- ohne apply_subs,
    # weil applyFromSelf verlangt wird.
    for policy_name, permission in sorted(ROOT_ONLY_POLICIES.items()):
        for group_name in sorted(set(config.policies.groups_for(policy_name))):
            entries.append(
                _entry(
                    group_name,
                    BY_NAME[permission].bit,
                    0,
                    apply_here=True,
                    apply_subs=False,
                )
            )

    return merge_entries(entries)


def build_desired_state(
    config: IntercomConfig, user_ids: dict[str, int]
) -> DesiredState:
    """Baut den Wunschzustand.

    ``user_ids`` bildet Nutzernamen auf registrierte IDs ab (aus
    ``Server.getUserIds``). Unbekannte Namen -- also ``-1`` -- landen in
    ``unknown_users`` und damit im Provisioning-Report, statt still zu
    verschwinden: Gruppenmitgliedschaft laeuft ueber Nutzer-IDs, ein nicht
    registrierter Name kann kein Mitglied sein.
    """
    state = DesiredState()

    members: dict[str, list[int]] = {group: [] for group in config.groups}
    for user_name, groups in sorted(config.users.items()):
        user_id = user_ids.get(user_name, -1)
        if user_id < 0:
            state.unknown_users.append(user_name)
            continue
        for group_name in groups:
            if group_name in PREDEFINED_GROUPS:
                continue
            members.setdefault(group_name, []).append(user_id)

    for group_name in config.groups:
        state.root_groups.append(
            ChannelGroup(
                name=group_name,
                inherit=True,
                inheritable=True,
                add=sorted(set(members.get(group_name, []))),
                remove=[],
            )
        )

    state.root_acls = _root_acls(config)

    def walk(spec: ChannelSpec, parent_path: str | None) -> None:
        state.channels.append(
            DesiredChannel(
                path=spec.path,
                name=spec.name,
                parent_path=parent_path,
                description=spec.description,
                position=spec.position,
                links=[link.strip("/") for link in spec.links],
                acls=_channel_acls(spec, config),
                groups=[],  # Gruppen leben ausschliesslich an der Wurzel.
                inherit=True,
                priority_groups=list(spec.priority),
                listen_to=[target.strip("/") for target in spec.listen_to],
            )
        )
        for child in spec.children:
            walk(child, spec.path)

    for top in config.channels:
        walk(top, None)

    if config.server.welcometext is not None:
        state.conf["welcometext"] = config.server.welcometext
    state.conf.update(config.server.conf)
    state.default_channel_path = config.server.defaultchannel

    return state
