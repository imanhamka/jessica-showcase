#!/bin/bash
# Safe concurrent push for claude-memory — used by memory-nudge.sh, session-end-memory.sh,
# and the SessionStart/PostToolUse auto-memory rule in CLAUDE.md.
# Handles two machines (iMac + MacBook) writing memory at the same time: retries on
# non-fast-forward, auto-merges (MEMORY.md uses a union merge driver — see .gitattributes),
# and on a genuine conflict elsewhere, keeps BOTH sides instead of silently dropping one.
# Also serializes same-machine concurrent callers (memory-nudge.sh backgrounded +
# session-end-memory.sh foreground can otherwise race on the same working copy) — macOS has
# no `flock(1)`, so this uses a portable mkdir-based lock instead.
set -uo pipefail

REPO="${CLAUDE_MEMORY_REPO:-$HOME/claude-memory}"
MSG="${1:-auto: memory update $(date '+%Y-%m-%d %H:%M')}"
LOG="$HOME/.claude/logs/memory-safe-push.log"
mkdir -p "$HOME/.claude/logs"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
HOST_SAFE="$(echo "$HOST" | tr -c 'A-Za-z0-9_-' '-')"

LOCKDIR="$HOME/.claude/logs/memory-safe-push.lock"
acquired=0
for _ in $(seq 1 30); do
  if mkdir "$LOCKDIR" 2>/dev/null; then
    acquired=1
    break
  fi
  sleep 1
done
if [[ "$acquired" -ne 1 ]]; then
  echo "[$(date '+%F %T')] [$HOST] lock timeout — another push held it >30s, skipping" >> "$LOG"
  exit 1
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO" 2>/dev/null || exit 0

# Claude writes memory files to MEMDIR, not to the repo. Sync them in here, inside the
# lock, so every caller picks up new files — calling this script alone used to commit an
# unchanged repo and silently drop them. Project dir name = $HOME with `/` replaced by `-`,
# computed per machine (differs between iMac and MacBook), never hardcoded.
MEMDIR="$HOME/.claude/projects/$(echo "$HOME" | tr '/' '-')/memory"
if [[ ! -d "$MEMDIR" ]]; then
  echo "[$(date '+%F %T')] [$HOST] MEMDIR not found: $MEMDIR — pushing repo state only" >> "$LOG"
elif [[ "$(cd "$MEMDIR" && pwd -P)" == "$(pwd -P)" ]]; then
  # iMac: MEMDIR is a symlink to this repo, so there is one directory and nothing to copy.
  # Deleting a memory file here is a real `git rm` and the deletion propagates on push.
  :
else
  # MacBook: MEMDIR is a separate directory. Copy new files in before committing — this used
  # to live in session-end-memory.sh, so calling this script alone silently dropped them.
  # No `--delete`: a stale MEMDIR would wipe memories another machine had already pushed.
  if ! rsync -a --include="*.md" --exclude="*" "$MEMDIR/" "$REPO/" 2>>"$LOG"; then
    echo "[$(date '+%F %T')] [$HOST] rsync FAILED from $MEMDIR — pushing repo state only" >> "$LOG"
  fi
fi

git add -A
if git diff --cached --quiet; then
  # Nothing new to stage. Only exit if origin already holds every local commit — exiting
  # unconditionally used to strand a hand-made commit here, silently and with status 0.
  if [[ "$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)" == "0" ]]; then
    exit 0
  fi
  echo "[$(date '+%F %T')] [$HOST] nothing to stage, but local commits are unpushed — pushing" >> "$LOG"
elif ! git commit -m "$MSG" --quiet 2>>"$LOG"; then
  echo "[$(date '+%F %T')] [$HOST] commit FAILED (see log above) — memory left staged, not pushed" >> "$LOG"
  exit 1
fi

for attempt in 1 2 3 4 5; do
  if git push origin main --quiet 2>>"$LOG"; then
    echo "[$(date '+%F %T')] [$HOST] pushed (attempt $attempt)" >> "$LOG"
    exit 0
  fi

  git fetch origin main --quiet 2>>"$LOG"

  if git merge origin/main --no-edit --quiet 2>>"$LOG"; then
    continue
  fi

  # Real conflict outside what the union driver could resolve — never silently drop
  # either side. Keep ours under a host-tagged filename, take theirs as the tracked file.
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if [[ -f "$f" ]]; then
      dest="${f%.md}.conflict-${HOST_SAFE}-$(date +%s).md"
      cp "$f" "$dest" 2>/dev/null
      git add "$dest" 2>/dev/null
    fi
    git checkout --theirs -- "$f" 2>/dev/null
    git add "$f" 2>/dev/null
  done < <(git diff --name-only --diff-filter=U)
  git commit -m "auto: merge conflict resolved, kept both sides ($HOST) $(date '+%Y-%m-%d %H:%M')" --quiet 2>>"$LOG"
  sleep "$attempt"
done

echo "[$(date '+%F %T')] [$HOST] push FAILED after retries — manual check needed" >> "$LOG"
exit 1
