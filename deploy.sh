#!/usr/bin/env bash
set -euo pipefail

# Funny Bot («Смешнявка») Deployment Script for Raspberry Pi
# Target directory: /opt/smeshniavka
# Default host: pi@rpi.ln
# Default SSH key: ~/.ssh/rpi_router_id_ed25519

RPI_HOST="${1:-pi@rpi.ln}"
SSH_KEY="${2:-$HOME/.ssh/rpi_router_id_ed25519}"
REMOTE_DIR="/opt/smeshniavka"

echo "=== Деплой Funny Bot («Смешнявка») на Raspberry Pi (${RPI_HOST}) ==="

SSH_OPTS=()
if [ -f "${SSH_KEY}" ]; then
    SSH_OPTS=(-i "${SSH_KEY}")
fi

# 1. Подготовка удаленного каталога
echo "--> Подготовка удаленной директории ${REMOTE_DIR}..."
ssh "${SSH_OPTS[@]}" "${RPI_HOST}" "sudo -n mkdir -p ${REMOTE_DIR} && sudo -n chown -R \$(whoami):\$(whoami) ${REMOTE_DIR}"

# 2. Синхронизация файлов кодовой базы
echo "--> Синхронизация файлов кодовой базы в ${REMOTE_DIR}..."
RSYNC_RSH="ssh"
if [ -f "${SSH_KEY}" ]; then
    RSYNC_RSH="ssh -i ${SSH_KEY}"
fi

rsync -avz --delete -e "${RSYNC_RSH}" \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude '.pytest_cache' \
    --exclude 'data/*.db' \
    --exclude 'data/*.db-wal' \
    --exclude 'data/*.db-shm' \
    --exclude 'data/bot.lock' \
    --exclude 'logs/*' \
    --exclude 'reports/*' \
    --exclude 'backups/*' \
    ./ "${RPI_HOST}:${REMOTE_DIR}/"

# 3. Подготовка структуры и запуск Docker Compose
echo "--> Настройка и сборка Docker-контейнера на Raspberry Pi..."
ssh "${SSH_OPTS[@]}" "${RPI_HOST}" bash -s << 'EOF'
set -euo pipefail
cd /opt/smeshniavka

# Создание рабочих каталогов с правами 700
mkdir -p data logs reports backups
chmod 700 data reports backups

# Если .env еще не создан, создаем шаблон из .env.example
if [ ! -f ".env" ]; then
    echo "⚠️  Файл .env не найден на RPi. Создаем шаблон из .env.example..."
    cp .env.example .env
    chmod 600 .env
fi

# Сборка Docker образа
echo "--> Сборка Docker образа smeshniavka..."
docker compose build

# Если .env заполнен валидным токеном, запускаем контейнер
if grep -q "1234567890:ABCdef" .env; then
    echo ""
    echo "⚠️  ВНИМАНИЕ: Файл .env содержит тестовые заглушки!"
    echo "Отредактируйте /opt/smeshniavka/.env на RPi, добавив реальный TG_TOKEN и ключи GEMINI_KEY_*."
    echo "После этого запустите бота:"
    echo "    cd /opt/smeshniavka && docker compose up -d"
else
    echo "--> Запуск контейнера через Docker Compose..."
    docker compose up -d
    echo "--> Статус контейнера:"
    docker compose ps
fi
EOF

echo ""
echo "=== Кодовая база синхронизирована и Docker-образ собран на RPi! ==="
