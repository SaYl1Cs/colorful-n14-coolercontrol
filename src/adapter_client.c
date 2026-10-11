/* Tiny bounded Unix-socket client: no Python, shell, NVML or ACPI access. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#ifndef SOCKET_PATH
#define SOCKET_PATH "/run/colorful-n14-control/adapter.sock"
#endif

static int valid(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "set-duty") == 0) {
        size_t len = strlen(argv[2]);
        if (!len || len > 3 || strspn(argv[2], "0123456789") != len)
            return 0;
        return atoi(argv[2]) <= 100;
    }
    if (argc != 2) return 0;
    const char *actions[] = {"get-rpm", "get-duty", "temp-cpu", "temp-gpu",
                             "enable", "reset", "self-check"};
    for (size_t i = 0; i < sizeof(actions)/sizeof(actions[0]); i++)
        if (strcmp(argv[1], actions[i]) == 0) return 1;
    return 0;
}

int main(int argc, char **argv) {
    if (!valid(argc, argv)) {
        fputs("usage: get-rpm|get-duty|temp-cpu|temp-gpu|enable|reset|self-check|set-duty 0..100\n", stderr);
        return 2;
    }
    signal(SIGPIPE, SIG_IGN);
    /* Bound connect as well as sends/receives if the server is wedged. */
    alarm(4);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct timeval timeout = {.tv_sec = 3};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0) {
        perror("socket timeout"); close(fd); return 1;
    }
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    if (strlen(SOCKET_PATH) >= sizeof(addr.sun_path)) {
        close(fd); return 1;
    }
    strcpy(addr.sun_path, SOCKET_PATH);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("N14 adapter service unavailable"); close(fd); return 1;
    }
    char request[64];
    int count = argc == 3 ? snprintf(request, sizeof(request), "%s %s\n", argv[1], argv[2])
                          : snprintf(request, sizeof(request), "%s\n", argv[1]);
    size_t sent = 0;
    while (sent < (size_t)count) {
        ssize_t n = send(fd, request + sent, (size_t)count - sent, 0);
        if (n <= 0) {
            if (errno == EINTR) continue;
            perror("send"); close(fd); return 1;
        }
        sent += (size_t)n;
    }
    char reply[1024];
    size_t used = 0;
    while (used < sizeof(reply)-1) {
        ssize_t n = recv(fd, reply + used, sizeof(reply)-1-used, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        used += (size_t)n;
        if (memchr(reply, '\n', used)) break;
    }
    close(fd);
    alarm(0);
    reply[used] = '\0';
    if (!used || reply[used-1] != '\n') {
        fputs("invalid or timed-out adapter response\n", stderr); return 1;
    }
    if (strncmp(reply, "OK ", 3) == 0) {
        /* Write commands historically return no stdout. */
        if (strcmp(reply+3, "ok\n") != 0 || strcmp(argv[1], "self-check") == 0)
            fputs(reply+3, stdout);
        return 0;
    }
    fputs(reply, stderr);
    return 1;
}
