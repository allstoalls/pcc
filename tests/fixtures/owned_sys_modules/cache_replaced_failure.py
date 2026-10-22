import sys
import gc
from cache_state import events

class Replacement:
    def __del__(self):
        gc.collect()
        events.append('replacement dropped')

sys.modules[__name__] = Replacement()
raise ValueError('replaced failure')
