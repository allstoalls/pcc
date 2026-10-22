def probe(callee, value):
    value = callee(value=value, later=[1, 2])
    value = callee(value=value, later=[3, 4])
    value = callee(value=value, later=[5, 6])
    value = callee(value=value, later=[7, 8])
    value = callee(value=value, later=[9, 10])
    value = callee(value=value, later=[11, 12])
    value = callee(value=value, later=[13, 14])
    value = callee(value=value, later=[15, 16])
    return value
