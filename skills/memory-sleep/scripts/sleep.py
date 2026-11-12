#!/usr/bin/env python3
"""Tooling for the memory-sleep skill: deterministic steps of a memory consolidation run.

Subcommands:
  survey                      find memory folders and measure their indexes (read-only)
  start   --memory DIR --minutes N   open a run: backup, snapshot, phases.log
  log     RUN "text"          append a timestamped line to phases.log, print time left
  hygiene RUN                 frontmatter, encoding, orphan files, disk usage (read-only)
  batches RUN --n K           split index entries into K batch files for parallel agents
  pairs   RUN --n 3           pick distant random pairs of memory entries for the REM phase
  turns   RUN --days 7        extract the owner's own messages (Claude Code transcripts, Codex history)
  check   RUN --candidate F   compare a candidate index with the live one, list lost facts
  install RUN --candidate F   replace the live index with the candidate (after check)
  finish  RUN                 measure the index again, print before/after

Nothing here deletes files. Every write goes either into the run folder or,
for `install` only, into the index file after a copy of it is saved.
"""

import argparse
import datetime as dt
import glob
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

INDEX = "MEMORY.md"
MAX_BYTES = 25000  # the index is loaded into every session; beyond this it is cut
MAX_LINES = 200
SHRINK_BYTES = 24000  # past this the index should be shrunk before it reaches the cut
# a link to a memory file: [title](file.md), [title](file.md#anchor), [title](<file name.md>)
LINK = re.compile(r"\[([^\]]+)\]\(<?([^)>#]+?\.md)(?:#[^)>]*)?>?\)")
ANY_LINK = re.compile(r"\]\(<?([^)>]+)>?\)")
TOKEN = re.compile(
    r"https?://[^\s)\]>,;]+"
    r"|[~A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:app|io|com|dev|org|net|ru|ai)\b[^\s)\],;]*"
    r"|\b[0-9a-f]{7,40}\b"
    r"|#\d+"
    r"|\b\d[\d.,:/\-]*\d\b"
    r"|\b\d\b"
)
DEFAULT_STATE = os.environ.get("MEMORY_SLEEP_HOME") or os.path.expanduser(
    "~/.agents/memory-sleep"
)


def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def now():
    return dt.datetime.now()


def measure(memory):
    idx = os.path.join(memory, INDEX)
    raw = open(idx, "rb").read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    links = LINK.findall(text)
    broken = sorted(
        {t for _, t in links if not os.path.exists(os.path.join(memory, t))}
    )
    files = [
        p
        for p in glob.glob(os.path.join(memory, "**", "*.md"), recursive=True)
        if os.path.basename(p) != INDEX
    ]
    return {
        "memory": memory,
        "index_bytes": len(raw),
        "index_lines": len(lines),
        "links": len(links),
        "broken_links": broken,
        "lines_over_220": sum(1 for l in lines if len(l) > 220),
        "memory_files": len(files),
        "over_limit": len(raw) > MAX_BYTES or len(lines) > MAX_LINES,
        "needs_shrink": len(raw) > SHRINK_BYTES or len(lines) > MAX_LINES,
    }


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def parse_ts(value):
    """Epoch seconds from an ISO timestamp, or None."""
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude_transcripts(memory, days, all_projects=False):
    """Recent Claude Code transcripts: the memory's own project, or every project."""
    projects = os.path.realpath(os.path.join(claude_home(), "projects"))
    own = os.path.dirname(os.path.realpath(memory)) if memory else None
    if own and os.path.dirname(own) == projects and not all_projects:
        dirs = [own]
    else:
        dirs = glob.glob(os.path.join(projects, "*"))
    cutoff = now().timestamp() - days * 86400
    return [
        p
        for d in dirs
        for p in glob.glob(os.path.join(d, "*.jsonl"))
        if os.path.getmtime(p) >= cutoff
    ]


def memory_candidates():
    """Folders where an agent's file memory is known to live."""
    found = glob.glob(os.path.join(claude_home(), "projects", "*", "memory"))
    found += [
        os.path.expanduser("~/.agents/memory"),
        os.path.join(codex_home(), "memory"),
    ]
    return sorted(m for m in found if os.path.exists(os.path.join(m, INDEX)))


