"""Native Mach-O linker entry over pcc's existing parser/assembler/linker.

Accepts --out PATH and repeated --native-object, --object, --asm or --archive
inputs. Unsupported CLI surfaces fail explicitly. This entry is qualified
separately before the compiler may select it instead of its host link helper.
"""

import os
import sys

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_exec import link_executable
from pcc.backend.native_object import NativeObject, decode_native_object


def main() -> None:
    output = ""
    entry = "_main"
    objects = []
    archives = []
    index = 1
    while index < len(sys.argv):
        option = sys.argv[index]
        if index + 1 >= len(sys.argv):
            raise ValueError("missing value for " + option)
        value = sys.argv[index + 1]
        index += 2
        if option == "--out":
            output = value
        elif option == "--entry":
            entry = value
        elif option == "--asm":
            with open(value, "r") as stream:
                assembly = stream.read()
            sections, undefined = assemble_file(assembly)
            objects.append(NativeObject.from_sections(sections, undefined=undefined))
        elif option in ("--native-object", "--object", "--archive"):
            with open(value, "rb") as stream:
                data = stream.read()
            if option == "--native-object":
                objects.append(decode_native_object(data))
            elif option == "--archive":
                archives.append(data)
            else:
                objects.append(data)
        else:
            raise ValueError("unsupported native linker option: " + option)
    if not output or not objects:
        raise ValueError("native linker requires --out and object/assembly input")
    image = link_executable(objects, archives=archives, entry=entry)
    temporary = output + ".tmp"
    with open(temporary, "wb") as stream:
        stream.write(image)
    os.chmod(temporary, 0o755)
    os.replace(temporary, output)


if __name__ == "__main__":
    main()
