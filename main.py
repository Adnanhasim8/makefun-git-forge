#!/usr/bin/env python3
"""make.fun git forge — a single-process, GitHub-compatible git forge.

Serves, from one long-lived process and one store of bare git repositories
(``.makefun/forge/<owner>/<repo>.git`` relative to the working directory):

* git smart HTTP (clone/fetch/push) via the ``git http-backend`` CGI,
* a GitHub REST v3 subset (repos, contents, git data, commits, branches,
  issues, pull requests, search),
* a minimal GraphQL endpoint (``repository`` / ``viewer``),
* an MCP server over stdio (``--mcp``) exposing repo tools.

Python 3 standard library only; git operations go through the ``git`` CLI.
No network calls to github.com / api.github.com are ever made.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# Store layout and small utilities
# --------------------------------------------------------------------------

STORE = os.path.join(os.getcwd(), ".makefun", "forge")
LOCK = threading.Lock()

OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_date(ts, tz="+0000"):
    """Format a unix timestamp the way git/GitHub author dates appear."""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_id(text):
    """Deterministic small numeric id, GitHub-shaped."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100_000_000 + 1


def valid_owner(name):
    return bool(name) and bool(OWNER_RE.match(name)) and name not in (".", "..")


def valid_repo(name):
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ".." in name.split("/"):
        return False
    return bool(REPO_RE.match(name))


def repo_dir(owner, repo):
    """Absolute path of the bare repo dir, or None if the names are unsafe."""
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not valid_owner(owner) or not valid_repo(repo):
        return None
    path = os.path.join(STORE, owner, repo + ".git")
    # Defence in depth: the resolved path must stay inside the store.
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(STORE)]) != os.path.abspath(STORE):
        return None
    return path


def repo_exists(owner, repo):
    path = repo_dir(owner, repo)
    return bool(path) and os.path.isdir(path) and os.path.exists(os.path.join(path, "HEAD"))


def list_owners():
    if not os.path.isdir(STORE):
        return []
    return sorted(d for d in os.listdir(STORE) if valid_owner(d) and os.path.isdir(os.path.join(STORE, d)))


def list_repos(owner):
    base = os.path.join(STORE, owner)
    if not os.path.isdir(base):
        return []
    out = []
    for d in sorted(os.listdir(base)):
        if d.endswith(".git") and os.path.exists(os.path.join(base, d, "HEAD")):
            out.append(d[:-4])
    return out


def all_repos():
    out = []
    for owner in list_owners():
        for repo in list_repos(owner):
            out.append((owner, repo))
    return out


# --------------------------------------------------------------------------
# git CLI plumbing
# --------------------------------------------------------------------------


def run_git(args, repo=None, data=None, extra_env=None):
    """Run git, return (returncode, stdout_bytes, stderr_bytes)."""
    cmd = ["git"]
    if repo:
        cmd += ["--git-dir", repo]
    cmd += args
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def git_text(args, repo, strip=True):
    rc, out, _ = run_git(args, repo=repo)
    if rc != 0:
        return None
    text = out.decode("utf-8", "replace")
    return text.strip() if strip else text


def default_branch(rdir):
    ref = git_text(["symbolic-ref", "HEAD"], rdir)
    if ref and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    # HEAD may point at an unborn branch name; fall back to any branch.
    branches = list_branches(rdir)
    if branches:
        return branches[0]["name"]
    if ref and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return "main"


def resolve_commit(rdir, rev):
    """Resolve any rev to a commit sha, or None."""
    if not rev:
        return None
    out = git_text(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], rdir)
    return out or None


def list_branches(rdir):
    out = git_text(["for-each-ref", "--format=%(refname:short)%09%(objectname)",
                    "refs/heads"], rdir)
    branches = []
    if out:
        for line in out.splitlines():
            if "\t" in line:
                name, sha = line.split("\t", 1)
                branches.append({"name": name, "sha": sha})
    return branches


def branch_sha(rdir, branch):
    return git_text(["rev-parse", "--verify", "--quiet",
                     "refs/heads/" + branch + "^{commit}"], rdir)


AUTHOR_LINE_RE = re.compile(r"^(.*?) <(.*?)> (\d+) ([+-]\d{4})$")


def parse_ident(line):
    m = AUTHOR_LINE_RE.match(line or "")
    if not m:
        return {"name": "", "email": "", "date": iso_now()}
    return {"name": m.group(1), "email": m.group(2), "date": git_date(m.group(3))}


