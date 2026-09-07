# Changelog

All notable changes to the `codebase-kg` plugin.

## [0.5.2] — 2026-09-06 — the gate reads the command, not just the word `grep`

### Fixed — a shell search is scoped to this repo, and only when it is a search

The gate resolves the repo once, from the session, so it had no way to tell what
a shell command was actually doing. `Grep` and `Glob` answer three questions
through `tool_input["path"]` — which repo, reading what, and is the file already
named — and none of them reached a command string. Everything below was denied:

- `cd other-repo && grep -rn x .` — another repo, which this graph cannot answer
  for. The worst of the four: the gate refused a search using a map of somewhere
  else.
- `cat f | grep x` — reads a pipe, never the tree.
- `grep -m1 version pyproject.toml` — names one file, so the question the gate
  asks is already answered.
- `gh pr merge && ... && grep x f.json` — a `PreToolUse` hook allows or denies
  the whole call, so one incidental clause took an unrelated merge down with it.

One rule now covers all four, and it is the rule `Grep` already followed: gate a
search aimed at the mapped tree that has not already located its file. It is
strictly a narrowing, so a real repo-wide search is still denied.

Two bugs found while testing it, both of which would have made the fix worse
than the problem:

- `shlex.split(posix=True)` reads a backslash as an escape, turning
  `C:\Users\me\repo` into `C:Usersmerepo` — a path resolving nowhere, so a
  search of another drive read as a search of this one. Splitting with
  `posix=False` and stripping quotes by hand keeps Windows paths intact.
- `find <path> -name x` collects predicates after its paths, so `-name`'s own
  value was counted as a path. It does not exist, a missing path reads as "still
  hunting", and the located-search exemption never applied.

## [0.5.1] — 2026-09-06 — one slash entry per feature

### Changed — the thin commands are gone; each feature is just its skill

Every feature shipped twice in the slash menu: `/codebase-kg:refresh` (a command)
and `/codebase-kg:kg-refresh` (the skill it immediately delegated to). Thirteen
entries for seven features.

There is no way to hide either one. Skill frontmatter carries `name`,
`description`, `allowed-tools` and `version` and nothing for visibility, and
auto-discovery lists everything it finds — so the only control is not shipping
the duplicate.

The six thin commands are deleted and their skills renamed `kg-refresh` →
`refresh`, `kg-build` → `build`, and so on. The slash name comes from the
skill's *directory*, so the short names survive the deletion, and every existing
`/codebase-kg:refresh` reference — including the ones the hooks print — still
resolves. Nothing was lost with the commands: their scoping already lived in the
skills, and a skill auto-triggers on its description where a command never did.

`setup` stays a command. It has no skill, being a fixed procedure with no
judgement in it, so it was never duplicated.

The one real cost is `argument-hint`, the menu affordance that showed what a
command takes. Skills have no equivalent; arguments still pass.

`test_plugin_surface.py` now asserts the invariant rather than the old pairing:
no name appears as both a skill and a command, a skill's directory matches its
frontmatter `name`, and every `/codebase-kg:x` reference in any document
resolves to something that exists.

## [0.5.0] — 2026-09-06 — the gate keeps asking, and the digest means something

### Changed — the search gate keeps asking instead of standing down

0.4.0's gate denied one search per session and then stood aside. An agent paid
that toll once and grepped freely for the rest of the turn, which is most of a
turn, so the gate was a formality rather than a habit.

It now denies every search aimed at mapped code, and there is no force flag: a
PreToolUse hook cannot add an argument to `Grep`, and a self-declared override
is a rubber stamp an agent learns to always pass. Three ways through, each
inferred from what the agent actually did:

- **A query buys credit** — one search per distinct file the answer named, plus
  `gate_credit` as a buffer (default 3, per-repo). The grant is `max`, not `+`,
  so five cheap answers naming nothing are worth 3 rather than 15, and a small
  answer never lowers an allowance a bigger one already earned.
- **A located search is free** — a search already scoped to a file the graph
  anchors is never gated and spends nothing. A directory still is; that is where
  you look when you do not know the file.
- **Repeating insists** — the identical search after a denial always passes.
  That is the escape hatch for unmapped code, and it is what makes the gate
  unable to strand anyone.

The anchor count is uncapped on purpose: it is not a guess the hook is making,
it is how many files the graph itself just named.

### Fixed — the source digest was unusable on Windows

The builder hashes the working tree; the git hooks hash the blob. On a repo with
`text=auto eol=lf` checked out on Windows those are never the same bytes, so
every mapped text file compared unequal and `changed_since_built` fired on all
of them. The signal carried no information at all — which is worse than the
`A`/`D`-only check it replaced, because that one at least stayed quiet.

`writer.content_sha` folds CRLF to LF before hashing, and the vendored hook
carries the same rule beside its copy of the coverage block. A line ending is
not something a description can be wrong about.

This invalidates every baseline written by 0.4.0: the first refresh after this
change re-hashes them, and until then the affected files report as drifted.

The unit tests could not have caught it. They wrote LF fixtures, where the two
readers agree by accident. `test_drift_check.py` now builds a repo that stores
LF and checks out CRLF, and `test_hook_parity.py` holds the two copies of the
digest rule together the way it already holds the coverage block.

### Fixed — `kg_stats.cli` was wrong in the only configuration that ships

`cli_invocations` resolved the package root by looking for a `pyproject.toml`
beside the package. That is true in a source checkout and false under
`uvx --from <plugin>/mcp`, where the package lives in a venv — so the field added
to give skills a runnable command handed them a bare `codebase-kg-build`, which
is on no skill's PATH. The test only ever exercised the checkout.

`package_root()` now tries three things in order: the repo checkout, the
directory the distribution records in PEP 610 `direct_url.json` (what `uvx --from`
leaves behind), and `CLAUDE_PLUGIN_ROOT`. Verified against a real uvx install
from a copied plugin cache, not just the checkout.

### Fixed — `setup.md` step 7 prescribed an attribute that breaks multi-graph repos

