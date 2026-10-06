#!/usr/bin/env bash
# nest-to-ha.sh – przepina termostat Nest z firmware NoLongerEvil na integrację
# Nest Local w Home Assistancie.
#
# Najpierw sprawdza, czy integracja odpowiada i czy termostat jest osiągalny.
# Potem ustawia jego cloudregisterurl przez lokalne API firmware NLE (port 8080),
# a gdy API nie ma – przez SSH. Poprzedni adres zapisuje w ~/nest-backup-<numer>.txt,
# skąd przywraca go opcja --restore.
#
# Działa na macOS (bash 3.2) i Linuksie. Wymaga curl, a do zmiany przez SSH – ssh.
# Zmienne NEST_* poniżej służą głównie testom.

set -u

HA_IP="${NEST_HA_IP:-192.168.1.10}"
HA_PORT="${NEST_HA_PORT:-9544}"
API_PORT="${NEST_API_PORT:-8080}"
SSH_PORT="${NEST_SSH_PORT:-22}"
POLL_INTERVAL="${NEST_POLL_INTERVAL:-10}"
POLL_TRIES="${NEST_POLL_TRIES:-30}"
NEST_IP=""
SERIAL=""
MODE="switch"
ASSUME_YES=0

HA_OK=0
PING_OK=0
MAC=""
API_STATE="" # ok | refused | timeout | unreachable
API_OK=0
SSH_STATE=""
CURRENT_URL=""
API_KEY=""
KEEPALIVE_PID=""

if [ -t 1 ]; then
  C_OK=$'\033[32m'
  C_WARN=$'\033[33m'
  C_ERR=$'\033[31m'
  C_B=$'\033[1m'
  C_0=$'\033[0m'
else
  C_OK=""
  C_WARN=""
  C_ERR=""
  C_B=""
  C_0=""
fi

say() { printf '%s\n' "$*"; }
ok() { printf '  %s✔%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_0" "$*"; }
fail() { printf '  %s✘%s %s\n' "$C_ERR" "$C_0" "$*"; }
section() { printf '\n%s%s%s\n' "$C_B" "$*" "$C_0"; }
die() {
  fail "$*"
  exit 1
}

usage() {
  cat <<'EOF'
Przepina termostat Nest z firmware NoLongerEvil na integrację Nest Local w Home Assistancie.

Użycie: nest-to-ha.sh [opcje] [IP_TERMOSTATU]

  IP_TERMOSTATU      adres z listy DHCP; bez niego skrypt szuka termostatu w sieci
  -a, --ha IP        adres Home Assistanta (domyślnie 192.168.1.10)
  -p, --port PORT    port integracji Nest Local (domyślnie 9544)
  -s, --serial SN    numer seryjny termostatu, czyli jego nazwa hosta z DHCP
      --check        tylko diagnostyka, niczego nie zmienia
      --restore      przywraca adres zapisany w ~/nest-backup-<numer>.txt
  -y, --yes          bez pytań (obudź termostat przed uruchomieniem)
  -h, --help         ta pomoc

Przykłady:
  nest-to-ha.sh 192.168.1.50            diagnostyka i przepięcie na HA
  nest-to-ha.sh --check 192.168.1.50    sama diagnostyka
  nest-to-ha.sh --restore               powrót do poprzedniego serwera
EOF
}

# ------------------------------------------------------------------ helpers

is_ipv4() {
  case "$1" in '' | *[!0-9.]*) return 1 ;; esac
  local IFS=.
  # shellcheck disable=SC2086
  set -- $1
  [ $# -eq 4 ] || return 1
  local octet
  for octet in "$@"; do
    [ -n "$octet" ] || return 1
    [ "$octet" -le 255 ] 2>/dev/null || return 1
  done
}

is_port() {
  case "$1" in '' | *[!0-9]*) return 1 ;; esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

tmpfile() { mktemp "${TMPDIR:-/tmp}/nest-to-ha.XXXXXX"; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  local answer
  printf '%s [t/N] ' "$1"
  read -r answer || return 1
  case "$answer" in t | T | tak | TAK | y | Y | yes) return 0 ;; esac
  return 1
}