def read_commit(rdir, sha):
    """Parse a commit object into a dict, or None."""
    rc, out, _ = run_git(["cat-file", "commit", sha], repo=rdir)
    if rc != 0:
        return None
    text = out.decode("utf-8", "replace")
    header, _, message = text.partition("\n\n")
    commit = {"sha": sha, "tree": None, "parents": [],
              "author": None, "committer": None, "message": message.strip("\n")}
    for line in header.splitlines():
        if line.startswith("tree "):
            commit["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            commit["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            commit["author"] = parse_ident(line[7:])
        elif line.startswith("committer "):
            commit["committer"] = parse_ident(line[10:])
    return commit


def list_commit_shas(rdir, rev, limit=100):
    out = git_text(["rev-list", "--max-count=%d" % limit, rev], rdir)
    if not out:
        return []
    return out.splitlines()


# --------------------------------------------------------------------------
# Per-repo metadata (issues / PRs / counters) as JSON inside the bare repo
# --------------------------------------------------------------------------


def _meta_path(rdir, name):
    return os.path.join(rdir, name)


def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def repo_meta(rdir):
    return load_json_file(_meta_path(rdir, "forge-meta.json"),
                          {"next_number": 1, "description": None, "created_at": None})


def load_issues(rdir):
    return load_json_file(_meta_path(rdir, "forge-issues.json"), [])


def load_pulls(rdir):
    return load_json_file(_meta_path(rdir, "forge-pulls.json"), [])


def next_number(rdir):
    with LOCK:
        meta = repo_meta(rdir)
        number = int(meta.get("next_number", 1))
        meta["next_number"] = number + 1
        save_json_atomic(_meta_path(rdir, "forge-meta.json"), meta)
        return number


def create_issue_record(rdir, owner, repo, title, body, login):
    with LOCK:
        issues = load_issues(rdir)
        number = next_number_locked(rdir)
        now = iso_now()
        record = {"number": number, "title": title or "", "body": body or "",
                  "state": "open", "user": login, "created_at": now,
                  "updated_at": now, "comments": 0}
        issues.append(record)
        save_json_atomic(_meta_path(rdir, "forge-issues.json"), issues)
        return record


def next_number_locked(rdir):
    """next_number without re-acquiring LOCK (caller already holds it)."""
    meta = repo_meta(rdir)
    number = int(meta.get("next_number", 1))
    meta["next_number"] = number + 1
    save_json_atomic(_meta_path(rdir, "forge-meta.json"), meta)
    return number


def validate_ref_for_pr(rdir, value):
    """A PR head/base must be an existing branch or SHA, never a number.

    Returns (ref_name, sha) or None when invalid.
    """
    if value is None:
        return None
    value = str(value)
    if not value or value.isdigit():
        return None
    sha = branch_sha(rdir, value)
    if sha:
        return (value, sha)
    if SHA_RE.match(value):
        sha = resolve_commit(rdir, value)
        if sha:
            return (value, sha)
    return None


def create_pull_record(rdir, title, body, login, head, base):
    """head/base are (ref, sha) pairs from validate_ref_for_pr."""
    with LOCK:
        pulls = load_pulls(rdir)
        number = next_number_locked(rdir)
        now = iso_now()
        record = {"number": number, "title": title or "", "body": body or "",
                  "state": "open", "user": login, "created_at": now,
                  "updated_at": now,
                  "head_ref": head[0], "head_sha": head[1],
                  "base_ref": base[0], "base_sha": base[1]}
        pulls.append(record)
        save_json_atomic(_meta_path(rdir, "forge-pulls.json"), pulls)
        return record


# --------------------------------------------------------------------------
# Auth — any dummy token is accepted; absence of a header means 401 (REST)
# --------------------------------------------------------------------------


def derive_login(headers):
    """Return the current user's login from the Authorization header.

    None when no Authorization header is present at all.
    """
    auth = headers.get("Authorization") if headers else None
    if not auth:
        return None
    parts = auth.strip().split(None, 1)
    scheme = parts[0].lower() if parts else ""
    value = parts[1] if len(parts) > 1 else ""
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value).decode("utf-8", "replace")
        except Exception:
            decoded = ""
        username = decoded.split(":", 1)[0].strip()
        if username:
            return username
        return "user"
    # Bearer / token / anything else: the credential string itself.
    token = value.strip()
    if token and LOGIN_RE.fullmatch(token):
        return token
    return "user"


# --------------------------------------------------------------------------
# GitHub-shaped JSON builders
# --------------------------------------------------------------------------


def user_json(login):
    return {"login": login, "id": stable_id("user:" + login),
            "node_id": "U_" + str(stable_id("user:" + login)),
            "avatar_url": "", "gravatar_id": "", "type": "User",
            "site_admin": False, "html_url": ""}


def repo_json(owner, repo, base_url=""):
    rdir = repo_dir(owner, repo)
    meta = repo_meta(rdir) if rdir else {}
    created = meta.get("created_at")
    if not created and rdir and os.path.isdir(rdir):
        created = iso_from_ts(os.path.getmtime(rdir))
    full = owner + "/" + repo
    return {
        "id": stable_id("repo:" + full),
        "node_id": "R_" + str(stable_id("repo:" + full)),
        "name": repo,
        "full_name": full,
        "private": False,
        "owner": user_json(owner),
        "html_url": "%s/%s/%s" % (base_url, owner, repo),
        "description": meta.get("description"),
        "fork": False,
        "url": "%s/repos/%s/%s" % (base_url, owner, repo),
        "clone_url": "%s/%s/%s.git" % (base_url, owner, repo),
        "git_url": "git://%s/%s/%s.git" % (base_url.split("://")[-1], owner, repo),
        "ssh_url": "git@%s:%s/%s.git" % (base_url.split("://")[-1], owner, repo),
        "homepage": None,
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "forks_count": 0,
        "open_issues_count": len(load_issues(rdir)) if rdir else 0,
        "default_branch": default_branch(rdir) if rdir else "main",
        "created_at": created or iso_now(),
        "updated_at": created or iso_now(),
        "pushed_at": created or iso_now(),
    }


def commit_json(rdir, commit, base_url, owner, repo):
    author = commit["author"] or {"name": "", "email": "", "date": iso_now()}
    committer = commit["committer"] or author
    return {
        "sha": commit["sha"],
        "node_id": "C_" + str(stable_id("commit:" + commit["sha"])),
        "commit": {
            "author": author,
            "committer": committer,
            "message": commit["message"],
            "tree": {"sha": commit["tree"],
                     "url": "%s/repos/%s/%s/git/trees/%s" % (base_url, owner, repo, commit["tree"])},
            "url": "%s/repos/%s/%s/git/commits/%s" % (base_url, owner, repo, commit["sha"]),
            "comment_count": 0,
            "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
        },
        "url": "%s/repos/%s/%s/commits/%s" % (base_url, owner, repo, commit["sha"]),
        "html_url": "%s/%s/%s/commit/%s" % (base_url, owner, repo, commit["sha"]),
        "comments_url": "",
        "author": None,
        "committer": None,
        "parents": [{"sha": p,
                     "url": "%s/repos/%s/%s/commits/%s" % (base_url, owner, repo, p),
                     "html_url": "%s/%s/%s/commit/%s" % (base_url, owner, repo, p)}
                    for p in commit["parents"]],
    }


