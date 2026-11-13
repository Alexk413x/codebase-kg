# Review fixes — 2026-11-12

Checklist from the read-only review of codebase-kg 0.15.4 plus the auto-update change.
Tick an item when its fix and a test for it are in the working tree. Add one line under
the item saying what changed. Items under "Needs a decision" wait for an answer from Alex.

## Fixes in progress

### High
- [x] **F1** Windows server start drops `server_port` and `max_workers`: `mcp/src/codebase_kg/shim.py:680` (`spawn_server_outside_job`) forwards only `CODEBASE_KG_*`, but `http_port()` (`shim.py:193`) and `pool.max_workers()` (`pool.py:60`) also read `CLAUDE_PLUGIN_OPTION_*`. A WMI-created process inherits no caller environment.
  Changed: `shim.py` `spawn_server_outside_job` now forwards `CLAUDE_PLUGIN_OPTION_*` as well as `CODEBASE_KG_*`. Test: `test_http_server.py::test_the_outside_job_spawner_forwards_the_plugin_settings`.
- [x] **F2** A `#` before a push hides it from the push gate: `hooks/kg_push_gate.py:87` replaces newlines with ` ; ` and lexes with comments on. `git commit -m fix#12 && git push`, `# publish\ngit push`, `echo a#b; git push` all pass.
  Changed: `kg_push_gate.py` new `_prepare` strips a `#` comment only at word start (and joins backslash-newline), and `_segments` lexes with `commenters=""`. Tests in `test_push_gate.py` (hash and comment cases).

### Medium
- [x] **F3** Push gate misses wrapped pushes (`hooks/kg_push_gate.py:40-47`): `timeout 60 git push`, `nice`, `stdbuf`, `xargs`, `eval`, backticks, `cmd /c git push`, `pwsh -c`, `git \<newline> push`, `sudo --user me git push`, `git subtree push`; PowerShell backtick-escaped quotes and here-strings raise `ValueError`, which allows the push.
  Changed: `kg_push_gate.py` `_inner_command` unwraps `eval`, `cmd /c`, `pwsh -c` and more shells; wrappers gain `timeout nice ionice stdbuf xargs setsid doas` and long `sudo` options; backticks split segments; `git subtree push` counts; `powershell_to_shell` drops here-strings and turns backtick-escaped quotes into backslash quotes. Tests: `test_a_wrapped_push_is_detected` and friends.
- [x] **F4** Push gate wrongly denies `git push --dry-run`, `git push -n`, `git push --help`.
  Changed: `kg_push_gate.py` `_pushes_nothing` makes `--dry-run`, `-n` (also bundled), `--help`, `-h` not a push. Test: `test_a_push_that_sends_nothing_is_not_gated`.
- [x] **F6** `mcp/tests/test_shared_server.py:349,567` launch the shim with `shutil.which("python3")`, which resolves to the Store stub; 10 tests fail on Windows. Use `sys.executable` or probe the candidate.
  Changed: `test_shared_server.py` `_shim` and the shadowed-stdlib test launch the shim with `sys.executable` instead of `shutil.which("python3")` (the Store stub). All 32 tests in the file pass.
- [x] **F7** `hooks/_codex_config.py` `refresh`: keep the file mode of `config.toml`, write through a symlink instead of replacing it.
  Changed: `hooks/_codex_config.py` `refresh` resolves a symlink with `os.path.realpath`, writes the temp file beside the real target and copies its mode with `shutil.copymode`. Tests `test_refresh_keeps_the_file_mode` and `test_refresh_writes_through_a_symlinked_config` skip on Windows (no POSIX modes, no symlink privilege), so they were not run red-to-green here.
- [x] **F8** `hooks/hooks.json:10` (every handler) and `mcp/claude.mcp.json:6`: when the script exits non-zero under `py -3`, the `||` chain runs it again under `python`. Resolve the interpreter once, then run the script once.
  Changed: `hooks/hooks.json` (all 31 commands) and `mcp/claude.mcp.json` `headersHelper` now probe the interpreter (`python`, a `python3` that starts, `py -3`) and run the script once. Tests: `test_hook_config.py::test_a_failing_hook_script_runs_once_not_once_per_interpreter`; the exact-string asserts in `test_http_server.py`, `test_sessions.py`, `test_shared_server.py` use the new form.
