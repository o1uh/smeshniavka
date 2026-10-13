#!/usr/bin/env bash
set -euo pipefail

# Funny Bot Deployment Script for Raspberry Pi
# Target directory: /opt/funny-bot
# Usage: ./deploy.sh [ssh_host_or_alias]
# Example: ./deploy.sh rpi

RPI_HOST="${1:-rpi}"
REMOTE_DIR="/opt/funny-bot"

echo "=== Деплой Funny Bot на Raspberry Pi (${RPI_HOST}) ==="

# 1. Проверка наличия .env
if [ ! -f ".env" ]; then
    echo "⚠️  Файл .env не найден локально! Не забудьте создать его на RPi в ${REMOTE_DIR}/.env."
fi

# 2. Создание каталога на RPi и настройка прав
echo "--> Подготовка удаленной директории ${REMOTE_DIR} на ${RPI_HOST}..."
ssh "${RPI_HOST}" "sudo mkdir -p ${REMOTE_DIR} && sudo chown -R \$(whoami):\$(whoami) ${REMOTE_DIR}"

# 3. Синхронизация файлов через rsync
echo "--> Синхронизация файлов..."
rsync -avz --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude '.pytest_cache' \
    --exclude 'data/*.db' \
    --exclude 'data/*.db-wal' \
    --exclude 'data/*.db-shm' \
    --exclude 'logs/*' \
    --exclude 'reports/*' \
    --exclude 'backups/*' \
    ./ "${RPI_HOST}:${REMOTE_DIR}/"

# 4. Настройка окружения на RPi
echo "--> Настройка Python и зависимостей на Raspberry Pi..."
ssh "${RPI_HOST}" bash -s << 'EOF'
set -euo pipefail
cd /opt/funny-bot

# Проверка ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "Установка ffmpeg..."
    sudo apt-get update && sudo apt-get install -y ffmpeg python3-venv python3-pip
fi

# Создание виртуального окружения
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Проверка dry-run
.venv/bin/python -m src.main --dry-run
EOF

echo ""
echo "=== Код успешно задеплоен на Raspberry Pi! ==="
echo "Дальнейшие шаги на RPi (${RPI_HOST}):"
echo "1. Проверьте /opt/funny-bot/.env и ключи: .venv/bin/python -m src.tools.check_keys"
echo "2. Узнайте ID чата: .venv/bin/python -m src.tools.discover"
echo "3. Запустите через systemd:"
echo "   sudo cp /opt/funny-bot/ops/bot.service /etc/systemd/system/funny-bot.service"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable --now funny-bot"
echo "   sudo systemctl status funny-bot"
