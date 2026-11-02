#!/bin/sh
# Tạo symlink từ repo này vào đúng chỗ trên macOS; file đang có được đổi tên *.bak-<ngày> trước khi thay.
set -e
DOT=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)

link() {  # link <nguồn trong repo> <đích>
  mkdir -p "$(dirname "$2")"
  if [ -L "$2" ] && [ "$(readlink "$2")" = "$1" ]; then echo "ok    $2"; return; fi
  [ -e "$2" ] || [ -L "$2" ] && mv "$2" "$2.bak-$STAMP" && echo "bak   $2.bak-$STAMP"
  ln -s "$1" "$2" && echo "link  $2 -> $1"
}

link "$DOT/ghostty/config"     "$HOME/Library/Application Support/com.mitchellh.ghostty/config"
link "$DOT/herdr/config.toml"  "$HOME/.config/herdr/config.toml"
link "$DOT/bin/gcfg"           "$HOME/.local/bin/gcfg"
link "$DOT/bin/gtheme"         "$HOME/.local/bin/gtheme"
