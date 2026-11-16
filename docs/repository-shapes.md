# Repository shapes

Most of the documentation describes an ordinary full clone of a small or medium repository.
This page describes what Agentic Preflight does in other repository shapes that larger teams use.
Each section says what works, what does not, the error you see, and the setting or command that helps.

Statements marked **Verified** are checked by `tests/test_repository_shapes.py`, unless the statement names another test.
Those tests use local repositories and `file://` remotes only.
Statements marked **Unverified** come from reading the code or Git's documentation. No test checks them.
**Known issue (pending)** marks current behavior that is a defect, not the intended design.

| Shape | Supported | Main limit |
|---|---|---|
| [Shallow clone](#shallow-clones) | Yes, when the merge base is in the history | `start` fails when the merge base is missing |
| [Partial clone](#partial-clones) | Yes, with a reachable promisor remote | Commands fetch missing blobs over the network |
| [Sparse checkout](#sparse-checkouts) | Review: yes. Lint and test: on the sparse set only. Docs: partly | The docs stage sees only the files in the sparse set |
| [Submodules](#submodules) | Yes, as pointers | Submodule contents are not reviewed |
| [Git LFS](#git-lfs) | Yes, as pointers | Review sees pointer files; the LFS hook needs composing |
| [Monorepo or very large tree](#monorepos-and-very-large-trees) | Yes | Latency grows with the tree; large diffs need `[diff] exclude` |
| [SHA-256 object format](#sha-256-object-format) | No | No commit can get an attestation |
| [Linked worktrees and bare repositories](#linked-worktrees-and-bare-repositories) | Yes, from a worktree | Commands such as `status`, `init`, and `start` do not run in the bare directory itself |

## Shallow clones

**Verified:**

- In a `--depth 1` clone where the branch starts at the fetched base tip, a run completes. `gate`, `push`, and the pre-push hook succeed.
- When the merge base of the branch and the base is not in the shallow history, `start` exits with code 3.
  The error code is `empty_diff`, and the message starts with `cannot find a merge base with 'main'`.
  `next` is `null`, so the envelope suggests no fix.
  The error code and the missing suggestion are a known issue (pending): [issue #392](https://github.com/elanthus/agentic-preflight/issues/392).
- After `git fetch --unshallow`, `start` succeeds.

**What helps:** fetch the missing history, then start again.
`git fetch --unshallow` fetches all of it.
To fetch less, deepen in steps with `git fetch --deepen=<n>` until `git merge-base <base> HEAD` prints a commit (unverified).
In CI, check out with full history, as the [attestation workflow example](attestations-and-ci.md) does with `fetch-depth: 0`.

**Unverified:** `start` fetches the base branch from `origin` before it synchronizes.
In a shallow clone, that fetch does not deepen the history below the commits it fetches.

## Partial clones

These statements cover a blobless clone (`--filter=blob:none`).
Treeless clones (`--filter=tree:0`) are not tested.

**Verified:**

- In `in_place` mode, on a branch that does not change `.agentic-preflight.toml` and with no earlier run on the branch, `start` fetches no blob.
  It compares trees, which a blobless clone has, and reads the configuration from the checkout.
- `context` needs the merge-base version of each changed file.
  Git fetches the missing blobs from the promisor remote while the tool runs `git diff`.
- When the promisor remote is unreachable, `context` exits with code 1 and error code `git_error`.
  The message is Git's error for the failed `git diff`.

**Unverified:**

- `submit-findings`, `stage run`, `mergeback`, `verify`, and the pre-push hook rebuild the same review diff.
  After the first `context`, the blobs are local, so these commands fetch nothing more in that clone.
  In another partial clone, such as a CI job, `verify` fetches them again.
- Grounding reads files from the merge-base commit, so it can fetch blobs too.
  It also reads the merge-base copy of `.agentic-preflight.toml` with `git show`.
  When the branch changes that file, the merge-base copy may be missing, and the read fetches it.
  `start` can make this read when an earlier run on the branch supplies evidence that it checks for reuse.
- An isolated validation worktree checks out the full head tree, which can fetch blobs that the source checkout does not have.
- These fetches happen inside Git commands that the tool runs with no timeout.
  A remote that stalls, or a credential prompt, stops the command until it is interrupted.
  The tool's own `fetch`, `ls-remote`, and `push` have a timeout (`[general] network_timeout_seconds`) and cannot prompt. The fetches that Git makes on its own inside other commands have neither.

**What helps:** keep the promisor remote reachable during a run.
Use a full clone, or a clone without a filter, where the network is unreliable.

## Sparse checkouts

**Verified:**

- The review diff is built from commits, not from the checkout.
  It includes changes to files outside the sparse set.
- The documentation surface lists only documentation files that are committed at the head commit and present in the checkout.
  A documentation file outside the sparse set is not on the surface, even when the branch changes it.
  A docs finding against it is therefore rejected.
  This is a known issue (pending): [issue #393](https://github.com/elanthus/agentic-preflight/issues/393).
- A `strict` validation worktree has the same sparse set as the source checkout, because `git worktree add` copies it (Git 2.38 or newer, the supported minimum).
  Stage commands run in that checkout: a lint command there sees the files in the sparse set and not the others.
  The test command runs in the same checkout (unverified).
  The `reusable` mode is not tested.
- The docs fingerprint's tree identities are the same in a sparse and a full checkout of one commit.
  Its `doc_surface_sha256` reads the checkout, so it differs. This is part of [issue #393](https://github.com/elanthus/agentic-preflight/issues/393).

**Unverified:** because `doc_surface_sha256` differs, docs evidence from a sparse checkout is not reused by a run in a full checkout of the same commits, and the reverse.

**What helps:** add the documentation directories to the sparse set with `git sparse-checkout add <dir>`, or run from a full checkout.

## Submodules

**Verified:**

- A change to a submodule pointer is one review unit, with the submodule's path.
  The diff shows the old and new `Subproject commit` lines.
  It always has this short form: since [pull request #366](https://github.com/elanthus/agentic-preflight/pull/366), the review diff passes `--submodule=short --ignore-submodules=none`, so the test's `diff.submodule = log` and `submodule.<name>.ignore = all` settings do not change it.
  The changes inside the submodule are not in the diff, so the review does not cover them.
- The documentation surface does not list files inside a submodule.
- A modified or untracked file inside a submodule makes `start` exit with code 3 and error code `dirty_tree`.
- A `strict` validation worktree has an empty submodule directory.
  With `[worktree] setup_command = "git submodule update --init"`, the setup step populates it before review and the stages.
  The test's submodule has a local path, so its command also passes `-c protocol.file.allow=always`, which Git requires for local submodule paths.

**Unverified:**

- In `in_place` mode, the stages use the submodules as they are checked out in your checkout.

**What helps:** review a submodule's own changes in its repository.
Add `git submodule update --init --recursive` to `[worktree] setup_command` when the stages need the submodule contents in an isolated worktree.

## Git LFS

The tests for this section are skipped when `git lfs` is not installed.

**Verified:**

- The review diff shows the LFS pointer text (`version`, `oid sha256:`, and `size`), not the file content.
- A `strict` validation worktree runs the LFS smudge filter when it checks out the branch.
  The file in it holds the real content, read from the local LFS store.
- `git lfs install` writes a pre-push hook.
  `init` then exits with code 3 and error code `hook_exists`, and leaves that hook unchanged.
- The envelope's `next.command` is `agentic-preflight init --force`.
  That command replaces the LFS hook, and pushes then stop uploading LFS objects.
  Do not run it in an LFS repository. This suggestion is a known issue (pending).
- The combined hook below blocks a push that has no attestation, and that blocked push uploads no LFS object.
  With a green run, `push` succeeds and the LFS object reaches the remote's LFS store.
  `status` reports the hook as active, and reports it as inactive after `git lfs update --force`.

The combined hook runs `agentic-preflight hook-check` first, so that a blocked push sends nothing, and runs `git lfs pre-push` only after the gate passes.
Both commands read the list of refs that Git writes on standard input, so each one gets its own copy.
Like the hook that `init` installs, it warns and allows the push when `agentic-preflight` is not on `PATH`:

```sh
#!/bin/sh
# Run the gate first so that a blocked push sends nothing, then let Git LFS
# upload objects. Both commands read the ref list on standard input, so each
# gets its own copy.
updates=$(cat)
if command -v agentic-preflight >/dev/null 2>&1; then
  printf '%s\n' "$updates" | AGENTIC_PREFLIGHT_PUSH_REMOTE="$1" agentic-preflight hook-check || exit $?
else
  echo "agentic-preflight: not found on PATH, skipping the quality gate (warn only)" >&2
fi
command -v git-lfs >/dev/null 2>&1 || { echo "git-lfs was not found on your path" >&2; exit 2; }
printf '%s\n' "$updates" | git lfs pre-push "$@"
```

Put it at the path that `init` reports, and make it executable.
A later `git lfs install --force` or `git lfs update --force` replaces the combined hook, and `status` then reports the hook as inactive; combine it again after either.
See [Existing hooks](pre-push-hook.md#existing-hooks).

**Unverified:**

- Each new isolated worktree writes a full copy of every LFS file at its head commit, so `strict` mode pays that copy on every run.
  The LFS store under the Git common directory is shared by linked worktrees, so an object that is already local is not downloaded again.
- An object that is not local, for example after a clone with `GIT_LFS_SKIP_SMUDGE=1`, is downloaded from the LFS server during the checkout, with no timeout.
- `stage run --baseline` makes a full scratch checkout of the base in every mode, `in_place` included, so each baseline check smudges every LFS file at the base.

## Monorepos and very large trees

**Verified,** with a synthetic tree of 400 files:

- Over `[diff] max_bytes`, `context` exits with code 2 and `data.mode` is `diff_too_large`.
  In this test the read stops early, so `data.complete` is `false`: `data.by_file` gives lower-bound sizes for the files read so far, and `data.unmeasured_files` lists the rest.
  Together they name every generated file.
- With the generated directory in `[diff] exclude`, a new run reviews only the other files and lists the excluded ones in `excluded_files`.

**Not verified here: latency.**
Measurements and fixes in the performance issues:

- [Issue #358](https://github.com/elanthus/agentic-preflight/issues/358), fixed: on a tree with 300,000 files, `status`, `submit-findings`, and `stage run` each took 4.5 to 6.5 seconds.
  The documentation surface and grounding now list only the paths their patterns can match.
  A pattern that can match at any depth, such as `**/*.md`, still lists the whole tree.
  See [the documentation surface](configuration.md#the-documentation-surface-docs).
- [Issue #359](https://github.com/elanthus/agentic-preflight/issues/359), fixed: `hook-check` took about 1.2 seconds for one ref.
  It now runs each Git lookup of a commit once and imports only its own command, and takes about 0.6 seconds.
  Verification still rebuilds the review manifests, so the time still grows with the diff.
- [Issue #356](https://github.com/elanthus/agentic-preflight/issues/356), fixed: `context` now stops reading the diff at four times `[diff] max_bytes`.

**Settings that help:**

- `[diff] exclude` for generated, vendored, and lock files.
  Setting it replaces the built-in globs, so list the defaults you still want.
  See the [configuration reference](configuration.md).
- `[diff] max_bytes` only when the change really is that large. The diff is never truncated.
- `[worktree] mode = "in_place"` or `"reusable"`, so that a run does not check out the whole tree again.
  `strict` mode creates a fresh worktree for every run. See [worktree modes](worktree-modes.md).
  In every mode, each `stage run --baseline` check pays one full scratch checkout of the base.

## SHA-256 object format

Repositories that use the SHA-256 object format are not supported.
Run records and attestation notes hold only SHA-1 object IDs, so no commit in such a repository can have a valid attestation.

**Verified:** `start` and `context` succeed, and `submit-findings` exits with code 1 and error code `internal_error`.

See [issue #394](https://github.com/elanthus/agentic-preflight/issues/394) and [COMPATIBILITY.md](../COMPATIBILITY.md#repository-shapes).

## Linked worktrees and bare repositories

**Verified:**

- Runs belong to the source worktree. Two linked worktrees of one clone run independently.
  `tests/test_worktree_ownership.py::test_two_linked_worktrees_can_run_gates_independently` checks this.
- With a bare common directory and a linked worktree:
  - `init` in the worktree installs the hook at `<bare>/hooks/pre-push`, which every worktree of the clone uses.
  - Run records are under `<bare>/agentic-preflight/`.
  - A run reaches `VERIFIED`.
- In the bare directory itself, `status`, `init`, and `start` exit with code 1 and error code `git_error`.
  The message includes Git's `this operation must be run in a work tree`.

**What helps:** run every command from a linked worktree, for example one made with `git worktree add <path> <branch>`.
