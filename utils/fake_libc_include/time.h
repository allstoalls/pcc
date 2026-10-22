#ifndef _FAKE_TIME_H
#define _FAKE_TIME_H

#include "_fake_defines.h"
#include "_fake_typedefs.h"

#ifndef _FAKE_STRUCT_TIMESPEC
#define _FAKE_STRUCT_TIMESPEC
struct timespec {
    time_t tv_sec;
    long tv_nsec;
};
#endif

struct tm {
    int tm_sec;
    int tm_min;
    int tm_hour;
    int tm_mday;
    int tm_mon;
    int tm_year;
    int tm_wday;
    int tm_yday;
    int tm_isdst;
    long tm_gmtoff;
    const char *tm_zone;
};

/* clock() ticks per second: 1000000 on Darwin and glibc (XSI), 1000 on Win64. */
#ifndef CLOCKS_PER_SEC
#ifdef _WIN64
#define CLOCKS_PER_SEC 1000
#else
#define CLOCKS_PER_SEC 1000000
#endif
#endif

#ifndef CLOCK_REALTIME
#define CLOCK_REALTIME 0
#endif
#ifndef CLOCK_MONOTONIC
#if defined(__APPLE__)
#define CLOCK_MONOTONIC 6
#else
#define CLOCK_MONOTONIC 1
#endif
#endif

clock_t clock(void);
int clock_gettime(clockid_t clock_id, struct timespec *tp);
int clock_getres(clockid_t clock_id, struct timespec *res);
time_t time(time_t *timer);
double difftime(time_t end, time_t beginning);
time_t mktime(struct tm *timeptr);
struct tm *gmtime(const time_t *timer);
struct tm *localtime(const time_t *timer);
struct tm *gmtime_r(const time_t *timer, struct tm *result);
struct tm *localtime_r(const time_t *timer, struct tm *result);
size_t strftime(char *s, size_t max, const char *format, const struct tm *tm);
char *ctime(const time_t *timer);
char *asctime(const struct tm *tm);
int nanosleep(const struct timespec *req, struct timespec *rem);

#endif
