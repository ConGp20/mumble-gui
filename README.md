# Stadion-Intercom – Administration

Admin-GUI, Provisionierung und Ueberwachung fuer ein Mumble-basiertes
PTT-Intercom im Leichtathletik-Stadion: Regie, Kameras, Zeitnahme (FinishLynx),
Stadionsprecher, Technik, Kampfgericht.

Kein Dashboard zum Anschauen, sondern ein Betriebs-Cockpit: jeder Client mit
IP, Ping, Paketverlust, Version und Zertifikat; jede ACL; jede Gruppe; und ein
Server, der sich aus einer YAML in den Wunschzustand bringt, statt dass jemand
dreissig Kanaele von Hand klickt.

---

## Inhalt

1. [Schnellstart](#schnellstart)
2. [Architektur](#architektur)
3. [Vertrag mit der Compose](#vertrag-mit-der-compose)
4. [Schema-Referenz `intercom.yaml`](#schema-referenz-intercomyaml)
5. [Kommandozeile](#kommandozeile)
6. [Bekannte Grenzen](#bekannte-grenzen)
7. [Fehlersuche](#fehlersuche)
8. [Entwicklung und Tests](#entwicklung-und-tests)

---

## Schnellstart

Zielsystem ist ein beliebiger Linux-Rechner mit Docker — Raspberry Pi,
Zima Board, Mini-PC, virtuelle Maschine. Gebaut **und gegen einen echten
mumble-server geprueft auf x86-64 und arm64**. Das Betriebssystem des Hosts
spielt keine Rolle (Ubuntu, Debian, Raspberry Pi OS): der Container bringt sein
eigenes mit.

### Docker aus der offiziellen Quelle

Bewusst **nicht** `apt install docker.io`. Nachgemessen auf Ubuntu 24.04 und
22.04 (Stand September 2026):

| | offizielle Quelle | Ubuntu-Paket |
|---|---|---|
| Engine | `docker-ce` 29.8.1 | `docker.io` 29.1.3 |
| Compose | `docker-compose-plugin` **5.5.1** | `docker-compose-v2` **2.40.3** |
| buildx | `docker-buildx-plugin` 0.37.1 | `docker-buildx` 0.30.1 |

Die Engine selbst ist nah dran — der Abstand bei Compose ist dagegen ein
Hauptversionssprung. Dazu kommt das Praktische: `apt install docker.io` allein
gibt weder `docker compose` noch `docker buildx`, beides sind getrennte Pakete,
die man einzeln kennen und nachziehen muss. Und Docker nennt `docker.io` in
seiner eigenen Anleitung unter den Paketen, die vor der Installation weg
muessen -- beides nebeneinander geht nicht.

Die folgenden Befehle sind nicht abgeschrieben, sondern in einem frischen
`ubuntu:24.04` durchlaufen: Ergebnis Docker 29.8.1, Compose v5.5.1,
buildx 0.37.1.

```bash
# Falls eine Distributionsfassung im Weg liegt (auf fertigen Pi-Abbildern oft):
sudo apt remove -y docker.io docker-compose docker-compose-v2 docker-doc \
                   podman-docker containerd runc

# Schluessel und Paketquelle von Docker eintragen:
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
     -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
                    docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker "$USER"     # danach ab- und wieder anmelden
```

> **Auf Raspberry Pi OS oder Debian** statt Ubuntu: in den beiden Zeilen mit
> `download.docker.com` das `ubuntu` durch `debian` ersetzen, und bei `Suites:`
> genuegt `$VERSION_CODENAME`. Sonst ist alles gleich.
>
> Die Paketquelle liefert `arm64` und `amd64` — derselbe Befehlssatz auf Pi wie
> auf dem Zima Board.

Zum Pruefen, bevor es weitergeht:

```bash
docker run --rm hello-world
docker compose version
```

### Einrichten

```bash
git clone <dieses-repo> ~/stadion-intercom
cd ~/stadion-intercom
./setup.sh
```

`setup.sh` prueft Docker, legt `./server` und `./admin-data` an, setzt sie auf
UID/GID 10000, ersetzt alle `ERSETZEN_*`-Werte in der `.env` durch
Zufallswerte, startet den Mumble-Server, wartet auf dessen Ice-Schnittstelle,
gleicht `MUMBLE_VERSION` an die tatsaechlich laufende Serverversion an, baut das
Admin-Image, zeigt den Provisioning-Plan und wendet ihn nach Bestaetigung an.

```bash
./setup.sh --check    # nur pruefen, nichts aendern
./setup.sh --yes      # ohne Rueckfragen
```

Danach liegt das GUI auf `http://<rechner>:8080/`. Benutzer und Passwort stehen in
der `.env` (`ADMIN_USER`, `ADMIN_PASSWORD`).

Nur das Image bauen, ohne `setup.sh`:

```bash
docker compose --profile gui build mumble-admin
# oder direkt:
docker build -t stadion-intercom/mumble-admin:v1.5.735 admin/
```

Gemessen ohne jeden Cache: **50 Sekunden auf x86-64**. Es wird nichts
uebersetzt, nur installiert — deshalb bleibt es auch auf schwacher Hardware in
derselben Groessenordnung. Das fertige Image meldet `docker images` mit rund
**330 MB**.

Es prueft sich selbst: schlaegt `import MumbleServer, Ice` oder
`import intercom.web.app` fehl, bricht der Bau ab, statt ein Image zu
hinterlassen, das erst im Stadion auffaellt. Hinter einem Proxy mit
TLS-Aufbruch: `admin/ca/README.md`.

**Auf einem Raspberry Pi** ist nichts weiter zu tun — `docker build` nimmt von
selbst die arm64-Fassung der Basisimages. Das ist nachgewiesen, nicht vermutet:
das arm64-Image wurde gebaut, gestartet und gegen denselben echten
mumble-server gefahren wie die x86-Fassung — `uname -m` meldet `aarch64`, Ice
3.7.8 mit 133 Pruefsummen, alle Seiten und Endpunkte antworten, der Monitor-Bot
haengt stumm und taub im Zielkanal.

> Der Nachweis lief unter QEMU-Emulation auf einem x86-Rechner. Er belegt, dass
> das Image auf arm64 **baut und laeuft** — nicht, wie schnell es auf echter
> Pi-Hardware ist. Die Antwortzeiten unter Emulation (Cockpit-Seite 0,22 s
> gegen 0,03 s nativ) sind Emulationskosten, kein Massstab fuer den Pi.

Wer auf einem x86-Rechner fuer den Pi bauen will (etwa um die SD-Karte zu
schonen):

```bash
docker build --platform linux/arm64 -t stadion-intercom/mumble-admin:v1.5.735 admin/
docker save stadion-intercom/mumble-admin:v1.5.735 | gzip > intercom-arm64.tgz
# auf dem Pi:
gunzip -c intercom-arm64.tgz | docker load
```

Dafuer muss auf dem bauenden Rechner die Emulation eingerichtet sein:
`docker run --privileged --rm tonistiigi/binfmt --install arm64`.

> **Das Cockpit laeuft unverschluesselt.** Passwort und Sitzungscookie gehen im
> Klartext ueber das Netz. Fuer ein abgeschlossenes Stadionnetz ist das
> vertretbar — aber nur dann. Haengt der Rechner mit einem Bein im Internet
> oder im Buero-LAN, gehoert ein Reverse Proxy mit TLS davor; die Anwendung
> aendert sich dafuer nicht, sie liefert weiterhin nur HTTP auf `LISTEN_PORT`.
>
> Erste Absicherung ohne Zusatzsoftware: `LISTEN_HOST` in der `.env` auf die
> Netzkarte des Stadionnetzes setzen statt auf `0.0.0.0`. Dann ist der Port auf
> den anderen Schnittstellen gar nicht erst offen.

---

## Architektur

```
  Stadion-LAN                    Host-Netz des Rechners
 ┌──────────────┐
 │ Mumble-Desktop│──64738──┐
 │ Mumla (PoC)   │         │   ┌──────────────────────────────────────┐
 │ Mumble iOS    │         └──▶│ mumble-server (murmur 1.5)           │
 └──────────────┘             │                                       │
                              │   Ice  127.0.0.1:6502  ◀──────────┐   │
                              └──────────────────────────────────┼───┘
                                                                 │
                              ┌──────────────────────────────────┼───┐
                              │ mumble-admin (ein Prozess)       │   │
                              │                                  │   │
                              │  ┌────────────┐  Ice-Aufrufe ────┘   │
                              │  │ IceClient  │                      │
                              │  │            │◀── Callbacks ────────┤
                              │  └─────┬──────┘   (eigener Adapter   │
                              │        │           auf 127.0.0.1)    │
                              │        ▼                             │
                              │  ┌────────────┐   ┌───────────────┐  │
                              │  │ LiveState  │◀──│ Monitor-Bot   │──┼──64738──▶
                              │  │ + Alarme   │   │ (pymumble)    │  │
                              │  └─────┬──────┘   └───────┬───────┘  │
                              │        │                  │          │
                              │        ▼                  ▼          │
                              │  ┌────────────┐   ┌───────────────┐  │
                              │  │ FastAPI    │   │ SQLite        │  │
                              │  │ + SSE      │   │ /data/…sqlite │  │
                              │  └─────┬──────┘   └───────────────┘  │
                              └────────┼─────────────────────────────┘
                                       │ HTTP 8080
                              ┌────────▼─────────┐
                              │  Browser im      │
                              │  Stadionnetz     │
                              └──────────────────┘
```

**Ein Container, ein Prozess.** `uvicorn` betreibt den asyncio-Loop; Polling,
Verlaufs-Aufraeumen und der Verbindungswaechter sind asyncio-Tasks. Ice und
pymumble bringen eigene Threads mit — deren Ereignisse werden mit
`call_soon_threadsafe` in den Loop gehoben, Ice-Aufrufe laufen ueber einen
kleinen Threadpool. Begruendung: DECISIONS.md, D-009.

Daraus folgt eine Regel fuer die Endpunkte: **wer `LiveState` anfasst, ist eine
Koroutine** (`async def`, laeuft im Loop), **wer SQLite anfasst, bleibt
synchron** (`def`, landet in FastAPIs Threadpool, wo Blockieren richtig ist).
Ein Ice-Aufruf darf in einer Koroutine nur ueber `await ctx.ice.run(...)`
passieren. Siehe DECISIONS.md, D-018. Jeder einzelne Ice-Aufruf ist zusaetzlich
auf 15 s begrenzt — nicht ueber `Ice.Override.Timeout`, das dafuer nachweislich
nicht taugt, sondern ueber `ice_invocationTimeout` (D-017).

**Warum zwei Datenquellen?** Ice liefert pro Client `udpPing`, `tcpPing`,
`bytespersec` und `tcponly` — aber **keinen Paketverlust**. Ein `getUserStats`
gibt es in der Slice nicht. `good/late/lost/resync` stehen ausschliesslich in
der `UserStats`-Nachricht des Mumble-Protokolls, die nur ein angemeldeter
Client abfragen kann. Genau dafuer haengt der Monitor-Bot im Server.

### Verzeichnisse

| Pfad | Inhalt |
|------|--------|
| `admin/intercom/ice/` | Ice-Anbindung: Client, Callbacks, Rechtetabelle, Domaenenmodell |
| `admin/intercom/provision/` | YAML-Schema, ACL-Abbildung, Planer, Anwender, Exporter |
| `admin/intercom/runtime.py` | Laufzeit-Abgleich: Priority Speaker und Listener |
| `admin/intercom/monitor/` | pymumble-Bot und Auswertung der `UserStats` |
| `admin/intercom/store/` | SQLite: Verlauf, Audit-Log, Notizen |
| `admin/intercom/web/` | FastAPI, SSE, Anmeldung, JSON-Schnittstelle |
| `admin/templates/`, `admin/static/` | Oberflaeche (htmx + Alpine, kein Bauschritt) |
| `admin/tests/` | Tests inkl. murmur-Doppel ueber echtes Ice |

---

## Vertrag mit der Compose

Alles, was zwischen `docker-compose.yml` und der Anwendung ausgetauscht wird,
steht an genau **einer** Stelle im Code: `admin/intercom/config.py`. Wer eine
Variable einfuehrt, aendert diese Datei und die Compose — sonst nichts.

### Umgebungsvariablen

| Variable | Vorgabe | Bedeutung |
|----------|---------|-----------|
| `MUMBLE_VERSION` | `v1.5.735` | **Einzige Versionsquelle.** Bestimmt den Tag des Server-Images *und* den Tag, aus dem `MumbleServer.ice` geladen wird. |
| `ICE_HOST` / `ICE_PORT` | `127.0.0.1` / `6502` | Ice-Endpunkt von murmur. |
| `ICE_SECRET` | – | `icesecretwrite` des Servers. Ohne passendes Secret keine schreibenden Aufrufe. Wird nie geloggt. |
| `ICE_SERVER_ID` | `1` | Virtueller Server. murmur zaehlt ab 1. |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `8080` | Wo das GUI lauscht (plain HTTP). |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / – | Vollzugang. |
| `ADMIN_READONLY_USER` / `ADMIN_READONLY_PASSWORD` | – | Optionaler Nur-Lese-Zugang. Leer = aus. |
| `SESSION_SECRET` | – | Signiert das Sitzungs-Cookie. Fehlt er, wird ein Zufallswert erzeugt und gewarnt — alle Anmeldungen gehen dann bei jedem Neustart verloren. |
| `INTERCOM_CONFIG` | `/config/intercom.yaml` | Pfad zur Wunschzustands-Datei. |
| `PROVISION_ON_START` | `true` | Beim Containerstart anwenden. |
| `PROVISION_PRUNE` | `false` | Kanaele/Gruppen loeschen, die nicht in der YAML stehen. Im Plan werden sie auch ohne den Schalter angezeigt. |
| `MONITOR_BOT_ENABLED` | `true` | Monitor-Bot an/aus. Aus = kein Paketverlust. |
| `MONITOR_BOT_NAME` | `monitor` | Anmeldename des Bots. |
| `MONITOR_BOT_CHANNEL` | `Intercom/Regie` | Kanal als **Pfad**, nicht als blosser Name. |
| `MONITOR_BOT_CERT` | `/data/monitor-cert.pem` | Client-Zertifikat. Fehlt es, wird eines erzeugt. |
| `MONITOR_STATS_INTERVAL_MS` | `5000` | Abstand zwischen zwei `UserStats`-Runden. |
| `MUMBLE_PORT` | `64738` | Port, auf dem der Bot sich anmeldet. |
| `POLL_INTERVAL_MS` | `2000` | Statistik-Polling. Werte unter 250 werden angehoben. |
| `HISTORY_RETENTION_HOURS` | `48` | Aufbewahrung des Metrik-Verlaufs. |
| `ALERT_PING_MS` | `80` | Schwelle Warnung; das Doppelte gilt als kritisch. |
| `ALERT_LOSS_PCT` | `2.0` | dito fuer Paketverlust. |
| `LOG_LEVEL` | `INFO` | |
| `DATA_DIR` | `/data` | SQLite und Bot-Zertifikat. |
| `SLICE_DIR` | `/opt/intercom/slice` | Uebersetzte Slice. Wird im Image gesetzt. |

### Volumes und Netz

* `./server:/data` (mumble-server), `./admin-data:/data` (mumble-admin),
  `./intercom.yaml:/config/intercom.yaml:ro`
* **Beide** Dienste laufen mit `network_mode: host`. Das ist keine Bequemlichkeit:
  Ice-Callbacks sind kein Polling — murmur baut eine Verbindung **zum
  Admin-Prozess** auf. Laege der Server in einem Bridge-Netz, waere `127.0.0.1`
  aus seiner Sicht sein eigener Namespace und der Rueckruf ginge ins Leere.
  Siehe DECISIONS.md, D-002.
* Das Admin-GUI startet nur mit dem Compose-Profil `gui`:
  `docker compose --profile gui up -d`.

---

## Schema-Referenz `intercom.yaml`

### Aufbau

```yaml
version: 1
server:        { defaultchannel: …, welcometext: … }
groups:        [ … ]
acl_templates: { name: [ … ] }
channels:      [ … ]          # Baum
policies:      { … }
users:         { name: { groups: [...], channel: … } }
networks:      [ { name, cidr, note } ]
devices:       { name: { … } }
```

### `groups`

Liste eigener Gruppennamen. Sie werden **am Wurzelkanal** angelegt und
vererben sich nach unten.

> **Abweichung von der urspruenglichen Vorgabe.** Vorgesehen war der oberste
> konfigurierte Kanal (`Intercom`). Das funktioniert fuer `kick`, `ban` und
> `register_users` nicht: murmur wertet diese Rechte ausschliesslich am
> Wurzelkanal aus (`src/ACL.cpp`: `if (ch->iId == 0 && applyFromSelf)`). Damit
> Richtlinien und Gruppen dieselbe Stelle sehen, liegen beide an der Wurzel.

Von murmur fest eingebaut und **nicht** neu definierbar: `all`, `auth`, `in`,
`out`, `sub`, `~sub`, `admin`.

In ACL-Eintraegen versteht murmur zusaetzlich Praefixe:

| Praefix | Bedeutung |
|---------|-----------|
| `!gruppe` | kehrt die Bedingung um |
| `~gruppe` | bezieht sich auf den Kanal, in dem die ACL steht |
| `#token` | Zugriffstoken statt Gruppe |
| `$hash` | Zertifikatshash statt Gruppe |

### `channels[]`

| Schluessel | Bedeutung |
|-----------|-----------|
| `name` | Pflicht. Eindeutig unter demselben Elternkanal, ohne `/`. |
| `description` | Tooltip im Client. |
| `position` | Sortierung (kleiner = weiter oben). |
| `acl_template` | Name aus `acl_templates`. Wird **zuerst** angewendet. |
| `acl` | Rohe ACL-Eintraege, gleiche Form wie in `acl_templates`. Danach angewendet. |
| `groups` | Eigene Gruppen **dieses** Kanals (Name, Mitglieder als Namen, optional `inherit`/`inheritable`). Die Gruppen aus `groups:` auf oberster Ebene liegen dagegen am Wurzelkanal. |
| `speak` | Gruppen, die hier sprechen duerfen. |
| `whisper_in` | Gruppen, die hierher fluestern duerfen. |
| `listen_for` | Gruppen, die den Kanal mithoeren duerfen. |
| `listen_to` | Kanaele, die die `speak`-Gruppen zusaetzlich mithoeren sollen. |
| `priority` | Gruppen, deren Mitglieder hier Priority Speaker sind. |
| `links` | Kanaele, mit denen verlinkt wird. |
| `children` | Unterkanaele. |

### Wie `speak` / `whisper_in` / `listen_for` zu ACLs werden

murmur gewaehrt **ohne jede ACL** bereits
`Traverse | Enter | Speak | Whisper | TextMessage | Listen` (`src/ACL.cpp`, `def`).
Ein `deny` ist also kein Zierrat, sondern noetig, um etwas wegzunehmen.

Pro Kanal entsteht immer dieselbe Reihenfolge — und Reihenfolge ist Semantik,
weil murmur `granted |= allow; granted &= ~deny` der Reihe nach auswertet, ein
spaeterer Eintrag also gewinnt:

1. Eintraege aus `acl_template`
2. Eintraege aus `acl`
3. ein zusammengefasster `@all`-Eintrag
4. je ein Eintrag pro Gruppe, alphabetisch

| Angabe | Ergebnis |
|--------|----------|
| `speak: [regie, leitung]` | `@all` **deny** Speak; `@regie`/`@leitung` **allow** Speak + Traverse + Enter |
| `speak: [all]` | `@all` **allow** Speak |
| `speak:` fehlt | zu Speak wird nichts gesagt; die Vererbung bleibt unangetastet |
| `whisper_in: [technik]` | `@all` deny Whisper; `@technik` allow Whisper + Traverse |
| `listen_for: [all]` | `@all` allow Listen |
| `listen_for: [regie]` | `@all` deny Listen; `@regie` allow Listen + Traverse |

**Traverse und Enter werden mitgegeben.** Wer sprechen soll, muss den Kanal
betreten koennen; wer mithoeren oder hineinfluestern soll, muss ihn wenigstens
sehen. Ohne das ergaeben restriktive Vorlagen Kanaele, die niemand benutzen
kann. Bei den Vorgabewerten aendert das nichts — nur dort, wo eine Vorlage
`Enter` oder `Traverse` entzieht.

### `policies`

| Richtlinie | Recht | Geltung |
|------------|-------|---------|
| `whisper_anywhere` | Whisper | Wurzel + Unterkanaele |
| `move_users` | Move | Wurzel + Unterkanaele |
| `mute_deafen` | MuteDeafen | Wurzel + Unterkanaele |
| `make_channel` | MakeChannel | Wurzel + Unterkanaele |
| `kick` | Kick | **nur Wurzel** |
| `ban` | Ban | **nur Wurzel** |
| `register_users` | Register | **nur Wurzel** |
| `guests_listen_only` | `@all` deny Speak+Whisper | Wurzel + Unterkanaele |
| `priority_speaker` | *kein ACL* | Nutzerzustand, zur Laufzeit gesetzt |

### `users`

```yaml
users:
  regie-1: { groups: [regie], channel: "Intercom/Regie" }
```

`groups` steuert die Mitgliedschaft. Der Name muss **registriert** sein — ohne
Registrierung gibt es keine Nutzer-ID, und Gruppenmitgliedschaft laeuft ueber
IDs. Unbekannte Namen landen im Provisioning-Report, statt still zu verschwinden.

`channel` ist optional und rein fuer die Alarmleiste: steht der Client nicht
dort, faellt es im Cockpit auf. **Verschoben wird niemand automatisch** —
waehrend eines Wettkampfs waere das gefaehrlich.

### Kanaleigene Gruppen

```yaml
channels:
  - name: Intercom
    children:
      - name: Kameras
        groups:
          - name: kamera-lokal
            add: [kam-1, kam-2]
        speak: [kamera-lokal]
```

Mitglieder stehen als **Namen**, nicht als IDs: Nutzer-IDs vergibt der Server
und waeren auf einem anderen Server bedeutungslos. Aufgeloest wird erst beim
Anwenden — ein nicht registrierter Name landet im Provisioning-Report.

Gruppen, die an einem Kanal existieren und **nicht** in der YAML stehen, bleiben
beim Anwenden erhalten; der Plan zeigt sie als „wuerde geloescht" an und
`PROVISION_PRUNE` entfernt sie. Genauso wie an der Wurzel — `setACL` ersetzt
immer alle Gruppen eines Kanals auf einmal.

### `networks` und `devices`

`networks` ordnet Client-IPs Segmenten zu (Netzsicht, Heatmap); der erste
passende Eintrag gewinnt. `devices` ist reine Dokumentation und erscheint im
Detailpanel.

### Rechtenamen

`Write`, `Traverse`, `Enter`, `Speak`, `MuteDeafen`, `Move`, `MakeChannel`,
`LinkChannel`, `Whisper`, `TextMessage`, `MakeTempChannel`, `Listen`, `Kick`,
`Ban`, `Register`, `SelfRegister`, `ResetUserContent`.

---

## Kommandozeile

```bash
docker compose exec mumble-admin intercom validate   # nur die YAML pruefen
docker compose exec mumble-admin intercom plan       # zeigen, was sich aendern wuerde
docker compose exec mumble-admin intercom apply      # anwenden (fragt nach)
docker compose exec mumble-admin intercom apply --yes --prune
docker compose exec mumble-admin intercom export > intercom-neu.yaml
docker compose exec mumble-admin intercom status     # Kurzbericht
```

Rueckgabewerte: `0` in Ordnung, `1` Fehler, `2` Konfiguration unbrauchbar,
`3` bei `plan --detailed-exitcode`, wenn es etwas zu tun gaebe.

Dieselbe Logik haengt im GUI hinter den Knoepfen. Plan und Anwendung laufen
durch **denselben** Code (`Reconciler` mit `dry_run`) — ein Plan kann also nicht
von dem abweichen, was ein `apply` anschliessend tut.

---

## Bekannte Grenzen

### Channel-Listener ueberleben keinen Reconnect

`startListening` **gibt es** in der Slice von 1.5 — die urspruengliche Annahme,
das ginge serverseitig nicht, war falsch. Die Slice-Dokumentation ist aber
irrefuehrend: sie nennt den ersten Parameter „The ID of the user", die
Implementierung nennt ihn `session` und loest ihn ueber `NEED_PLAYER` als
Sitzung auf (`MumbleServerIce.cpp`). `getListeningUsers` gibt entsprechend
**Session-IDs** zurueck.

Folge: Listener sind Sitzungszustand. Der Provisioner schreibt sie deshalb
nicht in den Soll-Zustand; stattdessen setzt der Laufzeit-Abgleich sie bei
`userConnected` und bei jedem Polling-Durchlauf nach. Im Cockpit steht pro
Nutzer Soll gegen Ist, Abweichungen sind rot.

### `Listen` fehlt in der Slice, wirkt aber

Die Slice von 1.5.857 deklariert keine Konstante `PermissionListen`. Der Server
kennt das Recht (`Listen = 0x800` in `src/ACL.h`), und `setACL` maskiert nur
gegen `ChanACL::All`, das `Listen` enthaelt. Wir fuehren die Bit-Tabelle
deshalb selbst und pruefen sie beim Start gegen die Slice. `listen_for` ist voll
funktionsfaehig. Belegt durch `test_listen_bit_ueberlebt_einen_echten_server`
samt Gegenprobe.

Ebenfalls korrigiert: die Slice heisst `PermissionRegisterSelf` (nicht
`PermissionSelfRegister`) und `ResetUserContent` (ohne `Permission`-Praefix).

### Nicht ueber Ice moeglich

| Was | Warum |
|-----|-------|
| `max_users` pro Kanal | Das `Channel`-Struct der Slice hat kein solches Feld, und es gibt keinen anderen Weg. Wird als Warnung gemeldet, nicht still ignoriert. Im Client von Hand setzbar. |
| Temporaere Kanaele anlegen | `addChannel` legt immer dauerhaft an; `Channel.temporary` ist beim Schreiben wirkungslos. |
| `priority_speaker` exportieren | Priority Speaker ist Nutzerzustand, kein ACL-Eintrag — der Server haelt dafuer keine Sollvorgabe vor. |
| Dauerhafte Gruppenmitgliedschaft ueber `addUserToGroup` | Ausdruecklich temporaer und an die Sitzung gebunden. Der Provisioner benutzt ausschliesslich `setACL`. |
| Paketverlust | Kein `getUserStats` in der Slice — dafuer gibt es den Monitor-Bot. |

### `getAllConf` ist nicht die wirksame Konfiguration

Die Namen der beiden Ice-Aufrufe fuehren in die Irre. Gegen murmur 1.5.735
nachgemessen, belegt in `MumbleServerIce.cpp` und `Meta.cpp`:

| Aufruf | Was wirklich drinsteht |
|---|---|
| `Server::getAllConf` | `SELECT key, value FROM config WHERE server_id = ?` — **nur** was jemand zur Laufzeit per `setConf` geaendert hat. Auf einem frischen Server steht dort ausser dem selbst erzeugten `certificate` **nichts**. |
| `Meta::getDefaultConf` | `qmConfig`, gebaut von `MetaParams` aus der **ini-Datei** plus den eingebauten Vorgaben. Beim Docker-Image ist das genau der Stand, den die `MUMBLE_CONFIG_*`-Variablen der Compose geschrieben haben. |

„Default" heisst also **nicht** „murmurs Werkseinstellung". Der wirksame Wert
ist: Datenbank, wenn dort ein Eintrag steht, sonst Datei. Die Ansicht
*Server → Konfiguration* zeigt deshalb drei Spalten — `Wirksam`, `Herkunft`
(Datenbank/Datei) und `Faellt zurueck auf` — statt eines irrefuehrenden
Ist/Soll-Vergleichs. Eine Zeile mit Herkunft **Datenbank** ist der interessante
Fall: dieser Wert wurde live geaendert und folgt der Compose nicht mehr, ueberlebt
aber jeden `docker compose up -d`.

Ebenfalls nachgemessen: **write-only ist nur `key` und `passphrase`.**
`impl_Server_getConf` prueft wortwoertlich diese zwei Namen. `getConf("icesecretwrite")`
gelingt und liefert einen **leeren String** — das Secret steht in der ini-Datei,
nicht in der `config`-Tabelle. Wer den leeren String fuer „kein Secret gesetzt"
haelt, zieht den falschen Schluss.

### VOX-Erkennung ist eine Heuristik

Mumble meldet den Sendemodus weder ueber Ice noch im Protokoll. Erkennbar ist
nur dauerhafter Datenfluss ohne Leerlauf. Der Alarm schlaegt erst nach laengerer
Zeit an, damit eine lange Ansage keinen Fehlalarm ausloest — und er ist im GUI
als Verdacht gekennzeichnet, nicht als Feststellung.

### Clients

* **Mumble Desktop** – voller Funktionsumfang, inklusive Channel Listener und
  Zertifikatsanmeldung.
* **Mumla (Android, PoC-Handhelds Inrico/Anysecu)** – PTT und Kanalwechsel
  funktionieren. Channel Listener setzt Mumla **nicht** selbst; genau deshalb
  setzt der Laufzeit-Abgleich sie serverseitig. Aeltere Fassungen melden nur das
  alte `version`-Feld, weshalb im Cockpit `version2` bevorzugt und auf `version`
  zurueckgefallen wird.
* **Mumble iOS** – PTT und Kanalwechsel; Funktionsumfang liegt zwischen den
  beiden.

### Verhalten bei einem Serverneustart

Faellt murmur weg, bleibt das Cockpit stehen: `/healthz` liefert weiter 200
(sonst wuerde der Docker-Healthcheck den Admin-Container wegen eines fremden
Dienstes neu starten, und beide kreisen umeinander), die Seiten zeigen den
letzten bekannten Stand, und ein Banner nennt den Grund. Die Verbindung wird im
Hintergrund mit wachsendem Abstand erneut versucht.

**Nach dem Wiederverbinden wird nicht erneut provisioniert.**
`PROVISION_ON_START` heisst Containerstart, nicht Serverneustart. Ein
Netzaussetzer waehrend des Wettkampfs darf nicht dazu fuehren, dass die ACLs
neu geschrieben werden und dabei eine bewusste Aenderung von vor fuenf Minuten
verlorengeht. Ist die Serverdatenbank tatsaechlich weg, zeigt der Plan die
Abweichung sofort — ein Klick auf „Anwenden" holt den Zustand zurueck.

Abgesichert durch `test_ueberlebt_einen_serverneustart_und_verbindet_neu`.

### Slice- und Versionsabgleich

Beim Start werden zwei Dinge geprueft: `Meta.getVersion()` gegen
`MUMBLE_VERSION`, und zusaetzlich die **Slice-Pruefsummen**
(`Meta.getSliceChecksums()` gegen `Ice.sliceChecksums`). Letzteres erkennt auch
einen Schnittstellenbruch innerhalb derselben Versionsnummer. Abweichungen
erscheinen als Banner im Cockpit und als Warnung im Log — sie verhindern den
Start **nicht**: ein Container, der deswegen nicht hochkommt, hilft waehrend
einer laufenden Veranstaltung niemandem.

---

## Fehlersuche

### „Keine Verbindung zur Ice-Schnittstelle"

```bash
docker compose logs --tail 80 mumble-server
docker compose exec mumble-admin intercom status
```

Der Reihe nach pruefen:

1. **Laeuft der Server?** `docker compose ps`
2. **Ist Ice ueberhaupt an?** In der Serverkonfiguration muessen `ice` und
   `icesecretwrite` gesetzt sein. In der Compose sind das
   `MUMBLE_CONFIG_ICE` und `MUMBLE_CONFIG_ICESECRETWRITE`. Die erzeugte
   ini-Zeile muss so aussehen:
   ```
   ice="tcp -h 127.0.0.1 -p 6502"
   ```
3. **Ist der Port offen?**
   `timeout 2 bash -c '</dev/tcp/127.0.0.1/6502' && echo offen`
4. **Beide im Host-Netz?** Steht bei einem der Dienste kein
   `network_mode: host`, funktionieren die Callbacks nicht.

### „murmur hat das Ice-Secret abgelehnt"

`ICE_SECRET` in der `.env` passt nicht zu `icesecretwrite` im Server. Nach jeder
Aenderung **beide** Container neu starten:

```bash
docker compose --profile gui up -d --force-recreate
```

Das Secret laesst sich absichtlich nicht ueber das GUI aendern — danach waere
die eigene Verbindung sofort tot.

### Versionsabweichung im Banner

```bash
docker exec mumble-server mumble-server --version
```

`MUMBLE_VERSION` in der `.env` auf `v<version>` setzen und das Admin-Image neu
bauen:

```bash
docker compose --profile gui up -d --build mumble-admin
```

`./setup.sh` macht das von selbst.

### Rechte auf `./server` oder `./admin-data`

Der Mumble-Server laeuft im Image als UID/GID **10000**; das Admin-Image legt
denselben Benutzer an.

```bash
sudo chown -R 10000:10000 ./server ./admin-data
```

Symptome: der Server startet und beendet sich sofort wieder, oder das Cockpit
zeigt das Banner „`/data/history.sqlite` ist nicht benutzbar" (dann laeuft das
GUI weiter, nur ohne Verlauf und Audit-Log).

### Kein Paketverlust in der Tabelle

Die Spalte zeigt `–`, solange der Monitor-Bot keine Werte liefert. Moegliche
Gruende:

1. `MONITOR_BOT_ENABLED=false`.
2. Der Bot ist nicht verbunden — im Cockpit oben rechts sichtbar, Fehlertext
   in der Kachel „Monitor-Bot".
3. **Der Bot hat zu wenig Rechte.** `Server::msgUserStats` fuellt die
   Paketzaehler nur, wenn der Fragende im selben Kanal steht **oder** `Ban` am
   Wurzelkanal hat:

   ```cpp
   bool extend = (uSource == pDstServerUser)
                 || hasPermission(uSource, qhChannels.value(0), ChanACL::Ban);
   bool local  = extend || (pDstServerUser->cChannel == uSource->cChannel);
   ```

   Deshalb hat die Beispielkonfiguration eine eigene Gruppe `monitor` mit genau
   einem Mitglied, und `policies.ban` enthaelt sie. Steht der Bot stattdessen
   nur in `regie`, misst er ausschliesslich die Clients in seinem eigenen Kanal
   — die Spalte bleibt fuer alle anderen leer, ohne dass irgendwo ein Fehler
   auftaucht. Siehe DECISIONS.md, D-015.
4. Ein Client laeuft nur ueber TCP: dessen UDP-Zaehler bleiben auf 0. Das
   Cockpit zeigt dann `–` statt `0,0 %` — „unbekannt" ist die richtige Aussage,
   und die Alarmschwelle darf darauf nicht anschlagen.

Dasselbe gilt fuer den Verlauf: die Sparkline „Verlust" im Detailpanel zeigt in
solchen Zeitraeumen eine **Unterbrechung**, keine Nulllinie. In der Datenbank
steht dort `NULL`. Der Unterschied ist der ganze Punkt — „kein Verlust" ist eine
Entwarnung, „nicht gemessen" ist keine. Eine Messung gilt nach 60 s als veraltet
(`LiveState.STATS_MAX_AGE_S`); danach faellt die Anzeige zurueck auf `–`, und
weder Alarmbalken noch `/metrics` melden noch etwas. Auch das ist Absicht: ein
Wert von vor zehn Minuten sieht aus wie eine Messung, ist aber eine Erinnerung.

### SSE bleibt stehen / Cockpit aktualisiert nicht

Ohne Reverse Proxy tritt das nicht auf. Setzt jemand spaeter einen davor, ist
Pufferung die erste Verdaechtige: die Anwendung schickt `X-Accel-Buffering: no`
und `Cache-Control: no-cache`, aber manche Konfigurationen ueberschreiben das
(bei nginx hilft `proxy_buffering off;`). Zum Eingrenzen direkt am Port testen,
am Proxy vorbei:

```bash
curl -N http://127.0.0.1:8080/api/events   # nach Anmeldung
```

Kommen dort laufend `event: state`-Zeilen an, liegt es am Proxy, nicht an der
Anwendung.

### Provisionierung schlaegt fehl

`intercom plan` zeigt jede Aenderung einzeln mit Vorher/Nachher. Fehlgeschlagene
Aenderungen stehen mit Fehlertext im Report — ein `apply` bricht **nicht** beim
ersten Fehler ab, sondern macht weiter und meldet am Ende alles.

Haeufigster Fall: „Nicht registriert: …". Diese Nutzer haben keine ID und
koennen darum in keiner Gruppe sein. Auf der Seite „Nutzer" registrieren
(verbundene Clients per Knopf mit ihrem aktuellen Zertifikat).

---

## Entwicklung und Tests

```bash
cd admin
python3.11 -m pip install -e ".[dev,ice]"   # ice nur ohne python3-zeroc-ice
./scripts/build_slice.sh v1.5.735 slice     # Slice holen und uebersetzen
python -m pytest -q                          # 219 Tests (ohne echten Server: 207)
python -m ruff check .
python -m mypy intercom
```

### Zwei Teststufen

**1. Gegen ein murmur-Doppel (laeuft ueberall).**
`tests/fake_murmur.py` spricht **echtes Ice** — eigener Objektadapter, echte
Proxies, echte Serialisierung, echte Rueckrufe. Nachgebildet ist die Semantik
aus `MumbleServerIce.cpp` und `ACL.cpp`: `setACL` ersetzt vollstaendig, `getACL`
liefert geerbte Eintraege markiert mit, Gruppen arbeiten mit Nutzer-IDs,
Listener haengen an der Session, `allow`/`deny` werden gegen `ChanACL::All`
maskiert.

**2. Gegen einen echten Server (optional).**

```bash
docker compose -f docker-compose.test.yml up -d
cd admin && python -m pytest -m integration -v
docker compose -f docker-compose.test.yml down -v
```

Diese Tests pruefen das, was ein Doppel nicht kann — vor allem, ob unsere Lesart
des murmur-Quelltextes stimmt: ueberlebt `Listen` (0x800) einen echten
`setACL`-Umlauf, wird ein Bit ausserhalb von `ChanACL::All` wirklich
wegmaskiert, ist Gruppenmitgliedschaft ueber `setACL` tatsaechlich dauerhaft.
Ohne erreichbaren Testserver werden sie uebersprungen.

Sie sind gegen **murmur 1.5.735** ausgefuehrt worden und laufen durch. Dabei
gefunden und behoben: eine unbrauchbare Variable in `docker-compose.test.yml`
(`MUMBLE_CONFIG_LOGLEVEL` gibt es nicht — der Testserver startete nicht), die
verdrehte Konfigurationsansicht und zwei Exporter-Fehler (siehe DECISIONS.md,
D-023 bis D-025). Ein Doppel haette keinen davon gezeigt.

> **Hinweis zum Netz.** In der Umgebung, in der dieser Code entstanden ist, ist
> `production.cloudfront.docker.com` — der Layer-Auslieferer von Docker Hub —
> von der Egress-Richtlinie gesperrt (CONNECT → 403), waehrend
> `auth.docker.io` und `registry-1.docker.io` antworten. Ein `docker pull`
> gegen Docker Hub scheitert deshalb beim Herunterladen der Layer. Bau und
> Tests liefen ueber den Spiegel `mirror.gcr.io`:
>
> ```bash
> docker pull mirror.gcr.io/mumblevoip/mumble-server:v1.5.735
> docker tag  mirror.gcr.io/mumblevoip/mumble-server:v1.5.735 \
>             mumblevoip/mumble-server:v1.5.735
> docker build --build-arg BASE_IMAGE=mirror.gcr.io/library/debian:bookworm-slim ...
> ```
>
> Auf einem normalen Netz ist das nicht noetig: dort genuegen die Vorgaben.

### Warum der Live-Strom nicht ueber den TestClient geprueft wird

Starlettes `TestClient` wartet, bis eine Antwort vollstaendig ist. Ein SSE-Strom
endet nie von allein — `client.stream("/api/events")` haengt also fuer immer.
Das ist eine Eigenheit des Testwerkzeugs, kein Fehler der Anwendung. Geprueft
werden deshalb die Route direkt (erster Rahmen, Kopfzeilen) und der Verteiler
als Einheit (Verwerfen alter Rahmen bei einem eingefrorenen Browser).

---

## Weiterfuehrend

* `DECISIONS.md` – warum etwas so gebaut ist, mit Belegstellen im
  Mumble-Quelltext.
* `/metrics` – Prometheus-Format: Ping, Verlust, Bandbreite und Online-Zeit je
  Client, Nutzerzahl je Kanal, Server-Laufzeit, offene Alarme. Ohne Anmeldung,
  damit ein Prometheus im Homelab es abholen kann.
* `/healthz` – Zustandsbericht fuer den Docker-Healthcheck. Enthaelt bewusst
  keine Secrets.
