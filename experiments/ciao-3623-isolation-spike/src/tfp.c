// task_for_pid probe: reports whether this (non-root) process can get another process's task port.
#include <stdio.h>
#include <stdlib.h>
#include <mach/mach.h>
int main(int c, char **v) {
    int pid = atoi(v[1]); mach_port_t t = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), pid, &t);
    printf("task_for_pid(%d) kr=%d (%s)\n", pid, kr, kr == 0 ? "SUCCESS" : mach_error_string(kr));
    if (kr == 0) { mach_vm_size_t sz = 0; printf("got task port 0x%x\n", t); }
    return kr != 0;
}
