# MINUS security model

MINUS runs on a single-user workstation, keeps personal memory on disk, holds API
keys, and can run tools: a coding shell, torrent ingest, and a Windows guest that
detonates untrusted files. This file records what is defended, how, and what is
explicitly **not** guaranteed. When a change weakens a control here, that change
needs a reason in its description.

## Credentials

- Every secret is read from the environment or `.env`: `OPENROUTER_API_KEY` /
  `MINUS_LLM_API_KEY`, the three Google values, `MINUS_QBITTORRENT_PASSWORD`,
  `MINUS_AIRVPN_PASSWORD`, `MINUS_WINDOWS_WINRM_PASSWORD`. None of them is ever
  written by the dashboard (`SECRETS` in `control/config_control.py` is a deny-list
  for both reads and writes), logged, or returned in `get_config`.
- MINUS drives AirVPN's own suite (bluetit + goldcrest) when credentials are
  configured, and still verifies the kernel route before any fetch. The password
  is redacted from every client output and error; the client command is allowed a
  passwordless `sudo -n` retry through one sudoers drop-in because a fresh
  `airvpn` group is not in the current login session.
- `.env` is rewritten atomically at 0600 (`services/env_file.py`); a value that was never set is never invented.
- Exposed secrets are rotated by the owner, not patched around: treat any value
  pasted into a chat, a shell history or a log as burned.

## Files and permissions

| data | mode |
|---|---|
| `memory/**` JSON (transcripts, condensed, deep notes, coding sessions) | 0600, directories 0700 |
| `logs/**` run and console logs (they contain transcripts) | 0600, directory 0700 |
| `memory/semantic_memory.db`, `quarantine/**`, `library/**` | 0600 (JSON manifests; SQLite follows umask) |
| `.env` | 0600, atomic replace preserving mode on rewrite |
| control socket | 0600 inside a 0700 parent; doubles as the single-instance lock |
| guest unattended install file | 0600, deleted in `finally` |

`write_json` sets the mode on the temporary file before the rename, so a file is
never visible with looser permissions than intended.

## Posture and exposure

`minus doctor` (and `minus doctor --json`) prints the effective posture without
secrets and lists every dangerous condition in full sentences: provider endpoint
and whether a key is configured (never the key), VPN enforcement state, sandbox
availability and reason, plugin/memory status, control-socket and storage
permissions, and each risky flag. It is read-only and makes no network calls.

## Network and the sandbox

- **qBittorrent is the downloader, so its binding is what is verified.** The
  preflight reads `current_network_interface` from qBittorrent's own
  `/api/v2/app/preferences` and requires an exact match with
  `MINUS_QBITTORRENT_VPN_INTERFACE`; a mismatch, an empty preference, or no
  configured interface refuses the fetch. Per-user policy routing (the download
  user routed through a tunnel) cannot be proven from outside, which is why the
  client's own interface binding is the check and why binding it in qBittorrent
  as well is the recommended defence in depth. The host-route check
  (`MINUS_VPN_INTERFACE`) remains for setups where MINUS itself fetches.
- **VPN preflight is fail-closed.** With `MINUS_REQUIRE_VPN_FOR_TRIAGE` on (the
  default), `TriagePipeline.download` refuses unless `MINUS_VPN_INTERFACE` is
  set, the link is up, and `ip route get 1.1.1.1` leaves through that interface.
  Enforcement on with no interface named is a refusal, not a pass.
- **Discovery results are attacker-influenced input.** `triage/discovery.py`
  queries public indexes and ranks what they return; its title, seed-count,
  "verified" and virus-score signals are a pre-filter for picking what to
  quarantine, never evidence that a file is safe. The selected magnet still goes
  through `download` → `scan` → `verdict`, and auto-promotion is only reachable
  on a `clean` label. An empty or failing provider is reported, not guessed
  around. `MINUS_DISCOVERY_ENABLED=false` removes search entirely and leaves the
  typed-magnet path unchanged.
