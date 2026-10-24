# Final Audit Report — WBT LAT_UNKNOWN_WRITES Research

**Audit date:** 2026-10-25  
**Auditor:** Antigravity (independent re-execution)  
**Branch audited:** `main` at merge commit `1656fa3` (PR #1)  
**Working branch:** `audit/upstream-readiness-2026-10-25`  
**Verdict:** **GO — maintainer inquiry is justified, with constraints documented below**

---

## Phase A — Repository State and Provenance

### Verified repository state

| Item | Value |
|---|---|
| Repository | `manoj-1407/wbt-research` |
| Default branch HEAD | `1656fa3` (Merge pull request #1) |
| Merge included | `880e640` fix(tests): pytest __main__ guard |
| Benchmark commit | `c53cd2a7eb6fb03b3dbcd3ab9fb07dd8e4d0f9b4` |
| Kernel source | WSL2 linux-msft-wsl-6.6.y @ `a07f9ea8a99139913acbcc1c160b132cb2a49c81` |
| Local pull state | Was stale before audit; `git pull origin main` brought to `1656fa3` |
| Working tree | Clean after pull (2 untracked: `__pycache__/`, `scripts/`) |
| Stash | `stash@{0}`: wbt-phase0-preserve-working-tree (preserved, untouched) |

### Experiment branches

| Branch | State |
|---|---|
| `main` | Merged, 80 files, full research package |
| `research/three-run-reconciliation` | Source of PR #1, preserved |
| `exp/qemu-guard-audit` | Earlier experiment, preserved |
| `exp/hold-depth-wbt-guard` | Earlier experiment, preserved |

### Provenance table

| Artifact | Provenance |
|---|---|
| Kernel patch | `0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch` |
| Benchmark run 1 | Actions run `40002470957`, commit `c53cd2a`, ZIP SHA `eb5cf6b8...` |
| Benchmark run 2 | Actions run `40004718895`, commit `c53cd2a`, ZIP SHA `d27aaed1...` |
| Benchmark run 3 | Actions run `40004771234`, commit `c53cd2a`, ZIP SHA `575bbfab...` |
| All 24 raw log files | Committed, SHA-256 verified against manifest (see Phase C) |
| CI verify-evidence | `verify-evidence.yml` — passed on PR #1 head `880e640` |

**Finding:** Local `main` was stale at audit start (at `fb4a55a`, not the merge commit). After `git pull`, `main` correctly contains all 80 files from the PR merge. This is a local-state issue, not a repository defect.

---

## Phase B — Kernel Patch Audit

### Files modified

The patch modifies exactly two files:
- `block/blk-wbt.c` — guard logic added to `LAT_UNKNOWN_WRITES` branch
- `include/trace/events/wbt.h` — new `TRACE_EVENT(wbt_guard)` added

### Guard logic (verified against patch text)

```c
case LAT_UNKNOWN_WRITES:
    if (cb->stat[READ].nr_samples &&
        cb->stat[READ].min > rwb->min_lat_nsec) {
            trace_wbt_guard(..., 1, "hold");
            break;   /* hold depth */
    }
    trace_wbt_guard(..., 0, "scale_up");
    scale_up(rwb);
    break;
```

### Source-level claim verification

| Claim | Verified? | Notes |
|---|---|---|
| `stat_sample_valid()` requires ≥1 read AND ≥3 write samples | ✅ Verified | Patch commit message and upstream-rfc-inquiry.md code block match kernel source pattern |
| `LAT_UNKNOWN_WRITES` calls `scale_up()` unconditionally | ✅ Verified | Patch shows existing behavior; guard is new addition |
| Guard uses `cb->stat[READ].min` | ✅ Verified | Exact field name confirmed in patch diff |
| Guard short-circuits on `r_samples == 0` | ✅ Verified | First condition `cb->stat[READ].nr_samples` prevents evaluation when no reads |
| Tracepoint `TP_printk` format matches parser regex | ✅ Verified | Format: `step=%d, r_samples=%llu, r_min=%lluus, target=%lluus, guard_fired=%d, action=%s` |
| Kernel version: WSL2 6.6.y @ `a07f9ea8...` | ✅ Documented | In `docs/final-decision.md` and build workflow |
| Patch applies to `block/blk-wbt.c` only (behavioral change) | ✅ Verified | `wbt.h` change is tracepoint definition only, no behavioral change |

### Patch concerns

1. **Index hashes are illustrative** (`aaaaaaa..bbbbbbb`): The patch uses placeholder index hashes. This is cosmetically imperfect but does not affect patch application — the diff is applied by content, not by index. Documented.
2. **`Signed-off-by`** uses `VoxBridge Research <research@voxbridge.local>` — a local placeholder identity. For any upstream submission this must be replaced with the author's real identity per kernel contribution process. **This is the most important correction needed before upstream submission.**
3. **No `Fixes:` tag**: Not required for an RFC inquiry but would be expected in a formal patch.
4. **`stat[READ].min` uses nanoseconds as input; tracepoint converts to microseconds** via `div_u64(r_min, 1000)`: Correct and consistent with `min_lat_nsec` target.

---

## Phase C — Benchmark Evidence Audit

### SHA-256 verification: ALL 24 logs match manifest

**Independently verified:** All 24 committed log files (`*_qemu.log`) across all three runs match the SHA-256 hashes in `results/reconciliation_manifest.json` exactly. No corruption, no substitution.

### Recalculation: All three runs VALID & VERIFIED

`python3 scripts/recalculate_all_runs.py` (exit 0):

**Run 1 (40002470957):**

| Config | P50 base→patch | P99 base→patch | wOPS | Guard holds/evals |
|---|---|---|---|---|
| ide_h | 762→736 µs (−3.41%) | 2427→2417 µs (−0.39%) | +8.1% | 0/8 |
| ide_s | 540→544 µs (+0.68%) | 1295→1310 µs (+1.16%) | 0.0% | 0/288 |
| vio_h | 862→876 µs (+1.68%) | 2139→2004 µs (−6.31%) | −1.1% | 0/0 |
| vio_s | 526→553 µs (+5.13%) | 1137→1158 µs (+1.83%) | 0.0% | 276/283 |

**Run 2 (40004718895):**

| Config | P50 base→patch | Guard holds/evals |
|---|---|---|
| ide_s | 1395→1368 µs (−1.94%) | 4/289 |
| vio_s | 1166→1155 µs (−0.93%) | 176/184 |

**Run 3 (40004771234):**

| Config | P50 base→patch | Guard holds/evals |
|---|---|---|
| ide_s | 1361→1372 µs (+0.82%) | 3/288 |
| vio_s | 1142→1124 µs (−1.60%) | 176/184 |

### Denominator correctness

The script correctly separates:
- Total timer windows (wbt_timer events)
- Guard evaluations (wbt_guard events, LAT_UNKNOWN_WRITES branch only)
- Hold decisions (guard_fired=1 live events)
- Scale-up decisions (guard_fired=0 live events)
- Teardown events (excluded from behavioral counts)

The `~95%` claim in `upstream-rfc-inquiry.md` is: 283 evaluations / 298 timer windows = 94.97% — **arithmetically correct**.

---

## Phase D — Three-Run Reconciliation Audit

### Verified percentages

| Run | vio_s P50 delta | vio_s P99 delta | Published | Verified |
|---|---|---|---|---|
| Run 1 | +27.00 µs = +5.13% | +20.83 µs = +1.83% | ✅ | ✅ |
| Rep 2 | −10.83 µs = −0.93% | −53.67 µs = −1.82% | ✅ | ✅ |
| Rep 3 | −18.33 µs = −1.60% | −40.83 µs = −1.62% | ✅ | ✅ |

All published percentages are arithmetically correct.

### Comparability problems (confirmed)

1. **~2.2× host-speed split**: Run 1 baseline vio_s P50 = 526 µs; Reps 2/3 baseline vio_s P50 ≈ 1142–1166 µs. Same commit, same parameters. Cause: QEMU TCG, different GitHub runner load. This is not three comparable replicates.
2. **Run order was fixed** (baseline always ran before patched in the CI matrix). This introduces potential systematic bias that cannot be separated from the guard's effect.
3. **The inert `ide_s` control flips sign** across runs (+0.68%, −1.94%, +0.82%), confirming that the sign changes in `vio_s` are not attributable to the patch.
4. **`ide_h` writer OPS bias** (+7.2–8.1%) is consistent across all three runs and cannot be caused by the patch (guard branch is not entered in heavy profile). This is a run-level noise floor.

**Verdict on reconciliation:** The three-run reconciliation is numerically correct. The conclusion (REVISE) follows correctly from the data. The data does not support a stable performance claim.

---

## Phase E — Guard Predicate Audit

### Current predicate

```c
cb->stat[READ].nr_samples && cb->stat[READ].min > rwb->min_lat_nsec
```

**What it measures:** Whether the fastest completed read in a 100 ms window exceeded the target latency.

**What it misses:** Tail latency. If one read completes at 50 µs (below target) while 99 others complete at 10 ms, `stat[READ].min = 50 µs` and the guard does not fire. TC-11 in the test suite explicitly documents this.

**Evidence from the data:**
- `patched_ide_s` Run 1: 288 evaluations, 0 holds. r_min was 62–77 µs against a 150 µs target. P50 ≈ 540 µs. The predicate was blind to the degraded distribution.
- `patched_ide_s` Reps 2/3: 3–4 holds only because the slower QEMU host pushed the read-floor across 150 µs — the predicate tracked the emulator's speed floor, not read quality.
- This confirms: **the predicate is measuring the emulator's performance boundary, not the workload's read-latency health.**

### Alternative predicates — evaluated

| Candidate | Measures | Misses | Sample requirement | WBT fit | Testable |
|---|---|---|---|---|---|
| **`stat[READ].min`** (current) | Fastest completed read | All tail latency | ≥1 read | Fits existing semantics | Yes (TC-11 demonstrates failure) |
| **`stat[READ].mean`** | Average latency | Outliers in both directions; requires accumulation not in current stat | ≥N reads | Partial — stat struct has `mean` field | Yes |
| **Sample-count gate + min** | Requires N reads before comparing min | Still misses fast-read masking | ≥N reads (configurable) | Fits | Yes |
| **Percentile (P50/P90)** | Distribution shape | Not in blk-stat struct; would require new infrastructure | ≥N reads | Does NOT fit without kernel stat changes | Requires new stat infrastructure |
| **`stat[READ].mean > target`** | Average read latency in window | Low-sample noise; high-latency outlier inflates mean | ≥3–5 reads | Partial fit — mean available | Yes |

### Most defensible next predicate

**`stat[READ].nr_samples >= N && stat[READ].mean > rwb->min_lat_nsec`**

Where N ≥ 3 (matching `RWB_MIN_WRITE_SAMPLES` for symmetry).

Rationale:
- Mean is already tracked in `struct blk_rq_stat` (field `mean`)
- Requiring N samples prevents firing on a single slow read
- Mean is more representative of distribution shape than minimum
- Still conservative: does not require percentile infrastructure
- Directly testable in `test_wbt_state_transition.py` without kernel changes
- Acknowledged limitation: mean can still be pulled down by fast reads, but less severely than minimum

**This is a design suggestion for the maintainer inquiry, not an implemented change.**

---

## Phase F — Harness Audit

### O_DIRECT fix verification

`benchmarks/wbt_writer.c` lines 21–43 contain an explicit comment documenting the O_DIRECT exemption (`wbt_should_throttle()` returns false for `REQ_SYNC|REQ_IDLE`). The writer uses **buffered page-cache writes + fsync** to generate WBT-trackable I/O. **Verified correct.**

### Reader I/O mode

Readers use `O_DIRECT` — correct because reads are never throttled by WBT but their completions are accumulated into `cb->stat[READ]`, which is the guard's input. Buffered reads would be answered from page cache and not generate block-layer latency.

### Harness defects found

| Defect | Severity | Notes |
|---|---|---|
| **Fixed baseline/patched run order** | Medium | Baseline always runs before patched in CI matrix. Cannot separate temporal drift from treatment effect. |
| **QEMU TCG only; no KVM** | High | Absolute latencies not transferable to hardware. Host-speed splits undermine between-run comparisons. |
| **IDE control is degenerate** | Medium | QD=1 → `max_depth=2`; lever is nearly inert even without guard. Cannot cleanly isolate guard effect. |
| **6 reader probes reported as "per-process"** | Low | Correctly documented as concurrent observations, not 6 repetitions. No misrepresentation found. |
| **No predefined pass/fail criteria** | Medium | Success criteria were evaluated post-hoc from results. |
| **Single disk image lifecycle per run** | Low | Fresh image per kernel pair is correct; documented in workflow. |

### Fixes made

**None** — harness defects are design limitations, not bugs. Fixing the run order would invalidate the historical comparison. They are documented for the next experiment design.

---

## Phase G — Test and Analysis Reproduction

### `python3 test_wbt_state_transition.py` (PYTHONIOENCODING=utf-8)

**Exit code: 0**

| Test case | Description | Result |
|---|---|---|
| TC-1 | Pure write window, no reads → both scale_up | **PASS** |
| TC-2 | Reads present, r_min < target → both scale_up | **PASS** |
| TC-3 | CRITICAL: r_min > target → baseline scale_up, patched hold | **PASS** |
| TC-4 | stat_sample_valid=True + r_min > target → both scale_down | **PASS** |
| TC-5 | stat_sample_valid=True + r_min < target → both scale_up | **PASS** |
| TC-6 | Boundary: r_min == target (not >) → both scale_up | **PASS** |
| TC-7 | 5-window hold vs escalation divergence | **PASS** |
| TC-8 | ULLONG_MAX sentinel, r_samples=0 → short-circuit, both scale_up | **PASS** |
| TC-9 | scale_step=-3, r_min > target → baseline −4, patched holds at −3 | **PASS** |
| TC-10 | w_samples=0 → LAT_UNKNOWN, noop for both | **PASS** |
| TC-11 | Fast min masks tail degradation → guard does NOT fire, both scale_up | **PASS** |

**11/11 PASS. Specification is internally consistent.**

**Windows encoding note:** Running without `PYTHONIOENCODING=utf-8` produces a `UnicodeEncodeError` on `→` characters in the cp1252 terminal. The tests pass correctly with the encoding set. This is a Windows console issue, not a test defect. The CI environment (Linux) is unaffected.

### `python3 scripts/recalculate_all_runs.py`

**Exit code: 0**

All three runs: SUCCESS (VALID & VERIFIED). All published numbers reproduced from raw logs. Cross-reconciliation summary matches `docs/three-run-reconciliation.md` exactly.

---

## Phase H — New Experiment Decision

### Decision: **BLOCKED — do not run new experiments yet**

Reasoning:

1. **The existing evidence is sufficient to answer the current question**: The performance hypothesis (holding depth improves read latency) is not supported. Three runs with sign-flipping treatment and sign-flipping control establish this as noise-dominated, not a real effect.

2. **Running more QEMU TCG experiments adds regimes, not replicates**: Without a KVM-capable host or fixed host-speed, each new run is another data point from a potentially different operating regime. This does not resolve the comparability problem.

3. **The predicate must be redesigned before any new experiment is meaningful**: Running more experiments with the current `stat[READ].min` predicate would produce more data about an acknowledged-flawed predicate. The data would not resolve whether the mechanism is sound.

### Pre-conditions for next experiment

| Required | Available locally | Blocker |
|---|---|---|
| KVM-capable Linux host | No (Windows/WSL2) | GitHub Actions `ubuntu-22.04` has no usable KVM |
| Revised predicate (`stat[READ].mean` + sample count) | Design ready (Phase E) | Implementation and kernel build needed |
| Alternating baseline/patched run order | Not in current CI | Workflow redesign needed |
| ≥5 truly comparable repetitions at same operating point | Not available | Requires KVM or bare metal |
| Pre-defined pass/fail criteria | Not formalized | Must be written before running |

### Experiment design (ready when environment is available)

**Hypothesis:** Replacing `stat[READ].min > target` with `stat[READ].nr_samples >= 3 && stat[READ].mean > target` reduces false-negative activations in `LAT_UNKNOWN_WRITES`, and the revised guard produces consistent read-latency improvement across ≥5 same-operating-point repetitions.

**Design:**
- Kernels: baseline + revised-guard-patched
- Control: `vio_h` (heavy profile, guard branch not reachable)  
- Treatment: `vio_s` (sparse, guard-active)
- Comparisons: `ide_s` as a within-run sanity check
- Run order: alternating (baseline → patched → patched → baseline → ...)
- Host: KVM-accelerated runner (or bare metal); record host frequency
- Repetitions: ≥5 at matched operating point
- Raw traces: committed per run
- Pass criteria (pre-defined):
  - Mechanism: `wbt_guard` fires in ≥90% of `LAT_UNKNOWN_WRITES` windows in `vio_s`
  - Writer safety: writer OPS within ±5% of baseline in `vio_s`
  - Performance: `vio_s` P50 improvement ≥2% in ≥4 of 5 repetitions, same direction
  - Control: `vio_h` P50 within ±3% of baseline (guard not reachable)
  - Noise floor: `ide_h` writer OPS within ±3% across all runs

---

## Phase I — Document Corrections

### `upstream-rfc-inquiry.md`

**Issue found:** The `Signed-off-by` in the inquiry text body (not the kernel patch) uses "Manoj Kumar" — which is correct for an inquiry email. The kernel patch uses `VoxBridge Research <research@voxbridge.local>`. These are inconsistent and the patch identity must be resolved before any upstream submission.

**Correction applied:** See updated `upstream-rfc-inquiry.md` (committed in this audit branch).

### `docs/final-decision.md`

**Verified:** Numbers match recalculation. No corrections required.

### `docs/three-run-reconciliation.md`

**Verified:** All percentages correct. All run IDs correct. Commit reference `c53cd2a` correct.

### `docs/limitations.md`

**Verified:** Accurately describes the QEMU TCG limitation, IDE degenerate-lever limitation, and predicate limitation. No corrections required.

### `README.md`

**Minor issue:** Section 6 ("Upstream Inquiry Draft") still contains the old placeholder inquiry text that predates the full research completion. It is superseded by `docs/upstream-rfc-inquiry.md`. The README correctly links to the docs folder.

---

## Phase J — Maintainer Inquiry Audit

### Finding: The existing draft is substantially correct but needs one correction

The draft correctly:
- Explains `stat_sample_valid()` behavior with source-level pseudocode
- Separates the 283/298 window count from the 276/283 hold count
- Reports the three-run inconsistency transparently
- Identifies the `stat[READ].min` structural limitation explicitly (TC-11 equivalent)
- Closes with three precise architectural questions
- Does NOT claim a reproducible performance improvement
- Does NOT claim upstream readiness

**One correction required:** The `Signed-off-by` in the email body says "Manoj Kumar" — this is correct for an email signature. But the inquiry references the patch by name. The patch currently uses `VoxBridge Research <research@voxbridge.local>` as the Signed-off-by. Before sending the inquiry, the patch's Signed-off-by must be updated to the real author identity. This should be noted in the inquiry text or resolved before sending.

**Corrected draft:** The existing `docs/upstream-rfc-inquiry.md` is suitable for sending after that identity issue is resolved. No other quantitative or factual corrections are needed.

---

## Phase K — Security and Repository Hygiene

### Token scan results

- **Git history scan** (all commits, all file types, patterns `ghp_`, `GITHUB_TOKEN =`): **Zero matches**
- **Working tree scan** (all `.py`, `.yml`, `.json`, `.md`, `.txt` files): **Zero matches**
- **`.gitignore`**: Correctly excludes `*.log` by default while re-including `benchmarks/smoke_test/*.log` and `results/**/*.log` for evidence tracking. No leaked secrets via `.gitignore` exclusions.

### Previous token exposure

The `docs/final-decision.md` references "the previously exposed token was not reused." No token value is present in any committed file. **Status: the token exposure was not to this repository's git history.** The exposure was likely in a CI log or environment variable. Action required: **verify token rotation was completed** (out-of-band — cannot be verified from repository content alone).

### Hygiene findings

| Item | Status |
|---|---|
| `__pycache__/` tracked | Not tracked (in `.gitignore`) |
| `.pytest_cache/` | Present in working tree, not tracked by git |
| `scripts/verify_github_api.py` committed | Present; no token hardcoded; uses `GITHUB_TOKEN` env var |
| Index hashes in patch (`aaaaaaa..bbbbbbb`) | Cosmetic, not a security issue |
| `VoxBridge Research` identity in patch | Must be replaced before upstream submission |

---

## Phase L — Final Research Decision

### **GO — Maintainer inquiry is justified**

**Rationale:**

**What is verified and defensible:**
1. The mechanism observation is real: `LAT_UNKNOWN_WRITES` discards read latency evidence and widens write depth — confirmed by `wbt_guard` tracepoint in 283/298 timer windows in Run 1.
2. The guard implementation is correct with respect to its stated predicate.
3. The performance hypothesis is honestly reported as unsupported — three runs with sign-flipping results.
4. The predicate limitation is identified, explained, and tested (TC-11).
5. All 24 raw log files are committed, SHA-256 verified, and reproducible.
6. The inquiry asks architectural questions, not performance claims.

**What requires action before sending:**
- Resolve the `Signed-off-by` identity in the kernel patch (replace `VoxBridge Research <research@voxbridge.local>` with real author identity).
- Verify the GitHub token rotation (out-of-band action).

**What is NOT claimed:**
- Reproducible performance improvement.
- Hardware validation.
- Upstream patch readiness.

**The inquiry is GO for sending after the identity issue is resolved.**

---

## Final Status Summary

| Phase | Status | Key Finding |
|---|---|---|
| A — Repository state | ✅ Complete | Local main was stale; pulled to `1656fa3`. All 80 files present. |
| B — Patch audit | ✅ Complete | Patch correct. Identity placeholder must be replaced before upstream. |
| C — Evidence audit | ✅ Complete | All 24 SHA-256 hashes verified. Recalculation exact. |
| D — Three-run audit | ✅ Complete | Numbers verified. Sign-flip confirmed. Not comparable replicates. |
| E — Predicate audit | ✅ Complete | `stat[READ].min` documented insufficient. `mean + sample count` proposed. |
| F — Harness audit | ✅ Complete | O_DIRECT fix confirmed. Fixed run order documented as design limitation. |
| G — Test reproduction | ✅ Complete | 11/11 TC pass. Recalculation exit 0. All verified independently. |
| H — Experiment decision | ✅ Complete | BLOCKED. KVM host required. Design ready for when environment available. |
| I — Document corrections | ✅ Complete | Inquiry identity note added. Other documents verified correct. |
| J — Inquiry finalization | ✅ Complete | Existing draft suitable after identity resolution. |
| K — Security audit | ✅ Complete | No tokens in repository. Token rotation must be confirmed out-of-band. |
| L — Final decision | ✅ Complete | **GO — inquiry justified after resolving patch identity.** |

### Manual actions still required

1. **Replace `VoxBridge Research <research@voxbridge.local>`** in the kernel patch with the real author's name and kernel.org-registered email before any upstream submission or inquiry that references the patch.
2. **Confirm GitHub token rotation** — the previously exposed token must be revoked if not already done.
3. **Send the inquiry** — the draft in `docs/upstream-rfc-inquiry.md` is ready after item 1 is resolved.

### Research verdict

**REVISE → GO for inquiry (not GO for patch submission)**

The mechanism is demonstrated. The hypothesis is honestly unsupported. The inquiry is ready to initiate a maintainer conversation about the design question. The patch is not ready for upstream submission.
