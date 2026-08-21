"""Safe-command classifier tests.

The contract is asymmetric: a false negative costs one approval card, a false
positive auto-runs a write. Every ambiguous case below is pinned to "unsafe".
"""

from __future__ import annotations

import pytest

from ciao.providers.safe_commands import is_destructive_command, is_read_only_command


@pytest.mark.parametrize("command", [
    "ls",
    "ls -la",
    "pwd",
    "cat README.md",
    "head -n 20 ciao/app.py",
    "tail -50 server.log",
    "wc -l *.py",
    "which python3",
    "echo hello world",
    "rg TODO ciao/",
    "grep -rn 'pattern' tests",
    "find . -name '*.py'",
    "find /tmp -type f -mtime +7",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1 -- ciao/app.py",
    "git show abc123",
    "git branch",
    "git branch -a -v",
    "git blame ciao/app.py",
    # Pipelines and chains are safe when every segment is.
    "ls -la | grep foo",
    "git status | head -20",
    "cat a.txt | grep x | wc -l",
    "ls && pwd",
    "ls; pwd",
    "find . -name '*.py' | wc -l",
    # Quoted operators are data, not shell syntax.
    'echo "a > b"',
    "grep 'foo|bar' file.txt",
    "rg 'a;b' src",
    "date +%Y-%m-%d",
    "date -u",
    "date --date yesterday",
    "hostname --short",
    "hostname -I",
])
def test_read_only_commands_are_safe(command):
    assert is_read_only_command(command) is True


@pytest.mark.parametrize("command", [
    "",
    "   ",
    "rm -rf /",
    "sudo ls",
    "mv a b",
    "cp a b",
    "chmod +x script.sh",
    "python script.py",
    "unknowncmd --flag",
    # A single unsafe segment poisons the whole chain.
    "ls; rm -rf /",
    "git status && git push",
    "ls || rm x",
    "cat file | tee out.txt",
    "grep foo|rm -rf /",
    "find . | xargs rm",
    # Redirection, background jobs, subshells.
    "ls > out.txt",
    "echo hi >> log.txt",
    "wc -l < file.txt",
    "ls & rm x",
    "(ls)",
    "diff <(ls a) <(ls b)",
    # Command substitution, even inside an otherwise safe command.
    "echo $(rm -rf /)",
    "echo `rm -rf /`",
    'echo "pwned: $(rm -rf /)"',
    # A second line is a second command; shlex would fold it into the first.
    "cat file\nrm -rf /",
    # Unbalanced quoting: whatever the shell makes of it, not verified.
    "echo 'unclosed",
    # Env assignments are not verified.
    "FOO=bar ls",
    # bash treats `#` as a comment only at word start; shlex's default
    # commenters would hide the write/second command behind the `#`.
    "cat foo#>out.txt",
    "ls a#; rm -rf /tmp/x",
    # Guarded commands with write-capable arguments.
    "find . -delete",
    "find . -exec rm {} ;",
    "find . -execdir rm {} ;",
    "find . -name '*.log' -fprint /tmp/out",
    "rg --pre 'rm -rf /tmp/x' pattern .",
    "rg --pre=gunzip pattern .",
    "tree -o /tmp/out",
    "tree --output=/tmp/out",
    "date -s 12:00",
    "date --set=12:00",
    "hostname new-name",
    "hostname -F /tmp/name",
    "git branch new-feature",
    "git branch -D main",
    "git log --output=/tmp/x",
    "git diff --ext-diff",
    "git diff --textconv",
    "git show --textconv",
    "git push",
    "git checkout main",
    "git",
])
def test_write_capable_or_unverifiable_commands_are_unsafe(command):
    assert is_read_only_command(command) is False


# ── Destructive-command classifier (the permissive auto default) ─────────
# The contract here is the mirror image of the read-only one: a false positive
# costs an approval card, a false negative auto-runs a removal. The bias leans
# toward "not destructive" when in doubt, because the whole point of the
# permissive default is to stop asking on ordinary writes.


@pytest.mark.parametrize("command", [
    "rm file.txt",
    "rm -rf /tmp/cache",
    "rmdir emptydir",
    "rmdir -p a/b/c",
    "unlink file",
    "shred -u secret.txt",
    "truncate -s 0 data.log",
    "wipefs /dev/sdb",
    "mkfs.ext4 /dev/sdb1",
    "fdisk /dev/sdb",
    "parted /dev/sdb mklabel gpt",
    "dd if=/dev/zero of=/dev/sdb",
    "blkdiscard /dev/sdb",
    "pvremove /dev/sdb1",
    "vgremove vgdata",
    "lvremove vg/lv",
    "shutdown now",
    "reboot",
    "poweroff",
    "halt",
    "userdel bob",
    "groupdel devs",
    "sudo rm -rf /tmp/cache",
    # A destructive segment poisons the whole chain.
    "ls; rm -rf /tmp/x",
    "git status && rm file.txt",
    "find . -delete",
    "find . -exec rm {} ;",
    "find . -name '*.log' -delete",
    # Git's own destructive verbs.
    "git clean -fd",
    "git reset --hard HEAD",
    "git restore src/app.py",
    "git rm old.py",
    "git push --force origin main",
    "git push -f origin main",
    "git prune",
])
def test_destructive_commands_are_destructive(command):
    assert is_destructive_command(command) is True


@pytest.mark.parametrize("command", [
    "",
    "ls",
    "cat README.md",
    "rmx tool",
    "rm -h",
    "echo hello",
    "pwd",
    "mkdir -p out",          # creates, does not remove
    "cp a b",
    "mv a b",
    "touch file",
    "cat a > b",
    "git status",
    "git push origin main",  # non-force push
    "git add .",
    "git commit -m 'x'",
    "npm install",
    "pip install foo",
    "brew update",
    # A non-destructive segment does not poison; only removal does.
    "git status && git push",
    "ls -la | grep foo",
    "cat a.txt > b.txt",
    "find . -name '*.log'",
    "sed -i 's/a/b/' f",     # edits in place but does not remove
    "rmdir --help",
])
def test_non_destructive_commands_are_not_destructive(command):
    assert is_destructive_command(command) is False
