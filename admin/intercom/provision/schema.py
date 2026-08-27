"""Schema und Validierung fuer ``intercom.yaml``.

Die YAML ist der Wunschzustand. Dieses Modul liest sie ein, prueft sie und
liefert ein Objektmodell, mit dem Planer, Anwender und Exporter arbeiten.

Bewusst ohne pydantic: die Fehlermeldungen sollen auf Deutsch und mit dem
konkreten Pfad in der Datei erscheinen ("channels[1].children[0].speak: Gruppe
'kamer' ist nicht in groups: definiert -- meintest du 'kamera'?"), und genau
solche Meldungen sind mit einem generischen Validator umstaendlicher als mit
zwanzig Zeilen eigenem Code.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml

from ..ice.permissions import BY_NAME, names_to_mask

__all__ = [
    "IntercomConfig",
    "ChannelSpec",
    "ServerSpec",
    "PolicySpec",
    "TemplateEntry",
    "NetworkSpec",
    "Issue",
    "ConfigInvalid",
    "load_config",
    "parse_config",
    "PREDEFINED_GROUPS",
    "ROOT_ONLY_POLICIES",
    "POLICY_PERMISSIONS",
]

#: Von murmur fest eingebaute Gruppen. Duerfen in speak/whisper_in/... benutzt,
#: aber nicht in groups: neu definiert werden.
#: Bedeutung (aus src/Group.cpp, Group::appliesToUser):
#:   all   jeder            auth  angemeldet (registriert)
#:   in    im Kanal         out   nicht im Kanal
#:   sub   in einem Unterkanal des ACL-Kanals
#:   ~sub  wie sub, aber bezogen auf den ACL-Kanal statt den Zielkanal
#:   admin die Admin-Gruppe
PREDEFINED_GROUPS: frozenset[str] = frozenset(
    {"all", "auth", "in", "out", "sub", "~sub", "admin"}
)

#: Richtlinie -> Recht. Diese Rechte wertet murmur ausschliesslich am
#: Wurzelkanal aus (src/ACL.cpp: ``if (ch->iId == 0 && applyFromSelf)``).
ROOT_ONLY_POLICIES: dict[str, str] = {
    "kick": "Kick",
    "ban": "Ban",
    "register_users": "Register",
}

#: Richtlinie -> Recht fuer alles, was im ganzen Baum gilt.
POLICY_PERMISSIONS: dict[str, str] = {
    "whisper_anywhere": "Whisper",
    "move_users": "Move",
    "mute_deafen": "MuteDeafen",
    "make_channel": "MakeChannel",
    **ROOT_ONLY_POLICIES,
}


Level = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Issue:
    """Ein Befund aus der Validierung."""

    level: Level
    path: str
    message: str

    def __str__(self) -> str:
        marker = {"error": "FEHLER", "warning": "WARNUNG", "info": "HINWEIS"}[self.level]
        return f"{marker}  {self.path}: {self.message}"


class ConfigInvalid(ValueError):
    """Die YAML ist unbrauchbar. Traegt alle Befunde, nicht nur den ersten."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        errors = [i for i in issues if i.level == "error"]
        super().__init__(
            f"{len(errors)} Fehler in der Konfiguration:\n"
            + "\n".join(str(i) for i in issues)
        )


def _suggest(name: str, options: Iterator[str] | list[str]) -> str:
    """Tippfehler-Hilfe: ' -- meintest du "kamera"?'"""
    matches = difflib.get_close_matches(name, list(options), n=1, cutoff=0.6)
    return f' -- meintest du "{matches[0]}"?' if matches else ""


@dataclass(slots=True)
class TemplateEntry:
    """Ein ACL-Eintrag aus ``acl_templates``."""

    group: str = ""
    userid: int = -1
    apply_here: bool = True
    apply_sub: bool = False
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @property
    def allow_mask(self) -> int:
        return names_to_mask(self.allow)

    @property
    def deny_mask(self) -> int:
        return names_to_mask(self.deny)


