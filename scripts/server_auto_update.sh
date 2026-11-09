#!/usr/bin/env bash
# Monthly update of the DE server that is safe to run when nobody is watching.
# The supervised version, with Docker and everything else, is server_update.sh.
#
#   bash scripts/server_auto_update.sh --dry-run   # what it would do now; changes nothing
#   bash scripts/server_auto_update.sh             # the update (the monthly timer runs this)
#
# It updates this, and only this:
#   * system packages, except Docker, containerd and runc. Nothing is removed,
#     changed config files are kept, nothing is restarted by the upgrade and the
#     server is not rebooted: a new kernel waits for you;
#   * the base images of the bot and the site. The two are rebuilt, through
#     update_bot.sh and update_web.sh (each keeps a rollback image), only when a
#     base image really changed AND the project folder holds exactly the code
#     that runs now (source_digest.sh). Otherwise nothing restarts.
# It never touches: Docker itself, Marzban, Xray and its geo files, the AmneziaWG
# container, the project's code (no git pull), SSH, the firewall.
#
# It starts only when everything is in order, and says why when it does not:
# no update, backup, apt or cleanup running; every container healthy; quiet
# hours (01:00-09:00 UTC); a scheduled backup exists and a fresh one is made
# right now; disk and memory to spare; the doctor (update_bot.sh --doctor)
# reports no problems. Afterwards it runs the doctor again. If the doctor finds
# something new after the rebuild, the bot and the site go back to the images
# they had. The owner gets one line in Telegram (NOTIFY=always|problems|never);
# the doctor shows the last run (scripts/auto_update_status.sh).
set -o pipefail

usage() {
  echo "usage: server_auto_update.sh [--dry-run]" >&2
  exit 2
}
[[ $# -le 1 ]] || usage
DRY=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ;;
  *) usage ;;
esac

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CONTAINER="${CONTAINER:-node-rental-bot-1}"
WEB_PROJECT="node-rental-web-local"
WEB_SERVICE="app-web"
BACKUP_ENV="${BACKUP_ENV:-/etc/node-backup.env}"
BACKUP_DIR="${BACKUP_DIR:-/root/server-backups}"
STATE_DIR="${STATE_DIR:-/var/lib/node-update}"
LOCK_FILE="${LOCK_FILE:-/run/lock/node-update.lock}"
REBOOT_FILE="${REBOOT_FILE:-/var/run/reboot-required}"
# Helper scripts; the defaults are the ones next to this file.
BOT_UPDATER="${BOT_UPDATER:-$SRC/scripts/update_bot.sh}"
WEB_UPDATER="${WEB_UPDATER:-$SRC/scripts/update_web.sh}"
BACKUP_RUNNER="${BACKUP_RUNNER:-$SRC/scripts/backup_all.sh}"
DIGEST_TOOL="${DIGEST_TOOL:-$SRC/scripts/source_digest.sh}"
# Settings come from the systemd unit, so each is checked: a typo must never
# widen what is done.
number() { if [[ "$1" =~ ^[0-9]+$ ]]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
WANT_FREE_MB="$(number "${WANT_FREE_MB:-}" 4096)"
MIN_AVAIL_MB="$(number "${MIN_AVAIL_MB:-}" 700)"
WINDOW_FROM_UTC="$(number "${WINDOW_FROM_UTC:-}" 1)"
WINDOW_TO_UTC="$(number "${WINDOW_TO_UTC:-}" 9)"
WAIT_CONTAINERS_SECONDS="$(number "${WAIT_CONTAINERS_SECONDS:-}" 240)"
WAIT_STEP_SECONDS="$(number "${WAIT_STEP_SECONDS:-}" 5)"
NOTIFY="${NOTIFY:-always}"
case "$NOTIFY" in always | problems | never) ;; *) NOTIFY=always ;; esac
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# A maintenance script of this project, started with bash or directly. This
# script itself is left out: the lock keeps two updates apart.
BUSY_PATTERN='bash +[^ ]*(server_update|update_bot|update_web|backup_all|restore_backup|ssh_hardening|server_cleanup)\.sh'
# Docker and its runtime are restarted by their own upgrade, which cuts every
# client for a minute: the owner chooses that moment (server_update.sh --apply).
HELD_BACK='^(docker|containerd|runc)([.-]|$)'

