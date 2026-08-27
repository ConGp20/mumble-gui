"""Domaenenmodell -- entkoppelt den Rest der Anwendung von Ice.

Alles ausserhalb von ``intercom.ice`` arbeitet mit diesen Dataclasses, nie mit
den von ``slice2py`` erzeugten Typen. Das hat drei Gruende:

1. Die generierten Typen lassen sich nicht ohne Ice-Laufzeit importieren. Tests
   fuer Provisioner-Logik und ACL-Abbildung laufen so ohne Ice.
2. Die Slice aendert sich zwischen Mumble-Versionen. Die Umrechnung liegt an
   einer Stelle (``from_ice``), nicht ueber die ganze Anwendung verstreut.
3. Die Ice-Typen sind fuer JSON unbrauchbar (``NetAddress`` ist ein 16-Byte-
   Tupel, ``version`` eine gepackte Ganzzahl).
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

__all__ = [
    "decode_address",
    "encode_address",
    "decode_version",
    "ServerVersion",
    "MumbleUser",
    "MumbleChannel",
    "ACLEntry",
    "ChannelGroup",
    "ChannelACL",
    "BanEntry",
    "LogEntry",
    "RegisteredUser",
]


# --------------------------------------------------------------------------- #
#  Hilfsfunktionen
# --------------------------------------------------------------------------- #


def decode_address(raw: Iterable[int] | None) -> str:
    """``NetAddress`` (16 Byte, IPv6-Darstellung) -> lesbare IP.

    murmur legt auch IPv4-Adressen als IPv4-mapped IPv6 ab
    (``::ffff:10.20.30.40``). Fuer das Cockpit wollen wir in dem Fall die
    IPv4-Schreibweise sehen, sonst ist die Netzsicht unlesbar.

    Die Slice markiert den Typ mit ``["python:seq:tuple"]``, wir bekommen also
    ein ``tuple[int, ...]``. Bei obfuscate=true liefert murmur Nullen -- daraus
    wird ``"(anonymisiert)"``.
    """
    if not raw:
        return ""
    octets = bytes(bytearray(raw))
    if len(octets) != 16:
        # Sollte nicht vorkommen; lieber sichtbar falsch als stillschweigend leer.
        return f"<{len(octets)} Byte statt 16>"
    if octets == b"\x00" * 16:
        return "(anonymisiert)"
    address = ipaddress.IPv6Address(octets)
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    return str(address)


def encode_address(text: str) -> tuple[int, ...]:
    """Lesbare IP -> ``NetAddress``. Gegenstueck zu :func:`decode_address`.

    Wird fuer Bans gebraucht, die murmur ebenfalls als 16-Byte-Adresse erwartet.
    """
    if not text or text == "(anonymisiert)":
        return tuple([0] * 16)
    parsed = ipaddress.ip_address(text.strip())
    if isinstance(parsed, ipaddress.IPv4Address):
        parsed = ipaddress.IPv6Address("::ffff:" + str(parsed))
    return tuple(parsed.packed)


def decode_version(legacy: int, modern: int = 0) -> str:
    """Mumble-Client-Version -> ``"1.5.735"``.

    Mumble hat zwei Kodierungen, siehe mumble-voip/mumble#5827:

    * ``version``  (int, alt):  ``0xAABBCC``  -> je ein Byte fuer major/minor/patch.
      Damit sind nur Patchlevel bis 255 darstellbar -- 1.4.287 passt nicht mehr.
    * ``version2`` (long, neu): ``0xAAAABBBBCCCC`` -> je zwei Byte.

    Wir bevorzugen ``version2``, wenn der Client sie mitschickt. Aeltere Clients
    (und Mumla in aelteren Fassungen) senden nur ``version``.
    """
    if modern:
        major = (modern >> 32) & 0xFFFF
        minor = (modern >> 16) & 0xFFFF
        patch = modern & 0xFFFF
        return f"{major}.{minor}.{patch}"
    if legacy:
        major = (legacy >> 16) & 0xFF
        minor = (legacy >> 8) & 0xFF
        patch = legacy & 0xFF
        return f"{major}.{minor}.{patch}"
    return ""


# --------------------------------------------------------------------------- #
#  Modelle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ServerVersion:
    """Ergebnis von ``Meta.getVersion``."""

    major: int
    minor: int
    patch: int
    text: str

    @property
    def short(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def matches_tag(self, tag: str) -> bool:
        """Passt die laufende Version zum Compose-Tag (``v1.5.735``)?"""
        wanted = tag.strip().lstrip("vV")
        if not wanted:
            return True
        parts = wanted.split(".")
        try:
            numbers = [int(p) for p in parts[:3]]
        except ValueError:
            return False
        actual = [self.major, self.minor, self.patch][: len(numbers)]
        return actual == numbers


@dataclass(slots=True)
class MumbleUser:
    """Ein verbundener Client. Spiegelt das ``User``-Struct vollstaendig.

    Alle Felder werden im Cockpit angezeigt -- auch die selten interessanten,
    weil der Betreiber ausdruecklich alles sehen will.
    """

    session: int
    userid: int
    name: str
    channel: int
    mute: bool = False
    deaf: bool = False
    suppress: bool = False
    self_mute: bool = False
    self_deaf: bool = False
    priority_speaker: bool = False
    recording: bool = False
    online_secs: int = 0
    idle_secs: int = 0
    bytes_per_sec: int = 0
    version_raw: int = 0
    version2_raw: int = 0
    release: str = ""
    os: str = ""
    os_version: str = ""
    identity: str = ""
    context: str = ""
    comment: str = ""
    address: str = ""
    tcp_only: bool = False
    udp_ping: float = 0.0
    tcp_ping: float = 0.0

    @property
    def registered(self) -> bool:
        """``userid == -1`` heisst: nicht registriert, nur angemeldet."""
        return self.userid >= 0

    @property
    def version(self) -> str:
        return decode_version(self.version_raw, self.version2_raw)

    @property
    def ping(self) -> float:
        """Der aussagekraeftigere Ping.

        UDP ist die Zahl, die zaehlt: darueber laeuft die Sprache. Ist der
        Client auf TCP zurueckgefallen (Firewall, kaputtes NAT), gibt es keinen
        UDP-Ping und wir zeigen den TCP-Wert -- der Client ist dann ohnehin ein
        Alarmfall.
        """
        return self.udp_ping if self.udp_ping > 0 else self.tcp_ping

    @classmethod
    def from_ice(cls, u: Any) -> "MumbleUser":
        return cls(
            session=u.session,
            userid=u.userid,
            name=u.name,
            channel=u.channel,
            mute=u.mute,
            deaf=u.deaf,
            suppress=u.suppress,
            self_mute=u.selfMute,
            self_deaf=u.selfDeaf,
            priority_speaker=u.prioritySpeaker,
            recording=u.recording,
            online_secs=u.onlinesecs,
            idle_secs=u.idlesecs,
            bytes_per_sec=u.bytespersec,
            version_raw=u.version,
            version2_raw=getattr(u, "version2", 0) or 0,
            release=u.release,
            os=u.os,
            os_version=u.osversion,
            identity=u.identity,
            context=u.context,
            comment=u.comment,
            address=decode_address(u.address),
            tcp_only=u.tcponly,
            udp_ping=round(float(u.udpPing), 2),
            tcp_ping=round(float(u.tcpPing), 2),
        )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["registered"] = self.registered
        data["version"] = self.version
        data["ping"] = self.ping
        return data


@dataclass(slots=True)
class MumbleChannel:
    """Ein Kanal. Spiegelt das ``Channel``-Struct."""

    id: int
    name: str
    parent: int
    description: str = ""
    temporary: bool = False
    position: int = 0
    links: list[int] = field(default_factory=list)
    max_users: int = 0
    """Aus ``getChannelState`` nicht verfuegbar -- murmur legt das pro Kanal in
    der Konfiguration ab. Wird von :class:`~intercom.ice.client.IceClient`
    separat ueber ``getConf`` nachgeladen, siehe dort."""

    @property
    def is_root(self) -> bool:
        return self.id == 0

    @classmethod
    def from_ice(cls, c: Any) -> "MumbleChannel":
        return cls(
            id=c.id,
            name=c.name,
            parent=c.parent,
            description=c.description,
            temporary=c.temporary,
            position=c.position,
            links=list(c.links),
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ACLEntry:
    """Ein ACL-Eintrag eines Kanals.

    ``inherited`` ist read-only: murmur liefert geerbte Eintraege bei ``getACL``
    mit, nimmt sie bei ``setACL`` aber als *eigene* Eintraege des Kanals an.
    Wer geerbte Eintraege zurueckschreibt, kopiert sie damit versehentlich in
    den Kanal. :func:`intercom.ice.client.IceClient.set_channel_acl` filtert sie
    deshalb heraus.
    """

    apply_here: bool
    apply_subs: bool
    allow: int
    deny: int
    group: str = ""
    userid: int = -1
    inherited: bool = False

    @property
    def is_group(self) -> bool:
        return self.userid < 0 and bool(self.group)

    @property
    def dangling(self) -> bool:
        """Weder hier noch fuer Unterkanaele -- der Eintrag tut nichts.

        Der ACL-Editor darf so etwas nicht erzeugen; beim Import zeigen wir es
        als Warnung an, weil per Hand geklickte Setups das haeufig enthalten.
        """
        return not self.apply_here and not self.apply_subs

    @classmethod
    def from_ice(cls, a: Any) -> "ACLEntry":
        return cls(
            apply_here=a.applyHere,
            apply_subs=a.applySubs,
            allow=a.allow,
            deny=a.deny,
            group=a.group,
            userid=a.userid,
            inherited=a.inherited,
        )

    def key(self) -> tuple[str, int, bool, bool]:
        """Identitaet fuer den Vergleich Ist/Soll im Provisioner."""
        return (self.group, self.userid, self.apply_here, self.apply_subs)

    def to_json(self) -> dict[str, Any]:
        from .permissions import mask_to_names

        data = asdict(self)
        data["allow_names"] = mask_to_names(self.allow)
        data["deny_names"] = mask_to_names(self.deny)
        data["dangling"] = self.dangling
        return data


@dataclass(slots=True)
class ChannelGroup:
    """Eine Gruppe an einem Kanal.

    ``add`` und ``remove`` sind Listen **registrierter Nutzer-IDs**, nicht
    Session-IDs -- belegt durch ``impl_Server_setACL`` in MumbleServerIce.cpp,
    wo sie unveraendert in ``Group::qsAdd`` / ``qsRemove`` wandern, und diese
    Mengen werden gegen ``ServerUser::iId`` geprueft.

    ``members`` und ``inherited`` sind read-only und werden beim Schreiben
    ignoriert.
    """

    name: str
    inherit: bool = True
    inheritable: bool = True
    add: list[int] = field(default_factory=list)
    remove: list[int] = field(default_factory=list)
    members: list[int] = field(default_factory=list)
    inherited: bool = False

    @classmethod
    def from_ice(cls, g: Any) -> "ChannelGroup":
        return cls(
            name=g.name,
            inherit=g.inherit,
            inheritable=g.inheritable,
            add=sorted(g.add),
            remove=sorted(g.remove),
            members=sorted(g.members),
            inherited=g.inherited,
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChannelACL:
    """Vollstaendiges Ergebnis von ``getACL`` fuer einen Kanal."""

    channel_id: int
    acls: list[ACLEntry] = field(default_factory=list)
    groups: list[ChannelGroup] = field(default_factory=list)
    inherit: bool = True

    def own_acls(self) -> list[ACLEntry]:
        """Nur die Eintraege, die diesem Kanal wirklich gehoeren."""
        return [a for a in self.acls if not a.inherited]

    def own_groups(self) -> list[ChannelGroup]:
        return [g for g in self.groups if not g.inherited]

    def group(self, name: str) -> ChannelGroup | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "inherit": self.inherit,
            "acls": [a.to_json() for a in self.acls],
            "groups": [g.to_json() for g in self.groups],
        }


@dataclass(slots=True)
class BanEntry:
    """Ein Bann. ``address``/``bits`` bilden zusammen eine Netzmaske."""

    address: str
    bits: int
    name: str = ""
    hash: str = ""
    reason: str = ""
    start: int = 0
    duration: int = 0

    @property
    def permanent(self) -> bool:
        return self.duration <= 0

    @classmethod
    def from_ice(cls, b: Any) -> "BanEntry":
        return cls(
            address=decode_address(b.address),
            bits=b.bits,
            name=b.name,
            hash=b.hash,
            reason=b.reason,
            start=b.start,
            duration=b.duration,
        )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["permanent"] = self.permanent
        return data


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Eine Zeile aus ``getLog``."""

    timestamp: int
    text: str

    @classmethod
    def from_ice(cls, entry: Any) -> "LogEntry":
        return cls(timestamp=entry.timestamp, text=entry.txt)

    def to_json(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "text": self.text}


@dataclass(slots=True)
class RegisteredUser:
    """Ein in der Serverdatenbank registrierter Nutzer.

    Die Felder stammen aus der ``UserInfoMap`` von ``getRegistration``. Welche
    Schluessel gesetzt sind, haengt davon ab, wie der Nutzer angelegt wurde --
    ein per Zertifikat registrierter Nutzer hat ``hash`` aber kein Passwort.
    """

    userid: int
    name: str
    email: str = ""
    comment: str = ""
    hash: str = ""
    last_active: str = ""
    kdf_iterations: str = ""

    @property
    def short_hash(self) -> str:
        """Erste 12 Zeichen -- genug zum Wiedererkennen, kurz genug fuer eine Tabelle."""
        return self.hash[:12] if self.hash else ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["short_hash"] = self.short_hash
        return data
