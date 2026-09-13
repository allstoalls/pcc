/*
 * Host-C oracle for py/py_http_runtime.py.
 *
 * The production pcc-Python archive compiles the owned implementation and
 * deliberately excludes this object.  Keep this source as the independent C
 * behavior oracle for the ordinary C runtime and focused parity tests.
 */
#include "py_internal.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef _WIN32
#include <dlfcn.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#endif

/* libcurl is loaded at runtime so HTTPS stays a small C-kernel transport
 * primitive and does not become a build-time dependency of every pcc binary. */
#ifndef _WIN32
typedef void PccCurl;
typedef int PccCurlCode;
typedef PccCurl *(*PccCurlEasyInit)(void);
typedef PccCurlCode (*PccCurlEasySetopt)(PccCurl *, int, ...);
typedef PccCurlCode (*PccCurlEasyPerform)(PccCurl *);
typedef void (*PccCurlEasyCleanup)(PccCurl *);

enum {
    PCC_CURLOPT_WRITEDATA = 10001,
    PCC_CURLOPT_URL = 10002,
    PCC_CURLOPT_WRITEFUNCTION = 20011,
    PCC_CURLOPT_USERAGENT = 10018,
    PCC_CURLOPT_LOW_SPEED_LIMIT = 19,
    PCC_CURLOPT_LOW_SPEED_TIME = 20,
    PCC_CURLOPT_FAILONERROR = 45,
    PCC_CURLOPT_FOLLOWLOCATION = 52,
    PCC_CURLOPT_CONNECTTIMEOUT = 78,
    PCC_CURLOPT_NOSIGNAL = 99
};

static size_t pcc_curl_write(void *ptr, size_t size, size_t nmemb, void *stream) {
    return fwrite(ptr, size, nmemb, (FILE *)stream);
}

static void *open_system_libcurl(void) {
#ifdef __APPLE__
    const char *names[] = {
        "/usr/lib/libcurl.4.dylib",
        "libcurl.4.dylib",
        "libcurl.dylib",
        NULL
    };
#else
    const char *names[] = {"libcurl.so.4", "libcurl.so", NULL};
#endif
    for (size_t i = 0; names[i] != NULL; i++) {
        void *handle = dlopen(names[i], RTLD_LAZY | RTLD_LOCAL);
        if (handle != NULL) return handle;
    }
    return NULL;
}

static int download_with_system_libcurl(const char *url, const char *dest) {
    void *library = open_system_libcurl();
    if (library == NULL) return -10;
    PccCurlEasyInit easy_init = (PccCurlEasyInit)dlsym(library, "curl_easy_init");
    PccCurlEasySetopt easy_setopt =
        (PccCurlEasySetopt)dlsym(library, "curl_easy_setopt");
    PccCurlEasyPerform easy_perform =
        (PccCurlEasyPerform)dlsym(library, "curl_easy_perform");
    PccCurlEasyCleanup easy_cleanup =
        (PccCurlEasyCleanup)dlsym(library, "curl_easy_cleanup");
    if (easy_init == NULL || easy_setopt == NULL || easy_perform == NULL
        || easy_cleanup == NULL) {
        dlclose(library);
        return -11;
    }
    FILE *out = fopen(dest, "wb");
    if (out == NULL) {
        dlclose(library);
        return -12;
    }
    PccCurl *curl = easy_init();
    if (curl == NULL) {
        fclose(out);
        dlclose(library);
        return -13;
    }
    int configured = 1;
    configured &= easy_setopt(curl, PCC_CURLOPT_URL, url) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_WRITEDATA, out) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_WRITEFUNCTION, pcc_curl_write) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_USERAGENT, "pcc-owned-acquire/1") == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_FOLLOWLOCATION, 1L) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_FAILONERROR, 1L) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_CONNECTTIMEOUT, 20L) == 0;
    /* No fixed total-transfer timeout: a 20MB sdist at modest bandwidth
     * legitimately exceeds any wall-clock cap (a hard 60s cap made
     * `pcc1 -m pip install numpy` fail exactly at the bandwidth cliff).
     * Abort on STALL instead: under 1KiB/s for 30 consecutive seconds. */
    configured &= easy_setopt(curl, PCC_CURLOPT_LOW_SPEED_LIMIT, 1024L) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_LOW_SPEED_TIME, 30L) == 0;
    configured &= easy_setopt(curl, PCC_CURLOPT_NOSIGNAL, 1L) == 0;
    PccCurlCode rc = configured ? easy_perform(curl) : -1;
    easy_cleanup(curl);
    fclose(out);
    dlclose(library);
    if (rc != 0) {
        remove(dest);
        return -14;
    }
    return 0;
}
#endif

