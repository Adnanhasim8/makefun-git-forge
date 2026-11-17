# makefun-git-forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Adnanhasim8/makefun-git-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Adnanhasim8/makefun-git-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

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

## Run it

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

---

<sub>Entered on [make.fun](https://make.fun/?ref=vev5ehp3&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
