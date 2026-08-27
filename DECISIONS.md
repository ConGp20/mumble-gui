# Entscheidungen

Warum etwas so gebaut ist, wie es gebaut ist. Jede Entscheidung nennt das
Problem, die gewaehlte Loesung und was dagegen sprach.

---

## D-001 – `docker-compose.yml`, `.env` und `intercom.yaml` wurden neu erstellt

**Problem.** Der Auftrag verweist auf drei bereits vorhandene Dateien in
`/volume1/docker/stadion-intercom/`, die den Vertrag definieren. Das
Repository `ConGp20/mumble-gui` war zum Start dieser Arbeit **vollstaendig
leer** – weder lokal noch auf dem Remote lag ein Commit, geschweige denn eine
der drei Dateien.

**Entscheidung.** Die drei Dateien wurden aus der Aufgabenbeschreibung
rekonstruiert. Sie nennt jede Umgebungsvariable (`ICE_HOST`, `ICE_PORT`,
`ICE_SECRET`, `ICE_SERVER_ID`, `POLL_INTERVAL_MS`, `ALERT_PING_MS`,
`ALERT_LOSS_PCT`, `MONITOR_BOT_NAME`, `HISTORY_RETENTION_HOURS`, `ADMIN_USER`,
`ADMIN_PASSWORD`, `SESSION_SECRET`, `LISTEN_PORT`, `PROVISION_ON_START`,
`PROVISION_PRUNE`, `MUMBLE_VERSION`), jeden Pfad (`/data/history.sqlite`,
`./server`, `./admin-data`), jeden Port (6502), das Compose-Profil `gui`, den
Servicenamen `mumble-admin` mit `network_mode: host` und das YAML-Schema.

**Konsequenz.** Existieren auf dem NAS bereits eigene Fassungen, gilt dort
deren Inhalt. Abzugleichen sind dann nur die Variablennamen aus `.env.example`
und das Schema aus `README.md`. Der Code liest **ausschliesslich** die in
`admin/intercom/config.py` aufgefuehrten Variablen – diese Datei ist der
Vertrag.

---

## D-002 – Beide Container laufen im Host-Netz

**Problem.** Ice-Callbacks (`Server.addCallback`) sind kein Polling: murmur
baut eine Verbindung **zum Admin-Prozess** auf. Der Admin-Prozess muss also
einen eigenen Ice-Objektadapter betreiben, den murmur erreichen kann.

**Entscheidung.** `mumble-server` **und** `mumble-admin` laufen mit
`network_mode: host`. Der Admin-Adapter bindet auf `127.0.0.1` mit Port `0`
(Ice waehlt einen freien Port).

**Verworfen.** Nur `mumble-admin` im Host-Netz und den Server in einem
Bridge-Netz zu lassen, funktioniert **nicht**: `127.0.0.1` waere aus Sicht des
Servers sein eigener Netzwerk-Namespace, der Rueckruf ginge ins Leere. Ein
Adapter auf `0.0.0.0` mit der Docker-Bridge-IP waere moeglich, macht die
Admin-Schnittstelle aber ohne Not von aussen erreichbar.

---

## D-003 – `Listen` (0x800) wird selbst definiert, nicht aus der Slice gelesen

**Problem.** `channels[].listen_for` soll auf das Recht „Mithoeren" abgebildet
werden. Die Slice-Datei von Mumble 1.5.857 deklariert dafuer **keine
Konstante** – es gibt kein `PermissionListen`.

**Befund.** Der Server kennt das Recht sehr wohl. `src/ACL.h`:

```
MakeTempChannel = 0x400,
Listen          = 0x800,
...
All = Write + Traverse + ... + Listen + ... + ResetUserContent
```

und `impl_Server_setACL` in `src/murmur/MumbleServerIce.cpp` maskiert die per
Ice uebergebenen Bits lediglich gegen `ChanACL::All`:

```cpp
acl->pDeny  = static_cast<ChanACL::Permissions>(ai.deny)  & ChanACL::All;
acl->pAllow = static_cast<ChanACL::Permissions>(ai.allow) & ChanACL::All;
```

`ChanACL::All` enthaelt `Listen`. Das Bit ueberlebt die Maskierung.

