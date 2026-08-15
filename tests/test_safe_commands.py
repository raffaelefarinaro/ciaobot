"""Safe-command classifier tests.

The contract is asymmetric: a false negative costs one approval card, a false
positive auto-runs a write. Every ambiguous case below is pinned to "unsafe".
"""

from __future__ import annotations

import pytest

from ciao.providers.safe_commands import is_read_only_command


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
    # Guarded commands with write-capable arguments.
    "find . -delete",
    "find . -exec rm {} ;",
    "find . -execdir rm {} ;",
    "find . -name '*.log' -fprint /tmp/out",
    "git branch new-feature",
    "git branch -D main",
    "git log --output=/tmp/x",
    "git push",
    "git checkout main",
    "git",
])
def test_write_capable_or_unverifiable_commands_are_unsafe(command):
    assert is_read_only_command(command) is False
