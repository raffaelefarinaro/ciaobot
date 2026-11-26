// DYLD_INSERT_LIBRARIES payload: runs inside the real (pinned) app process.
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/un.h>
__attribute__((constructor)) static void go(void) {
    int s = socket(AF_UNIX, SOCK_STREAM, 0); struct sockaddr_un a; memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX; strcpy(a.sun_path, getenv("SPIKE_SOCK") ? getenv("SPIKE_SOCK") : "/var/ciao-authority/run/authority.sock");
    FILE *f = fopen(getenv("INJECT_OUT") ? getenv("INJECT_OUT") : "/tmp/inject.out", "w");
    if (connect(s, (struct sockaddr*)&a, sizeof a) != 0) { fprintf(f, "connect failed\n"); fclose(f); return; }
    const char *m = "SUBMIT injected-from-dylib"; write(s, m, strlen(m));
    char b[512] = {0}; read(s, b, 511); fprintf(f, "%s\n", b); fclose(f);
}
