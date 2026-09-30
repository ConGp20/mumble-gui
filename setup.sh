#!/usr/bin/env bash
# =============================================================================
#  Stadion-Intercom – Einrichtung
#
#  Bringt einen frischen Docker-Host von null auf ein laufendes Intercom:
#  Verzeichnisse, Secrets, Mumble-Server, Admin-GUI. Kanäle legt man danach
#  in der Oberfläche an – der Server ist die Wahrheit, nicht eine Datei.
#
#  Idempotent: ein zweiter Lauf ändert nur, was noch nicht stimmt.
#
#  Bewusst sparsames Bash: ohne mapfile/readarray, ohne assoziative Arrays und
#  ohne GNU-eigene sed-Schalter. Läuft damit auf Debian, Ubuntu, Raspberry Pi
#  OS und auch auf den abgespeckten Bash-Fassungen mancher NAS-Systeme.
#
#      ./setup.sh            komplette Einrichtung
#      ./setup.sh --check    nur prüfen, nichts ändern
#      ./setup.sh --yes      wird angenommen, aber nicht mehr gebraucht:
#                            das Skript stellt keine Rückfragen mehr
#      ./setup.sh --help     Kurzhilfe
# =============================================================================
set -euo pipefail

HIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HIER}"

NUR_PRUEFEN=0
PROBLEME=0

# --- Ausgabe -----------------------------------------------------------------
# Farbe nur, wenn wirklich ein Terminal dranhängt. In einem Log oder unter
# systemd wären Escape-Sequenzen nur Müll.
if [ -t 1 ]; then
  C_ROT=$'\033[31m'; C_GRUEN=$'\033[32m'; C_GELB=$'\033[33m'
  C_BLAU=$'\033[36m'; C_FETT=$'\033[1m'; C_AUS=$'\033[0m'
else
  C_ROT=""; C_GRUEN=""; C_GELB=""; C_BLAU=""; C_FETT=""; C_AUS=""
fi

titel()   { printf '\n%s==> %s%s\n' "${C_FETT}${C_BLAU}" "$*" "${C_AUS}"; }
ok()      { printf '    %s✔%s %s\n' "${C_GRUEN}" "${C_AUS}" "$*"; }
info()    { printf '    %s·%s %s\n' "${C_BLAU}" "${C_AUS}" "$*"; }
warnung() { printf '    %s!%s %s\n' "${C_GELB}" "${C_AUS}" "$*"; }
fehler()  { printf '    %s✘%s %s\n' "${C_ROT}" "${C_AUS}" "$*" >&2; PROBLEME=$((PROBLEME + 1)); }
abbruch() { printf '\n%sAbbruch:%s %s\n' "${C_ROT}${C_FETT}" "${C_AUS}" "$*" >&2; exit 1; }

fehlerfalle() {
  printf '\n%sUnerwarteter Fehler in Zeile %s.%s\n' "${C_ROT}${C_FETT}" "$1" "${C_AUS}" >&2
  printf 'Der letzte Befehl endete mit Rückgabewert %s.\n' "$2" >&2
  exit 1
}
trap 'fehlerfalle "${LINENO}" "$?"' ERR

hilfe() {
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "${arg}" in
    --check) NUR_PRUEFEN=1 ;;
    # Angenommen, damit vorhandene Aufrufe weiter laufen. Seit die
    # Einrichtung nichts mehr anlegt, gibt es keine Rückfrage mehr.
    --yes|-y) : ;;
    --help|-h) hilfe ;;
    *) abbruch "Unbekannte Option ${arg}. --help zeigt die Möglichkeiten." ;;
  esac
done


# =============================================================================
#  1. Docker
# =============================================================================
titel "Docker prüfen"

if ! command -v docker > /dev/null 2>&1; then
  fehler "docker ist nicht installiert."
  info "Aus der offiziellen Quelle einrichten, nicht über docker.io: das"
  info "Paket der Distribution bringt weder 'docker compose' noch"
  info "'docker buildx' mit, und Docker nennt es unter den Paketen, die vor"
  info "der eigenen Installation weg müssen. Die Schritte stehen im README"
  info "unter \"Docker aus der offiziellen Quelle\"; kurz:"
  info "  https://docs.docker.com/engine/install/ubuntu/"
  abbruch "Ohne Docker geht es nicht weiter."
