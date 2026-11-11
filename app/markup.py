from dataclasses import dataclass
from html.parser import HTMLParser

@dataclass(frozen=True)
class Event:
    kind: str
    tag: str
    attrs: tuple
    text: str
    line: int
    column: int

class TokenReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
    def handle_starttag(self, tag, attrs):
        line,col=self.getpos()
        self.events.append(Event("start",tag,tuple(attrs),"",line,col))
    def handle_endtag(self, tag):
        line,col=self.getpos()
        self.events.append(Event("end",tag,(),"",line,col))
    def handle_data(self, data):
        if data.strip():
            line,col=self.getpos()
            self.events.append(Event("text","",(),data,line,col))

def tokens(html):
    parser=TokenReader();parser.feed(html);parser.close()
    return parser.events
