/* Host-C differential oracle for py/py_hash_runtime.py.
 * Excluded from the production pcc-Python runtime archive. */
#include "py_internal.h"
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint32_t state[8];
    uint64_t bit_count;
    unsigned char block[64];
    size_t block_len;
} PccSha256;

static uint32_t sha_rotr(uint32_t value, unsigned shift) {
    return (value >> shift) | (value << (32U - shift));
}

static void sha256_transform(PccSha256 *ctx, const unsigned char block[64]) {
    static const uint32_t k[64] = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
        0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
        0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
        0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
        0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
        0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
        0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
        0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
        0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
        0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
        0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
        0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
        0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
        0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
        0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U
    };
    uint32_t w[64];
    for (size_t i = 0; i < 16; i++) {
        size_t j = i * 4;
        w[i] = ((uint32_t)block[j] << 24) | ((uint32_t)block[j + 1] << 16)
            | ((uint32_t)block[j + 2] << 8) | (uint32_t)block[j + 3];
    }
    for (size_t i = 16; i < 64; i++) {
        uint32_t s0 = sha_rotr(w[i - 15], 7) ^ sha_rotr(w[i - 15], 18)
            ^ (w[i - 15] >> 3);
        uint32_t s1 = sha_rotr(w[i - 2], 17) ^ sha_rotr(w[i - 2], 19)
            ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2];
    uint32_t d = ctx->state[3], e = ctx->state[4], f = ctx->state[5];
    uint32_t g = ctx->state[6], h = ctx->state[7];
    for (size_t i = 0; i < 64; i++) {
        uint32_t s1 = sha_rotr(e, 6) ^ sha_rotr(e, 11) ^ sha_rotr(e, 25);
        uint32_t choice = (e & f) ^ ((~e) & g);
        uint32_t temp1 = h + s1 + choice + k[i] + w[i];
        uint32_t s0 = sha_rotr(a, 2) ^ sha_rotr(a, 13) ^ sha_rotr(a, 22);
        uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = s0 + majority;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    ctx->state[0] += a; ctx->state[1] += b; ctx->state[2] += c;
    ctx->state[3] += d; ctx->state[4] += e; ctx->state[5] += f;
    ctx->state[6] += g; ctx->state[7] += h;
}

static void sha256_init(PccSha256 *ctx) {
    static const uint32_t initial[8] = {
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U
    };
    memcpy(ctx->state, initial, sizeof(initial));
    ctx->bit_count = 0;
    ctx->block_len = 0;
    memset(ctx->block, 0, sizeof(ctx->block));
}

static void sha256_update(PccSha256 *ctx, const unsigned char *data, size_t len) {
    ctx->bit_count += (uint64_t)len * 8U;
    while (len > 0) {
        size_t room = 64 - ctx->block_len;
        size_t take = len < room ? len : room;
        memcpy(ctx->block + ctx->block_len, data, take);
        ctx->block_len += take;
        data += take;
        len -= take;
        if (ctx->block_len == 64) {
            sha256_transform(ctx, ctx->block);
            ctx->block_len = 0;
        }
    }
}