fi

COMPOSE=""
if docker compose version > /dev/null 2>&1; then
  COMPOSE="docker compose"
  ok "docker compose (v2) gefunden"
elif command -v docker-compose > /dev/null 2>&1; then
  COMPOSE="docker-compose"
  warnung "Nur docker-compose (v1) gefunden. Es sollte gehen, v2 ist aber empfohlen."
  info "v1 wird seit 2023 nicht mehr gepflegt. Das Paket docker-compose-plugin"
  info "aus der offiziellen Docker-Quelle bringt v2 mit -- siehe README."
else
  abbruch "Weder 'docker compose' noch 'docker-compose' gefunden."
fi

if ! docker info > /dev/null 2>&1; then
  fehler "Der Docker-Daemon antwortet nicht. Läuft er, und darf dieser Benutzer ihn ansprechen?"
  info "Starten:      sudo systemctl start docker"
  info "Ohne sudo:    sudo usermod -aG docker \$USER  (danach neu anmelden)"
  [ "${NUR_PRUEFEN}" -eq 1 ] || abbruch "Ohne laufenden Docker-Daemon geht es nicht weiter."
else
  ok "Docker-Daemon erreichbar"
fi

# Nur die Compose-Datei ist Voraussetzung. Die intercom.yaml wurde frueher
# mitgeprueft -- sie ist seit der Umkehr keine Quelle der Wahrheit mehr,
# sondern nur noch das Format fuer Sicherungen, und fehlt im Normalfall.
[ -f docker-compose.yml ] || abbruch "docker-compose.yml fehlt. Wird dieses Skript im Projektverzeichnis ausgeführt?"
ok "docker-compose.yml gefunden"

# =============================================================================
#  2. Verzeichnisse
# =============================================================================
titel "Verzeichnisse"

# Der mumble-server läuft im Image als 10000:10000; das Admin-Image legt
# denselben Benutzer an. Damit gehören beide Datenverzeichnisse demselben.
for verzeichnis in server admin-data; do
  if [ -d "${verzeichnis}" ]; then
    info "${verzeichnis}/ ist vorhanden"
  elif [ "${NUR_PRUEFEN}" -eq 1 ]; then
    warnung "${verzeichnis}/ fehlt (würde angelegt)"
  else
    mkdir -p "${verzeichnis}"
    ok "${verzeichnis}/ angelegt"
  fi

  if [ -d "${verzeichnis}" ]; then
    besitzer="$(stat -c '%u:%g' "${verzeichnis}" 2> /dev/null || echo '?')"
    if [ "${besitzer}" = "10000:10000" ]; then
      ok "${verzeichnis}/ gehört 10000:10000"
    elif [ "${NUR_PRUEFEN}" -eq 1 ]; then
      warnung "${verzeichnis}/ gehört ${besitzer} (würde auf 10000:10000 gesetzt)"
    else
      if chown -R 10000:10000 "${verzeichnis}" 2> /dev/null; then
        ok "${verzeichnis}/ auf 10000:10000 gesetzt"
      else
        warnung "${verzeichnis}/ liess sich nicht umschreiben. Bitte von Hand:"
        info "sudo chown -R 10000:10000 ${HIER}/${verzeichnis}"
      fi
    fi
  fi
done

# =============================================================================
#  3. Secrets in der .env
# =============================================================================
titel "Secrets"

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    if [ "${NUR_PRUEFEN}" -eq 1 ]; then
      warnung ".env fehlt (würde aus .env.example erzeugt)"
    else
      cp .env.example .env
      ok ".env aus .env.example erzeugt"
    fi
  else
    abbruch "Weder .env noch .env.example vorhanden."
  fi
fi

erzeuge_secret() {
  if command -v openssl > /dev/null 2>&1; then
    openssl rand -base64 24
  else
    # openssl ist fast immer da; falls doch nicht, taugt urandom genauso.
    head -c 24 /dev/urandom | base64
  fi
}