packages=0
images=skipped
reboot=no
blockers=()
notes=()
held=()
doctor_fails=""

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
free_mb() { df -Pm / 2>/dev/null | awk 'NR == 2 { print $4 }'; }
avail_mb() { free -m 2>/dev/null | awk 'NR == 2 { print $7 }'; }
running_containers() { docker ps --format '{{.Names}}' 2>/dev/null | sort; }
# Running containers that restart, report "unhealthy" or have not finished starting.
unhealthy_containers() {
  docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null \
    | awk -F'|' '$2 ~ /unhealthy|health: starting|Restarting/ { printf "%s ", $1 }' | sed 's/ *$//'
}

# What the doctor reads (scripts/auto_update_status.sh).
state_write() {  # state_write <ok|skipped|failed> <note>
  ((DRY)) && return 0
  local note="${2//$'\n'/ }" last_ok
  mkdir -p "$STATE_DIR" 2>/dev/null || return 0
  # A skipped run keeps the time of the last run that finished.
  last_ok="$(sed -n 's/^last_ok=//p' "$STATE_DIR/last-run" 2>/dev/null | head -n1)"
  [[ "$1" == ok ]] && last_ok="$(date +%s)"
  printf 'time=%s\nlast_ok=%s\nresult=%s\npackages=%s\nimages=%s\nreboot=%s\nnote=%s\n' \
    "$(date +%s)" "$last_ok" "$1" "$packages" "$images" "$reboot" "$note" >"$STATE_DIR/last-run.tmp" 2>/dev/null \
    && mv -f "$STATE_DIR/last-run.tmp" "$STATE_DIR/last-run" 2>/dev/null
  return 0
}
# A short note to the owner from the bot, best effort.
notify() {  # notify <always|problem> <text>
  ((DRY)) && return 0
  [[ "$NOTIFY" == never ]] && return 0
  [[ "$1" == always && "$NOTIFY" != always ]] && return 0
  docker exec "$CONTAINER" python scripts/send_backup.py text "$2" >/dev/null 2>&1 || true
}
# Nothing was changed: the next month's run tries again.
skip() {  # skip <reason> [alert <text for the owner>]
  say "AUTO_UPDATE_SKIPPED: $1"
  state_write skipped "$1"
  [[ "${2:-}" == alert ]] && notify problem "⚠️ ${3:-Автообновление сервера не запущено: $1}. Сервер не менялся."
  exit 0
}
# Something is wrong and has to be looked at.
die() {  # die <reason> <text for the owner>
  printf 'AUTO_UPDATE_FAILED: %s\n' "$1" >&2
  state_write failed "$1"
  notify problem "⚠️ $2 Проверьте: bash scripts/update_bot.sh --doctor"
  exit 1
}
# A reason not to start. The real run stops here; the dry run writes it down and goes on.
block() {  # block <reason> [alert <text for the owner>]
  if ((DRY)); then
    blockers+=("$1")
    say "  BLOCKED: $1"
  else
    skip "$@"
  fi
}

doctor() {  # prints what is not OK; returns the doctor's status
  local out status
  out="$(bash "$BOT_UPDATER" --doctor 2>&1)"
  status=$?
  printf '%s\n' "$out" | grep -E '^(FAIL|WARN)' | sed 's/^/  /'
  printf '%s\n' "$out" | tail -n 1 | sed 's/^/  /'
  doctor_fails="$(printf '%s\n' "$out" | grep -E '^FAIL' | head -n 3 | sed 's/^FAIL *//' | tr '\n' ';' | sed 's/;$//')"
  return "$status"
}