- [x] **F9** SessionStart can exceed its 10 s hook timeout when the pin is stale and the machine is offline (`hooks/kg_session_start.py` `tag_published`, 6 s, plus the other git calls). Keep the total under the timeout.
  Changed: `hooks/kg_session_start.py` adds `HOOK_BUDGET` (8 s) and `_timeout`; `_git`, `tag_published` and `repin` draw per-call timeouts (3 s, 4 s, 3 s) from one deadline that `main` sets, and skip once it is spent. Tests: `test_a_stale_pin_on_an_offline_machine_stays_inside_the_hook_timeout`, `test_every_git_call_is_skipped_once_the_budget_is_spent`.
- [x] **F10** A write that exceeds the 60 s call timeout leaks `.code_graph.db.*.edit` in `knowledge/` (`mcp/src/codebase_kg/pool.py:103-107`, `edit_core.py:157`). Remove stale copies.
  Changed: `edit_core.py` new `_sweep_abandoned_copies` (called first in `apply`) deletes `.<graph>.*.edit` copies older than `STALE_EDIT_AGE` (600 s). Test: `test_edits.py::test_a_write_removes_the_copies_an_abandoned_write_left_behind`.

### Low
- [x] **F11** `hooks/kg_session_start.py` `repin`/`tag_published`: pass `--` before the URL given to `git ls-remote`.
  Changed: `hooks/kg_session_start.py` `tag_published` runs `git ls-remote --tags -- <url> <tag>`. Test: `test_a_url_that_looks_like_an_option_is_not_read_as_one`.
- [x] **F12** `mcp/src/codebase_kg/tcp_transport.py:139`: `on_open()` runs before the handshake, so an unauthenticated local process keeps the daemon alive.
  Changed: `tcp_transport.py` `TcpHandler.handle` calls `on_open()` only after the handshake is accepted, and `on_close()` only if it opened. Test: `test_tcp_transport.py::test_only_an_authenticated_connection_counts_as_a_use_of_the_server`.
- [x] **F13** Gate state is a non-atomic read-modify-write (`hooks/_config.py:109`); write to a temp file and replace.
  Changed: `hooks/_config.py` `write_state` writes a temp file beside the state and `os.replace`s it (three tries on Windows `PermissionError`); `kg_search_gate.py` `_prune` also clears stale `.*.tmp`. Tests: `test_hook_config.py::test_write_state_replaces_the_file_through_a_temp_copy`, `test_a_failed_replace_leaves_the_old_state_whole_and_no_temp_file`.
- [x] **F14** Stale spawn-lock race (`mcp/src/codebase_kg/shim.py:590-618`): two processes can both clear the lock; `release_lock` unlinks without checking ownership.
  Changed: `shim.py` `take_lock` clears an abandoned lock through `_clear_abandoned`, which holds an exclusive `.lock.reap` marker and re-judges the lock inside it; `release_lock` deletes the lock only when it holds this process id. Tests: `test_review_transport.py::test_a_second_process_does_not_clear_a_lock_the_first_just_took`, `test_a_lock_another_process_holds_is_not_released_by_this_one`.
- [x] **F15** The search-gate credit hook writes state before checking for a graph (`hooks/kg_search_gate.py:476-478`).
  Changed: `hooks/kg_search_gate.py` `_gate` grants credit only when `find_graph` finds a graph. Test: `test_search_gate.py::test_a_graph_query_in_a_repo_with_no_graph_leaves_no_state`.
- [x] **F16** The server-down probe runs before the `server_down_noted` check and loads the shim twice (`hooks/kg_search_gate.py:496`, `:320`).
  Partly: `kg_search_gate.py` `_run` now caches the loaded shim for one run (`_shim`), so the probe and the notice load it once. I did not move the probe after the `server_down_noted` check: the probe is what notices the server coming back up, so skipping it once the notice was shown would leave the gate off for the session. Test: `test_review_hooks.py::test_the_server_down_notice_loads_the_shim_once`.
- [x] **F17** Docs still describe the old session-start behaviour: the `_git` docstring ("nothing in this hook writes") and `repin` docstring ("the one write") in `hooks/kg_session_start.py`; `hooks/README.md:136`; `docs/DESIGN.md:225-227`; `git-hooks/README.md:155`. Document that `SKIP_KG` also skips the Codex config refresh.
  Changed: docstrings in `hooks/kg_session_start.py` (`_git`, `repin`, module); `hooks/README.md`, `docs/DESIGN.md` (rule 11), `git-hooks/README.md` describe the pin move, the Codex refresh and that `SKIP_KG` skips both. Test (guards the SKIP_KG claim): `test_codex_config_refresh.py::test_skip_kg_also_skips_the_codex_config_refresh`.
