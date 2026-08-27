"""Ist-Zustand -> ``intercom.yaml``.

Zweck: Sicherung, und die Uebernahme eines von Hand geklickten Servers in die
Konfigurationsdatei.

Lesbar wo moeglich, exakt wo noetig
-----------------------------------
Aus rohen ACL-Bitmasken laesst sich ``speak: [regie]`` nicht in jedem Fall
zurueckgewinnen -- eine handgeklickte ACL folgt keinem Muster. Der Exporter
geht deshalb so vor:

1. Er raet aus den ACLs eines Kanals ``speak`` / ``whisper_in`` / ``listen_for``.
2. Er laesst diese Vermutung durch **denselben** Generator laufen, den der
   Provisioner benutzt (``acl_map._channel_acls``).
3. Stimmt das Ergebnis Bit fuer Bit mit dem Ist-Zustand ueberein, schreibt er
   die lesbare Form. Sonst schreibt er die Eintraege roh unter ``acl:``.

Dadurch ist der Export **immer** verlustfrei: ``apply(export(server))`` fuehrt
zum selben Serverzustand. Fuer Konfigurationen, die aus dieser Datei stammen,
kommt zusaetzlich die lesbare Form heraus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from ..ice.permissions import mask_to_names
from ..ice.types import ACLEntry, MumbleChannel
from .acl_map import LISTEN, SPEAK, WHISPER, _channel_acls
from .schema import ChannelSpec, IntercomConfig, PolicySpec, ServerSpec, TemplateEntry

if TYPE_CHECKING:
    from ..ice.client import IceClient

__all__ = ["export_state", "export_yaml"]


def _acl_to_dict(entry: ACLEntry) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if entry.is_group:
        data["group"] = entry.group
    else:
        data["userid"] = entry.userid
    data["apply_here"] = entry.apply_here
    data["apply_sub"] = entry.apply_subs
    data["allow"] = mask_to_names(entry.allow)
    data["deny"] = mask_to_names(entry.deny)
    return data


def _guess_rules(entries: list[ACLEntry]) -> tuple[list[str], list[str], list[str]]:
    """Raet ``speak`` / ``whisper_in`` / ``listen_for`` aus den ACLs."""
    all_entry = next(
        (e for e in entries if e.group == "all" and e.apply_here and not e.apply_subs),
        None,
    )

    def collect(bit: int) -> list[str]:
        if all_entry is not None and all_entry.allow & bit:
            return ["all"]
        if all_entry is None or not (all_entry.deny & bit):
            return []
        return sorted(
            e.group
            for e in entries
            if e.is_group and e.group != "all" and e.allow & bit
        )

    return collect(SPEAK), collect(WHISPER), collect(LISTEN)


def _acl_signature(entries: list[ACLEntry]) -> list[tuple[str, int, bool, bool, int, int]]:
    return [
        (e.group, e.userid, e.apply_here, e.apply_subs, e.allow, e.deny)
        for e in entries
    ]


def _channel_to_spec(
    channel: MumbleChannel,
    path: str,
    own_acls: list[ACLEntry],
    config_stub: IntercomConfig,
) -> ChannelSpec:
    """Baut die YAML-Darstellung eines Kanals -- lesbar, wenn es aufgeht.

    Zwei Anlaeufe, bevor roh geschrieben wird:

    1. Nur ``speak`` / ``whisper_in`` / ``listen_for``.
    2. Dazu ein ``acl:``-Eintrag fuer die Bits, die ``@all`` zusaetzlich
       erlaubt bekommen hat. Die stammen typischerweise aus einer Vorlage wie
       ``nur-hoeren`` (Traverse, Enter) und lassen sich aus den drei Regeln
       allein nicht herleiten.
    """
    speak, whisper_in, listen_for = _guess_rules(own_acls)
    target = _acl_signature(own_acls)

    def baue(
        acl: list[TemplateEntry] | None = None,
        *,
        mit_regeln: bool = True,
    ) -> ChannelSpec:
        return ChannelSpec(
            name=channel.name,
            description=channel.description,
            position=channel.position,
            path=path,
            acl=list(acl or []),
            speak=list(speak) if mit_regeln else [],
            whisper_in=list(whisper_in) if mit_regeln else [],
            listen_for=list(listen_for) if mit_regeln else [],
        )

    def attempt(extra: list[TemplateEntry]) -> ChannelSpec | None:
        candidate = baue(extra)
        if _acl_signature(_channel_acls(candidate, config_stub)) == target:
            return candidate
        return None

    match = attempt([])

    if match is None:
        # Zweiter Anlauf: die Restbits von @all als expliziten Eintrag.
        derived = _channel_acls(baue(), config_stub)
        derived_all = next(
            (e for e in derived if e.group == "all" and e.apply_here and not e.apply_subs),
            None,
        )
        actual_all = next(
            (e for e in own_acls if e.group == "all" and e.apply_here and not e.apply_subs),
            None,
        )
        if actual_all is not None:
            residual_allow = actual_all.allow & ~(derived_all.allow if derived_all else 0)
            residual_deny = actual_all.deny & ~(derived_all.deny if derived_all else 0)
            if residual_allow or residual_deny:
                match = attempt(
                    [
                        TemplateEntry(
                            group="all",
                            apply_here=True,
                            apply_sub=False,
                            allow=mask_to_names(residual_allow),
                            deny=mask_to_names(residual_deny),
                        )
                    ]
                )

    if match is not None:
        return match

    # Nicht rekonstruierbar -- roh schreiben, damit nichts verloren geht.
    spec = baue(mit_regeln=False)
    spec.acl = [
        TemplateEntry(
            group=entry.group,
            userid=entry.userid,
            apply_here=entry.apply_here,
            apply_sub=entry.apply_subs,
            allow=mask_to_names(entry.allow),
            deny=mask_to_names(entry.deny),
        )
        for entry in own_acls
    ]
    return spec


def export_state(
    client: IceClient, *, roots: list[str] | None = None
) -> dict[str, Any]:
    """Liest den Server und baut daraus die YAML-Struktur.

    ``roots`` begrenzt den Export auf bestimmte Kanaele der obersten Ebene
    (Vorgabe: alle direkten Unterkanaele der Wurzel).
    """
    from .planner import Reconciler

    channels = client.get_channels()
    paths = Reconciler._build_paths(channels)
    id_to_path = {cid: path for path, cid in paths.items()}

    registered = client.get_registered_users()
    root_acl = client.get_acl(0)

    # -- Gruppen und Mitglieder ------------------------------------------
    group_names: list[str] = []
    user_groups: dict[str, list[str]] = {}
    for group in root_acl.own_groups():
        group_names.append(group.name)
        for userid in group.add:
            name = registered.get(userid)
            if name:
                user_groups.setdefault(name, []).append(group.name)

    # -- Richtlinien aus den Wurzel-ACLs zurueckgewinnen -------------------
    policies = PolicySpec()
    from ..ice.permissions import BY_NAME
    from .schema import POLICY_PERMISSIONS

    for policy_name, permission in POLICY_PERMISSIONS.items():
        bit = BY_NAME[permission].bit
        members = sorted(
            {
                entry.group
                for entry in root_acl.own_acls()
                if entry.is_group and entry.group not in {"all", "admin"} and entry.allow & bit
            }
        )
        setattr(policies, policy_name, members)
    policies.guests_listen_only = any(
        entry.group == "all" and entry.apply_subs and (entry.deny & SPEAK)
        for entry in root_acl.own_acls()
    )
    # priority_speaker ist kein ACL, sondern Nutzerzustand -- aus den ACLs also
    # nicht rekonstruierbar. Siehe README, Abschnitt "Bekannte Grenzen".

    # -- Kanalbaum ---------------------------------------------------------
    stub = IntercomConfig(groups=group_names)
    top_level = (
        roots
        if roots is not None
        else sorted(
            id_to_path[cid] for cid, c in channels.items() if c.parent == 0 and cid != 0
        )
    )

    def build(path: str) -> dict[str, Any] | None:
        channel_id = paths.get(path)
        if channel_id is None:
            return None
        channel = channels[channel_id]
        acl = client.get_acl(channel_id)
        spec = _channel_to_spec(channel, path, acl.own_acls(), stub)

        node: dict[str, Any] = {"name": spec.name}
        if spec.description:
            node["description"] = spec.description
        if spec.position:
            node["position"] = spec.position
        if spec.acl:
            node["acl"] = [
                {
                    **({"group": e.group} if e.group else {"userid": e.userid}),
                    "apply_here": e.apply_here,
                    "apply_sub": e.apply_sub,
                    "allow": e.allow,
                    "deny": e.deny,
                }
                for e in spec.acl
            ]
        for key_name, value in (
            ("speak", spec.speak),
            ("whisper_in", spec.whisper_in),
            ("listen_for", spec.listen_for),
        ):
            if value:
                node[key_name] = value
        if channel.links:
            node["links"] = sorted(
                id_to_path[link] for link in channel.links if link in id_to_path
            )

        children = sorted(
            (id_to_path[cid] for cid, c in channels.items() if c.parent == channel_id),
            key=lambda p: (channels[paths[p]].position, p),
        )
        child_nodes = [build(child) for child in children]
        child_nodes = [c for c in child_nodes if c]
        if child_nodes:
            node["children"] = child_nodes
        return node

    channel_nodes = [node for node in (build(path) for path in top_level) if node]

    # -- Serverkonfiguration ----------------------------------------------
    conf = client.get_all_conf()
    server = ServerSpec(welcometext=conf.get("welcometext") or None)
    default_channel = conf.get("defaultchannel")
    if default_channel and default_channel.isdigit():
        server.defaultchannel = id_to_path.get(int(default_channel)) or None

    document: dict[str, Any] = {"version": 1}
    server_node: dict[str, Any] = {}
    if server.defaultchannel:
        server_node["defaultchannel"] = server.defaultchannel
    if server.welcometext:
        server_node["welcometext"] = server.welcometext
    if server_node:
        document["server"] = server_node
    if group_names:
        document["groups"] = group_names
    document["channels"] = channel_nodes

    policy_node = {
        name: getattr(policies, name)
        for name in POLICY_PERMISSIONS
        if getattr(policies, name)
    }
    if policies.guests_listen_only:
        policy_node["guests_listen_only"] = True
    if policy_node:
        document["policies"] = policy_node

    if user_groups:
        document["users"] = {
            name: {"groups": sorted(groups)} for name, groups in sorted(user_groups.items())
        }

    return document


def export_yaml(client: IceClient, *, roots: list[str] | None = None) -> str:
    """Wie :func:`export_state`, aber gleich als YAML-Text."""
    document = export_state(client, roots=roots)
    header = (
        "# Aus dem laufenden Server exportiert.\n"
        "#\n"
        "# Hinweis: priority_speaker laesst sich nicht exportieren -- Priority\n"
        "# Speaker ist ein Nutzerzustand und kein ACL-Eintrag, der Server haelt\n"
        "# dafuer keine Sollvorgabe vor. Ebenso fehlen listen_to (Listener haengen\n"
        "# an der Sitzung), networks und devices (reine Dokumentation).\n"
        "# Diese Abschnitte aus der bisherigen intercom.yaml uebernehmen.\n"
    )
    body = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return header + body
