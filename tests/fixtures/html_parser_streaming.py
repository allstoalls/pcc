"""Native changed-shape witness for the owned HTMLParser provider."""
from html.parser import HTMLParser
import gc


class Recorder(HTMLParser):
    def __init__(self, convert):
        super().__init__(convert_charrefs=convert, scripting=True)
        self.events = []

    def handle_starttag(self, tag, attrs):
        self.events.append(('start', tag, attrs, self.get_starttag_text(), self.getpos()))

    def handle_endtag(self, tag):
        self.events.append(('end', tag))

    def handle_data(self, data):
        self.events.append(('data', data))

    def handle_comment(self, data):
        self.events.append(('comment', data))

    def handle_decl(self, data):
        self.events.append(('decl', data))

    def handle_pi(self, data):
        self.events.append(('pi', data))

    def handle_entityref(self, name):
        self.events.append(('entity', name))

    def handle_charref(self, name):
        self.events.append(('char', name))

    def unknown_decl(self, data):
        self.events.append(('unknown', data))


class LinkRecorder(Recorder):
    CDATA_CONTENT_ELEMENTS = ('link',)


def main():
    text = ('<!DOCTYPE html><A h="x&amp;y" bare disabled>one\n&#x80;'
            '<!-- hi --!><br/><?work?><![CDATA[x<y]]></A>'
            '<script>if(a<b)&amp;</SCRIPT><textarea>&copy;<b></textarea>'
            '<noscript><a href="ignored"></noscript>tail &notit;')
    for convert in (True, False):
        for step in (1, 3, 1000):
            parser = Recorder(convert)
            i = 0
            while i < len(text):
                parser.feed(text[i:i + step])
                i += step
                gc.collect()
            parser.close()
            print(convert, step, parser.events, parser.getpos())
            parser.reset()
            parser.events = []
            parser.feed('reset &amp')
            parser.close()
            print(parser.events, parser.getpos())
    parser = LinkRecorder(True)
    parser.feed('<link>inside</linK>outside')
    parser.close()
    print(parser.events, parser.cdata_elem)
    parser.feed('</LINK>')
    parser.close()
    print(parser.events, parser.cdata_elem)
    gc.collect()


main()
