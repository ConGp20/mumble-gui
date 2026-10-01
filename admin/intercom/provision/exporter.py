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

from collections.abc import Mapping
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


def _im_export(pfad: str, top_level: list[str]) -> bool:
    """Liegt ``pfad`` in einem der exportierten Teilbaeume?"""
    return any(pfad == wurzel or pfad.startswith(wurzel + "/") for wurzel in top_level)


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

    #: Was der Export nicht abbilden kann. Landet als Hinweis im Kopf des
    #: Dokuments -- nicht still weggelassen.
    ausgelassen: list[str] = []

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
        # Eigene Gruppen des Kanals. Ohne sie ginge beim Sichern verloren, was
        # jemand von Hand an einem Unterkanal angelegt hat -- die ACL-Eintraege
        # wuerden auf eine Gruppe zeigen, die es nach dem Wiedereinspielen nicht
        # mehr gibt. Mitglieder als NAMEN, weil Nutzer-IDs serverspezifisch sind.
        eigene_gruppen = [
            {
                "name": gruppe.name,
                **({"inherit": False} if not gruppe.inherit else {}),
                **({"inheritable": False} if not gruppe.inheritable else {}),
                "add": [registered[uid] for uid in gruppe.add if uid in registered],
                **(
                    {"remove": [registered[uid] for uid in gruppe.remove if uid in registered]}
                    if gruppe.remove
                    else {}
                ),
            }
            for gruppe in acl.own_groups()
        ]
        if eigene_gruppen:
            node["groups"] = eigene_gruppen

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
    # get_effective_conf, nicht get_all_conf: letzteres liefert nur die
    # Datenbank-Uebersteuerungen. Auf einem ueber die Compose eingerichteten
    # Server stehen welcometext und defaultchannel in der ini-Datei -- der
    # Export haette sie stillschweigend verloren.
    conf = client.get_effective_conf()
    server = ServerSpec(welcometext=conf.get("welcometext") or None)
    default_channel = conf.get("defaultchannel")
    if default_channel and default_channel.isdigit():
        pfad = id_to_path.get(int(default_channel))
        # Nur uebernehmen, wenn der Kanal auch im Export vorkommt. Bei einem
        # auf roots= eingeschraenkten Export kann der Vorgabekanal ausserhalb
        # liegen; dann verwiese die YAML auf einen Kanal, den sie selbst nicht
        # anlegt, und waere nicht wieder einlesbar (parse_config lehnt sie mit
        # "Kanal gibt es nicht" ab). Das ist kein stilles Weglassen -- der
        # Hinweis steht unten im Kopf des Dokuments.
        if pfad and _im_export(pfad, top_level):
            server.defaultchannel = pfad
        elif pfad:
            ausgelassen.append(
                f"server.defaultchannel verweist auf {pfad!r} -- der Kanal "
                "liegt ausserhalb des exportierten Teilbaums und fehlt daher."
            )

    document: dict[str, Any] = {"version": 1}
    if ausgelassen:
        # Unter einem Schluessel mit fuehrendem Unterstrich: parse_config
        # ignoriert unbekannte Schluessel, und export_yaml hebt den Inhalt in
        # den Kommentarkopf.
        document["_ausgelassen"] = ausgelassen
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


def _wunsch_mit_namen(
    client: IceClient, wunsch: Mapping[str, Mapping[int, list[str]]]
) -> dict[str, dict[str, list[str]]]:
    """Nutzer-IDs zu Namen aufloesen. Wer nicht mehr registriert ist, faellt weg."""
    namen = client.get_registered_users()
    ergebnis: dict[str, dict[str, list[str]]] = {}
    for art, je_person in wunsch.items():
        eintraege = {
            namen[userid]: list(pfade)
            for userid, pfade in sorted(je_person.items())
            if userid in namen and pfade
        }
        if eintraege:
            ergebnis[art] = eintraege
    return ergebnis


def export_yaml(
    client: IceClient,
    *,
    roots: list[str] | None = None,
    wunsch: Mapping[str, Mapping[int, list[str]]] | None = None,
    verbindungen: Mapping[str, Mapping[str, list[str]]] | None = None,
    netze: list[Mapping[str, Any]] | None = None,
    ruftasten: Mapping[str, Mapping[int, str]] | None = None,
) -> str:
    """Wie :func:`export_state`, aber gleich als YAML-Text.

    ``wunsch`` ist der Wunschzustand aus dem Store -- fester Platz, dauerhaftes
    Mithoeren, Vorrang. Er steht nicht am Server und ginge ohne diesen Abschnitt
    beim Einspielen verloren. Geschluesselt wird nach **Nutzernamen**, nicht nach
    ID: murmur vergibt IDs beim Wiederanlegen neu, und eine gespeicherte ID
    zeigte danach auf die falsche Person.
    """
    document = export_state(client, roots=roots)
    ausgelassen = document.pop("_ausgelassen", [])
    if wunsch:
        abschnitt = _wunsch_mit_namen(client, wunsch)
        if abschnitt:
            document["wunsch"] = abschnitt
    if verbindungen:
        # Verbindungen stehen ohnehin als Pfade -- hier ist nichts aufzuloesen.
        gefiltert = {
            art: {von: list(nach) for von, nach in je_platz.items() if nach}
            for art, je_platz in verbindungen.items()
            if je_platz
        }
        if gefiltert:
            document["verbindungen"] = gefiltert
    if ruftasten:
        # Der oberste Platz hat den leeren Pfad; in der Datei steht er als "/",
        # weil ein leerer Schluessel beim Lesen niemandem etwas sagt. Ein
        # Schraegstrich kann in keinem Platznamen vorkommen.
        belegt = {
            (pfad or "/"): {int(t): r for t, r in sorted(je_taste.items()) if r}
            for pfad, je_taste in sorted(ruftasten.items())
        }
        belegt = {pfad: tasten for pfad, tasten in belegt.items() if tasten}
        if belegt:
            document["ruftasten"] = belegt
    if netze:
        document["networks"] = [
            {
                "name": n["name"],
                "cidr": n["cidr"],
                **({"note": n["notiz"]} if n.get("notiz") else {}),
            }
            for n in netze
        ]
    header = (
        "# Aus dem laufenden Server exportiert.\n"
        "#\n"
        "# Hinweis: priority_speaker laesst sich nicht exportieren -- Priority\n"
        "# Speaker ist ein Nutzerzustand und kein ACL-Eintrag, der Server haelt\n"
        "# dafuer keine Sollvorgabe vor. Ebenso fehlen listen_to (Listener haengen\n"
        "# an der Sitzung) und devices (reine Dokumentation).\n"
    )
    if document.get("wunsch") or document.get("verbindungen") or document.get("ruftasten"):
        header += (
            "#\n"
            "# Diese Abschnitte kommen nicht vom Server, sondern aus der Oberflaeche:\n"
            "#   wunsch        fester Platz, dauerhaftes Mithoeren, Vorrang je Person\n"
            "#   verbindungen  Verbindungen zwischen zwei Plaetzen\n"
            "#   ruftasten     Belegung der Ruftasten je Platz ('/' = ueberall)\n"
            "# Mumble merkt sich nichts davon. Beim Einspielen werden sie uebernommen.\n"
        )
    for hinweis in ausgelassen:
        header += "#\n# Nicht uebernommen: " + hinweis + "\n"
    body = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return header + body
