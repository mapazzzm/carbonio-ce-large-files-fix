#!/bin/bash
# Carbonio CE large-files-fix installer.
# Idempotent: re-running is safe. Every change is backed up under
# /root/backups/carbonio-ce-large-files-fix/<timestamp>/.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  case "${LANG:-en}" in
    ru*|RU*) echo "Запустите от root." >&2 ;;
    *)       echo "Run as root." >&2 ;;
  esac
  exit 1
fi

# --- i18n: pick locale based on $LANG ($LC_ALL has priority if set) -------
LC=$(echo "${LC_ALL:-${LANG:-en}}" | tr '[:upper:]' '[:lower:]')
case "$LC" in
  ru*) MSG_LOCALE=ru ;;
  *)   MSG_LOCALE=en ;;
esac
msg() {  # msg "русский" "english"
  if [ "$MSG_LOCALE" = "ru" ]; then echo "$1"; else echo "$2"; fi
}

HERE=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date +%Y%m%d_%H%M%S)
BAK=/root/backups/carbonio-ce-large-files-fix/$STAMP
mkdir -p "$BAK"
msg "Резервные копии сохраняются в $BAK" "Backups go to $BAK"

# Cleanup intermediate files on exit (success or failure).
CU_TMP=/tmp/cu_prefix_install.$$.js
trap 'rm -f "$CU_TMP"' EXIT
echo

# --- 0. Version sanity check ----------------------------------------------
need_pkg() {
  local pkg=$1 expected=$2
  local got
  got=$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || echo "MISSING")
  if [ "$got" = "MISSING" ]; then
    msg "ОШИБКА: пакет $pkg не установлен." "ERROR: $pkg is not installed." >&2
    exit 2
  fi
  msg "  $pkg: $got (ожидается $expected)" "  $pkg: $got (expected $expected)"
}
msg "[0/8] Проверка версий пакетов:" "[0/8] Version check:"
need_pkg carbonio-files-ce       1.1.2-1noble
need_pkg carbonio-storages-ce    1.0.16-1ubuntu
need_pkg carbonio-files-ui       2.15.2-1ubuntu
echo

# --- 1. Python deps -------------------------------------------------------
msg "[1/8] Python-зависимости (aiohttp, aiofiles, asyncpg)..." \
    "[1/8] Python deps (aiohttp, aiofiles, asyncpg)..."
python3 -c "import aiohttp, aiofiles, asyncpg" 2>/dev/null || \
  pip3 install --quiet --break-system-packages aiohttp aiofiles asyncpg
msg "  готово" "  ok"

# --- 2. PostgreSQL role ---------------------------------------------------
msg "[2/8] PostgreSQL: создание роли 'zextras' с read-only на link/node/revision..." \
    "[2/8] PostgreSQL: ensure 'zextras' role with read-only on link/node/revision..."
sudo -u postgres psql -d "carbonio-files-db" -q -f "$HERE/patches/postgres_role.sql"
msg "  готово" "  ok"

# --- 3. JAR patch ---------------------------------------------------------
msg "[3/8] JAR-патч (BlobService.verifyBlobExists, ifne→ifge)..." \
    "[3/8] JAR patch (BlobService verifyBlobExists, ifne→ifge)..."
cp -a /usr/share/carbonio/carbonio-files.jar "$BAK/"
python3 "$HERE/patches/blobservice_patch.py"
msg "  готово" "  ok"

# --- 4. Java heap bump ----------------------------------------------------
msg "[4/8] Java heap: -Xmx4096m → -Xmx12288m..." "[4/8] Java heap: -Xmx4096m → -Xmx12288m..."
cp -a /usr/bin/carbonio-files "$BAK/carbonio-files.wrapper"
sed -i 's/-Xmx4096m/-Xmx12288m/' /usr/bin/carbonio-files
msg "  готово" "  ok"

# --- 5. Sidecar -----------------------------------------------------------
msg "[5/8] Sidecar-сервис (carbonio-chunked-upload)..." \
    "[5/8] Sidecar service (carbonio-chunked-upload)..."
[ -f /usr/local/bin/carbonio-chunked-upload.py ] && cp -a /usr/local/bin/carbonio-chunked-upload.py "$BAK/" || true
[ -f /etc/systemd/system/carbonio-chunked-upload.service ] && cp -a /etc/systemd/system/carbonio-chunked-upload.service "$BAK/" || true
install -m 0755 "$HERE/sidecar/carbonio-chunked-upload.py" /usr/local/bin/
install -m 0644 "$HERE/sidecar/carbonio-chunked-upload.service" /etc/systemd/system/
install -d -o zextras -g zextras -m 0750 /opt/zextras/chunked-upload-tmp
systemctl daemon-reload
systemctl enable carbonio-chunked-upload >/dev/null 2>&1
# Always restart (not just start) so a re-install picks up code changes.
systemctl restart carbonio-chunked-upload
msg "  готово" "  ok"