wait_containers() {  # wait_containers <names...>: until each runs (and is healthy if it can be)
  local deadline=$((SECONDS + WAIT_CONTAINERS_SECONDS)) name state missing
  while ((SECONDS < deadline)); do
    missing=()
    for name in "$@"; do
      state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null)"
      [[ "$state" == "running " || "$state" == "running healthy" ]] || missing+=("$name")
    done
    ((${#missing[@]} == 0)) && return 0
    sleep "$WAIT_STEP_SECONDS"
  done
  for name in "${missing[@]}"; do say "  not back: $name"; done
  return 1
}

if ((DRY)); then
  say "== AUTO UPDATE DRY RUN: what the monthly update would do now; nothing is changed"
else
  say "== AUTO UPDATE: the monthly update"
fi
say "source: $SRC $(git -C "$SRC" log -1 --format='%h %s' 2>/dev/null)"
say "time: $STAMP"

# ------------------------------------------------------------------ checks
step "Checks before anything is changed"
docker info >/dev/null 2>&1 || skip "docker is not answering"
command -v flock >/dev/null 2>&1 || die "flock is missing (apt-get install util-linux)" "Автообновление не запустилось: нет flock."
command -v pgrep >/dev/null 2>&1 || die "pgrep is missing (apt-get install procps)" "Автообновление не запустилось: нет pgrep."
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null
exec 9>"$LOCK_FILE" || die "the lock file $LOCK_FILE cannot be opened" "Автообновление не запустилось: не открывается файл блокировки."
if ! flock -n 9; then
  say "AUTO_UPDATE_SKIPPED: another update is still running"
  exit 0
fi

if pgrep -f "$BUSY_PATTERN" >/dev/null 2>&1; then
  block "an update, a backup, a cleanup or another maintenance script is running"
fi
if pgrep -x 'apt|apt-get|dpkg|unattended-upgr' >/dev/null 2>&1; then
  block "the package manager is running"
fi

hour="$(date -u +%-H)"
if ((hour < WINDOW_FROM_UTC || hour >= WINDOW_TO_UTC)); then
  if ((DRY)); then
    say "  note: the real run starts only between $WINDOW_FROM_UTC:00 and $WINDOW_TO_UTC:00 UTC (now $hour:xx)"
  else
    skip "outside the quiet hours ($WINDOW_FROM_UTC:00-$WINDOW_TO_UTC:00 UTC)"
  fi
fi

troubled="$(unhealthy_containers)"
[[ -z "$troubled" ]] || block "a container is not healthy: $troubled"
before_running="$(running_containers)"
[[ $'\n'"$before_running"$'\n' == *$'\n'"$CONTAINER"$'\n'* ]] || block "the bot container is not running"

if [[ ! -f "$BACKUP_ENV" ]]; then
  block "no scheduled backup (bash scripts/install_backup.sh first)" alert \
    "Автообновление не запущено: не настроены резервные копии (bash scripts/install_backup.sh), без свежей копии сервер не обновляется"
fi

free="$(free_mb)"
free="${free:-0}"
avail="$(avail_mb)"
avail="${avail:-0}"
say "  free on /: $free MB (wanted: $WANT_FREE_MB); memory available: $avail MB (wanted: $MIN_AVAIL_MB)"
if ((free < WANT_FREE_MB)); then
  block "only $free MB free on /, $WANT_FREE_MB MB wanted (bash scripts/server_cleanup.sh)" alert \
    "Автообновление не запущено: на диске свободно $free МБ, нужно $WANT_FREE_MB МБ (bash scripts/server_cleanup.sh)"
fi
if ((avail < MIN_AVAIL_MB)); then
  block "only $avail MB of memory available, $MIN_AVAIL_MB MB wanted"
fi

say "  doctor before:"
doctor
doctor_before=$?
if ((doctor_before != 0)); then
  block "the doctor finds problems before the update: $doctor_fails" alert \
    "Автообновление не запущено: до обновления проверка находит проблемы ($doctor_fails)"
fi

# ------------------------------------------------------------------ backup
step "Backup"
if ((DRY)); then
  say "  would make a fresh encrypted backup first ($BACKUP_RUNNER)"
elif backup_out="$(bash "$BACKUP_RUNNER" 2>&1)"; then
  say "  $(printf '%s\n' "$backup_out" | grep -E '^BACKUP_OK' | head -n1)"
else
  skip "the fresh backup failed: $(printf '%s\n' "$backup_out" | tail -n 1)" alert \
    "Автообновление не запущено: свежая резервная копия не сделана"
fi

# ---------------------------------------------------------------- packages
step "System packages"
if ! command -v apt-get >/dev/null 2>&1; then
  say "  apt-get not found: skipped"
elif [[ -n "$(dpkg --audit 2>/dev/null)" ]]; then
  notes+=("пакеты системы не обновлялись: система пакетов в неполном состоянии (dpkg --audit)")
  say "  the package system is half-configured (dpkg --audit): packages skipped"
elif ! apt-get update -qq >/dev/null 2>&1; then
  notes+=("пакеты системы не обновлялись: не обновился список пакетов")
  say "  the package lists could not be refreshed: packages skipped"
else
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  mapfile -t upgradable < <(apt list --upgradable 2>/dev/null | grep -v '^Listing' | cut -d/ -f1)
  todo=()
  for pkg in "${upgradable[@]}"; do
    [[ -n "$pkg" ]] || continue
    if [[ "$pkg" =~ $HELD_BACK ]]; then held+=("$pkg"); else todo+=("$pkg"); fi
  done
  say "  packages to upgrade: ${#todo[@]}"
  ((${#todo[@]})) && printf '    %s\n' "${todo[@]}" | head -n 40
  for pkg in "${held[@]}"; do
    say "  held back for you: $pkg $(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null) -> $(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ { print $2 }')"
  done
  if ((${#todo[@]})); then
    # Look first: an upgrade that would remove anything is not an unattended one.
    simulation="$(apt-get -s install --only-upgrade "${todo[@]}" 2>&1)"
    simulated=$?
    removals="$(printf '%s\n' "$simulation" | grep -E '^Remv' | awk '{ print $2 }' | tr '\n' ' ')"
    if ((simulated != 0)); then
      notes+=("пакеты системы не обновлялись: проверка обновления не прошла")
      say "  the trial run failed: packages skipped"
    elif [[ -n "$removals" ]]; then
      notes+=("пакеты системы не обновлялись: обновление удалило бы пакеты ($removals)")
      say "  the upgrade would remove packages ($removals): packages skipped"
    elif ((DRY)); then
      say "  would upgrade ${#todo[@]} package(s); nothing is removed, changed configs are kept"
    else
      mkdir -p "$BACKUP_DIR"
      versions="$BACKUP_DIR/apt-versions-$STAMP.txt"
      dpkg-query -W -f='${Package}=${Version}\n' "${todo[@]}" 2>/dev/null >"$versions"
      before_names="$(running_containers)"
      if apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        -o DPkg::Lock::Timeout=300 install --only-upgrade "${todo[@]}" >"$BACKUP_DIR/apt-auto-$STAMP.log" 2>&1; then
        packages=${#todo[@]}
        say "  upgraded $packages package(s); versions before: $versions"
        say "  back if ever needed: apt-get install --allow-downgrades \$(cat $versions)"
      else
        die "apt upgrade failed: see $BACKUP_DIR/apt-auto-$STAMP.log" \
          "Автообновление остановилось на пакетах системы: apt не справился (лог $BACKUP_DIR/apt-auto-$STAMP.log; начните с dpkg --audit)."
      fi
      # A container that is not back after the upgrade is started again, then waited for.
      names=()
      while IFS= read -r name; do
        [[ -n "$name" ]] && names+=("$name")
      done <<<"$before_names"
      for name in "${names[@]}"; do
        [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]] || docker start "$name" >/dev/null 2>&1
      done
      if wait_containers "${names[@]}"; then
        say "  all ${#names[@]} containers run"
      else
        die "containers are not back after the package upgrade" \
          "После обновления пакетов часть контейнеров не поднялась (docker ps -a)."
      fi
    fi
  fi
fi
[[ -f "$REBOOT_FILE" ]] && reboot=yes
[[ "$reboot" == yes ]] && say "  a reboot is needed for the new kernel or libraries: choose a quiet hour"

# ------------------------------------------------------------------ images
step "Images of the bot and the site"
mapfile -t bases < <(awk 'toupper($1) == "FROM" { print $2 }' "$SRC/Dockerfile" 2>/dev/null | sort -u)
changed=()
for image in "${bases[@]}"; do
  [[ "$image" == *:* || "$image" == */* ]] || continue  # a build stage, not an image
  if [[ "$image" == *-builder ]]; then
    say "  skipped: $image (a new compiler layer would rebuild Caddy: about a gigabyte of memory and disk)"
    continue
  fi
  if ((DRY)); then
    say "  would pull: $image"
    continue
  fi
  old_id="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)"
  if docker pull -q "$image" >/dev/null 2>&1; then
    new_id="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)"
    if [[ "$old_id" == "$new_id" ]]; then
      say "  up to date: $image"
    else
      say "  new version: $image"
      changed+=("$image")
    fi
  else
    say "  kept the local $image (pull failed)"
  fi
done

web_container="$(docker ps -a --filter "label=com.docker.compose.project=$WEB_PROJECT" \
  --filter "label=com.docker.compose.service=$WEB_SERVICE" --format '{{.Names}}' 2>/dev/null | head -n1)"
stamp_of() { docker inspect -f '{{index .Config.Labels "node.rental.source"}}' "$1" 2>/dev/null; }
folder_digest="$(bash "$DIGEST_TOOL" "$SRC" 2>/dev/null)"
bot_digest="$(stamp_of "$CONTAINER")"
web_digest=""
[[ -n "$web_container" ]] && web_digest="$(stamp_of "$web_container")"
code_state=same
code_en=""
code_ru=""
if [[ -z "$folder_digest" ]]; then
  code_state=unknown
  code_en="the digest of the project folder could not be computed"
  code_ru="не удалось сверить код в папке проекта с запущенным"
elif [[ -z "$bot_digest" || -z "$web_digest" ]]; then
  code_state=unstamped
  code_en="the running images carry no source stamp yet (run bash scripts/server_update.sh --apply once)"
  code_ru="у запущенных образов ещё нет метки кода (один раз выполните вручную bash scripts/server_update.sh --apply)"
elif [[ "$folder_digest" != "$bot_digest" || "$folder_digest" != "$web_digest" ]]; then
  code_state=differs
  code_en="the project folder holds code that is not deployed yet"
  code_ru="в папке проекта лежит код, который ещё не выкатывали (автообновление его не выкатывает)"
fi
if [[ "$code_state" == same ]]; then
  say "  code in the project folder = code that runs: yes"
else
  say "  code in the project folder = code that runs: no ($code_en)"
fi

bot_rollback=""
web_rollback=""
if ((DRY)); then
  say "  would rebuild the bot and the site only if a base image changed and the code is the same"
elif ((${#changed[@]} == 0)); then
  images=unchanged
  say "  no base image changed: the bot and the site are not touched"
elif [[ "$code_state" != same ]]; then
  images=skipped
  notes+=("есть новые базовые образы, но бот и сайт не пересобирались: $code_ru")
  say "  new base images, but no rebuild: $code_en"
else
  step "Bot and site"
  bot_out="$(bash "$BOT_UPDATER" "$SRC" --apply 2>&1)"
  printf '%s\n' "$bot_out" | tail -n 3 | sed 's/^/  /'
  bot_rollback="$(printf '%s\n' "$bot_out" | sed -n 's/^ROLLBACK_IMAGE_SAVED //p' | head -n1)"
  grep -q 'UPDATE_OK' <<<"$bot_out" \
    || die "the bot update did not finish (it went back by itself)" \
      "Обновление бота не прошло проверку и откатилось само, бот работает на прежнем образе. Пакетов системы обновлено: $packages."
  web_out="$(bash "$WEB_UPDATER" "$SRC" --apply 2>&1)"
  printf '%s\n' "$web_out" | tail -n 3 | sed 's/^/  /'
  web_rollback="$(printf '%s\n' "$web_out" | sed -n 's/^ROLLBACK_IMAGE \([^ ]*\).*/\1/p' | head -n1)"
  grep -q 'WEB_UPDATE_OK' <<<"$web_out" \
    || die "the site update did not finish (it went back by itself)" \
      "Обновление сайта не прошло проверку и откатилось само, сайт работает на прежнем образе. Бот уже обновлён."
  images=rebuilt
fi

if ((DRY)); then
  step "Result"
  if ((${#blockers[@]})); then
    say "AUTO_UPDATE_DRY_RUN: the real run would stop now: ${blockers[*]}"
  else
    say "AUTO_UPDATE_DRY_RUN: nothing blocks the update; nothing was changed."
  fi
  exit 0
fi

# ------------------------------------------------------------ after the work
step "Doctor after"
doctor
if (($? != 0)); then
  # The doctor was clean before, so this is new. System packages cannot be taken
  # back by a script, but the images can.
  if [[ -n "$bot_rollback" || -n "$web_rollback" ]]; then
    say "  new problems after the rebuild: the bot and the site go back to their previous images"
    [[ -n "$web_rollback" ]] && bash "$WEB_UPDATER" --rollback "$web_rollback" 2>&1 | tail -n 2 | sed 's/^/  /'
    [[ -n "$bot_rollback" ]] && bash "$BOT_UPDATER" --rollback "$bot_rollback" 2>&1 | tail -n 2 | sed 's/^/  /'
    images=rolled-back
    failures="$doctor_fails"
    doctor
    if (($? == 0)); then
      die "new problems after the rebuild ($failures); the images went back and the doctor is clean again" \
        "После обновления проверка нашла проблемы ($failures). Бот и сайт возвращены на прежние образы, сейчас всё в порядке; обновление пакетов системы осталось ($packages)."
    fi
    die "new problems after the update ($failures); the images went back, the doctor still finds problems" \
      "После обновления проверка нашла проблемы ($failures), возврат образов не помог: причина, скорее всего, в пакетах системы. Нужен ваш разбор: bash scripts/update_bot.sh --doctor."
  fi
  die "new problems after the package upgrade ($doctor_fails)" \
    "После обновления пакетов системы проверка нашла проблемы ($doctor_fails). Образы не менялись. Версии пакетов до обновления: $BACKUP_DIR/apt-versions-$STAMP.txt."
fi

# --------------------------------------------------------------------- done
note_text=""
for note in "${notes[@]}"; do note_text+=" ${note^}."; done
text="🔄 Ежемесячное обновление сервера выполнено. Пакетов системы обновлено: $packages."
case "$images" in
  rebuilt) text+=" Бот и сайт пересобраны на свежих базовых образах, проверка после обновления чистая." ;;
  unchanged) text+=" Базовые образы не изменились, бот и сайт не трогались." ;;
esac
text+="$note_text"
if ((${#held[@]})); then
  text+=" Docker и его среда не обновляются автоматически, доступны новые версии (${held[*]}): bash scripts/server_update.sh --apply в тихий час."
fi
if [[ "$reboot" == yes ]]; then
  text+=" Нужна перезагрузка (новое ядро или библиотеки): в тихий час выполните reboot, контейнеры поднимутся сами."
fi
note="packages $packages, images $images"
state_write ok "$note"
say ""
say "AUTO_UPDATE_OK packages=$packages images=$images reboot=$reboot"
notify always "$text"