if [ "$(uname -s)" = "Darwin" ]; then
  PING_ONE="-c 1 -t 2"
else
  PING_ONE="-c 1 -W 2"
fi

ping_once() {
  command -v ping >/dev/null 2>&1 || return 1
  # shellcheck disable=SC2086
  ping $PING_ONE "$1" >/dev/null 2>&1
}

# Prints ok | refused | timeout | unreachable for a TCP port (3 s limit, bash only).
tcp_state() {
  local errf pid killer rc
  errf=$(tmpfile)
  (exec 3<>"/dev/tcp/$1/$2") 2>"$errf" &
  pid=$!
  (sleep 3 && kill "$pid") >/dev/null 2>&1 &
  killer=$!
  wait "$pid" 2>/dev/null
  rc=$?
  kill "$killer" >/dev/null 2>&1
  wait "$killer" 2>/dev/null
  if [ "$rc" -eq 0 ]; then
    echo ok
  elif [ "$rc" -gt 128 ]; then
    echo timeout
  elif grep -qi refused "$errf"; then
    echo refused
  else
    echo unreachable
  fi
  rm -f "$errf"
}

mac_of() {
  arp -an 2>/dev/null | grep -F "($1)" | sed -nE 's/.* at ([0-9a-fA-F:]+) .*/\1/p' | head -n 1
}

is_nest_mac() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    18:b4:30:* | 64:16:66:*) return 0 ;;
  esac
  return 1
}

json_field() {
  printf '%s' "$1" | sed -n "s/.*\"$2\" *: *\"\([^\"]*\)\".*/\1/p" | head -n 1 | sed 's|\\/|/|g'
}

api_url() { printf 'http://%s:%s/cgi-bin/api/settings' "$NEST_IP" "$API_PORT"; }
api_get() { curl -s --noproxy '*' -m 6 "$(api_url)" 2>/dev/null; }
api_post() {
  curl -s --noproxy '*' -m 15 -X POST -H 'Content-Type: application/json' \
    --data "$1" "$(api_url)" 2>/dev/null
}

backup_file() { printf '%s/nest-backup-%s.txt' "$HOME" "$(upper "$1")"; }

# A sleeping Nest drops off Wi-Fi; steady traffic keeps it awake while we work.
keepalive_start() {
  command -v ping >/dev/null 2>&1 || return 0
  ping -i 1 "$NEST_IP" >/dev/null 2>&1 &
  KEEPALIVE_PID=$!
}

keepalive_stop() {
  [ -n "$KEEPALIVE_PID" ] || return 0
  kill "$KEEPALIVE_PID" >/dev/null 2>&1
  KEEPALIVE_PID=""
}

macos_lan_hint() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  say "    macOS: w Ustawieniach systemowych > Prywatność i ochrona > Sieć lokalna aplikacja,"
  say "    w której uruchamiasz skrypt (Terminal, iTerm, VS Code), musi mieć włączony dostęp."
}

reflash_hint() {
  say "    Wgraj aktualny firmware instalatorem NoLongerEvil:"
  say "    https://github.com/codykociemba/NoLongerEvil-Thermostat/releases"
  if [ "$MODE" = "restore" ]; then
    say "    W kreatorze wybierz serwer, którego używałeś wcześniej."
  else
    say "    W kreatorze wybierz Self-Hosted, potem zakładkę NLE Server (nie Home Assistant –"
    say "    ta zawsze ustawia port 9543) i wpisz IP $HA_IP, port $HA_PORT."
  fi
}

# ---------------------------------------------------------- Home Assistant

