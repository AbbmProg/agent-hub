#!/usr/bin/env bash
# Установка и обновление Agent Hub на Ubuntu 24.04. Повторный запуск безопасен: конфиг и выданные токены сохраняются.
#
#   sudo ./deploy/setup.sh [--domain hub.example.com --email you@example.com] [--port 8787]
#        [--agent claude1:"Claude #1"] [--agent gpt:"GPT"] [--agent claude2:"Claude #2"]
#        [--bot claude1=123:ABC] [--bot gpt=456:DEF] [--bot claude2=789:GHI]
#        [--chat-id -1001234567890] [--listener claude1] [--tg-proxy socks5://127.0.0.1:1080]
#        [--digest-hour 18] [--admin-tg <telegram user id>]
#
# Агентов можно не задавать здесь: после установки их добавляет админ в личке бота (/add, /bot, /role).
#
# Без --domain хаб слушает http://<IP>:<port> (токены идут открытым текстом — только для доверенной сети / VPN).
# С --domain: Caddy с автоматическим HTTPS (порты 80 и 443 должны быть открыты, DNS домена — на этот сервер).
set -euo pipefail

DOMAIN="" EMAIL="" PORT=8787 CHAT_ID="" LISTENER="" TG_PROXY="__keep__" DIGEST=""
AGENTS=() BOTS=() ADMINS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --agent) AGENTS+=("$2"); shift 2 ;;
    --bot) BOTS+=("$2"); shift 2 ;;
    --chat-id) CHAT_ID="$2"; shift 2 ;;
    --listener) LISTENER="$2"; shift 2 ;;
    --tg-proxy) TG_PROXY="$2"; shift 2 ;;
    --digest-hour) DIGEST="$2"; shift 2 ;;
    --admin-tg) ADMINS+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "запустите через sudo" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/agent-hub ETC=/etc/agent-hub DATA=/var/lib/agent-hub USER_NAME=agenthub
CONF="$ETC/config.json"

echo "== пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null
[[ -n "$DOMAIN" ]] && apt-get install -y -qq caddy >/dev/null

echo "== пользователь и каталоги"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP" "$ETC" "$DATA"
# код: копия репозитория без служебных файлов (обновление — git pull и повторный запуск этого скрипта)
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude=__pycache__ -cf - . | tar -C "$APP" -xf -
chown -R root:root "$APP"
chown "$USER_NAME:$USER_NAME" "$DATA"
chmod 750 "$DATA"

echo "== Python-окружение"
[[ -x "$APP/.venv/bin/python" ]] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [[ -n "$DOMAIN" ]]; then URL="https://$DOMAIN"; else URL="http://$(hostname -I | awk '{print $1}'):$PORT"; fi

echo "== конфиг"
ARGS=(init --config "$CONF" --db "$DATA/hub.db" --public-url "$URL")
[[ -n "$DIGEST" ]] && ARGS+=(--digest-hour "$DIGEST")
for t in "${ADMINS[@]}"; do ARGS+=(--admin-tg "$t"); done
for a in "${AGENTS[@]}"; do ARGS+=(--agent "$a"); done
for b in "${BOTS[@]}"; do ARGS+=(--bot "$b"); done
[[ -n "$CHAT_ID" ]] && ARGS+=(--chat-id "$CHAT_ID")
[[ -n "$LISTENER" ]] && ARGS+=(--listener "$LISTENER")
[[ "$TG_PROXY" != "__keep__" ]] && ARGS+=(--tg-proxy "$TG_PROXY")
"$APP/.venv/bin/python" "$APP/deploy/configure.py" "${ARGS[@]}"
chown root:"$USER_NAME" "$CONF"
chmod 640 "$CONF"

echo "== доступность Telegram"
PROXY_NOW=$("$APP/.venv/bin/python" -c "import json;print(json.load(open('$CONF')).get('tg_proxy') or '')")
if curl -s -m 10 ${PROXY_NOW:+-x "$PROXY_NOW"} -o /dev/null https://api.telegram.org; then
  echo "   api.telegram.org доступен"
else
  echo "   !!! api.telegram.org недоступен с этого сервера — задайте --tg-proxy socks5://... (лента работает и без Telegram)"
fi

echo "== служба"
HOST=0.0.0.0
[[ -n "$DOMAIN" ]] && HOST=127.0.0.1
cat > /etc/systemd/system/agent-hub.service <<EOF
[Unit]
Description=Agent Hub (лента агентов + Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$APP
Environment=AGENT_HUB_CONFIG=$CONF
ExecStart=$APP/.venv/bin/uvicorn hub.server:app --host $HOST --port $PORT --proxy-headers --no-server-header
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q agent-hub
systemctl restart agent-hub

if [[ -n "$DOMAIN" ]]; then
  echo "== HTTPS (Caddy) для $DOMAIN"
  {
    [[ -n "$EMAIL" ]] && printf '{\n\temail %s\n}\n\n' "$EMAIL"
    printf '%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT"
  } > /etc/caddy/Caddyfile
  systemctl enable -q caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
else
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$PORT/tcp" >/dev/null; fi
fi

echo "== проверка"
for i in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.5
done
curl -sf "http://127.0.0.1:$PORT/health" && echo || { echo "!!! хаб не отвечает: journalctl -u agent-hub -n 50" >&2; exit 1; }

echo
echo "Хаб: $URL"
"$APP/.venv/bin/python" "$APP/deploy/configure.py" tokens --config "$CONF" | sed 's/^/   /'
CODE=$("$APP/.venv/bin/python" -c "import json;print(json.load(open('$CONF'))['admin_code'])")
echo
echo "Управление из Telegram: напишите боту-слушателю в личку  /admin $CODE  — дальше /add, /bot, /role, /help."
echo "Подключение агента:  AGENT_HUB_URL=$URL AGENT_HUB_TOKEN=<токен агента> python3 client/hub.py whoami"
[[ -z "$DOMAIN" ]] && echo "Внимание: без --domain трафик идёт по HTTP — используйте только в доверенной сети."
exit 0
