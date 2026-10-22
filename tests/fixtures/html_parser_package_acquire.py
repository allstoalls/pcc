"""Original imported package subclass; do not substitute an extracted class."""
from pcc.package.acquire import _SimpleLinks
import gc


def main():
    parser = _SimpleLinks()
    for chunk in ('<A HREF="pkg.whl?x=1&am', 'p;y=2" ',
                  'data-requires-python=">=3.10">one</A>',
                  '<script><a href="ignored"></script><a href=other.whl/>'):
        parser.feed(chunk)
        gc.collect()
    parser.close()
    print(parser.hrefs)
    print(parser.links)
    parser.reset()
    parser.feed('<a href="reset.whl"/>')
    parser.close()
    print(parser.hrefs)
    print(parser.links)
    gc.collect()


main()