if [ -f .env ]; then
  ERSETZT=""
  OFFEN=""
  TMP_ENV=".env.neu.$$"

  # Zeilenweise neu schreiben statt sed: ein base64-Secret enthält '/', '+'
  # und '=', und jeder dieser Werte würde ein naives 's/alt/neu/' zerlegen.
  : > "${TMP_ENV}"
  while IFS= read -r zeile || [ -n "${zeile}" ]; do
    schluessel="${zeile%%=*}"
    wert="${zeile#*=}"
    case "${zeile}" in
      \#*|"") printf '%s\n' "${zeile}" >> "${TMP_ENV}"; continue ;;
    esac
    case "${wert}" in
      ERSETZEN*)
        if [ "${NUR_PRUEFEN}" -eq 1 ]; then
          OFFEN="${OFFEN} ${schluessel}"
          printf '%s\n' "${zeile}" >> "${TMP_ENV}"
        else
          neuer="$(erzeuge_secret)"
          printf '%s=%s\n' "${schluessel}" "${neuer}" >> "${TMP_ENV}"
          ERSETZT="${ERSETZT} ${schluessel}"
        fi
        ;;
      *) printf '%s\n' "${zeile}" >> "${TMP_ENV}" ;;
    esac
  done < .env

  if [ "${NUR_PRUEFEN}" -eq 1 ]; then
    rm -f "${TMP_ENV}"
    if [ -n "${OFFEN}" ]; then
      warnung "Noch auf Platzhalter:${OFFEN}"
    else
      ok "Alle Secrets sind gesetzt"
    fi
  else
    mv "${TMP_ENV}" .env
    chmod 600 .env 2> /dev/null || true
    if [ -n "${ERSETZT}" ]; then
      # Nur die NAMEN, niemals die Werte.
      ok "Neue Zufallswerte gesetzt für:${ERSETZT}"
    else
      ok "Alle Secrets waren bereits gesetzt"
    fi
  fi
fi

# .env einlesen, um ICE_PORT und LISTEN_PORT zu kennen.
ICE_PORT="$(grep -E '^ICE_PORT=' .env 2> /dev/null | head -1 | cut -d= -f2- || true)"
LISTEN_PORT="$(grep -E '^LISTEN_PORT=' .env 2> /dev/null | head -1 | cut -d= -f2- || true)"
ADMIN_USER="$(grep -E '^ADMIN_USER=' .env 2> /dev/null | head -1 | cut -d= -f2- || true)"
[ -n "${ICE_PORT}" ] || ICE_PORT="6502"
[ -n "${LISTEN_PORT}" ] || LISTEN_PORT="8080"
[ -n "${ADMIN_USER}" ] || ADMIN_USER="admin"

if [ "${NUR_PRUEFEN}" -eq 1 ]; then
  titel "Zusammenfassung (--check)"
  if [ "${PROBLEME}" -eq 0 ]; then
    ok "Keine blockierenden Probleme gefunden."
    info "Ohne --check würde jetzt der Mumble-Server gestartet und provisioniert."
  else
    fehler "${PROBLEME} Problem(e) gefunden – siehe oben."
  fi
  exit "$([ "${PROBLEME}" -eq 0 ] && echo 0 || echo 1)"
fi

# =============================================================================
#  4. Mumble-Server starten
# =============================================================================
titel "Mumble-Server starten"

${COMPOSE} up -d mumble-server
ok "Container gestartet"

port_offen() {
  if (exec 3<> "/dev/tcp/127.0.0.1/$1") 2> /dev/null; then
    exec 3<&- 2> /dev/null || true
    exec 3>&- 2> /dev/null || true
    return 0
  fi
  command -v nc > /dev/null 2>&1 && nc -z 127.0.0.1 "$1" > /dev/null 2>&1
}

printf '    %s·%s Warte auf die Ice-Schnittstelle auf Port %s ' "${C_BLAU}" "${C_AUS}" "${ICE_PORT}"
VERSUCHE=0
while [ "${VERSUCHE}" -lt 60 ]; do
  if port_offen "${ICE_PORT}"; then break; fi
  printf '.'
  sleep 1
  VERSUCHE=$((VERSUCHE + 1))
done
printf '\n'

