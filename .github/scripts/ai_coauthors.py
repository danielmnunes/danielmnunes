#!/usr/bin/env python3
"""
Varre os repositórios públicos do usuário no GitHub, procura trailers
"Co-authored-by:" nos commits e agrega quais AGENTES DE IA colaboraram.
O resultado é injetado no README.md entre os marcadores:
    <!-- AI-COAUTHORS:START --> ... <!-- AI-COAUTHORS:END -->

Uso:
    GITHUB_TOKEN=xxx python ai_coauthors.py --user danielmnunes --readme README.md

Requer apenas a biblioteca padrão do Python.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
COAUTHOR_RE = re.compile(r"co-authored-by:\s*(.+?)\s*<([^>]+)>", re.IGNORECASE)

# Assinaturas conhecidas de agentes de IA -> (nome amigável, badge shields.io).
# A checagem é por substring no e-mail e no nome do co-autor (minúsculo).
AI_AGENTS = [
    ("anthropic.com",           "Claude (Anthropic)",  "https://img.shields.io/badge/Claude-D97757?style=flat-square&logo=anthropic&logoColor=white"),
    ("claude",                  "Claude (Anthropic)",  "https://img.shields.io/badge/Claude-D97757?style=flat-square&logo=anthropic&logoColor=white"),
    ("copilot",                 "GitHub Copilot",      "https://img.shields.io/badge/GitHub%20Copilot-000000?style=flat-square&logo=githubcopilot&logoColor=white"),
    ("cursor",                  "Cursor",              "https://img.shields.io/badge/Cursor-000000?style=flat-square&logo=cursor&logoColor=white"),
    ("devin-ai-integration",    "Devin",              "https://img.shields.io/badge/Devin-111111?style=flat-square&logoColor=white"),
    ("aider",                   "Aider",               "https://img.shields.io/badge/Aider-14A800?style=flat-square&logoColor=white"),
    ("openai",                  "OpenAI Codex",        "https://img.shields.io/badge/OpenAI%20Codex-412991?style=flat-square&logo=openai&logoColor=white"),
    ("chatgpt",                 "OpenAI Codex",        "https://img.shields.io/badge/OpenAI%20Codex-412991?style=flat-square&logo=openai&logoColor=white"),
    ("codex",                   "OpenAI Codex",        "https://img.shields.io/badge/OpenAI%20Codex-412991?style=flat-square&logo=openai&logoColor=white"),
    ("gemini",                  "Gemini",              "https://img.shields.io/badge/Gemini-8E75B2?style=flat-square&logo=googlegemini&logoColor=white"),
    ("windsurf",                "Windsurf",            "https://img.shields.io/badge/Windsurf-58C4DC?style=flat-square&logoColor=white"),
    ("sourcegraph",             "Cody (Sourcegraph)",  "https://img.shields.io/badge/Cody-FF5543?style=flat-square&logoColor=white"),
    ("cody",                    "Cody (Sourcegraph)",  "https://img.shields.io/badge/Cody-FF5543?style=flat-square&logoColor=white"),
]


def match_agent(name, email):
    hay = f"{name} {email}".lower()
    for needle, label, badge in AI_AGENTS:
        if needle in hay:
            return label, badge
    return None


def gh_get(url, token):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "ai-coauthors-script")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()), resp.headers


def paginate(url, token, max_pages=10):
    page = 1
    while page <= max_pages:
        sep = "&" if "?" in url else "?"
        data, _ = gh_get(f"{url}{sep}per_page=100&page={page}", token)
        if not data:
            break
        yield from data
        if len(data) < 100:
            break
        page += 1


def list_repos(user, token):
    for repo in paginate(f"{API}/users/{user}/repos?type=owner&sort=pushed", token):
        if not repo.get("fork"):
            yield repo["full_name"]


def scan_repo(full_name, user, token, max_pages=5):
    """Retorna lista de (agent_label, badge) por commit encontrado."""
    hits = []
    url = f"{API}/repos/{full_name}/commits?author={user}"
    try:
        for commit in paginate(url, token, max_pages=max_pages):
            msg = (commit.get("commit") or {}).get("message", "")
            for m in COAUTHOR_RE.finditer(msg):
                agent = match_agent(m.group(1), m.group(2))
                if agent:
                    hits.append(agent)
    except urllib.error.HTTPError as e:
        if e.code in (409, 404):  # repo vazio ou sem acesso
            return hits
        raise
    return hits


def build_section(counts, repos_per_agent):
    if not counts:
        return ("_Nenhum agente de IA co-autor encontrado ainda. "
                "Adicione `Co-authored-by:` nos seus commits para aparecer aqui._")
    order = sorted(counts, key=lambda k: counts[k][0], reverse=True)
    lines = [
        "Agentes de IA que colaboraram nos meus commits "
        "(via trailer `Co-authored-by:`), agregados automaticamente:",
        "",
        "| Agente | Commits | Repositórios |",
        "| --- | :---: | :---: |",
    ]
    for label in order:
        badge, total = counts[label][1], counts[label][0]
        nrepos = len(repos_per_agent[label])
        lines.append(f"| ![{label}]({badge}) | {total} | {nrepos} |")
    lines.append("")
    lines.append("<sub>Atualizado automaticamente por GitHub Actions.</sub>")
    return "\n".join(lines)


def inject(readme_path, section):
    start, end = "<!-- AI-COAUTHORS:START -->", "<!-- AI-COAUTHORS:END -->"
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()
    block = f"{start}\n{section}\n{end}"
    if start in content and end in content:
        new = re.sub(re.escape(start) + r".*?" + re.escape(end), block,
                     content, flags=re.DOTALL)
    else:
        new = content.rstrip() + "\n\n" + block + "\n"
    if new != content:
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(new)
        print("README atualizado.")
    else:
        print("Nenhuma mudança no README.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--readme", default="README.md")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("METRICS_TOKEN")

    counts = {}            # label -> [total_commits, badge]
    repos_per_agent = {}   # label -> set(repos)
    for full_name in list_repos(args.user, token):
        for label, badge in scan_repo(full_name, args.user, token):
            counts.setdefault(label, [0, badge])
            counts[label][0] += 1
            repos_per_agent.setdefault(label, set()).add(full_name)
        print(f"scanned {full_name}", file=sys.stderr)

    inject(args.readme, build_section(counts, repos_per_agent))


if __name__ == "__main__":
    main()
