#!/bin/sh
# Wires THIS clone for the committed code graph. Run once per clone.
#
# Everything it sets lives in .git/config, which git never clones — so hooks and
# graph diffing are inert in a fresh checkout until someone runs this. That is
# deliberate on git's part: cloning a repo must not cause it to execute code.
# Nothing installs this for you; a person chooses to run it.
#
#   sh .githooks/install.sh
#
# Needs plain git and POSIX sh. `uv` is needed only for readable graph diffs.
# No Claude Code and no plugin install required. Re-running changes nothing that
# is already correct.
#
# Overrides (env):
#   KG_VERSION     tag of codebase-kg to export with (default: the pin below)
#   KG_SOURCE      full uvx --from source, replacing the tag-pinned URL
#   KG_TEXTCONV    full textconv command, replacing uvx entirely
#                  e.g. KG_TEXTCONV="python -m codebase_kg.export"
set -eu

# Pinned to a TAG, never a branch: this command runs on every diff of the graph
# and must not change under the repo silently. /codebase-kg:setup stamps this
# line with the plugin version that wrote it; bump it deliberately.
KG_VERSION="${KG_VERSION:-0.9.0}"
KG_SOURCE="${KG_SOURCE:-git+https://github.com/Alexk413x/codebase-kg.git@codebase-kg--v${KG_VERSION}#subdirectory=mcp}"
# --quiet is not cosmetic: without it uv prints resolution lines into the body of
# every `git diff` of the graph.
KG_TEXTCONV="${KG_TEXTCONV:-uvx --quiet --from \"${KG_SOURCE}\" codebase-kg-export}"

GRAPH="${KG_GRAPH_PATH:-knowledge/code_graph.db}"
HOOK_FILES="pre-commit pre-push kg_pre_commit.py kg_pre_push.py"

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# Anchor every path on this script's own location, then work from that repo's
# root. Keyed off the caller's cwd instead, `sh /path/to/other/clone/.githooks/
# install.sh` silently configured whatever repo the caller was standing in — it
# set core.hooksPath on the wrong repo and exited 0.
hooks_abs=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! (CDPATH= cd -- "$hooks_abs" && git rev-parse --is-inside-work-tree >/dev/null 2>&1); then
  warn "codebase-kg: $hooks_abs is not inside a git work tree."
  exit 1
fi

# --show-prefix keeps this on git's own path spelling. Comparing `pwd` against
# `git rev-parse --show-toplevel` does not work under Git Bash on Windows, where
# one reports a POSIX-style prefix carrying the drive as a leading path segment
# and the other a native drive letter. Described rather than shown: this file is
# vendored into other repos, and a repo that greps committed files for
# machine-specific paths cannot tell an example from the real thing.
hooks_rel=$(CDPATH= cd -- "$hooks_abs" && git rev-parse --show-prefix)
hooks_rel=${hooks_rel%/}
[ -n "$hooks_rel" ] || hooks_rel="."

# GRAPH and every git config call below are relative to the repo root.
CDPATH= cd -- "$(CDPATH= cd -- "$hooks_abs" && git rev-parse --show-toplevel)"

status=0

# --- 1. core.hooksPath -------------------------------------------------------
current=$(git config --get core.hooksPath 2>/dev/null || true)
if [ -z "$current" ]; then
  git config core.hooksPath "$hooks_rel"
  say "core.hooksPath -> $hooks_rel"
else
  current_abs=$(CDPATH= cd -- "$(git rev-parse --show-toplevel)" 2>/dev/null &&
                CDPATH= cd -- "$current" 2>/dev/null && pwd) || current_abs=""
  if [ "$current_abs" = "$hooks_abs" ]; then
    say "core.hooksPath already $current"
  else
    warn "codebase-kg: core.hooksPath is already set to '$current', which is not"
    warn "  $hooks_rel. That is someone's deliberate choice, so this script will not"
    warn "  clobber it. Either point it here, or add these two lines to the"
    warn "  pre-commit and pre-push in '$current' (pre-push: before anything that"
    warn "  reads stdin):"
    warn "    if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi"
    warn "    \"\$PY\" \"$hooks_rel/kg_pre_commit.py\" || true   # kg_pre_push.py in pre-push"
    status=1
  fi
fi

