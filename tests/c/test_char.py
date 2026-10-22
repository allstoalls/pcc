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


class TestChar(unittest.TestCase):
    def test_char_constant(self):
        """Test char constant codegen produces i8."""
        cg = CCodeGenerator()
        p = CParser()
        ast = p.parse('''
            int main(){
                char c = 'A';
                return 0;
            }
        ''')
        cg.generate_code(ast)
        ir_str = str(cg.module)
        # Should contain i8 type for char
        assert 'i8' in ir_str

    def test_char_escape_constant(self):
        """Test char constant with escape like '\\n'."""
        cg = CCodeGenerator()
        p = CParser()
        ast = p.parse(r'''
            int main(){
                char c = '\n';
                return 0;
            }
        ''')
        cg.generate_code(ast)
        ir_str = str(cg.module)
        assert 'i8' in ir_str

    def test_unicode_prefixed_char_constants_parse(self):
        """u'' and U'' character prefixes should be accepted."""
        cg = CCodeGenerator()
        p = CParser()
        ast = p.parse(r'''
            int main(){
                int a = u'A';
                int b = U'B';
                return a + b == ('A' + 'B') ? 0 : 1;
            }
        ''')
        cg.generate_code(ast)
        ir_str = str(cg.module)
        assert "main" in ir_str


if __name__ == '__main__':
    unittest.main()
