import sys
import cache_cycle_owner
observed = cache_cycle_owner.state
same = sys.modules["cache_cycle_owner"] is cache_cycle_owner
