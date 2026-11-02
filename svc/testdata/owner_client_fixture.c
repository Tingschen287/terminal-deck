/* n077 测试夹具：扮演「owner 界面程序」的最小客户端。只在测试里编译使用，不是产品代码。
 *
 *   fixture <socket> <request-file> <out-file>
 *       连上写回 socket，发送 request-file 的内容，把响应一行写进 out-file。
 *   fixture spawn <child> <socket> <request-file> <out-file>
 *       以子进程拉起 <child>（参数照传）并等它结束：模拟「某个进程把 owner 界面程序当子进程拉起」。
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static int run_client(const char *sock, const char *request, const char *out) {
    char buf[65536];
    int in = open(request, O_RDONLY);
    if (in < 0) return 2;
    ssize_t n = read(in, buf, sizeof(buf));
    close(in);
    if (n <= 0) return 2;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, sock, sizeof(addr.sun_path) - 1);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) return 3;
    if (write(fd, buf, (size_t)n) != n) return 4;
    char resp[65536];
    size_t got = 0;
    while (got < sizeof(resp) - 1) {
        ssize_t r = read(fd, resp + got, sizeof(resp) - 1 - got);
        if (r <= 0) break;
        got += (size_t)r;
        if (memchr(resp, '\n', got)) break;
    }
    close(fd);
    char tmp[4096];
    snprintf(tmp, sizeof(tmp), "%s.part", out);
    int o = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (o < 0) return 5;
    if (write(o, resp, got) != (ssize_t)got) return 5;
    close(o);
    return rename(tmp, out) == 0 ? 0 : 5;
}

int main(int argc, char **argv) {
    if (argc == 6 && strcmp(argv[1], "spawn") == 0) {
        pid_t pid = fork();
        if (pid == 0) {
            execl(argv[2], argv[2], argv[3], argv[4], argv[5], (char *)NULL);
            _exit(127);
        }
        int status = 0;
        waitpid(pid, &status, 0);
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }
    if (argc == 4) return run_client(argv[1], argv[2], argv[3]);
    fprintf(stderr, "usage\n");
    return 64;
}
