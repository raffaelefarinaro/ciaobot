#!/usr/bin/env python3
"""Linux principal-isolation probe (run as root inside a throwaway container).
Authority = uid 2001 (ciaoauth), agent/engine = uid 2002 (same uid for both). Measures SO_PEERCRED,
state-file access, socket replacement and what the authority can learn about a peer."""
import os, socket, struct, subprocess, sys, time, hashlib

AUTH, AGENT = 2001, 2002
os.makedirs('/srv/authority/state', exist_ok=True); os.makedirs('/srv/authority/run', exist_ok=True)
os.chown('/srv/authority', AUTH, AUTH); os.chown('/srv/authority/state', AUTH, AUTH); os.chown('/srv/authority/run', AUTH, AUTH)
os.chmod('/srv/authority/state', 0o700); os.chmod('/srv/authority/run', 0o755)
SOCK = '/srv/authority/run/a.sock'

def authority():
    os.setgid(AUTH); os.setuid(AUTH)
    open('/srv/authority/state/key', 'wb').write(os.urandom(32)); os.chmod('/srv/authority/state/key', 0o600)
    s = socket.socket(socket.AF_UNIX); s.bind(SOCK); os.chmod(SOCK, 0o666); s.listen(8)
    while True:
        c, _ = s.accept()
        pid, uid, gid = struct.unpack('3i', c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        msg = c.recv(200).decode()
        try: exe = os.readlink(f'/proc/{pid}/exe'); h = hashlib.sha256(open(exe, 'rb').read()).hexdigest()[:12]
        except Exception as e: exe, h = f'unreadable ({e.__class__.__name__}: {e.strerror})', '-'
        c.send(f'peer uid={uid} gid={gid} pid={pid} exe={exe} sha={h} msg={msg}'.encode()); c.close()

sys.stdout.flush()
pid = os.fork()
if pid == 0: authority()
time.sleep(1)

def as_agent(fn):
    sys.stdout.flush(); r, w = os.pipe(); p = os.fork()
    if p == 0:
        os.close(r); os.setgid(AGENT); os.setuid(AGENT); os.dup2(w, 1)
        try: fn()
        except Exception as e: print('EXC', e)
        sys.stdout.flush(); os._exit(0)
    os.close(w); os.waitpid(p, 0); return os.read(r, 4000).decode().strip()

def probe(name, fn): print(f'{name}: {as_agent(fn)}')
def tryop(f):
    def g():
        try: print('OK', f())
        except Exception as e: print('DENIED', e.__class__.__name__, getattr(e, 'strerror', e))
    return g

probe('read state/key', tryop(lambda: open('/srv/authority/state/key', 'rb').read()))
probe('list state', tryop(lambda: os.listdir('/srv/authority/state')))
probe('write state', tryop(lambda: open('/srv/authority/state/events.log', 'w').write('x')))
probe('unlink socket', tryop(lambda: os.unlink(SOCK)))
probe('rename run dir', tryop(lambda: os.rename('/srv/authority/run', '/srv/authority/run2')))
probe('kill authority', tryop(lambda: os.kill(pid, 9)))
probe('ptrace-read authority memory (/proc/pid/mem)', tryop(lambda: open(f'/proc/{pid}/mem', 'rb').read(1)))
probe('read authority environ', tryop(lambda: open(f'/proc/{pid}/environ', 'rb').read(1)))
def conn():
    s = socket.socket(socket.AF_UNIX); s.connect(SOCK); s.send(b'SUBMIT forged'); print(s.recv(300).decode())
probe('connect to authority socket (0666) as agent uid', conn)
# two different agent-uid programs: indistinguishable by uid, distinguishable only if authority can read /proc/pid/exe
def conn2():
    open('/tmp/copy', 'wb').write(open('/usr/local/bin/python3.12', 'rb').read()); os.chmod('/tmp/copy', 0o755)
    subprocess.run(['/tmp/copy', '-c', "import socket;s=socket.socket(socket.AF_UNIX);s.connect('%s');s.send(b'from-copied-binary');print(s.recv(300).decode())" % SOCK])
probe('copied binary run by agent uid (authority is non-root)', conn2)
os.kill(pid, 9); os.waitpid(pid, 0)
# variant: authority as root can read peer exe
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    os.unlink(SOCK) if os.path.exists(SOCK) else None
    s = socket.socket(socket.AF_UNIX); s.bind(SOCK); os.chmod(SOCK, 0o666); s.listen(8)
    while True:
        c, _ = s.accept(); p, u, g = struct.unpack('3i', c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try: exe = os.readlink(f'/proc/{p}/exe'); r = f'ROOT authority sees uid={u} exe={exe} sha={hashlib.sha256(open(exe,"rb").read()).hexdigest()[:12]}'
        except Exception as e: r = f'ROOT authority sees uid={u} exe unreadable ({e.strerror})'
        c.send(r.encode()); c.close()
time.sleep(1)
probe('root authority: normal agent connection', conn)
probe('root authority: copied binary', conn2)
os.kill(pid, 9)
print('kernel.yama.ptrace_scope =', open('/proc/sys/kernel/yama/ptrace_scope').read().strip() if os.path.exists('/proc/sys/kernel/yama/ptrace_scope') else 'n/a')
