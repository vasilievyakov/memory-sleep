#!/usr/bin/env python3
"""Tooling for the memory-sleep skill: deterministic steps of a memory consolidation run.

Subcommands:
  survey                      find memory folders and measure their indexes (read-only)
  start   --memory DIR --minutes N   open a run: backup, snapshot, phases.log
  log     RUN "text"          append a timestamped line to phases.log, print time left
  hygiene RUN                 frontmatter, encoding, orphan files, disk usage (read-only)
  batches RUN --n K           split index entries into K batch files for parallel agents
  pairs   RUN --n 3           pick distant random pairs of memory entries for the REM phase
  turns   RUN --days 7        extract the owner's own messages from recent transcripts
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
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+\.md)\)")
TOKEN = re.compile(
    r"https?://[^\s)\]>,;]+"
    r"|~?/[\w.\-/]+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:app|io|com|dev|org|net|ru|ai)\b[^\s)\],;]*"
    r"|\b[0-9a-f]{7,40}\b"
    r"|#\d+"
    r"|\b\d[\d.,:/\-]*\d\b"
)
DEFAULT_STATE = os.path.expanduser("~/.claude/memory-sleep")


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
    }


def recent_transcripts(project_dir, days):
    cutoff = now().timestamp() - days * 86400
    return [
        p
        for p in glob.glob(os.path.join(project_dir, "*.jsonl"))
        if os.path.getmtime(p) >= cutoff
    ]


def cmd_survey(a):
    found = (
        [a.memory]
        if a.memory
        else sorted(
            os.path.dirname(p)
            for p in glob.glob(
                os.path.expanduser(f"~/.claude/projects/*/memory/{INDEX}")
            )
        )
    )
    out = []
    for m in found:
        m = os.path.abspath(os.path.expanduser(m))
        if not os.path.exists(os.path.join(m, INDEX)):
            continue
        d = measure(m)
        d["transcripts_7d"] = len(recent_transcripts(os.path.dirname(m), 7))
        out.append(d)
    runs = sorted(glob.glob(os.path.join(a.state, "runs", "*", "meta.json")))
    last = json.load(open(runs[-1])) if runs else None
    cfg = os.path.join(a.state, "config.json")
    print(
        json.dumps(
            {
                "memories": out,
                "limits": {"bytes": MAX_BYTES, "lines": MAX_LINES},
                "last_run": last,
                "config": json.load(open(cfg)) if os.path.exists(cfg) else None,
                "state_dir": a.state,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


def cmd_start(a):
    memory = os.path.abspath(os.path.expanduser(a.memory))
    if not os.path.exists(os.path.join(memory, INDEX)):
        sys.exit(f"no {INDEX} in {memory}")
    base = os.path.join(a.state, "runs", now().strftime("%Y-%m-%d"))
    run, n = base, 2
    while os.path.exists(run):
        run, n = f"{base}-{n}", n + 1
    os.makedirs(run)
    with tarfile.open(os.path.join(run, "memory-before.tgz"), "w:gz") as tf:
        tf.add(memory, arcname=".")
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
    json.dump(
        meta, open(os.path.join(run, "meta.json"), "w"), ensure_ascii=False, indent=1
    )
    with open(os.path.join(run, "phases.log"), "w", encoding="utf-8") as f:
        f.write(
            f"{start:%H:%M} N1: backup memory-before.tgz, index {m['index_bytes']} bytes, "
            f"{m['index_lines']} lines, {m['links']} links, {len(m['broken_links'])} broken, "
            f"{m['lines_over_220']} long lines; deadline {deadline:%H:%M}\n"
        )
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


def load_meta(run):
    return json.load(open(os.path.join(run, "meta.json")))


def cmd_log(a):
    meta = load_meta(a.run)
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
    root = os.path.expanduser("~/.claude")
    try:
        res = subprocess.run(
            ["du", "-sk"] + sorted(glob.glob(os.path.join(root, "*"))),
            capture_output=True,
            text=True,
            timeout=120,
        )
        rows = [l.split("\t") for l in res.stdout.strip().split("\n") if "\t" in l]
        rows.sort(key=lambda r: -int(r[0]))
        usage = [
            f"{int(k) // 1024} MB  {os.path.relpath(p, root)}" for k, p in rows[:8]
        ]
    except Exception as e:  # measurement only; a failure here must not stop the run
        usage = [f"du failed: {e}"]
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
)


def cmd_turns(a):
    memory = load_meta(a.run)["memory"]
    proj = os.path.dirname(memory)
    dirs = (
        glob.glob(os.path.join(os.path.dirname(proj), "*"))
        if a.all_projects
        else [proj]
    )
    files = [p for d in dirs for p in recent_transcripts(d, a.days)]
    out_path = os.path.join(a.run, "user-turns.md")
    sessions = turns = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for p in sorted(files, key=os.path.getmtime):
            got = []
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        d.get("type") != "user"
                        or d.get("isSidechain")
                        or d.get("isMeta")
                    ):
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
                        t = t.strip()
                        if not t or len(t) > a.max_chars or t.startswith(SKIP_PREFIX):
                            continue
                        got.append((d.get("timestamp", "")[:10], t))
            if got:
                sessions += 1
                turns += len(got)
                out.write(
                    f"\n## session {os.path.basename(p)[:8]} ({os.path.basename(os.path.dirname(p))})\n"
                )
                for date, t in got:
                    out.write(f"- [{date}] {t}\n")
    print(
        json.dumps(
            {
                "file": out_path,
                "transcripts": len(files),
                "sessions_with_turns": sessions,
                "turns": turns,
                "bytes": os.path.getsize(out_path),
            },
            ensure_ascii=False,
        )
    )


def structure(text):
    heads = [l.strip() for l in text.split("\n") if l.startswith("#")]
    targets = sorted({t for _, t in LINK.findall(text)})
    return heads, targets


def lost_facts(memory, old, new):
    """Identifiers (paths, URLs, numbers, hashes) that left the index and are not in the linked file."""
    new_by_target = {}
    for line in new.split("\n"):
        for _, t in LINK.findall(line):
            new_by_target.setdefault(t, "")
            new_by_target[t] += line + "\n"
    lost = []
    cache = {}
    for line in old.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        m = LINK.search(line)
        target = m.group(2) if m else None
        body = LINK.sub(lambda mm: mm.group(1), line)
        scope = new_by_target.get(target, "") if target else new
        if target and target not in cache:
            p = os.path.join(memory, target)
            cache[target] = read(p) if os.path.exists(p) else ""
        for tok in {t.rstrip(".,;:)") for t in TOKEN.findall(body)}:
            if len(tok) < 3 or tok in scope:
                continue
            if target and tok in cache[target]:
                continue
            if not target and tok in new:
                continue
            lost.append({"fact": tok, "target": target, "old_line": line[:200]})
    return lost


def do_check(run, candidate, max_bytes, max_line):
    memory = load_meta(run)["memory"]
    old = read(os.path.join(memory, INDEX))
    raw = open(candidate, "rb").read()
    new = raw.decode("utf-8", errors="replace")
    oh, ot = structure(old)
    nh, nt = structure(new)
    rep = {
        "bytes": len(raw),
        "bytes_ok": len(raw) <= max_bytes,
        "lines": len(new.split("\n")),
        "lines_ok": len(new.split("\n")) <= MAX_LINES,
        "sections_same": oh == nh,
        "links_dropped": sorted(set(ot) - set(nt)),
        "links_added": sorted(set(nt) - set(ot)),
        "links_broken": [t for t in nt if not os.path.exists(os.path.join(memory, t))],
        "long_lines": [l[:80] for l in new.split("\n") if len(l) > max_line],
        "lost_facts": lost_facts(memory, old, new),
    }
    rep["ok"] = (
        rep["bytes_ok"]
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
    if not rep["ok"] and not a.accept_report:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        sys.exit(
            "check failed: fix the candidate, or pass --accept-report after each item is resolved by hand"
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
    meta = load_meta(a.run)
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
    x.add_argument("--memory", required=True)
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
    x.add_argument("--max-chars", type=int, default=1500)
    x.set_defaults(f=cmd_turns)
    for name, fn in (("check", cmd_check), ("install", cmd_install)):
        x = s.add_parser(name)
        x.add_argument("run")
        x.add_argument("--candidate", required=True)
        x.add_argument("--max-bytes", type=int, default=24000)
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
