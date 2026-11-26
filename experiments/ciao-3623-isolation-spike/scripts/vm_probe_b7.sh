#!/bin/zsh
# DYLD injection control/inject run in the VM (needs the authority built and install.sh run once).
sudo() { echo admin | command sudo -S -p '' "$@"; }
cd ~/spike/build
pkill -f 'spikeapp gui'; sudo pkill -f 'authority /var/ciao-authority/c1'; sleep 1
sudo rm -rf /var/ciao-authority/c1state /var/ciao-authority/c1run; sudo mkdir -p /var/ciao-authority/c1run
(sudo nohup ./authority /var/ciao-authority/c1run/a.sock /var/ciao-authority/c1state /Library/PrivilegedHelperTools/com.ciao.spike.authority.req >/dev/null 2>&1 &); sleep 1.5; sudo chmod 666 /var/ciao-authority/c1run/a.sock
export SPIKE_SOCK=/var/ciao-authority/c1run/a.sock
codesign -d --verbose=2 CiaoSpike-hardened.app 2>&1 | grep -E "flags"; codesign -d --verbose=2 CiaoSpike-plain.app 2>&1 | grep -E "flags"
for v in hardened plain; do
  CDH=$(codesign -d -vvv CiaoSpike-$v.app 2>&1 | sed -n 's/^CDHash=//p'); sudo sh -c "echo 'cdhash H\"$CDH\"' > /Library/PrivilegedHelperTools/com.ciao.spike.authority.req"
  for mode in control inject; do
    rm -f /tmp/inj.out
    if [ $mode = inject ]; then INJECT_OUT=/tmp/inj.out DYLD_INSERT_LIBRARIES=$PWD/inject.dylib CiaoSpike-$v.app/Contents/MacOS/spikeapp who > /tmp/app.out 2>&1; else CiaoSpike-$v.app/Contents/MacOS/spikeapp who > /tmp/app.out 2>&1; fi
    echo "$v/$mode: app exit=$? app says: $(cut -c1-30 /tmp/app.out) | dylib ran: $([ -f /tmp/inj.out ] && echo yes: $(cat /tmp/inj.out) || echo no)"
  done
done
