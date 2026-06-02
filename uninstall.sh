#!/bin/bash
# Carbonio CE large-files-fix uninstaller.
# Restores from the most recent backup under /root/backups/carbonio-ce-large-files-fix/.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  case "${LANG:-en}" in
    ru*|RU*) echo "Запустите от root." >&2 ;;
    *)       echo "Run as root." >&2 ;;
  esac
  exit 1
fi

LC=$(echo "${LC_ALL:-${LANG:-en}}" | tr '[:upper:]' '[:lower:]')
case "$LC" in
  ru*) MSG_LOCALE=ru ;;
  *)   MSG_LOCALE=en ;;
esac
msg() {  # msg "русский" "english"
  if [ "$MSG_LOCALE" = "ru" ]; then echo "$1"; else echo "$2"; fi
}

BAK=$(ls -td /root/backups/carbonio-ce-large-files-fix/*/ 2>/dev/null | head -1)
if [ -z "$BAK" ]; then
  msg "Не найден каталог резервных копий в /root/backups/carbonio-ce-large-files-fix/" \
      "No backup directory found in /root/backups/carbonio-ce-large-files-fix/" >&2
  exit 2
fi
msg "Восстановление из: $BAK" "Restoring from: $BAK"

# 1. JAR
if [ -f "$BAK/carbonio-files.jar" ]; then
  cp -a "$BAK/carbonio-files.jar" /usr/share/carbonio/carbonio-files.jar
  msg "  JAR восстановлен" "  JAR restored"
fi

# 2. Java wrapper (heap)
if [ -f "$BAK/carbonio-files.wrapper" ]; then
  cp -a "$BAK/carbonio-files.wrapper" /usr/bin/carbonio-files
  msg "  /usr/bin/carbonio-files восстановлен" "  /usr/bin/carbonio-files restored"
fi

# 3. nginx templates
for t in nginx.conf.web.https.template nginx.conf.web.https.default.template; do
  if [ -f "$BAK/$t" ]; then
    cp -a "$BAK/$t" /opt/zextras/conf/nginx/templates/$t
    msg "  $t восстановлен" "  $t restored"
  fi
done
su - zextras -c "/opt/zextras/libexec/zmproxyconfgen" >/dev/null 2>&1
kill -HUP "$(cat /run/carbonio/nginx.pid)"
sleep 1
chown zextras:zextras /opt/zextras/data/tmp/nginx/client

# 4. JS chunks — search files-ui, mails-ui AND shell-ui (PostHog patch)
for f in "$BAK"/*.chunk.js; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  target=$(find /opt/zextras/web/iris/carbonio-files-ui \
                /opt/zextras/web/iris/carbonio-mails-ui \
                /opt/zextras/web/iris/carbonio-shell-ui \
    -name "$base" -not -name "*.bak.*" -not -name "*.map" 2>/dev/null | head -1)
  if [ -n "$target" ]; then
    cp -a "$f" "$target"
    chown zextras:zextras "$target"
    msg "  $base восстановлен ($target)" "  $base restored ($target)"
  fi
done

# 5. Sidecar
if systemctl list-unit-files | grep -q carbonio-chunked-upload; then
  systemctl disable --now carbonio-chunked-upload 2>/dev/null || true
  rm -f /etc/systemd/system/carbonio-chunked-upload.service
  rm -f /usr/local/bin/carbonio-chunked-upload.py
  systemctl daemon-reload
  msg "  sidecar удалён" "  sidecar removed"
fi

# 6. carbonio-files restart
systemctl restart carbonio-files
sleep 3
until curl -s --max-time 2 http://127.78.0.2:10000/health/live/ -o /dev/null -w "%{http_code}\n" 2>/dev/null | grep -q 204; do
  sleep 2
done
chown zextras:zextras /opt/zextras/data/tmp/nginx/client

echo
if [ "$MSG_LOCALE" = "ru" ]; then
  cat <<EOF
Удалено. Роль PostgreSQL 'zextras' и каталог /opt/zextras/chunked-upload-tmp/ оставлены.
Удалить их вручную (если нужна чистота):
  sudo -u postgres psql -d 'carbonio-files-db' -c "DROP ROLE zextras;"
  rm -rf /opt/zextras/chunked-upload-tmp
EOF
else
  cat <<EOF
Uninstalled. PostgreSQL role 'zextras' and staging dir /opt/zextras/chunked-upload-tmp/ left intact.
Drop them manually if you want a clean slate:
  sudo -u postgres psql -d 'carbonio-files-db' -c "DROP ROLE zextras;"
  rm -rf /opt/zextras/chunked-upload-tmp
EOF
fi