if ! port_offen "${ICE_PORT}"; then
  fehler "Port ${ICE_PORT} antwortet nach 60 Sekunden nicht."
  info "Logs ansehen mit:  ${COMPOSE} logs --tail 80 mumble-server"
  info "Häufigste Ursache: 'ice' und 'icesecretwrite' fehlen in der"
  info "Serverkonfiguration, oder ./server gehört nicht 10000:10000."
  abbruch "Ohne Ice kann das Admin-GUI nichts steuern."
fi
ok "Ice antwortet auf 127.0.0.1:${ICE_PORT}"

# =============================================================================
#  5. Serverversion auslesen
# =============================================================================
titel "Serverversion"

VERSION_ROH=""
for binaer in mumble-server murmurd murmur; do
  if VERSION_ROH="$(docker exec mumble-server "${binaer}" --version 2> /dev/null)"; then
    [ -n "${VERSION_ROH}" ] && break
  fi
  VERSION_ROH=""
done

if [ -n "${VERSION_ROH}" ]; then
  # Aus "mumble-server 1.5.735" o. Ä. die reine Versionsnummer schneiden.
  VERSION_NUM="$(printf '%s' "${VERSION_ROH}" | tr ' ' '\n' \
                 | grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' | head -1 | sed 's/^v//')"
else
  VERSION_NUM=""
fi

if [ -n "${VERSION_NUM}" ]; then
  ok "Server meldet Version ${VERSION_NUM}"
  GEWUENSCHT="v${VERSION_NUM}"
  AKTUELL="$(grep -E '^MUMBLE_VERSION=' .env | head -1 | cut -d= -f2- || true)"
  if [ "${AKTUELL}" != "${GEWUENSCHT}" ]; then
    warnung "MUMBLE_VERSION steht auf '${AKTUELL}', der Server läuft als '${GEWUENSCHT}'."
    info "Wird angeglichen – die Slice-Datei für das Admin-Image kommt aus diesem Tag."
    TMP_ENV=".env.neu.$$"
    while IFS= read -r zeile || [ -n "${zeile}" ]; do
      case "${zeile}" in
        MUMBLE_VERSION=*) printf 'MUMBLE_VERSION=%s\n' "${GEWUENSCHT}" >> "${TMP_ENV}" ;;
        *) printf '%s\n' "${zeile}" >> "${TMP_ENV}" ;;
      esac
    done < .env
    mv "${TMP_ENV}" .env
    ok "MUMBLE_VERSION=${GEWUENSCHT} gesetzt"
  else
    ok "MUMBLE_VERSION passt bereits"
  fi
else
  warnung "Die Serverversion liess sich nicht auslesen."
  info "MUMBLE_VERSION aus der .env wird unverändert verwendet."
  info "Weicht sie ab, meldet das Cockpit es als Banner."
fi

# =============================================================================
#  6. Admin-GUI bauen und starten
# =============================================================================
titel "Oberfläche starten"
# Erst versuchen, das fertige Image zu ziehen -- es wird von GitHub fuer
# x86-64 und arm64 gebaut. Nur wenn das nicht klappt (eigene Abspaltung ohne
# Registry, kein Internet, geaenderter Quelltext), wird selbst gebaut.
if ${COMPOSE} pull mumble-admin > /dev/null 2>&1; then
  ok "Fertiges Image geladen – es muss nichts gebaut werden"
  ${COMPOSE} up -d
else
  info "Kein fertiges Image verfügbar – es wird lokal gebaut."
  info "Das dauert beim ersten Mal ein paar Minuten."
  ${COMPOSE} up -d --build
fi
ok "mumble-admin gestartet"

printf '    %s·%s Warte auf das GUI auf Port %s ' "${C_BLAU}" "${C_AUS}" "${LISTEN_PORT}"
VERSUCHE=0
GUI_DA=1
while [ "${VERSUCHE}" -lt 90 ]; do
  if curl -fsS "http://127.0.0.1:${LISTEN_PORT}/healthz" > /dev/null 2>&1; then
    GUI_DA=0
    break
  fi
  printf '.'
  sleep 1
  VERSUCHE=$((VERSUCHE + 1))
done
printf '\n'

if [ "${GUI_DA}" -ne 0 ]; then
  fehler "Das GUI antwortet nicht auf http://127.0.0.1:${LISTEN_PORT}/healthz"
  info "Logs:  ${COMPOSE} logs --tail 120 mumble-admin"
  abbruch "Einrichtung unvollständig."
