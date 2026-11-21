#!/bin/sh
# Installs (or updates) the Timelog GNOME Shell extension for the current user.
# Run it on the desktop, from a clone of the repository. On Wayland, log out and back in
# after the first install; then it is enabled here and set up from its Settings menu item.
set -eu
cd "$(dirname "$0")"

uuid=timelog@subsite.fi
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

# pack compiles schemas/ and includes extension.js, prefs.js, metadata.json and stylesheet.css.
gnome-extensions pack --force --out-dir "$out" .
gnome-extensions install --force "$out/$uuid.shell-extension.zip"

if gnome-extensions enable "$uuid" 2>/dev/null; then
    echo "Installed and enabled $uuid. Open its menu in the top bar → Settings to add the address and API key."
    echo "(After an update, log out and back in to load the new version.)"
else
    echo "Installed $uuid. Log out and back in, then run: gnome-extensions enable $uuid"
fi