def git_commit_json(rdir, commit, base_url, owner, repo):
    """The /git/commits/:sha shape (distinct from the /commits list shape)."""
    author = commit["author"] or {"name": "", "email": "", "date": iso_now()}
    committer = commit["committer"] or author
    return {
        "sha": commit["sha"],
        "node_id": "C_" + str(stable_id("commit:" + commit["sha"])),
        "url": "%s/repos/%s/%s/git/commits/%s" % (base_url, owner, repo, commit["sha"]),
        "html_url": "%s/%s/%s/commit/%s" % (base_url, owner, repo, commit["sha"]),
        "author": author,
        "committer": committer,
        "message": commit["message"],
        "tree": {"sha": commit["tree"],
                 "url": "%s/repos/%s/%s/git/trees/%s" % (base_url, owner, repo, commit["tree"])},
        "parents": [{"sha": p,
                     "url": "%s/repos/%s/%s/git/commits/%s" % (base_url, owner, repo, p),
                     "html_url": "%s/%s/%s/commit/%s" % (base_url, owner, repo, p)}
                    for p in commit["parents"]],
        "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None},
    }


def issue_json(record, base_url, owner, repo):
    return {
        "id": stable_id("issue:%s/%s#%d" % (owner, repo, record["number"])),
        "node_id": "I_" + str(stable_id("issue:%s/%s#%d" % (owner, repo, record["number"]))),
        "url": "%s/repos/%s/%s/issues/%d" % (base_url, owner, repo, record["number"]),
        "repository_url": "%s/repos/%s/%s" % (base_url, owner, repo),
        "html_url": "%s/%s/%s/issues/%d" % (base_url, owner, repo, record["number"]),
        "number": record["number"],
        "title": record["title"],
        "user": user_json(record["user"]),
        "labels": [],
        "state": record["state"],
        "locked": False,
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "comments": record.get("comments", 0),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "closed_at": None,
        "body": record["body"],
    }


def pull_json(record, rdir, base_url, owner, repo):
    def side(ref, sha):
        return {
            "label": "%s:%s" % (owner, ref),
            "ref": ref,
            "sha": sha,
            "user": user_json(owner),
            "repo": repo_json(owner, repo, base_url),
        }
    return {
        "id": stable_id("pull:%s/%s#%d" % (owner, repo, record["number"])),
        "node_id": "PR_" + str(stable_id("pull:%s/%s#%d" % (owner, repo, record["number"]))),
        "url": "%s/repos/%s/%s/pulls/%d" % (base_url, owner, repo, record["number"]),
        "html_url": "%s/%s/%s/pull/%d" % (base_url, owner, repo, record["number"]),
        "diff_url": "%s/%s/%s/pull/%d.diff" % (base_url, owner, repo, record["number"]),
        "patch_url": "%s/%s/%s/pull/%d.patch" % (base_url, owner, repo, record["number"]),
        "number": record["number"],
        "state": record["state"],
        "locked": False,
        "title": record["title"],
        "user": user_json(record["user"]),
        "body": record["body"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "closed_at": None,
        "merged_at": None,
        "merge_commit_sha": None,
        "assignees": [],
        "requested_reviewers": [],
        "labels": [],
        "head": side(record["head_ref"], record["head_sha"]),
        "base": side(record["base_ref"], record["base_sha"]),
        "merged": False,
        "comments": 0,
        "review_comments": 0,
        "commits": 0,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }


# --------------------------------------------------------------------------
# Contents / trees built from git objects
# --------------------------------------------------------------------------


def ls_tree_entries(rdir, treeish, recursive=False):
    """Return list of dicts {path, mode, type, sha, size} for a tree-ish."""
    args = ["ls-tree", "-l"]
    if recursive:
        args += ["-r", "-t"]
    args.append(treeish)
    rc, out, _ = run_git(args, repo=rdir)
    if rc != 0:
        return None
    entries = []
    for line in out.decode("utf-8", "replace").splitlines():
        # "<mode> <type> <sha> <size>\t<path>"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4:
            continue
        mode, typ, sha, size = parts[0], parts[1], parts[2], parts[3]
        entries.append({"path": path, "mode": mode, "type": typ, "sha": sha,
                        "size": None if size == "-" else int(size)})
    return entries


def contents_json(rdir, owner, repo, path, ref, base_url):
    """GitHub /contents response for a path at a ref, or None if missing."""
    path = path.strip("/")
    rev = ref or "HEAD"
    if path:
        typ = git_text(["cat-file", "-t", "%s:%s" % (rev, path)], rdir)
        if typ is None:
            return None
    else:
        typ = "tree"
    if typ == "blob":
        rc, data, _ = run_git(["cat-file", "blob", "%s:%s" % (rev, path)], repo=rdir)
        if rc != 0:
            return None
        sha = git_text(["rev-parse", "%s:%s" % (rev, path)], rdir)
        encoded = base64.b64encode(data).decode("ascii")
        wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))
        if wrapped:
            wrapped += "\n"
        name = path.split("/")[-1]
        return {
            "name": name, "path": path, "sha": sha, "size": len(data),
            "url": "%s/repos/%s/%s/contents/%s" % (base_url, owner, repo, path),
            "html_url": "%s/%s/%s/blob/%s/%s" % (base_url, owner, repo, ref or default_branch(rdir), path),
            "git_url": "%s/repos/%s/%s/git/blobs/%s" % (base_url, owner, repo, sha),
            "download_url": "%s/%s/%s/raw/%s/%s" % (base_url, owner, repo, ref or default_branch(rdir), path),
            "type": "file", "encoding": "base64", "content": wrapped,
            "_links": {"self": "%s/repos/%s/%s/contents/%s" % (base_url, owner, repo, path),
                       "git": "%s/repos/%s/%s/git/blobs/%s" % (base_url, owner, repo, sha),
                       "html": "%s/%s/%s/blob/%s/%s" % (base_url, owner, repo, ref or default_branch(rdir), path)},
        }
    if typ == "tree":
        treeish = "%s:%s" % (rev, path) if path else rev
        entries = ls_tree_entries(rdir, treeish)
        if entries is None:
            return None
        out = []
        for e in entries:
            name = e["path"].split("/")[-1]
            full = (path + "/" + name) if path else name
            etype = "dir" if e["type"] == "tree" else ("file" if e["type"] == "blob" else "submodule")
            out.append({
                "name": name, "path": full, "sha": e["sha"],
                "size": e["size"] or 0,
                "url": "%s/repos/%s/%s/contents/%s" % (base_url, owner, repo, full),
                "html_url": "%s/%s/%s/%s/%s/%s" % (base_url, owner, repo,
                                                   "tree" if etype == "dir" else "blob",
                                                   ref or default_branch(rdir), full),
                "git_url": "%s/repos/%s/%s/git/%s/%s" % (base_url, owner, repo,
                                                         "trees" if etype == "dir" else "blobs", e["sha"]),
                "download_url": None if etype == "dir" else
                                "%s/%s/%s/raw/%s/%s" % (base_url, owner, repo, ref or default_branch(rdir), full),
                "type": etype,
                "_links": {"self": "%s/repos/%s/%s/contents/%s" % (base_url, owner, repo, full),
                           "git": "%s/repos/%s/%s/git/%s/%s" % (base_url, owner, repo,
                                                                "trees" if etype == "dir" else "blobs", e["sha"]),
                           "html": "%s/%s/%s" % (base_url, owner, repo)},
            })
        return out
    return None


