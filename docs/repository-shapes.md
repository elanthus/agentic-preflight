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
| [Sparse checkout](#sparse-checkouts) | Review, lint, and test: yes. Docs: partly | The docs stage sees only the files in the sparse set |
| [Submodules](#submodules) | Yes, as pointers | Submodule contents are not reviewed |
| [Git LFS](#git-lfs) | Yes, as pointers | Review sees pointer files; the LFS hook needs composing |
| [Monorepo or very large tree](#monorepos-and-very-large-trees) | Yes | Latency grows with the tree; large diffs need `[diff] exclude` |
| [SHA-256 object format](#sha-256-object-format) | No | No commit can get an attestation |
| [Linked worktrees and bare repositories](#linked-worktrees-and-bare-repositories) | Yes, from a worktree | Commands do not run in the bare directory itself |

## Shallow clones

**Verified:**

- In a `--depth 1` clone where the branch starts at the fetched base tip, a run completes. `gate`, `push`, and the pre-push hook succeed.
- When the merge base of the branch and the base is not in the shallow history, `start` exits with code 3.
  The error code is `empty_diff`, and the message starts with `cannot find a merge base with 'main'`.
  `next` is `null`, so the envelope suggests no fix.
  The error code and the missing suggestion are a known issue (pending).
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

- In `in_place` mode, `start` fetches no blob: it compares trees, which a blobless clone has.
- `context` needs the merge-base version of each changed file.
  Git fetches the missing blobs from the promisor remote while the tool runs `git diff`.
- When the promisor remote is unreachable, `context` exits with code 1 and error code `git_error`.
  The message is Git's error for the failed `git diff`.

**Unverified:**

- `submit-findings`, `stage run`, `mergeback`, `verify`, and the pre-push hook rebuild the same review diff.
  After the first `context`, the blobs are local, so these commands fetch nothing more in that clone.
  In another partial clone, such as a CI job, `verify` fetches them again.
- Grounding reads files from the merge-base commit, so it can fetch blobs too.
- An isolated validation worktree checks out the full head tree, which can fetch blobs that the source checkout does not have.
- These fetches happen inside Git commands that the tool runs with no timeout.
  A remote that stalls, or a credential prompt, stops the command until it is interrupted.
  [Issue #348](https://github.com/elanthus/agentic-preflight/issues/348) proposes timeouts for the tool's `fetch` and `push`. It does not name the fetches that Git makes on its own inside other commands.

**What helps:** keep the promisor remote reachable during a run.
Use a full clone, or a clone without a filter, where the network is unreliable.

## Sparse checkouts

**Verified:**

- The review diff is built from commits, not from the checkout.
  It includes changes to files outside the sparse set.
- The documentation surface lists only documentation files that are present in the checkout.
  A documentation file outside the sparse set is not on the surface, even when the branch changes it.
  A docs finding against it is therefore rejected.
  This is a known issue (pending).
- A `strict` validation worktree has the same sparse set as the source checkout, because `git worktree add` copies it.
  Lint and test commands run in that checkout, so they see only the files in the sparse set.
  The `reusable` mode is not tested.
- The docs fingerprint's tree identities are the same in a sparse and a full checkout of one commit.
  Its `doc_surface_sha256` reads the checkout, so it differs.

**Unverified:** because `doc_surface_sha256` differs, docs evidence from a sparse checkout is not reused by a run in a full checkout of the same commits, and the reverse.

**What helps:** add the documentation directories to the sparse set with `git sparse-checkout add <dir>`, or run from a full checkout.

## Submodules

**Verified:**

- A change to a submodule pointer is one review unit, with the submodule's path.
  The diff shows the old and new `Subproject commit` lines.
  The changes inside the submodule are not in the diff, so the review does not cover them.
- The documentation surface does not list files inside a submodule.
- A modified or untracked file inside a submodule makes `start` exit with code 3 and error code `dirty_tree`.
- A `strict` validation worktree has an empty submodule directory.
  With `[worktree] setup_command = "git submodule update --init"`, the setup step populates it before review and the stages.
  The test's submodule has a local path, so its command also passes `-c protocol.file.allow=always`, which Git requires for local submodule paths.

**Unverified:**

- In `in_place` mode, the stages use the submodules as they are checked out in your checkout.
- The text of a submodule diff depends on Git's `diff.submodule` setting.
  [Pull request #366](https://github.com/elanthus/agentic-preflight/pull/366) proposes building review patches independently of Git configuration.

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
- The composed hook below blocks a push that has no attestation.
  With a green run, `push` succeeds and the LFS object reaches the remote's LFS store.
  `status` reports the hook as active.

Both commands read the list of refs that Git writes on standard input, so each one needs its own copy:

```sh
#!/bin/sh
# Both commands read the ref list on standard input, so each gets a copy.
updates=$(cat)
printf '%s\n' "$updates" | git lfs pre-push "$@" || exit $?
printf '%s\n' "$updates" | AGENTIC_PREFLIGHT_PUSH_REMOTE="$1" agentic-preflight hook-check
```

Put it at the path that `init` reports, and make it executable.
See [Existing hooks](pre-push-hook.md#existing-hooks).

**Unverified:**

- Each new isolated worktree writes a full copy of every LFS file at its head commit, so `strict` mode pays that copy on every run.
  The LFS store under the Git common directory is shared by linked worktrees, so an object that is already local is not downloaded again.
- An object that is not local, for example after a clone with `GIT_LFS_SKIP_SMUDGE=1`, is downloaded from the LFS server during the checkout, with no timeout.

## Monorepos and very large trees

**Verified,** with a synthetic tree of 400 files:

- Over `[diff] max_bytes`, `context` exits with code 2, `data.mode` is `diff_too_large`, and `data.by_file` lists each file's size.
- With the generated directory in `[diff] exclude`, a new run reviews only the other files and lists the excluded ones in `excluded_files`.

**Not verified here: latency.**
Measurements in the open performance issues:

- [Issue #358](https://github.com/elanthus/agentic-preflight/issues/358): on a tree with 300,000 files, `status`, `submit-findings`, and `stage run` each took 4.5 to 6.5 seconds.
  The documentation surface and grounding each list the whole tree.
- [Issue #359](https://github.com/elanthus/agentic-preflight/issues/359): `hook-check` took about 1.3 seconds for one ref with a one-file diff.
- [Issue #356](https://github.com/elanthus/agentic-preflight/issues/356): the whole diff is read before the `[diff] max_bytes` check.

**Settings that help:**

- `[diff] exclude` for generated, vendored, and lock files.
  Setting it replaces the built-in globs, so list the defaults you still want.
  See the [configuration reference](configuration.md).
- `[diff] max_bytes` only when the change really is that large. The diff is never truncated.
- `[worktree] mode = "in_place"` or `"reusable"`, so that a run does not check out the whole tree again.
  `strict` mode creates a fresh worktree for every run. See [worktree modes](worktree-modes.md).

## SHA-256 object format

Repositories that use the SHA-256 object format are not supported.
Run records and attestation notes hold only SHA-1 object IDs, so no commit in such a repository can have a valid attestation.

**Verified:** `start` and `context` succeed, and `submit-findings` exits with code 1 and error code `internal_error`.

See [issue #351](https://github.com/elanthus/agentic-preflight/issues/351) and [COMPATIBILITY.md](../COMPATIBILITY.md#repository-shapes).

## Linked worktrees and bare repositories

**Verified:**

- Runs belong to the source worktree. Two linked worktrees of one clone run independently.
  `tests/test_worktree_ownership.py::test_two_linked_worktrees_can_run_gates_independently` checks this.
- With a bare common directory and a linked worktree:
  - `init` in the worktree installs the hook at `<bare>/hooks/pre-push`, which every worktree of the clone uses.
  - Run records are under `<bare>/agentic-preflight/`.
  - A run reaches `VERIFIED`.
- In the bare directory itself, commands exit with code 1 and error code `git_error`.
  The message includes Git's `this operation must be run in a work tree`.

**What helps:** run every command from a linked worktree, for example one made with `git worktree add <path> <branch>`.