@dataclass(slots=True)
class ServerSpec:
    defaultchannel: str | None = None
    welcometext: str | None = None
    #: Weitere Schluessel werden unveraendert per setConf gesetzt.
    conf: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PolicySpec:
    priority_speaker: list[str] = field(default_factory=list)
    whisper_anywhere: list[str] = field(default_factory=list)
    move_users: list[str] = field(default_factory=list)
    mute_deafen: list[str] = field(default_factory=list)
    kick: list[str] = field(default_factory=list)
    ban: list[str] = field(default_factory=list)
    make_channel: list[str] = field(default_factory=list)
    register_users: list[str] = field(default_factory=list)
    guests_listen_only: bool = False

    def groups_for(self, name: str) -> list[str]:
        value = getattr(self, name, [])
        return value if isinstance(value, list) else []


@dataclass(slots=True)
class ChannelSpec:
    """Ein Kanal im Wunschzustand."""

    name: str
    description: str = ""
    position: int = 0
    temporary: bool = False
    acl_template: str | None = None
    #: Rohe ACL-Eintraege, gleiche Form wie in acl_templates. Werden nach der
    #: Vorlage und vor den aus speak/whisper_in/listen_for abgeleiteten
    #: Eintraegen angewendet. Der Exporter benutzt sie, um ein von Hand
    #: geklicktes Setup verlustfrei abzubilden.
    acl: list["TemplateEntry"] = field(default_factory=list)
    speak: list[str] = field(default_factory=list)
    whisper_in: list[str] = field(default_factory=list)
    listen_for: list[str] = field(default_factory=list)
    listen_to: list[str] = field(default_factory=list)
    priority: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    children: list["ChannelSpec"] = field(default_factory=list)
    #: Nicht ueber Ice setzbar, siehe validate(). Nur zur Dokumentation.
    max_users: int = 0
    #: Vollstaendiger Pfad, beim Einlesen gesetzt ("Intercom/Kameras/Kamera 1").
    path: str = ""

    def walk(self) -> Iterator["ChannelSpec"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(slots=True)
class NetworkSpec:
    name: str
    cidr: str
    note: str = ""


@dataclass(slots=True)
class IntercomConfig:
    """Der komplette Wunschzustand."""

    version: int = 1
    server: ServerSpec = field(default_factory=ServerSpec)
    groups: list[str] = field(default_factory=list)
    acl_templates: dict[str, list[TemplateEntry]] = field(default_factory=dict)
    channels: list[ChannelSpec] = field(default_factory=list)
    policies: PolicySpec = field(default_factory=PolicySpec)
    users: dict[str, list[str]] = field(default_factory=dict)
    #: Optionaler Soll-Kanal je Nutzer (Pfad). Nur fuer die Alarmleiste im
    #: Cockpit -- der Provisioner schiebt niemanden von sich aus herum.
    user_channels: dict[str, str] = field(default_factory=dict)
    networks: list[NetworkSpec] = field(default_factory=list)
    devices: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Befunde aus der Validierung, auch die nicht-toedlichen.
    issues: list[Issue] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    @property
    def top_channel(self) -> ChannelSpec | None:
        """Der oberste konfigurierte Kanal (in der Beispieldatei ``Intercom``)."""
        return self.channels[0] if self.channels else None

    def all_channels(self) -> Iterator[ChannelSpec]:
        for channel in self.channels:
            yield from channel.walk()

    def channel_by_path(self, path: str) -> ChannelSpec | None:
        needle = path.strip().strip("/")
        for channel in self.all_channels():
            if channel.path == needle:
                return channel
        return None

    def known_groups(self) -> set[str]:
        return set(self.groups) | set(PREDEFINED_GROUPS)

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]


# --------------------------------------------------------------------------- #
#  Einlesen
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"Liste erwartet, {type(value).__name__} bekommen")