static int parse_http_url(
    const char *url,
    char *host,
    size_t host_cap,
    char *port,
    size_t port_cap,
    char *path,
    size_t path_cap
) {
    const char *prefix = "http://";
    size_t prefix_len = strlen(prefix);
    if (url == NULL || strncmp(url, prefix, prefix_len) != 0) return -1;
    const char *authority = url + prefix_len;
    const char *slash = strchr(authority, '/');
    const char *end = slash != NULL ? slash : url + strlen(url);
    const char *colon = NULL;
    for (const char *p = authority; p < end; p++) {
        if (*p == ':') {
            colon = p;
            break;
        }
    }
    size_t host_len = (size_t)((colon != NULL ? colon : end) - authority);
    if (host_len == 0 || host_len >= host_cap) return -1;
    memcpy(host, authority, host_len);
    host[host_len] = '\0';
    if (colon != NULL) {
        size_t port_len = (size_t)(end - colon - 1);
        if (port_len == 0 || port_len >= port_cap) return -1;
        memcpy(port, colon + 1, port_len);
        port[port_len] = '\0';
    } else {
        if (port_cap < 3) return -1;
        strcpy(port, "80");
    }
    const char *path_src = slash != NULL ? slash : "/";
    size_t path_len = strlen(path_src);
    if (path_len == 0 || path_len >= path_cap) return -1;
    memcpy(path, path_src, path_len + 1);
    return 0;
}

#ifndef _WIN32
static int send_all(int fd, const char *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
        int64_t n = pcc_platform_socket_send(
            (int64_t)fd, buf + off, (int64_t)(len - off), 0
        );
#else
        ssize_t n = send(fd, buf + off, len - off, 0);
#endif
        if (n < 0) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
            if (n == -EINTR) continue;
#else
            if (errno == EINTR) continue;
#endif
            return -1;
        }
        if (n == 0) return -1;
        off += (size_t)n;
    }
    return 0;
}

static int connect_http_socket(const char *host, const char *port) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
    return (int)pcc_platform_tcp_connect(host, port);
#else
    struct addrinfo hints;
    struct addrinfo *result = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    int rc = getaddrinfo(host, port, &hints, &result);
    if (rc != 0) return -1;
    int fd = -1;
    for (struct addrinfo *rp = result; rp != NULL; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, rp->ai_addr, rp->ai_addrlen) == 0) break;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(result);
    return fd;
#endif
}
#endif

int64_t py_http_download_to_file(PyObject *url_obj, PyObject *dest_obj) {
#ifdef _WIN32
    (void)url_obj;
    (void)dest_obj;
    return -1;
#else
    const char *url = py_str_utf8(url_obj);
    const char *dest = py_str_utf8(dest_obj);
#ifndef _WIN32
    if (strncmp(url, "http://", 7) == 0 || strncmp(url, "https://", 8) == 0) {
        int curl_rc = download_with_system_libcurl(url, dest);
        if (curl_rc == 0 || strncmp(url, "https://", 8) == 0) return curl_rc;
    }
#endif
    char host[512];
    char port[32];
    char path[4096];
    if (parse_http_url(url, host, sizeof(host), port, sizeof(port), path, sizeof(path)) != 0) {
        return -2;
    }

    int fd = connect_http_socket(host, port);
    if (fd < 0) return -3;

    char request[8192];
    int req_len = snprintf(
        request,
        sizeof(request),
        "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\nUser-Agent: pcc/1\r\n\r\n",
        path,
        host
    );
    if (req_len <= 0 || (size_t)req_len >= sizeof(request)) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
        (void)pcc_platform_close((int64_t)fd);
#else
        close(fd);
#endif
        return -4;
    }
    if (send_all(fd, request, (size_t)req_len) != 0) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
        (void)pcc_platform_close((int64_t)fd);
#else
        close(fd);
#endif
        return -5;
    }

    FILE *out = fopen(dest, "wb");
    if (out == NULL) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
        (void)pcc_platform_close((int64_t)fd);
#else
        close(fd);
#endif
        return -6;
    }

    char buf[8192];
    char header[65536];
    size_t header_len = 0;
    int header_done = 0;
    int status_ok = 0;
    for (;;) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
        int64_t n = pcc_platform_socket_recv(
            (int64_t)fd, buf, (int64_t)sizeof(buf), 0
        );
#else
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
#endif
        if (n < 0) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_SOCKET
            if (n == -EINTR) continue;
#else
            if (errno == EINTR) continue;
#endif
            fclose(out);
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
            (void)pcc_platform_close((int64_t)fd);
#else
            close(fd);
#endif
            return -7;
        }
        if (n == 0) break;
        size_t off = 0;
        if (!header_done) {
            while (off < (size_t)n && header_len + 1 < sizeof(header)) {
                header[header_len++] = buf[off++];
                header[header_len] = '\0';
                if (
                    header_len >= 4
                    && header[header_len - 4] == '\r'
                    && header[header_len - 3] == '\n'
                    && header[header_len - 2] == '\r'
                    && header[header_len - 1] == '\n'
                ) {
                    header_done = 1;
                    status_ok = strncmp(header, "HTTP/1.0 200", 12) == 0
                        || strncmp(header, "HTTP/1.1 200", 12) == 0;
                    break;
                }
            }
            if (!header_done) continue;
        }
        if (off < (size_t)n) {
            if (fwrite(buf + off, 1, (size_t)n - off, out) != (size_t)n - off) {
                fclose(out);
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
                (void)pcc_platform_close((int64_t)fd);
#else
                close(fd);
#endif
                return -8;
            }
        }
    }
    fclose(out);
#ifdef PCC_USE_FREESTANDING_PLATFORM_IO
    (void)pcc_platform_close((int64_t)fd);
#else
    close(fd);
#endif
    return status_ok ? 0 : -9;
#endif
}
