#!/usr/bin/env bash
# Устанавливает laptop-doctor: root-помощник + polkit (через sudo), ярлык, автозапуск, команду laptop-doctor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/laptop-doctor"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"

python3 -c "import PyQt6.QtWidgets" 2>/dev/null || { echo "Нужен PyQt6: sudo apt install python3-pyqt6"; exit 1; }

echo "==> root-помощник и правило polkit (нужен пароль sudo)"
sudo install -o root -g root -m 0755 "$REPO/helper/laptop-doctor-helper" /usr/local/libexec/laptop-doctor-helper
sudo install -o root -g root -m 0644 "$REPO/data/org.svv.laptop-doctor.policy" \
  /usr/share/polkit-1/actions/org.svv.laptop-doctor.policy

echo "==> команда $BIN"
mkdir -p "$(dirname "$BIN")" "$APPS" "$AUTOSTART"
cat > "$BIN" <<EOF
#!/bin/sh
PYTHONPATH="$REPO" exec /usr/bin/python3 -m laptop_doctor "\$@"
EOF
chmod +x "$BIN"

echo "==> ярлык в меню и автозапуск"
desktop() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=Здоровье ноутбука
Comment=Проверки, исправления и очистка (laptop-doctor)
Exec=$BIN $1
Icon=preferences-system-performance
Terminal=false
Categories=System;Monitor;
StartupNotify=false
EOF
}
desktop "--show" > "$APPS/laptop-doctor.desktop"
desktop "" > "$AUTOSTART/laptop-doctor.desktop"
echo "X-GNOME-Autostart-Delay=20" >> "$AUTOSTART/laptop-doctor.desktop"

echo
echo "Готово. Запуск сейчас: laptop-doctor &   (или найдите «Здоровье ноутбука» в меню)"
echo "Проверка в терминале: laptop-doctor --check -v"