It said to add `*.db binary diff=codegraph`. Git applies the last matching
pattern, so in a repo that already routes `*_driver_graph.db` or
`*cartographer_graph*.db` to their own exporters, that line captures them too and
renders them through the wrong one — an error on every `git show`, not a diff.
Step 7 now reads `git check-attr` first, leaves correct wiring alone, and adds
only the specific path when other `.db` files exist.

### Fixed — the export CLI required the repo root as its cwd

`export.py` defaulted to a relative `knowledge/code_graph.db` with no parent
search while the MCP server had always walked up, so every skill instruction that
runs the exporter carried an unwritten "from the repo root" precondition. The
walk-up lives in `store.discover_graph` and is shared rather than copied. An
explicitly named path is still taken as given.

### Changed — `commands/refresh.md` stops restating the skill

The two write paths were explained in the command, the skill, `skills/README.md`
and `SCHEMA.md` §7.1. The command now points at the skill, which is what "thin →
skills" meant.

### Fixed — the last four pyright errors

`test_links.py` called `.links` on `CodeGraph.node`'s `Node | None`. A `node_of`
helper asserts the node exists, so a missing one fails as a named assertion
rather than an `AttributeError`.

## [0.4.0] — 2026-09-06 — the graph gets consulted first, and one command wires the repo

### Added — a PreToolUse search gate

`hooks/kg_search_gate.py`. The graph was complete, indexed, committed, and unused: nothing made an
agent reach for it, so `Grep` re-derived the map every session — slower than the graph, and blind to
the components a search string does not appear in. Documentation did not fix this, because an
instruction that competes with a habit loses.

The gate denies the **first** `Grep`, `Glob`, or shell `grep`/`rg`/`fd`/`find -name` of a session,
with the instruction to query `kg_search` / `kg_node` / `kg_neighborhood` first, then stands down for
the rest of that session — complied with or not. Any codebase-kg MCP call stands it down too, so an
agent that already started at the graph never sees it.

Three properties, in this order:

- **It cannot strand an agent.** One interruption per session, spent before the message is emitted so
  a failure between the two cannot re-gate the next search. A malformed payload, an unreadable graph,
  or an unwritable state file all fail *open*. No search is permanently blocked: if the graph does
  not cover it, run it again.
- **It fires on the search, not the neighbors.** Shell detection is deliberately narrow — a false
  positive denies unrelated work — so `find` and `Get-ChildItem` count only with a name/path filter,
  and an ordinary `find . -type d` does not.
- **It stays out of repos it has no business in.** No graph, `SKIP_KG` set, `search_gate: off`, or a
  search scoped outside `root` or into an ignored dir, and it is silent.

`search_gate` (`block` | `warn` | `off`) and `gate_shell_search` join the per-dev
`.claude/codebase-kg.local.md`. `search_gate: off` arrives at the hook as a bool, because the
frontmatter parser coerces `off` — handled, since the alternative is a setting that reads as unknown
and silently stays on.

Ships with the plugin. There is nothing to install per repo.

### Added — `kg-query`, the skill the search gate hands off to

The gate denies a search and instructs the agent to run `kg_search`, then `kg_node` /
`kg_neighborhood`, then read the anchored files. No skill owned that workflow, no description
triggered on "where does the feed ranking live", and `kg_neighborhood` and `kg_find_by_link` were
registered, tested, documented in the README, and named in **no** skill's `allowed-tools` — so the
gate interrupted an agent and handed it to nothing.

`/codebase-kg:query` and `skills/kg-query` are that handoff: locate through the index, expand through
the neighborhood, confirm in the source, and report any stale map you crossed on the way. Read-only
by construction — it has no write tools.

### Fixed — the CLIs the skills instruct are now runnable outside the plugin

Every skill's build step said `python -m codebase_kg.build`. That works inside the plugin's own
checkout and nowhere else: a target repo has the plugin but no importable `codebase_kg`, so the
instruction was a `ModuleNotFoundError` at the moment a skill had finished its real work. The
`uvx --from <plugin>/mcp` form would have worked, but a skill cannot build it — `CLAUDE_PLUGIN_ROOT`
is not set in the shell a skill's Bash runs in, and the console scripts are not on PATH.

`codebase-kg-build` and `codebase-kg-migrate` join `codebase-kg-export` as entry points, and
`kg_stats` now reports a `cli` field carrying the invocation that works *in this repo* — the
`uvx --from` form from a source checkout, the bare console script from a wheel install. The server is
the one component that knows where it was loaded from, so it answers rather than the caller guessing.
The skills read it before running anything.

### Fixed — the staleness checks read status `M`, and the digests they never opened

`analyze()` split a change set into `A` (source nothing anchors) and `D` (anchored source that is
gone) and dropped everything else. A real change set is mostly `M`, so both hooks were silent
through the drift that actually accumulates. On one repo that let a graph fall 48 commits behind:
the check would have named the 27 new files and said nothing about the 47 modified ones — two
thirds of the drift. The `source` table of SHA-256 digests, written on every build and documented in
SCHEMA.md §6.3 as the thing that makes a green `kg_validate` mean something, was read by neither
hook.

Two changes, one bucket each:

- **A modified file that no node covers is now reported.** Keying the gap bucket on `A` meant a file
  that predates the graph was invisible forever — it is never "added" again, so it was never
  mentioned again.
- **A modified file that *is* mapped is compared against its recorded digest.** A mismatch is the
  `changed_since_built` signal, scoped to the change set: the anchor still resolves, so nothing else
  in the toolchain notices, but the description may no longer fit.

Digests come out of git, never off disk — the index (`:path`) at commit time, the pushed tips at
push time, one `git cat-file --batch` for the whole set. Reading the working tree would have
compared a push of a branch that is not checked out against whatever happened to be on disk. A file
with no baseline is not reported: absent evidence reads as "no baseline", never as "unchanged".

