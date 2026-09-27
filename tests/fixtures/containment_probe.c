#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static char input[65537];
static char mode[40];
static char argument[96];

static long long now_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) exit(2);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void pause_ms(unsigned ms) {
    struct timespec ts = { .tv_sec = ms / 1000, .tv_nsec = (long)(ms % 1000) * 1000000 };
    while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {}
}

static void result(const char *name, const char *status, const char *fields) {
    printf("Magma V2.29-4 [Seed = 1]\nquit.\nPROBE %s %s %s\n", name, status, fields);
    fflush(stdout);
}

static void invalid(void) {
    fputs("invalid probe request\n", stderr);
    exit(2);
}

static unsigned number(const char *s, unsigned minimum, unsigned maximum) {
    char *end;
    unsigned long n;
    if (!*s) invalid();
    for (const char *digit = s; *digit; digit++)
        if (*digit < '0' || *digit > '9') invalid();
    errno = 0;
    n = strtoul(s, &end, 10);
    if (errno || *end || n < minimum || n > maximum) invalid();
    return (unsigned)n;
}

static void nonce(const char *s) {
    size_t n = strlen(s);
    if (n == 0 || n > 32) invalid();
    for (size_t i = 0; i < n; i++) {
        if (!((s[i] >= 'a' && s[i] <= 'z') ||
              (s[i] >= 'A' && s[i] <= 'Z') ||
              (s[i] >= '0' && s[i] <= '9'))) invalid();
    }
}

static void nonce_hold(char *s, unsigned *hold) {
    char *colon = strchr(s, ':');
    *hold = 0;
    if (colon) {
        *colon++ = 0;
        *hold = number(colon, 0, 3000);
    }
    nonce(s);
}

static void parse_request(void) {
    size_t n = fread(input, 1, sizeof(input), stdin);
    if (n == sizeof(input) || ferror(stdin)) invalid();
    if (memchr(input, 0, n)) invalid();
    input[n] = 0;
    int found = 0;
    char *save;
    for (char *line = strtok_r(input, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        if (strncmp(line, "CALC_PROBE", 10) != 0) continue;
        if (found++ || line[10] != ' ') invalid();
        char extra;
        int fields = sscanf(line, "CALC_PROBE %39s %95s %c", mode, argument, &extra);
        if (fields < 1 || fields > 2) invalid();
        if (fields == 1) argument[0] = 0;
    }
    if (found != 1) invalid();
}

static void no_argument(void) { if (*argument) invalid(); }
static void need_argument(void) { if (!*argument) invalid(); }

static void network(const char *name, int family, const char *address, unsigned port,
                    const char *target) {
    int fd = socket(family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int error = fd < 0 ? errno : 0;
    int connected = 0;
    long long start = now_ms();
    if (fd >= 0) {
        struct sockaddr_storage storage = {0};
        socklen_t length;
        if (family == AF_INET) {
            struct sockaddr_in *v4 = (struct sockaddr_in *)&storage;
            v4->sin_family = AF_INET;
            v4->sin_port = htons((uint16_t)port);
            inet_pton(AF_INET, address, &v4->sin_addr);
            length = sizeof(*v4);
        } else {
            struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&storage;
            v6->sin6_family = AF_INET6;
            v6->sin6_port = htons((uint16_t)port);
            inet_pton(AF_INET6, address, &v6->sin6_addr);
            length = sizeof(*v6);
        }
        int rc = connect(fd, (struct sockaddr *)&storage, length);
        if (rc == 0) connected = 1;
        else if (errno == EINPROGRESS) {
            struct pollfd pfd = { .fd = fd, .events = POLLOUT };
            rc = poll(&pfd, 1, 250);
            if (rc > 0) {
                socklen_t size = sizeof(error);
                if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0)
                    connected = error == 0;
                else error = errno;
            } else error = rc == 0 ? ETIMEDOUT : errno;
        } else error = errno;
        close(fd);
    }
    char fields[160];
    snprintf(fields, sizeof(fields), "family=%s target=%s port=%u connected=%d errno=%d elapsed_ms=%lld",
             family == AF_INET ? "inet" : "inet6", target, port, connected,
             connected ? 0 : error, now_ms() - start);
    result(name, connected ? "OK" : "BLOCKED", fields);
}

static socklen_t abstract_address(struct sockaddr_un *address, const char *name) {
    memset(address, 0, sizeof(*address));
    address->sun_family = AF_UNIX;
    snprintf(address->sun_path + 1, sizeof(address->sun_path) - 1, "calc-probe-%s", name);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 +
                       strlen(address->sun_path + 1));
}

