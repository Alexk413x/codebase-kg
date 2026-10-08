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

1. **Bump the version in all four places.** They must agree. `mcp/tests/test_install_sh.py`
   asserts that the pin tracks the manifest.

   | file | field |
   |---|---|
   | `.claude-plugin/plugin.json` | `version` |
   | `mcp/pyproject.toml` | `version` |
   | `git-hooks/install.sh` | `KG_VERSION` default |
   | `mcp/uv.lock` | the `codebase-kg` entry (uv rewrites it) |

2. **Write the changelog entry** at the top of `CHANGELOG.md`, above the previous
   release. Name the defect and what it cost, not the diff.

3. **Run the checks locally.** The local result is the gate.

   ```sh
   cd mcp
   uv run python scripts/gen_catalog.py   # if a tool in server.py changed
   uv run pytest -q
   cd ..
   claude plugin validate --strict .
   claude plugin eval .
   ```

   `claude plugin eval` refuses the hard links in `mcp/.venv`. Run it on a copy made with
   `git ls-files -co --exclude-standard`, which leaves `.venv` out.

4. **Open a PR and merge to main.** Tags point at the *merge* commit, which does
   not exist until then.

5. **Tag the merge commit and push:**

   ```sh
   git checkout main && git pull
   claude plugin tag --push -m "codebase-kg %s — <one line>"
   ```

   `claude plugin tag` builds `codebase-kg--v<version>` from `plugin.json`,
   checks it against the enclosing marketplace entry, and refuses on a dirty
   tree. Use it rather than `git tag` by hand.

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
   claude plugin marketplace update codebase-kg
   claude plugin update codebase-kg@codebase-kg
   ```

   The `codebase-kg` marketplace is this repo's `.claude-plugin/marketplace.json`,
   and it installs from `main`. Check that the `codebase-kg@codebase-kg` entry in
   `~/.claude/plugins/installed_plugins.json` names the new version. A session
   loads the plugin cache, not the branch you merged.

8. **Restart the server.** A running server keeps the build it started with.
   A newer build takes the port from an older one when it starts, so restart
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