check_ha() {
  section "Home Assistant"
  local resp
  resp=$(curl -s --noproxy '*' -m 5 "http://$HA_IP:$HA_PORT/nest/ping" 2>/dev/null)
  case "$resp" in
    *'"status"'*'"ok"'*)
      HA_OK=1
      ok "Integracja Nest Local odpowiada pod http://$HA_IP:$HA_PORT"
      return 0
      ;;
  esac
  fail "Integracja Nest Local nie odpowiada pod http://$HA_IP:$HA_PORT"
  case "$(tcp_state "$HA_IP" "$HA_PORT")" in
    refused)
      say "    Port $HA_PORT jest zamknięty: integracja nie jest dodana w HA albo używa innego"
      say "    portu (opcja -p)."
      ;;
    ok) say "    Na porcie $HA_PORT odpowiada coś innego niż integracja Nest Local (opcja -p)." ;;
    timeout) say "    $HA_IP nie odpowiada – sprawdź adres HA (opcja -a)." ;;
    *)
      say "    Brak połączenia z $HA_IP – sprawdź adres HA (opcja -a) i czy ten komputer"
      say "    jest w tej samej sieci."
      macos_lan_hint
      ;;
  esac
  return 1
}

wait_for_ha() {
  if [ -z "$SERIAL" ]; then
    warn "Nie znam numeru seryjnego, więc nie sprawdzę, czy termostat połączył się z HA."
    return 0
  fi
  local limit=$((POLL_TRIES * ${POLL_INTERVAL%.*}))
  if [ "$limit" -ge 120 ]; then limit="$((limit / 60)) min"; else limit="$limit s"; fi
  say "  Czekam, aż termostat połączy się z Home Assistantem (do $limit)."
  say "  Ctrl+C przerywa czekanie – termostat i tak się połączy."
  local i=0 resp
  while [ "$i" -lt "$POLL_TRIES" ]; do
    resp=$(curl -s --noproxy '*' -m 5 \
      "http://$HA_IP:$HA_PORT/nest/transport/device/$(upper "$SERIAL")" 2>/dev/null)
    case "$resp" in
      *object_key*)
        ok "Termostat $SERIAL połączył się z Home Assistantem."
        say "    Encje pojawią się w HA, gdy termostat prześle pełny stan. Jeśli po kilku"
        say "    minutach ich nie ma, zrestartuj termostat (przytrzymaj ekran ok. 10 s)."
        return 0
        ;;
    esac
    sleep "$POLL_INTERVAL"
    i=$((i + 1))
  done
  warn "Termostat jeszcze się nie zgłosił. Zrestartuj go (przytrzymaj ekran ok. 10 s)"
  say "    i sprawdź HA za kilka minut."
  return 1
}

# --------------------------------------------------------------- thermostat

wake_prompt() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  section "Obudź termostat"
  say "  Uśpiony Nest nie odpowiada w sieci. Naciśnij pierścień, otwórz Settings > Technical Info"
  say "  i nie pozwól zgasnąć ekranowi, dopóki skrypt pracuje (co kilka sekund przekręć pierścień)."
  printf '  Gdy ekran świeci, naciśnij Enter. '
  read -r _ || true
}

find_nest() {
  local prefix=${HA_IP%.*} i found count
  section "Szukam termostatu w sieci $prefix.0/24"
  if command -v ping >/dev/null 2>&1; then
    for i in $(seq 1 254); do
      ping_once "$prefix.$i" &
    done
    wait
  fi
  found=$(arp -an 2>/dev/null | grep -iE ' at (18:b4:30|64:16:66):' |
    sed -nE 's/^[^(]*\(([0-9.]+)\).*/\1/p' | sort -u)
  count=$(printf '%s' "$found" | grep -c . || true)
  if [ "$count" -eq 0 ]; then
    fail "Nie znalazłem urządzenia z adresem MAC Nest Labs (18:b4:30 / 64:16:66)."
    die "Podaj IP termostatu z listy DHCP: $0 IP_TERMOSTATU"
  fi
  if [ "$count" -gt 1 ]; then
    fail "Znalazłem kilka urządzeń Nest:"
    printf '%s\n' "$found" | sed 's/^/      /'
    die "Podaj IP właściwego termostatu jako argument."
  fi
  NEST_IP=$found
  ok "Termostat: $NEST_IP"
}

