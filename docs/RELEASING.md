# Releasing codebase-kg

Every release needs a git tag, and the tag is not bookkeeping. `git-hooks/install.sh`
writes a textconv driver pinned to one:

```
uvx --quiet --from "git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v<version>#subdirectory=mcp" codebase-kg-export
```

A clone wired against a version whose tag was never pushed resolves nothing. The
probe fails, `diff.codegraph` is left unset, and every `git show` of a committed
graph prints `Binary files differ` with nothing anywhere explaining why. Git says
nothing when a textconv command cannot resolve — it falls back silently.

## The checklist

1. **Bump the version in all six places.** They must agree. `mcp/tests/test_install_sh.py`
   fails when they do not.

   | file | field |
   |---|---|
   | `.claude-plugin/plugin.json` | `version` |
   | `.codex-plugin/plugin.json` | `version` |
   | `.antigravity-plugin/plugin.json` | `version` |
   | `mcp/pyproject.toml` | `version` |
   | `git-hooks/install.sh` | `KG_VERSION` default |
   | `mcp/uv.lock` | the `codebase-kg` entry (uv rewrites it) |

   When `SCHEMA_VERSION` in `mcp/src/codebase_kg/schema.py` changes, also add the new schema
   and this release to `SCHEMA_SINCE` in `hooks/kg_session_start.py`. The SessionStart hook
   uses it to report a diff driver that is pinned too low to read the graph.

2. **Write the changelog entry** at the top of `CHANGELOG.md`, above the previous
   release. Name the defect and what it cost, not the diff.

3. **Run the checks locally.** The local result is the gate.

   ```sh
   cd mcp
   uv run --frozen python scripts/gen_catalog.py   # if a tool in server.py changed
   uv run --frozen pytest -q
   uv run --frozen pyright
   uv run --frozen ruff check ..
   uv run --isolated --frozen --python 3.9 pytest -q   # the floor: hooks and server run on 3.9
   cd ..
   claude plugin validate --strict .
   claude plugin eval <copy> --scaffold --trust-plugin
   ```

   On Windows, run all of this from Git Bash. The hook tests and each case's `scaffold.sh` need a
   POSIX shell, and PowerShell passes `scaffold.sh` a path with backslashes, which fails every case
   with exit 127.

   `--scaffold` runs each case's `scaffold.sh`. Without it the query cases have no graph to query,
   and the query skill correctly skips the graph tools.

   The cases copy `evals/fixtures/app` (a small Kotlin tree with a built `knowledge/code_graph.db`)
   or `evals/fixtures/nograph` into the working folder. `evals/fixtures/graph.json` is the authoring
   JSON that `app`'s database was built from; a test checks that building it gives the same nodes.
   Rebuild the database from it with `kg_cli.py build evals/fixtures/graph.json -o
   evals/fixtures/app/knowledge/code_graph.db --source-root evals/fixtures/app/src` when the fixture
   changes. `evals/mocks/codebase-kg` holds the mocked tool list and answers the `mocked` cases use.

   `claude plugin eval` refuses the hard links in `mcp/.venv`. Run it on a copy made with
   `git ls-files -co --exclude-standard`, which leaves `.venv` out.

   Give the copy a port of its own. The copy is a development copy, so it leaves port 47821 to the
   installed plugin's server and its sessions use that server, which may be an older version with
   no hook endpoints: the gates are then off and the cases measure nothing. `CODEBASE_KG_PORT` in
   the shell does not reach the sessions the eval starts, so change the default of `server_port`
   in the copy's `.claude-plugin/plugin.json` to a free port before the run. After the run, stop
   the copy's server, which outlives the copy:

   ```sh
   python3 <copy>/mcp/launch/kg_cli.py server stop --port <that port>
   ```

4. **Open a PR and merge to main.** Tags point at the *merge* commit, which does
   not exist until then.

5. **Check the tag.** On every push to `main`, `.github/workflows/release.yml`
   tags the commit `codebase-kg--v<version>` from `plugin.json` when that tag is
   missing. The annotation is the title of the version's changelog entry. Confirm
   it with `git ls-remote --tags origin`. To tag by hand, use
   `claude plugin tag --push -m "codebase-kg %s — <one line>"` on a clean `main`.

6. **Verify the tag resolves**, which is the only step that proves the release
   works. A tag listing does not — it shows the ref exists, not that the pinned
   URL installs. Clone into a throwaway, build a repo with a committed graph, and
   run the installer with no overrides:

   ```sh
   sh .githooks/install.sh
   ```

   It must print `textconv driver renders <graph> as JSON`. Then confirm
   `git diff HEAD~1 HEAD -- <graph>` renders the JSON export instead of
   `Binary files differ`.

7. **Refresh the install:**

   ```sh
   claude plugin marketplace update alexk413x
   claude plugin update codebase-kg@alexk413x
   ```

   The `alexk413x` marketplace installs from the `release` branch, which
   `.github/workflows/release.yml` rebuilds from `main` on every push. Wait for that
   workflow to finish before you update. Check that the `codebase-kg@alexk413x` entry in
   `~/.claude/plugins/installed_plugins.json` names the new version. A session
   loads the plugin cache, not the branch you merged.

8. **Restart the server.** A running server keeps the build it started with.
   A release or an installed copy of a higher version takes the port from an older server when it starts, so restart
   Claude Code, or stop the old server first:

   ```sh
   uv run --no-project --quiet "<plugin>/mcp/launch/kg_cli.py" server stop
   ```

   `kg_cli.py server status` prints the running build.

## Vendored files

Anything under `git-hooks/` is copied into other people's repos verbatim. Before
releasing a change there, remember it lands in every consumer on the next
re-vendor, and that fixing it downstream does nothing.
`mcp/tests/test_vendored_portability.py` covers the one failure mode that already
escaped: an absolute path naming one machine.
