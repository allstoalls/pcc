# Preface

## How to Read This Book: Three Layers

On a first pass, do not try to memorize every file name. Read the book in three layers: first, why pcc exists; second, how the C frontend, Python frontend, runtime, and backends form an execution chain; third, what evidence keeps each claim from overreaching.

- Chapters 1-4 establish the shared language: what "owning execution" means and how the C frontend lowers real C programs into LLVM IR.
- Chapters 5-15 cover the main system: the Python frontend, object model, exceptions, ownership, GC, backends, no-libpython, and the bootstrap fixed point.
- Chapters 16-18 turn to the long-term direction: the value model, package and C-API compatibility, and the testing discipline that keeps claims honest.

## What this book is

This is a design-and-implementation book about **a real, living system**. Its
lineage runs back to Lions' commentary on Sixth Edition UNIX and to McKusick
et al.'s *The Design and Implementation of the 4.4BSD Operating System*: rather
than teaching general principles through toy examples, it lays a complete
system open and answers, layer by layer, three questions — **why this design,
how it is implemented, and how it has actually broken in the real world.**

The system is pcc: a compiler toolchain authored in Python. One repository
holds two compilers and one runtime —

1. a mature **C frontend** that lowers C to LLVM IR and compiles real projects
   such as Lua, SQLite, PostgreSQL `libpq`, zlib, OpenSSL, and nginx;
2. an experimental **typed-Python frontend** that carries the self-hosting
   track;
3. a native runtime with **five pluggable GC backends**, plus a self backend
   that emits native code without LLVM.

pcc's thesis is not "make Python fast." It is to make Python execution
**ownable**: compiled, auditable, self-hostable, package-aware, and honest
about every fallback boundary. Performance is treated as a consequence of
proven semantics, never a license to weaken Python behavior. That stance runs
through this book exactly as it runs through every engineering rule in the
repository.

## What this book is not

This is neither a user manual for pcc nor a compilers textbook. The reader is
assumed to know what an AST, SSA, reference counting, and tricolor marking
are; the book does not redefine them. Instead it shows how those ideas press
against each other inside a system that must simultaneously satisfy CPython
semantics, no-libpython linking, five-way GC equality, and a byte-level
bootstrap fixed point.

The book also inherits pcc's **claim hygiene**: every capability claim is
mode-labeled. Host pcc is not pcc1; libpython mode is not no-libpython; the
LLVM backend is not the self backend; a green stage1 is not the
pcc1→pcc2→pcc3 fixed point. Whatever is experimental is called experimental,
and confirmed defects — such as the typed-int overflow problem in Chapter 16 —
are written up as open problems. A book that lies about its own system has no
standing to discuss design.

## Assumed background

- You read Python and C, and can follow short fragments of LLVM IR and
  AArch64 assembly.
- You know the standard phases of a compiler and the basic vocabulary of GC.
- Ideally you have the pcc repository at hand. Every reference in this book
  points to a real file and a real identifier; the book is written to be read
  against the source.

## How the book is written

Every chapter follows one structure: first **the problem and the design
space** (why the subsystem exists, what the alternatives were, why they were
rejected); then **the mechanism**, grounded in real source files and function
names; then **History and Lessons** — case studies drawn from the two hundred
odd investigation records under [docs/investigations/](../../docs/investigations): symptom, wrong
hypothesis, evidence chain, root cause, and the invariant left behind.
Exercises close each chapter, ranging from "verify this claim in the source"
to "argue a design trade-off."

The case studies are first-class content, not garnish. A system's real design
constraints often become visible only after they have been violated — after
they turned the bootstrap red or crashed a collector. [docs/investigations/](../../docs/investigations)
is this system's fossil record; the book cites those files by name and date so
that every statement can be checked against the original evidence.

## How to read it

Nine parts, eighteen chapters. Three paths:

- **The compiler path**: Chapters 1–6 (overview, C frontend, Python frontend)
  → Chapters 12–13 (LLVM and self backends) → Chapter 15 (the bootstrap fixed
  point).
- **The runtime and GC path**: Chapters 1–2 → Chapters 7–9 (object model,
  exceptions, ownership) → Chapters 10–11 (the five GCs) → Chapter 14
  (no-libpython) → Chapter 16 (the value model).
- **The methodology path**: Chapter 1 → Chapter 15 → Chapter 18. If all you
  want to know is how a repository worked on by many agents keeps from lying
  to itself, read these three.

## Version and conventions

The book corresponds to the pcc repository as of June 2026 (after 0.1.5, with
all five GC bootstrap gates green on the mainline). The code will keep
evolving, so source references always use **file path plus identifier name**
(e.g. `pcc_gc_store_ptr()` in [pcc/runtime/src/py_gc_backend.c](../../pcc/runtime/src/py_gc_backend.c)), never
line numbers. Where the book and the code disagree, the code wins — which is
precisely the reading habit this book hopes to teach.

Code identifiers, CLI flags, and environment variables (`PCC_GC_BACKEND`,
`--python-libpython=off`) appear verbatim. The Chinese edition's fixed
terminology table is Appendix B.
