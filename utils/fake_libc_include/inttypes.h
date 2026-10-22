#include "_fake_defines.h"
#include "_fake_typedefs.h"

/* C99 7.8.1 fprintf/fscanf macros. int64_t, intmax_t and intptr_t follow
 * the owned target predefines: long on LP64 targets, long long on Win64. */
#ifdef _WIN64
#define __PCC_PRI64 "ll"
#else
#define __PCC_PRI64 "l"
#endif
#ifndef PRId8
#define PRId8 "" "d"
#endif
#ifndef PRIdLEAST8
#define PRIdLEAST8 "" "d"
#endif
#ifndef PRIdFAST8
#define PRIdFAST8 "" "d"
#endif
#ifndef SCNd8
#define SCNd8 "hh" "d"
#endif
#ifndef SCNdLEAST8
#define SCNdLEAST8 "hh" "d"
#endif
#ifndef SCNdFAST8
#define SCNdFAST8 "hh" "d"
#endif
#ifndef PRId16
#define PRId16 "" "d"
#endif
#ifndef PRIdLEAST16
#define PRIdLEAST16 "" "d"
#endif
#ifndef PRIdFAST16
#define PRIdFAST16 "" "d"
#endif
#ifndef SCNd16
#define SCNd16 "h" "d"
#endif
#ifndef SCNdLEAST16
#define SCNdLEAST16 "h" "d"
#endif
#ifndef SCNdFAST16
#define SCNdFAST16 "h" "d"
#endif
#ifndef PRId32
#define PRId32 "" "d"
#endif
#ifndef PRIdLEAST32
#define PRIdLEAST32 "" "d"
#endif
#ifndef PRIdFAST32
#define PRIdFAST32 "" "d"
#endif
#ifndef SCNd32
#define SCNd32 "" "d"
#endif
#ifndef SCNdLEAST32
#define SCNdLEAST32 "" "d"
#endif
#ifndef SCNdFAST32
#define SCNdFAST32 "" "d"
#endif
#ifndef PRId64
#define PRId64 __PCC_PRI64 "d"
#endif
#ifndef PRIdLEAST64
#define PRIdLEAST64 __PCC_PRI64 "d"
#endif
#ifndef PRIdFAST64
#define PRIdFAST64 __PCC_PRI64 "d"
#endif
#ifndef SCNd64
#define SCNd64 __PCC_PRI64 "d"
#endif
#ifndef SCNdLEAST64
#define SCNdLEAST64 __PCC_PRI64 "d"
#endif
#ifndef SCNdFAST64
#define SCNdFAST64 __PCC_PRI64 "d"
#endif
#ifndef PRIdMAX
#define PRIdMAX __PCC_PRI64 "d"
#endif
#ifndef PRIdPTR
#define PRIdPTR __PCC_PRI64 "d"
#endif
#ifndef SCNdMAX
#define SCNdMAX __PCC_PRI64 "d"
#endif
#ifndef SCNdPTR
#define SCNdPTR __PCC_PRI64 "d"
#endif
#ifndef PRIi8
#define PRIi8 "" "i"
#endif
#ifndef PRIiLEAST8
#define PRIiLEAST8 "" "i"
#endif
#ifndef PRIiFAST8
#define PRIiFAST8 "" "i"
#endif
#ifndef SCNi8
#define SCNi8 "hh" "i"
#endif
#ifndef SCNiLEAST8
#define SCNiLEAST8 "hh" "i"
#endif
#ifndef SCNiFAST8
#define SCNiFAST8 "hh" "i"
#endif
#ifndef PRIi16
#define PRIi16 "" "i"
#endif
#ifndef PRIiLEAST16
#define PRIiLEAST16 "" "i"
#endif
#ifndef PRIiFAST16
#define PRIiFAST16 "" "i"
#endif
#ifndef SCNi16
#define SCNi16 "h" "i"
#endif
#ifndef SCNiLEAST16
#define SCNiLEAST16 "h" "i"
#endif
#ifndef SCNiFAST16
#define SCNiFAST16 "h" "i"
#endif
#ifndef PRIi32
#define PRIi32 "" "i"
#endif
#ifndef PRIiLEAST32
#define PRIiLEAST32 "" "i"
#endif
#ifndef PRIiFAST32
#define PRIiFAST32 "" "i"
#endif
#ifndef SCNi32
#define SCNi32 "" "i"
#endif
#ifndef SCNiLEAST32
#define SCNiLEAST32 "" "i"
#endif
#ifndef SCNiFAST32
#define SCNiFAST32 "" "i"
#endif
#ifndef PRIi64
#define PRIi64 __PCC_PRI64 "i"
#endif
#ifndef PRIiLEAST64
#define PRIiLEAST64 __PCC_PRI64 "i"
#endif
#ifndef PRIiFAST64
#define PRIiFAST64 __PCC_PRI64 "i"
#endif
#ifndef SCNi64
#define SCNi64 __PCC_PRI64 "i"
#endif
#ifndef SCNiLEAST64
#define SCNiLEAST64 __PCC_PRI64 "i"
#endif
#ifndef SCNiFAST64
#define SCNiFAST64 __PCC_PRI64 "i"
#endif
#ifndef PRIiMAX
#define PRIiMAX __PCC_PRI64 "i"
#endif
#ifndef PRIiPTR
#define PRIiPTR __PCC_PRI64 "i"
#endif
#ifndef SCNiMAX
#define SCNiMAX __PCC_PRI64 "i"
#endif
#ifndef SCNiPTR
#define SCNiPTR __PCC_PRI64 "i"
#endif
#ifndef PRIo8
#define PRIo8 "" "o"
#endif
#ifndef PRIoLEAST8
#define PRIoLEAST8 "" "o"
#endif
#ifndef PRIoFAST8
#define PRIoFAST8 "" "o"
#endif
#ifndef SCNo8
#define SCNo8 "hh" "o"
#endif
#ifndef SCNoLEAST8
#define SCNoLEAST8 "hh" "o"
#endif
#ifndef SCNoFAST8
#define SCNoFAST8 "hh" "o"
#endif
#ifndef PRIo16
#define PRIo16 "" "o"
#endif
#ifndef PRIoLEAST16
#define PRIoLEAST16 "" "o"
#endif
#ifndef PRIoFAST16
#define PRIoFAST16 "" "o"
#endif
#ifndef SCNo16
#define SCNo16 "h" "o"
#endif
#ifndef SCNoLEAST16
#define SCNoLEAST16 "h" "o"
#endif
#ifndef SCNoFAST16
#define SCNoFAST16 "h" "o"
#endif
#ifndef PRIo32
#define PRIo32 "" "o"
#endif
#ifndef PRIoLEAST32
#define PRIoLEAST32 "" "o"
#endif
#ifndef PRIoFAST32
#define PRIoFAST32 "" "o"
#endif
#ifndef SCNo32
#define SCNo32 "" "o"
#endif
#ifndef SCNoLEAST32
#define SCNoLEAST32 "" "o"
#endif
#ifndef SCNoFAST32
#define SCNoFAST32 "" "o"
#endif
#ifndef PRIo64
#define PRIo64 __PCC_PRI64 "o"
#endif
#ifndef PRIoLEAST64
#define PRIoLEAST64 __PCC_PRI64 "o"
#endif
#ifndef PRIoFAST64
#define PRIoFAST64 __PCC_PRI64 "o"
#endif
#ifndef SCNo64
#define SCNo64 __PCC_PRI64 "o"
#endif
#ifndef SCNoLEAST64
#define SCNoLEAST64 __PCC_PRI64 "o"
#endif
#ifndef SCNoFAST64
#define SCNoFAST64 __PCC_PRI64 "o"
#endif
#ifndef PRIoMAX
#define PRIoMAX __PCC_PRI64 "o"
#endif
#ifndef PRIoPTR
#define PRIoPTR __PCC_PRI64 "o"
#endif
#ifndef SCNoMAX
#define SCNoMAX __PCC_PRI64 "o"
#endif
#ifndef SCNoPTR
#define SCNoPTR __PCC_PRI64 "o"
#endif
#ifndef PRIu8
#define PRIu8 "" "u"
#endif
#ifndef PRIuLEAST8
#define PRIuLEAST8 "" "u"
#endif
#ifndef PRIuFAST8
#define PRIuFAST8 "" "u"
#endif
#ifndef SCNu8
#define SCNu8 "hh" "u"
#endif
#ifndef SCNuLEAST8
#define SCNuLEAST8 "hh" "u"
#endif
#ifndef SCNuFAST8
#define SCNuFAST8 "hh" "u"
#endif
#ifndef PRIu16
#define PRIu16 "" "u"
#endif
#ifndef PRIuLEAST16
#define PRIuLEAST16 "" "u"
#endif
#ifndef PRIuFAST16
#define PRIuFAST16 "" "u"
#endif
#ifndef SCNu16
#define SCNu16 "h" "u"
#endif
#ifndef SCNuLEAST16
#define SCNuLEAST16 "h" "u"
#endif
#ifndef SCNuFAST16
#define SCNuFAST16 "h" "u"
#endif
#ifndef PRIu32
#define PRIu32 "" "u"
#endif
#ifndef PRIuLEAST32
#define PRIuLEAST32 "" "u"
#endif
#ifndef PRIuFAST32
#define PRIuFAST32 "" "u"
#endif
#ifndef SCNu32
#define SCNu32 "" "u"
#endif
#ifndef SCNuLEAST32
#define SCNuLEAST32 "" "u"
#endif
#ifndef SCNuFAST32
#define SCNuFAST32 "" "u"
#endif
#ifndef PRIu64
#define PRIu64 __PCC_PRI64 "u"
#endif
#ifndef PRIuLEAST64
#define PRIuLEAST64 __PCC_PRI64 "u"
#endif
#ifndef PRIuFAST64
#define PRIuFAST64 __PCC_PRI64 "u"
#endif
#ifndef SCNu64
#define SCNu64 __PCC_PRI64 "u"
#endif
#ifndef SCNuLEAST64
#define SCNuLEAST64 __PCC_PRI64 "u"
#endif
#ifndef SCNuFAST64
#define SCNuFAST64 __PCC_PRI64 "u"
#endif
#ifndef PRIuMAX
#define PRIuMAX __PCC_PRI64 "u"
#endif
#ifndef PRIuPTR
#define PRIuPTR __PCC_PRI64 "u"
#endif
#ifndef SCNuMAX
#define SCNuMAX __PCC_PRI64 "u"
#endif
#ifndef SCNuPTR
#define SCNuPTR __PCC_PRI64 "u"
#endif
#ifndef PRIx8
#define PRIx8 "" "x"
#endif
#ifndef PRIxLEAST8
#define PRIxLEAST8 "" "x"
#endif
#ifndef PRIxFAST8
#define PRIxFAST8 "" "x"
#endif
#ifndef SCNx8
#define SCNx8 "hh" "x"
#endif
#ifndef SCNxLEAST8
#define SCNxLEAST8 "hh" "x"
#endif
#ifndef SCNxFAST8
#define SCNxFAST8 "hh" "x"
#endif
#ifndef PRIx16
#define PRIx16 "" "x"
#endif
#ifndef PRIxLEAST16
#define PRIxLEAST16 "" "x"
#endif
#ifndef PRIxFAST16
#define PRIxFAST16 "" "x"
#endif
#ifndef SCNx16
#define SCNx16 "h" "x"
#endif
#ifndef SCNxLEAST16
#define SCNxLEAST16 "h" "x"
#endif
#ifndef SCNxFAST16
#define SCNxFAST16 "h" "x"
#endif
#ifndef PRIx32
#define PRIx32 "" "x"
#endif
#ifndef PRIxLEAST32
#define PRIxLEAST32 "" "x"
#endif
#ifndef PRIxFAST32
#define PRIxFAST32 "" "x"
#endif
#ifndef SCNx32
#define SCNx32 "" "x"
#endif
#ifndef SCNxLEAST32
#define SCNxLEAST32 "" "x"
#endif
#ifndef SCNxFAST32
#define SCNxFAST32 "" "x"
#endif
#ifndef PRIx64
#define PRIx64 __PCC_PRI64 "x"
#endif
#ifndef PRIxLEAST64
#define PRIxLEAST64 __PCC_PRI64 "x"
#endif
#ifndef PRIxFAST64
#define PRIxFAST64 __PCC_PRI64 "x"
#endif
#ifndef SCNx64
#define SCNx64 __PCC_PRI64 "x"
#endif
#ifndef SCNxLEAST64
#define SCNxLEAST64 __PCC_PRI64 "x"
#endif
#ifndef SCNxFAST64
#define SCNxFAST64 __PCC_PRI64 "x"
#endif
#ifndef PRIxMAX
#define PRIxMAX __PCC_PRI64 "x"
#endif
#ifndef PRIxPTR
#define PRIxPTR __PCC_PRI64 "x"
#endif
#ifndef SCNxMAX
#define SCNxMAX __PCC_PRI64 "x"
#endif
#ifndef SCNxPTR
#define SCNxPTR __PCC_PRI64 "x"
#endif
#ifndef PRIX8
#define PRIX8 "" "X"
#endif
#ifndef PRIXLEAST8
#define PRIXLEAST8 "" "X"
#endif
#ifndef PRIXFAST8
#define PRIXFAST8 "" "X"
#endif
#ifndef PRIX16
#define PRIX16 "" "X"
#endif
#ifndef PRIXLEAST16
#define PRIXLEAST16 "" "X"
#endif
#ifndef PRIXFAST16
#define PRIXFAST16 "" "X"
#endif
#ifndef PRIX32
#define PRIX32 "" "X"
#endif
#ifndef PRIXLEAST32
#define PRIXLEAST32 "" "X"
#endif
#ifndef PRIXFAST32
#define PRIXFAST32 "" "X"
#endif
#ifndef PRIX64
#define PRIX64 __PCC_PRI64 "X"
#endif
#ifndef PRIXLEAST64
#define PRIXLEAST64 __PCC_PRI64 "X"
#endif
#ifndef PRIXFAST64
#define PRIXFAST64 __PCC_PRI64 "X"
#endif
#ifndef PRIXMAX
#define PRIXMAX __PCC_PRI64 "X"
#endif
#ifndef PRIXPTR
#define PRIXPTR __PCC_PRI64 "X"
#endif