**Entscheidung.** `admin/intercom/ice/permissions.py` fuehrt die Bit-Tabelle
selbst, inklusive `Listen = 0x800`. `verify_against_slice()` vergleicht beim
Start alle Werte mit der einkompilierten Slice und meldet Abweichungen als
Banner. Der Test `test_acl_schreiben_und_lesen` belegt, dass 0x800 einen
`setACL`/`getACL`-Umlauf uebersteht.

**Konsequenz.** `listen_for` ist voll funktionsfaehig und muss **nicht**, wie
im Auftrag vermutet, auf Anzeige und Erinnerung reduziert werden.

Nebenbefund, ebenfalls in der Tabelle korrigiert: die Slice heisst
`PermissionRegisterSelf` (nicht `PermissionSelfRegister`) und
`ResetUserContent` (ohne `Permission`-Praefix).

---

## D-004 – Channel-Listener werden zur Laufzeit gesetzt, nicht provisioniert

**Problem.** `channels[].listen_to` soll dafuer sorgen, dass Mitglieder der
`speak`-Gruppen eines Kanals zusaetzlich bestimmte andere Kanaele mithoeren.
Der Auftrag vermutete, das gehe serverseitig womoeglich gar nicht.

**Befund.** Es geht – die Slice hat `startListening`, `stopListening`,
`isListening`, `getListeningChannels`, `getListeningUsers`. **Aber**: die
Slice-Dokumentation nennt den ersten Parameter „The ID of the user", und das
ist falsch. Die Implementierung nennt ihn `session` und loest ihn ueber
`NEED_PLAYER` als Sitzung auf:

```cpp
static void impl_Server_startListening(..., int session, int channelid) {
    NEED_SERVER; NEED_CHANNEL; NEED_PLAYER;
    server->startListeningToChannel(user, channel);
```

`getListeningUsers` gibt entsprechend **Session-IDs** zurueck, keine Nutzer-IDs.

**Entscheidung.** Listener sind Sitzungszustand und ueberleben keinen
Reconnect. Der Provisioner schreibt sie deshalb nicht in den Soll-Zustand,
sondern der Laufzeit-Abgleich setzt sie per Callback bei `userConnected` und
`userStateChanged` – genau wie beim Priority Speaker. Im Cockpit steht pro
Nutzer Soll (aus `listen_to`) gegen Ist (`getListeningChannels`); eine
Abweichung wird rot markiert.

`test_listener_haengen_an_der_session` haelt das Verhalten fest.

---

## D-005 – Gruppenmitgliedschaft nur ueber `setACL`, nie ueber `addUserToGroup`

**Problem.** Es gibt zwei Wege, jemanden in eine Gruppe zu bekommen.

