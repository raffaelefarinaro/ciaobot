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
