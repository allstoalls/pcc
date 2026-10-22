#!/usr/bin/env python3
"""PCC vs clang benchmark suite.

Fair comparison: both compile with LLVM, measure exec-only via isolated JIT/subprocess.
"""

import argparse
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

INLINE_BENCHMARKS = {
    # ── Classic Algorithms ──────────────────────────────────────────
    "fib(38)": """
        int fib(int n) {
            if (n <= 1) return n;
            return fib(n-1) + fib(n-2);
        }
        int main() { return fib(38) % 256; }
    """,

    "ackermann(3,9)": """
        int ack(int m, int n) {
            if (m == 0) return n + 1;
            if (n == 0) return ack(m - 1, 1);
            return ack(m - 1, ack(m, n - 1));
        }
        int main() { return ack(3, 9) % 256; }
    """,

    "tak(24,16,8)": """
        int tak(int x, int y, int z) {
            if (y >= x) return z;
            return tak(tak(x-1, y, z), tak(y-1, z, x), tak(z-1, x, y));
        }
        int main() { return tak(24, 16, 8) % 256; }
    """,

    "collatz_max": """
        int main() {
            int max_steps = 0;
            for (int n = 1; n < 100000; n++) {
                int steps = 0;
                long val = n;
                while (val != 1) {
                    if (val % 2 == 0) val /= 2;
                    else val = 3 * val + 1;
                    steps++;
                }
                if (steps > max_steps) max_steps = steps;
            }
            return max_steps % 256;
        }
    """,

    "gcd_stress": """
        int gcd(int a, int b) { while (b) { int t = b; b = a % b; a = t; } return a; }
        int main() {
            int sum = 0;
            for (int i = 1; i < 10000; i++)
                for (int j = 1; j < 1000; j++)
                    sum += gcd(i, j);
            return sum % 256;
        }
    """,

    # ── Sorting ─────────────────────────────────────────────────────
    "qsort_10k": """
        int arr[10000];
        void swap(int *a, int *b) { int t = *a; *a = *b; *b = t; }
        int partition(int *a, int lo, int hi) {
            int pivot = a[hi], i = lo - 1;
            for (int j = lo; j < hi; j++)
                if (a[j] < pivot) { i++; swap(&a[i], &a[j]); }
            swap(&a[i+1], &a[hi]);
            return i + 1;
        }
        void quicksort(int *a, int lo, int hi) {
            if (lo < hi) {
                int p = partition(a, lo, hi);
                quicksort(a, lo, p-1);
                quicksort(a, p+1, hi);
            }
        }
        int main() {
            int n = 10000;
            for (int i = 0; i < n; i++)
                arr[i] = (i * 2654435761u) % n;
            for (int rep = 0; rep < 50; rep++) {
                for (int i = 0; i < n; i++)
                    arr[i] = (arr[i] + i * 31) % n;
                quicksort(arr, 0, n-1);
            }
            return arr[n/2] % 256;
        }
    """,

    "insertion_sort_5k": """
        int arr[5000];
        int main() {
            int n = 5000;
            for (int i = 0; i < n; i++)
                arr[i] = (n - i + i * 7) % n;
            for (int rep = 0; rep < 20; rep++) {
                for (int i = 1; i < n; i++) {
                    int key = arr[i], j = i - 1;
                    while (j >= 0 && arr[j] > key) {
                        arr[j+1] = arr[j];
                        j--;
                    }
                    arr[j+1] = key;
                }
            }
            return arr[n/2] % 256;
        }
    """,

    "heapsort_10k": """
        int arr[10000];
        void sift_down(int *a, int start, int end) {
            int root = start;
            while (root * 2 + 1 <= end) {
                int child = root * 2 + 1;
                int sw = root;
                if (a[sw] < a[child]) sw = child;
                if (child+1 <= end && a[sw] < a[child+1]) sw = child+1;
                if (sw == root) return;
                int t = a[root]; a[root] = a[sw]; a[sw] = t;
                root = sw;
            }
        }
        int main() {
            int n = 10000;
            for (int i = 0; i < n; i++)
                arr[i] = (int)(((unsigned)i * 1103515245u + 12345u) % (unsigned)n);
            for (int rep = 0; rep < 50; rep++) {
                for (int i = n/2 - 1; i >= 0; i--) sift_down(arr, i, n-1);
                for (int i = n-1; i > 0; i--) {
                    int t = arr[0]; arr[0] = arr[i]; arr[i] = t;
                    sift_down(arr, 0, i-1);
                }
            }
            return arr[n/2] % 256;
        }
    """,

    # ── Numeric / Math ──────────────────────────────────────────────
    "sum_squares_1M": """
        int main() {
            long sum = 0;
            for (int i = 0; i < 1000000; i++)
                sum += (long)i * i;
            return (int)(sum % 256);
        }
    """,

    "prime_count_100k": """
        char sieve[100001];
        int main() {
            int n = 100000;
            for (int i = 2; i <= n; i++) sieve[i] = 1;
            for (int i = 2; i * i <= n; i++)
                if (sieve[i])
                    for (int j = i*i; j <= n; j += i) sieve[j] = 0;
            int count = 0;
            for (int i = 2; i <= n; i++) if (sieve[i]) count++;
            return count % 256;
        }
    """,

    "mandelbrot_128": """
        int main() {
            int W = 128, H = 128, max_iter = 100, total = 0;
            for (int py = 0; py < H; py++) {
                for (int px = 0; px < W; px++) {
                    double x0 = (double)px / W * 3.5 - 2.5;
                    double y0 = (double)py / H * 2.0 - 1.0;
                    double x = 0, y = 0;
                    int iter = 0;
                    while (x*x + y*y <= 4.0 && iter < max_iter) {
                        double xt = x*x - y*y + x0;
                        y = 2.0*x*y + y0;
                        x = xt;
                        iter++;
                    }
                    total += iter;
                }
            }
            return total % 256;
        }
    """,

    "pi_leibniz_10M": """
        int main() {
            double pi = 0.0;
            for (int i = 0; i < 10000000; i++) {
                double term = 1.0 / (2 * i + 1);
                if (i % 2 == 0) pi += term;
                else pi -= term;
            }
            int result = (int)(pi * 1000000);
            return result % 256;
        }
    """,

    "nbody_1k": """
        double bx[50], by[50], bz[50];
        double vx[50], vy[50], vz[50];
        double mass[50];
        int main() {
            int n = 50;
            for (int i = 0; i < n; i++) {
                bx[i] = (i * 17) % 100 - 50;
                by[i] = (i * 31) % 100 - 50;
                bz[i] = (i * 47) % 100 - 50;
                vx[i] = vy[i] = vz[i] = 0;
                mass[i] = 1.0 + (i % 10);
            }
            double dt = 0.01;
            for (int step = 0; step < 1000; step++) {
                for (int i = 0; i < n; i++) {
                    for (int j = i+1; j < n; j++) {
                        double dx = bx[j]-bx[i], dy = by[j]-by[i], dz = bz[j]-bz[i];
                        double dist2 = dx*dx + dy*dy + dz*dz + 0.01;
                        double dist = 1.0;
                        /* Newton's approximation for 1/sqrt */
                        for (int k = 0; k < 3; k++)
                            dist = dist * (1.5 - 0.5 * dist2 * dist * dist);
                        double f = dist * dist * dist;
                        double fx = dx * f, fy = dy * f, fz = dz * f;
                        vx[i] += fx*mass[j]*dt; vy[i] += fy*mass[j]*dt; vz[i] += fz*mass[j]*dt;
                        vx[j] -= fx*mass[i]*dt; vy[j] -= fy*mass[i]*dt; vz[j] -= fz*mass[i]*dt;
                    }
                }
                for (int i = 0; i < n; i++) {
                    bx[i] += vx[i]*dt; by[i] += vy[i]*dt; bz[i] += vz[i]*dt;
                }
            }
            return ((int)(bx[0]*1000)) % 256;
        }
    """,

    # ── Matrix / Array ──────────────────────────────────────────────
    "matmul_64": """
        int A[64][64], B[64][64], C[64][64];
        int main() {
            for (int i = 0; i < 64; i++)
                for (int j = 0; j < 64; j++) {
                    A[i][j] = i + j; B[i][j] = i * j + 1;
                }
            for (int rep = 0; rep < 200; rep++)
                for (int i = 0; i < 64; i++)
                    for (int j = 0; j < 64; j++) {
                        int s = 0;
                        for (int k = 0; k < 64; k++) s += A[i][k] * B[k][j];
                        C[i][j] = s;
                    }
            return C[32][32] % 256;
        }
    """,

    "transpose_256": """
        int M[256][256];
        int main() {
            for (int i = 0; i < 256; i++)
                for (int j = 0; j < 256; j++) M[i][j] = i*256+j;
            for (int rep = 0; rep < 500; rep++)
                for (int i = 0; i < 256; i++)
                    for (int j = i+1; j < 256; j++) {
                        int t = M[i][j]; M[i][j] = M[j][i]; M[j][i] = t;
                    }
            return M[128][64] % 256;
        }
    """,

    "dot_product_1M": """
        int A[1000000], B[1000000];
        int main() {
            for (int i = 0; i < 1000000; i++) { A[i] = i % 100; B[i] = (i*7) % 100; }
            long sum = 0;
            for (int rep = 0; rep < 5; rep++)
                for (int i = 0; i < 1000000; i++) sum += (long)A[i] * B[i];
            return (int)(sum % 256);
        }
    """,

    # ── String / Byte ───────────────────────────────────────────────
    "byte_histogram": """
        unsigned char data[100000];
        int hist[256];
        int main() {
            for (int i = 0; i < 100000; i++) data[i] = (i * 37 + 13) % 256;
            for (int rep = 0; rep < 100; rep++) {
                for (int i = 0; i < 256; i++) hist[i] = 0;
                for (int i = 0; i < 100000; i++) hist[data[i]]++;
            }
            int max = 0;
            for (int i = 0; i < 256; i++) if (hist[i] > max) max = hist[i];
            return max % 256;
        }
    """,

    "bitcount_1M": """
        int popcount(unsigned int x) {
            int c = 0;
            while (x) { c++; x &= x-1; }
            return c;
        }
        int main() {
            int total = 0;
            for (unsigned int i = 0; i < 1000000; i++)
                total += popcount(i);
            return total % 256;
        }
    """,

    # ── Data Structures ─────────────────────────────────────────────
    "binary_search_1M": """
        int arr[100000];
        int bsearch(int *a, int n, int key) {
            int lo = 0, hi = n - 1;
            while (lo <= hi) {
                int mid = lo + (hi - lo) / 2;
                if (a[mid] == key) return mid;
                if (a[mid] < key) lo = mid + 1;
                else hi = mid - 1;
            }
            return -1;
        }
        int main() {
            int n = 100000;
            for (int i = 0; i < n; i++) arr[i] = i * 3;
            int found = 0;
            for (int rep = 0; rep < 200; rep++)
                for (int q = 0; q < n; q += 7)
                    if (bsearch(arr, n, q*3) >= 0) found++;
            return found % 256;
        }
    """,

    "linked_list_walk": """
        struct Node { int val; int next; };
        struct Node pool[50000];
        int main() {
            int n = 50000;
            for (int i = 0; i < n-1; i++) { pool[i].val = i; pool[i].next = i+1; }
            pool[n-1].val = n-1; pool[n-1].next = -1;
            long sum = 0;
            for (int rep = 0; rep < 200; rep++) {
                int cur = 0;
                while (cur != -1) { sum += pool[cur].val; cur = pool[cur].next; }
            }
            return (int)(sum % 256);
        }
    """,

    "hash_table_sim": """
        int table[16384];
        int hash(int key) { return ((unsigned)key * 2654435761u) >> 18; }
        int main() {
            for (int i = 0; i < 16384; i++) table[i] = -1;
            for (int i = 0; i < 10000; i++) table[hash(i)] = i;
            int found = 0;
            for (int rep = 0; rep < 1000; rep++)
                for (int i = 0; i < 10000; i++)
                    if (table[hash(i)] == i) found++;
            return found % 256;
        }
    """,

    # ── Dynamic Programming ─────────────────────────────────────────
    "dp_knapsack": """
        int dp[1001][1001];
        int w[] = {2,3,4,5,1,6,7,3,2,8,4,5,1,9,3,7,2,6,4,8};
        int v[] = {3,4,5,6,2,7,8,4,3,9,5,6,2,10,4,8,3,7,5,9};
        int main() {
            int n = 20, W = 1000;
            for (int rep = 0; rep < 50; rep++) {
                for (int i = 0; i <= n; i++) dp[i][0] = 0;
                for (int j = 0; j <= W; j++) dp[0][j] = 0;
                for (int i = 1; i <= n; i++)
                    for (int j = 1; j <= W; j++) {
                        dp[i][j] = dp[i-1][j];
                        if (w[i-1] <= j && dp[i-1][j-w[i-1]] + v[i-1] > dp[i][j])
                            dp[i][j] = dp[i-1][j-w[i-1]] + v[i-1];
                    }
            }
            return dp[n][W] % 256;
        }
    """,

    "dp_lcs": """
        int dp[501][501];
        char a[501], b[501];
        int main() {
            int na = 500, nb = 500;
            for (int i = 0; i < na; i++) a[i] = 'a' + (i * 7) % 26;
            for (int i = 0; i < nb; i++) b[i] = 'a' + (i * 13) % 26;
            int result = 0;
            for (int rep = 0; rep < 20; rep++) {
                for (int i = 0; i <= na; i++) dp[i][0] = 0;
                for (int j = 0; j <= nb; j++) dp[0][j] = 0;
                for (int i = 1; i <= na; i++)
                    for (int j = 1; j <= nb; j++) {
                        if (a[i-1] == b[j-1]) dp[i][j] = dp[i-1][j-1] + 1;
                        else dp[i][j] = dp[i-1][j] > dp[i][j-1] ? dp[i-1][j] : dp[i][j-1];
                    }
                result = dp[na][nb];
            }
            return result % 256;
        }
    """,

    "dp_edit_distance": """
        int dp[301][301];
        char a[301], b[301];
        int min3(int a, int b, int c) { int m = a < b ? a : b; return m < c ? m : c; }
        int main() {
            int na = 300, nb = 300;
            for (int i = 0; i < na; i++) a[i] = 'a' + (i * 3) % 26;
            for (int i = 0; i < nb; i++) b[i] = 'a' + (i * 5 + 1) % 26;
            int result = 0;
            for (int rep = 0; rep < 50; rep++) {
                for (int i = 0; i <= na; i++) dp[i][0] = i;
                for (int j = 0; j <= nb; j++) dp[0][j] = j;
                for (int i = 1; i <= na; i++)
                    for (int j = 1; j <= nb; j++) {
                        int cost = a[i-1] != b[j-1];
                        dp[i][j] = min3(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+cost);
                    }
                result = dp[na][nb];
            }
            return result % 256;
        }
    """,

    "dp_coin_change": """
        int dp[100001];
        int coins[] = {1, 5, 10, 25, 50, 100};
        int main() {
            int amount = 100000, nc = 6;
            for (int rep = 0; rep < 5; rep++) {
                dp[0] = 0;
                for (int i = 1; i <= amount; i++) dp[i] = 999999;
                for (int c = 0; c < nc; c++)
                    for (int i = coins[c]; i <= amount; i++)
                        if (dp[i - coins[c]] + 1 < dp[i])
                            dp[i] = dp[i - coins[c]] + 1;
            }
            return dp[amount] % 256;
        }
    """,

    "dp_fibonacci_big": """
        int main() {
            long a = 0, b = 1;
            long sum = 0;
            for (int rep = 0; rep < 100000; rep++) {
                a = 0; b = 1;
                for (int i = 0; i < 90; i++) { long t = a + b; a = b; b = t; }
                sum += b;
            }
            return (int)(sum % 256);
        }
    """,

    # ── Bit Manipulation ────────────────────────────────────────────
    "bit_reverse": """
        unsigned rev(unsigned x) {
            x = ((x >> 1) & 0x55555555) | ((x & 0x55555555) << 1);
            x = ((x >> 2) & 0x33333333) | ((x & 0x33333333) << 2);
            x = ((x >> 4) & 0x0F0F0F0F) | ((x & 0x0F0F0F0F) << 4);
            x = ((x >> 8) & 0x00FF00FF) | ((x & 0x00FF00FF) << 8);
            return (x >> 16) | (x << 16);
        }
        int main() {
            unsigned sum = 0;
            for (unsigned i = 0; i < 5000000; i++) sum += rev(i);
            return sum % 256;
        }
    """,

    "crc32_100k": """
        unsigned crc_table[256];
        void init_crc() {
            for (unsigned i = 0; i < 256; i++) {
                unsigned c = i;
                for (int j = 0; j < 8; j++) c = (c >> 1) ^ (0xEDB88320 & (-(c & 1)));
                crc_table[i] = c;
            }
        }
        int main() {
            init_crc();
            unsigned char data[1000];
            for (int i = 0; i < 1000; i++) data[i] = (i * 37 + 13) % 256;
            unsigned crc = 0;
            for (int rep = 0; rep < 100000; rep++) {
                crc = 0xFFFFFFFF;
                for (int i = 0; i < 1000; i++)
                    crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 0xFF];
                crc ^= 0xFFFFFFFF;
            }
            return crc % 256;
        }
    """,

    "xorshift_10M": """
        int main() {
            unsigned x = 123456789, y = 362436069, z = 521288629, w = 88675123;
            unsigned sum = 0;
            for (int i = 0; i < 10000000; i++) {
                unsigned t = x ^ (x << 11);
                x = y; y = z; z = w;
                w = w ^ (w >> 19) ^ t ^ (t >> 8);
                sum += w;
            }
            return sum % 256;
        }
    """,

    "gray_code": """
        int main() {
            unsigned sum = 0;
            for (int rep = 0; rep < 100; rep++)
                for (unsigned i = 0; i < 1000000; i++) {
                    unsigned g = i ^ (i >> 1);
                    unsigned b = g;
                    b ^= b >> 16; b ^= b >> 8; b ^= b >> 4; b ^= b >> 2; b ^= b >> 1;
                    sum += b;
                }
            return sum % 256;
        }
    """,

    # ── Floating Point ──────────────────────────────────────────────
    "newton_sqrt": """
        int main() {
            double sum = 0;
            for (int i = 1; i <= 1000000; i++) {
                double x = (double)i;
                double g = x * 0.5;
                for (int j = 0; j < 20; j++) g = 0.5 * (g + x / g);
                sum += g;
            }
            return (int)(sum) % 256;
        }
    """,

    "euler_e": """
        int main() {
            double e = 1.0;
            int result = 0;
            for (int rep = 0; rep < 1000000; rep++) {
                e = 1.0;
                double fact = 1.0;
                for (int i = 1; i < 20; i++) { fact *= i; e += 1.0 / fact; }
                result = (int)(e * 1000000);
            }
            return result % 256;
        }
    """,

    "taylor_sin": """
        int main() {
            double sum = 0;
            for (int i = 0; i < 2000000; i++) {
                double x = (double)i * 0.000001;
                double term = x, s = x;
                for (int n = 1; n < 10; n++) {
                    term *= -x * x / ((2*n) * (2*n+1));
                    s += term;
                }
                sum += s;
            }
            return (int)(sum * 100) % 256;
        }
    """,

    "horner_poly": """
        int main() {
            double coeffs[] = {1.0, -0.5, 0.25, -0.125, 0.0625, -0.03125, 0.015625, -0.0078125};
            int nc = 8;
            double sum = 0;
            for (int rep = 0; rep < 5000000; rep++) {
                double x = (double)(rep % 1000) * 0.001;
                double r = coeffs[nc-1];
                for (int i = nc-2; i >= 0; i--) r = r * x + coeffs[i];
                sum += r;
            }
            return (int)(sum * 100) % 256;
        }
    """,

    "dot_product_fp": """
        double a[100000], b[100000];
        int main() {
            for (int i = 0; i < 100000; i++) { a[i] = (i%100)*0.01; b[i] = ((i*7)%100)*0.01; }
            double sum = 0;
            for (int rep = 0; rep < 100; rep++)
                for (int i = 0; i < 100000; i++) sum += a[i] * b[i];
            return (int)(sum) % 256;
        }
    """,

    # ── Graph / Tree ────────────────────────────────────────────────
    "tree_depth": """
        int left[100001], right[100001], val[100001];
        int next_node;
        int build(int lo, int hi) {
            if (lo > hi) return 0;
            int mid = (lo + hi) / 2;
            int n = ++next_node;
            val[n] = mid;
            left[n] = build(lo, mid - 1);
            right[n] = build(mid + 1, hi);
            return n;
        }
        int depth(int n) {
            if (n == 0) return 0;
            int l = depth(left[n]);
            int r = depth(right[n]);
            return 1 + (l > r ? l : r);
        }
        int search(int n, int key) {
            if (n == 0) return 0;
            if (val[n] == key) return 1;
            if (key < val[n]) return search(left[n], key);
            return search(right[n], key);
        }
        int main() {
            next_node = 0;
            int root = build(1, 100000);
            int sum = 0;
            for (int rep = 0; rep < 20; rep++)
                for (int i = 1; i <= 100000; i++) sum += search(root, i);
            return (sum + depth(root)) % 256;
        }
    """,

    "graph_bfs": """
        int head[1001], to[20001], nxt[20001], ecnt;
        int q[1001], dist[1001];
        void add(int u, int v) { ecnt++; to[ecnt]=v; nxt[ecnt]=head[u]; head[u]=ecnt; }
        int bfs(int src, int n) {
            for (int i=1;i<=n;i++) dist[i]=-1;
            int front=0, back=0;
            q[back++]=src; dist[src]=0;
            while (front<back) {
                int u=q[front++];
                for (int e=head[u];e;e=nxt[e])
                    if (dist[to[e]]<0) { dist[to[e]]=dist[u]+1; q[back++]=to[e]; }
            }
            int sum=0;
            for (int i=1;i<=n;i++) if(dist[i]>=0) sum+=dist[i];
            return sum;
        }
        int main() {
            int n=1000;
            for (int i=1;i<n;i++) { add(i,i+1); add(i+1,i); }
            for (int i=1;i<=n;i+=3) { int j=(i*7)%n+1; add(i,j); add(j,i); }
            int total=0;
            for (int rep=0;rep<100;rep++)
                for (int s=1;s<=n;s+=10) total+=bfs(s,n);
            return total%256;
        }
    """,

    "floyd_warshall_128": """
        int d[128][128];
        int main() {
            int n=128, INF=999999;
            for (int i=0;i<n;i++) for(int j=0;j<n;j++) d[i][j]= i==j?0:INF;
            for (int i=0;i<n;i++) { d[i][(i+1)%n]=1; d[i][(i+3)%n]=2; d[i][(i*7+5)%n]=3; }
            for (int rep=0;rep<20;rep++)
                for (int k=0;k<n;k++)
                    for (int i=0;i<n;i++)
                        for (int j=0;j<n;j++)
                            if (d[i][k]+d[k][j]<d[i][j]) d[i][j]=d[i][k]+d[k][j];
            return d[0][n/2]%256;
        }
    """,

    # ── Crypto-like ─────────────────────────────────────────────────
    "sha256_like": """
        unsigned rotr(unsigned x, int n) { return (x >> n) | (x << (32-n)); }
        unsigned ch(unsigned x,unsigned y,unsigned z) { return (x&y)^(~x&z); }
        unsigned maj(unsigned x,unsigned y,unsigned z) { return (x&y)^(x&z)^(y&z); }
        int main() {
            unsigned h0=0x6a09e667,h1=0xbb67ae85,h2=0x3c6ef372,h3=0xa54ff53a;
            unsigned h4=0x510e527f,h5=0x9b05688c,h6=0x1f83d9ab,h7=0x5be0cd19;
            for (int rep=0;rep<500000;rep++) {
                unsigned a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
                for (int i=0;i<64;i++) {
                    unsigned s1=rotr(e,6)^rotr(e,11)^rotr(e,25);
                    unsigned t1=h+s1+ch(e,f,g)+0x428a2f98+i;
                    unsigned s0=rotr(a,2)^rotr(a,13)^rotr(a,22);
                    unsigned t2=s0+maj(a,b,c);
                    h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
                }
                h0+=a;h1+=b;h2+=c;h3+=d;h4+=e;h5+=f;h6+=g;h7+=h;
            }
            return (h0^h1^h2^h3)%256;
        }
    """,

    "rc4_like": """
        unsigned char S[256];
        void swap(unsigned char *a, unsigned char *b) { unsigned char t=*a;*a=*b;*b=t; }
        int main() {
            unsigned char key[]={1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16};
            int total=0;
            for (int rep=0;rep<10000;rep++) {
                for (int i=0;i<256;i++) S[i]=i;
                int j=0;
                for (int i=0;i<256;i++) { j=(j+S[i]+key[i%16])%256; swap(&S[i],&S[j]); }
                int i2=0; j=0;
                for (int k=0;k<1000;k++) {
                    i2=(i2+1)%256; j=(j+S[i2])%256; swap(&S[i2],&S[j]);
                    total+=S[(S[i2]+S[j])%256];
                }
            }
            return total%256;
        }
    """,

    "tea_encrypt": """
        void encrypt(unsigned *v, unsigned *k) {
            unsigned v0=v[0],v1=v[1],sum=0,delta=0x9e3779b9;
            for (int i=0;i<32;i++) {
                sum+=delta;
                v0+=((v1<<4)+k[0])^(v1+sum)^((v1>>5)+k[1]);
                v1+=((v0<<4)+k[2])^(v0+sum)^((v0>>5)+k[3]);
            }
            v[0]=v0; v[1]=v1;
        }
        int main() {
            unsigned key[4]={1,2,3,4};
            unsigned data[2]={0,0};
            for (int i=0;i<5000000;i++) encrypt(data,key);
            return (data[0]^data[1])%256;
        }
    """,

    # ── String Processing ───────────────────────────────────────────
    "naive_strstr": """
        char text[100001], pat[101];
        int main() {
            for (int i=0;i<100000;i++) text[i]='a'+(i*3)%26;
            text[100000]=0;
            for (int i=0;i<100;i++) pat[i]='a'+(i*7)%26;
            pat[100]=0;
            int count=0;
            for (int rep=0;rep<50;rep++)
                for (int i=0;i<=99900;i++) {
                    int match=1;
                    for (int j=0;j<100&&match;j++) if(text[i+j]!=pat[j]) match=0;
                    if (match) count++;
                }
            return count%256;
        }
    """,

    "run_length_encode": """
        char data[100000], out[200000];
        int main() {
            for (int i=0;i<100000;i++) data[i]='A'+(i/10)%5;
            int total=0;
            for (int rep=0;rep<200;rep++) {
                int olen=0;
                for (int i=0;i<100000;) {
                    char c=data[i]; int cnt=1;
                    while (i+cnt<100000 && data[i+cnt]==c && cnt<9) cnt++;
                    out[olen++]='0'+cnt; out[olen++]=c;
                    i+=cnt;
                }
                total+=olen;
            }
            return total%256;
        }
    """,

    "caesar_cipher": """
        char buf[100001];
        int main() {
            for (int i=0;i<100000;i++) buf[i]='a'+(i*17)%26;
            int sum=0;
            for (int rep=0;rep<500;rep++) {
                for (int i=0;i<100000;i++) buf[i]='a'+(buf[i]-'a'+3)%26;
                sum+=buf[50000];
            }
            return sum%256;
        }
    """,

    # ── Reduction / Scan ────────────────────────────────────────────
    "prefix_sum_1M": """
        int a[1000000];
        int main() {
            for (int i=0;i<1000000;i++) a[i]=i%100;
            for (int rep=0;rep<10;rep++)
                for (int i=1;i<1000000;i++) a[i]+=a[i-1];
            return a[999999]%256;
        }
    """,

    "max_subarray": """
        int a[1000000];
        int main() {
            for (int i=0;i<1000000;i++) a[i]=(i*2654435761u)%201-100;
            int best=0;
            for (int rep=0;rep<20;rep++) {
                int max_ending=0; best=-999999;
                for (int i=0;i<1000000;i++) {
                    max_ending+=a[i];
                    if (max_ending>best) best=max_ending;
                    if (max_ending<0) max_ending=0;
                }
            }
            return (best<0?-best:best)%256;
        }
    """,

    "min_max_scan": """
        int a[500000];
        int main() {
            for (int i=0;i<500000;i++) a[i]=(int)(((unsigned)i*1103515245u+12345u)%1000000u);
            int mn=a[0],mx=a[0];
            for (int rep=0;rep<200;rep++) {
                mn=a[0]; mx=a[0];
                for (int i=1;i<500000;i++) {
                    if (a[i]<mn) mn=a[i];
                    if (a[i]>mx) mx=a[i];
                }
            }
            return (mn+mx)%256;
        }
    """,

    # ── Stencil / Image-like ────────────────────────────────────────
    "blur_256": """
        int img[258][258], out[258][258];
        int main() {
            for (int i=0;i<258;i++) for(int j=0;j<258;j++) img[i][j]=(i*j)%256;
            for (int rep=0;rep<100;rep++)
                for (int i=1;i<257;i++)
                    for (int j=1;j<257;j++)
                        out[i][j]=(img[i-1][j]+img[i+1][j]+img[i][j-1]+img[i][j+1]+img[i][j])/5;
            return out[128][128]%256;
        }
    """,

    "game_of_life": """
        char grid[102][102], next[102][102];
        int main() {
            for (int i=1;i<=100;i++) for(int j=1;j<=100;j++) grid[i][j]=(i*j+i+j)%3==0;
            for (int gen=0;gen<500;gen++) {
                for (int i=1;i<=100;i++)
                    for (int j=1;j<=100;j++) {
                        int n=grid[i-1][j-1]+grid[i-1][j]+grid[i-1][j+1]
                             +grid[i][j-1]+grid[i][j+1]
                             +grid[i+1][j-1]+grid[i+1][j]+grid[i+1][j+1];
                        next[i][j]=(grid[i][j]&&(n==2||n==3))||(!grid[i][j]&&n==3);
                    }
                for (int i=1;i<=100;i++) for(int j=1;j<=100;j++) grid[i][j]=next[i][j];
            }
            int alive=0;
            for (int i=1;i<=100;i++) for(int j=1;j<=100;j++) alive+=grid[i][j];
            return alive%256;
        }
    """,

    "jacobi_256": """
        double u[66][66], unew[66][66];
        int main() {
            int n=64;
            for (int i=0;i<=n+1;i++) for(int j=0;j<=n+1;j++) { u[i][j]=0; unew[i][j]=0; }
            for (int j=0;j<=n+1;j++) { u[0][j]=1.0; unew[0][j]=1.0; }
            for (int iter=0;iter<500;iter++) {
                for (int i=1;i<=n;i++)
                    for (int j=1;j<=n;j++)
                        unew[i][j]=0.25*(u[i-1][j]+u[i+1][j]+u[i][j-1]+u[i][j+1]);
                for (int i=1;i<=n;i++) for(int j=1;j<=n;j++) u[i][j]=unew[i][j];
            }
            return (int)(u[n/2][n/2]*1000)%256;
        }
    """,

    # ── Misc Compute ────────────────────────────────────────────────
    "tower_of_hanoi": """
        int moves;
        void hanoi(int n, int from, int to, int aux) {
            if (n==0) return;
            hanoi(n-1, from, aux, to);
            moves++;
            hanoi(n-1, aux, to, from);
        }
        int main() {
            for (int rep=0;rep<50;rep++) { moves=0; hanoi(20, 1, 3, 2); }
            return moves%256;
        }
    """,

    "pascal_triangle": """
        int C[501][501];
        int main() {
            for (int rep=0;rep<50;rep++) {
                for (int i=0;i<=500;i++) { C[i][0]=1; for(int j=1;j<=i;j++) C[i][j]=C[i-1][j-1]+C[i-1][j]; }
            }
            return C[500][250]%256;
        }
    """,

    "sieve_segmented": """
        char is_prime[100001];
        int primes[10000];
        int main() {
            int n=100000, pcnt=0;
            for (int rep=0;rep<50;rep++) {
                for (int i=2;i<=n;i++) is_prime[i]=1;
                pcnt=0;
                for (int i=2;i<=n;i++) {
                    if (is_prime[i]) { primes[pcnt++]=i; }
                    for (int j=0;j<pcnt&&primes[j]<=n/i;j++) {
                        is_prime[i*primes[j]]=0;
                        if (i%primes[j]==0) break;
                    }
                }
            }
            return pcnt%256;
        }
    """,

    "counting_sort": """
        int arr[500000], cnt[1001];
        int main() {
            int n=500000, maxv=1000;
            for (int i=0;i<n;i++) arr[i]=(i*2654435761u)%maxv;
            for (int rep=0;rep<20;rep++) {
                for (int i=0;i<=maxv;i++) cnt[i]=0;
                for (int i=0;i<n;i++) cnt[arr[i]]++;
                int idx=0;
                for (int v=0;v<=maxv;v++) for(int c=0;c<cnt[v];c++) arr[idx++]=v;
            }
            return arr[n/2]%256;
        }
    """,

    "merge_sort_10k": """
        int arr[10000], tmp[10000];
        void merge(int l, int m, int r) {
            int i=l,j=m+1,k=l;
            while (i<=m&&j<=r) tmp[k++]=(arr[i]<=arr[j])?arr[i++]:arr[j++];
            while (i<=m) tmp[k++]=arr[i++];
            while (j<=r) tmp[k++]=arr[j++];
            for (int x=l;x<=r;x++) arr[x]=tmp[x];
        }
        void msort(int l, int r) {
            if (l>=r) return;
            int m=(l+r)/2; msort(l,m); msort(m+1,r); merge(l,m,r);
        }
        int main() {
            int n=10000;
            for (int rep=0;rep<100;rep++) {
                for (int i=0;i<n;i++) arr[i]=(int)(((unsigned)i*1103515245u+12345u+(unsigned)rep)%(unsigned)n);
                msort(0,n-1);
            }
            return arr[n/2]%256;
        }
    """,

    "matrix_chain": """
        int dp[101][101];
        int dims[] = {10,20,30,40,50,60,30,20,10,40,50,60,70,80,90,20,30,40,50,60,10};
        int main() {
            int n=20;
            int result=0;
            for (int rep=0;rep<500;rep++) {
                for (int i=1;i<=n;i++) dp[i][i]=0;
                for (int len=2;len<=n;len++)
                    for (int i=1;i<=n-len+1;i++) {
                        int j=i+len-1; dp[i][j]=999999999;
                        for (int k=i;k<j;k++) {
                            int cost=dp[i][k]+dp[k+1][j]+dims[i-1]*dims[k]*dims[j];
                            if (cost<dp[i][j]) dp[i][j]=cost;
                        }
                    }
                result=dp[1][n];
            }
            return result%256;
        }
    """,

    "power_mod": """
        long power(long base, long exp, long mod) {
            long result = 1;
            base %= mod;
            while (exp > 0) {
                if (exp & 1) result = result * base % mod;
                exp >>= 1;
                base = base * base % mod;
            }
            return result;
        }
        int main() {
            long sum = 0;
            for (int i = 1; i <= 1000000; i++)
                sum += power(i, 1000000007, 998244353);
            return (int)(sum % 256);
        }
    """,

    "fenwick_tree": """
        int bit[100001];
        void update(int i, int v, int n) { for(;i<=n;i+=i&(-i)) bit[i]+=v; }
        int query(int i) { int s=0; for(;i>0;i-=i&(-i)) s+=bit[i]; return s; }
        int main() {
            int n=100000;
            for (int i=1;i<=n;i++) update(i,i%100,n);
            int sum=0;
            for (int rep=0;rep<50;rep++)
                for (int i=1;i<=n;i++) sum+=query(i);
            return sum%256;
        }
    """,

    "dfs_cycle": """
        int head[5001], to[20001], nxt[20001], ecnt;
        int vis[5001], col[5001];
        void add(int u,int v) { ecnt++;to[ecnt]=v;nxt[ecnt]=head[u];head[u]=ecnt; }
        int cycles;
        void dfs(int u) {
            col[u]=1;
            for (int e=head[u];e;e=nxt[e]) {
                if (col[to[e]]==1) cycles++;
                else if (!col[to[e]]) dfs(to[e]);
            }
            col[u]=2;
        }
        int main() {
            int n=5000;
            for (int i=1;i<n;i++) add(i,i+1);
            for (int i=1;i<=n;i+=7) add(i,(i*13)%n+1);
            int total=0;
            for (int rep=0;rep<100;rep++) {
                cycles=0;
                for (int i=1;i<=n;i++) col[i]=0;
                for (int i=1;i<=n;i++) if(!col[i]) dfs(i);
                total+=cycles;
            }
            return total%256;
        }
    """,

    "coord_compress": """
        int vals[200000], sorted[200000], rank[200000];
        void isort(int *a, int n) {
            for (int i=1;i<n;i++) { int k=a[i],j=i-1; while(j>=0&&a[j]>k){a[j+1]=a[j];j--;} a[j+1]=k; }
        }
        int main() {
            int n=2000;
            for (int i=0;i<n;i++) vals[i]=(i*2654435761u)%100000;
            int total=0;
            for (int rep=0;rep<100;rep++) {
                for (int i=0;i<n;i++) sorted[i]=vals[i];
                isort(sorted,n);
                int un=0;
                for (int i=0;i<n;i++) if(i==0||sorted[i]!=sorted[i-1]) sorted[un++]=sorted[i];
                for (int i=0;i<n;i++) {
                    int lo=0,hi=un-1,r=0;
                    while(lo<=hi) { int m=(lo+hi)/2; if(sorted[m]<=vals[i]){r=m;lo=m+1;}else hi=m-1; }
                    rank[i]=r;
                }
                for (int i=0;i<n;i++) total+=rank[i];
            }
            return total%256;
        }
    """,

    "interval_merge": """
        int starts[10000], ends[10000];
        void sort_by_start(int n) {
            for (int i=1;i<n;i++) {
                int ks=starts[i],ke=ends[i],j=i-1;
                while(j>=0&&starts[j]>ks){starts[j+1]=starts[j];ends[j+1]=ends[j];j--;}
                starts[j+1]=ks;ends[j+1]=ke;
            }
        }
        int main() {
            int n=10000;
            for (int i=0;i<n;i++) { starts[i]=(i*37)%100000; ends[i]=starts[i]+(i%100)+1; }
            int total=0;
            for (int rep=0;rep<20;rep++) {
                sort_by_start(n);
                int merged=1, cs=starts[0], ce=ends[0];
                for (int i=1;i<n;i++) {
                    if (starts[i]<=ce) { if(ends[i]>ce) ce=ends[i]; }
                    else { merged++; cs=starts[i]; ce=ends[i]; }
                }
                total+=merged;
            }
            return total%256;
        }
    """,

    "roman_numeral": """
        int vals[]={1000,900,500,400,100,90,50,40,10,9,5,4,1};
        int main() {
            int total=0;
            for (int rep=0;rep<100;rep++) {
                for (int num=1;num<=3999;num++) {
                    int n=num, len=0;
                    for (int i=0;i<13;i++) {
                        while(n>=vals[i]) { n-=vals[i]; len++; }
                    }
                    total+=len;
                }
            }
            return total%256;
        }
    """,

    "fizzbuzz_count": """
        int main() {
            int f3=0,f5=0,f15=0;
            for (int rep=0;rep<10000;rep++)
                for (int i=1;i<=100000;i++) {
                    if (i%15==0) f15++;
                    else if (i%3==0) f3++;
                    else if (i%5==0) f5++;
                }
            return (f3+f5+f15)%256;
        }
    """,

    "life_1d": """
        char cells[10001], next_cells[10001];
        int main() {
            int n=10000;
            for (int i=0;i<n;i++) cells[i]=(i*17+3)%3==0;
            int alive=0;
            for (int gen=0;gen<5000;gen++) {
                for (int i=1;i<n-1;i++) {
                    int p=cells[i-1]+cells[i]+cells[i+1];
                    next_cells[i]=(p==1)||(p==2&&cells[i]);
                }
                next_cells[0]=cells[0]; next_cells[n-1]=cells[n-1];
                for (int i=0;i<n;i++) cells[i]=next_cells[i];
            }
            for (int i=0;i<n;i++) alive+=cells[i];
            return alive%256;
        }
    """,

    "histogram_equalize": """
        unsigned char img[100000];
        int hist[256], cdf[256];
        int main() {
            int n=100000;
            for (int i=0;i<n;i++) img[i]=(i*37+13)%256;
            for (int rep=0;rep<100;rep++) {
                for (int i=0;i<256;i++) hist[i]=0;
                for (int i=0;i<n;i++) hist[img[i]]++;
                cdf[0]=hist[0];
                for (int i=1;i<256;i++) cdf[i]=cdf[i-1]+hist[i];
                int cmin=0;
                for (int i=0;i<256;i++) if(cdf[i]>0){cmin=cdf[i];break;}
                for (int i=0;i<n;i++)
                    img[i]=(unsigned char)(((long)(cdf[img[i]]-cmin)*255)/(n-cmin));
            }
            int sum=0; for(int i=0;i<n;i++) sum+=img[i];
            return sum%256;
        }
    """,

    "sparse_matvec": """
        int row[50000],col[50000],val[50000];
        int x[1000],y[1000];
        int main() {
            int n=1000,nnz=50000;
            for (int i=0;i<nnz;i++) { row[i]=i%n; col[i]=(i*7)%n; val[i]=i%10+1; }
            for (int i=0;i<n;i++) x[i]=i+1;
            int sum=0;
            for (int rep=0;rep<200;rep++) {
                for (int i=0;i<n;i++) y[i]=0;
                for (int i=0;i<nnz;i++) y[row[i]]+=val[i]*x[col[i]];
                sum=0; for(int i=0;i<n;i++) sum+=y[i];
            }
            return sum%256;
        }
    """,

    "radix_sort": """
        unsigned arr[100000], tmp[100000];
        void radix(unsigned *a, unsigned *b, int n) {
            for (int shift=0;shift<32;shift+=8) {
                int cnt[256]; for(int i=0;i<256;i++) cnt[i]=0;
                for (int i=0;i<n;i++) cnt[(a[i]>>shift)&0xFF]++;
                int sum=0; for(int i=0;i<256;i++){int t=cnt[i];cnt[i]=sum;sum+=t;}
                for (int i=0;i<n;i++) b[cnt[(a[i]>>shift)&0xFF]++]=a[i];
                unsigned *t=a; for(int i=0;i<n;i++) a[i]=b[i];
            }
        }
        int main() {
            int n=100000;
            for (int i=0;i<n;i++) arr[i]=i*2654435761u;
            for (int rep=0;rep<10;rep++) {
                for (int i=0;i<n;i++) arr[i]=(arr[i]+rep*17)^(i*31);
                radix(arr,tmp,n);
            }
            return arr[n/2]%256;
        }
    """,

    "two_sum_hash": """
        int keys[16384], vals[16384];
        int get(int k) { int h=((unsigned)k*2654435761u)>>18; while(keys[h]!=k&&keys[h]!=-1)h=(h+1)&16383; return vals[h]; }
        void put(int k,int v) { int h=((unsigned)k*2654435761u)>>18; while(keys[h]!=-1&&keys[h]!=k)h=(h+1)&16383; keys[h]=k;vals[h]=v; }
        int main() {
            int arr[]={2,7,11,15,1,8,3,6,4,9,5,10,12,14,13,0};
            int n=16, target=9;
            int found=0;
            for (int rep=0;rep<500000;rep++) {
                for (int i=0;i<16384;i++){keys[i]=-1;vals[i]=-1;}
                for (int i=0;i<n;i++) {
                    int comp=target-arr[i];
                    if (get(comp)>=0) found++;
                    put(arr[i],i);
                }
            }
            return found%256;
        }
    """,
}