**Befund.** `addUserToGroup` ist laut Slice ausdruecklich temporaer („This state
is not saved, and is intended for temporary memberships") und nimmt eine
**Session**. Dauerhafte Mitgliedschaft steht in der `add`-Liste einer Gruppe
und wird ueber `setACL` geschrieben; dort sind es **registrierte Nutzer-IDs**.
`impl_Server_setACL` uebernimmt `gi.add`/`gi.remove` unveraendert nach
`Group::qsAdd`/`qsRemove`.

**Entscheidung.** Der Provisioner benutzt ausschliesslich `setACL`. Ein Name
aus `users:`, der nicht registriert ist, wird nicht stillschweigend
uebersprungen, sondern landet als Eintrag im Provisioning-Report.
`addUserToGroup` bleibt fuer die eine Stelle reserviert, an der es passt: eine
befristete Berechtigung waehrend der Veranstaltung, ueber das GUI.

---

## D-006 – Geerbte ACL-Eintraege werden vor dem Schreiben entfernt

**Problem.** `getACL` liefert geerbte Eintraege der Elternkanaele mit
(`inherited = true`). `setACL` ersetzt ACLs und Gruppen des Kanals
**vollstaendig**.

**Befund.** `impl_Server_setACL` wertet `inherited` beim Schreiben nicht aus.
Wer eine gelesene ACL-Liste unveraendert zurueckschreibt, legt damit alle
geerbten Eintraege als *eigene* Eintraege des Kanals an und friert die
Vererbung ein – die Aenderung im Elternkanal wirkt danach nicht mehr.

**Entscheidung.** `IceClient.set_channel_acl` filtert `inherited`-Eintraege
und -Gruppen grundsaetzlich heraus. Jeder schreibende Pfad geht durch diese
Methode. `test_geerbte_eintraege_werden_beim_schreiben_gefiltert` sichert das ab.

---

## D-007 – Versionsabgleich ueber Slice-Pruefsummen statt nur ueber die Versionsnummer

**Problem.** Der Auftrag verlangt, `Meta.getVersion()` gegen die einkompilierte
Version zu halten. Das erkennt aber keinen Schnittstellenbruch innerhalb
derselben Versionsnummer.

**Entscheidung.** Zusaetzlich zum Versionsvergleich werden die
Slice-Pruefsummen verglichen: `Meta.getSliceChecksums()` liefert die Summen der
Serverfassung, `Ice.sliceChecksums` die unserer. `slice2py` fuellt letztere nur
mit dem Schalter `--checksum` – der steht in `admin/scripts/build_slice.sh`.
Beides zusammen landet als Banner im Cockpit und als Warnung im Log.

**Bewusst kein Abbruch.** Ein Versionsunterschied macht die meisten Aufrufe
noch nicht kaputt. Ein Container, der deswegen nicht startet, hilft waehrend
einer laufenden Veranstaltung niemandem.

---

## D-008 – `zeroc-ice` wird im Image aus dem Quelltext gebaut

**Problem.** Der Auftrag gibt Python 3.11 vor und weist darauf hin, dass es
fuer neuere Python-Versionen keine verlaesslichen Wheels gibt.

**Befund, gemessen statt vermutet.**

* PyPI liefert fuer `zeroc-ice==3.7.11` **kein** Wheel fuer CPython 3.11 – nur
  das Quell-Archiv. `pip download zeroc-ice` zieht `zeroc_ice-3.7.11.tar.gz`.
* Das Ubuntu-Paket `python3-zeroc-ice` (24.04, `3.7.10-2.1build1`) ist gegen
  **Python 3.12** gebaut (`Depends: python3 (<< 3.13), python3 (>= 3.12~)`) und
  damit unter 3.11 unbrauchbar.
* Der Bau aus dem Quelltext funktioniert: mit `build-essential`, `libssl-dev`
  und `libbz2-dev` uebersetzt `pip install --no-binary :all: zeroc-ice==3.7.11`
  unter Python 3.11 fehlerfrei. `slice2py` landet danach in `/usr/local/bin`.

**Entscheidung.** Das Dockerfile baut `zeroc-ice` in einer Builder-Stufe aus
dem Quelltext und kopiert nur das Ergebnis ins Laufzeit-Image. Das kostet beim
ersten Bau einige Minuten, ist dafuer unabhaengig von Distributionspaketen und
an Python 3.11 gebunden.

---

## D-009 – Ein Prozess mit asyncio-Tasks statt `supervisord`

**Problem.** Web-Server, Ice-Callbacks, Polling, Monitor-Bot und
Verlaufs-Aufraeumen laufen nebeneinander.

**Entscheidung.** Ein einziger Prozess. `uvicorn` betreibt den asyncio-Loop,
Polling und Aufraeumen sind asyncio-Tasks. Ice und pymumble bringen eigene
Threads mit; deren Ereignisse werden per `call_soon_threadsafe` in den Loop
gehoben, Ice-Aufrufe laufen ueber einen kleinen Threadpool
(`AsyncIceClient`), damit der Loop nie blockiert.

**Verworfen.** `supervisord` haette einen zweiten Prozess fuer den Monitor-Bot
erlaubt, aber der Bot muss seine Messwerte in dieselbe SQLite schreiben und im
Cockpit sofort sichtbar sein. Zwei Prozesse haetten dafuer IPC gebraucht –
Aufwand ohne Gegenwert. Ein Absturz des Bots darf das GUI nicht mitreissen;
das loest ein Supervisor-Task im Loop mit Backoff.

---

## D-010 – Integrationstests gegen einen echten Server sind vorhanden, liefen hier aber nicht

**Problem.** Der Auftrag verlangt Tests gegen einen echten Mumble-Server im
Docker.

**Befund.** In der Umgebung, in der dieser Code entstanden ist, ist der
Docker-Daemon zwar startbar, der Zugriff auf die Docker-Hub-Layer
(`production.cloudfront.docker.com`) wird aber von der Egress-Richtlinie mit
`403` blockiert. Ein `mumblevoip/mumble-server`-Image liess sich daher nicht
laden.

**Entscheidung.** Zwei Ebenen:

1. `admin/tests/fake_murmur.py` ist ein murmur-Doppel, das **echtes Ice**
   spricht – eigener Objektadapter, echte Proxies, echte Serialisierung, echte
   Rueckrufe. Nachgebildet ist die Semantik aus `MumbleServerIce.cpp` und
   `ACL.cpp`: `setACL` ersetzt vollstaendig, `getACL` liefert geerbte Eintraege
   markiert mit, Gruppen arbeiten mit Nutzer-IDs, Listener haengen an der
   Session, `allow`/`deny` werden gegen `ChanACL::All` maskiert. Diese Tests
   laufen ueberall und sind gruen.
2. `admin/tests/test_integration_real_server.py` faehrt einen echten
   `mumble-server`-Container hoch. Er wird uebersprungen, wenn kein Docker
   erreichbar ist, und ist mit `-m integration` gezielt ausfuehrbar.

**Offen.** Ebene 2 wurde in dieser Umgebung **nicht ausgefuehrt**. Sie laeuft
vom Entwicklungsrechner gegen den Testaufbau, nicht im Laufzeit-Image -- dort
sind weder pytest noch die Testdateien enthalten, und das soll so bleiben:

```bash
docker compose -f docker-compose.test.yml up -d
cd admin && python -m pytest -m integration -v
docker compose -f docker-compose.test.yml down -v
```


---

## D-011 - Gruppen und Richtlinien liegen am Wurzelkanal

**Problem.** Die Vorgabe lautete, Gruppen am obersten Kanal des Baums
(`Intercom`) anzulegen und die Richtlinien dort mit Apply-sub zu setzen.

**Befund.** Fuer `kick`, `ban` und `register_users` funktioniert das nicht.
`src/ACL.cpp`:

```cpp
// These permissions are only grantable from the root channel
// as they affect the users globally.
if (ch->iId == 0 && applyFromSelf) {
    if (acl->pAllow & Kick) granted |= Kick;
```

Ein solcher Eintrag an `Intercom` waere wirkungslos -- und zwar lautlos. Dazu
kommt: Gruppen vererben sich nur nach **unten**. Eine an `Intercom` definierte
Gruppe ist an der Wurzel unbekannt, ein Wurzel-ACL koennte sie also gar nicht
referenzieren.

**Entscheidung.** Gruppen **und** Richtlinien liegen am Wurzelkanal (ID 0).
Richtlinien, die im ganzen Baum gelten, bekommen `apply_here` + `apply_subs`;
die drei Wurzel-Rechte bekommen nur `apply_here`, weil `applyFromSelf` verlangt
wird. Bewahrt wird dabei der von murmur angelegte Eintrag `@admin -> Write`, den
`setACL` sonst mitloeschen wuerde.

**Konsequenz.** Gruppen gelten serverweit, nicht nur unter `Intercom`. In dieser
Installation gibt es keinen zweiten Baum; gaebe es einen, waeren die Gruppen
auch dort sichtbar.

---

## D-012 - `speak`-Gruppen bekommen Traverse und Enter mit

**Problem.** Die Abbildung `speak: [regie]` erzeugt "@all deny Speak,
@regie allow Speak". Mit einer Vorlage, die `Enter` entzieht (etwa
`geschlossen`), entstuende ein Kanal, in dem `regie` sprechen duerfte, ihn aber
nicht betreten kann -- also ein unbenutzbarer Kanal.

