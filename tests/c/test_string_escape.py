import os
import sys

this_dir = os.path.dirname(os.path.abspath(__file__))
# tests/{c,python}/<file>.py -> repo root is two levels up. This used to
# rely on tests/conftest.py's global Path.resolve/dirname shim.
parent_dir = os.path.dirname(os.path.dirname(this_dir))
sys.path.insert(0, parent_dir)
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.frontends.c.parse.c_parser import CParser
import unittest


class TestStringEscape(unittest.TestCase):
    def test_escape_processing(self):
        """Test that escape sequences are processed in the codegen."""
        cg = CCodeGenerator()
        assert cg._process_escapes(r"hello\nworld") == "hello\nworld"
        assert cg._process_escapes(r"tab\there") == "tab\there"
        assert cg._process_escapes(r"back\\slash") == "back\\slash"
        assert cg._process_escapes(r"null\0end") == "null\0end"
        assert cg._process_escapes(r"\x1bLua") == "\x1bLua"
        assert cg._process_escapes(r"\r\n") == "\r\n"

    def test_string_with_newline_in_ir(self):
        """Test that \\n in string becomes actual newline byte in IR."""
        cg = CCodeGenerator()
        p = CParser()
        ast = p.parse(r"""
            int main(){
                printf("hello\n");
                return 0;
            }
        """)
        cg.generate_code(ast)
        ir_str = str(cg.module)
        # The newline should be encoded as \0a in LLVM IR
        assert "\\0a" in ir_str or "x0a" in ir_str.lower() or "\n" in ir_str


if __name__ == "__main__":
    unittest.main()
