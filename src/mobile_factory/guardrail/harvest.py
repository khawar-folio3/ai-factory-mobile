from __future__ import annotations

import json
from collections import Counter
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
) -> dict[str, Any]:
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

    with ThreadPoolExecutor(jobs) as ex:
        raw = [c for batch in ex.map(lambda p: _pr_comments(root, repo, p), prs.values()) for c in batch]

    source = "flag"
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
    with ThreadPoolExecutor(jobs) as ex:
        threads: dict[str, dict[str, Any]] = {}
        for t in ex.map(lambda n: _threads(root, repo, n), inline_prs):
            threads.update(t)
    for c in kept:
        c["resolution"] = threads.get(c["id"])

    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / "reviews.jsonl"
    merged = {c["id"]: c for c in (_read_jsonl(out) if since else [])}
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
        "prs_with_owner_comments": reviewed,
        "comments": len(rows),
        "changes_requested": sum(1 for c in rows if c["state"] == "CHANGES_REQUESTED"),
    }
    (data_dir / "harvest_meta.json").write_text(json.dumps(meta, indent=2))
    if reviewed < min_prs:
        raise FactoryError(f"only {reviewed} PRs with owner comments (< {min_prs}): not enough history to learn from")
    return meta


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.is_file() else []