- **A Windows detonation is Windows-only.** The guest launches Windows shapes
  (`.exe`, `.bat`, `.ps1`, `.msi`, `.lnk`, `.scr`, `.com`) and, in review mode,
  opens everything else with a Windows application — VLC for video (Windows
  Media Player when VLC is absent), Paint for images, Notepad for text, the shell
  association otherwise. A movie opened by a player is a real parser path and
  counts as a detonation when an explicit handler stays alive; the
  shell-association dialog is never counted, and a missing CD or an unopened kept
  file keeps the review incomplete. It is still a *Windows* parser path: the
  guest never runs ELF, Mach-O, Linux scripts, or `.jar` files.
  `TriagePipeline.promote` therefore refuses a `clean`
  verdict when the staged tree contains those Linux-active files, even if
  another artifact was opened or detonated. They need a Linux-side detonation or
  manual quarantine review; a Windows-clean label is not proof they are safe on
  this host.
- **Manual release is explicit, passive-only and requires VM evidence.**
  `triage review` shows the static facts and runs `ffprobe` over up to five media
  files under bubblewrap, with read-only input and no network; that is parser
  evidence, not a detonation, and it cannot satisfy the manual-release gate.
  `triage promote --accept-review` releases **only** a `needs_review` verdict
  whose files are passive media/text and whose static/airlock flags contain no
  executable, script, archive, document or other active content, *and* only when
  `verdict.json` or `vm_review.json` records `sandbox.detonated` or
  `sandbox.opened > 0` from the Windows guest. A dry run, a host-only scan, or
  `--no-detonate` is refused, and the error names `minus triage vm`. Its
  provenance records the accepting user, UTC time, reason, `detonated` and
  `vm_reviewed: true`. `suspicious`, `malicious` and active-content payloads are
  never releasable this way. The model-facing `triage_promote` tool can pass
  `accept_review` and `reason`, and the same evidence requirement applies to it.
- **The guest has no route anywhere.** The QEMU NIC is
  `-netdev user,restrict=on` plus a host→guest WinRM forward; QEMU's own
  documentation states the guest cannot contact the host and no guest packet is
  routed out, while forwarding rules are unaffected. In detonation mode artifacts
  are pushed and the report is pulled over WinRM; in review mode artifacts travel
  on a read-only ISO/CD that QEMU mounts with `readonly=on`. Either way there is
  no guest→host connection at all.
- **No host filesystem in the guest.** No shared folders, no SMB, no 9p. The base
  image is immutable; each run gets a throwaway qcow2 overlay that is deleted with
  the run directory. Review mode's ISO is a read-only second CD, not a share: the
  guest copies files into its own overlay and anything not kept is moved to a
  guest-side graveyard inside that overlay, which dies with the run.
- **Untrusted filenames are sanitised** before they become guest paths: a torrent
  can name a file `..\..\Windows\x`, and on Windows a backslash is a separator.
  Only `[A-Za-z0-9._-]` survives, with a numeric prefix that also rules out
  reserved device names. The ISO's ISO9660 names are reduced to `A-Z0-9_`, while
  the sanitised name rides in Joliet for Windows to display.
- **Released content is Jellyfin-ready and least-privilege:** `library/` is flat
  and MP4-only — `library/<movie>.mp4`, 0644 files in a 0755 directory, no
  group/write bits, an atomic publish (each selected file is copied and fsynced
  into a hidden staging directory, then renamed into place), and no-clobber
  suffixes. A selected `.mkv`, audio file or document is not copied; a promotion
  with no `.mp4` is refused. The 0600 provenance JSON lives outside the library
  at `memory/library_provenance/<slug>.provenance.json`, so a media scanner sees
  only videos. The staged host tree is deleted after a successful promotion.
- **Bounded input:** detonation uploads are bounded by
  `MINUS_WINDOWS_MAX_ARTIFACT_BYTES`, per file and in total; anything over it is
  skipped and named in the verdict's notes, never silently dropped. Review
  mode instead refuses before building the ISO when a single file exceeds the
  ISO9660 level-3 per-file limit (just under 4 GiB) or the work volume lacks the
  image space plus `MINUS_WINDOWS_MIN_FREE_BYTES`.

## Airlock without a Windows guest

Full Windows detonation needs qemu plus ~30 GiB of image, which this class of machine may
not have. `triage/airlock.py` is the no-execution alternative:

