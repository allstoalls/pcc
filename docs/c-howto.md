# Compiling C with pcc

pcc compiles C with its own preprocessor, code generator and linker (the
default `self` backend). The commands below run from the source checkout; see
the [README](../README.md#install) for setup.

## 1. One file

```bash
uv run pcc hello.c -o hello_c
./hello_c
```

## 2. A directory of C files

For an ordinary multi-file program, compile each file as its own translation
unit, the way `cc` and `make` do:

```bash
uv run pcc --separate-tus src/ -o app
```

Exactly one file must define `main()`. Add `--jobs N` to compile files in
parallel.

Without `--separate-tus`, pcc concatenates every `.c` file in the directory
into one translation unit, with the `main()` file last. That is quick for small
programs, but `static` functions, `static` variables and macros from different
files then share one scope and can collide.

## 3. Preprocessor flags and prebuilt objects

```bash
uv run pcc --cpp-arg=-DUSE_FOO=1 --cpp-arg=-Iinclude \
  --link-arg=libfoo.a --separate-tus src/ -o app
```

Repeat `--cpp-arg` for each preprocessor flag. With `-o`, `--link-arg` accepts
object files and archives (`.o`, `.a`, `.lib`); the owned linker rejects
linker flags such as `-lm`.

## 4. Projects with a Makefile

pcc can follow an existing build instead of compiling every file it finds.
The project must already be configured (for example, `./configure` has run)
so that `make` can resolve the goal.

- `--sources-from-make GOAL` runs `make -nB GOAL` in the input directory and
  compiles the `.c` files that goal uses, with its `-D`, `-U`, `-I` and
  `-include` flags. Combine it with `--separate-tus`.
- `--depends-on PATH[=GOAL]` compiles your entry file together with a library
  file or directory. `=GOAL` selects the library's sources through
  `make -nB GOAL`. Only the entry file may define `main()`; repeat the option
  for more libraries.
- `--prepare-cmd CMD` runs a shell command before compiling, and
  `--ensure-make-goal PATH=GOAL` runs `make -C PATH GOAL`, for example to
  generate headers. Both run the project's own host tools.

## 5. Real projects: SQLite, zlib, PostgreSQL libpq

The tests build vendored projects under `projects/` through two link routes.

**pcc's own linker.** zlib ([tests/c/test_zlib.py](../tests/c/test_zlib.py)),
the SQLite amalgamation
([tests/integration/test_sqlite.py](../tests/integration/test_sqlite.py)) and
PCRE ([tests/integration/test_pcre.py](../tests/integration/test_pcre.py)) run
through the same emitter and linker as `-o`. Their test names still say
`mcjit`; the code path is pcc's owned execution.

**The host C compiler.** PostgreSQL `libpq` and Lua are tested only with
`--system-link`: pcc compiles each translation unit to assembly, and the host
C compiler assembles and links it. This is a compatibility route, not an owned
pcc build. libpq also needs its own `configure` and `make` to generate the
configured headers and the support archives (`libpgport_shlib.a`,
`libpgcommon_shlib.a` and zlib's `libz.a`). After that preparation, the libpq
integration test runs:

```bash
uv run pcc --system-link --jobs 2 \
  --depends-on projects/postgresql-17.4/src/interfaces/libpq=libpq.a \
  --link-arg=projects/zlib-1.3.1/libz.a \
  --link-arg=projects/postgresql-17.4/src/common/libpgcommon_shlib.a \
  --link-arg=projects/postgresql-17.4/src/port/libpgport_shlib.a \
  --link-arg=-lm \
  projects/test_postgres_main.c
```

Its output includes `libpq version 170004` and `OK`. The preparation steps and
configure options are in
[tests/integration/test_postgres.py](../tests/integration/test_postgres.py).
No test builds libpq with pcc's own linker yet.

## 6. Inspect or cross-compile

```bash
uv run pcc hello.c --emit-llvm=hello_c.ll
uv run pcc hello.c --emit-asm hello_c.s
uv run pcc hello.c --emit-obj hello_c.o --target x86_64-unknown-linux-gnu
```

The IR uses LLVM syntax; no external LLVM is involved. `--target` requires an
emit option or `--system-link`.

For ownership boundaries and what each route proves, see
[C compilation and ownership](developer-reference.md#c-compilation-and-ownership-boundaries).