EXTERNAL_BENCHMARK_FILES = (
    "binary_trees.c",
    "bitcount.c",
    "crc32.c",
    "dhrystone.c",
    "edit_distance.c",
    "fannkuch_redux.c",
    "fft.c",
    "floyd_warshall.c",
    "huffman.c",
    "sha256.c",
    "spectral_norm.c",
    "state_machine.c",
)


def load_external_benchmarks():
    bench_dir = Path(__file__).resolve().parent / "c"
    loaded = {}
    for filename in EXTERNAL_BENCHMARK_FILES:
        path = bench_dir / filename
        loaded[f"file/{path.stem}"] = path.read_text()
    return loaded


BENCHMARKS = dict(INLINE_BENCHMARKS)
BENCHMARKS.update(load_external_benchmarks())


def clean_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def host_cc():
    for candidate in ("clang", "cc", "gcc"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("No system C compiler found")


def benchmark_names(selected):
    if selected:
        unknown = [name for name in selected if name not in BENCHMARKS]
        if unknown:
            raise KeyError(f"unknown benchmarks: {', '.join(unknown)}")
        return selected
    return list(BENCHMARKS)


def slugify(name):
    return re.sub(r"\W+", "_", name).strip("_") or "bench"


_MAIN_DECL_RE = re.compile(r"\bint\s+main\s*\(", re.MULTILINE)


def _wrap_with_checksum(code: str) -> str:
    """Rewrite source so `main`'s return value is printed to stdout.

    Renames the user's `int main(` definition to `__pcc_bench_main(`, then
    appends a new `main` that calls it, captures the 32-bit int return,
    and prints a deterministic checksum line to stdout. This gives
    bench's correctness gate strict stdout equality instead of the
    current "both exit 0, both print empty" degenerate case.

    Also prepends `#include <stdio.h>` if not already present.
    """
    rewritten, n = _MAIN_DECL_RE.subn("int __pcc_bench_main(", code, count=1)
    if n != 1:
        return code
    prefix = "" if "<stdio.h>" in rewritten else "#include <stdio.h>\n"
    wrapper = (
        "\n"
        "int main(int __pcc_bench_argc, char **__pcc_bench_argv) {\n"
        "    int __pcc_bench_r = __pcc_bench_main();\n"
        "    printf(\"pcc_bench_rv=%d\\n\", __pcc_bench_r);\n"
        "    return __pcc_bench_r;\n"
        "}\n"
    )
    return prefix + rewritten + wrapper


def write_source(workdir, name, code):
    path = Path(workdir) / f"{slugify(name)}.c"
    path.write_text(_wrap_with_checksum(code))
    return path


def fmt(seconds):
    if seconds is None:
        return "N/A"
    if seconds < 0.001:
        return f"{seconds * 1e6:.0f}us"
    if seconds < 1.0:
        return f"{seconds * 1e3:.2f}ms"
    return f"{seconds:.3f}s"


def ratio_str(lhs, rhs):
    if lhs is None or rhs in (None, 0):
        return "N/A"
    return f"{lhs / rhs:.2f}x"


def geometric_mean(values):
    clean = [value for value in values if value and value > 0]
    if not clean:
        return None
    return math.exp(statistics.fmean(math.log(value) for value in clean))


def classify_ratio(value, tie_band=0.05):
    if value is None:
        return "error"
    if value <= 1.0 - tie_band:
        return "faster"
    if value >= 1.0 + tie_band:
        return "slower"
    return "tied"


def run_binary(bin_path, runs, timeout=300):
    subprocess.run(
        [str(bin_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=clean_env(),
    )
    timings = []
    stdout = ""
    stderr = ""
    returncode = None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = subprocess.run(
            [str(bin_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=clean_env(),
        )
        timings.append(time.perf_counter() - t0)
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
    # Methodology: use median of per-run timings, not min. Min amplifies
    # a "best-case noise" sample. Median is more representative of the
    # typical execution cost under the same system noise floor.
    return {
        "exec_time_s": statistics.median(timings) if timings else 0.0,
        "exec_time_min_s": min(timings) if timings else 0.0,
        "exec_time_max_s": max(timings) if timings else 0.0,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    }


def build_clang(src_path, opt_level, workdir):
    cc = host_cc()
    bin_path = Path(workdir) / f"{src_path.stem}.clang.O{opt_level}.out"
    cmd = [
        cc,
        f"-O{opt_level}",
        "-march=native",
        "-o",
        str(bin_path),
        str(src_path),
        "-lm",
    ]
    t0 = time.perf_counter()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env=clean_env(),
    )
    compile_time = time.perf_counter() - t0
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "clang compile failed")
    return bin_path, compile_time


def sanitize_variant_label(label):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", label)


def unique_ordered(items):
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def build_pcc(
    src_path,
    code,
    opt_level,
    workdir,
    *,
    variant_label,
    use_passes,
    disabled_passes=None,
):
    from pcc.frontends.c.evaluator.c_evaluator import (
        _compile_preprocessed_translation_unit_artifact,
        _preprocess_translation_unit_source,
    )
    from pcc.frontends.c.passes import PassContext, PassPipeline

    label = sanitize_variant_label(variant_label)
    bin_path = Path(workdir) / (
        f"{src_path.stem}.pcc.{label}.O{opt_level}.out"
    )

    pipeline = PassPipeline.default()
    ctx = PassContext(opt_level=opt_level)
    if not use_passes:
        pipeline.enabled = False
        ctx.enabled = False
    else:
        for pass_name in disabled_passes or ():
            ctx.disable_pass(pass_name)

    t0 = time.perf_counter()
    codestr = _preprocess_translation_unit_source(code, str(src_path.parent), False)
    artifact = _compile_preprocessed_translation_unit_artifact(
        src_path.name,
        codestr,
        pass_pipeline=pipeline,
        pass_ctx=ctx,
    )
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator, _artifact_to_compiled_unit
    evaluator = CEvaluator(backend="self")
    evaluator.emit_executable([_artifact_to_compiled_unit(artifact)], str(bin_path), optimize=opt_level)
    compile_time = time.perf_counter() - t0
    return bin_path, compile_time, ctx.pass_report()


def _run_variant_matrix(
    variant_defs,
    src_path,
    wrapped_code,
    opt_level,
    tmpdir,
    clang_exec,
    runs,
):
    variants = {}
    for variant_name, disabled_passes in variant_defs.items():
        try:
            variant_bin, variant_compile, variant_report = build_pcc(
                src_path,
                wrapped_code,
                opt_level,
                tmpdir,
                variant_label=f"{variant_name}-off",
                use_passes=True,
                disabled_passes=disabled_passes,
            )
            variant_exec = run_binary(variant_bin, runs)
        except Exception as exc:
            variants[variant_name] = {"ok": False, "error": str(exc)}
            continue

        variant_match = (
            variant_exec["returncode"] == clang_exec["returncode"]
            and variant_exec["stdout"] == clang_exec["stdout"]
        )
        variants[variant_name] = {
            "ok": True,
            "compile_time_s": variant_compile,
            "outputs_match": variant_match,
            "pass_report": variant_report,
            **variant_exec,
        }
    return variants


def benchmark_one(name, code, opt_levels, runs, pass_groups, pass_defs):
    # Wrap once: all callees (clang, pcc-allpass, pcc-nopass, pcc-group-off)
    # see the same source with `__pcc_bench_main` + checksum-printing main.
    wrapped_code = _wrap_with_checksum(code)
    result = {"name": name, "levels": {}}
    with tempfile.TemporaryDirectory(prefix="pcc_bench_") as tmpdir:
        # write_source re-wraps idempotently via the regex; since
        # wrapped_code already has `__pcc_bench_main` and no `int main(`
        # subst target, write_source's wrapper becomes a no-op. Use
        # wrapped_code directly to keep tree-of-truth singular.
        src_path = Path(tmpdir) / f"{slugify(name)}.c"
        src_path.write_text(wrapped_code)
        for opt_level in opt_levels:
            level = {"ok": False}
            try:
                clang_bin, clang_compile = build_clang(src_path, opt_level, tmpdir)
                clang_exec = run_binary(clang_bin, runs)

                pcc_bin, pcc_compile, pass_report = build_pcc(
                    src_path,
                    wrapped_code,
                    opt_level,
                    tmpdir,
                    variant_label="allpass",
                    use_passes=True,
                )
                pcc_exec = run_binary(pcc_bin, runs)

                nopass_bin, nopass_compile, _ = build_pcc(
                    src_path,
                    wrapped_code,
                    opt_level,
                    tmpdir,
                    variant_label="nopass",
                    use_passes=False,
                )
                nopass_exec = run_binary(nopass_bin, runs)
            except Exception as exc:
                level["error"] = str(exc)
                result["levels"][opt_level] = level
                continue

            outputs_match = (
                pcc_exec["returncode"] == clang_exec["returncode"]
                and pcc_exec["stdout"] == clang_exec["stdout"]
            )
            nopass_match = (
                nopass_exec["returncode"] == clang_exec["returncode"]
                and nopass_exec["stdout"] == clang_exec["stdout"]
            )
            group_off = _run_variant_matrix(
                pass_groups,
                src_path,
                wrapped_code,
                opt_level,
                tmpdir,
                clang_exec,
                runs,
            )
            pass_off = _run_variant_matrix(
                pass_defs,
                src_path,
                wrapped_code,
                opt_level,
                tmpdir,
                clang_exec,
                runs,
            )
            level.update(
                {
                    "ok": True,
                    "clang": {"compile_time_s": clang_compile, **clang_exec},
                    "pcc_allpass": {
                        "compile_time_s": pcc_compile,
                        "pass_report": pass_report,
                        **pcc_exec,
                    },
                    "pcc_nopass": {
                        "compile_time_s": nopass_compile,
                        **nopass_exec,
                    },
                    "group_off": group_off,
                    "pass_off": pass_off,
                    "outputs_match": outputs_match,
                    "nopass_match": nopass_match,
                }
            )
            result["levels"][opt_level] = level
    return result


def matching_exec_rows(results, opt_level, min_clean_s):
    rows = []
    for result in results:
        level = result["levels"].get(opt_level, {})
        if not level.get("ok"):
            continue
        if not level.get("outputs_match") or not level.get("nopass_match"):
            continue
        clang_exec = level["clang"]["exec_time_s"]
        pcc_exec = level["pcc_allpass"]["exec_time_s"]
        nopass_exec = level["pcc_nopass"]["exec_time_s"]
        is_clean = min(clang_exec, pcc_exec, nopass_exec) >= min_clean_s
        rows.append((result, level, is_clean))
    return rows


def _opt_level_label(opt_level: int) -> str:
    """Render the effective opt-level label for reports.

    `opt_level=0` in pcc runs LLVM's O1 pipeline as a floor (see
    `pcc/frontends/c/passes/base.py:187`). Calling that column "O0" overstates
    how bare the backend is. Use "O0+O1floor" so readers know the
    backend ran SROA/mem2reg/InstCombine/inlining.
    """
    if opt_level == 0:
        return "O0+O1floor"
    return f"O{opt_level}"


def print_exec_table(results, opt_level, min_clean_s):
    rows = matching_exec_rows(results, opt_level, min_clean_s)
    clean_rows = [(result, level) for result, level, is_clean in rows if is_clean]
    label = _opt_level_label(opt_level)
    print("=" * 120)
    print(
        f"EXEC-ONLY {label} comparison "
        f"(clean geomean threshold {min_clean_s * 1e3:.1f}ms, median-of-runs timing)"
    )
    print("=" * 120)
    print(
        f"{'Benchmark':<24} {'clang':>10} {'pcc all':>10} {'pcc/clang':>10} "
        f"{'pcc no':>10} {'all/no':>10} {'passes':>8} {'clean':>6}"
    )
    clean_ratios = []
    clean_pass_ratios = []
    counts = {"faster": 0, "tied": 0, "slower": 0}
    pass_counts = {"faster": 0, "tied": 0, "slower": 0}
    for result, level, is_clean in rows:
        clang_exec = level["clang"]["exec_time_s"]
        pcc_exec = level["pcc_allpass"]["exec_time_s"]
        nopass_exec = level["pcc_nopass"]["exec_time_s"]
        ratio = pcc_exec / clang_exec
        pass_ratio = pcc_exec / nopass_exec
        if is_clean:
            clean_ratios.append(ratio)
            clean_pass_ratios.append(pass_ratio)
            counts[classify_ratio(ratio)] += 1
            pass_counts[classify_ratio(pass_ratio)] += 1
        total_pass_ms = sum(
            metric["total_time_ms"]
            for metric in level["pcc_allpass"]["pass_report"].get("passes", {}).values()
        )
        print(
            f"{result['name']:<24} {fmt(clang_exec):>10} {fmt(pcc_exec):>10} "
            f"{ratio_str(pcc_exec, clang_exec):>10} {fmt(nopass_exec):>10} "
            f"{ratio_str(pcc_exec, nopass_exec):>10} {total_pass_ms:>7.2f}ms "
            f"{'yes' if is_clean else 'no':>6}"
        )
    print()
    print(
        "clang vs pcc all-pass (clean only):"
        f" geomean={ratio_str(geometric_mean(clean_ratios), 1.0)} "
        f" faster={counts['faster']} tied={counts['tied']} slower={counts['slower']}"
    )
    print(
        "pass effectiveness (all-pass vs no-pass, clean only):"
        f" geomean={ratio_str(geometric_mean(clean_pass_ratios), 1.0)} "
        f" faster={pass_counts['faster']} tied={pass_counts['tied']} slower={pass_counts['slower']}"
    )
    print(f"matched benchmarks: {len(rows)}/{len(results)}")
    print(f"clean benchmarks: {len(clean_rows)}/{len(results)}")
    issues = []
    for result in results:
        level = result["levels"].get(opt_level, {})
        if not level.get("ok"):
            issues.append((result["name"], f"error: {level.get('error', 'unknown error')}"))
            continue
        if not level.get("outputs_match"):
            issues.append((result["name"], "pcc all-pass output mismatch"))
        elif not level.get("nopass_match"):
            issues.append((result["name"], "pcc no-pass output mismatch"))
    if issues:
        print("excluded from matched rows:")
        for name, reason in issues:
            print(f"  {name:<24} {reason}")
    print()


def print_compile_summary(results, opt_level):
    clang_ratios = []
    nopass_ratios = []
    for result in results:
        level = result["levels"].get(opt_level, {})
        if not level.get("ok"):
            continue
        clang_compile = level["clang"]["compile_time_s"]
        pcc_compile = level["pcc_allpass"]["compile_time_s"]
        nopass_compile = level["pcc_nopass"]["compile_time_s"]
        clang_ratios.append(pcc_compile / clang_compile)
        nopass_ratios.append(pcc_compile / nopass_compile)
    print(
        f"Compile {_opt_level_label(opt_level)}: pcc/clang geomean={ratio_str(geometric_mean(clang_ratios), 1.0)} "
        f"all-pass/no-pass geomean={ratio_str(geometric_mean(nopass_ratios), 1.0)}"
    )


def print_cross_opt_pass_summary(results, allpass_opt_level, nopass_opt_level, min_clean_s):
    compile_ratios = []
    exec_ratios = []
    total_ratios = []
    counts = {"faster": 0, "tied": 0, "slower": 0}
    matched_rows = 0
    clean_rows = 0

    for result in results:
        allpass_level = result["levels"].get(allpass_opt_level, {})
        nopass_level = result["levels"].get(nopass_opt_level, {})
        if not allpass_level.get("ok") or not nopass_level.get("ok"):
            continue
        if not allpass_level.get("outputs_match") or not nopass_level.get("nopass_match"):
            continue
        matched_rows += 1

        all_compile = allpass_level["pcc_allpass"]["compile_time_s"]
        all_exec = allpass_level["pcc_allpass"]["exec_time_s"]
        nopass_compile = nopass_level["pcc_nopass"]["compile_time_s"]
        nopass_exec = nopass_level["pcc_nopass"]["exec_time_s"]
        if min(all_exec, nopass_exec) < min_clean_s:
            continue
        clean_rows += 1

        compile_ratios.append(all_compile / nopass_compile)
        exec_ratio = all_exec / nopass_exec
        exec_ratios.append(exec_ratio)
        total_ratios.append(
            (all_compile + all_exec) / (nopass_compile + nopass_exec)
        )
        counts[classify_ratio(exec_ratio)] += 1

    print(
        f"Cross-opt summary: O{allpass_opt_level} all-pass vs O{nopass_opt_level} no-pass "
        f"(clean threshold {min_clean_s * 1e3:.1f}ms)"
    )
    print(
        f"  compile geomean={ratio_str(geometric_mean(compile_ratios), 1.0)} "
        f"exec geomean={ratio_str(geometric_mean(exec_ratios), 1.0)} "
        f"total geomean={ratio_str(geometric_mean(total_ratios), 1.0)} "
        f"exec faster={counts['faster']} tied={counts['tied']} slower={counts['slower']} "
        f"matched={matched_rows}/{len(results)} clean={clean_rows}/{len(results)}"
    )
    print()


def print_top_passes(results, opt_level, topn):
    totals = {}
    for result in results:
        level = result["levels"].get(opt_level, {})
        if not level.get("ok"):
            continue
        for name, metric in level["pcc_allpass"]["pass_report"].get("passes", {}).items():
            totals[name] = totals.get(name, 0.0) + metric["total_time_ms"]
    top = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:topn]
    print(f"Top pass time totals for {_opt_level_label(opt_level)}:")
    for name, total_ms in top:
        print(f"  {name:<24} {total_ms:>9.2f}ms")
    print()


def print_pass_group_matrix(results, opt_level, min_clean_s, pass_groups):
    if not pass_groups:
        return

    print("=" * 120)
    print(
        f"PASS-GROUP ABLATION {_opt_level_label(opt_level)} "
        "(all-pass/group-off; lower is better for keeping the group)"
    )
    print("=" * 120)
    print(
        f"{'Group':<18} {'all/off exec':>12} {'all/off cmp':>12} "
        f"{'pass_ms':>10} {'helps':>7} {'tied':>6} {'hurts':>7} "
        f"{'matched':>8} {'clean':>6}"
    )
    for group_name, pass_names in pass_groups.items():
        exec_ratios = []
        compile_ratios = []
        pass_ms_total = 0.0
        helps = tied = hurts = matched = clean = 0
        for result in results:
            level = result["levels"].get(opt_level, {})
            if not level.get("ok"):
                continue
            if not level.get("outputs_match") or not level.get("nopass_match"):
                continue
            variant = level.get("group_off", {}).get(group_name, {})
            if not variant.get("ok") or not variant.get("outputs_match"):
                continue

            matched += 1
            all_exec = level["pcc_allpass"]["exec_time_s"]
            off_exec = variant["exec_time_s"]
            all_compile = level["pcc_allpass"]["compile_time_s"]
            off_compile = variant["compile_time_s"]
            is_clean = min(
                level["clang"]["exec_time_s"],
                all_exec,
                off_exec,
            ) >= min_clean_s
            if not is_clean:
                continue

            clean += 1
            exec_ratio = all_exec / off_exec
            exec_ratios.append(exec_ratio)
            compile_ratios.append(all_compile / off_compile)
            pass_ms_total += sum(
                metric["total_time_ms"]
                for name, metric in level["pcc_allpass"]["pass_report"].get("passes", {}).items()
                if name in pass_names
            )
            bucket = classify_ratio(exec_ratio)
            if bucket == "faster":
                helps += 1
            elif bucket == "slower":
                hurts += 1
            else:
                tied += 1

        print(
            f"{group_name:<18} "
            f"{ratio_str(geometric_mean(exec_ratios), 1.0):>12} "
            f"{ratio_str(geometric_mean(compile_ratios), 1.0):>12} "
            f"{pass_ms_total:>9.2f} "
            f"{helps:>7} {tied:>6} {hurts:>7} {matched:>8} {clean:>6}"
        )
    print()


def print_pass_matrix(results, opt_level, min_clean_s, pass_defs):
    if not pass_defs:
        return

    print("=" * 120)
    print(
        f"PASS ABLATION {_opt_level_label(opt_level)} "
        "(all-pass/pass-off; lower is better for removing the pass)"
    )
    print("=" * 120)
    print(
        f"{'Pass':<28} {'all/off exec':>12} {'all/off cmp':>12} "
        f"{'pass_ms':>10} {'helps':>7} {'tied':>6} {'hurts':>7} "
        f"{'matched':>8} {'clean':>6}"
    )
    for pass_name in pass_defs:
        exec_ratios = []
        compile_ratios = []
        pass_ms_total = 0.0
        helps = tied = hurts = matched = clean = 0
        for result in results:
            level = result["levels"].get(opt_level, {})
            if not level.get("ok"):
                continue
            if not level.get("outputs_match") or not level.get("nopass_match"):
                continue
            variant = level.get("pass_off", {}).get(pass_name, {})
            if not variant.get("ok") or not variant.get("outputs_match"):
                continue

            matched += 1
            all_exec = level["pcc_allpass"]["exec_time_s"]
            off_exec = variant["exec_time_s"]
            all_compile = level["pcc_allpass"]["compile_time_s"]
            off_compile = variant["compile_time_s"]
            is_clean = min(
                level["clang"]["exec_time_s"],
                all_exec,
                off_exec,
            ) >= min_clean_s
            if not is_clean:
                continue

            clean += 1
            exec_ratio = all_exec / off_exec
            exec_ratios.append(exec_ratio)
            compile_ratios.append(all_compile / off_compile)
            pass_ms_total += level["pcc_allpass"]["pass_report"].get("passes", {}).get(
                pass_name,
                {},
            ).get("total_time_ms", 0.0)
            bucket = classify_ratio(exec_ratio)
            if bucket == "faster":
                helps += 1
            elif bucket == "slower":
                hurts += 1
            else:
                tied += 1

        print(
            f"{pass_name:<28} "
            f"{ratio_str(geometric_mean(exec_ratios), 1.0):>12} "
            f"{ratio_str(geometric_mean(compile_ratios), 1.0):>12} "
            f"{pass_ms_total:>9.2f} "
            f"{helps:>7} {tied:>6} {hurts:>7} {matched:>8} {clean:>6}"
        )
    print()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", action="append", dest="benches")
    parser.add_argument(
        "--opt-level",
        action="append",
        dest="opt_levels",
        type=int,
        choices=(0, 1, 2, 3),
    )
    parser.add_argument(
        "--group-matrix",
        action="store_true",
        help="Run pass-group ablations and print all-pass/group-off summaries.",
    )
    parser.add_argument(
        "--pass-group",
        action="append",
        dest="pass_groups",
        help="Pass group to include in ablation output. Repeatable. Defaults to all groups.",
    )
    parser.add_argument(
        "--pass-matrix",
        action="store_true",
        help="Run per-pass ablations and print all-pass/pass-off summaries.",
    )
    parser.add_argument(
        "--pass",
        action="append",
        dest="passes",
        help="Pass to include in per-pass ablation output. Repeatable. Defaults to selected pass-group members or all default passes.",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--min-clean-ms",
        type=float,
        default=1.0,
        help="Only include benchmarks at or above this execution time in clean exec summaries.",
    )
    parser.add_argument("--top-passes", type=int, default=8)
    return parser.parse_args()


def main():
    from pcc.frontends.c.passes import default_pass_groups, unique_default_pass_names

    args = parse_args()

    names = benchmark_names(args.benches)
    opt_levels = args.opt_levels or [1, 2, 3]
    min_clean_s = args.min_clean_ms / 1000.0
    group_defs = {}
    if args.group_matrix:
        available_groups = default_pass_groups()
        selected_groups = args.pass_groups or list(available_groups)
        unknown_groups = sorted(set(selected_groups) - set(available_groups))
        if unknown_groups:
            raise SystemExit(f"Unknown pass groups: {', '.join(unknown_groups)}")
        group_defs = {
            group_name: available_groups[group_name]
            for group_name in selected_groups
        }
    pass_defs = {}
    if args.pass_matrix:
        available_groups = default_pass_groups()
        available_passes = unique_default_pass_names()
        selected_passes = list(args.passes or ())
        if not selected_passes:
            if args.pass_groups:
                unknown_groups = sorted(set(args.pass_groups) - set(available_groups))
                if unknown_groups:
                    raise SystemExit(
                        f"Unknown pass groups: {', '.join(unknown_groups)}"
                    )
                for group_name in args.pass_groups:
                    selected_passes.extend(available_groups[group_name])
            else:
                selected_passes.extend(available_passes)
        selected_passes = list(unique_ordered(selected_passes))
        unknown_passes = sorted(set(selected_passes) - set(available_passes))
        if unknown_passes:
            raise SystemExit(f"Unknown passes: {', '.join(unknown_passes)}")
        pass_defs = {
            pass_name: (pass_name,)
            for pass_name in selected_passes
        }

    print("=" * 120)
    print("PCC benchmark suite")
    print("=" * 120)
    print(f"benchmarks: {len(names)}")
    print(f"opt levels: {', '.join(f'O{level}' for level in opt_levels)}")
    print(f"timed runs: {args.runs}")
    print(f"clean exec threshold: {args.min_clean_ms:.1f}ms")
    if group_defs:
        print(f"pass groups: {', '.join(group_defs)}")
    if pass_defs:
        print(f"pass ablations: {', '.join(pass_defs)}")
    print()

    results = []
    for name in names:
        print(f"Running {name} ...")
        results.append(
            benchmark_one(
                name,
                BENCHMARKS[name],
                opt_levels,
                args.runs,
                group_defs,
                pass_defs,
            )
        )
    print()

    for opt_level in opt_levels:
        print_compile_summary(results, opt_level)
        print_exec_table(results, opt_level, min_clean_s)
        print_pass_group_matrix(results, opt_level, min_clean_s, group_defs)
        print_pass_matrix(results, opt_level, min_clean_s, pass_defs)
        print_top_passes(results, opt_level, args.top_passes)

    if 0 in opt_levels and 2 in opt_levels:
        print_cross_opt_pass_summary(results, 0, 2, min_clean_s)


if __name__ == "__main__":
    main()
