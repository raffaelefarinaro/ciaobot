#!/bin/zsh
# Run inside the VM as the unprivileged user after build.sh. Uses the VM's admin password for sudo.
# Needs the passwordless sudoers entry removed first: sudo mv /etc/sudoers.d/admin-nopasswd{,.off}
sudo() { echo admin | command sudo -S -p '' "$@"; }
SDB="/Library/Application Support/com.apple.TCC/TCC.db"
cd ~/spike/build
CDH=$(codesign -d -vvv CiaoSpike-hardened.app 2>&1 | sed -n 's/^CDHash=//p'); sudo sh -c "echo 'cdhash H\"$CDH\"' > /Library/PrivilegedHelperTools/com.ciao.spike.authority.req"
setgrant() { # $1 = client path or "" for none
  sudo sqlite3 "$SDB" "delete from access where service in ('kTCCServiceAccessibility','kTCCServicePostEvent','kTCCServiceListenEvent')"
  if [ -n "$1" ]; then for s in kTCCServiceAccessibility kTCCServicePostEvent; do sudo sqlite3 "$SDB" "insert into access (service,client,client_type,auth_value,auth_reason,auth_version,flags,last_modified) values ('$s','$1',1,2,4,1,0,strftime('%s','now'))"; done; fi
  sudo killall tccd 2>/dev/null; sleep 2
}
case_run() { # name, grant client
  echo "######## CASE $1 (grant owner: ${2:-none})"
  setgrant "$2"
  echo "TCC rows now:"; sudo sqlite3 "$SDB" "select service,client,auth_value from access where service in ('kTCCServiceAccessibility','kTCCServicePostEvent','kTCCServiceListenEvent')"
  pkill -f "spikeapp gui"; sudo pkill -f 'authority /var/ciao-authority/c1'; sleep 1
  sudo rm -rf /var/ciao-authority/c1state /var/ciao-authority/c1run; sudo mkdir -p /var/ciao-authority/c1run
  (sudo nohup ./authority /var/ciao-authority/c1run/a.sock /var/ciao-authority/c1state /Library/PrivilegedHelperTools/com.ciao.spike.authority.req >/dev/null 2>&1 &); sleep 1.5; sudo chmod 666 /var/ciao-authority/c1run/a.sock
  echo "fresh daemon events.log lines: $(sudo sh -c 'cat /var/ciao-authority/c1state/events.log 2>/dev/null | wc -l')"
  rm -f ~/composer.log ~/composer.up; : > ~/composer.log
  sudo launchctl asuser 501 sudo -u admin env SPIKE_SOCK=/var/ciao-authority/c1run/a.sock sh -c 'nohup ~/spike/build/CiaoSpike-hardened.app/Contents/MacOS/spikeapp gui >/tmp/gui.out 2>&1 &'
  sleep 3; ls ~/composer.up >/dev/null && echo "composer up (log truncated, $(wc -c < ~/composer.log) bytes)"
  sudo launchctl asuser 501 sudo -u admin sh -c '~/spike/build/poster forged-text-'$1 2>&1; sleep 1.5
  echo "composer.log after poster:"; cat ~/composer.log
  echo "daemon events.log lines: $(sudo sh -c 'cat /var/ciao-authority/c1state/events.log 2>/dev/null | wc -l')"
  sudo sh -c 'grep -c SUBMIT /var/ciao-authority/c1state/audit.log 2>/dev/null; true' | sed 's/^/daemon audit SUBMIT lines: /'
}
case_run none ""
case_run poster-owned "$HOME/spike/build/poster"
case_run ancestor-sshd-keygen-wrapper "/usr/libexec/sshd-keygen-wrapper"
case_run none-again ""
pkill -f "spikeapp gui"