`read_graph` and `analyze` now return `NamedTuple`s (`Graph`, `Findings`) rather than bare tuples,
so the next field is an addition instead of a break.

### Changed — `/codebase-kg:setup` replaces `install-hooks` and `setup-diff`

Two commands to wire one repo left the textconv driver reading as optional, and it is not: without it
`git diff` says "Binary files differ" and a reviewer takes the commit message on faith. One
idempotent command now does the git hooks, the `.gitattributes` attribute, and the textconv driver,
and is safe to re-run to repair the wiring.

## [0.3.0] — 2026-09-06 — write tools, and hooks that actually fire

### Added — write tools, so a one-field fix is not a whole rebuild

Four MCP tools: `kg_upsert_node`, `kg_delete_node`, `kg_add_link`, `kg_remove_link`. Nine read-only
tools became thirteen.

An agent cannot edit SQLite, so every change went through `export → edit the JSON → build`. That
round trip is genuinely good for bulk work and absurd overhead for "change one node's description" —
and it left a scratch JSON in the repo root that somebody had to remember to delete. The absence of
write tools was the gap; the scratch file was a workaround for it.

**The round trip stays.** It is still the path for a parity sweep, a restructuring, or anything where
reading the diff before applying it is the point. Every tool description says which is which, and the
eleven docs describing the loop now say so too.

Three properties, all in the new `edits.py`:

- **Atomic.** The mutation runs against a private copy of the file inside one transaction, and the
  copy replaces the original only at the end — the same mechanism `writer.build` already used. A
  CHECK violated by the third of five nodes leaves the committed graph byte-identical: not rolled
  back, *never opened for writing*. It also keeps `CodeGraph` strictly read-only, so a bug in an
  edit still cannot mutate the artifact.
- **Validated, not merely constrained.** The CHECKs reject a malformed row; they cannot see that a
  delete stranded the counterpart the peer graph links back to. `kg_validate` — the same function
  the tool calls — runs against the copy, and the write is refused if it introduced a finding the
  graph did not already have. The test is *no new findings*, never *clean*: a real graph carries
  findings, and demanding zero would lock the tools out of the graphs that most need editing.
- **Reported.** Every call returns the rows it touched, field by field, before and after. That is
  the diff review the export path gave for free, and a tool that mutated silently would remove it.

A delete says what it takes before it takes it. `dry_run` defaults to true and lists the anchors,
outbound edges and external links that cascade, plus the inbound edges that `ON DELETE RESTRICT`
blocks it on — those need an explicit `cascade_inbound`.

`writer._fts_text` and `writer._replace` became public (`fts_text`, `replace_file`) so the edit path
and the build path cannot drift on how a node is indexed or how a new file lands. `server._open_peer`
moved to `tools.open_peer`, because a write judged by a validation run that skipped the peer would
accept the one thing that check exists to catch.

### Added — the staleness check runs at commit, not only at push

`pre-push` compares against the upstream branch. Once you have pushed, that range is empty and the
check reports nothing — which is exactly when someone thinks to look at it. A repo can drift a whole
session's work past the graph and be told nothing at any point.

`pre-commit` asks the same question of the staged change set. Staged-against-HEAD is a comparison
that is always available, and the commit needing the graph update is still the one in front of you.

`kg_pre_commit.py` imports the coverage rule from `kg_pre_push.py` rather than repeating it, so there
is one implementation to keep in step with `codebase_kg/coverage.py`.

**Neither hook refreshes, and neither can.** `/codebase-kg:refresh` maps changed files to nodes,
hands a JSON diff to a person to read, and decides what to add, edit or remove. A shell hook has no
way to make those calls, and one that wrote its own guess into the graph would be manufacturing
knowledge rather than recording it. The hooks say the graph needs attention; a person refreshes it.

Advisory and never blocking, like the hook beside it — a gate that stops a commit gets bypassed with
`--no-verify` and then ignored. On by default, silenced with `SKIP_KG=1` when a change deliberately
outruns the graph.

### Fixed — `root: "."` silenced both hooks while the graph validated clean

A graph whose `meta.root` was `.` rather than the empty string disabled the post-edit nudge and the
pre-push staleness check completely. Both guard on `rel == root or rel.startswith(root + "/")`, and
`.` is truthy while being a prefix of no repo-relative path — so every file was discarded before any
check ran. Installed, executable, correctly wired, and mute.

It hid because the two surfaces treat `root` differently. The package **joins** it (`repo / "."` is
`repo`), so `kg_validate`, `kg_stats` and coverage all reported the graph healthy; the hooks
**compare** it as a string prefix, where the same value matches nothing. A real repo ran this way
with 198 anchored files and no hook output at all.

`Meta.__post_init__` now folds `.` to `""` on construction, so no newly built graph can carry it, and
a rebuild repairs an existing one. Both vendored copies gained an identical `norm_root()` for the
graphs already committed, with parity tests asserting the two do not drift — the same guarantee
`IGNORE_DIRS` has.

## [0.2.3] — 2026-08-01 — the untested surfaces, tested

Coverage across everything shipped: **89% → 92%**, 304 → 394 tests. The headline number moved a
little; what moved a lot is where the coverage is.

| | before | after |
|---|---|---|
| `hooks/kg_post_edit_check.py` | **0%** — never imported | 96% |
| `hooks/_config.py` | 64% | 99% |
| `codebase_kg/server.py` | 82% | 95% |

The post-edit hook runs after every `Edit`/`Write`/`MultiEdit` in every repo that installs the
plugin, and no test had ever imported it — `coverage.py` reported it as `never imported` while the
package around it sat at 91%. The most-run code in the plugin was the least tested.

### Fixed — one typo in `.local.md` silently disabled the hook forever

Found by the new tests. `nudge_every` goes straight into `int()`, and `main` swallows every
exception so an advisory hook can never break an edit. Together those meant `nudge_every: evry`
raised, got swallowed, and the hook went permanently silent for that repo — no nudge, no error, no
way to notice. That is the same invisible-disable the frontmatter comment-strip exists to prevent.