# --------------------------------------------------------------------------
# git smart HTTP via the git http-backend CGI
# --------------------------------------------------------------------------

HTTP_BACKEND = None
for candidate in ("/usr/lib/git-core/git-http-backend",
                  "/usr/libexec/git-core/git-http-backend",
                  "/usr/local/libexec/git-core/git-http-backend"):
    if os.path.exists(candidate):
        HTTP_BACKEND = candidate
        break
if HTTP_BACKEND is None:
    try:
        exec_path = subprocess.run(["git", "--exec-path"], stdout=subprocess.PIPE,
                                   check=True).stdout.decode().strip()
        cand = os.path.join(exec_path, "git-http-backend")
        if os.path.exists(cand):
            HTTP_BACKEND = cand
    except Exception:
        HTTP_BACKEND = None


def handle_git_http(handler, method, path, query, body):
    """Bridge one smart-HTTP request to git http-backend. Returns True."""
    # path looks like /owner/repo[.git]/(info/refs|git-upload-pack|git-receive-pack)
    m = re.match(r"^/([^/]+)/([^/]+)/(info/refs|git-upload-pack|git-receive-pack)$", path)
    if not m:
        return False
    owner, repo, service = m.group(1), m.group(2), m.group(3)
    rdir = repo_dir(owner, repo)
    if rdir is None:
        handler.send_json(404, {"message": "Not Found"})
        return True
    repo_name = repo[:-4] if repo.endswith(".git") else repo
    if HTTP_BACKEND is not None:
        return _git_via_backend(handler, method, owner, repo_name, service, query, body)
    return _git_via_plumbing(handler, method, owner, repo_name, rdir, service, query, body)