diagnose() {
  section "Termostat $NEST_IP"
  local _try json
  for _try in 1 2 3; do
    if ping_once "$NEST_IP"; then
      PING_OK=1
      break
    fi
  done
  MAC=$(mac_of "$NEST_IP")
  API_STATE=$(tcp_state "$NEST_IP" "$API_PORT")
  if [ "$API_STATE" = ok ]; then
    json=$(api_get)
    case "$json" in
      *cloudregisterurl*)
        API_OK=1
        CURRENT_URL=$(json_field "$json" cloudregisterurl)
        ;;
    esac
  fi
  SSH_STATE=$(tcp_state "$NEST_IP" "$SSH_PORT")

  if [ "$PING_OK" -eq 1 ]; then ok "odpowiada na ping"; else warn "nie odpowiada na ping"; fi
  if [ -n "$MAC" ]; then
    if is_nest_mac "$MAC"; then
      ok "adres MAC $MAC (Nest Labs)"
    else
      warn "adres MAC $MAC nie należy do Nest Labs – sprawdź, czy to dobry adres IP"
    fi
  fi
  if [ "$API_OK" -eq 1 ]; then
    ok "lokalne API NLE (port $API_PORT) działa, cloudregisterurl = ${CURRENT_URL:-?}"
  else
    case "$API_STATE" in
      ok) warn "port $API_PORT otwarty, ale to nie jest API NoLongerEvil" ;;
      refused) warn "brak lokalnego API NLE (port $API_PORT zamknięty)" ;;
      *) warn "port $API_PORT nie odpowiada" ;;
    esac
  fi
  case "$SSH_STATE" in
    ok) ok "SSH (port $SSH_PORT) otwarte" ;;
    refused) warn "SSH (port $SSH_PORT) zamknięte" ;;
    *) warn "SSH (port $SSH_PORT) nie odpowiada" ;;
  esac
}

explain_unreachable() {
  if [ "$API_STATE" = ok ] && [ "$API_OK" -eq 0 ]; then
    fail "Pod $NEST_IP:$API_PORT odpowiada coś innego niż API NoLongerEvil."
    say "    Sprawdź, czy to adres termostatu (lista DHCP albo Settings > Technical Info > Network)."
  elif [ "$API_STATE" = refused ] || [ "$SSH_STATE" = refused ]; then
    fail "Termostat odpowiada, ale nie ma lokalnego API NLE ani SSH – to starsza wersja"
    say "    firmware NoLongerEvil."
    reflash_hint
  elif [ "$PING_OK" -eq 1 ]; then
    fail "Termostat odpowiada na ping, ale nie na porty $API_PORT i $SSH_PORT."
    say "    Mógł znowu zasnąć – obudź go i uruchom skrypt ponownie. Jeśli to się powtarza:"
    reflash_hint
  elif [ -n "$MAC" ]; then
    fail "Termostat jest w sieci (adres MAC $MAC), ale nie odpowiada – najpewniej śpi."
    say "    Obudź go, nie pozwól zgasnąć ekranowi i uruchom skrypt ponownie."
  else
    fail "Termostat jest nieosiągalny z tego komputera."
    say "    Możliwe przyczyny: termostat śpi (obudź go i uruchom skrypt ponownie), jest w innej"
    say "    sieci Wi-Fi (np. gościnnej) albo router blokuje ruch między urządzeniami."
    say "    Sprawdź w DHCP, czy adres termostatu jest w tej samej podsieci co HA ($HA_IP) –"
    say "    termostat musi móc połączyć się z HA niezależnie od tego skryptu."
    if [ "$HA_OK" -eq 0 ] && [ "$API_STATE" = unreachable ]; then macos_lan_hint; fi
  fi
}

guess_serial() {
  local name=""
  if command -v host >/dev/null 2>&1; then
    name=$(host -W 2 "$NEST_IP" 2>/dev/null | sed -n 's/.*pointer \(.*\)$/\1/p' | head -n 1)
  fi
  if [ -z "$name" ] && command -v dscacheutil >/dev/null 2>&1; then
    name=$(dscacheutil -q host -a ip_address "$NEST_IP" 2>/dev/null | sed -n 's/^name: //p' | head -n 1)
  fi
  name=${name%.}
  name=${name%%.*}
  case "$name" in '' | *[!0-9A-Za-z]*) return 1 ;; esac
  [ "${#name}" -ge 10 ] || return 1
  printf '%s' "$name"
}