# --- 2. executable bits ------------------------------------------------------
# Git invokes hooks directly on macOS and Linux: a non-executable hook is skipped
# with no message at all.
needs_index_fix=""
for f in $HOOK_FILES; do
  [ -f "$hooks_abs/$f" ] || continue
  chmod +x "$hooks_abs/$f" 2>/dev/null || true
  mode=$(CDPATH= cd -- "$hooks_abs" && git ls-files -s -- "$f" 2>/dev/null | awk '{print $1}')
  case "$mode" in
    100644) needs_index_fix="$needs_index_fix $hooks_rel/$f" ;;
  esac
done
if [ -n "$needs_index_fix" ]; then
  warn "codebase-kg: these are committed non-executable, so every other clone will"
  warn "  skip them. Fix once and commit (this script does not stage anything):"
  warn "    git update-index --chmod=+x$needs_index_fix"
fi

# --- 3. the .gitattributes routing the textconv driver depends on ------------
attr=$(git check-attr diff -- "$GRAPH" 2>/dev/null | sed 's/.*: //')
if [ "$attr" != "codegraph" ]; then
  warn "codebase-kg: $GRAPH is not routed to the codegraph diff driver"
  warn "  (git check-attr says '$attr'). Add to .gitattributes and commit:"
  warn "    $GRAPH binary diff=codegraph"
fi

# --- 4. the textconv driver --------------------------------------------------
# Display only. It changes how git renders the graph, never what is committed.
set_cfg() {
  have=$(git config --get "$1" 2>/dev/null || true)
  if [ "$have" = "$2" ]; then
    say "$1 already set"
  else
    git config "$1" "$2"
    say "$1 -> $2"
  fi
}

# An explicit KG_TEXTCONV override is the caller's responsibility; only the
# default uvx form gets a presence check.
textconv_runnable() {
  case "$KG_TEXTCONV" in
    uvx*) command -v uvx >/dev/null 2>&1 ;;
    *) true ;;
  esac
}

# Probe BEFORE writing anything. A configured textconv command that fails does
# not degrade to "Binary files differ" -- git aborts the diff outright
# ("fatal: unable to read files to diff"). An unreachable driver is worse than
# no driver, so an unverified one is not left in place.
textconv_works() {
  [ -f "$GRAPH" ] || return 0  # nothing to probe against; write it and hope
  out=$(eval "$KG_TEXTCONV \"\$GRAPH\"" 2>/dev/null) || return 1
  case "$out" in \{*) return 0 ;; *) return 1 ;; esac
}

if ! textconv_runnable; then
  warn "codebase-kg: uvx not found, so graph diffs are left as 'Binary files"
  warn "  differ'. Hooks are wired. Install uv (https://docs.astral.sh/uv/) and"
  warn "  re-run this script, or re-run with a local exporter:"
  warn "    KG_TEXTCONV=\"python -m codebase_kg.export\" sh \"\$0\""
elif textconv_works; then
  set_cfg diff.codegraph.textconv "$KG_TEXTCONV"
  # binary=true: use textconv for display, never try to make an appliable patch.
  set_cfg diff.codegraph.binary true
  # cachetextconv caches converted output per blob, so re-reading history does
  # not re-export. It stores that cache in a notes ref, and writing a note needs
  # a committer identity -- without one git fails the whole diff rather than
  # falling back, so leave it off rather than trade a readable diff for a fatal
  # error.
  if git var GIT_COMMITTER_IDENT >/dev/null 2>&1; then
    set_cfg diff.codegraph.cachetextconv true
  else
    warn "codebase-kg: no git identity here, so diff.codegraph.cachetextconv is"
    warn "  left unset (its cache lives in a notes ref, which needs one). Set"
    warn "  user.email and user.name, then re-run to turn caching on."
  fi
  [ -f "$GRAPH" ] && say "textconv driver renders $GRAPH as JSON"
else
  # Also clears a driver an earlier run left behind: while it is configured and
  # broken, every `git diff` touching the graph fails instead of falling back.
  git config --unset diff.codegraph.textconv 2>/dev/null || true
  warn "codebase-kg: the textconv driver did not produce JSON, so it is left"
  warn "  unset -- a broken one makes 'git diff' on the graph fail outright"
  warn "  rather than fall back. Hooks are wired; graph diffs read 'Binary files"
  warn "  differ' until this works. Check you can reach the source:"
  warn "    git ls-remote https://github.com/Alexk413x/codebase-kg.git >/dev/null"
  warn "  A missing tag, or no read access to a private repo, both land here."
  warn "  With the package installed locally, skip uv entirely:"
  warn "    KG_TEXTCONV=\"python -m codebase_kg.export\" sh \"\$0\""
  status=1
fi

say "codebase-kg: done."
exit $status