**Entscheidung.** Gruppen aus `speak` erhalten zusaetzlich `Traverse | Enter`,
Gruppen aus `whisper_in` und `listen_for` zusaetzlich `Traverse`. Wer sprechen
soll, muss den Kanal betreten koennen; wer mithoeren oder hineinfluestern soll,
muss ihn wenigstens sehen.

**Warum das nichts kaputt macht.** murmur gewaehrt ohne jede ACL bereits
`Traverse | Enter | Speak | Whisper | TextMessage | Listen` (`ACL.cpp`, `def`).
Bei den Vorgabewerten aendert die Zugabe also nichts -- sie wirkt nur dort, wo
eine Vorlage diese Rechte vorher entzogen hat. Dokumentiert in der
Schema-Referenz der README.

---

## D-013 - Zusaetzlicher Schluessel `channels[].acl`

**Problem.** `export` soll den Ist-Zustand als YAML im selben Schema
zurueckschreiben, ausdruecklich auch fuer "ein per Hand geklicktes Setup". Aus
rohen ACL-Bitmasken lassen sich `speak` / `whisper_in` / `listen_for` aber nicht
in jedem Fall zurueckgewinnen -- eine handgeklickte ACL folgt keinem Muster.

**Entscheidung.** Das Schema bekommt `channels[].acl`: rohe Eintraege in
derselben Form wie in `acl_templates`, angewendet nach der Vorlage und vor den
abgeleiteten Eintraegen.