static void abstract_socket(int listen_mode) {
    need_argument();
    unsigned hold = 0;
    if (listen_mode) nonce_hold(argument, &hold);
    else nonce(argument);
    struct sockaddr_un address;
    socklen_t length = abstract_address(&address, argument);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int error = fd < 0 ? errno : 0;
    long long attempt = now_ms();
    int ok = 0;
    if (fd >= 0) {
        int rc = listen_mode ? bind(fd, (struct sockaddr *)&address, length) :
                               connect(fd, (struct sockaddr *)&address, length);
        if (rc == 0 && listen_mode) rc = listen(fd, 1);
        ok = rc == 0;
        if (!ok) error = errno;
        if (ok && listen_mode) pause_ms(hold);
        close(fd);
    }
    char fields[160];
    snprintf(fields, sizeof(fields), "nonce=%s attempted_ms=%lld closed_ms=%lld errno=%d",
             argument, attempt, now_ms(), ok ? 0 : error);
    result(mode, ok ? "OK" : (listen_mode ? "DENIED" : "BLOCKED"), fields);
}

static void tmp_file(int write_mode) {
    need_argument();
    unsigned hold = 0;
    if (write_mode) nonce_hold(argument, &hold);
    else nonce(argument);
    char path[128], fields[160];
    snprintf(path, sizeof(path), "/tmp/calc-probe-%s", argument);
    int fd = open(path, write_mode ? O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC : O_RDONLY | O_CLOEXEC, 0600);
    int error = fd < 0 ? errno : 0;
    int ok = fd >= 0;
    if (ok && write_mode) {
        ok = write(fd, "probe\n", 6) == 6;
        if (!ok) error = errno;
        if (ok) pause_ms(hold);
    } else if (ok) {
        char marker[6];
        ok = read(fd, marker, sizeof(marker)) == 6 && memcmp(marker, "probe\n", 6) == 0;
        if (!ok) error = errno ? errno : EIO;
    }
    if (fd >= 0) close(fd);
    snprintf(fields, sizeof(fields), "nonce=%s operation=%s errno=%d",
             argument, write_mode ? "write" : "read", ok ? 0 : error);
    result(mode, ok ? "OK" : (!write_mode && error == ENOENT ? "ABSENT" : "DENIED"), fields);
}

static void tmp_scan(void) {
    no_argument();
    DIR *dir = opendir("/tmp");
    if (!dir) {
        char fields[64];
        snprintf(fields, sizeof(fields), "operation=scan errno=%d", errno);
        result(mode, "DENIED", fields);
        return;
    }
    unsigned count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)))
        if (strncmp(entry->d_name, "calc-probe-", 11) == 0) count++;
    closedir(dir);
    char fields[64];
    snprintf(fields, sizeof(fields), "operation=scan probe_entries=%u", count);
    result(mode, count == 0 ? "EMPTY" : "OK", fields);
}

static void path_write(void) {
    need_argument();
    char *separator = strchr(argument, ':');
    if (!separator) invalid();
    *separator++ = 0;
    nonce(separator);
    const char *base = NULL;
    if (strcmp(argument, "root") == 0) base = "";
    else if (strcmp(argument, "app") == 0) base = "/app";
    else if (strcmp(argument, "data") == 0) base = "/data";
    else if (strcmp(argument, "magma") == 0) base = "/opt/magma";
    else if (strcmp(argument, "home") == 0) base = "/home/calculator";
    else if (strcmp(argument, "usrlib") == 0) base = "/usr/lib";
    else if (strcmp(argument, "lib") == 0) base = "/lib";
    else invalid();
    char path[160], fields[160];
    snprintf(path, sizeof(path), "%s/calc-probe-%s", base, separator);
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    int error = fd < 0 ? errno : 0;
    if (fd >= 0) {
        close(fd);
        unlink(path);
    }
    snprintf(fields, sizeof(fields), "target=%s nonce=%s operation=create errno=%d",
             argument, separator, error);
    result(mode, fd >= 0 ? "OK" : "DENIED", fields);
}

