# Windows VM review

The QEMU guest has two modes. A **detonation** uploads launchable Windows shapes
(`.exe`, `.ps1`, `.lnk`, `.msi`, `.bat`, `.cmd`, `.scr`, `.com`) over WinRM and
executes them. A **review** puts every staged file, whatever its type, on a
read-only CD and opens it with the Windows application a person would use. The
review mode exists because most torrents are films, albums, images and
subtitles: there is no Windows program to execute, but there is still a parser
that a malicious file could attack.

This document covers review mode. The detonation path, image build and VM
hardening are described in `README.md` and `docs/SECURITY.md`.

## What review mode does

1. The host selects which staged files are worth keeping (see
   [Selection and the graveyard](#selection-and-the-graveyard)).
2. Every non-empty artifact is written to an ISO labelled `MINUS_REVIEW`.
   QEMU attaches it as a second CD with `readonly=on`.
3. The guest finds the CD by volume label and opens each file directly from
   it. The CD is read-only; players write only their own cache to the overlay.
   - **VLC** for video (`mp4`, `mkv`, `avi`, `mov`, `wmv`, `m4v`, `webm`, `mpg`,
     `mpeg`, `ts`, `flv`), installed at `C:\tools\vlc` by `--vlc-zip`; falls
     back to **Windows Media Player** when VLC is absent;
   - **Paint** for images (`jpg`, `jpeg`, `png`, `gif`, `bmp`, `webp`, `tif`,
     `tiff`);
   - **Notepad** for text and subtitles (`txt`, `srt`, `vtt`, `nfo`, `md`, `log`,
     `ini`, `cfg`);
   - the **shell association** for any other type. An explicit handler is
     preferred because it is deterministic. The shell association is started for
     completeness but is **never counted as opened**: on Windows 10 it is often
     the "how do you want to open this file?" dialog, which is not a parser. An
     explicit handler counts only if it is still alive two seconds after it
     starts, so a player that rejects a corrupt container does not manufacture a
     detonation.
4. Launchable shapes are executed rather than opened, exactly as in a
   detonation. A torrent containing both a film and a crack gets both paths
   exercised.
5. While that happens the guest records process, network, file-write, registry
   and Defender evidence (see [Telemetry](#telemetry)).
6. Small files not selected for keeping are copied to `C:\triage\graveyard` in
   the overlay, which is discarded with the run. A large non-kept file is left on
   the CD instead of doubling the torrent into the overlay.

A review is complete only when the CD was found, the time budget did not expire,
and every file in the keep set was actually opened or launched. A missing CD, an
exhausted budget, or a kept file whose handler never opened it sets
`completed: false`; the host then reports `detonated: false`
and the promotion gate refuses the item even if another file opened successfully.
A review that did not cover the whole kept set is not evidence about the files it
missed.

A review counts as a detonation only when the guest reports that it opened or
launched something.
`SandboxReport.detonated` is true when the harness completed and
`launched + opened > 0`. A player that never opens the container leaves the
verdict at `needs_review`.

## When it runs

`TriagePipeline.scan(torrent_hash, detonate=True)` uses review mode
automatically when all of these hold:

- `MINUS_TRIAGE_VM_REVIEW` is on (the default);
- the staged tree has at least one file;
- the configured sandbox exposes `review_unavailable_reason()` and it returns an
  empty string. That means qemu/qemu-img are on `PATH`, the base image and work
  directory exist, free space is above `MINUS_WINDOWS_MIN_FREE_BYTES`, and both
  `pywinrm` and `pycdlib` are installed.

The standalone pass does the same run without rescanning:

```bash
minus triage vm <hash>                 # write vm_review.json, change no verdict
minus triage vm <hash> --keep movie.mp4
```

It writes `quarantine/<hash>/vm_review.json` and leaves `verdict.json` alone.
`scan` writes the same file when it used review mode. The MCP tool
`triage_vm_review(torrent_hash, keep="...")` calls the same pipeline method.
`--no-detonate` skips the guest entirely.

If the guest is not configured, `minus triage vm` refuses with the reason
(`pywinrm is not installed`, `base image … does not exist`, and so on) rather
than pretending a dry run reviewed anything. `minus vm-check` reports
whether a run could start and what is missing. It checks the detonation
prerequisites (qemu, base image, work directory, free space, `pywinrm`);
review additionally needs `pycdlib`, which `minus triage vm` names if it is
absent.

## Selection and the graveyard

The only files that can ever reach the library are the selected `.mp4` files;
everything else is release furniture or a second payload. With no `--keep`:

1. every video (`.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, `.m4v`, `.wmv`, `.mpg`,
   `.mpeg`, `.flv`, `.ts`);
2. else every audio file (`.mp3`, `.flac`, `.m4a`, `.aac`, `.ogg`, `.opus`,
   `.wav`);
3. else every file.

`--keep` overrides that with a comma-separated list of relative paths or
basenames. A name that matches no staged file is refused rather than silently
ignored. Promotion computes its own selection from the host manifest, not from
the guest's graveyard, so pass the same list: a promotion selection that
includes a file the review did not keep is refused, not silently copied.

The library itself is MP4-only and flat: `library/<movie>.mp4`. A selected
`.mkv`, audio file or document is not copied; if the selection has no `.mp4`
at all, promotion refuses. Inside the guest, small files not in the keep set are
copied to `C:\triage\graveyard`; a large non-kept file stays on the read-only
CD. That overlay directory is deleted with the run, and the staged host tree is
deleted after a successful promotion. Provenance is written outside the library,
at `memory/library_provenance/<slug>.provenance.json` (0600), recording the
`selected` and `graveyard` lists.

This is the Jellyfin layout: point a library at `library/` and it sees only
flat `.mp4` files. Artwork, NFO files, subtitles, release folders and provenance
sidecars never appear there.

## The ISO transfer

A detonation uploads through WinRM and skips an artifact larger than
`MINUS_WINDOWS_MAX_ARTIFACT_BYTES` (64 MiB by default), as well as anything that
would take the total upload past the same cap. That is far below a single
episode, which is why review mode does not use WinRM for artifacts at all.

Review mode builds an ISO9660 level-3 image with Joliet names, which carries the
real sanitised filename while the ISO9660 name is reduced to `A-Z0-9_`. It has
two hard limits, both checked before the image is written:

- **just under 4 GiB per file** — the ISO9660 level-3 single-file limit. A 4K
  remux over it is refused with its size rather than producing an image Windows
  cannot read.
- **free space** — the total artifact size plus `MINUS_WINDOWS_MIN_FREE_BYTES`
  must fit on the work volume.

QEMU mounts the ISO read-only. The guest can read it and copy files into its own
overlay, but cannot write back to the host's image. There is no shared folder,
no SMB and no 9p mount; the CD is a one-way channel.

## Telemetry

The guest records the same evidence in both modes:

| evidence | source |
|---|---|
| Defender detections | `MpCmdRun.exe -Scan -ScanType 3 -DisableRemediation` then `Get-MpThreatDetection` |
| processes | Sysmon event 1, or a `Get-Process` diff against the baseline |
| network endpoints | Sysmon event 3, or `Get-NetTCPConnection` |
| file writes | Sysmon event 11 (excluding `C:\Windows\*`) |
| registry writes | Sysmon event 13, or a `Run` key baseline diff |

**Sysmon is optional but preferred.** Install it when building the base image;
the harness detects the `Sysmon64` service and reads its operational log. Without
it, the polling fallback is still real telemetry — it just samples instead of
seeing every event, so a short-lived child process can slip between polls. The
result says which it used in `notes` (`no Sysmon: process/network telemetry is
polling-based`).

The guest's network device is QEMU's `user,restrict=on` NIC with a single
host→guest WinRM forward. Nothing in review mode changes that: there is no
guest egress. A sample that tries to phone home has nowhere to go, and the
absence of a connection is not evidence that it would not have tried somewhere
else.

## Reading the evidence

`vm_review.json` has the verdict-independent facts:

```json
{
  "torrent_hash": "…",
  "name": "…",
  "reviewed_at": "2026-…",
  "selected": ["Movie/movie.mp4"],
  "engine": "windows-vm",
  "completed": true,
  "detonated": true,
  "opened": 1,
  "reviewed": true,
  "defender_detections": [],
  "processes": ["wmplayer.exe"],
  "network_endpoints": [],
  "file_writes": [],
  "registry_writes": [],
  "notes": "reviewed: opened 1, launched 0; graveyard: 2 file(s)"
}
```

`completed` is false when the guest could not copy every file or ran out of its
time budget; `detonated` is then false regardless of how many files opened, and
the promotion gate refuses the item. `triage_verdict` reports `detonated`,
`reviewed` and `opened`; `triage_review`
uses both the verdict's `sandbox` block and `vm_review.json` to decide
`can_accept_review`. Promotion of a `needs_review` item refuses until
`detonated` is true or `opened > 0`:

```bash
minus triage promote <hash> --accept-review --yes --keep movie.mp4
```

A dry run, a host-only ffprobe pass, or a scan with `--no-detonate` cannot
satisfy that requirement, and the refusal names `minus triage vm`.

`triage/scoring.py` still decides the label. Review mode can produce `clean` for
a media-only torrent when the player opened a file and no telemetry fired, but a
`clean` verdict is still refused if the torrent also contains Linux-active
files: the Windows guest cannot exercise an ELF binary, a shell script or a
`.jar`.

## Honesty about the result

Opening a movie in VLC (or Windows Media Player when VLC is absent) exercises
the same container parser a user's Windows machine would use, and that is a real
improvement over a filename-only check. It is still **evidence, not proof**:

- one player, one codec build, one guest image, one point in time;
- telemetry observes process, network, file and registry effects, not memory or
  logic bugs;
- Defender is run without remediation, so a detection is recorded but not
  cleaned — a clean result means "nothing was reported", not "nothing is there";
- the hypervisor is the isolation boundary; a qemu escape is out of scope;
- without Sysmon, short-lived behaviour between polls can be missed.

Treat a reviewed `needs_review` item as "the known parser path did not misbehave
in this guest", and decide whether that is enough.

## Setup and knobs

```bash
uv sync --extra windows                     # pywinrm + pycdlib
minus vm-image --iso Win10_Enterprise_x64.iso \
               --work-dir /srv/minus/windows \
               --sysmon-zip Sysmon.zip \
               --vlc-zip vlc-3.0.24-win64.zip   # portable zip from get.videolan.org
minus vm-check                              # what is missing, if anything
```

`minus vm-image` needs `MINUS_WINDOWS_WINRM_PASSWORD` set; it writes
`base.qcow2` and `base.json` under `--work-dir` (which needs at least 40 GiB
free). The image is built once from
official Microsoft Windows 10 x64 media (Enterprise LTSC or Enterprise
Evaluation); trimmed builds are not acceptable because they remove WinRM,
Defender, PowerShell or Sysmon.

Environment:

| variable | default | purpose |
|---|---|---|
| `MINUS_WINDOWS_BASE_IMAGE` | empty | immutable `base.qcow2`; unset means the dry-run runner |
| `MINUS_WINDOWS_WORK_DIR` | empty | overlays, review ISOs, `failed/` evidence |
| `MINUS_TRIAGE_VM_REVIEW` | `true` | use review mode in `scan` |
| `MINUS_TRIAGE_VM_REVIEW_TIMEOUT` | `300` | guest harness budget after boot, in seconds |
| `MINUS_WINDOWS_MAX_ARTIFACT_BYTES` | 64 MiB | detonation upload cap, per file and total (not review) |
| `MINUS_WINDOWS_MIN_FREE_BYTES` | 20 GiB | free space required before a run |
| `MINUS_WINDOWS_KEEP_EVIDENCE` | `true` | archive failed runs under `<work dir>/failed/<run>/` |
| `MINUS_WINDOWS_CPUS`, `MINUS_WINDOWS_MEMORY_MB` | 4, 6144 | guest size |
| `MINUS_WINDOWS_BOOT_TIMEOUT` | 420 | seconds WinRM may take to come up |
| `MINUS_WINDOWS_GUEST_TIMEOUT` | 180 | WinRM operation timeout for guest calls |
| `MINUS_WINDOWS_WINRM_USER`, `MINUS_WINDOWS_WINRM_PASSWORD` | `minus`, empty | guest control account |

`MINUS_WINDOWS_KEEP_EVIDENCE` only affects failed runs: a successful run's
overlay, ISO and console log are removed, while a failed one archives
`overlay.qcow2`, `console.log` and `qemu.pid` under
`<MINUS_WINDOWS_WORK_DIR>/failed/<run>/` so the
failure can be inspected. The guest graveyard is inside that overlay and dies
with it either way.