Der Exporter geht in drei Stufen vor: erst raet er die lesbare Form, dann laesst
er die Vermutung durch **denselben** Generator laufen, den der Provisioner
benutzt, und vergleicht Bit fuer Bit. Passt es nicht, versucht er es mit den
Restbits von `@all` als explizitem Eintrag (das faengt den haeufigen Fall
"Vorlage hat Traverse/Enter beigesteuert"). Passt es dann immer noch nicht,
schreibt er alles roh.

**Konsequenz.** Der Export ist **immer** verlustfrei, und fuer Konfigurationen
aus dieser Datei kommt trotzdem die lesbare Form heraus. Der Name einer
`acl_template` laesst sich dabei nicht rekonstruieren -- der Server speichert nur
das Ergebnis, nicht die Herkunft.

---

## D-014 - `users[].channel` als reine Anzeige

**Problem.** Der Auftrag verlangt den Alarm "Client nicht in seinem
Soll-Kanal". Einen Soll-Kanal gab das Schema aber nicht her: `users` kannte nur
`groups`, und aus einer Gruppenzugehoerigkeit laesst sich bei mehreren
passenden Kanaelen kein eindeutiger Kanal ableiten.

**Entscheidung.** `users[name].channel` als optionaler Pfad. Er wird **nur** fuer
die Alarmleiste ausgewertet; der Provisioner fasst ihn nicht an und verschiebt
niemanden automatisch. Waehrend eines laufenden Wettkampfs jemanden ungefragt in
einen anderen Kanal zu ziehen, waere die gefaehrlichere Variante -- das GUI zeigt
die Abweichung, die Entscheidung bleibt beim Menschen.

---

## D-015 - Der Monitor-Bot braucht `Ban` am Wurzelkanal

**Problem.** In der ersten Fassung stand der Bot in der Gruppe `regie`. Damit
haette er fuer fast alle Clients keinen Paketverlust gemessen -- und die Spalte
waere leer geblieben, ohne dass irgendwo ein Fehler aufgetaucht waere.

**Befund.** `Server::msgUserStats` in `src/murmur/Messages.cpp`:

```cpp
bool extend = (uSource == pDstServerUser)
              || hasPermission(uSource, qhChannels.value(0), ChanACL::Ban);
...
bool local  = extend || (pDstServerUser->cChannel == uSource->cChannel);
if (local) {
    mpusss = msg.mutable_from_client();
    mpusss->set_good(...); mpusss->set_lost(...);
```

Die Paketzaehler haengen an `local`. Ohne `Ban` am Wurzelkanal saehe der Bot
ausschliesslich die Clients in seinem eigenen Kanal.

**Entscheidung.** Eine eigene Gruppe `monitor` mit genau einem Mitglied, und
`policies.ban: [leitung, monitor]`. Das Recht liegt bewusst **nicht** bei
`regie`: es ist eine echte Befugnis und soll nicht an einer Personengruppe
haengen, die es nie braucht. Der Bot selbst bannt niemanden -- er sendet nie
etwas ausser `UserStats`-Abfragen, und das ist strukturell sichergestellt.

**Alternative verworfen.** Den Bot in jeden Kanal zu schicken, waere die einzige
Moeglichkeit ohne `Ban` -- mit einem Client geht das aber nicht gleichzeitig, und
reihum zu wandern wuerde die Messung unbrauchbar zerhacken.

---

## D-016 - `.env` ist nicht versioniert

**Problem.** `setup.sh` schreibt echte Secrets in die `.env`. Eine versionierte
`.env` waere eine Falle: der naechste `git add -A` auf dem NAS wuerde sie
mitnehmen.

**Entscheidung.** Nur `.env.example` liegt im Repository, `.env` steht in der
`.gitignore`. `setup.sh` erzeugt sie beim ersten Lauf aus der Vorlage und setzt
`chmod 600`. In der Historie dieses Repositories standen zu keinem Zeitpunkt
echte Secrets -- die eingecheckte Fassung enthielt ausschliesslich
`ERSETZEN_*`-Platzhalter.
