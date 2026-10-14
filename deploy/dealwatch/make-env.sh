#!/usr/bin/env bash
# Writes /srv/dealwatch/.env on the Pi. Run it on the Pi, from a real terminal, so nothing secret is typed
# or pasted anywhere else:
#
#   ssh -t pi-apps /srv/dealwatch/make-env.sh                    first time: login password
#   ssh -t pi-apps /srv/dealwatch/make-env.sh --replace          new login password
#   ssh -t pi-apps /srv/dealwatch/make-env.sh --assistant-token  connect Claude (price checks, smart entry, Ask)
#
# Each run keeps the other values as they are and restarts the app if it is running.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/.env"
MODE="${1:-}"

[ -t 0 ] || { echo "Run this in a terminal (ssh -t ...), it asks for a secret."; exit 1; }

# The value of a key as it stands in the file, quotes included. Empty when absent.
current() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | head -1 || true; }

password="$(current DEALWATCH_AUTH_PASSWORD)"
secret="$(current DEALWATCH_SECRET_KEY)"
token="$(current DEALWATCH_CLAUDE_CODE_OAUTH_TOKEN)"

case "$MODE" in
"" | --replace)
    if [ -f "$ENV_FILE" ] && [ "$MODE" != "--replace" ]; then
        echo "$ENV_FILE already exists. Nothing done."
        echo "New login password: $0 --replace     Claude: $0 --assistant-token"
        exit 1
    fi
    echo "Choose the password for Dealwatch's login page."
    read -r -s -p "Password (12 characters or more): " typed; echo
    read -r -s -p "Again: " again; echo
    [ "$typed" = "$again" ] || { echo "The two entries differ."; exit 1; }
    [ "${#typed}" -ge 12 ] || { echo "Use 12 characters or more."; exit 1; }
    case "$typed" in *"'"*) echo "Leave out the single quote ('), the file format cannot hold it."; exit 1 ;; esac
    password="'$typed'"
    # A new key with a new password: every device has to sign in again.
    secret="'$(openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-48)'"
    ;;
--assistant-token)
    [ -f "$ENV_FILE" ] || { echo "Set the login password first: $0"; exit 1; }
    echo "Paste the token printed by 'make assistant-token' on the Mac (docs/assistant.md)."
    read -r -s -p "Token (hidden): " typed
    # A token copied from a narrow terminal comes in several lines: the rest of the paste is read and joined.
    # "|| [ -n ... ]" keeps a last line that has no line break after it.
    while IFS= read -r -s -t 1 more || [ -n "$more" ]; do typed="$typed$more"; more=""; done; echo
    typed="$(printf %s "$typed" | tr -d '[:space:]')"
    case "$typed" in
    sk-ant-oat*) ;;
    *) echo "That does not look like a Claude plan token (they start with sk-ant-oat). Nothing changed."; exit 1 ;;
    esac
    [ "${#typed}" -ge 100 ] || { echo "Only ${#typed} characters arrived; a token has about 108. Copy all of it and try again. Nothing changed."; exit 1; }
    token="'$typed'"
    ;;
*)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac

umask 077
cat > "$ENV_FILE.new" <<ENV
# Written by make-env.sh. Readable by you only. Never copy this file into git.
DEALWATCH_AUTH_PASSWORD=$password
DEALWATCH_SECRET_KEY=$secret

# Signs Claude in to your Claude plan. Set it with: make-env.sh --assistant-token
DEALWATCH_CLAUDE_CODE_OAUTH_TOKEN=$token
ENV
mv "$ENV_FILE.new" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Written: $ENV_FILE"

if docker compose --project-directory "$DIR" ps -q 2>/dev/null | grep -q .; then
    echo "Restarting the app with the new settings ..."
    docker compose --project-directory "$DIR" up -d --force-recreate --wait
fi