static void tmp_exec(const char *self) {
    need_argument();
    nonce(argument);
    char path[128], fields[160];
    snprintf(path, sizeof(path), "/tmp/calc-probe-exec-%s", argument);
    int source = open(self, O_RDONLY | O_CLOEXEC);
    int destination = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0700);
    int error = source < 0 || destination < 0 ? errno : 0;
    if (!error) {
        char buffer[8192];
        ssize_t n;
        while ((n = read(source, buffer, sizeof(buffer))) > 0) {
            ssize_t done = 0;
            while (done < n) {
                ssize_t count = write(destination, buffer + done, (size_t)(n - done));
                if (count <= 0) { error = errno ? errno : EIO; break; }
                done += count;
            }
            if (error) break;
        }
        if (n < 0) error = errno;
    }
    if (source >= 0) close(source);
    if (destination >= 0) close(destination);
    if (!error) {
        int request_pipe[2];
        if (pipe(request_pipe) < 0) error = errno;
        else {
            static const char request[] = "CALC_PROBE answer\n";
            if (write(request_pipe[1], request, sizeof(request) - 1) != sizeof(request) - 1)
                error = errno ? errno : EIO;
            close(request_pipe[1]);
            if (!error) {
                pid_t child = fork();
                if (child < 0) error = errno;
                else if (child == 0) {
                    char *const args[] = { path, "-w", "-n", NULL };
                    if (dup2(request_pipe[0], STDIN_FILENO) < 0) _exit(errno);
                    if (request_pipe[0] != STDIN_FILENO) close(request_pipe[0]);
                    close(STDOUT_FILENO);
                    close(STDERR_FILENO);
                    execv(path, args);
                    _exit(errno > 0 && errno < 256 ? errno : EIO);
                } else {
                    int status;
                    pid_t waited;
                    do { waited = waitpid(child, &status, 0); }
                    while (waited < 0 && errno == EINTR);
                    if (waited < 0) error = errno;
                    else if (!WIFEXITED(status)) error = EIO;
                    else error = WEXITSTATUS(status);
                }
            }
            close(request_pipe[0]);
        }
    }
    unlink(path);
    snprintf(fields, sizeof(fields), "nonce=%s operation=copy_exec errno=%d", argument, error);
    result(mode, error ? "DENIED" : "OK", fields);
}