static void sha256_final(PccSha256 *ctx, unsigned char out[32]) {
    ctx->block[ctx->block_len++] = 0x80;
    if (ctx->block_len > 56) {
        while (ctx->block_len < 64) ctx->block[ctx->block_len++] = 0;
        sha256_transform(ctx, ctx->block);
        ctx->block_len = 0;
    }
    while (ctx->block_len < 56) ctx->block[ctx->block_len++] = 0;
    for (size_t i = 0; i < 8; i++) {
        ctx->block[63 - i] = (unsigned char)(ctx->bit_count >> (i * 8));
    }
    sha256_transform(ctx, ctx->block);
    for (size_t i = 0; i < 8; i++) {
        out[i * 4] = (unsigned char)(ctx->state[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(ctx->state[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(ctx->state[i] >> 8);
        out[i * 4 + 3] = (unsigned char)ctx->state[i];
    }
}

/* Match the pcc-Python private context ABI without depending on C padding:
 * eight 64-bit words, bit count, block length, then the 64-byte partial block.
 * Every returned snapshot is immutable and owned by its caller. */
static PyObject *sha256_state_pack(const PccSha256 *ctx) {
    unsigned char state[144];
    for (size_t i = 0; i < 8; i++) {
        uint64_t word = ctx->state[i];
        memcpy(state + i * 8, &word, 8);
    }
    uint64_t block_len = ctx->block_len;
    memcpy(state + 64, &ctx->bit_count, 8);
    memcpy(state + 72, &block_len, 8);
    memcpy(state + 80, ctx->block, 64);
    return py_bytes_new((const char *)state, 144);
}

static void sha256_state_load(PccSha256 *ctx, PyObject *state_obj) {
    const char *state = py_bytes_data_ptr(state_obj);
    for (size_t i = 0; i < 8; i++) {
        uint64_t word;
        memcpy(&word, state + i * 8, 8);
        ctx->state[i] = (uint32_t)word;
    }
    uint64_t block_len;
    memcpy(&ctx->bit_count, state + 64, 8);
    memcpy(&block_len, state + 72, 8);
    ctx->block_len = (size_t)block_len;
    memcpy(ctx->block, state + 80, 64);
}

PyObject *py_sha256_state_new(void) {
    PccSha256 ctx;
    sha256_init(&ctx);
    return sha256_state_pack(&ctx);
}

PyObject *py_sha256_state_update(PyObject *state, PyObject *data) {
    PccSha256 ctx;
    sha256_state_load(&ctx, state);
    sha256_update(&ctx, (const unsigned char *)py_bytes_data_ptr(data),
                  (size_t)py_bytes_len(data));
    return sha256_state_pack(&ctx);
}

PyObject *py_sha256_state_digest(PyObject *state) {
    PccSha256 ctx;
    unsigned char digest[32];
    sha256_state_load(&ctx, state);
    sha256_final(&ctx, digest);
    return py_bytes_new((const char *)digest, 32);
}

PyObject *py_sha256_bytes_digest(PyObject *data) {
    PccSha256 ctx;
    unsigned char digest[32];
    sha256_init(&ctx);
    sha256_update(&ctx, (const unsigned char *)py_bytes_data_ptr(data),
                  (size_t)py_bytes_len(data));
    sha256_final(&ctx, digest);
    return py_bytes_new((const char *)digest, 32);
}

static PyObject *sha256_file_hex_bounded(PyObject *path_obj, int64_t max_bytes) {
    if (max_bytes <= 0) return py_str_new("", 0);
    const char *path = py_str_utf8(path_obj);
    FILE *fh = fopen(path, "rb");
    if (fh == NULL) return py_str_new("", 0);
    PccSha256 ctx;
    sha256_init(&ctx);
    unsigned char buffer[32768];
    int64_t total = 0;
    for (;;) {
        int64_t remaining = max_bytes - total;
        size_t read_cap = sizeof(buffer);
        if (remaining < (int64_t)sizeof(buffer)) {
            /* Read one sentinel byte past the admitted payload so an exact
             * max-sized file is distinguishable from an oversized prefix. */
            read_cap = (size_t)remaining + 1U;
        }
        size_t count = fread(buffer, 1, read_cap, fh);
        if (count > (size_t)remaining) {
            fclose(fh);
            return py_str_new("", 0);
        }
        total += (int64_t)count;
        if (count > 0) sha256_update(&ctx, buffer, count);
        if (count < read_cap) {
            if (ferror(fh)) {
                fclose(fh);
                return py_str_new("", 0);
            }
            break;
        }
    }
    fclose(fh);
    unsigned char digest[32];
    char hex[65];
    static const char digits[] = "0123456789abcdef";
    sha256_final(&ctx, digest);
    for (size_t i = 0; i < 32; i++) {
        hex[i * 2] = digits[digest[i] >> 4];
        hex[i * 2 + 1] = digits[digest[i] & 15];
    }
    hex[64] = '\0';
    return py_str_new(hex, 64);
}

PyObject *py_sha256_file_hex(PyObject *path_obj) {
    return sha256_file_hex_bounded(path_obj, INT64_MAX);
}

PyObject *py_sha256_file_hex_bounded(PyObject *path_obj, int64_t max_bytes) {
    return sha256_file_hex_bounded(path_obj, max_bytes);
}

