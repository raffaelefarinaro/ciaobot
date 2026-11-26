#!/bin/sh
# Provision _ciao + root LaunchDaemon (run with sudo inside the macOS VM). Idempotent.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BIN=/Library/PrivilegedHelperTools/com.ciao.spike.authority
LABEL=com.ciao.spike.authority
if ! dscl . -read /Users/_ciao >/dev/null 2>&1; then
  dscl . -create /Groups/_ciao; dscl . -create /Groups/_ciao PrimaryGroupID 299
  dscl . -create /Users/_ciao; dscl . -create /Users/_ciao UniqueID 299
  dscl . -create /Users/_ciao PrimaryGroupID 299; dscl . -create /Users/_ciao UserShell /usr/bin/false
  dscl . -create /Users/_ciao NFSHomeDirectory /var/empty; dscl . -create /Users/_ciao RealName "Ciao authority"
  dscl . -create /Users/_ciao IsHidden 1
fi
mkdir -p /Library/PrivilegedHelperTools
install -o root -g wheel -m 755 "$HERE/build/authority" $BIN
# pin the operator app by cdhash of the installed (plain or hardened) bundle executable
APP=${1:-$HERE/build/CiaoSpike-hardened.app}
CDH=$(codesign -d -vvv "$APP" 2>&1 | sed -n 's/^CDHash=//p')
echo "cdhash H\"$CDH\"" > /tmp/req.$$; install -o root -g wheel -m 644 /tmp/req.$$ $BIN.req; rm /tmp/req.$$
install -d -o _ciao -g _ciao -m 755 /var/ciao-authority /var/ciao-authority/run
install -d -o _ciao -g _ciao -m 700 /var/ciao-authority/state
cat > /tmp/plist.$$ <<EOF
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>UserName</key><string>_ciao</string><key>GroupName</key><string>_ciao</string>
<key>ProgramArguments</key><array><string>$BIN</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>/var/ciao-authority/state/stderr.log</string>
</dict></plist>
EOF
install -o root -g wheel -m 644 /tmp/plist.$$ /Library/LaunchDaemons/$LABEL.plist; rm /tmp/plist.$$
launchctl bootout system/$LABEL 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/$LABEL.plist
sleep 1; launchctl print system/$LABEL | grep -E "state|pid|user" | head
