#!/bin/sh
# Glimpse hub installer: clone (or update) the repository, create its Python environment, put `gli` on PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/lyfeng001/Glimpse/main/install.sh | sh
#
# Or look at it first:
#   curl -fsSLo glimpse-install.sh https://raw.githubusercontent.com/lyfeng001/Glimpse/main/install.sh
#   less glimpse-install.sh && sh glimpse-install.sh
#
# No sudo: everything goes into your home directory. It never touches config.json, data/ or anything outside
# GLIMPSE_DIR and the `gli` symlink. Settings (environment variables):
#   GLIMPSE_DIR      where to put it            (default ~/Glimpse)
#   GLIMPSE_REF      branch or tag to install   (default main; a release tag such as v0.4.2 pins it)
#   GLIMPSE_REPO     repository URL             (default https://github.com/lyfeng001/Glimpse.git)
#   GLIMPSE_BIN_DIR  where `gli` is linked      (default ~/.local/bin)
#
# The whole script is one function called on its last line, so a download cut short runs nothing.
set -eu

glimpse_install() {
  DIR="${GLIMPSE_DIR:-$HOME/Glimpse}"
  REF="${GLIMPSE_REF:-main}"
  REPO="${GLIMPSE_REPO:-https://github.com/lyfeng001/Glimpse.git}"

  say() { printf '%s\n' "$*"; }
  die() { printf 'glimpse install: %s\n' "$*" >&2; exit 1; }

  # --- what the hub needs
  command -v git >/dev/null 2>&1 || die "git is missing (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git)"
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || die "python3 is missing (3.9 or newer)"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' </dev/null \
    || die "python3 is $("$PY" -c 'import platform; print(platform.python_version())' </dev/null); Glimpse's hub needs 3.9 or newer"
  "$PY" -c 'import venv, ensurepip' </dev/null 2>/dev/null \
    || die "python3 has no venv / ensurepip (Debian/Ubuntu: sudo apt install python3-venv)"

  # --- get the code
  if [ -d "$DIR/.git" ]; then
    say "updating $DIR to $REF"
    git -C "$DIR" diff --quiet && git -C "$DIR" diff --cached --quiet \
      || die "$DIR has local changes; commit or stash them, then run this again"
    git -C "$DIR" fetch --quiet --tags origin </dev/null
    if git -C "$DIR" rev-parse --verify --quiet "refs/remotes/origin/$REF" >/dev/null; then
      git -C "$DIR" checkout --quiet "$REF" </dev/null
      git -C "$DIR" merge --quiet --ff-only "origin/$REF" </dev/null || die "$DIR: $REF can't be fast-forwarded"
    else
      git -C "$DIR" checkout --quiet "$REF" </dev/null || die "no branch or tag $REF in $REPO"
    fi
    FRESH=0
  elif [ -e "$DIR" ]; then
    die "$DIR exists and is not a Glimpse checkout; set GLIMPSE_DIR to another place"
  else
    say "installing Glimpse ($REF) into $DIR"
    git clone --quiet --branch "$REF" "$REPO" "$DIR" </dev/null || die "cannot clone $REPO ($REF)"
    FRESH=1
  fi

  # --- Python environment, config.json (from the example, only when missing), `gli` on PATH
  "$DIR/gli" setup </dev/null
  "$DIR/gli" install </dev/null

  command -v tailscale >/dev/null 2>&1 || say "
note: tailscale is not installed. The hub listens on your Tailscale address and gets its HTTPS
certificate from it: https://tailscale.com/download (then: tailscale up; in the admin console enable
MagicDNS and HTTPS Certificates)."

  if [ "$FRESH" = 1 ]; then
    say "
Glimpse is installed in $DIR. Next:
  1. edit $DIR/config.json   (your API keys, and the machines the hub reaches over ssh)
  2. gli password            (optional: a login password for your devices)
  3. gli hub                 (run it; on macOS: $DIR/scripts/install_launchd.sh starts it at login)
  4. gli url                 (a one-time link: open it on your phone, then Share -> Add to Home Screen)"
  else
    say "
Updated. Restart the hub to run the new code (gli hub, or with launchd: launchctl kickstart -k gui/\$(id -u)/dev.glimpse.hub)."
  fi
}

glimpse_install "$@"