def _parse_acl_entries(
    raw_entries: Any, issues: list[Issue], where: str
) -> list[TemplateEntry]:
    """Liest eine Liste roher ACL-Eintraege (acl_templates und channels[].acl)."""
    parsed: list[TemplateEntry] = []
    for index, raw_entry in enumerate(raw_entries or []):
        spot = f"{where}[{index}]"
        if not isinstance(raw_entry, dict):
            issues.append(Issue("error", spot, "Abbildung erwartet."))
            continue
        entry = TemplateEntry(
            group=str(raw_entry.get("group", "") or ""),
            userid=int(raw_entry.get("userid", -1)),
            apply_here=bool(raw_entry.get("apply_here", True)),
            apply_sub=bool(raw_entry.get("apply_sub", False)),
            allow=_as_list(raw_entry.get("allow")),
            deny=_as_list(raw_entry.get("deny")),
        )
        for kind, names in (("allow", entry.allow), ("deny", entry.deny)):
            for permission in names:
                if permission not in BY_NAME:
                    issues.append(
                        Issue(
                            "error",
                            f"{spot}.{kind}",
                            f"Unbekanntes Recht {permission!r}"
                            + _suggest(permission, BY_NAME)
                            + ". Erlaubt: "
                            + ", ".join(BY_NAME),
                        )
                    )
        if not entry.apply_here and not entry.apply_sub:
            issues.append(
                Issue(
                    "error",
                    spot,
                    "apply_here und apply_sub sind beide false -- der Eintrag "
                    "wirkt nirgends. murmur behaelt solche Leichen, sie "
                    "verwirren nur.",
                )
            )
        if not entry.group and entry.userid < 0:
            issues.append(Issue("error", spot, "Entweder 'group:' oder 'userid:' angeben."))
        parsed.append(entry)
    return parsed


def _parse_channel(raw: Any, parent_path: str, issues: list[Issue], where: str) -> ChannelSpec:
    if not isinstance(raw, dict):
        issues.append(Issue("error", where, "Kanal muss eine Abbildung mit 'name:' sein."))
        return ChannelSpec(name="?", path=parent_path)

    name = str(raw.get("name", "")).strip()
    if not name:
        issues.append(Issue("error", where, "Kanal ohne 'name:'."))
        name = "?"
    if "/" in name:
        issues.append(
            Issue(
                "error",
                f"{where}.name",
                f"Kanalname {name!r} enthaelt '/'. Das Zeichen trennt Pfade und "
                "darf im Namen nicht vorkommen.",
            )
        )

    path = f"{parent_path}/{name}" if parent_path else name

    spec = ChannelSpec(
        name=name,
        description=str(raw.get("description", "") or ""),
        position=int(raw.get("position", 0) or 0),
        temporary=bool(raw.get("temporary", False)),
        acl_template=raw.get("acl_template") or None,
        acl=_parse_acl_entries(raw.get("acl"), issues, f"{where}.acl"),
        speak=_as_list(raw.get("speak")),
        whisper_in=_as_list(raw.get("whisper_in")),
        listen_for=_as_list(raw.get("listen_for")),
        listen_to=_as_list(raw.get("listen_to")),
        priority=_as_list(raw.get("priority")),
        links=_as_list(raw.get("links")),
        max_users=int(raw.get("max_users", 0) or 0),
        path=path,
    )

    seen: dict[str, int] = {}
    for index, child_raw in enumerate(raw.get("children") or []):
        child = _parse_channel(child_raw, path, issues, f"{where}.children[{index}]")
        if child.name in seen:
            issues.append(
                Issue(
                    "error",
                    f"{where}.children[{index}].name",
                    f"Es gibt bereits einen Kanal {child.name!r} unter {path!r}. "
                    "murmur laesst keine zwei gleichnamigen Geschwisterkanaele zu.",
                )
            )
        seen[child.name] = index
        spec.children.append(child)

    return spec