- static checks over the staged tree: private/executable permission flags, zip
  member path traversal, expansion bombs (size or ratio), encrypted archives,
  oversized archives and member-count/tree-size caps (so a hostile torrent
  cannot make the *inspection* the denial of service), corrupt-archive notes,
  and which malware engines (ClamAV/YARA) are present (absence is reported,
  never assumed clean);
- `run_isolated(root, argv)` wraps anything that must run in `bwrap
  --unshare-all` (network, user, pid namespaces), read-only system binds, the
  staged tree read-only at `/input`, no host home, all capabilities dropped
  (`--cap-drop ALL`);
- staging now strips the executable bit and forces 0600 files / 0700
  directories before anything is inspected. A verdict still cannot be `clean`
  without a detonation, so promotion stays refused until a real guest exists.
- **The model review is advisory but asymmetric.** `triage/review.py` reads
  code-shaped text files under caps and asks the configured model what they would
  do. The prompt treats every byte as data, not instructions, but prompt injection
  is still possible; that is why the review can only *escalate*. A `malicious`
  read deletes immediately, `high` is suspicious, and `medium` or a failed review
  blocks `clean` rather than being treated as innocent.
- **Malicious verdicts delete the payload immediately** (`MINUS_TRIAGE_DELETE_MALICIOUS`,
  default on): the staged tree is removed in one `rmtree`, a `CRITICAL` line names
  the torrent and the reasons, and the scan result carries an `alert` string. The
  verdict survives as a 0600 record under `quarantine/rejected/` containing the
  hash, reasons and file *names* — never the bytes.
- **Auto-cleanup deletes the download only after a verdict.** `MINUS_TRIAGE_AUTO_CLEANUP`
  (default on) asks qBittorrent to remove the torrent and the files it downloaded once a
  promotion has succeeded — a `clean` release, or a manual `--accept-review` whose
  provenance (operator, UTC time, reason, `detonated`, `vm_reviewed: true`) is already written
  under `memory/library_provenance/` — or a `malicious` verdict has been rejected. It never deletes before
  a verdict exists, and it keeps the download for `needs_review` and `suspicious`, which
  are exactly the states a human is meant to inspect and possibly release. A failed
  qBittorrent call is logged and does not undo the promotion or rejection. This changes
  only what happens to the duplicate copy; the trust boundary is unchanged — discovery
  is still a pre-filter and only a verdict can promote.
- **Boundary, stated plainly:** without the Windows guest configured, storing a
  file is inert; nothing in the airlock path executes Windows binaries (no
  Wine/VM), so a PE cannot run, and the verdict stays `needs_review`.
  `run_isolated` is namespace containment, not a VM — kernel bugs remain in
  scope, and the downloader's own parser (qBittorrent/libtorrent) is attack
  surface MINUS does not sandbox. Keep qBittorrent updated, bind its WebUI to
  loopback, and mount quarantine `noexec,nosuid,nodev` if the filesystem allows
  it.
- `torrent_add` accepts only magnet/HTTP(S) strings — never a local path — so
  MINUS cannot be handed a file to seed; qBittorrent uses its own configured
  save path.

## Posture, secrets and exposure

- `minus secrets-scan` walks the repository (skipping venvs, caches, runtime
  data) and reports key-shaped findings as `path:line kind` without ever printing
  the match. Credential fields on `Settings` are `repr=False`, so a traceback or
  log line cannot leak them.
- `minus doctor` reports listening sockets from `ss -ltn` and flags public
  binds, plus every dangerous posture item.

## MCP server

`minus mcp` is a local stdio process with the user's privileges; it is not a
sandbox. It exposes the same registry the assistant uses, so the workspace
confinement, VPN gate, quarantine staging and promotion rule all still apply.
`escalate` and the coding tools are not exposed: one needs a live conversation,
the other needs an approval UI the client does not have. stdout carries only
JSON-RPC; logs go to stderr and the run log.

## Untrusted input and tool execution

- Tool arguments are validated against generated schemas before the body runs
  (`tools/registry.py`); a disabled tool cannot be dispatched.
- Coding tools are confined to the session workspace by
  `resolve_workspace_path` (resolve before containment), which rejects absolute
  paths, `..`, and symlinks out.