`_as_int` now falls back instead of raising, for both the config value and the on-disk counter. A
typo costs the setting, not the feature. Falsy still means "unset" — the fix is to the crash, not to
the semantics.

### `/codebase-kg:setup`, tested through real git

The exporter had unit tests and still shipped broken for the one use that matters, because nothing
ran it the way git does: git spawns the textconv command itself, with no shell in between, and reads
its stdout as bytes. `test_textconv.py` configures a genuine repo exactly as the command documents,
then asserts on what `git diff`, `git show` and `git log -p` actually print — including under
`PYTHONIOENCODING=cp1252`, which is the reported failure reproduced on any platform.

Two things it pins that are easy to get wrong:

- `git show <rev>:<path>` does **not** go through textconv — it is a blob dump. Asserting on it looks
  like it works, because descriptions are UTF-8 text inside the SQLite file and a substring check
  passes against the raw bytes while proving nothing.
- Without the driver, git says `Binary files … differ`. That baseline is asserted too, so the
  rendering tests cannot pass for the wrong reason.

### Also

- Every MCP tool is now invoked through `mcp.call_tool`, the surface an agent actually reaches.
  `test_server.py` proved the eight tools were *registered* and `test_tools.py` proved the query
  functions were correct; the two-line wrapper joining them was uncovered on all eight, and it is
  the only place a swapped argument or a missing `_open_peer` could live.
- `kg_search` returns `results` while `kg_find_by_kind` returns `nodes`. Pinned as a test rather
  than fixed — renaming either changes what every already-built agent reads.
- `hooks/_config.is_source_file` remains extension-based rather than `covers`-aware, now asserted
  explicitly so it reads as a decision rather than drift.

## [0.2.2] — 2026-08-01 — the declaration outranks the guess

Three defects found by using 0.2.1 on a real repo, all of the same shape: something the plugin
*assumed* quietly overruling something the repo *said*.

### Fixed — `covers` was outranked by a hardcoded directory deny-list

0.2.1 moved coverage from inferred to declared, and then let `IGNORE_DIRS` — `.git`, `.github`,
`.githooks`, `node_modules`, … — veto the declaration. A repo whose `covers` named `.githooks/*`,
with nodes anchored on those files, got no coverage credit for them at all:

- `tools.walk_sources` pruned the directory *before* `classify` ran, so those files were neither
  `covered` nor `gap`. They were absent — the fifth, invisible bucket that `coverage.py` opens by
  promising cannot exist. Measured on the reporting repo: `110 covered`, rising to `112` once the
  declaration was honoured.

  `out_of_scope` does **not** move, and the expectation that it would (`184 → 182`) misread the
  bug. `classify` computes it as `len(files) - len(in_scope)`; a pruned file is missing from both
  terms, so it was never counted as out-of-scope in the first place. Honouring the declaration adds
  it to `files` *and* to `in_scope`, leaving the difference unchanged. A drop there would have meant
  something else moved — the correct signature of this fix is `covered` rising alone.
- `kg_pre_push.is_source` checked `IGNORE_DIRS` one line above a docstring stating that a `covers`
  declaration "wins outright". It did not.

Both now consult the declaration first. `coverage.declared_roots()` extracts the directories a
pattern names literally, and the walk keeps those while still pruning everything else — so a repo
that declares `.githooks/*` gets those files counted without dragging `node_modules` back into
every walk. A pattern that opens with a wildcard (`**/*.py`) lifts no prune, by design.

`hooks/_config.is_source_file` (the post-edit nudge) is **not** covers-aware and still decides by
extension. It is a heuristic for when to suggest a refresh rather than a counted report, so the
stakes are lower — but it remains the one surface with its own notion of "source file".

### Fixed — `codebase-kg-export` crashed on any non-UTF-8 stdout

Descriptions routinely contain `—`, `…` and `→`. On Windows a piped stdout defaults to the ANSI
code page, which encodes none of them, so the exporter died with `UnicodeEncodeError` *after*
doing all its work. Git's textconv driver pipes exactly that stdout, which made
`/codebase-kg:setup` fail on every repo with a committed graph — and the only workaround was
prefixing `PYTHONIOENCODING=utf-8`, which git gives you nowhere to put.

New `cli.py`: `use_utf8()` on every CLI entry point, and `write_out()` for the exporter, which
writes UTF-8 bytes straight to `stdout.buffer` because for textconv the bytes *are* the product.
`build.py` had the same latent crash on its `… and N more` line and is fixed with it.

### Fixed — a schema mismatch gave the same advice in both directions

"Rebuild it with /codebase-kg:build" was correct for a graph *behind* the server and actively
harmful for one *ahead* of it — it sent you to regenerate a good file with a stale plugin, which
reproduces the mismatch and discards whatever the newer schema recorded. Older graphs are now
pointed at `python -m codebase_kg.upgrade`; newer ones at updating the plugin.

### Also

- `tools.IGNORE_DIRS` was a **third** copy of the list, guarded by nothing. `test_hook_parity.py`
  compared the other two and was written precisely because they had drifted; it now covers all
  three.
- Version bumped so a reinstall is observable. `/plugin` reinstalling an identical version reports
  success and changes nothing, which is indistinguishable from a fix that did not land.

## [0.2.1] — 2026-07-31 — coverage is declared, and drift is detectable

**Schema v3.** Existing graphs upgrade in place with `python -m codebase_kg.upgrade` — every node,
anchor and edge is preserved verbatim. A v2 file is refused by the store until it is upgraded,
rather than being read with two of its answers silently missing.

That is a breaking change carrying a patch version, deliberately: the plugin has a single user and
four repos, all of which upgrade with one command. Read the number as bookkeeping, not as a promise
that a v2 graph still loads — it does not. Once anyone else depends on this, a schema bump gets a
minor version.

### Fixed — a file type with no coverage was invisible, not merely uncovered