def _git_via_backend(handler, method, owner, repo_name, service, query, body):
    path_info = "/%s/%s.git/%s" % (owner, repo_name, service)
    env = {
        "GATEWAY_INTERFACE": "CGI/1.1",
        "REQUEST_METHOD": method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query,
        "GIT_PROJECT_ROOT": os.path.abspath(STORE),
        "GIT_HTTP_EXPORT_ALL": "1",
        "REMOTE_ADDR": "127.0.0.1",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_SOFTWARE": "makefun-forge",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    login = derive_login(handler.headers)
    env["REMOTE_USER"] = login or "anonymous"
    ctype = handler.headers.get("Content-Type")
    if ctype:
        env["CONTENT_TYPE"] = ctype
    if body:
        env["CONTENT_LENGTH"] = str(len(body))
    proc = subprocess.run([HTTP_BACKEND], input=body or b"",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out = proc.stdout
    # Split CGI headers from the body.
    split_at = out.find(b"\r\n\r\n")
    sep_len = 4
    if split_at < 0:
        split_at = out.find(b"\n\n")
        sep_len = 2
    if split_at < 0:
        handler.send_bytes(500, b"git backend error", "text/plain")
        return True
    head = out[:split_at].decode("iso-8859-1")
    payload = out[split_at + sep_len:]
    status = 200
    headers = {}
    for line in head.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key.lower() == "status":
            try:
                status = int(value.split()[0])
            except (ValueError, IndexError):
                status = 200
        elif key.lower() not in ("content-length",):
            headers[key] = value
    handler.send_bytes(status, payload, headers.get("Content-Type", "text/plain"),
                       extra_headers=headers)
    return True


def _pkt_line(data):
    if data is None:
        return b"0000"
    payload = data + b"\n" if not data.endswith(b"\n") else data
    return ("%04x" % (len(payload) + 4)).encode() + payload


def _git_via_plumbing(handler, method, owner, repo_name, rdir, service, query, body):
    """Fallback when git http-backend is unavailable: drive upload/receive-pack."""
    if not os.path.isdir(rdir):
        handler.send_bytes(404, b"not found", "text/plain")
        return True
    params = parse_qs(query)
    svc = params.get("service", [""])[0]
    if method == "GET" and service == "info/refs" and svc in ("git-upload-pack", "git-receive-pack"):
        proc = subprocess.run(["git", svc.replace("git-", ""), "--http-backend-info-refs", "."],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=rdir)
        refs = proc.stdout
        if proc.returncode != 0:
            handler.send_bytes(500, b"backend error", "text/plain")
            return True
        banner = _pkt_line(("# service=" + svc).encode())
        payload = banner + b"0000" + refs
        handler.send_bytes(200, payload,
                           "application/x-%s-advertisement" % svc)
        return True
    if method == "POST" and service in ("git-upload-pack", "git-receive-pack"):
        prog = service.replace("git-", "")
        proc = subprocess.run(["git", prog, "--stateless-rpc", "."], input=body or b"",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=rdir)
        handler.send_bytes(200, proc.stdout,
                           "application/x-%s-result" % service)
        return True
    handler.send_bytes(404, b"not found", "text/plain")
    return True


# --------------------------------------------------------------------------
# REST v3
# --------------------------------------------------------------------------


def pull_files(rdir, record):
    base, head = record["base_sha"], record["head_sha"]
    rng = "%s...%s" % (base, head)
    _, ns_out, _ = run_git(["diff", "--name-status", "--no-renames", rng], repo=rdir)
    _, num_out, _ = run_git(["diff", "--numstat", "--no-renames", rng], repo=rdir)
    stats = {}
    for line in num_out.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            add = 0 if parts[0] == "-" else int(parts[0])
            dele = 0 if parts[1] == "-" else int(parts[1])
            stats[parts[2]] = (add, dele)
    status_map = {"A": "added", "M": "modified", "D": "removed",
                  "R": "renamed", "C": "copied", "T": "modified"}
    files = []
    for line in ns_out.decode("utf-8", "replace").splitlines():
        if "\t" not in line:
            continue
        code, path = line.split("\t", 1)
        add, dele = stats.get(path, (0, 0))
        sha = git_text(["rev-parse", "--quiet", "%s:%s" % (head, path)], rdir)
        if not sha:
            sha = git_text(["rev-parse", "--quiet", "%s:%s" % (base, path)], rdir)
        _, patch_out, _ = run_git(["diff", rng, "--", path], repo=rdir)
        files.append({
            "sha": sha or "0000000000000000000000000000000000000000",
            "filename": path,
            "status": status_map.get(code[:1], "modified"),
            "additions": add, "deletions": dele, "changes": add + dele,
            "patch": patch_out.decode("utf-8", "replace"),
        })
    return files


def _search_terms(q):
    terms = []
    for tok in (q or "").split():
        if ":" in tok:  # GitHub qualifiers (user:, in:, ...) — ignored
            continue
        terms.append(tok.lower())
    return terms


def handle_rest(handler, method, seg, query, data, login, base_url):
    """Dispatch a REST request. seg = path segments. Returns True if handled."""
    # ---- search ------------------------------------------------------
    if len(seg) == 2 and seg[0] == "search" and method == "GET":
        q = query.get("q", [""])[0]
        terms = _search_terms(q)
        if seg[1] == "repositories":
            items = []
            for owner, repo in all_repos():
                rdir = repo_dir(owner, repo)
                meta = repo_meta(rdir)
                hay = ("%s/%s %s" % (owner, repo, meta.get("description") or "")).lower()
                if all(t in hay for t in terms):
                    items.append(repo_json(owner, repo, base_url))
            handler.send_json(200, {"total_count": len(items),
                                    "incomplete_results": False, "items": items})
            return True
        if seg[1] == "issues":
            items = []
            for owner, repo in all_repos():
                rdir = repo_dir(owner, repo)
                for rec in load_issues(rdir):
                    hay = (rec["title"] + " " + rec["body"]).lower()
                    if all(t in hay for t in terms):
                        items.append(issue_json(rec, base_url, owner, repo))
                for rec in load_pulls(rdir):
                    hay = (rec["title"] + " " + rec["body"]).lower()
                    if all(t in hay for t in terms):
                        item = issue_json(rec, base_url, owner, repo)
                        item["pull_request"] = {
                            "url": "%s/repos/%s/%s/pulls/%d" % (base_url, owner, repo, rec["number"]),
                            "html_url": "%s/%s/%s/pull/%d" % (base_url, owner, repo, rec["number"]),
                            "diff_url": "", "patch_url": ""}
                        items.append(item)
            handler.send_json(200, {"total_count": len(items),
                                    "incomplete_results": False, "items": items})
            return True
        if seg[1] == "users":
            logins = set(list_owners()) | {login}
            items = [user_json(u) for u in sorted(logins)
                     if all(t in u.lower() for t in terms)]
            handler.send_json(200, {"total_count": len(items),
                                    "incomplete_results": False, "items": items})
            return True
        return False

    # ---- current user -------------------------------------------------
    if seg == ["user", "repos"]:
        if method == "POST":
            name = (data or {}).get("name")
            if not name or not valid_repo(str(name)):
                handler.send_json(422, {"message": "Repository creation failed.",
                                        "errors": [{"field": "name", "code": "invalid"}]})
                return True
            name = str(name)
            rdir = repo_dir(login, name)
            if rdir is None:
                handler.send_json(422, {"message": "Repository creation failed."})
                return True
            if os.path.isdir(rdir):
                handler.send_json(422, {"message": "Repository creation failed.",
                                        "errors": [{"field": "name", "code": "already_exists"}]})
                return True
            os.makedirs(os.path.dirname(rdir), exist_ok=True)
            rc, _, err = run_git(["init", "--bare", "-b", "main", rdir])
            if rc != 0:
                handler.send_json(500, {"message": "init failed: " + err.decode("utf-8", "replace")})
                return True
            run_git(["config", "http.receivepack", "true"], repo=rdir)
            meta = repo_meta(rdir)
            meta["description"] = (data or {}).get("description")
            meta["created_at"] = iso_now()
            save_json_atomic(_meta_path(rdir, "forge-meta.json"), meta)
            handler.send_json(201, repo_json(login, name, base_url))
            return True
        if method == "GET":
            items = [repo_json(login, r, base_url) for r in list_repos(login)]
            handler.send_json(200, items)
            return True
        return False

    if len(seg) == 3 and seg[0] == "users" and seg[2] == "repos" and method == "GET":
        username = seg[1]
        items = [repo_json(username, r, base_url) for r in list_repos(username)]
        handler.send_json(200, items)
        return True

    # ---- repo-scoped ---------------------------------------------------
    if len(seg) >= 3 and seg[0] == "repos":
        owner, repo = seg[1], seg[2]
        rdir = repo_dir(owner, repo)
        if rdir is None or not repo_exists(owner, repo):
            handler.send_json(404, {"message": "Not Found"})
            return True
        rest = seg[3:]

        if not rest and method == "GET":
            handler.send_json(200, repo_json(owner, repo, base_url))
            return True

        if rest and rest[0] == "contents" and method == "GET":
            path = "/".join(rest[1:])
            ref = query.get("ref", [None])[0]
            result = contents_json(rdir, owner, repo, path, ref, base_url)
            if result is None:
                handler.send_json(404, {"message": "Not Found"})
            else:
                handler.send_json(200, result)
            return True

        if len(rest) == 3 and rest[0] == "git" and rest[1] == "trees" and method == "GET":
            sha = rest[2]
            recursive = query.get("recursive", [""])[0] in ("1", "true")
            tree_sha = resolve_commit(rdir, sha) or sha
            entries = ls_tree_entries(rdir, tree_sha, recursive=recursive)
            if entries is None:
                handler.send_json(404, {"message": "Not Found"})
                return True
            tree = []
            for e in entries:
                item = {"path": e["path"], "mode": e["mode"], "type": e["type"],
                        "sha": e["sha"],
                        "url": "%s/repos/%s/%s/git/%s/%s" % (
                            base_url, owner, repo,
                            "trees" if e["type"] == "tree" else "blobs", e["sha"])}
                if e["type"] == "blob":
                    item["size"] = e["size"] or 0
                tree.append(item)
            handler.send_json(200, {"sha": tree_sha,
                                    "url": "%s/repos/%s/%s/git/trees/%s" % (base_url, owner, repo, tree_sha),
                                    "tree": tree, "truncated": False})
            return True

        if len(rest) == 3 and rest[0] == "git" and rest[1] == "commits" and method == "GET":
            commit = read_commit(rdir, rest[2])
            if commit is None:
                sha = resolve_commit(rdir, rest[2])
                commit = read_commit(rdir, sha) if sha else None
            if commit is None:
                handler.send_json(404, {"message": "Not Found"})
            else:
                handler.send_json(200, git_commit_json(rdir, commit, base_url, owner, repo))
            return True

        if len(rest) >= 3 and rest[0] == "git" and rest[1] == "refs" and method == "GET":
            refname = "/".join(rest[2:])
            sha = git_text(["rev-parse", "--verify", "--quiet",
                            "refs/" + refname + "^{commit}"], rdir)
            if not sha:
                handler.send_json(404, {"message": "Not Found"})
                return True
            handler.send_json(200, {
                "ref": "refs/" + refname,
                "node_id": "REF_" + str(stable_id("ref:%s/%s:%s" % (owner, repo, refname))),
                "url": "%s/repos/%s/%s/git/refs/%s" % (base_url, owner, repo, refname),
                "object": {"type": "commit", "sha": sha,
                           "url": "%s/repos/%s/%s/git/commits/%s" % (base_url, owner, repo, sha)}})
            return True

        if rest == ["commits"] and method == "GET":
            rev = query.get("sha", [None])[0] or default_branch(rdir)
            try:
                per_page = min(int(query.get("per_page", ["100"])[0]), 100)
            except ValueError:
                per_page = 100
            shas = list_commit_shas(rdir, rev, limit=per_page)
            items = []
            for sha in shas:
                commit = read_commit(rdir, sha)
                if commit:
                    items.append(commit_json(rdir, commit, base_url, owner, repo))
            handler.send_json(200, items)
            return True

        if rest == ["branches"] and method == "GET":
            items = [{"name": b["name"],
                      "commit": {"sha": b["sha"],
                                 "url": "%s/repos/%s/%s/commits/%s" % (base_url, owner, repo, b["sha"])},
                      "protected": False}
                     for b in list_branches(rdir)]
            handler.send_json(200, items)
            return True

        if rest and rest[0] == "issues":
            if len(rest) == 1 and method == "GET":
                items = [issue_json(r, base_url, owner, repo) for r in load_issues(rdir)]
                handler.send_json(200, items)
                return True
            if len(rest) == 1 and method == "POST":
                record = create_issue_record(rdir, owner, repo,
                                             (data or {}).get("title"),
                                             (data or {}).get("body"), login)
                handler.send_json(201, issue_json(record, base_url, owner, repo))
                return True
            if len(rest) == 2 and method == "GET":
                try:
                    number = int(rest[1])
                except ValueError:
                    handler.send_json(404, {"message": "Not Found"})
                    return True
                for rec in load_issues(rdir):
                    if rec["number"] == number:
                        handler.send_json(200, issue_json(rec, base_url, owner, repo))
                        return True
                handler.send_json(404, {"message": "Not Found"})
                return True
            return False

        if rest and rest[0] == "pulls":
            if len(rest) == 1 and method == "GET":
                items = [pull_json(r, rdir, base_url, owner, repo) for r in load_pulls(rdir)]
                handler.send_json(200, items)
                return True
            if len(rest) == 1 and method == "POST":
                data = data or {}
                head = validate_ref_for_pr(rdir, data.get("head"))
                base = validate_ref_for_pr(rdir, data.get("base"))
                if head is None or base is None:
                    handler.send_json(422, {"message": "Validation Failed",
                                            "errors": [{"field": "head/base",
                                                        "code": "invalid",
                                                        "message": "head and base must be an existing branch or SHA"}]})
                    return True
                record = create_pull_record(rdir, data.get("title"), data.get("body"),
                                            login, head, base)
                handler.send_json(201, pull_json(record, rdir, base_url, owner, repo))
                return True
            if len(rest) == 2 and method == "GET":
                try:
                    number = int(rest[1])
                except ValueError:
                    handler.send_json(404, {"message": "Not Found"})
                    return True
                for rec in load_pulls(rdir):
                    if rec["number"] == number:
                        handler.send_json(200, pull_json(rec, rdir, base_url, owner, repo))
                        return True
                handler.send_json(404, {"message": "Not Found"})
                return True
            if len(rest) == 3 and rest[2] == "files" and method == "GET":
                try:
                    number = int(rest[1])
                except ValueError:
                    handler.send_json(404, {"message": "Not Found"})
                    return True
                for rec in load_pulls(rdir):
                    if rec["number"] == number:
                        handler.send_json(200, pull_files(rdir, rec))
                        return True
                handler.send_json(404, {"message": "Not Found"})
                return True
            return False

    return False


# --------------------------------------------------------------------------
# GraphQL — minimal, correct subset: repository{...} and viewer{login}
# --------------------------------------------------------------------------


def _gql_tokens(query):
    tokens = []
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch in " \t\r\n,":
            i += 1
        elif ch == "#":
            while i < n and query[i] != "\n":
                i += 1
        elif ch in "{}():":
            tokens.append(ch)
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and query[j] != '"':
                if query[j] == "\\" and j + 1 < n:
                    buf.append(query[j + 1])
                    j += 2
                else:
                    buf.append(query[j])
                    j += 1
            tokens.append(("str", "".join(buf)))
            i = j + 1
        elif ch == "$":
            m = re.match(r"\$([_A-Za-z][_0-9A-Za-z]*)", query[i:])
            tokens.append(("var", m.group(1)))
            i += len(m.group(0))
        elif ch.isdigit() or ch == "-":
            m = re.match(r"-?\d+(?:\.\d+)?", query[i:])
            tokens.append(("num", m.group(0)))
            i += len(m.group(0))
        else:
            m = re.match(r"[_A-Za-z][_0-9A-Za-z]*", query[i:])
            if not m:
                i += 1
                continue
            tokens.append(("name", m.group(0)))
            i += len(m.group(0))
    return tokens


def _gql_parse(query, variables):
    """Parse a query into a list of root fields: {name, args, children}."""
    tokens = _gql_tokens(query)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def parse_value():
        nonlocal pos
        tok = peek()
        pos += 1
        if isinstance(tok, tuple):
            kind, val = tok
            if kind == "str":
                return val
            if kind == "var":
                return variables.get(val)
            if kind == "num":
                return float(val) if "." in val else int(val)
            if kind == "name":
                if val == "true":
                    return True
                if val == "false":
                    return False
                return val
        return None

    def parse_selection_set():
        nonlocal pos
        fields = []
        if peek() != "{":
            return fields
        pos += 1
        while pos < len(tokens) and peek() != "}":
            tok = peek()
            if not (isinstance(tok, tuple) and tok[0] == "name"):
                pos += 1
                continue
            name = tok[1]
            pos += 1
            if peek() == ":":  # alias
                pos += 1
                tok = peek()
                if isinstance(tok, tuple):
                    name = tok[1]
                pos += 1
            args = {}
            if peek() == "(":
                pos += 1
                while pos < len(tokens) and peek() != ")":
                    key = peek()
                    if isinstance(key, tuple) and key[0] == "name":
                        pos += 1
                        if peek() == ":":
                            pos += 1
                        args[key[1]] = parse_value()
                    else:
                        pos += 1
                pos += 1
            children = []
            if peek() == "{":
                children = parse_selection_set()
            fields.append({"name": name, "args": args, "children": children})
        pos += 1  # consume '}'
        return fields

    # Skip an optional operation keyword / name / variable definitions.
    if peek() == ("name", "query") or peek() == ("name", "mutation") or peek() == ("name", "subscription"):
        pos += 1
        if isinstance(peek(), tuple) and peek()[0] == "name":
            pos += 1
        if peek() == "(":
            depth = 0
            while pos < len(tokens):
                if peek() == "(":
                    depth += 1
                elif peek() == ")":
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
                pos += 1
    return parse_selection_set()


def handle_graphql(handler, query_text, variables, login, base_url):
    variables = variables or {}
    try:
        root_fields = _gql_parse(query_text or "", variables)
    except Exception as exc:  # malformed query
        handler.send_json(400, {"errors": [{"message": "Parse error: %s" % exc}]})
        return True
    data = {}
    errors = []
    for field in root_fields:
        name = field["name"]
        if name == "viewer":
            obj = {}
            for child in field["children"]:
                if child["name"] == "login":
                    obj["login"] = login
                else:
                    obj[child["name"]] = None
                    errors.append({"message": 'Cannot query field "%s" on type "User".' % child["name"],
                                   "type": "INVALID_FIELD"})
            data["viewer"] = obj
        elif name == "repository":
            owner = field["args"].get("owner")
            repo = field["args"].get("name")
            if not owner or not repo or not repo_exists(str(owner), str(repo)):
                data["repository"] = None
                errors.append({"message": "Could not resolve to a Repository with the name '%s/%s'." % (owner, repo),
                               "type": "NOT_FOUND"})
                continue
            owner, repo = str(owner), str(repo)
            rdir = repo_dir(owner, repo)
            meta = repo_meta(rdir)
            obj = {}
            for child in field["children"]:
                cname = child["name"]
                if cname == "name":
                    obj["name"] = repo
                elif cname == "nameWithOwner":
                    obj["nameWithOwner"] = owner + "/" + repo
                elif cname == "description":
                    obj["description"] = meta.get("description")
                elif cname == "url":
                    obj["url"] = "%s/%s/%s" % (base_url, owner, repo)
                elif cname == "owner":
                    sub = {}
                    for subchild in child["children"]:
                        if subchild["name"] == "login":
                            sub["login"] = owner
                        else:
                            sub[subchild["name"]] = None
                            errors.append({"message": 'Cannot query field "%s" on type "User".' % subchild["name"],
                                           "type": "INVALID_FIELD"})
                    obj["owner"] = sub
                else:
                    obj[cname] = None
                    errors.append({"message": 'Cannot query field "%s" on type "Repository".' % cname,
                                   "type": "INVALID_FIELD"})
            data["repository"] = obj
        else:
            data[name] = None
            errors.append({"message": 'Cannot query field "%s" on type "Query".' % name,
                           "type": "INVALID_FIELD"})
    payload = {"data": data}
    if errors:
        payload["errors"] = errors
    handler.send_json(200, payload)
    return True


# --------------------------------------------------------------------------
# MCP over stdio (JSON-RPC 2.0)
# --------------------------------------------------------------------------

MCP_TOOLS = [
    {"name": "repo_info",
     "description": "Get information about a repository in the forge store.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "get_file",
     "description": "Read a file from a repository at its default branch.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "path": {"type": "string"}},
                     "required": ["owner", "repo", "path"]}},
    {"name": "list_commits",
     "description": "List commits of a repository branch, newest first.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "branch": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "create_pull_request",
     "description": "Create a pull request between two branches or SHAs.",
     "inputSchema": {"type": "object",
                     "properties": {"owner": {"type": "string"},
                                    "repo": {"type": "string"},
                                    "title": {"type": "string"},
                                    "head": {"type": "string"},
                                    "base": {"type": "string"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
]


def _mcp_repo_or_error(owner, repo):
    rdir = repo_dir(owner, repo)
    if rdir is None or not repo_exists(owner, repo):
        raise ValueError("repository not found: %s/%s" % (owner, repo))
    return rdir


def mcp_call_tool(name, args):
    """Run one MCP tool; returns the text payload. Raises ValueError on failure."""
    args = args or {}
    if name == "repo_info":
        owner, repo = str(args.get("owner", "")), str(args.get("repo", ""))
        rdir = _mcp_repo_or_error(owner, repo)
        head_sha = resolve_commit(rdir, "HEAD")
        count = 0
        if head_sha:
            out = git_text(["rev-list", "--count", "HEAD"], rdir)
            count = int(out) if out and out.isdigit() else 0
        info = {"name": repo, "full_name": owner + "/" + repo,
                "owner": {"login": owner},
                "description": repo_meta(rdir).get("description"),
                "default_branch": default_branch(rdir),
                "branches": [b["name"] for b in list_branches(rdir)],
                "commits": count,
                "open_issues": len(load_issues(rdir)),
                "open_pull_requests": len(load_pulls(rdir))}
        return json.dumps(info, indent=2)
    if name == "get_file":
        owner, repo = str(args.get("owner", "")), str(args.get("repo", ""))
        path = str(args.get("path", "")).strip("/")
        rdir = _mcp_repo_or_error(owner, repo)
        if not path or ".." in path.split("/"):
            raise ValueError("invalid path")
        rc, data, _ = run_git(["cat-file", "blob", "HEAD:" + path], repo=rdir)
        if rc != 0:
            raise ValueError("file not found: " + path)
        return data.decode("utf-8", "replace")
    if name == "list_commits":
        owner, repo = str(args.get("owner", "")), str(args.get("repo", ""))
        rdir = _mcp_repo_or_error(owner, repo)
        branch = str(args.get("branch") or default_branch(rdir))
        shas = list_commit_shas(rdir, branch, limit=100)
        commits = []
        for sha in shas:
            commit = read_commit(rdir, sha)
            if commit:
                commits.append({"sha": sha, "message": commit["message"],
                                "author": commit["author"], "committer": commit["committer"],
                                "parents": commit["parents"]})
        return json.dumps(commits, indent=2)
    if name == "create_issue":
        owner, repo = str(args.get("owner", "")), str(args.get("repo", ""))
        rdir = _mcp_repo_or_error(owner, repo)
        record = create_issue_record(rdir, owner, repo, args.get("title"),
                                     args.get("body"), owner)
        return json.dumps(issue_json(record, "", owner, repo), indent=2)
    if name == "create_pull_request":
        owner, repo = str(args.get("owner", "")), str(args.get("repo", ""))
        rdir = _mcp_repo_or_error(owner, repo)
        head = validate_ref_for_pr(rdir, args.get("head"))
        base = validate_ref_for_pr(rdir, args.get("base"))
        if head is None or base is None:
            raise ValueError("head and base must be an existing branch or SHA")
        record = create_pull_record(rdir, args.get("title"), args.get("body"),
                                    owner, head, base)
        return json.dumps(pull_json(record, rdir, "", owner, repo), indent=2)
    raise KeyError(name)


def run_mcp():
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(req, dict):
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "makefun-forge", "version": "1.0.0"}}})
        elif method == "ping":
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": MCP_TOOLS}})
        elif method == "tools/call":
            if rid is None:
                continue
            params = req.get("params") or {}
            name = params.get("name")
            if name not in [t["name"] for t in MCP_TOOLS]:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32602, "message": "Unknown tool: %s" % name}})
                continue
            try:
                text = mcp_call_tool(name, params.get("arguments"))
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": text}], "isError": False}})
            except Exception as exc:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": "Error: %s" % exc}],
                    "isError": True}})
        elif method and method.startswith("notifications/"):
            continue
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "Method not found: %s" % method}})


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

