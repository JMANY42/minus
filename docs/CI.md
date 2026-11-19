# Continuous integration

`.github/workflows/ci.yml` runs on every push to `main`, on every pull request
against `main`, and on demand. It uses only `actions/checkout@v4` and
`astral-sh/setup-uv@v6`, reads the repository with a read-only token, and
cancels a superseded run when a branch moves.

## Jobs

| Job | When | Blocks? | Runs |
| --- | --- | --- | --- |
| `tests` | every run | always | `uv sync --locked --extra dashboard`, `pytest -q` |
| `lint` | every run | changed files | `ruff check --ignore E501`, `ruff format --check` |
| `types` | every run | changed files | `mypy --follow-imports=silent` |
| `runtime-deps` | every run | always | apt install, version print, `minus --help`, probes |

The `dashboard` extra is installed in `tests` because the dashboard and harness
tests import `textual`. Without it those tests skip rather than pass, which
would make a green run meaningless. The workflow installs no `harness`,
`windows`, `audio` or `embeddings` extra: the file must stay byte-identical in
both trees, and the upstream tree has no `harness` or `windows` extra at all.

Every job is capped with `timeout-minutes`, so a hang costs minutes rather than
the default six hours.

## Blocking versus advisory

`lint` and `types` do not gate the whole tree, because the whole tree does not
pass today. The changed-files list is computed from the pull request's target
commit, or from the commit a push replaced; when that object is absent (a
brand-new branch, whose "before" is the all-zero object) the step falls back to
`HEAD^`, and when there is no parent at all the list is empty and both gates
skip. Only files added, copied, modified, renamed or type-changed are checked;
deleted files need no linting. Both gates run only the changed files, and the
`continue-on-error: true` steps run the full tree for visibility.

The pre-existing baseline, measured with the same commands the workflow uses:

| Check | Upstream tree | This tree |
| --- | --- | --- |
| `ruff check --ignore E501` | 22 findings | 22 findings |
| `ruff format --check` | 2 files unformatted | 10 files unformatted |
| `mypy src` | 4 errors | 3 errors |

Upstream's mypy errors are the dashboard against Textual's stubs plus one in
`logging_config`; this tree has the dashboard ones only.

Those numbers are why the full-tree steps are advisory and the changed-files
steps are blocking. Blocking on the full tree would reject every pull request
until the backlog is paid down; blocking on changed files catches a new
regression without freezing the repository. The baseline is pre-existing and is
expected to shrink, not to be pinned: do not add per-file ignores, a baseline
file, or `# type: ignore` comments whose only purpose is to keep the full-tree
run green. When a change fixes one of those findings, the advisory output should
get shorter.

## Runtime prerequisites

| Component | Ubuntu package | Used by | How CI covers it |
| --- | --- | --- | --- |
| qBittorrent | `qbittorrent-nox` | `minus qbt`, VPN binding | installed; version; `qbt --help` |
| QEMU | `qemu-system-x86`, `qemu-utils` | Windows sandbox | installed; versions; `vm-check` |
| mtools | `mtools` | answer floppy | installed; version |
| AirVPN | none | VPN preflight | not installed; fakes only |
| Windows image | none | detonation, review | not built; runner import only |
| Jellyfin | none | bridge installer | not installed; patched temp page |

`runtime-deps` installs the first three packages with `apt-get install
--no-install-recommends` and prints each version, so a failure names the missing
binary instead of surfacing later as an opaque import error. Every runtime probe
is guarded with `importlib.util.find_spec`: the development tree has `minus
bridge`, `minus qbt` and `minus vm-check`, while the upstream tree has none of
them, and the workflow file is identical in both. A missing module is reported
as a skip with its name, not a failure.

The following are deliberately outside CI:

- **AirVPN.** The client is account-bound, the tunnel is privileged, and its
  configuration is per user. There is no unattended way to give a hosted runner
  a working tunnel, and a real tunnel would test the provider as much as this
  code. The preflight is covered by fakes in `tests/test_qbittorrent_vpn.py`;
  a real tunnel stays a manual smoke test on a machine with credentials.
- **The Windows guest image.** Building it needs a licensed Windows ISO, about
  40 GiB of disk, and a first boot long enough to exceed any sane job timeout.
  CI therefore proves the honest smaller claim: the runner imports, the
  install arguments build, and `minus vm-check` reports itself unavailable
  rather than pretending it could detonate. `tests/test_windows_vm.py` covers
  the command construction without running QEMU.
- **Jellyfin.** Not installed. The frontend installer is unit-tested against a
  temporary `index.html` in `tests/test_remote_bridge.py`, which exercises the
  tag insertion, backup and restore paths without a Jellyfin server.

## Reproducing locally

Install what the `tests` job installs and run the suite:

```bash
uv sync --locked --extra dashboard
uv run pytest -q
```

Reproduce the changed-files gates (replace `HEAD^` with the pull request's base
commit when you want the same diff CI computes):

```bash
base="$(git rev-parse HEAD^)"
git diff --name-only --diff-filter=ACMRT "${base}...HEAD" -- '*.py' | sort -u > changed-python.txt
mapfile -t files < changed-python.txt
uv run ruff check --ignore E501 "${files[@]}"
uv run ruff format --check "${files[@]}"
uv run mypy --follow-imports=silent "${files[@]}"
```

Reproduce the runtime job's prerequisites (Debian/Ubuntu; `ubuntu-latest` is the
reference platform, package versions may differ):

```bash
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  qbittorrent-nox qemu-system-x86 qemu-utils mtools
uv sync --locked
uv run minus --help
```

Line-length checking (`E501`) is excluded on purpose at every layer: both the
local commands and the workflow pass `--ignore E501`. `src/minus/prompts.py` is
wire content, and rewrapping it would change what the model receives, so
formatting the tree to satisfy the rule would alter behaviour. `ruff format
--check` never rewraps lines and therefore needs no flag.