`uncovered_sources` derived the set of "source extensions" from the extensions the graph *already*
anchored. That is language-agnostic and it is **silent by construction**: a category with zero
coverage contributes zero extensions, so it was never examined and never reported.

Measured on the RPN calculator, where all 142 anchors were `.kt`:

| | files | uncovered | reported before? |
|---|---|---|---|
| `.kt` | 92 | 1 | ✅ |
| `.xml` | 34 | **34** | ❌ |
| `.kts` (Gradle) | 3 | **3** | ❌ |
| `.properties` | 5 | **5** | ❌ |
| `.toml`, `.pro` | 2 | **2** | ❌ |

`kg_validate` reported one missing file and returned otherwise clean, on a graph that described none
of the build configuration. No question you could ask it would have said so.

Coverage is now **declared**, via `meta.covers` / `meta.exempt` (glob patterns, gitignore-flavoured).
Every file under `root` lands in exactly one bucket — `covered`, `gap`, `exempt`, `out_of_scope` —
so there is no invisible state left. `exempt` records a deliberate decision, which previously looked
identical to an oversight.

A graph with no declaration keeps working and keeps reporting what it can, but says
`coverage.declared: false` with an explicit warning that the answer is incomplete. An incomplete
check that admits it is a different thing from one that looks clean.