GIT_PATH_RE = re.compile(r"^/[^/]+/[^/]+/(info/refs|git-upload-pack|git-receive-pack)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout/stderr clean
        pass

    def _base_url(self):
        host = self.headers.get("Host", "127.0.0.1")
        return "http://" + host

    def send_bytes(self, status, payload, content_type="application/json",
                   extra_headers=None):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for key, value in extra_headers.items():
                if key.lower() in ("content-type", "content-length"):
                    continue
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode("utf-8"),
                        "application/json")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""

        if path == "/":
            self.send_json(200, {"name": "make.fun git forge", "status": "ok",
                                 "store": ".makefun/forge"})
            return

        if GIT_PATH_RE.match(path):
            handle_git_http(self, method, path, parsed.query, body)
            return

        if path in ("/graphql", "/api/graphql"):
            login = derive_login(self.headers) or "user"
            query_text, variables = None, {}
            if method == "GET":
                query_text = query.get("query", [None])[0]
                raw_vars = query.get("variables", [None])[0]
                if raw_vars:
                    try:
                        variables = json.loads(raw_vars)
                    except ValueError:
                        variables = {}
            else:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    query_text = payload.get("query")
                    variables = payload.get("variables") or {}
                except (ValueError, UnicodeDecodeError):
                    query_text = body.decode("utf-8", "replace")
            handle_graphql(self, query_text, variables, login, self._base_url())
            return

        # Everything below is REST and requires an Authorization header.
        login = derive_login(self.headers)
        if login is None:
            self.send_json(401, {"message": "Requires authentication"})
            return
        data = None
        if body:
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = None
        seg = [s for s in path.split("/") if s != ""]
        if handle_rest(self, method, seg, query, data, login, self._base_url()):
            return
        self.send_json(404, {"message": "Not Found"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("GET")


def main(argv):
    if "--mcp" in argv:
        run_mcp()
        return 0
    port = None
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            port = None
    if port is None and "--port" in argv:
        idx = argv.index("--port")
        if idx + 1 < len(argv):
            try:
                port = int(argv[idx + 1])
            except ValueError:
                port = None
    if port is None:
        port = 8077
    os.makedirs(STORE, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("make.fun forge listening on port %d (store: %s)" % (port, STORE),
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