# Exchange the serial (the thermostat's hostname) for the key of the local API.
try_initialize() {
  local cand resp
  for cand in "$1" "$(upper "$1")"; do
    [ -n "$cand" ] || continue
    resp=$(api_post "{\"initialize\":\"$cand\"}")
    API_KEY=$(json_field "$resp" api_key)
    if [ -n "$API_KEY" ]; then
      SERIAL=$cand
      return 0
    fi
  done
  return 1
}

get_api_key() {
  if [ -n "$SERIAL" ] && try_initialize "$SERIAL"; then return 0; fi
  # First-boot window of the firmware: within 30 minutes of a reboot, while the
  # thermostat still points at the NoLongerEvil cloud, it hands out the key once.
  local resp
  resp=$(api_post '{"setup":"true"}')
  API_KEY=$(json_field "$resp" api_key)
  if [ -n "$API_KEY" ]; then
    SERIAL=$(json_field "$resp" device_name)
    return 0
  fi
  [ "$ASSUME_YES" -eq 1 ] && return 1
  say "  Podaj numer seryjny termostatu. To jego nazwa hosta na liście DHCP; jest też"
  say "  w Settings > Technical Info na termostacie."
  local attempt answer
  for attempt in 1 2 3; do
    printf '  Numer seryjny (próba %s/3): ' "$attempt"
    read -r answer || return 1
    answer=$(printf '%s' "$answer" | tr -d '[:space:]-')
    [ -n "$answer" ] || continue
    if try_initialize "$answer"; then return 0; fi
    warn "Termostat nie przyjął numeru \"$answer\"."
  done
  return 1
}

set_endpoint_api() {
  local resp status
  resp=$(api_post "{\"api_key\":\"$API_KEY\",\"endpoint\":\"$1\"}")
  status=$(json_field "$resp" status)
  if [ "$status" = "new" ]; then
    [ -n "$SERIAL" ] || SERIAL=$(json_field "$resp" device_name)
    return 0
  fi
  fail "Termostat odrzucił zmianę: ${resp:-brak odpowiedzi}"
  return 1
}

# Runs on the thermostat (busybox sh). Prints SERIAL=, OLD= and NEW= lines and
# reboots only if the new address is in place.
remote_script() {
  cat <<'REMOTE'
F=/etc/nestlabs/client.config
SED=sed
if command -v busybox2 >/dev/null 2>&1; then SED="busybox2 sed"; fi
cur() { grep cloudregisterurl "$F" | $SED -n 's/.*value="\([^"]*\)".*/\1/p' | head -n 1; }
echo "SERIAL=$(hostname)"
echo "OLD=$(cur)"
[ -f "$F.before-ha" ] || cp "$F" "$F.before-ha"
ESC=$(printf '%s' "$NEW_URL" | $SED 's/[&|]/\\&/g')
$SED "s|<a key=\"cloudregisterurl\" value=\"[^\"]*\"|<a key=\"cloudregisterurl\" value=\"$ESC\"|g" "$F" >/tmp/client.config.new &&
  grep -qF "\"$NEW_URL\"" /tmp/client.config.new &&
  cat /tmp/client.config.new >"$F"
rm -f /tmp/client.config.new
echo "NEW=$(cur)"
if [ "$(cur)" = "$NEW_URL" ]; then
  (trap '' HUP; sleep 3; reboot) </dev/null >/dev/null 2>&1 &
fi
REMOTE
}

set_url_ssh() {
  remote_script | ssh -p "$SSH_PORT" \
    -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o HostKeyAlgorithms=+ssh-rsa \
    "root@$NEST_IP" "NEW_URL='$1' sh -s"
}

