#!/bin/sh

# Visual-only preview for the first-install terminal experience.
# It does not download, verify, install, or modify anything.

set -eu

fast=0
color=1

usage() {
    cat <<'USAGE'
Usage: sh scripts/install-preview.sh [--fast] [--no-color]

Shows a simulated Ciaobot first-install flow in the terminal.
This preview does not perform an installation.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --fast) fast=1; shift ;;
        --no-color) color=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ ! -t 1 ] || [ -n "${NO_COLOR:-}" ]; then
    color=0
fi

if [ "$color" -eq 1 ]; then
    orange=$(printf '\033[38;5;209m')
    soft=$(printf '\033[38;5;223m')
    green=$(printf '\033[38;5;114m')
    muted=$(printf '\033[38;5;245m')
    reset=$(printf '\033[0m')
else
    orange=
    soft=
    green=
    muted=
    reset=
fi

pause() {
    [ "$fast" -eq 1 ] || sleep "${1:-0.45}"
}

progress_line() {
    percent=$1
    message=$2
    width=28
    filled=$((percent * width / 100))
    empty=$((width - filled))
    blocks='############################'
    spaces='                            '

    printf '\r  %s[%3d%%]%s %s%s%s' \
        "$orange" "$percent" "$reset" \
        "$orange" "$(printf '%.*s' "$filled" "$blocks")" "$reset"
    printf '%s%s %s' \
        "$(printf '%.*s' "$empty" "$spaces")" \
        "$muted" "$message"
}

step() {
    percent=$1
    message=$2
    progress_line "$percent" "$message"
    pause 0.55
    printf ' %s✓%s\n' "$green" "$reset"
}

printf '\n'
printf '%s╭────────────────────────────────────────────────────────────╮%s\n' "$orange" "$reset"
printf '%s│%s  %s› ciao%s  %s· first-install terminal preview%s              %s│%s\n' \
    "$orange" "$reset" "$soft" "$reset" "$muted" "$reset" "$orange" "$reset"
printf '%s╰────────────────────────────────────────────────────────────╯%s\n' "$orange" "$reset"
printf '\n'
printf '  %sWelcome! Let’s get Ciaobot ready for its first hello.%s\n' "$soft" "$reset"
printf '  %sThis is a preview — no files will be downloaded or changed.%s\n\n' "$muted" "$reset"

step 8 'checking macOS and architecture'
step 18 'finding a home for Ciaobot'
step 34 'downloading the signed release'
step 48 'verifying the release signature'
step 61 'unpacking the bundled runtime'
step 73 'setting up the local engine'
step 84 'keeping your workspace safe'
step 94 'starting the menu-bar app'
step 100 'finishing the first hello'

printf '\n'
printf '  %s✦ ciao%s       %sItalian%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ hola%s       %sSpanish%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ salut%s      %sFrench%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ hallo%s      %sGerman%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ olá%s        %sPortuguese%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ こんにちは%s  %sJapanese%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ 안녕하세요%s  %sKorean%s\n' "$orange" "$reset" "$muted" "$reset"
printf '  %s✦ مرحبا%s      %sArabic%s\n' "$orange" "$reset" "$muted" "$reset"

printf '\n'
printf '%s╭────────────────────────────────────────────────────────────╮%s\n' "$green" "$reset"
printf '%s│%s  %s✓ Ciaobot is ready.%s                                   %s│%s\n' \
    "$green" "$reset" "$soft" "$reset" "$green" "$reset"
printf '%s│%s  Open the app and say ciao in any language.              %s│%s\n' \
    "$green" "$reset" "$green" "$reset"
printf '%s╰────────────────────────────────────────────────────────────╯%s\n' "$green" "$reset"
printf '\n'