The pre-push hook reads the same declaration out of the graph, so the three surfaces that had three
different notions of "source file" (inferred extensions, a hardcoded deny-list, and the post-edit
hook's own list) now agree.

### Added — source baselines, so a green check means something

The `source` table records a SHA-256 of each anchored file as it stood when the graph was built.
`kg_validate` reports `changed_since_built` when the working tree no longer matches.

This closes the gap the anchor check cannot: a refactor that keeps a class name and rewrites its
body passes "symbol still found" cleanly while making the description false. 142/142 anchors
resolving never meant the descriptions were accurate — now there is a signal that distinguishes them.

Two decisions keep it honest:

- **Drift does not set `ok: false`.** It means "go look", not "something is broken". Folding it in
  would fail every graph the moment anyone edited a covered file — the false-alarm failure mode of
  the date-based gate 0.2.0 removed.
- **A rebuild does not silently re-bless nodes nobody re-read.** Baselines survive the
  export → edit → build round trip, so untouched nodes keep flagging. `--rebaseline` is the explicit
  way to assert "I have re-checked these", and it shows up in review as a `sources` change.

### Added — readable diffs for the committed database

`.gitattributes` now marks the graph `diff=codegraph`, and `/codebase-kg:setup` configures a
textconv driver so `git diff` / `show` / `log -p` render the graph as its JSON export instead of
`Binary files differ`. What is committed is unchanged; a clone that skips the setup sees the old
behaviour rather than an error. Merge-conflict resolution via export → merge → rebuild is documented
in [`docs/REVIEW.md`](docs/REVIEW.md), and works because the build is byte-deterministic.

### Added

- **`python -m codebase_kg.upgrade`** — in-place v2 → v3, with `--covers` / `--exempt` to declare
  coverage at the same time. Reads the old shape with raw sqlite3 rather than loosening the store's
  version check for every reader.
- **`codebase-kg-export`** console script — git's textconv driver invokes a command, not a module.
- `build --rebaseline`, `build --source-root`, `build --no-hash`.

### Changed

- `kg_validate` replaces `uncovered_sources` with a `coverage` object (`declared`, `covered`,
  `gaps`, `exempt`, `out_of_scope`) and adds `changed_since_built`.
- `tools.uncovered_sources()` → `tools.coverage_report()`.
- `codec.from_dict()` returns `(meta, nodes, sources)`; `codec.to_dict()` takes an optional
  `sources` map. This is what makes the round trip lossless now that baselines exist.

## [0.2.0] — 2026-07-30 — the graph is a committed database

**Breaking.** The per-repo artifact changes from `knowledge/KNOWLEDGE_GRAPH.md` to a committed
`knowledge/code_graph.db` (SQLite). Every repo migrates once —
`python -m codebase_kg.migrate knowledge/KNOWLEDGE_GRAPH.md` — see [`docs/MIGRATION.md`](docs/MIGRATION.md).
The MCP tool names (`kg_*`) and the plugin name (`codebase-kg`) are deliberately **unchanged**.

### Fixed — a wide-table graph loaded as a single node

The pre-0.2 loader only understood the vertical key/value node table. Real graphs also use a wide
one-row-per-node form (`| id | kind | anchors | summary | edges | … |`), and against one of those the
loader read every row as a node called `kind` — collapsing the whole graph to **one** unusable node,
so every `kg_*` tool silently returned nothing for that repo. Measured against a real 93 KB graph:
1 node before, **162** after. The migration parser handles both shapes.

This was not a hypothetical: it had been shipping. The plugin's own fixtures were all vertical, so
the test suite passed throughout.

### Fixed — a rebuild while the MCP server ran could not write (Windows)

The server cached an open handle on the graph. Windows refuses to replace a file anyone holds open,
so `/codebase-kg:refresh` would fail to write its own output whenever the server was running. The
graph is now opened per tool call and closed again — affordable because opening a store is
constant-time, and it removes the cache-invalidation logic entirely. The writer additionally retries
its atomic rename briefly, so an editor or file indexer holding the file is a non-event.

### Changed — the store

- **Committed SQLite** with a versioned schema, opened read-only. Cold open is ~1 ms at any graph
  size, against a Markdown parse that was ~12 ms at 175 nodes and ~820 ms at 10,500 — a change of
  scaling class, not a constant factor.
- **Integrity is enforced at write time, not audited afterwards.** Foreign keys make a dangling edge
  unwritable; the primary key makes a duplicate id unwritable; CHECK constraints encode the three
  legal parity shapes and reject line-number anchors. `kg_validate` reports these as
  `guaranteed_by_schema` instead of searching for them.
- **Transactional and deterministic builds.** A build lands whole or not at all, and rebuilding an
  unchanged graph is byte-identical, so a no-op refresh leaves the git diff empty.
- **Persisted FTS5 index.** An in-memory index had been deferred because building it landed on the
  load path; persisting it removes that objection — built once at write time, free on open. Ranked
  search over 175 nodes runs in ~0.6 ms including hydration.
- Anchors are normalized into their own indexed table, which is what makes the new reverse lookup a
  lookup rather than a scan.
- On-disk size is honestly larger: ~390 KB vs ~115 KB of Markdown for 175 nodes, the cost of
  carrying an index. Git's packfile delta handles it.

### Changed — `summary` → `description`

One short line saying what a component is and does, capped at 240 chars. **Ticket ids, dates and
change narrative are rejected** by the builder and by a CHECK constraint on length.

In the real Android graph, summaries were 62% of the payload and carried 171 ticket refs plus
sentences like "Live-verified: screen-off keeps state=PLAYING" — content that duplicates git and the
tracker, goes stale immediately, and was the sole reason the format needed a maintenance ceremony at
all. Migration scrubs it: 175 nodes, average description 321 → 177 chars, zero ticket refs left,
zero nodes needing a manual rewrite.

Per-node `updated` dates are gone for the same reason; the artifact carries one `generated` stamp.

### Changed — the pre-push hook no longer blocks, and no longer looks at dates

It used to reject a push when source changed and the graph's `refreshed:` header wasn't today. That
contradicted the plugin's own "advisory, never blocking" principle, and a date cannot measure
freshness anyway — it proves someone edited the file, not that the nodes match the code. A real
graph sat at `refreshed: 2026-07-12` with three nodes stale from a later commit, under a gate
designed to prevent exactly that.

It now reports, and exits 0: new source files in the push that no node covers, and deleted files the
graph still anchors on. Both are facts about the changeset. Still stdlib-only and vendorable.

### Added

- **`kg_find_by_path`** — reverse lookup: given a source file, which node(s) own it and what
  connects to them. The inverse of every other tool; accepts a bare filename as a path suffix.
- **`python -m codebase_kg.build`** and **`python -m codebase_kg.export`** — the JSON authoring path,
  since an agent cannot write SQLite with an editor. Refresh is export → edit → build, and the round
  trip is lossless.
- **`python -m codebase_kg.migrate`** — the one-time conversion, with a report of everything it
  changed.
- `kg_validate` now reports **uncovered sources** — files under `root` no node anchors on. Derived
  from the extensions the graph already uses, so it stays language-agnostic.
- `kg_stats` reports **isolated nodes** (no edge in either direction), usually a missed relationship.
- `kg_neighborhood` returns hop counts and reaches 3 hops (was 2).
- Search splits CamelCase identifiers at index and query time, so "video playback" finds
  `VideoPlaybackService`. Tokens under 3 characters match exactly rather than as prefixes — prefix
  matching "to" hit "tonight", "token" and "tools".

### Docs

`SCHEMA.md` and `docs/DESIGN.md` rewritten; `docs/MIGRATION.md` added; all six commands, five skills,
both reference docs, and the templates updated. `templates/KNOWLEDGE_GRAPH.template.md` →
`templates/code_graph.template.json`; `docs/examples/EXAMPLE_KG.md` → `EXAMPLE_GRAPH.json`. Both
build cleanly as-is.

### Tests

222 passing (was 36). New coverage for the schema constraints (proving each bad write actually
fails), writer determinism and atomicity, the description contract, both markdown table shapes, the
JSON round trip, and migration fidelity against the fixtures.

## [0.1.1] — 2026-07-29

### Fixed

- **`kg-audit` skill frontmatter now parses.** Its `description` was an unquoted YAML scalar containing `: ` (`…the deep, SEMANTIC, multi-agent sweep: it partitions the KG…`), which YAML reads as a key/value separator. The frontmatter failed to parse, and Claude Code drops **all** frontmatter fields when that happens — so the skill loaded with no `name`, `description`, or `allowed-tools`, meaning it could not trigger reliably and its tool allowlist was silently lost. Converted to a `>-` block scalar; text unchanged.

## [Unreleased]

### Fixed — 2026-07-03 — code-review findings: live-reloading server, push-accurate gate, loader hardening

- **MCP server reloads the KG (H1).** The KG path was resolved once at startup and the parsed
  graph/peer cached forever — a repo with no KG at session start errored on every tool call even
  after `/codebase-kg:build`, and `kg_validate` after an edit validated the pre-edit snapshot. Now
  the path is re-resolved while unresolved, and both the graph and the peer KG are re-read whenever
  the file's mtime/size changes. Module docstring + `mcp/README.md` reconciled. +2 tests.
- **MCP server honors `.claude/codebase-kg.local.md` `kg_path` (M7).** SCHEMA.md §1/§7 said the
  per-clone override applied to "the tools", but only the hooks and the pre-push gate read it. The
  server's walk-up resolution now checks the `.local.md` frontmatter at each level too. +1 test.
- **Pre-push gate reads git's stdin (M3).** The gate guessed the diff range from the checked-out
  branch (`@{u}..HEAD` → `origin/main..HEAD` → `HEAD`), so pushing a different branch diffed the
  wrong changeset, a repo whose remote default isn't main/master silently passed, and two-dot
  ranges counted upstream-side changes. It now parses the
  `<local_ref> <local_sha> <remote_ref> <remote_sha>` lines git feeds on stdin and gates exactly the
  pushed refs: three-dot `remote_sha...local_sha` per ref, new branches diffed from the merge-base
  with the remote default (else every not-yet-remote commit), ref deletions skipped. A manual run
  without stdin falls back to `@{u}...HEAD` / `origin/main|master...HEAD` and **fails loudly** when
  no base exists instead of silently passing. `git-hooks/README.md`, the `pre-push` wrapper, and
  `/codebase-kg:setup` note the stdin ordering requirement. +8 tests, verified end-to-end
  against a real repo (existing ref / new branch / deletion / manual run).
- **Anchor check understands `Type.method` (M1).** `kg_validate` grepped the anchor symbol as one
  literal word, so the SCHEMA-endorsed `File.kt#Type.method` form was false-flagged ("symbol not
  found"). Dotted symbols are now checked segment-wise. Also caches file contents per validate call
  instead of re-reading a file once per anchor. +1 test.
- **Loader strips inline `# comments` from node rows (M2).** The template showed `  # optional …`
  tails on node-table rows but the loader only stripped comments on header lines, so garbage like
  `matched    # optional — multi-codebase only` escaped into parity values. Non-prose node fields
  (everything except `summary`/`divergence`) now drop a whitespace-preceded `#` tail — anchors and
  counterparts are safe because their `#` is always glued to the path. The template's node rows
  lost their inline comments (explanation moved to an HTML comment above the block). +1 test.
- **Duplicate node ids are reported (M5).** Duplicate ids silently collapsed (last wins) while
  `kg_validate` said ok, violating SCHEMA.md §4. The `Graph` now records collisions
  (`duplicate_ids`) and `kg_validate` reports them (and fails `ok`). +2 tests.
- **Table separator rows no longer truncate nodes (M6).** A `| --- | --- |` row (inserted by
  Prettier/markdownlint) inside a node block flushed the node, cutting it to id-only. Separator
  rows (dashes/colons cells) are now skipped. +1 test.
- **Advisory hook command is portable (M4).** `hooks/hooks.json` hardcoded `python`, which doesn't
  exist on stock macOS/many Linux. Now `python3 <script> || python <script>` — works under both
  POSIX `sh` and Windows `cmd.exe`, and the double-run objection can't apply because the script is
  fail-safe (always exits 0). Choice documented in `hooks/README.md`.
- **Minor:** pre-push `kg_path.lstrip("./")` charset-strip → `removeprefix("./")`; legacy
  `last refreshed` regex gains `(?i)` to match its sibling; the advisory hook's `exclude_ext` now
  matches dotfiles like `.gitignore` (aligned with the pre-push twin); `kg_validate`'s
  `root.strip("/")` no longer mangles roots (trailing slashes only; SCHEMA.md §3 + template now say
  `root` is repo-relative); `/codebase-kg:setup` references the vendored files via
  `${CLAUDE_PLUGIN_ROOT}/git-hooks/…`; `docs/DESIGN.md` stale `/codebase-kg:kg-*` command names
  corrected; dead `_HEADER_KEYS` removed from the loader; `.claude-plugin/plugin.json` gains
  `"version": "0.1.0"` (matching `mcp/pyproject.toml`); `_suggest` sorts by `(-score, id)` so the
  id tiebreak is no longer reversed; the five skills drop the nonstandard `when_to_use` frontmatter
  key (unique bits folded into `description`). 36 → 52 tests, all passing.

### Changed — 2026-06-29 — `knowledge/` is the single KG location (no repo-root fallback)

- The KG **always** lives at `knowledge/KNOWLEDGE_GRAPH.md`. The previous repo-root *fallback* is
  removed everywhere: the `kg-build` skill now unconditionally creates the `knowledge/` folder and
  writes there, and all three resolvers (advisory hook `find_kg`, pre-push gate `find_kg_rel`, MCP
  server `_resolve_graph_path`) resolve a single rule — explicit `kg_path` override → else
  `knowledge/KNOWLEDGE_GRAPH.md` — with no root candidate. `kg_path` now **defaults to**
  `knowledge/KNOWLEDGE_GRAPH.md` (was empty/auto-discover), so everything points to one configurable
  path. A repo without that file simply has no KG yet (hook + gate no-op). `SCHEMA.md` §1/§7, the
  `kg-build` skill, `DESIGN.md`, the config template, and the MCP/server docs updated. 36 tests pass.
  **Migration:** a repo with a root-level `KNOWLEDGE_GRAPH.md` must move it to `knowledge/` (or set
  `kg_path` in `.claude/codebase-kg.local.md`); it is no longer discovered at the root.

### Removed — 2026-06-29 — completed design docs (`docs/BUILD_PLAN.md`, `docs/MCP_SURFACE.md`)

- Deleted the 6-phase build plan and the Phase-2 MCP-surface design spec now that all phases ship.
  Their still-relevant content lives in the authoritative docs: locked decisions + dogfood target in
  `docs/DESIGN.md`, the live tool surface in `mcp/README.md`, and the schema in `SCHEMA.md`. Fixed
  the inbound references in `README.md`, `docs/DESIGN.md`, and `docs/examples/EXAMPLE_KG.md`; the
  earlier dated changelog entry that lists both files is left as the historical record.

### Fixed — 2026-06-14 — `CODEBASE_KG_PATH` is now optional (server auto-discovers the KG)

- `.mcp.json` referenced `${CODEBASE_KG_PATH}` as a **required** env var, so Claude Code refused to
  launch the MCP server whenever it was undefined (`MCP server codebase-kg invalid: Missing
  environment variables: CODEBASE_KG_PATH`). Changed to an empty default (`${CODEBASE_KG_PATH:-}`):
  the var stays an **optional override**, and when unset the server falls through to its `knowledge/`
  walk-up auto-discovery — so the KG is found in any repo with **no per-project config**. The
  documented `CLI arg → $CODEBASE_KG_PATH → walk-up` resolution order is unchanged.

### Changed — 2026-06-09 — project config lives in the committed KG header (not a `.local.md`)

- The shared, project-level config (`codebase`/`root`/`counterpart`) is the **committed KG header**.
  The pre-push gate and the advisory hook now read `root` from the header (auto-discovering the KG),
  so **no committed config file is needed**. `.claude/codebase-kg.local.md` is now correctly an
  **optional, gitignored per-developer override** (by the `.local` convention) — used only to
  override the header for one clone. Resolves the contradiction of committing a `.local` file.
  `SCHEMA.md` §7, the config template, the `install-hooks` command, and the git-hooks/hooks READMEs
  updated. +1 test (36 passing).

### Changed — 2026-06-09 — `knowledge/` is the default KG location

- The default KG location is now **`knowledge/KNOWLEDGE_GRAPH.md`** (a `knowledge/` subdirectory),
  not the repo root — a consistent home for the KG and related committed reference docs.
  Auto-discovery in the MCP server, the pre-push gate, and the advisory hook now prefers
  `knowledge/` and falls back to repo root. `SCHEMA.md`, the `kg-build` skill, `README.md`, and the
  config template updated. (Existing root-level KGs still work via the fallback + explicit `kg_path`.)

### Added — 2026-06-09 — pre-push KG gate (`git-hooks/`)

- A blocking, **stdlib-only, vendorable** git pre-push hook (`git-hooks/kg_pre_push.py` + `pre-push`
  wrapper) that rejects a push when tracked source under the KG's `root` changed but
  `KNOWLEDGE_GRAPH.md` isn't in sync (not in the changeset, or header `refreshed:`/legacy
  `last refreshed` not today). Override: `git push --no-verify`. No dependency on the MCP package, so
  it runs for every clone/CI. The semantic update stays the agent's `/codebase-kg:refresh`.
- `/codebase-kg:setup` command — agent-guided, non-destructive install (vendors the checker,
  wires `core.hooksPath`/`pre-push`, integrates into an existing hook rather than overwriting it).
- +10 tests (35 passing). This is the **enforcement** layer complementing the in-session nudge; the
  two linked repos stay eventually consistent because refresh reconciles parity vs the peer KG.

### Added — 2026-06-08 — per-node `updated` date

- New optional per-node `updated: YYYY-MM-DD` field (finer-grained than the header `refreshed`).
  `kg-build` stamps every node; `kg-refresh` bumps only touched nodes, so a node whose `updated`
  lags `refreshed` is a candidate stale node. Loader parses it (+ `last_updated`/`last-updated`
  aliases); `kg_node` returns it, `kg_search` shows it, `kg_stats` reports
  `updated.{oldest,newest,missing,stale_vs_refreshed}`. Schema/template/skills updated. +3 tests.

### Added — 2026-06-08 — Phases 2–6

- **Phase 2 — MCP query server** (`mcp/`): FastMCP stdio server parsing `KNOWLEDGE_GRAPH.md` into a
  queryable graph; 7 tools (`kg_search` / `kg_node` / `kg_neighborhood` / `kg_find_by_kind` /
  `kg_parity_gaps` / `kg_stats` / `kg_validate`). `loader`+`tools` are stdlib-only with 21 passing
  tests over a cross-linked ios/android fixture pair + source tree. Loader tolerates legacy
  hand-written field names (parses a real 700-line Android KG, 98 nodes). Root `.mcp.json`.
- **Phase 3 — skills + commands**: five advisory skills (`kg-build`, `kg-refresh`, `kg-audit`,
  `kg-link`, `kg-validate`) with references for the multi-agent ones; five thin slash commands.
- **Phase 4 — advisory hook** (`hooks/`): PostToolUse freshness nudge; never blocks, fail-safe,
  state in OS temp; honors `codebase-kg.local.md`.
- **Phase 5 — cross-codebase parity**: counterpart resolution + reciprocity in `kg_validate`,
  `kg_parity_gaps`, and the `kg-link` skill — verified end-to-end on the fixture pair.
- **Phase 6 — dogfood** (`docs/DOGFOOD.md`): read-only validation against a real Android KG;
  live migration of the external repos staged as a go-ahead step.
- `.gitattributes` (LF normalization).

### Reviewed — 2026-06-08 — plugin-dev validator + skill-reviewer

- Ran the `plugin-dev:plugin-validator` and `plugin-dev:skill-reviewer` agents — both PASS.
- **Fixed:** MCP server now loads the graph **lazily**, so it starts cleanly in a repo that has no
  `KNOWLEDGE_GRAPH.md` yet (the `/codebase-kg:build` first-run case) — a missing KG surfaces on first
  tool call, not as a server that refuses to start.
- **Sharpened** the `kg-validate` (structural/deterministic) vs `kg-audit` (semantic/deep) trigger
  descriptions so generic "check my KG" queries route deterministically.
- **Genericized** the kg-audit anecdote (no repo-specific reference) and two micro-wordings.

### Added — 2026-06-08 — Phase 1 scaffold + schema

- Repo skeleton (standalone plugin at root): `.claude-plugin/plugin.json` + thin
  `marketplace.json`, `.gitignore`, MIT `LICENSE`, `README.md`, `CHANGELOG.md`.
- **`SCHEMA.md`** — the canonical KG spec: generic node shape
  (`id / kind / anchors / summary / edges / parity / counterpart / divergence`), symbol-anchor
  rule (`path#Symbol`, never line numbers), document structure, cross-codebase parity model, and
  the no-header-only-refresh update policy.
- **`docs/examples/EXAMPLE_KG.md`** — a hand-built iOS↔Android slice exercising all three parity
  shapes (matched / divergent / android-only) with reciprocal `counterpart` links.
- `docs/DESIGN.md` (locked decisions, resolved open questions, genericity rules, principles),
  `docs/MCP_SURFACE.md` (Phase 2 tool-surface design), `docs/BUILD_PLAN.md` (the full 6-phase plan).
- `templates/KNOWLEDGE_GRAPH.template.md` (empty-KG template) +
  `templates/codebase-kg.local.md.example` (per-repo config).
- Phase placeholders with design notes: `mcp/` (Phase 2), `skills/` (Phase 3), `hooks/` (Phase 4).

### Not yet built

- Phase 2 — MCP query server (`mcp/`) + root `.mcp.json`.
- Phase 3 — skills (`kg-build` / `kg-refresh` / `kg-audit` / `kg-link` / `kg-validate`).
- Phase 4 — advisory post-edit freshness hook.
- Phase 5 — `counterpart` resolution + `kg_parity_gaps`.
- Phase 6 — dogfood on a real iOS↔Android pair.