- `minus code` gates every write/patch/shell behind `ApprovalRequest`; `--yes`
  (auto-approve) is explicit, and a non-TTY without it is refused. Run `--yes`
  only in a disposable workspace.
- **Lua plugins are off by default** (`MINUS_PLUGINS_ENABLED=false`). `lupa` runs
  Lua in-process with no instruction or memory limit; removing `os`/`io`/`require`
  prevents accidents, not an adversary. Enabling a plugin directory is equivalent
  to running its code with your account.
- Prompt injection is real: torrent names, file contents, logs and web text can
  steer the model. The guards are the tool schema, the approval gate, the
  workspace confinement and the VPN gate — not the model's judgement.

## Malware detonation

- The guest is treated as hostile. Keep KVM/QEMU patched; the hypervisor is the
  isolation boundary and MINUS cannot add to it.
- Defender runs with `-DisableRemediation` so evidence survives; the guest is
  discarded afterwards regardless. Sysmon is optional but preferred: the base
  image installs it from `minus vm-image --sysmon-zip`, and without it the
  harness polls processes, TCP connections and the `Run` keys instead. Polling
  sees less; it does not turn "nothing observed" into proof.
- A verdict is evidence, not a guarantee. `clean` means "detonated and nothing was
  observed", never "safe". For a reviewed movie that means the guest's player
  opened the container and telemetry recorded no suspicious process, network,
  file or registry activity — exactly the parser a user's Windows machine would
  use, but still one guest, one build and one time window. Promotion refuses
  anything else.
- Never detonate on the host, and never mount the library into the guest.
- **Keep the guest's one network device restricted.** The runner uses QEMU's
  `user,restrict=on` NIC with a single host→guest WinRM forward; do not bridge it or add a
  host share, SMB or 9p mount for convenience. The guest is assumed compromised by the sample
  it runs, so any route out of it is a route from malware to the network MINUS was careful to
  keep it off.
- **Only official Microsoft media builds the guest.** An official Windows 10 Enterprise LTSC
  (or Enterprise Evaluation) x64 ISO is the input to `minus vm-image`; trimmed builds such as
  "Tiny11" or "Lean11" are not acceptable whatever their size. They remove Defender, WinRM,
  PowerShell and often Sysmon — the baseline controls and the only control channel — so a
  sample that looks inert in one would not have been given a fair chance to act, and the
  `clean` verdict it produced would be meaningless.
- **The guest's hardware is chosen to be boring.** SeaBIOS, an IDE disk and an e1000 NIC all
  have Windows 10 in-box drivers, so the image can be built from official media with no
  OVMF/UEFI firmware, no emulated TPM and no virtio-win ISO. Fewer third-party drivers in the
  build path means fewer things that can fail silently before the sample ever runs; speed is
  not the requirement for a minutes-long detonation, and an optional virtio-win ISO exists
  only for whoever wants it.
- **Image and run directories need an ext4 volume.** The base image is a 64 GiB sparse qcow2,
  ~20–30 GiB of real blocks once Windows is installed. Keep `MINUS_WINDOWS_BASE_IMAGE`,
  `MINUS_WINDOWS_WORK_DIR` on ext4 (the ~62 GiB root is fine) rather than on an NTFS volume
  shared with Windows: a hibernated or Fast-Startup NTFS still has Windows' journal open, so
  letting Linux replay it write-access corrupts the filesystem for both systems. If NTFS is
  unavoidable, disable Fast Startup and shut Windows down fully first, and treat it as
  temporary. `MINUS_WINDOWS_MIN_FREE_BYTES` (default 20 GiB) is checked before a run starts;
  the Windows ISO can be deleted once the base image exists, but the base image cannot.

## Privacy

- Transcripts and facts go to the configured model provider on every turn. That is
  the product, and it is worth knowing: a self-hosted endpoint is the only way to
  keep prompts local.
- A direct provider key (`MINUS_LLM_BASE_URL`) changes who sees them.

## Known limits

- The Lua sandbox is not a security boundary (see above).
- The control socket is unauthenticated by design; it is protected by filesystem
  permissions on a 0700 directory and by the single-instance probe.
- A compromised user account, or any process running as that user, can read the
  API keys in `.env` and the memory database.
- No audit log exists for tool calls beyond the run log.
