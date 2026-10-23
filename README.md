# WBT Kernel Build — Deployment Guide

## What this directory contains

| File | Purpose |
|------|---------|
| `.github/workflows/build-wbt-kernels.yml` | GitHub Actions workflow |
| `0001-wbt-hold-depth-on-LAT_UNKNOWN_WRITES-when-reads-are-elevated.patch` | Human-readable patch for review/submission |
| `test_wbt_state_transition.py` | Specification test for WBT state transitions |

---

## Step 1 — Fork the Microsoft WSL2 kernel repository

You must own a GitHub repository that hosts the workflow YAML.
The workflow itself clones Microsoft's repo; it does not need to be a fork.

```
# Create a new empty repository on GitHub (e.g. manoj-1407/wbt-research)
# Then push this directory to it:

cd D:\io_investigation\wbt_patch
git init
git add .
git commit -m "Add WBT research build workflow"
git remote add origin https://github.com/manoj-1407/wbt-research.git
git push -u origin main
```

---

## Step 2 — Trigger the workflow

1. Go to your repository on GitHub.
2. Click **Actions** → **WBT Kernel Build — Baseline vs Patched**.
3. Click **Run workflow** → **Run workflow** (branch: main).

The workflow runs entirely on GitHub's Ubuntu 22.04 runners.
Your laptop does **not** compile anything.

Expected wall-clock time: ~60–80 minutes for each build job (GitHub free tier).

---

## Step 3 — Download the artifacts

After both jobs complete, download from the Actions run page:

| Artifact | Contents |
|----------|---------|
| `wbt-baseline-<sha>` | vmlinux, bzImage, .config, build.log |
| `wbt-patched-<sha>` | vmlinux, bzImage, .config, build.log, wbt-patch-applied.diff |
| `build-comparison-report` | Diff + both build logs |

---

## Step 4 — Verify the diff (before installing anything)

```powershell
# In PowerShell, inspect the diff from the downloaded artifact:
cat wbt-patch-applied.diff
```

Confirm it shows exactly the LAT_UNKNOWN_WRITES guard and nothing else.

---

## Step 5 — Install the patched kernel (OPTIONAL — read warnings first)

> [!CAUTION]
> Installing a custom kernel changes your WSL2 boot configuration.
> If the kernel fails to boot, WSL will not start.
> **Do NOT do this until you have verified the build succeeds and
> reviewed the diff.**

If you decide to proceed:

1. Copy `bzImage` from the **patched** artifact to `D:\WSL_Kernels\bzImage-wbt-patched`.
2. Edit `C:\Users\vharr\.wslconfig` — add (do NOT remove the existing `[wsl2]` block):

```ini
[wsl2]
memory=3GB
processors=2
swap=1GB
kernel=D:\\WSL_Kernels\\bzImage-wbt-patched
```

3. Shut down WSL:
```powershell
wsl --shutdown
```

4. Restart Ubuntu and verify:
```bash
uname -r
# Should show your custom build version string

# Verify WBT is built-in:
cat /sys/block/sdb/queue/wbt_lat_usec 2>/dev/null || echo "WBT not active on this device"
```

5. **Reverting**: Remove the `kernel=` line from `.wslconfig` and run `wsl --shutdown` again.

---

## What the patch does NOT do

- It does not change WBT behaviour for pure write-only workloads (no read samples → original `scale_up()` path unchanged).
- It does not introduce a new state or timer.
- It does not touch `RWB_MIN_WRITE_SAMPLES`, window sizes, or any other parameter.

---

## Next steps after a successful build

1. **Source-faithful state transition test** — write a Python test that
   replays the exact `wb_timer_fn` switch logic from the patched source
   and verifies the guard fires when expected.

2. **Native WBT measurement** — boot the baseline kernel, reproduce the
   6–8 ms read P99 scenario, then boot the patched kernel and compare.
   Only this experiment can validate or refute the hypothesis.

3. **Kernel mailing list submission** — if the experiment confirms benefit,
   prepare a proper patch for `linux-block@vger.kernel.org` following
   the Linux kernel patch submission checklist.
