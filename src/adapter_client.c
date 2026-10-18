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

#define MAX_PROVENANCE 65536U
#define MAX_REQUEST (MAX_PROVENANCE + 128U)
#define MAX_REPLY (MAX_PROVENANCE + 1024U)

static int decimal_duty(const char *text) {
    size_t len = strlen(text);
    unsigned value = 0;
    if (len == 0 || len > 3) return -1;
    for (size_t i = 0; i < len; i++) {
        if (text[i] < '0' || text[i] > '9') return -1;
        value = value * 10U + (unsigned)(text[i] - '0');
    }
    return value <= 100U ? (int)value : -1;
}

static int valid(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "set-duty") == 0)
        return decimal_duty(argv[2]) >= 0;
    if (argc == 3 && strcmp(argv[1], "keylight-set") == 0)
        return decimal_duty(argv[2]) >= 0;
    if (argc == 3 && (strcmp(argv[1], "install-assignment") == 0 ||
                      strcmp(argv[1], "renew-assignment") == 0)) {
        size_t len = strlen(argv[2]);
        return len > 0 && len <= MAX_PROVENANCE && strpbrk(argv[2], "\r\n") == NULL;
    }
    if (argc == 4 && strcmp(argv[1], "set-duty-provenance") == 0) {
        if (decimal_duty(argv[2]) < 0) return 0;
        size_t len = strlen(argv[3]);
        if (len == 0 || len > MAX_PROVENANCE) return 0;
        return strpbrk(argv[3], "\r\n") == NULL;
    }
    if (argc != 2) return 0;
    const char *actions[] = {"get-rpm", "get-duty", "temp-cpu", "temp-gpu",
                             "temp-cpu-json", "temp-gpu-json", "temp-cpu/json", "temp-gpu/json",
                             "enable", "reset", "self-check", "fence-close", "fence-close-full",
                             "fence-status", "assignment-status", "keylight-get", "keylight-save",
                             "keylight-restore", "lid-refresh"};
    for (size_t i = 0; i < sizeof(actions) / sizeof(actions[0]); i++)
        if (strcmp(argv[1], actions[i]) == 0) return 1;
    return 0;
}

static int make_request(int argc, char **argv, char **request, size_t *length) {
    size_t needed;
    if (argc == 4) {
        size_t duty_len = strlen(argv[2]);
        size_t label_len = strlen(argv[3]);
        if (label_len > MAX_PROVENANCE || duty_len > 3 ||
            label_len > MAX_REQUEST - duty_len - 25U) return -1;
        needed = duty_len + label_len + strlen("set-duty-provenance  \n") + 1U;
        if (needed > MAX_REQUEST) return -1;
        *request = malloc(needed);
        if (*request == NULL) return -1;
        int written = snprintf(*request, needed, "set-duty-provenance %s %s\n",
                               argv[2], argv[3]);
        if (written < 0 || (size_t)written >= needed) {
            free(*request);
            *request = NULL;
            return -1;
        }
        *length = (size_t)written;
        return 0;
    }
    if (argc == 3) {
        needed = strlen(argv[1]) + strlen(argv[2]) + 3U;
        if (needed > MAX_REQUEST) return -1;
        *request = malloc(needed);
        if (*request == NULL) return -1;
        int written = snprintf(*request, needed, "%s %s\n", argv[1], argv[2]);
        if (written < 0 || (size_t)written >= needed) {
            free(*request);
            *request = NULL;
            return -1;
        }
        *length = (size_t)written;
        return 0;
    }
    needed = strlen(argv[1]) + 2U;
    if (needed > MAX_REQUEST) return -1;
    *request = malloc(needed);
    if (*request == NULL) return -1;
    int written = snprintf(*request, needed, "%s\n", argv[1]);
    if (written < 0 || (size_t)written >= needed) {
        free(*request);
        *request = NULL;
        return -1;
    }
    *length = (size_t)written;
    return 0;
}

static int send_all(int fd, const char *data, size_t length) {
    size_t sent = 0;
    while (sent < length) {
        ssize_t n = send(fd, data + sent, length - sent, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        sent += (size_t)n;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (!valid(argc, argv)) {
        fputs("usage: get-rpm|get-duty|temp-cpu|temp-gpu|temp-cpu-json|temp-gpu-json|"
              "enable|reset|self-check|temp-cpu/json|temp-gpu/json|fence-close|"
              "fence-close-full|fence-status|assignment-status|keylight-get|"
              "keylight-save|keylight-restore|lid-refresh|set-duty 0..100|"
              "keylight-set 0..100|set-duty-provenance 0..100 JSON\n", stderr);
        return 2;
    }
    signal(SIGPIPE, SIG_IGN);
    /* Bound connect as well as sends/receives if the server is wedged. */
    alarm(4);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct timeval timeout = {.tv_sec = 3, .tv_usec = 0};
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

    char *request = NULL;
    size_t request_length = 0;
    if (make_request(argc, argv, &request, &request_length) != 0) {
        fputs("request is too large\n", stderr);
        close(fd);
        return 1;
    }
    int result = send_all(fd, request, request_length);
    free(request);
    if (result != 0) {
        perror("send"); close(fd); return 1;
    }

    char reply[MAX_REPLY + 1U];
    size_t used = 0;
    while (used < MAX_REPLY) {
        ssize_t n = recv(fd, reply + used, MAX_REPLY - used, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        used += (size_t)n;
        if (memchr(reply, '\n', used) != NULL) break;
    }
    close(fd);
    alarm(0);
    if (!used || used > MAX_REPLY || reply[used - 1U] != '\n' ||
        memchr(reply, '\n', used) != reply + used - 1U || memchr(reply, '\0', used)) {
        fputs("invalid or timed-out adapter response\n", stderr); return 1;
    }
    reply[used] = '\0';
    if (strncmp(reply, "OK ", 3) == 0) {
        /* Write commands historically return no stdout; reads and status RPCs do. */
        if (strcmp(reply + 3, "ok\n") != 0 ||
            strcmp(argv[1], "self-check") == 0 ||
            strcmp(argv[1], "get-rpm") == 0 || strcmp(argv[1], "get-duty") == 0 ||
            strcmp(argv[1], "temp-cpu") == 0 || strcmp(argv[1], "temp-gpu") == 0 ||
            strcmp(argv[1], "temp-cpu-json") == 0 || strcmp(argv[1], "temp-gpu-json") == 0 ||
            strcmp(argv[1], "fence-status") == 0 || strcmp(argv[1], "assignment-status") == 0 ||
            strcmp(argv[1], "keylight-get") == 0)
            fputs(reply + 3, stdout);
        return 0;
    }
    fputs(reply, stderr);
    return 1;
}