save_backup() {
  [ -n "$SERIAL" ] || return 0
  [ -n "$1" ] || return 0
  local file
  file=$(backup_file "$SERIAL")
  if [ -f "$file" ]; then
    ok "Kopia poprzedniego adresu już istnieje: $file"
    return 0
  fi
  {
    printf 'serial=%s\n' "$SERIAL"
    printf 'ip=%s\n' "$NEST_IP"
    printf 'cloudregisterurl=%s\n' "$1"
    printf 'saved=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } >"$file"
  ok "Poprzedni adres zapisałem w $file"
}

# ------------------------------------------------------------------ actions

apply_url_api() {
  local target=$1 now
  case "$target" in
    */entry) ;;
    *)
      warn "Lokalne API ustawia tylko adresy kończące się na /entry."
      return 1
      ;;
  esac
  [ -n "$SERIAL" ] || SERIAL=$(guess_serial) || true
  if ! get_api_key; then
    fail "Termostat nie wydał klucza lokalnego API."
    say "    Uruchom skrypt ponownie z numerem seryjnym termostatu: -s NUMER"
    return 1
  fi
  ok "Klucz API uzyskany (termostat $SERIAL)"
  confirm "  Zmienić cloudregisterurl z ${CURRENT_URL:-?} na $target?" ||
    die "Przerwane, niczego nie zmieniłem."
  if [ "$MODE" = "switch" ]; then save_backup "$CURRENT_URL"; fi
  set_endpoint_api "${target%/entry}" || return 1
  sleep 1
  now=$(json_field "$(api_get)" cloudregisterurl)
  if [ "$now" = "$target" ]; then
    ok "Termostat ustawiony na $target (za ok. 10 s zrestartuje swoje oprogramowanie)"
  else
    warn "Termostat przyjął zmianę, ale zwraca adres: ${now:-?}"
  fi
  return 0
}

apply_url_ssh() {
  local target=$1 out old new
  confirm "  Zmienić cloudregisterurl przez SSH na $target? Termostat się zrestartuje." ||
    die "Przerwane, niczego nie zmieniłem."
  say "  Łączę się przez SSH jako root (domyślne hasło: nolongerevil)..."
  out=$(set_url_ssh "$target")
  [ -n "$SERIAL" ] || SERIAL=$(printf '%s\n' "$out" | sed -n 's/^SERIAL=//p' | head -n 1)
  old=$(printf '%s\n' "$out" | sed -n 's/^OLD=//p' | head -n 1)
  new=$(printf '%s\n' "$out" | sed -n 's/^NEW=//p' | head -n 1)
  if [ "$new" != "$target" ]; then
    fail "Zmiana przez SSH nie powiodła się."
    if [ -n "$out" ]; then printf '%s\n' "$out" | sed 's/^/      /'; fi
    return 1
  fi
  if [ "$MODE" = "switch" ] && [ "$old" != "$target" ]; then save_backup "$old"; fi
  ok "Termostat ustawiony na $target i się restartuje"
}

# Point the thermostat at $1: local API first, SSH when the API is missing or refuses.
apply_url() {
  local target=$1
  if [ "$API_OK" -eq 1 ]; then
    if [ "$CURRENT_URL" = "$target" ]; then
      ok "Termostat już wskazuje na $target"
      return 0
    fi
    apply_url_api "$target" && return 0
    [ "$SSH_STATE" = ok ] || return 1
    warn "Próbuję przez SSH."
  fi
  if [ "$SSH_STATE" = ok ]; then
    apply_url_ssh "$target"
    return
  fi
  explain_unreachable
  return 1
}

do_switch() {
  check_ha || exit 1
  wake_prompt
  [ -n "$NEST_IP" ] || find_nest
  keepalive_start
  diagnose
  section "Przepinanie na Home Assistanta"
  apply_url "http://$HA_IP:$HA_PORT/entry" || exit 1
  keepalive_stop
  wait_for_ha
}

