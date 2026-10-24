import re
from ftrace_wbt_tracer import guard_pat

sample_line = "          dd-160     [000] ..s1.     2.763941: wbt_guard: 8:0: step=0, r_samples=4, r_min=8500us, target=2000us, guard_fired=1, action=hold"
m = guard_pat.search(sample_line)
assert m is not None, "Failed to match guard_pat!"
ts, dev, step, r_samples, r_min_us, target_us, guard_fired, action = m.groups()
assert ts == "2.763941"
assert dev == "8:0"
assert step == "0"
assert r_samples == "4"
assert r_min_us == "8500"
assert target_us == "2000"
assert guard_fired == "1"
assert action == "hold"
print("SUCCESS: guard_pat regex verified with exact Linux ftrace format!")