def parse_config(data: Any, source: str = "<speicher>") -> IntercomConfig:
    """Rohdaten -> :class:`IntercomConfig`, samt Validierung.

    Wirft :class:`ConfigInvalid`, sobald ein Befund die Stufe ``error`` hat.
    Warnungen bleiben in ``config.issues`` stehen.
    """
    issues: list[Issue] = []

    if data is None:
        raise ConfigInvalid([Issue("error", source, "Die Datei ist leer.")])
    if not isinstance(data, dict):
        raise ConfigInvalid(
            [Issue("error", source, "Auf oberster Ebene wird eine Abbildung erwartet.")]
        )

    config = IntercomConfig(version=int(data.get("version", 1) or 1))
    if config.version != 1:
        issues.append(
            Issue(
                "warning",
                "version",
                f"Schema-Version {config.version} ist unbekannt; es wird wie "
                "Version 1 gelesen.",
            )
        )

    # -- server ------------------------------------------------------------
    raw_server = data.get("server") or {}
    if not isinstance(raw_server, dict):
        issues.append(Issue("error", "server", "Abbildung erwartet."))
        raw_server = {}
    extra_conf = {
        str(k): str(v)
        for k, v in raw_server.items()
        if k not in {"defaultchannel", "welcometext"}
    }
    config.server = ServerSpec(
        defaultchannel=(str(raw_server["defaultchannel"]) if raw_server.get("defaultchannel") else None),
        welcometext=(str(raw_server["welcometext"]) if raw_server.get("welcometext") else None),
        conf=extra_conf,
    )

    # -- groups ------------------------------------------------------------
    config.groups = _as_list(data.get("groups"))
    seen_groups: set[str] = set()
    for index, group in enumerate(config.groups):
        if group in PREDEFINED_GROUPS:
            issues.append(
                Issue(
                    "error",
                    f"groups[{index}]",
                    f"{group!r} ist eine von murmur fest eingebaute Gruppe und darf "
                    "nicht neu definiert werden. Eingebaut sind: "
                    + ", ".join(sorted(PREDEFINED_GROUPS)),
                )
            )
        if group in seen_groups:
            issues.append(Issue("warning", f"groups[{index}]", f"{group!r} steht doppelt."))
        seen_groups.add(group)
        if not group.replace("-", "").replace("_", "").isalnum():
            issues.append(
                Issue(
                    "warning",
                    f"groups[{index}]",
                    f"{group!r} enthaelt Sonderzeichen. murmur deutet fuehrende "
                    "Zeichen '!', '~', '#' und '$' in ACLs besonders -- solche "
                    "Gruppennamen sind nicht erreichbar.",
                )
            )

    # -- acl_templates ------------------------------------------------------
    raw_templates = data.get("acl_templates") or {}
    if not isinstance(raw_templates, dict):
        issues.append(Issue("error", "acl_templates", "Abbildung erwartet."))
        raw_templates = {}
    for template_name, entries in raw_templates.items():
        config.acl_templates[str(template_name)] = _parse_acl_entries(
            entries, issues, f"acl_templates.{template_name}"
        )

    # -- channels -----------------------------------------------------------
    raw_channels = data.get("channels") or []
    if not isinstance(raw_channels, list):
        issues.append(Issue("error", "channels", "Liste erwartet."))
        raw_channels = []
    top_names: set[str] = set()
    for index, raw_channel in enumerate(raw_channels):
        channel = _parse_channel(raw_channel, "", issues, f"channels[{index}]")
        if channel.name in top_names:
            issues.append(
                Issue(
                    "error",
                    f"channels[{index}].name",
                    f"Kanal {channel.name!r} steht zweimal auf oberster Ebene.",
                )
            )
        top_names.add(channel.name)
        config.channels.append(channel)
    if not config.channels:
        issues.append(
            Issue("error", "channels", "Es ist kein einziger Kanal definiert.")
        )

    # -- policies -----------------------------------------------------------
    raw_policies = data.get("policies") or {}
    if not isinstance(raw_policies, dict):
        issues.append(Issue("error", "policies", "Abbildung erwartet."))
        raw_policies = {}
    known_policies = {f.name for f in PolicySpec.__dataclass_fields__.values()}
    for key in raw_policies:
        if key not in known_policies:
            issues.append(
                Issue(
                    "error",
                    f"policies.{key}",
                    f"Unbekannte Richtlinie{_suggest(str(key), known_policies)}. "
                    "Bekannt: " + ", ".join(sorted(known_policies)),
                )
            )
    config.policies = PolicySpec(
        priority_speaker=_as_list(raw_policies.get("priority_speaker")),
        whisper_anywhere=_as_list(raw_policies.get("whisper_anywhere")),
        move_users=_as_list(raw_policies.get("move_users")),
        mute_deafen=_as_list(raw_policies.get("mute_deafen")),
        kick=_as_list(raw_policies.get("kick")),
        ban=_as_list(raw_policies.get("ban")),
        make_channel=_as_list(raw_policies.get("make_channel")),
        register_users=_as_list(raw_policies.get("register_users")),
        guests_listen_only=bool(raw_policies.get("guests_listen_only", False)),
    )

    # -- users --------------------------------------------------------------
    raw_users = data.get("users") or {}
    if not isinstance(raw_users, dict):
        issues.append(Issue("error", "users", "Abbildung erwartet."))
        raw_users = {}
    for user_name, raw_user in raw_users.items():
        if isinstance(raw_user, dict):
            config.users[str(user_name)] = _as_list(raw_user.get("groups"))
            if raw_user.get("channel"):
                config.user_channels[str(user_name)] = str(raw_user["channel"]).strip("/")
        elif isinstance(raw_user, list):
            config.users[str(user_name)] = [str(g) for g in raw_user]
        else:
            issues.append(
                Issue(
                    "error",
                    f"users.{user_name}",
                    "Erwartet wird '{ groups: [...] }' oder direkt eine Gruppenliste.",
                )
            )

    # -- networks / devices --------------------------------------------------
    for index, raw_network in enumerate(data.get("networks") or []):
        if not isinstance(raw_network, dict) or not raw_network.get("cidr"):
            issues.append(
                Issue("error", f"networks[{index}]", "'name:' und 'cidr:' noetig.")
            )
            continue
        import ipaddress

        cidr = str(raw_network["cidr"])
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            issues.append(Issue("error", f"networks[{index}].cidr", f"{cidr!r}: {exc}"))
            continue
        config.networks.append(
            NetworkSpec(
                name=str(raw_network.get("name", cidr)),
                cidr=cidr,
                note=str(raw_network.get("note", "") or ""),
            )
        )

    raw_devices = data.get("devices") or {}
    if isinstance(raw_devices, dict):
        config.devices = {
            str(k): {str(ik): str(iv) for ik, iv in (v or {}).items()}
            for k, v in raw_devices.items()
        }

    _validate_references(config, issues)

    config.issues = issues
    if any(i.level == "error" for i in issues):
        raise ConfigInvalid(issues)
    return config


