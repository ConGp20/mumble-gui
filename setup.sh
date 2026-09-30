#!/usr/bin/env bash
# =============================================================================
#  Stadion-Intercom – Einrichtung
#
#  Bringt einen frischen Docker-Host von null auf ein laufendes Intercom:
#  Verzeichnisse, Secrets, Mumble-Server, Admin-GUI, Provisionierung.
#
#  Idempotent: ein zweiter Lauf ändert nur, was noch nicht stimmt.
#
#  Bewusst sparsames Bash: ohne mapfile/readarray, ohne assoziative Arrays und
#  ohne GNU-eigene sed-Schalter. Läuft damit auf Debian, Ubuntu, Raspberry Pi
#  OS und auch auf den abgespeckten Bash-Fassungen mancher NAS-Systeme.
#
#      ./setup.sh            komplette Einrichtung
#      ./setup.sh --check    nur prüfen, nichts ändern
#      ./setup.sh --yes      ohne Rückfragen
#      ./setup.sh --help     Kurzhilfe
# =============================================================================
set -euo pipefail

HIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HIER}"

NUR_PRUEFEN=0
OHNE_RUECKFRAGE=0
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
    --yes|-y) OHNE_RUECKFRAGE=1 ;;
    --help|-h) hilfe ;;
    *) abbruch "Unbekannte Option ${arg}. --help zeigt die Möglichkeiten." ;;
  esac
done

frage() {
  # $1 = Frage. Rückgabe 0 = ja.
  [ "${OHNE_RUECKFRAGE}" -eq 1 ] && return 0
  if [ ! -t 0 ]; then
    warnung "Keine Rückfrage möglich (kein Terminal). Mit --yes erneut aufrufen."
    return 1
  fi
  local antwort
  printf '    %s?%s %s [j/N] ' "${C_GELB}" "${C_AUS}" "$1"
  read -r antwort
  case "${antwort}" in [jJyY]*) return 0 ;; *) return 1 ;; esac
}

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

for datei in docker-compose.yml intercom.yaml; do
  [ -f "${datei}" ] || abbruch "${datei} fehlt. Wird dieses Skript im Projektverzeichnis ausgeführt?"
done
ok "docker-compose.yml und intercom.yaml vorhanden"

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
titel "Admin-GUI bauen und starten"
info "Der erste Bau dauert ein paar Minuten – Abhängigkeiten werden geladen."

${COMPOSE} --profile gui up -d --build
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
titel "Provisionierung"

set +e
${COMPOSE} exec -T mumble-admin intercom plan --detailed-exitcode
PLAN_CODE=$?
set -e

case "${PLAN_CODE}" in
  0) ok "Der Server entspricht bereits der intercom.yaml – nichts zu tun." ;;
  3)
    if frage "Diese Änderungen jetzt anwenden?"; then
      ${COMPOSE} exec -T mumble-admin intercom apply --yes
      ok "Provisionierung angewendet"
    else
      warnung "Nicht angewendet. Später jederzeit möglich mit:"
      info "${COMPOSE} exec mumble-admin intercom apply"
    fi
    ;;
  2) fehler "Die intercom.yaml ist fehlerhaft – siehe Meldungen oben." ;;
  *) fehler "Der Plan ist fehlgeschlagen (Rückgabewert ${PLAN_CODE})." ;;
esac

# =============================================================================
#  8. Abschluss
# =============================================================================
titel "Fertig"

HOST_IP="$(ip route get 1.1.1.1 2> /dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
[ -n "${HOST_IP}" ] || HOST_IP="$(hostname -I 2> /dev/null | awk '{print $1}' || true)"
[ -n "${HOST_IP}" ] || HOST_IP="<rechner-ip>"

printf '\n'
printf '    %sGUI%s        http://%s:%s/\n' "${C_FETT}" "${C_AUS}" "${HOST_IP}" "${LISTEN_PORT}"
printf '    %sBenutzer%s   %s (Passwort steht in der .env als ADMIN_PASSWORD)\n' \
       "${C_FETT}" "${C_AUS}" "${ADMIN_USER}"
printf '\n'
printf '    %sSuperUser%s  Das Mumble-SuperUser-Passwort steht in der .env als\n' "${C_FETT}" "${C_AUS}"
printf '               MUMBLE_SUPERUSER_PASSWORD. Es wird für das Admin-GUI\n'
printf '               NICHT gebraucht – nur, falls jemand sich direkt mit dem\n'
printf '               Mumble-Client als SuperUser anmelden will. Ändern geht\n'
printf '               im GUI unter „Nutzer“.\n'
printf '\n'
printf '    %sNur HTTP%s   Das Cockpit läuft unverschlüsselt. Passwort und\n' "${C_FETT}" "${C_AUS}"
printf '               Sitzungscookie gehen im Klartext über das Netz. Das ist\n'
printf '               für ein abgeschlossenes Stadionnetz vertretbar, aber nur\n'
printf '               dann. LISTEN_HOST in der .env auf die Netzkarte des\n'
printf '               Stadionnetzes setzen, nicht 0.0.0.0 stehen lassen, wenn\n'
printf '               der Rechner noch woanders hängt.\n'
printf '\n'
printf '    Weiter:    %s exec mumble-admin intercom status\n' "${COMPOSE}"
printf '               %s logs -f mumble-admin\n' "${COMPOSE}"
printf '\n'

if [ "${PROBLEME}" -gt 0 ]; then
  warnung "${PROBLEME} Hinweis(e) oben beachten."
  exit 1
fi
exit 0