do_check() {
  check_ha || true
  wake_prompt
  [ -n "$NEST_IP" ] || find_nest
  keepalive_start
  diagnose
  section "Wniosek"
  local how
  if [ "$API_OK" -eq 1 ] && [ "$CURRENT_URL" = "http://$HA_IP:$HA_PORT/entry" ]; then
    ok "Termostat już wskazuje na Home Assistanta."
  elif [ "$API_OK" -eq 1 ] || [ "$SSH_STATE" = ok ]; then
    if [ "$API_OK" -eq 1 ]; then how="przez lokalne API"; else how="przez SSH (hasło root)"; fi
    ok "Termostat da się przepiąć $how."
    if [ "$HA_OK" -eq 1 ]; then
      say "    Uruchom skrypt bez --check."
    else
      say "    Najpierw dodaj integrację Nest Local w HA, potem uruchom skrypt bez --check."
    fi
  else
    explain_unreachable
  fi
}

do_restore() {
  local file count original
  if [ -n "$SERIAL" ]; then
    file=$(backup_file "$SERIAL")
  else
    count=$(find "$HOME" -maxdepth 1 -name 'nest-backup-*.txt' 2>/dev/null | grep -c . || true)
    [ "$count" -ge 1 ] || die "Brak kopii nest-backup-*.txt w $HOME."
    [ "$count" -eq 1 ] || die "W $HOME jest kilka kopii – wskaż termostat opcją -s NUMER."
    file=$(find "$HOME" -maxdepth 1 -name 'nest-backup-*.txt' 2>/dev/null | head -n 1)
  fi
  [ -f "$file" ] || die "Brak kopii $file"
  original=$(sed -n 's/^cloudregisterurl=//p' "$file" | head -n 1)
  [ -n "$original" ] || die "Kopia $file nie zawiera adresu."
  [ -n "$SERIAL" ] || SERIAL=$(sed -n 's/^serial=//p' "$file" | head -n 1)
  [ -n "$NEST_IP" ] || NEST_IP=$(sed -n 's/^ip=//p' "$file" | head -n 1)
  is_ipv4 "$NEST_IP" || die "Kopia nie zawiera adresu IP termostatu – podaj go jako argument."
  section "Kopia"
  ok "$file"
  ok "poprzedni adres: $original"
  wake_prompt
  keepalive_start
  diagnose
  section "Przywracanie poprzedniego adresu"
  if ! apply_url "$original"; then
    say "    Inne drogi powrotu: przez SSH skopiuj na /etc/nestlabs/client.config plik"
    say "    client.config.before-ha (kopia sprzed zmiany przez SSH) albo client.config.old"
    say "    (sprzed ostatniej zmiany przez API) i zrestartuj termostat; albo instalator NLE."
    exit 1
  fi
}

# --------------------------------------------------------------------- main

while [ $# -gt 0 ]; do
  case "$1" in
    -a | --ha | -p | --port | -s | --serial)
      [ $# -ge 2 ] || die "Brak wartości dla $1"
      case "$1" in
        -a | --ha) HA_IP=$2 ;;
        -p | --port) HA_PORT=$2 ;;
        *) SERIAL=$(printf '%s' "$2" | tr -d '[:space:]-') ;;
      esac
      shift 2
      ;;
    --check)
      MODE=check
      shift
      ;;
    --restore)
      MODE=restore
      shift
      ;;
    -y | --yes)
      ASSUME_YES=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      usage >&2
      die "Nieznana opcja: $1"
      ;;
    *)
      NEST_IP=$1
      shift
      ;;
  esac
done

command -v curl >/dev/null 2>&1 || die "Brak programu curl."
is_ipv4 "$HA_IP" || die "Adres HA musi być adresem IPv4 (np. 192.168.1.10), a jest: $HA_IP"
is_port "$HA_PORT" || die "Nieprawidłowy port: $HA_PORT"
if [ -n "$NEST_IP" ]; then is_ipv4 "$NEST_IP" || die "Nieprawidłowy adres termostatu: $NEST_IP"; fi

trap keepalive_stop EXIT

case "$MODE" in
  check) do_check ;;
  restore) do_restore ;;
  *) do_switch ;;
esac
