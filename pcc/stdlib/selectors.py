"""pcc.stdlib.selectors - compile-time friendly selector skeleton."""
from __future__ import annotations

EVENT_READ = 1
EVENT_WRITE = 2


class SelectorKey:
    def __init__(self, fileobj, fd, events, data):
        self.fileobj = fileobj
        self.fd = fd
        self.events = events
        self.data = data


class BaseSelector:
    def __init__(self):
        self._map = {}

    def register(self, fileobj, events, data=None):
        fd = int(fileobj) if isinstance(fileobj, int) else fileobj.fileno()
        key = SelectorKey(fileobj, fd, events, data)
        self._map[fileobj] = key
        return key

    def unregister(self, fileobj):
        key = self._map[fileobj]
        del self._map[fileobj]
        return key

    def modify(self, fileobj, events, data=None):
        self.unregister(fileobj)
        return self.register(fileobj, events, data)

    def select(self, timeout=None):
        return []

    def close(self):
        self._map = {}

    def get_map(self):
        return self._map


class SelectSelector(BaseSelector):
    pass


class PollSelector(BaseSelector):
    pass


class EpollSelector(BaseSelector):
    pass


class KqueueSelector(BaseSelector):
    pass


DefaultSelector = SelectSelector