static void memory_touch(void) {
    need_argument();
    unsigned mib = number(argument, 1, 512);
    size_t length = (size_t)mib * 1024 * 1024;
    volatile unsigned char *memory = mmap(NULL, length, PROT_READ | PROT_WRITE,
                                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    int error = memory == MAP_FAILED ? errno : 0;
    unsigned touched = 0;
    if (!error) {
        for (size_t offset = 0; offset < length; offset += 4096) {
            memory[offset] = 1;
            touched++;
        }
        munmap((void *)memory, length);
    }
    char fields[96];
    snprintf(fields, sizeof(fields), "requested_mib=%u touched_pages=%u errno=%d", mib, touched, error);
    result(mode, error ? "DENIED" : "OK", fields);
}

static void fork_limit(void) {
    need_argument();
    unsigned requested = number(argument, 1, 80);
    pid_t children[80];
    unsigned created = 0;
    int gate[2];
    if (pipe(gate) != 0) exit(2);
    int error = 0;
    for (; created < requested; created++) {
        pid_t child = fork();
        if (child < 0) { error = errno; break; }
        if (child == 0) {
            close(gate[1]);
            char unused;
            ssize_t received = read(gate[0], &unused, 1);
            if (received < 0) _exit(2);
            _exit(0);
        }
        children[created] = child;
    }
    close(gate[0]);
    close(gate[1]);
    for (unsigned i = 0; i < created; i++) waitpid(children[i], NULL, 0);
    char fields[96];
    snprintf(fields, sizeof(fields), "requested=%u created=%u errno=%d", requested, created, error);
    result(mode, error ? "DENIED" : "OK", fields);
}

static void cpu_burn(void) {
    need_argument();
    unsigned duration = number(argument, 1, 3000);
    pid_t children[2];
    unsigned created = 0;
    long long start = now_ms();
    for (; created < 2; created++) {
        pid_t child = fork();
        if (child < 0) break;
        if (child == 0) {
            volatile unsigned long work = 0;
            while (now_ms() - start < duration) work++;
            _exit(0);
        }
        children[created] = child;
    }
    int error = created == 2 ? 0 : errno;
    for (unsigned i = 0; i < created; i++) waitpid(children[i], NULL, 0);
    char fields[96];
    snprintf(fields, sizeof(fields), "workers=%u elapsed_ms=%lld errno=%d",
             created, now_ms() - start, error);
    result(mode, error ? "DENIED" : "OK", fields);
}

static void flood(int fd) {
    need_argument();
    unsigned bytes = number(argument, 1, 1024 * 1024 - 128);
    char block[4096];
    memset(block, 'A', sizeof(block));
    unsigned written = 0;
    while (written < bytes) {
        size_t count = bytes - written < sizeof(block) ? bytes - written : sizeof(block);
        ssize_t n = write(fd, block, count);
        if (n <= 0) { if (n == 0) errno = EIO; break; }
        written += (unsigned)n;
    }
    char fields[96];
    snprintf(fields, sizeof(fields), "requested=%u written=%u errno=%d",
             bytes, written, written == bytes ? 0 : errno);
    result(mode, written == bytes ? "OK" : "DENIED", fields);
}

static void descendant_hold(void) {
    need_argument();
    unsigned duration = number(argument, 1, 8000);
    pid_t child = fork();
    if (child < 0) {
        char fields[64];
        snprintf(fields, sizeof(fields), "operation=fork errno=%d", errno);
        result(mode, "DENIED", fields);
        return;
    }
    if (child == 0) { pause_ms(duration); _exit(0); }
    char fields[96];
    snprintf(fields, sizeof(fields), "child_pid=%ld hold_ms=%u started_ms=%lld",
             (long)child, duration, now_ms());
    result(mode, "OK", fields);
    waitpid(child, NULL, 0);
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "-w") || strcmp(argv[2], "-n")) invalid();
    parse_request();
    if (!strcmp(mode, "answer")) { no_argument(); result(mode, "OK", "42"); }
    else if (!strcmp(mode, "net4")) { need_argument(); network(mode, AF_INET, "127.0.0.1", number(argument, 1, 65535), "loopback"); }
    else if (!strcmp(mode, "net6")) { need_argument(); network(mode, AF_INET6, "::1", number(argument, 1, 65535), "loopback"); }
    else if (!strcmp(mode, "metadata")) { no_argument(); network(mode, AF_INET, "169.254.169.254", 80, "metadata"); }
    else if (!strcmp(mode, "abstract_listen")) abstract_socket(1);
    else if (!strcmp(mode, "abstract_connect")) abstract_socket(0);
    else if (!strcmp(mode, "tmp_write")) tmp_file(1);
    else if (!strcmp(mode, "tmp_read")) tmp_file(0);
    else if (!strcmp(mode, "tmp_scan")) tmp_scan();
    else if (!strcmp(mode, "path_write")) path_write();
    else if (!strcmp(mode, "tmp_exec")) tmp_exec(argv[0]);
    else if (!strcmp(mode, "memory_touch")) memory_touch();
    else if (!strcmp(mode, "fork_limit")) fork_limit();
    else if (!strcmp(mode, "cpu_burn")) cpu_burn();
    else if (!strcmp(mode, "stdout_flood")) flood(STDOUT_FILENO);
    else if (!strcmp(mode, "stderr_flood")) flood(STDERR_FILENO);
    else if (!strcmp(mode, "descendant_hold")) descendant_hold();
    else invalid();
    return 0;
}