def cmd_survey(a):
    out = []
    for m in [a.memory] if a.memory else memory_candidates():
        m = os.path.realpath(os.path.expanduser(m))
        if not os.path.exists(os.path.join(m, INDEX)):
            continue
        d = measure(m)
        d["transcripts_7d"] = len(claude_transcripts(m, 7))
        out.append(d)
    runs = sorted(
        glob.glob(os.path.join(a.state, "runs", "*", "meta.json")), key=os.path.getmtime
    )
    last = json.load(open(runs[-1])) if runs else None
    cfg = os.path.join(a.state, "config.json")
    print(
        json.dumps(
            {
                "memories": out,
                "sources": {
                    "claude_code": os.path.isdir(os.path.join(claude_home(), "projects")),
                    "codex": os.path.isdir(codex_home()),
                    "codex_turns_7d": sum(len(v) for v in codex_turns(7, 1500).values()),
                },
                "limits": {"bytes": MAX_BYTES, "lines": MAX_LINES, "shrink_at_bytes": SHRINK_BYTES},
                "last_run": last,
                "config": json.load(open(cfg)) if os.path.exists(cfg) else None,
                "state_dir": a.state,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


def cmd_start(a):
    memory = None
    if not a.turns_only:
        if not a.memory:
            sys.exit("--memory is required (or pass --turns-only to run without a memory folder)")
        # realpath: a memory folder reached through a symlink must be archived as files
        memory = os.path.realpath(os.path.expanduser(a.memory))
        if not os.path.exists(os.path.join(memory, INDEX)):
            sys.exit(f"no {INDEX} in {memory}")
    base = os.path.join(a.state, "runs", now().strftime("%Y-%m-%d"))
    run, n = base, 2
    while os.path.exists(run):
        run, n = f"{base}-{n}", n + 1
    try:
        os.makedirs(run)
    except OSError as e:
        sys.exit(f"cannot write to {a.state}: {e}. Pass --state <writable folder>.")
    m = None
    if memory:
        archive = os.path.join(run, "memory-before.tgz")
        with tarfile.open(archive, "w:gz", dereference=True) as tf:
            tf.add(memory, arcname=".")
        with tarfile.open(archive) as tf:
            archived = sum(1 for x in tf.getmembers() if x.isfile())
        on_disk = sum(len(files) for _, _, files in os.walk(memory, followlinks=True))
        if archived < on_disk:
            sys.exit(f"backup is incomplete: {archived} files archived, {on_disk} on disk")
        shutil.copy2(os.path.join(memory, INDEX), os.path.join(run, "MEMORY.before.md"))
        m = measure(memory)
        json.dump(
            m,
            open(os.path.join(run, "metrics-before.json"), "w"),
            ensure_ascii=False,
            indent=1,
        )
    start = now()
    deadline = start + dt.timedelta(minutes=a.minutes)
    meta = {
        "memory": memory,
        "started": start.isoformat(timespec="seconds"),
        "minutes": a.minutes,
        "deadline": deadline.isoformat(timespec="seconds"),
        "run": run,
    }
    json.dump(meta, open(os.path.join(run, "meta.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(run, "phases.log"), "w", encoding="utf-8") as f:
        if m:
            f.write(
                f"{start:%H:%M} N1: backup memory-before.tgz ({archived} files), "
                f"index {m['index_bytes']} bytes, {m['index_lines']} lines, {m['links']} links, "
                f"{len(m['broken_links'])} broken, {m['lines_over_220']} long lines; "
                f"deadline {deadline:%H:%M}\n"
            )
        else:
            f.write(f"{start:%H:%M} N1: no memory folder, turns only; deadline {deadline:%H:%M}\n")
    keep_awake = None
    if platform.system() == "Darwin":
        keep_awake = f"caffeinate -i -t {a.minutes * 60 + 120}"
    elif shutil.which("systemd-inhibit"):
        keep_awake = f"systemd-inhibit --what=idle sleep {a.minutes * 60 + 120}"
    print(
        json.dumps(
            {**meta, "metrics": m, "keep_awake_command": keep_awake},
            ensure_ascii=False,
            indent=1,
        )
    )


def load_meta(run, need_memory=True):
    meta = json.load(open(os.path.join(run, "meta.json")))
    if need_memory and not meta.get("memory"):
        sys.exit("this run was started with --turns-only: there is no memory folder to work on")
    return meta


def cmd_log(a):
    meta = load_meta(a.run, need_memory=False)
    t = now()
    with open(os.path.join(a.run, "phases.log"), "a", encoding="utf-8") as f:
        f.write(f"{t:%H:%M} {a.text}\n")
    start = dt.datetime.fromisoformat(meta["started"])
    end = dt.datetime.fromisoformat(meta["deadline"])
    print(
        f"elapsed {int((t - start).total_seconds() // 60)} min, "
        f"left {int((end - t).total_seconds() // 60)} min"
    )


def cmd_hygiene(a):
    memory = load_meta(a.run)["memory"]
    index_text = read(os.path.join(memory, INDEX))
    linked = {os.path.normpath(t) for _, t in LINK.findall(index_text)}
    rep = {
        "no_frontmatter": [],
        "missing_fields": [],
        "not_utf8": [],
        "not_in_index": [],
        "empty": [],
    }
    for p in sorted(glob.glob(os.path.join(memory, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, memory)
        if rel == INDEX:
            continue
        raw = open(p, "rb").read()
        if not raw.strip():
            rep["empty"].append(rel)
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rep["not_utf8"].append(rel)
            text = raw.decode("utf-8", errors="replace")
        if (
            os.sep not in rel
        ):  # top-level memory files are expected to carry frontmatter
            if not text.startswith("---"):
                rep["no_frontmatter"].append(rel)
            else:
                head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
                miss = [
                    k
                    for k in ("name", "description", "type")
                    if not re.search(rf"^\s*{k}:", head, re.M)
                ]
                if miss:
                    rep["missing_fields"].append(f"{rel}: {', '.join(miss)}")
            if os.path.normpath(rel) not in linked:
                rep["not_in_index"].append(rel)
    usage = []
    for root in (claude_home(), codex_home()):
        if not os.path.isdir(root):
            continue
        try:
            res = subprocess.run(
                ["du", "-sk"] + sorted(glob.glob(os.path.join(root, "*"))),
                capture_output=True,
                text=True,
                timeout=120,
            )
            rows = [l.split("\t") for l in res.stdout.strip().split("\n") if "\t" in l]
            rows.sort(key=lambda r: -int(r[0]))
            usage += [
                f"{int(k) // 1024} MB  {os.path.basename(root)}/{os.path.relpath(q, root)}"
                for k, q in rows[:5]
            ]
        except Exception as e:  # measurement only; a failure here must not stop the run
            usage.append(f"du failed for {os.path.basename(root)}: {e}")
    rep["disk_usage_top"] = usage
    json.dump(
        rep,
        open(os.path.join(a.run, "hygiene.json"), "w"),
        ensure_ascii=False,
        indent=1,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=1))


def index_entries(memory, section=None):
    """Lines of the index that link to a memory file, with their section heading."""
    out, sec = [], ""
    for line in read(os.path.join(memory, INDEX)).split("\n"):
        if line.startswith("#"):
            sec = line.strip()
            continue
        m = LINK.search(line)
        if m and (not section or section.lower() in sec.lower()):
            out.append(
                {
                    "section": sec,
                    "line": line,
                    "target": m.group(2),
                    "title": m.group(1),
                }
            )
    return out


def cmd_batches(a):
    memory = load_meta(a.run)["memory"]
    entries = index_entries(memory, a.section)
    loose = [
        l
        for l in read(os.path.join(memory, INDEX)).split("\n")
        if l.strip().startswith("-") and not LINK.search(l)
    ]
    n = max(1, min(a.n, len(entries)))
    files = []
    for i in range(n):
        part = entries[i::n]
        path = os.path.join(a.run, f"{a.prefix}{i + 1}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"memory dir: {memory}\n\n")
            for e in part:
                f.write(f"{e['section']}\n{e['line']}\n\n")
        files.append({"file": path, "entries": len(part)})
    if loose:
        with open(
            os.path.join(a.run, f"{a.prefix}-unlinked.txt"), "w", encoding="utf-8"
        ) as f:
            f.write("\n".join(loose) + "\n")
    print(
        json.dumps(
            {"batches": files, "unlinked_lines": len(loose)},
            ensure_ascii=False,
            indent=1,
        )
    )


def words(s):
    return {w for w in re.findall(r"[\w-]{4,}", s.lower())}


def cmd_pairs(a):
    memory = load_meta(a.run)["memory"]
    entries = [
        e
        for e in index_entries(memory, a.section)
        if os.path.exists(os.path.join(memory, e["target"]))
    ]
    if len(entries) < 4:
        sys.exit("fewer than 4 linked entries: REM phase has nothing to recombine")
    seed = a.seed if a.seed is not None else random.randrange(10**6)
    rng = random.Random(seed)
    cands = []
    for _ in range(60):
        x, y = rng.sample(entries, 2)
        wx, wy = words(x["line"]), words(y["line"])
        overlap = len(wx & wy) / max(1, len(wx | wy))
        cands.append((overlap, x, y))
    cands.sort(key=lambda c: c[0])
    picked, used = [], set()
    for _, x, y in cands:
        if x["target"] in used or y["target"] in used:
            continue
        picked.append((x, y))
        used |= {x["target"], y["target"]}
        if len(picked) == a.n:
            break
    path = os.path.join(a.run, "pairs.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"seed {seed}; memory dir: {memory}\n\n")
        for i, (x, y) in enumerate(picked, 1):
            f.write(
                f"{i}. {x['title']} ({x['target']}) + {y['title']} ({y['target']})\n"
            )
    print(read(path))


SKIP_PREFIX = (
    "<",
    "[Subagent",
    "Another Claude session",
    "This session is being continued",
    "Caveat:",
    "[Image",
    "[Request interrupted",
    "# AGENTS.md instructions",
)


def keep_turn(t, max_chars):
    """Text of an owner's message to keep (long ones are cut, not dropped), or None."""
    t = (t or "").strip()
    if not t or t.startswith(SKIP_PREFIX):
        return None
    if len(t) > max_chars:
        return t[:max_chars] + f" [cut, {len(t)} chars in the original]"
    return t


def claude_turns(path, max_chars, cutoff):
    """The owner's own messages from one Claude Code transcript, inside the time window."""
    got = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"type":"user"' not in line and '"type": "user"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "user":
                continue
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            ts = parse_ts(d.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue
            c = (d.get("message") or {}).get("content")
            texts = (
                [c]
                if isinstance(c, str)
                else [
                    b.get("text", "")
                    for b in (c or [])
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
            )
            for t in texts:
                t = keep_turn(t, max_chars)
                if t:
                    got.append((str(d.get("timestamp", ""))[:10], t))
    return got


def codex_turns(days, max_chars):
    """The owner's own messages from Codex CLI: prompt history plus session rollouts, deduplicated."""
    cutoff = now().timestamp() - days * 86400
    sessions, seen = {}, set()

    def add(sid, ts, text):
        t = keep_turn(text, max_chars)
        # history and rollouts record the same prompt: keep one copy per session,
        # but let a prompt repeated in another session through (a repeat is a signal)
        if not t or (str(sid), t) in seen:
            return
        seen.add((str(sid), t))
        date = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        sessions.setdefault(str(sid), []).append((date, t))

    path = os.path.join(codex_home(), "history.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = d.get("ts")
                if isinstance(ts, (int, float)) and ts >= cutoff:
                    add(d.get("session_id", ""), ts, str(d.get("text") or ""))

    pattern = os.path.join(codex_home(), "sessions", "**", "*.jsonl")
    for p in sorted(glob.glob(pattern, recursive=True)):
        if os.path.getmtime(p) < cutoff:
            continue
        sid, rows, subagent = os.path.basename(p)[-41:-6], [], False
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if d.get("type") == "session_meta":
                    sid = str(pl.get("id") or sid)
                    # sessions spawned by another agent carry that agent's prompt, not the owner's
                    if pl.get("parent_thread_id") or pl.get("forked_from_id") or pl.get("agent_role"):
                        subagent = True
                        break
                if d.get("type") != "response_item" or pl.get("type") != "message":
                    continue
                if pl.get("role") != "user":
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                for b in pl.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "input_text":
                        rows.append((ts, b.get("text", "")))
        if not subagent:
            for ts, text in rows:
                add(sid, ts, text)
    return sessions


def cmd_turns(a):
    memory = load_meta(a.run, need_memory=False)["memory"]
    cutoff = now().timestamp() - a.days * 86400
    out_path = os.path.join(a.run, "user-turns.md")
    sessions = []  # (source, label, [(date, text)])
    n_files = 0
    if a.source in ("auto", "claude"):
        files = claude_transcripts(memory, a.days, a.all_projects)
        n_files = len(files)
        for p in sorted(files, key=os.path.getmtime):
            got = claude_turns(p, a.max_chars, cutoff)
            if got:
                label = f"{os.path.basename(p)[:8]} ({os.path.basename(os.path.dirname(p))})"
                sessions.append(("claude", label, got))
    if a.source in ("auto", "codex"):
        for sid, got in codex_turns(a.days, a.max_chars).items():
            sessions.append(("codex", sid[:8], got))

    # A long text that shows up in three or more sessions is a template some tool sends,
    # not something the owner typed. Short repeats stay: a repeated correction is a signal.
    seen_in = {}
    for i, (_, _, got) in enumerate(sessions):
        for _, t in got:
            seen_in.setdefault(t, set()).add(i)
    templates = {t for t, s in seen_in.items() if len(s) >= 3 and len(t) > 200}

    stats = {
        "claude": {"transcripts": n_files, "sessions": 0, "turns": 0},
        "codex": {"sessions": 0, "turns": 0},
        "dropped": {"templates": 0, "duplicates": 0},
    }
    with open(out_path, "w", encoding="utf-8") as out:
        for source, label, got in sessions:
            kept, local = [], set()
            for date, t in got:
                if t in templates:
                    stats["dropped"]["templates"] += 1
                elif t in local:  # the same message stored twice inside one session
                    stats["dropped"]["duplicates"] += 1
                else:
                    local.add(t)
                    kept.append((date, t))
            if not kept:
                continue
            stats[source]["sessions"] += 1
            stats[source]["turns"] += len(kept)
            out.write(f"\n## {source} session {label}\n")
            for date, t in kept:
                out.write(f"- [{date}] {t}\n")
    print(
        json.dumps(
            {
                "file": out_path,
                **stats,
                "turns": stats["claude"]["turns"] + stats["codex"]["turns"],
                "bytes": os.path.getsize(out_path),
            },
            ensure_ascii=False,
        )
    )


def structure(text):
    heads = [l.strip() for l in text.split("\n") if l.startswith("#")]
    targets = sorted({x.strip() for x in ANY_LINK.findall(text)})  # every link, any syntax
    return heads, targets


def has_token(tok, text):
    """True if tok stands in text as a whole identifier, not as a part of a longer one."""
    return re.search(rf"(?<![\w]){re.escape(tok)}(?![\w])", text) is not None


def lost_facts(memory, old, new):
    """Identifiers (paths, URLs, numbers, hashes) that left the index and are not in the linked file."""
    new_by_target = {}
    for line in new.split("\n"):
        for _, t in LINK.findall(line):
            new_by_target[t] = new_by_target.get(t, "") + line + "\n"
    lost = []
    cache = {}
    for line in old.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        m = LINK.search(line)
        target = m.group(2) if m else None
        body = ANY_LINK.sub("]", line)
        scope = new_by_target.get(target, "") if target else new
        if target and target not in cache:
            p = os.path.join(memory, target)
            cache[target] = read(p) if os.path.exists(p) else ""
        for tok in sorted({t.rstrip(".,;:)") for t in TOKEN.findall(body)}):
            if not tok or has_token(tok, scope):
                continue
            if target and has_token(tok, cache[target]):
                continue
            lost.append({"fact": tok, "target": target, "old_line": line[:200]})
    return lost


def do_check(run, candidate, max_bytes, max_line):
    memory = load_meta(run)["memory"]
    old = read(os.path.join(memory, INDEX))
    raw = open(candidate, "rb").read()
    try:
        new, utf8_ok = raw.decode("utf-8"), True
    except UnicodeDecodeError:
        new, utf8_ok = raw.decode("utf-8", errors="replace"), False
    oh, ot = structure(old)
    nh, nt = structure(new)
    local = sorted({t for _, t in LINK.findall(new)})
    rep = {
        "utf8_ok": utf8_ok,
        "bytes": len(raw),
        "bytes_ok": len(raw) <= max_bytes,
        "lines": len(new.split("\n")),
        "lines_ok": len(new.split("\n")) <= MAX_LINES,
        "sections_same": oh == nh,
        "links_dropped": sorted(set(ot) - set(nt)),
        "links_added": sorted(set(nt) - set(ot)),
        "links_broken": [t for t in local if not os.path.exists(os.path.join(memory, t))],
        "long_lines": [l[:80] for l in new.split("\n") if len(l) > max_line],
        "lost_facts": lost_facts(memory, old, new),
    }
    rep["ok"] = (
        rep["utf8_ok"]
        and rep["bytes_ok"]
        and rep["lines_ok"]
        and rep["sections_same"]
        and not rep["links_dropped"]
        and not rep["links_broken"]
        and not rep["lost_facts"]
    )
    return memory, rep


def cmd_check(a):
    _, rep = do_check(a.run, a.candidate, a.max_bytes, a.max_line)
    json.dump(
        rep, open(os.path.join(a.run, "check.json"), "w"), ensure_ascii=False, indent=1
    )
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    sys.exit(0 if rep["ok"] else 1)


def cmd_install(a):
    memory, rep = do_check(a.run, a.candidate, a.max_bytes, a.max_line)
    hard = [
        k
        for k in ("utf8_ok", "bytes_ok", "lines_ok", "sections_same")
        if not rep[k]
    ] + [k for k in ("links_dropped", "links_broken") if rep[k]]
    if hard or (rep["lost_facts"] and not a.accept_report):
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        sys.exit(
            "check failed"
            + (f" ({', '.join(hard)})" if hard else "")
            + ": fix the candidate. --accept-report only waives lost_facts, "
            "after each of them is resolved by hand."
        )
    live = os.path.join(memory, INDEX)
    n = 1
    while os.path.exists(os.path.join(a.run, f"MEMORY.pre-install-{n}.md")):
        n += 1
    shutil.copy2(live, os.path.join(a.run, f"MEMORY.pre-install-{n}.md"))
    fd, tmp = tempfile.mkstemp(dir=memory, prefix=".MEMORY.", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(open(a.candidate, "rb").read())
    os.replace(tmp, live)
    print(
        f"installed: {os.path.getsize(live)} bytes; previous copy MEMORY.pre-install-{n}.md"
    )


def cmd_finish(a):
    meta = load_meta(a.run, need_memory=False)
    minutes = int((now() - dt.datetime.fromisoformat(meta["started"])).total_seconds() // 60)
    if not meta.get("memory"):
        print(json.dumps({"minutes": minutes, "run": a.run, "memory": None}, indent=1))
        return
    before = json.load(open(os.path.join(a.run, "metrics-before.json")))
    after = measure(meta["memory"])
    json.dump(
        after,
        open(os.path.join(a.run, "metrics-after.json"), "w"),
        ensure_ascii=False,
        indent=1,
    )
    keys = ["index_bytes", "index_lines", "links", "lines_over_220", "memory_files"]
    print(
        json.dumps(
            {
                "before_after": {k: [before[k], after[k]] for k in keys},
                "broken_links": [
                    len(before["broken_links"]),
                    len(after["broken_links"]),
                ],
                "minutes": int(
                    (now() - dt.datetime.fromisoformat(meta["started"])).total_seconds()
                    // 60
                ),
                "run": a.run,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--state", default=DEFAULT_STATE, help="where runs and config live")
    s = p.add_subparsers(dest="cmd", required=True)

    x = s.add_parser("survey")
    x.add_argument("--memory")
    x.set_defaults(f=cmd_survey)
    x = s.add_parser("start")
    x.add_argument("--memory")
    x.add_argument("--turns-only", action="store_true")
    x.add_argument("--minutes", type=int, default=30)
    x.set_defaults(f=cmd_start)
    x = s.add_parser("log")
    x.add_argument("run")
    x.add_argument("text")
    x.set_defaults(f=cmd_log)
    x = s.add_parser("hygiene")
    x.add_argument("run")
    x.set_defaults(f=cmd_hygiene)
    x = s.add_parser("batches")
    x.add_argument("run")
    x.add_argument("--n", type=int, default=3)
    x.add_argument("--section")
    x.add_argument("--prefix", default="batch")
    x.set_defaults(f=cmd_batches)
    x = s.add_parser("pairs")
    x.add_argument("run")
    x.add_argument("--n", type=int, default=3)
    x.add_argument("--section")
    x.add_argument("--seed", type=int)
    x.set_defaults(f=cmd_pairs)
    x = s.add_parser("turns")
    x.add_argument("run")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("--all-projects", action="store_true")
    x.add_argument("--source", choices=["auto", "claude", "codex"], default="auto")
    x.add_argument("--max-chars", type=int, default=1500)
    x.set_defaults(f=cmd_turns)
    for name, fn in (("check", cmd_check), ("install", cmd_install)):
        x = s.add_parser(name)
        x.add_argument("run")
        x.add_argument("--candidate", required=True)
        x.add_argument("--max-bytes", type=int, default=SHRINK_BYTES)
        x.add_argument("--max-line", type=int, default=220)
        if name == "install":
            x.add_argument("--accept-report", action="store_true")
        x.set_defaults(f=fn)
    x = s.add_parser("finish")
    x.add_argument("run")
    x.set_defaults(f=cmd_finish)

    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