fi
ok "GUI antwortet"

# =============================================================================
#  7. Provisionierung
# =============================================================================
titel "Plätze"

# Hier wird bewusst NICHTS angelegt.
#
# Der Server ist die Wahrheit, nicht eine Datei: was in der Oberfläche
# entsteht, bleibt dort. Eine Einrichtung, die ungefragt elf Kanäle mit
# fremden Namen hinstellt, nimmt genau diese Entscheidung vorweg -- und man
# räumt erst mal auf, bevor man anfangen kann.
#
# Stattdessen steht in der Oberfläche unter „Einrichten" ein Baukasten bereit:
# ein Knopf, vorher ein Testlauf, der zeigt was entsteht, und danach gehört
# alles dir.
ok "Der Server bleibt leer – angelegt wird gleich in der Oberfläche"

# =============================================================================
#  8. Abschluss
# =============================================================================
titel "Fertig"

HOST_IP="$(ip route get 1.1.1.1 2> /dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
[ -n "${HOST_IP}" ] || HOST_IP="$(hostname -I 2> /dev/null | awk '{print $1}' || true)"
[ -n "${HOST_IP}" ] || HOST_IP="<rechner-ip>"

printf '\n'
printf '    %sOberfläche%s  http://%s:%s/\n' "${C_FETT}" "${C_AUS}" "${HOST_IP}" "${LISTEN_PORT}"
printf '    %sAnmeldung%s   %s / Passwort steht in der .env als ADMIN_PASSWORD\n' \
       "${C_FETT}" "${C_AUS}" "${ADMIN_USER}"
printf '\n'
printf '    %s─── So geht es weiter ────────────────────────────────────%s\n' "${C_BLAU}" "${C_AUS}"
printf '      1  Einrichten   einen Baukasten anwenden (erst Testlauf)\n'
printf '      2  –            alle einmal mit Mumble verbinden lassen\n'
printf '      3  Personen     jeden registrieren – der Server erkennt Leute\n'
printf '                      am Zertifikat, nicht am Namen\n'
printf '      4  Pult         per Ziehen in Rollen und auf Plätze, Rechte\n'
printf '                      verteilen\n'
printf '      5  Einrichten   Sicherung herunterladen\n'
printf '\n'
printf '      Die Seite „Anleitung“ erklärt Mumbles Modell in Klartext –\n'
printf '      vor allem, warum Rechte am Platz hängen und nicht an der Person.\n'
printf '\n'
printf '    %s─── Im Betrieb ───────────────────────────────────────────%s\n' "${C_BLAU}" "${C_AUS}"
printf '      %s ps                     was läuft\n' "${COMPOSE}"
printf '      %s logs -f mumble-admin   mitlesen\n' "${COMPOSE}"
printf '      %s stop                   anhalten, Daten bleiben\n' "${COMPOSE}"
printf '      %s down                   beenden, Daten bleiben\n' "${COMPOSE}"
printf '      %s up -d                  wieder hoch\n' "${COMPOSE}"
printf '\n'
printf '      Immer aus %s – sonst findet Compose die Datei nicht.\n' "$(pwd)"
printf '\n'
printf '    %s─── Bitte beachten ───────────────────────────────────────%s\n' "${C_BLAU}" "${C_AUS}"
printf '      Unverschlüsselt. Passwort und Sitzungscookie gehen im Klartext\n'
printf '      über das Netz – für ein abgeschlossenes Stadionnetz vertretbar,\n'
printf '      sonst nicht. Hängt der Rechner noch woanders, LISTEN_HOST in der\n'
printf '      .env auf die Netzkarte des Stadionnetzes setzen statt 0.0.0.0.\n'
printf '\n'
printf '      Das SuperUser-Passwort (MUMBLE_SUPERUSER_PASSWORD in der .env)\n'
printf '      wird für die Oberfläche nicht gebraucht – nur, falls sich jemand\n'
printf '      direkt mit dem Mumble-Programm als SuperUser anmelden will.\n'
printf '\n'

if [ "${PROBLEME}" -gt 0 ]; then
  warnung "${PROBLEME} Hinweis(e) oben beachten."
  exit 1
fi
exit 0
