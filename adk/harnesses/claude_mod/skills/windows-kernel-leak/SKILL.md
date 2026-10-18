---
name: windows-kernel-leak
description: >-
  Diagnose a Windows box where every program starts slowly (hooks time out, a bare `cmd /c exit` takes seconds) by measuring the kernel pool first. Covers reading pool tags without admin rights, finding the growing tag, a tag-filtered WPR trace, attributing un-freed allocations to processes and symbolized kernel paths, and the fix.
---
<!-- Generated from the awskills pack (skills/windows-kernel-leak.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# windows-kernel-leak — when every program on the box starts slowly

Hooks time out. `python -c pass` takes four seconds. You blame Python, then the hooks,
then antivirus, then whatever process tops Task Manager. None of those is the cause.
CPU looks fine and Task Manager reports plenty of free RAM.

> Live-measured (2026-10-18, 32-core / 128 GB workstation, 4 days of uptime): `whoami.exe`
> took **470 ms** and `cmd /c exit` took **0.6–2.7 s**, all of it inside CreateProcess. CPU
> was at 46% and file opens took under 25 ms. The kernel pool had grown to **10.6 GB
> nonpaged + 19.8 GB paged**, and commit stood at 169/187 GB. Tag `Toke` held 6.8 GB:
> **3.27 million live access tokens**, growing 3–15 per second. Tag `File` held 4.8 GB:
> 12.7 million file objects. The whole system had only 311k handles, so no user process
> was holding these objects; references inside the kernel kept them alive.

## The tool

[`tools/win-kernel-leak.py`](../tools/win-kernel-leak.py) is pure stdlib. Every mode except
`trace` runs without admin rights.

```bash
python tools/win-kernel-leak.py pool            # totals + top tags; exit 1 over 4 GB (--max-gb N)
python tools/win-kernel-leak.py rate 30         # which tags GROW, live objects per second
python tools/win-kernel-leak.py spawn 5         # CreateProcess latency (the symptom)
python tools/win-kernel-leak.py trace Toke 60   # ADMIN: WPR pool trace for ONE tag -> CSV
python tools/win-kernel-leak.py attribute X.csv # un-freed allocations by process + kernel path
python tools/win-kernel-leak.py --self-test     # proves the parsers and exit codes still fail
```

Exit codes: `0` clean, `1` defect found, `2` could not judge. Treat `2` as a failure.

## The order that finds it in minutes

1. **`spawn`.** If the median is over ~250 ms, the problem is process creation, not your
   script. Stop tuning timeouts.
2. **`pool`.** A healthy desktop keeps paged plus nonpaged pool under about 1–2 GB. Tens of
   GB means a kernel leak, and only a reboot gives that memory back.
3. **`rate 30`.** Ignore the biggest tag. Look for the one whose *live count* rises steadily.
   A tag that rises and falls (here `File` and `FMfn`) is cache churn. A tag that only rises
   (here `Toke`) is the leak.
4. **Check handles before you blame a process.** If the system-wide handle count is far below
   the leaked object count, no user process holds them. Killing apps will not help; a driver or
   kernel component holds the references.
5. **`trace <TAG>` (admin), then `attribute`.** This shows which kernel path *creates* the
   objects that are still alive at the end of the window, and in which process.

## What a trace can and cannot tell you

An allocation stack names **who created** the object, **not who forgot to release it**. In
the case above, `attribute` showed that 705 of 1,019 tokens still alive came from
`PspAllocateProcess > SeSubProcessToken` (the primary token every new process gets). The
rest came from ALPC client security contexts. So the box leaked tokens on its two busiest
paths: process spawns and local RPC. The component holding the extra reference does not
appear in those stacks.

To name the holder, use **object reference tracing** on the tag:

- set it in `gflags` under *Kernel Flags → Object Reference Tracing* with tag `Toke`;
- reboot;
- once the leak has regrown, read the traces with `!obtrace` in a kernel debugger.

This is the one step this skill cannot do live.

**Bisecting drivers by stopping them mostly fails.** `sc stop` on a filter or callback driver
usually leaves it `RUNNING` or stuck in `STOP_PENDING`. Stopping its user-mode service changes
nothing in the kernel. Bisect by *disabling* the driver (`sc config <drv> start= disabled`),
rebooting, and comparing `rate` across boots.

## Traps measured in the session that wrote this

- **Never trace the unfiltered `Pool` profile.** One run wrote **29 GB in 30 s** and dropped
  413M events. Always filter to one tag, as `trace` does.
- **Pool tag table layout (x64):** `SYSTEM_POOLTAG` is 40 bytes. `PagedUsed` is at offset 16 and
  `NonPagedUsed` at offset 32, after 4 bytes of padding at offset 12. The array starts at offset 8.
  Getting this wrong yields plausible-looking garbage, not an error.
- **dbghelp `SYMBOL_INFO.SizeOfStruct` must be 88.** Any other value makes `SymFromAddr` fail
  silently, even with the right PDB loaded.
- **`tracerpt` under Git Bash:** `-lr` cannot be combined with `-of CSV`.
- **PowerShell:** a function named `measure` is shadowed by the built-in `measure` alias
  (`Measure-Object`), and the error message does not say so.
- **A Stop/UserPromptSubmit hook that times out leaves orphans.** The harness kills the
  shell, not the interpreter it started, so slow spawns pile up hung hook processes (42 on
  this box, some 2.6 days old). Kill orphans older than the hook's timeout.

## The fix

- **Now:** reboot. Nothing else returns leaked pool.
- **Prevent the blind spot:** run `pool --max-gb 4` on a schedule and alert on exit 1. The
  leak took four days to become visible to a person; a scheduled check sees it on day one.
- **Find the cause:** enable object reference tracing on the growing tag before that reboot,
  and read `!obtrace` after it.
