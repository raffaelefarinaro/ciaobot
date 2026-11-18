import Foundation

/// The recording `hdiutil` double shared by the disk-image tests. It behaves
/// like a very small hdiutil: `create` packs the `-srcfolder` it was given,
/// `attach` mounts that same content at `-mountpoint`, and every call is
/// appended to `$FAKE_HDIUTIL_LOG`. Nothing touches a real disk, so a test can
/// prove the script staged and inspected a volume layout without mounting one.
///
/// Environment:
/// - `FAKE_HDIUTIL_LOG`   call log, one line per call, arguments joined by `|`
/// - `FAKE_HDIUTIL_STATE` directory remembering the last `-srcfolder`
/// - `FAKE_HDIUTIL_PROBE` optional key=value observations
/// - `FAKE_HDIUTIL_VERIFY_STATUS` / `FAKE_HDIUTIL_ATTACH_STATUS` failure knobs
/// - `FAKE_HDIUTIL_SKIP_LINK=1` mounts a volume without the Applications link
/// - `FAKE_HDIUTIL_SKIP_LAYOUT=1` mounts a volume without the `.DS_Store` and
///   `.background.tiff` the staging folder carried
/// - `FAKE_HDIUTIL_CORRUPT_DSSTORE=1` / `FAKE_HDIUTIL_CORRUPT_BACKGROUND=1`
///   append a byte to that mounted file, so it differs from the staged one
/// - `FAKE_HDIUTIL_SKIP_BACKGROUND=1` mounts the volume without `.background.tiff`
/// - `FAKE_HDIUTIL_SWAP_ENGINE=1` appends bytes to the engine's interpreter on the
///   mounted app, so the mounted engine differs from the staged one
/// - `FAKE_HDIUTIL_OUTPUT` a path whose existence is recorded at detach time,
///   so a test can prove nothing was published before the image was verified
enum FakeHdiutil {
    static let script = """
    #!/bin/sh
    [ -n "${FAKE_HDIUTIL_LOG:-}" ] && {
        for argument in "$@"; do printf '%s|' "$argument"; done
        printf '\\n'
    } >> "$FAKE_HDIUTIL_LOG"
    state=${FAKE_HDIUTIL_STATE:?}
    probe=${FAKE_HDIUTIL_PROBE:-/dev/null}
    command=$1
    shift
    case $command in
        create)
            srcfolder=
            pending=
            while [ $# -gt 0 ]; do
                case $1 in
                    -srcfolder) srcfolder=$2; shift 2 ;;
                    -*) shift ;;
                    *) pending=$1; shift ;;
                esac
            done
            [ -n "$srcfolder" ] && [ -n "$pending" ] || exit 1
            printf '%s\\n' "$srcfolder" > "$state/srcfolder"
            printf 'create-stage-app=%s\\n' "$([ -d "$srcfolder/Ciaobot.app" ] && printf yes || printf no)" >> "$probe"
            printf 'create-stage-link=%s\\n' "$(readlink "$srcfolder/Applications" 2>/dev/null)" >> "$probe"
            printf 'create-stage-dsstore=%s\\n' "$([ -f "$srcfolder/.DS_Store" ] && printf yes || printf no)" >> "$probe"
            printf 'create-stage-background=%s\\n' "$([ -f "$srcfolder/.background.tiff" ] && printf yes || printf no)" >> "$probe"
            cp "$srcfolder/.DS_Store" "$state/stage.DS_Store" 2>/dev/null || true
            printf 'stand-in disk image\\n' > "$pending"
            ;;
        verify)
            exit "${FAKE_HDIUTIL_VERIFY_STATUS:-0}"
            ;;
        attach)
            mountpoint=
            while [ $# -gt 0 ]; do
                case $1 in
                    -mountpoint) mountpoint=$2; shift 2 ;;
                    -*) shift ;;
                    *) shift ;;
                esac
            done
            [ -n "$mountpoint" ] || exit 1
            [ "${FAKE_HDIUTIL_ATTACH_STATUS:-0}" = 0 ] || exit "${FAKE_HDIUTIL_ATTACH_STATUS}"
            srcfolder=$(cat "$state/srcfolder")
            mkdir -p "$mountpoint" || exit 1
            cp -R "$srcfolder/Ciaobot.app" "$mountpoint/Ciaobot.app" || exit 1
            if [ "${FAKE_HDIUTIL_SKIP_LAYOUT:-0}" != 1 ]; then
                for layout in .DS_Store .background.tiff; do
                    [ ! -f "$srcfolder/$layout" ] || cp "$srcfolder/$layout" "$mountpoint/$layout" || exit 1
                done
            fi
            [ "${FAKE_HDIUTIL_CORRUPT_DSSTORE:-0}" != 1 ] || printf 'corrupt\\n' >> "$mountpoint/.DS_Store"
            [ "${FAKE_HDIUTIL_CORRUPT_BACKGROUND:-0}" != 1 ] || printf 'corrupt\\n' >> "$mountpoint/.background.tiff"
            [ "${FAKE_HDIUTIL_SKIP_BACKGROUND:-0}" != 1 ] || rm -f "$mountpoint/.background.tiff"
            [ "${FAKE_HDIUTIL_SWAP_ENGINE:-0}" != 1 ] \
                || printf 'CIAOBOT-TEST-ADHOC\n' >> "$mountpoint/Ciaobot.app/Contents/Resources/engine/bin/python3.12" 2>/dev/null \
                || true
            if [ "${FAKE_HDIUTIL_SKIP_LINK:-0}" != 1 ]; then
                ln -s /Applications "$mountpoint/Applications" || exit 1
            fi
            printf 'attach-mountpoint=%s\\n' "$mountpoint" >> "$probe"
            ;;
        detach)
            printf 'detach=%s\\n' "${1:-}" >> "$probe"
            if [ -n "${FAKE_HDIUTIL_OUTPUT:-}" ]; then
                printf 'detach-published=%s\\n' "$([ -e "$FAKE_HDIUTIL_OUTPUT" ] && printf yes || printf no)" >> "$probe"
            fi
            ;;
    esac
    exit 0
    """
}