- [x] **F18** `README.md:258-284` layout tree omits `copilot/`, `evals/`, `.codex/`, `.github/plugin/plugin.json`, `mcp/launch/kg_copilot.py`; `hooks/README.md` does not list `kg_copilot.py` or `copilot-hooks.json`.
  Changed: `README.md` layout tree gains `.codex/`, `.github/`, `copilot/`, `evals/` and `kg_copilot.py`; `hooks/README.md` table lists `copilot-hooks.json` and `kg_copilot.py`. Doc-only, so no test.
- [x] **F19** `exclude_ext` is read (`hooks/_config.py:35`) but missing from `templates/codebase-kg.local.md.example` and `SCHEMA.md`.
  Changed: `templates/codebase-kg.local.md.example` (commented example plus the default list) and `SCHEMA.md` section 8 now describe `exclude_ext`. Test: `test_hook_parity.py::test_every_hook_setting_is_documented_in_the_template_and_the_schema`.
- [x] **F20** `async: true` on the MCP-matcher hook makes `timeout: 10` dead config (`hooks/hooks.json:175-185`); remove it or say why it is there.
  Kept `async: true` (the credit hook must not delay a graph query); `hooks/hooks.json` description and `hooks/README.md` now say why. Test: `test_the_async_credit_hook_says_why_it_is_async`.

## Decided 2026-11-12

- [x] **F5** Decision: yes, cover Git Bash on Windows. `mcp/launch/kg-shim` probes each candidate (`py -3`, `python3`, `python`) before it uses one.
  Changed: `mcp/launch/kg-shim` runs each candidate with `-c ""` and `exec`s the first that starts; none starting exits 127 with a message. Tests: `test_shared_server.py::test_the_posix_shim_launcher_skips_a_python3_that_does_not_start`, `..._says_so_when_no_interpreter_starts`.
- [x] **Q1** Decision: canonicalise after edits. Every write tool leaves the same bytes a full build of the same content gives (`mcp/src/codebase_kg/edit_core.py:164-199`, claim in `writer.py:8`).
  Changed: `edit_core.py` `_land_canonical` reads the validated copy back and lands it through `writer.build` (baselines passed through, no re-hash; new `keep_v3` option so a v3 graph stays v3); stale sweep also covers `.tmp`. Tests: `test_edits.py` byte-identity per write tool, v3, baselines, no leftover copies. 20k nodes: edit ~3.9 s before, ~3.9-4.7 s after.
- [x] **Q2** Decision: Python 3.9 everywhere. `mcp/pyproject.toml`, the pyright config, `mcp/launch/kg_cli.py` and `mcp/uv.lock` move to the 3.9 floor that `ruff.toml` already states.
  Changed: `requires-python` and pyright `pythonVersion` are 3.9, `kg_cli.py` header too, `uv.lock` regenerated. `fastmcp` (dev only; needs 3.10) has a `python_full_version >= '3.10'` marker and `tests/conftest.py` skips the six modules that import it on 3.9. `compileall` of hooks, git-hooks, mcp/src and mcp/launch passes on 3.9; the rest of the suite passes there except what needs a project install. Test fixes: `Path.write_text(newline=)` (3.10+) replaced in three tests. Not changed: build backend `hatchling>=1.29` needs 3.10, so `pip install` of the package on 3.9 is not possible (see report).
- [x] **Q3** Decision: support PowerShell too. The hook commands in `hooks/hooks.json` and the `headersHelper` in `mcp/claude.mcp.json:6` must run on Windows without Git Bash as well as in a POSIX shell.
  Changed: every hook command and the `headersHelper` is `. "${CLAUDE_PLUGIN_ROOT}/mcp/launch/run/<script>.cmd"`, one sh/cmd polyglot launcher per script (7), tested in sh, bash, dash, Windows PowerShell 5.1 and pwsh 7 with a plugin path holding a space. Tests: `test_hook_config.py` (shape, template, LF, per-shell runs); exact-string asserts updated in `test_http_server.py`, `test_sessions.py`, `test_shared_server.py`, `test_codex_support.py`, `test_push_gate.py`. Docs: `hooks/README.md` "How a hook starts", `README.md`.
