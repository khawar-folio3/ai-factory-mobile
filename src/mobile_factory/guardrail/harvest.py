from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import FactoryError
from ..integrations.github import gh, gh_json

_THREADS_QUERY = """query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $n) { author { login }
    reviewThreads(first: 100) { nodes { isResolved isOutdated
      comments(first: 30) { nodes { databaseId author { login } body } } } } } } }"""


def _bot(user: dict[str, Any] | None) -> bool:
    return not user or user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def _pr_comments(root: Path, repo: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
    n = pr["number"]
    reviews = json.loads(gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{n}/reviews") or "[]")
    comments = json.loads(gh(root, "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{n}/comments") or "[]")
    reviews = [r for page in reviews for r in page]
    comments = [c for page in comments for c in page]
    states = {r["id"]: r["state"] for r in reviews}
    common = {
        "pr": n,
        "base": pr["baseRefName"],
        "title": pr["title"],
        "pr_author": (pr.get("author") or {}).get("login", ""),
        "merged_at": pr["mergedAt"],
    }
    out = []
    for c in comments:
        if _bot(c.get("user")):
            continue
        out.append(
            {
                "id": f"c{c['id']}",
                **common,
                "kind": "inline",
                "reviewer": c["user"]["login"],
                "state": states.get(c.get("pull_request_review_id"), "COMMENTED"),
                "path": c.get("path"),
                "line": c.get("line") or c.get("original_line"),
                "diff_hunk": "\n".join((c.get("diff_hunk") or "").splitlines()[-8:]),
                "body": c.get("body") or "",
                "created_at": c.get("created_at"),
            }
        )
    for r in reviews:
        if _bot(r.get("user")) or not (r.get("body") or "").strip():
            continue
        out.append(
            {
                "id": f"r{r['id']}",
                **common,
                "kind": "review",
                "reviewer": r["user"]["login"],
                "state": r["state"],
                "path": None,
                "line": None,
                "diff_hunk": "",
                "body": r["body"],
                "created_at": r.get("submitted_at"),
            }
        )
    return [c for c in out if c["reviewer"] != c["pr_author"] and len(c["body"]) >= 15]


def _threads(root: Path, repo: str, n: int) -> dict[str, dict[str, Any]]:
    owner, name = repo.split("/", 1)
    try:
        d = gh_json(
            root,
            "api",
            "graphql",
            "-f",
            f"query={_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"n={n}",
        )
    except FactoryError:
        return {}
    pr = d["data"]["repository"]["pullRequest"]
    author = (pr.get("author") or {}).get("login")
    res: dict[str, dict[str, Any]] = {}
    for t in pr["reviewThreads"]["nodes"]:
        cs = [
            {
                "id": c["databaseId"],
                "by": "author" if (c.get("author") or {}).get("login") == author else "reviewer",
                "body": c["body"][:300],
            }
            for c in t["comments"]["nodes"]
        ]
        for i, c in enumerate(cs):
            res[f"c{c['id']}"] = {
                "resolved": t["isResolved"],
                "outdated": t["isOutdated"],
                "replies": [{"by": x["by"], "body": x["body"]} for x in cs[i + 1 :]],
            }
    return res


def codeowners(root: Path) -> list[str]:
    for f in (root / ".github/CODEOWNERS", root / "CODEOWNERS", root / "docs/CODEOWNERS"):
        if f.is_file():
            names = {
                tok[1:]
                for ln in f.read_text().splitlines()
                if not ln.strip().startswith("#")
                for tok in ln.split()
                if tok.startswith("@") and "/" not in tok
            }
            return sorted(names)
    return []


def harvest(
    root: Path,
    repo: str,
    data_dir: Path,
    *,
    limit: int = 300,
    bases: list[str] | None = None,
    owners: list[str] | None = None,
    since: str = "",
    jobs: int = 8,
    min_prs: int = 20,
    progress: Callable[[str, int, int], None] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Incremental by default: PRs in processed_prs.json are never fetched again. `full` invalidates everything first."""
    tick = progress or (lambda phase, done, total: None)
    data_dir.mkdir(parents=True, exist_ok=True)
    record_file = data_dir / "processed_prs.json"
    record: dict[str, Any] = json.loads(record_file.read_text()) if record_file.is_file() and not full else {}
    if owners and record.get("owners") and {o.lower() for o in owners} != {o.lower() for o in record["owners"]}:
        record, full = {}, True  # other reviewers' comments were never kept: an incremental run would miss them
    if full:
        invalidate(data_dir)
    done_prs: dict[str, str] = record.get("prs", {})
    tick("Listing merged PRs", 0, 0)
    prs: dict[int, dict[str, Any]] = {}
    for b in bases or [""]:
        args = [
            "pr",
            "list",
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,title,author,mergedAt,baseRefName",
        ]
        if b:
            args += ["--base", b]
        for p in gh_json(root, *args) or []:
            if p["mergedAt"][:10] >= since:
                prs[p["number"]] = p

    new = {n: p for n, p in prs.items() if str(n) not in done_prs}
    raw: list[dict[str, Any]] = []
    tick("Reading review comments", 0, len(new))
    with ThreadPoolExecutor(jobs) as ex:
        for i, batch in enumerate(ex.map(lambda p: _pr_comments(root, repo, p), new.values()), 1):
            raw += batch
            tick("Reading review comments", i, len(new))

    source = "flag"
    if not owners and record.get("owners"):
        owners, source = record["owners"], record.get("owner_source", "recorded")
    if not owners:
        owners, source = codeowners(root), "CODEOWNERS"
    if not owners:
        counts = Counter(c["reviewer"] for c in raw)
        top = counts.most_common(1)[0][1] if counts else 0
        owners, source = [r for r, n in counts.most_common(3) if n * 5 >= top], "top-reviewers"
    if not owners:
        raise FactoryError("no code owners could be determined; pass --owners")
    wanted = {o.lower() for o in owners}
    kept = [c for c in raw if c["reviewer"].lower() in wanted]

    inline_prs = sorted({c["pr"] for c in kept if c["kind"] == "inline"})
    tick("Resolving review threads", 0, len(inline_prs))
    with ThreadPoolExecutor(jobs) as ex:
        threads: dict[str, dict[str, Any]] = {}
        for i, t in enumerate(ex.map(lambda n: _threads(root, repo, n), inline_prs), 1):
            threads.update(t)
            tick("Resolving review threads", i, len(inline_prs))
    for c in kept:
        c["resolution"] = threads.get(c["id"])

    out = data_dir / "reviews.jsonl"
    merged = {c["id"]: c for c in _read_jsonl(out)}  # invalidate() already emptied it for a full run
    merged.update({c["id"]: c for c in kept})
    rows = sorted(merged.values(), key=lambda c: c["created_at"] or "")
    out.write_text("".join(json.dumps(c) + "\n" for c in rows))

    reviewed = len({c["pr"] for c in rows})
    meta = {
        "repo": repo,
        "harvested_at": datetime.now(UTC).date().isoformat(),
        "since": since,
        "bases": bases or [],
        "owners": owners,
        "owner_source": source,
        "prs_scanned": len(prs),
        "prs_new": len(new),
        "prs_already_processed": len(prs) - len(new),
        "full": full,
        "prs_with_owner_comments": reviewed,
        "comments": len(rows),
        "changes_requested": sum(1 for c in rows if c["state"] == "CHANGES_REQUESTED"),
    }
    (data_dir / "harvest_meta.json").write_text(json.dumps(meta, indent=2))
    done_prs.update({str(n): p["mergedAt"][:10] for n, p in new.items()})
    record_file.write_text(
        json.dumps({"repo": repo, "owners": owners, "owner_source": source, "prs": done_prs}, indent=1) + "\n"
    )
    if reviewed < min_prs:
        raise FactoryError(f"only {reviewed} PRs with owner comments (< {min_prs}): not enough history to learn from")
    return meta


def invalidate(data_dir: Path) -> None:
    """Forget every harvested comment and processed PR so the next harvest extracts from scratch."""
    for name in ("reviews.jsonl", "processed_prs.json", "harvest_meta.json", DISTILLED, "reviews-new.jsonl"):
        (data_dir / name).unlink(missing_ok=True)
    for tally in data_dir.glob("tally-*.json"):
        tally.unlink()


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.is_file() else []


MAX_PARALLEL = 16  # default for agents.max_parallel
MIN_CHUNK = 25  # comments per tally: smaller chunks cost more in startup than they save


def chunks(
    data_dir: Path, min_size: int = MIN_CHUNK, source: str = "reviews.jsonl", parallel: int = MAX_PARALLEL
) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of `source`: as many parallel tally subagents as the work and `parallel` allow."""
    p = data_dir / source
    n = sum(1 for ln in p.read_text().splitlines() if ln.strip()) if p.is_file() else 0
    size = max(min_size, -(-n // max(1, parallel)))
    return [(a, min(a + size - 1, n)) for a in range(1, n + 1, size)]


def parallel_plan(data_dir: Path, source: str = "reviews.jsonl", parallel: int = MAX_PARALLEL) -> str:
    rel = str(data_dir).replace(str(Path.home()), "~")
    lines = [
        f"PARALLEL start ALL of these in one message; each tallies its lines of {rel}/{source}, read-only."
        " Give each subagent exactly the short description shown: it is its on-screen label"
    ]
    lines += [
        f"  factory-learn-tally  lines {a}-{b}  ->  {rel}/tally-{i}.json  (description: tally {i})"
        for i, (a, b) in enumerate(chunks(data_dir, source=source, parallel=parallel), 1)
    ]
    lines.append("THEN     cluster every tally file into the taste rules file (guardrail-learn skill, steps 4-6)")
    return "\n".join(lines)


DISTILLED = "distilled_ids.json"  # comment ids the current taste rules were built from


def undistilled(data_dir: Path) -> list[dict[str, Any]]:
    """Harvested comments the taste rules have not seen yet; written to reviews-new.jsonl for a refresh tally."""
    rows = _read_jsonl(data_dir / "reviews.jsonl")
    seen_file = data_dir / DISTILLED
    seen = set(json.loads(seen_file.read_text())) if seen_file.is_file() else None
    new = rows if seen is None else [c for c in rows if str(c["id"]) not in seen]
    (data_dir / "reviews-new.jsonl").write_text("".join(json.dumps(c) + "\n" for c in new))
    return new


def mark_distilled(data_dir: Path) -> None:
    ids = sorted({str(c["id"]) for c in _read_jsonl(data_dir / "reviews.jsonl")})
    (data_dir / DISTILLED).write_text(json.dumps(ids) + "\n")
