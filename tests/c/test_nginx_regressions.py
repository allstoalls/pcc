from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def test_static_bitfield_pointer_initializer_compiles_and_runs():
    src = r"""
        typedef unsigned long size_t;
        typedef unsigned char u_char;

        typedef struct {
            unsigned len:28;
            unsigned valid:1;
            unsigned no_cacheable:1;
            unsigned not_found:1;
            unsigned escape:1;
            u_char *data;
        } ngx_variable_value_t;

        #define ngx_http_variable(v) { sizeof(v) - 1, 1, 0, 0, 0, (u_char *) v }

        static ngx_variable_value_t value = ngx_http_variable("x");

        int main(void) {
            return !(value.valid == 1 && value.len == 1 && value.data[0] == 'x');
        }
    """

    ret = CEvaluator().evaluate(src)
    assert ret == 0
