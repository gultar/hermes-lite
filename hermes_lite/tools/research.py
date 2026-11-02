"""Up-to-date library knowledge for a model with a fixed training date — no API keys needed.

package_info  latest version, release date, links and recent releases (PyPI, npm, crates.io), plus the
              version this project already uses.
github        repository search, README, files and folders, releases (public API and raw files);
              code search only when a GitHub token is available (gh CLI login or GITHUB_TOKEN).
read_docs     a library's documentation in model-friendly form: the project's llms.txt when it has one,
              otherwise the page itself, cut down to the sections that match the question.

Results are short by default (a small model's context is precious) and cached for a week.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from .base import Tool, ToolContext, error, ok, register, short

UA = "hermes-lite/0.1 (+local coding agent)"
CACHE_TTL = 7 * 86400


# ── shared helpers ────────────────────────────────────────────────────────
def _cache_dir(ctx: ToolContext) -> Path:
    d = ctx.home / "cache" / "research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(ctx: ToolContext, url: str, *, headers: dict | None = None, as_json: bool = False, ttl: int = CACHE_TTL):
    """GET with a week-long disk cache. Returns (status, body) where body is text or parsed JSON."""
    key = hashlib.sha1((url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()
    path = _cache_dir(ctx) / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["status"], data["body"]
    h = {"User-Agent": UA, **(headers or {})}
    r = httpx.get(url, headers=h, timeout=20, follow_redirects=True)
    body = r.json() if as_json and r.status_code == 200 else r.text
    if r.status_code == 200:
        path.write_text(json.dumps({"status": r.status_code, "body": body}), encoding="utf-8")
    return r.status_code, body


def _limit(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0] + f"\n[... cut at {n} characters]"


def _clean_md(text: str) -> str:
    """Drop the HTML that READMEs use for logos and badges; keep the words."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?im)^\s*(\[!\[[^\n]*|!\[[^\]]*\]\([^)]*\)\s*)$", "", text)  # badge-only lines
    text = re.sub(r"<img[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?(p|div|a|span|picture|source|br|hr|center|sub|sup|details|summary)\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sections(markdown: str, query: str, budget: int) -> str:
    """Keep the sections whose heading/body best match the query, in document order."""
    if not query:
        return _limit(markdown, budget)
    parts = re.split(r"(?m)^(?=#{1,4} )", markdown)
    words = [w for w in re.findall(r"[a-z0-9_.]+", query.lower()) if len(w) > 2]
    if not words or len(parts) < 2:
        return _limit(markdown, budget)

    def score(sec: str) -> float:
        low = sec.lower()
        head = low.split("\n", 1)[0]
        return sum(low.count(w) + 5 * head.count(w) for w in words) / (1 + len(sec) / 4000)
    ranked = sorted(range(len(parts)), key=lambda i: -score(parts[i]))
    keep, used = set(), 0
    for i in ranked:
        if score(parts[i]) <= 0 or used >= budget:
            break
        keep.add(i)
        used += len(parts[i])
    if not keep:
        return _limit(markdown, budget)
    return _limit("\n".join(parts[i].rstrip() for i in sorted(keep)), budget)


# ── package_info ──────────────────────────────────────────────────────────
def _project_pin(ctx: ToolContext, name: str) -> str | None:
    """The version spec this project already declares, if any."""
    root = Path(ctx.cwd)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                if name in data.get(section, {}):
                    return f"{data[section][name]} in package.json ({section})"
        except (OSError, ValueError):
            pass
    norm = re.sub(r"[-_.]+", "[-_.]", re.escape(name.lower()).replace("\\-", "-"))
    for f in ("requirements.txt", "pyproject.toml", "Cargo.toml"):
        p = root / f
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(rf'^\s*"?{norm}\b', line.lower()):
                    return f"{line.strip().strip(',')} in {f}"
    return None


def _guess_ecosystem(ctx: ToolContext) -> list[str]:
    root = Path(ctx.cwd)
    if (root / "package.json").exists():
        return ["npm", "pypi", "crates"]
    if (root / "Cargo.toml").exists():
        return ["crates", "pypi", "npm"]
    return ["pypi", "npm", "crates"]


def _pypi(ctx, name):
    status, d = _get(ctx, f"https://pypi.org/pypi/{quote(name)}/json", as_json=True, ttl=6 * 3600)
    if status != 200:
        return None
    info = d["info"]
    rel = sorted(((v, files[0]["upload_time"][:10]) for v, files in d.get("releases", {}).items() if files),
                 key=lambda x: x[1], reverse=True)
    urls = info.get("project_urls") or {}
    return {"ecosystem": "pypi", "name": info["name"], "latest": info["version"],
            "released": next((t for v, t in rel if v == info["version"]), None),
            "summary": info.get("summary"), "requires_python": info.get("requires_python"),
            "recent_versions": [f"{v} ({t})" for v, t in rel[:8]],
            "docs": urls.get("Documentation") or urls.get("documentation") or info.get("docs_url"),
            "homepage": info.get("home_page") or urls.get("Homepage") or urls.get("homepage"),
            "repository": urls.get("Source") or urls.get("Repository") or urls.get("source") or urls.get("GitHub")}


def _npm(ctx, name):
    status, d = _get(ctx, f"https://registry.npmjs.org/{quote(name, safe='@/')}", as_json=True, ttl=6 * 3600)
    if status != 200 or "dist-tags" not in d:
        return None
    latest = d["dist-tags"].get("latest")
    times = {k: v[:10] for k, v in d.get("time", {}).items() if k not in ("created", "modified")}
    recent = sorted(times.items(), key=lambda x: x[1], reverse=True)[:8]
    meta = d.get("versions", {}).get(latest, {})
    repo = (d.get("repository") or {}).get("url") if isinstance(d.get("repository"), dict) else d.get("repository")
    out = {"ecosystem": "npm", "name": d.get("name"), "latest": latest, "released": times.get(latest),
           "summary": d.get("description"), "recent_versions": [f"{v} ({t})" for v, t in recent],
           "homepage": d.get("homepage"), "repository": repo,
           "other_tags": {k: v for k, v in d["dist-tags"].items() if k != "latest"} or None,
           "node": (meta.get("engines") or {}).get("node")}
    if meta.get("deprecated"):
        out["deprecated"] = meta["deprecated"]
    return out


def _crates(ctx, name):
    status, d = _get(ctx, f"https://crates.io/api/v1/crates/{quote(name)}", as_json=True, ttl=6 * 3600)
    if status != 200 or "crate" not in d:
        return None
    c = d["crate"]
    vers = [f"{v['num']} ({v['created_at'][:10]})" for v in d.get("versions", [])[:8] if not v.get("yanked")]
    return {"ecosystem": "crates", "name": c["name"], "latest": c.get("max_stable_version") or c.get("max_version"),
            "released": (c.get("updated_at") or "")[:10], "summary": c.get("description"),
            "recent_versions": vers, "docs": c.get("documentation") or f"https://docs.rs/{c['name']}",
            "homepage": c.get("homepage"), "repository": c.get("repository")}


def package_info(args: dict, ctx: ToolContext) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return error("package_info: give the package 'name'.")
    eco = (args.get("ecosystem") or "auto").lower()
    order = _guess_ecosystem(ctx) if eco == "auto" else [eco]
    fetchers = {"pypi": _pypi, "npm": _npm, "crates": _crates, "cargo": _crates, "pip": _pypi}
    for e in order:
        fn = fetchers.get(e)
        if fn is None:
            return error(f"Unknown ecosystem {e!r}; use pypi, npm or crates.")
        try:
            res = fn(ctx, name)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return error(f"Could not reach the {e} registry: {exc}")
        if res:
            res = {k: v for k, v in res.items() if v}
            if pin := _project_pin(ctx, name):
                res["this_project_uses"] = pin
            res["note"] = ("Your training data may predate the latest version. Check read_docs or the "
                           "release notes (github action=releases) before relying on remembered APIs.")
            return ok(**res)
    return error(f"No package named {name!r} on {', '.join(order)}.")


# ── github ────────────────────────────────────────────────────────────────
def _session_dir(ctx: ToolContext) -> Path:
    """GitHub files too big to show at once, kept for this session only (removed when it ends)."""
    return ctx.home / "scratch" / ctx.session_id / "github"


def _session_copy(ctx: ToolContext, repo: str, ref: str, path: str) -> Path:
    safe = lambda x: re.sub(r"[^\w.\-]+", "_", x)
    return _session_dir(ctx) / f"{safe(repo.replace('/', '__'))}@{safe(ref)}" / Path(*[safe(p) for p in path.split("/")])


def _file_view(ctx: ToolContext, repo: str, ref: str, path: str, local: Path, args: dict, budget: int,
               ranged: bool) -> str:
    """A big GitHub file the way read_file shows a big local file: the outline (classes, functions,
    their line ranges) and the first lines; then any range on request, from the session's copy."""
    from .files import read_file
    lines = local.read_text(encoding="utf-8", errors="replace").count("\n") + 1
    if ranged:
        res = read_file({"path": str(local), "offset": args.get("offset"), "limit": args.get("limit")}, ctx)
    else:
        head = max(40, min(int(ctx.cfg.get_path("agent.read_head_lines", 300)), budget // 60))
        res = read_file({"path": str(local), "limit": head}, ctx)
    try:
        d = json.loads(res)
    except ValueError:
        return res
    if d.get("success") is False:
        return res
    d.pop("path", None)
    d.update(repo=repo, path=path, ref=ref, cached_copy=str(local))
    end = (d.get("next_offset") or lines + 1) - 1
    d["note"] = (f"{path} has {lines} lines, more than fits at once. Above: its outline (line ranges) and "
                 f"lines 1-{end}. Read any part with github action=file "
                 f"(same repo/path/ref) plus offset and limit, or read_file {local} with offset and limit — "
                 "both use this session's copy, no new download.") if not ranged else \
        (d.get("note") or "") + " (from this session's copy; no new download)"
    return json.dumps({"success": True, **d}, ensure_ascii=False)


def _token() -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _gh_headers(raw: bool = False) -> dict:
    h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if tok := _token():
        h["Authorization"] = f"Bearer {tok}"
    return h


def _repo_name(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"github\.com[/:]([^/\s]+/[^/\s#?]+)", s)
    s = m.group(1) if m else s
    return s.removesuffix(".git").strip("/")


GH = "https://api.github.com"
GITHUB_BASE = GH  # overridable in tests
RAW_BASE = "https://raw.githubusercontent.com"


def _gh_error(status: int, body) -> str:
    if status == 403 and "rate limit" in str(body).lower():
        return "GitHub's rate limit for anonymous requests is used up (60 per hour). Try again later, or log in with `gh auth login`."
    if status == 404:
        return "Not found on GitHub (check the owner/repo name or path)."
    return f"GitHub answered {status}: {short(str(body), 200)}"


def github(args: dict, ctx: ToolContext) -> str:
    action = args.get("action", "search")
    budget = int(args.get("max_chars") or 6000)
    try:
        if action == "search":
            q = args.get("query") or ""
            if not q:
                return error("github search: give a 'query'.")
            status, d = _get(ctx, f"{GITHUB_BASE}/search/repositories?q={quote(q)}&sort=stars&per_page=6",
                             headers=_gh_headers(), as_json=True, ttl=86400)
            if status != 200:
                return error(_gh_error(status, d))
            return ok(results=[{"repo": r["full_name"], "stars": r["stargazers_count"],
                                "updated": r["pushed_at"][:10], "description": short(r.get("description") or "", 160),
                                "archived": r.get("archived") or None} for r in d.get("items", [])])
        repo = _repo_name(args.get("repo", ""))
        if action != "code_search" and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            return error("Give 'repo' as owner/name, e.g. encode/httpx.")
        if action == "readme":
            status, d = _get(ctx, f"{GITHUB_BASE}/repos/{repo}/readme", headers=_gh_headers(raw=True))
            if status != 200:  # API blocked or rate-limited: raw files still work
                for branch in ("HEAD", "main", "master"):
                    status, d = _get(ctx, f"{RAW_BASE}/{repo}/{branch}/README.md")
                    if status == 200:
                        break
            if status != 200:
                return error(_gh_error(status, d))
            return ok(repo=repo, readme=_sections(_clean_md(d), args.get("query", ""), budget))
        if action == "file":
            path = (args.get("path") or "").strip("/")
            ref = args.get("ref") or "HEAD"
            local = _session_copy(ctx, repo, ref, path) if path else None
            ranged = args.get("offset") is not None or args.get("limit") is not None
            if local is not None and local.is_file():  # fetched earlier in this session: no download
                return _file_view(ctx, repo, ref, path, local, args, budget, ranged)
            status, d = _get(ctx, f"{RAW_BASE}/{repo}/{ref}/{path}")
            if status == 200 and path:
                doc = path.lower().endswith((".md", ".markdown", ".rst"))
                if (len(d) <= budget and not ranged) or (doc and args.get("query") and not ranged):
                    text = _clean_md(d) if doc else d
                    return ok(repo=repo, path=path, ref=ref, content=_sections(text, args.get("query", ""), budget))
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(d, encoding="utf-8")
                return _file_view(ctx, repo, ref, path, local, args, budget, ranged)
            status, d = _get(ctx, f"{GITHUB_BASE}/repos/{repo}/contents/{path}?ref={ref}", headers=_gh_headers(),
                             as_json=True, ttl=86400)
            if status == 200 and isinstance(d, list):
                return ok(repo=repo, path=path or "/", entries=[e["name"] + ("/" if e["type"] == "dir" else "")
                                                                 for e in d][:200])
            return error(_gh_error(status, d))
        if action == "releases":
            status, d = _get(ctx, f"{GITHUB_BASE}/repos/{repo}/releases?per_page=5", headers=_gh_headers(),
                             as_json=True, ttl=6 * 3600)
            if status != 200:
                return error(_gh_error(status, d) + " For a changelog, try action=file path=CHANGELOG.md.")
            per = max(500, budget // max(1, len(d)))
            return ok(repo=repo, releases=[{"tag": r["tag_name"], "date": (r.get("published_at") or "")[:10],
                                            "prerelease": r.get("prerelease") or None,
                                            "notes": _limit(r.get("body") or "", per)} for r in d])
        if action == "code_search":
            if not _token():
                return error("GitHub code search needs a login (run `gh auth login`, or set GITHUB_TOKEN). "
                             "Meanwhile use web_search with site:github.com.")
            q = args.get("query") or ""
            if repo:
                q += f" repo:{repo}"
            status, d = _get(ctx, f"{GITHUB_BASE}/search/code?q={quote(q)}&per_page=8",
                             headers={**_gh_headers(), "Accept": "application/vnd.github.text-match+json"},
                             as_json=True, ttl=86400)
            if status != 200:
                return error(_gh_error(status, d))
            return ok(results=[{"repo": i["repository"]["full_name"], "path": i["path"],
                                "snippets": [short(m.get("fragment", ""), 300) for m in i.get("text_matches", [])][:2]}
                               for i in d.get("items", [])])
        return error(f"Unknown action {action!r}.")
    except httpx.HTTPError as exc:
        return error(f"Could not reach GitHub: {exc}")


# ── read_docs ─────────────────────────────────────────────────────────────
def _docs_url_for(ctx: ToolContext, name: str) -> str | None:
    for fn in (_pypi, _npm, _crates):
        try:
            info = fn(ctx, name)
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        if info:
            for key in ("docs", "homepage", "repository"):
                if info.get(key):
                    return str(info[key])
    return None


def _llms_txt(ctx: ToolContext, url: str) -> tuple[str, str] | None:
    """Look for llms.txt next to the page, then at the site root."""
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    paths = []
    parts = [p for p in u.path.split("/") if p]
    for i in range(len(parts), -1, -1):
        paths.append("/" + "/".join(parts[:i]) + ("/" if i else ""))
    for p in dict.fromkeys(paths):
        candidate = urljoin(base, p) + "llms.txt"
        try:
            status, body = _get(ctx, candidate)
        except httpx.HTTPError:
            continue
        if status == 200 and body.lstrip().startswith("#") and "<html" not in body[:500].lower():
            return candidate, body
    return None


def read_docs(args: dict, ctx: ToolContext) -> str:
    from .web import _extract_one
    url, lib = (args.get("url") or "").strip(), (args.get("library") or "").strip()
    query = (args.get("query") or "").strip()
    budget = int(args.get("max_chars") or 6000)
    if not url and lib:
        url = _docs_url_for(ctx, lib) or ""
        if not url:
            return error(f"Could not find documentation for {lib!r}. Try web_search, or give a 'url'.")
        if "github.com" in url:
            return github({"action": "readme", "repo": url, "query": query, "max_chars": budget}, ctx)
    if not url:
        return error("read_docs: give a 'url', or a 'library' name to look up.")
    found = _llms_txt(ctx, url)
    if found:
        source, index = found
        links = re.findall(r"\[([^\]]+)\]\(([^)\s]+)\)(?::\s*([^\n]*))?", index)
        if query and links:
            words = [w for w in re.findall(r"[a-z0-9_.]+", query.lower()) if len(w) > 2]
            scored = sorted(links, key=lambda l: -sum((l[0] + " " + (l[2] or "") + " " + l[1]).lower().count(w)
                                                        for w in words))
            best = [l for l in scored[:2] if any(w in (l[0] + (l[2] or "") + l[1]).lower() for w in words)]
            pages = []
            for title, href, _ in best:
                link = urljoin(source, href)
                try:
                    status, body = _get(ctx, link)
                except httpx.HTTPError:
                    continue
                if status == 200:
                    pages.append(f"# {title} ({link})\n\n" + _sections(body, query, budget // max(1, len(best))))
            if pages:
                return ok(source=source, via="llms.txt", content=_limit("\n\n".join(pages), budget))
        return ok(source=source, via="llms.txt index",
                  content=_sections(index, query, budget),
                  note="This is the docs index. Call read_docs again with one of the listed URLs, or with a query.")
    res = _extract_one(url, ctx, 400_000)
    if res.get("error"):
        return error(f"Could not read {url}: {res['error']}")
    return ok(source=res["url"], title=res.get("title"), via="page",
              content=_sections(res["content"], query, budget))


register(Tool(
    name="package_info", emoji="📦", read_only=True,
    schema={"name": "package_info", "description": (
        "Current facts about a library from its registry (PyPI, npm, crates.io): latest version and release "
        "date, recent versions, docs/repository links, deprecation, and the version THIS project already "
        "declares. Your training data may be older than the library: check here before choosing a version, "
        "installing, or using an API you are not sure still exists."), "parameters": {
        "type": "object", "properties": {
            "name": {"type": "string", "description": "Package name, e.g. httpx, express, @tanstack/react-query, serde."},
            "ecosystem": {"type": "string", "enum": ["auto", "pypi", "npm", "crates"], "default": "auto",
                          "description": "auto guesses from the project (package.json → npm, Cargo.toml → crates)."},
        }, "required": ["name"]}},
    handler=package_info, label=lambda a: a.get("name", ""),
))

register(Tool(
    name="github", emoji="🐙", read_only=True,
    schema={"name": "github", "description": (
        "Read GitHub without a browser. action=search: find repositories (query). action=readme: a repo's "
        "README, optionally only the sections matching query. action=file: a file (path, optional ref) or a "
        "folder listing; a file too big to show at once returns its outline (classes, functions and their "
        "line ranges) and first lines, then any range with offset/limit. action=releases: the latest release notes — check them when an API seems to have "
        "changed. action=code_search: usage examples across GitHub (needs the user's gh login). Results are "
        "short; pass query to get only the relevant part."), "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "readme", "file", "releases", "code_search"]},
            "repo": {"type": "string", "description": "owner/name or a github.com URL."},
            "query": {"type": "string", "description": "Search words, or what to look for inside a README/file."},
            "path": {"type": "string", "description": "action=file: path inside the repo (empty = root listing)."},
            "ref": {"type": "string", "description": "action=file: branch, tag or commit (default: default branch)."},
            "offset": {"type": "integer", "description": "action=file: first line to read (a big file shows its outline first)."},
            "limit": {"type": "integer", "description": "action=file: number of lines to read."},
            "max_chars": {"type": "integer", "description": "Result budget (default 6000)."},
        }, "required": ["action"]}},
    handler=github,
    label=lambda a: " ".join(x for x in (a.get("action", ""), a.get("repo", ""), a.get("path", ""),
                                         short(a.get("query", ""), 40)) if x),
))

register(Tool(
    name="read_docs", emoji="📘", read_only=True,
    schema={"name": "read_docs", "description": (
        "Read a library's current documentation, cut to what you asked about. Give a docs 'url', or a "
        "'library' name to find its docs automatically, plus a 'query' naming the API or topic. Uses the "
        "project's llms.txt (docs written for AI models) when it exists, otherwise the page itself. Use it "
        "before relying on remembered APIs, and when an error suggests the API changed."), "parameters": {
        "type": "object", "properties": {
            "library": {"type": "string", "description": "Package name to look up (pypi/npm/crates)."},
            "url": {"type": "string", "description": "A documentation URL (overrides library)."},
            "query": {"type": "string", "description": "The API, option or topic you need, e.g. 'streaming responses'."},
            "max_chars": {"type": "integer", "description": "Result budget (default 6000)."},
        }}},
    handler=read_docs,
    label=lambda a: " ".join(x for x in (a.get("library") or a.get("url", ""), short(a.get("query", ""), 50)) if x),
))
