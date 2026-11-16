# make.fun git forge

A standalone, GitHub-compatible git forge in a single Python file
(standard library only, plus the `git` CLI). One long-lived process serves,
from one store of bare git repositories:

- **git smart HTTP** — clone / fetch / push (`git http-backend`)
- **REST v3 subset** — repos, contents, git trees/commits/refs, commits,
  branches, issues, pull requests (incl. `/files`), and search
- **GraphQL** — `repository(owner, name) { ... }` and `viewer { login }`
- **MCP over stdio** — `repo_info`, `get_file`, `list_commits`,
  `create_issue`, `create_pull_request`

Repositories live as bare git repos under `.makefun/forge/<owner>/<repo>.git`
relative to the working directory. Issue/PR metadata is stored as JSON files
inside each bare repo, so state persists across restarts and always reflects
real pushes.

## Run

```sh
python3 main.py                 # HTTP on port 8077
PORT=9000 python3 main.py       # port from the environment
python3 main.py --port 9000     # port from argv
python3 main.py --mcp           # MCP server over stdio instead of HTTP
```

Any dummy token works for authentication (Basic or Bearer); REST endpoints
return `401` when no `Authorization` header is sent. Clone/push like GitHub:

```sh
git clone http://token@127.0.0.1:8077/octocat/hello.git
```