def _validate_references(config: IntercomConfig, issues: list[Issue]) -> None:
    """Querbezuege pruefen: Gruppen, Vorlagen, Kanalpfade."""
    known_groups = config.known_groups()
    all_paths = {c.path for c in config.all_channels()}

    def check_groups(names: list[str], where: str) -> None:
        for name in names:
            bare = name.lstrip("!~#$")
            if name.startswith(("#", "$")):
                # Zugriffstoken bzw. Zertifikatshash -- keine Gruppe.
                continue
            if bare not in known_groups:
                issues.append(
                    Issue(
                        "error",
                        where,
                        f"Gruppe {bare!r} ist nicht in groups: definiert"
                        + _suggest(bare, known_groups)
                        + ".",
                    )
                )

    for channel in config.all_channels():
        where = f"channels[{channel.path}]"
        check_groups(channel.speak, f"{where}.speak")
        check_groups(channel.whisper_in, f"{where}.whisper_in")
        check_groups(channel.listen_for, f"{where}.listen_for")
        check_groups(channel.priority, f"{where}.priority")

        if channel.acl_template and channel.acl_template not in config.acl_templates:
            issues.append(
                Issue(
                    "error",
                    f"{where}.acl_template",
                    f"Vorlage {channel.acl_template!r} ist nicht in acl_templates: "
                    "definiert" + _suggest(channel.acl_template, config.acl_templates) + ".",
                )
            )

        for target in channel.listen_to:
            if target.strip("/") not in all_paths:
                issues.append(
                    Issue(
                        "error",
                        f"{where}.listen_to",
                        f"Kanal {target!r} gibt es nicht"
                        + _suggest(target, all_paths)
                        + ".",
                    )
                )
            elif target.strip("/") == channel.path:
                issues.append(
                    Issue(
                        "warning",
                        f"{where}.listen_to",
                        "Der Kanal hoert sich selbst -- das tut nichts.",
                    )
                )

        for target in channel.links:
            if target.strip("/") not in all_paths:
                issues.append(
                    Issue(
                        "error",
                        f"{where}.links",
                        f"Kanal {target!r} gibt es nicht" + _suggest(target, all_paths) + ".",
                    )
                )

        if channel.max_users:
            issues.append(
                Issue(
                    "warning",
                    f"{where}.max_users",
                    "max_users laesst sich nicht provisionieren: das Channel-Struct "
                    "der Slice kennt kein solches Feld, und murmur bietet ueber Ice "
                    "keinen anderen Weg. Der Wert wird ignoriert; im Client laesst "
                    "er sich von Hand setzen.",
                )
            )

        if channel.temporary:
            issues.append(
                Issue(
                    "warning",
                    f"{where}.temporary",
                    "Temporaere Kanaele lassen sich ueber Ice nicht anlegen -- "
                    "addChannel erzeugt immer einen dauerhaften Kanal, und "
                    "Channel.temporary ist beim Schreiben wirkungslos. Der Kanal "
                    "wird dauerhaft angelegt.",
                )
            )

    for policy_name in ("priority_speaker", "whisper_anywhere", "move_users",
                        "mute_deafen", "kick", "ban", "make_channel", "register_users"):
        check_groups(config.policies.groups_for(policy_name), f"policies.{policy_name}")

    for user_name, groups in config.users.items():
        for group in groups:
            if group in PREDEFINED_GROUPS:
                issues.append(
                    Issue(
                        "error",
                        f"users.{user_name}.groups",
                        f"{group!r} ist eine eingebaute Gruppe; murmur verwaltet ihre "
                        "Mitgliedschaft selbst und ignoriert Eintraege.",
                    )
                )
            elif group not in known_groups:
                issues.append(
                    Issue(
                        "error",
                        f"users.{user_name}.groups",
                        f"Gruppe {group!r} ist nicht in groups: definiert"
                        + _suggest(group, known_groups)
                        + ".",
                    )
                )

    for user_name, wanted_channel in config.user_channels.items():
        if wanted_channel not in all_paths:
            issues.append(
                Issue(
                    "error",
                    f"users.{user_name}.channel",
                    f"Kanal {wanted_channel!r} gibt es nicht"
                    + _suggest(wanted_channel, all_paths)
                    + ".",
                )
            )

    if config.server.defaultchannel:
        target = config.server.defaultchannel.strip("/")
        if target not in all_paths:
            issues.append(
                Issue(
                    "error",
                    "server.defaultchannel",
                    f"Kanal {config.server.defaultchannel!r} gibt es nicht"
                    + _suggest(target, all_paths)
                    + ".",
                )
            )


def load_config(path: str | Path) -> IntercomConfig:
    """Liest und validiert ``intercom.yaml``."""
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigInvalid(
            [Issue("error", str(file_path), f"nicht lesbar: {exc}")]
        ) from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigInvalid(
            [Issue("error", str(file_path), f"kein gueltiges YAML: {exc}")]
        ) from exc
    return parse_config(data, source=str(file_path))