- [x] **Q4** Decision: leave `graph_path` as it is (`mcp/src/codebase_kg/resolve.py:83-84`). No change.
- [x] **Q5** Decision: keep `compact` out of the SessionStart matchers (`hooks/hooks.json`). Say in `hooks/README.md` that the omission is deliberate.
  Changed: `hooks/README.md` says `compact` is left out on purpose and why. Test: `test_hook_config.py::test_session_start_leaves_out_compact_on_purpose`.
- [x] **Q6** Decision: keep `.codex/config.toml.example`, `evals/fixtures/graph.json` and `templates/code_graph.template.json`, and link each from the docs.
  Changed: `README.md` (Codex paragraph, layout tree) describes `.codex/config.toml.example`; `docs/RELEASING.md` describes `evals/fixtures` and says `graph.json` is the source `app`'s database was built from (scaffold.sh copies the built db, it does not build it); `skills/build/SKILL.md` lists the template. Test: `test_codec.py::test_the_evals_fixture_graph_json_builds_the_fixture_database`.
- [x] **Q7** Decision: skip the icon and `privacyPolicyUrl` for now. Moved to Later.
- [x] **Q8** Decision: keep the `find`, `sudo`, `Get-ChildItem` and `gci` gate entries, so file-name searches still go to the graph first. No change.

## Later (suggested features, not started)

- [x] Schema-aware fallback notice when the repin cannot run and the pin cannot read the graph.
  Done: `unreadable_pin` and `SCHEMA_SINCE` in `hooks/kg_session_start.py`; `advice` prints one line when the pin cannot move and cannot read the graph.
- [ ] Stable Codex launcher path outside the versioned plugin folder.
  Not started, judged not worth it now: the SessionStart hook already moves the path after an update, and a stable launcher would still need that rewrite to migrate existing tables. Revisit if the first-session restart shows up in real use.
- [ ] `bin/codebase-kg` wrapper so skills allow `Bash(codebase-kg *)`.
  Not started, judged not worth it now: it changes the `allowed-tools` rule of every skill, and how Claude Code runs a plugin `bin/` file on Windows without Git Bash is untested.
- [x] `statusMessage` on the server-start hook.
  Done: `hooks/hooks.json`, SessionStart server-start entry.
- [ ] `context: fork` or an `agents/` reviewer for the audit skill.
  Not started, judged not worth it: the audit skill starts its own reviewers, and a forked skill runs as a subagent, which may not be able to start them. It would also lose the conversation the audit is asked about.
- [x] Slimmer tool descriptions, starting with `kg_upsert_node`.
  Compact `tools` JSON, 13 tools: 17,102 to 11,857 bytes; `kg_upsert_node` 4,352 to 2,266 bytes.
- [x] Plugin directory listing: add an icon and `privacyPolicyUrl` (Q7).
  Half done: README "Privacy" section and `privacyPolicyUrl` in `.claude-plugin/plugin.json`. Icon added 2026-11-12: `.claude-plugin/icon.png`, 1024 px. The directory takes an icon once, so replace it before the first submission if you want a different one.

## Found while verifying

- [x] **V1** `mcp/tests/test_http_battery.py:393` (`test_a_missing_or_wrong_token_is_403_not_401`) fails now and then on Windows with `ConnectionAbortedError [WinError 10053]` (2 of 13 runs on 2026-11-12). `http_transport.py:240` refuses with 403 in code not changed today. Likely cause, not confirmed: the server closes the connection before it reads the request body, so the client sometimes sees an abort where it should see the 403.
  Fixed: `Handler._drain` in `mcp/src/codebase_kg/http_transport.py` reads the unread body before a refusal closes; test `test_a_refused_post_is_answered_after_its_body_is_read`. Cause confirmed: the new test failed every run before the fix.
- [x] **V2** Package install on Python 3.9: `mcp/pyproject.toml` build backend lowered to `hatchling>=1.27.0`; wheel built and imported on 3.9.25.
- [x] **V3** PowerShell as a full path (follow-up to Q3): exit code carried (`; exit $LASTEXITCODE` in every command, `call exit /b %%errorlevel%%` in the launchers), the launcher finds its script without `CLAUDE_PLUGIN_ROOT`, and tests in `mcp/tests/test_hook_config.py` run it in Windows PowerShell 5.1 and PowerShell 7. Not verified: a Claude Code session on Windows without Git Bash; macOS and Linux.
