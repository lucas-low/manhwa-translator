#!/bin/zsh
# Installs mt-stack for the current user. Needs an existing manga-image-translator checkout with its venv.
# It never touches ~/manga-image-translator/.env, and nothing in this repo holds a key.
set -e
REPO="${0:A:h}"
MT_HOME="${MT_HOME:-$HOME/manga-image-translator}"
LA="$HOME/Library/LaunchAgents"

[[ -x "$MT_HOME/venv/bin/python" ]] || { echo "No venv at $MT_HOME/venv. Set MT_HOME to your manga-image-translator checkout."; exit 1; }

mkdir -p "$HOME/.config/mt-stack" "$HOME/bin" "$LA"
[[ -f "$HOME/.config/mt-stack/gpt_config.yaml" ]] || cp "$REPO/config/gpt_config.yaml" "$HOME/.config/mt-stack/gpt_config.yaml"

for f in "$REPO"/launchd/*.plist; do
  sed -e "s#__REPO__#$REPO#g" -e "s#__MT_HOME__#$MT_HOME#g" "$f" > "$LA/${f:t}"
  echo "wrote $LA/${f:t}"
done
for s in mt-start mt-stop mt-status mt-translate; do ln -sf "$REPO/scripts/$s" "$HOME/bin/$s"; done

echo
echo "Next:"
echo "  1. ollama pull aya-expanse:32b   (or set the model in your manga-image-translator .env)"
echo "  2. chrome://extensions -> Developer mode -> Load unpacked -> $REPO/extension"
echo "  3. mt-start"