# --- 6. nginx templates ---------------------------------------------------
msg "[6/8] nginx-шаблоны (вставка 3 location-блоков)..." \
    "[6/8] nginx templates (insert 3 location blocks)..."
TPL_DIR=/opt/zextras/conf/nginx/templates
for t in nginx.conf.web.https.template nginx.conf.web.https.default.template; do
  cp -a "$TPL_DIR/$t" "$BAK/"
  if ! grep -q "services/files/upload-chunked" "$TPL_DIR/$t"; then
    python3 - "$TPL_DIR/$t" "$HERE/nginx/location-blocks.conf" <<'PYEOF'
import sys
target, snippet = sys.argv[1], sys.argv[2]
with open(target) as f: t = f.read()
with open(snippet) as f: s = f.read()
marker = "    location /services/files/"
i = t.find(marker)
if i < 0: sys.exit(f"marker not found in {target}")
new = t[:i] + s.rstrip() + "\n\n" + t[i:]
with open(target, "w") as f: f.write(new)
PYEOF
  fi
done
su - zextras -c "/opt/zextras/libexec/zmproxyconfgen" >/dev/null 2>&1
su - zextras -c "/opt/zextras/common/sbin/nginx -t -c /opt/zextras/conf/nginx.conf" 2>&1 | tail -2
kill -HUP "$(cat /run/carbonio/nginx.pid)"
sleep 1
chown zextras:zextras /opt/zextras/data/tmp/nginx/client
msg "  готово" "  ok"

# --- 7. JS patches (files-ui + shell-ui + mails-ui) -----------------------
msg "[7/8] JS-патчи (files-ui + shell-ui + mails-ui)..." \
    "[7/8] JS patches (files-ui + shell-ui + mails-ui)..."
UI_BASE=$(ls -d /opt/zextras/web/iris/carbonio-files-ui/*/ 2>/dev/null | grep -v i18n | grep -v current | head -1)
CHUNK=$(ls "${UI_BASE}659."*.chunk.js 2>/dev/null | grep -v '\.bak\.' | grep -v '\.map' | head -1)
UPVIEW=$(ls "${UI_BASE}uploadView."*.chunk.js 2>/dev/null | grep -v '\.map' | head -1 | xargs -n1 basename)
if [ -z "$CHUNK" ] || [ -z "$UPVIEW" ]; then
  msg "  ОШИБКА: не найден 659.*.chunk.js или uploadView.*.chunk.js в $UI_BASE" \
      "  ERROR: cannot locate 659.*.chunk.js or uploadView.*.chunk.js in $UI_BASE" >&2
  exit 3
fi
# Substitute the runtime uploadView chunk name into the prefix.
sed "s/uploadView\\.0c7dbda7\\.chunk\\.js/$UPVIEW/g" "$HERE/js/chunked-upload-prefix.js" \
  > "$CU_TMP"

# 7a. files-ui 659.*.chunk.js
cp -a "$CHUNK" "$BAK/"
if ! grep -q "__cuPatched" "$CHUNK"; then
  # Bump Carbonio's UploadStore parallel-upload limit to 16. Different
  # Carbonio CE versions ship with different defaults (1, 3, ...), so we
  # match the structural pattern instead of a literal value. Small files
  # can then run truly in parallel (browser HTTP/1.1 socket cap ~6 still
  # limits actual transfers, but progress events stay continuous instead
  # of jumping in batches). Medium/large plain uploads are additionally
  # gated to LargePlainConcurrency=5 in chunked-upload-prefix.js.
  sed -i 's/return{LIMIT:[0-9]\+,/return{LIMIT:16,/g; s/(;n\.length<[0-9]\+&&e\.length>0/(;n.length<16\&\&e.length>0/g' "$CHUNK"
  # Replace native XHR construction with our wrapper. The prefix exposes
  # window.__cuXHR; Carbonio's upload/version/action code uses `new XMLHttpRequest`
  # at 3-4 sites. Without this substitution the wrapper would be defined but
  # never invoked → no overlay, no chunked upload, no progress tracking.
  sed -i 's/new XMLHttpRequest/new (window.__cuXHR||XMLHttpRequest)/g' "$CHUNK"
  cat "$CU_TMP" "$CHUNK" > "$CHUNK.tmp"
  mv "$CHUNK.tmp" "$CHUNK"
  chown zextras:zextras "$CHUNK"
  msg "  files-ui: пропатчен ${CHUNK##*/} (uploadView=$UPVIEW)" \
      "  files-ui: patched ${CHUNK##*/} (uploadView=$UPVIEW)"
else
  msg "  files-ui: уже пропатчен, пропускаю" "  files-ui: already patched, skipping"
fi

# 7b. shell-ui PostHog warn suppression (Carbonio architectural noise)
SHELL_BASE=$(ls -d /opt/zextras/web/iris/carbonio-shell-ui/*/ 2>/dev/null | grep -v current | head -1)
SHELL_CHUNK=$(grep -l 'PostHog.js.*already loaded elsewhere' "$SHELL_BASE"*.chunk.js 2>/dev/null | head -1)
if [ -n "$SHELL_CHUNK" ]; then
  cp -a "$SHELL_CHUNK" "$BAK/"
  python3 - "$SHELL_CHUNK" <<'PYEOF'
import sys
f = sys.argv[1]
OLD = 'console.warn("[PostHog.js] `posthog` was already loaded elsewhere. This may cause issues.")'
NEW = 'void 0'
with open(f, 'r', encoding='utf-8') as fh: c = fh.read()
if c.count(OLD) == 1:
    with open(f, 'w', encoding='utf-8') as fh: fh.write(c.replace(OLD, NEW))
    print("  shell-ui: PostHog warn suppressed in", f.rsplit("/", 1)[-1])
else:
    print("  shell-ui: PostHog warn already patched or pattern changed - skipping")
PYEOF
  chown zextras:zextras "$SHELL_CHUNK"
else
  msg "  shell-ui: chunk с PostHog warn не найден — пропускаю" \
      "  shell-ui: chunk with PostHog warn not found - skipping"
fi

# 7c. mails-ui mails-edit-view.*.chunk.js (smart-link mail attach via fetch)
MAILS_BASE=$(ls -d /opt/zextras/web/iris/carbonio-mails-ui/*/ 2>/dev/null | grep -v i18n | grep -v current | head -1)
MAILS_EV=$(ls "${MAILS_BASE}mails-edit-view."*.chunk.js 2>/dev/null | grep -v '\.bak\.' | grep -v '\.map' | head -1)
if [ -z "$MAILS_EV" ]; then
  msg "  ПРЕДУПРЕЖДЕНИЕ: mails-edit-view.*.chunk.js не найден в $MAILS_BASE — пропускаю mail-attach патч" \
      "  WARNING: mails-edit-view.*.chunk.js not found in $MAILS_BASE - skipping mail-attach patch" >&2
else
  cp -a "$MAILS_EV" "$BAK/"
  if grep -q "__cuPatched" "$MAILS_EV"; then
    msg "  mails-ui: уже пропатчен, пропускаю" "  mails-ui: already patched, skipping"
  else
    python3 - "$MAILS_EV" <<'PYEOF'
import sys
f = sys.argv[1]
OLD = 'fetch("/services/files/upload"'
NEW = '(window.__cuFetch||fetch)("/services/files/upload"'
with open(f, 'r', encoding='utf-8') as fh: c = fh.read()
n = c.count(OLD)
if n != 1:
    sys.exit(f"ERROR: expected exactly 1 occurrence of fetch(...), got {n}")
with open(f, 'w', encoding='utf-8') as fh: fh.write(c.replace(OLD, NEW))
PYEOF
    cat "$CU_TMP" "$MAILS_EV" > "$MAILS_EV.tmp"
    mv "$MAILS_EV.tmp" "$MAILS_EV"
    chown zextras:zextras "$MAILS_EV"
    msg "  mails-ui: пропатчен ${MAILS_EV##*/}" "  mails-ui: patched ${MAILS_EV##*/}"
  fi
fi

# --- 8. Restart carbonio-files for JAR patch ------------------------------
msg "[8/8] Перезапуск carbonio-files..." "[8/8] Restart carbonio-files..."
systemctl restart carbonio-files
sleep 3
until curl -s --max-time 2 http://127.78.0.2:10000/health/live/ -o /dev/null -w "%{http_code}\n" 2>/dev/null | grep -q 204; do
  sleep 2
done
chown zextras:zextras /opt/zextras/data/tmp/nginx/client
msg "  carbonio-files HEALTHY" "  carbonio-files HEALTHY"

# Backup rotation — keep the 10 most recent backup directories.
ls -1tdr /root/backups/carbonio-ce-large-files-fix/*/ 2>/dev/null \
  | head -n -10 | xargs -r rm -rf

echo
if [ "$MSG_LOCALE" = "ru" ]; then
  cat <<EOF
================ ГОТОВО ================
Резервные копии:   $BAK
Sidecar:           systemctl status carbonio-chunked-upload
Тест:              загрузить файл > 200 МБ в Carbonio Files (сначала Ctrl+F5)
Удалить:           $HERE/uninstall.sh

Если что-то пошло не так — см. docs/troubleshooting.md или восстановите из $BAK.
EOF
else
  cat <<EOF
================ DONE ================
Backups:    $BAK
Sidecar:    systemctl status carbonio-chunked-upload
Test:       upload a > 200 MB file in Carbonio Files (hard-refresh Ctrl+F5 first)
Uninstall:  $HERE/uninstall.sh

If anything is off, see docs/troubleshooting.md or restore from $BAK.
EOF
fi
