#!/usr/bin/env python3
"""
Varre os repositórios públicos do usuário no GitHub e agrega quais AGENTES
DE IA colaboraram nos commits. Duas fontes:

  * ``Co-authored-by: Nome <email>`` — só entra se o nome ou o e-mail bater
    com um agente conhecido;
  * ``Assisted-by: <modelo> and <agente>`` — o texto antes do último
    `` and `` é o modelo; o texto depois é o agente (pode conter espaços).
    Os dois lados NÃO viram dois agentes. O agente é normalizado para a
    mesma linha do ``Co-authored-by`` quando é a mesma ferramenta
    (``AGENT_ALIASES``). Cada commit conta uma vez por agente. Os modelos
    distintos (só os que vêm de ``Assisted-by``) aparecem na coluna Modelos.

``Assisted-by`` segue a convenção de trailer do Git: chave no começo da
linha, no parágrafo final, separada do corpo por uma linha em branco,
sem diferenciar maiúsculas. Menções no meio do texto são ignoradas.

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
import urllib.parse
import urllib.request

API = "https://api.github.com"
COAUTHOR_RE = re.compile(r"co-authored-by:\s*(.+?)\s*<([^>]+)>", re.IGNORECASE)
# Separador exigido pelo commit-msg hook: "MODEL and AGENT".
# O último " and " separa os dois; o nome do agente pode ter espaços.
_AND_SEP = re.compile(r"\s+and\s+", re.IGNORECASE)
_EMAIL_SUFFIX = re.compile(r"\s*<[^>]*>\s*$")
_TRAILER_PREFIXES = (
    "Signed-off-by: ",
    "(cherry picked from commit ",
)

# Rótulo canônico -> badge shields.io (flat-square, igual ao restante do README).
BADGES = {
    "Claude (Anthropic)": "https://img.shields.io/badge/Claude-D97757?style=flat-square&logo=anthropic&logoColor=white",
    "GitHub Copilot": "https://img.shields.io/badge/GitHub%20Copilot-000000?style=flat-square&logo=githubcopilot&logoColor=white",
    "Cursor": "https://img.shields.io/badge/Cursor-000000?style=flat-square&logo=cursor&logoColor=white",
    "Devin": "https://img.shields.io/badge/Devin-111111?style=flat-square&logoColor=white",
    "Aider": "https://img.shields.io/badge/Aider-14A800?style=flat-square&logoColor=white",
    "OpenAI Codex": "https://img.shields.io/badge/OpenAI%20Codex-412991?style=flat-square&logo=openai&logoColor=white",
    "Gemini": "https://img.shields.io/badge/Gemini-8E75B2?style=flat-square&logo=googlegemini&logoColor=white",
    "Windsurf": "https://img.shields.io/badge/Windsurf-58C4DC?style=flat-square&logoColor=white",
    "Cody (Sourcegraph)": "https://img.shields.io/badge/Cody-FF5543?style=flat-square&logoColor=white",
    "Grok": "https://img.shields.io/badge/Grok-000000?style=flat-square",
    "OpenCode": "https://img.shields.io/badge/OpenCode-000000?style=flat-square&logo=opencode&logoColor=white",
    "Antigravity CLI": "https://img.shields.io/badge/Antigravity%20CLI-4285F4?style=flat-square",
}

# Substring (minúscula) no nome ou e-mail de um Co-authored-by -> rótulo.
# A ordem importa: a primeira que aparecer ganha.
AI_AGENT_NEEDLES = [
    ("anthropic.com", "Claude (Anthropic)"),
    ("claude", "Claude (Anthropic)"),
    ("copilot", "GitHub Copilot"),
    ("cursor", "Cursor"),
    ("devin-ai-integration", "Devin"),
    ("aider", "Aider"),
    ("openai", "OpenAI Codex"),
    ("chatgpt", "OpenAI Codex"),
    ("codex", "OpenAI Codex"),
    ("gemini", "Gemini"),
    ("windsurf", "Windsurf"),
    ("sourcegraph", "Cody (Sourcegraph)"),
    ("cody", "Cody (Sourcegraph)"),
    ("grok", "Grok"),
    ("opencode", "OpenCode"),
    ("antigravity", "Antigravity CLI"),
]

# Nome exato do agente em Assisted-by (já normalizado) -> rótulo canônico.
# Funde com a linha que o Co-authored-by já usa quando é a mesma ferramenta.
AGENT_ALIASES = {
    "grok": "Grok",
    "codex": "OpenAI Codex",
    "openai codex": "OpenAI Codex",
    "github copilot": "GitHub Copilot",
    "github copilot cli": "GitHub Copilot",
    "copilot": "GitHub Copilot",
    "opencode": "OpenCode",
    "open code": "OpenCode",
    "antigravity": "Antigravity CLI",
    "antigravity cli": "Antigravity CLI",
    "claude": "Claude (Anthropic)",
    "claude (anthropic)": "Claude (Anthropic)",
    "cursor": "Cursor",
    "gemini": "Gemini",
    "devin": "Devin",
    "aider": "Aider",
    "windsurf": "Windsurf",
    "cody": "Cody (Sourcegraph)",
    "cody (sourcegraph)": "Cody (Sourcegraph)",
    "sourcegraph": "Cody (Sourcegraph)",
}


def _fold(name):
    return " ".join(name.split()).casefold()


def match_agent(name, email):
    """Reconhece um co-autor conhecido. None se não for agente de IA."""
    hay = f"{name} {email}".lower()
    for needle, label in AI_AGENT_NEEDLES:
        if needle in hay:
            return label, BADGES[label]
    return None


def canonical_agent(name, email="", allow_unknown=False):
    """Devolve (rótulo, badge) para um nome de agente.

    Atalhos exatos de AGENT_ALIASES ganham da busca por substring, para que
    "Codex" e "GitHub Copilot CLI" caiam na mesma linha dos co-autores.
    """
    folded = _fold(name)
    label = AGENT_ALIASES.get(folded)
    if label:
        return label, BADGES[label]
    matched = match_agent(name, email)
    if matched:
        return matched
    if not allow_unknown:
        return None
    label = " ".join(name.split())
    return label, BADGES.get(label) or _badge_for_unknown(label)


def _badge_for_unknown(label):
    # shields.io: "_" é espaço, "__" é underscore, "--" é hífen.
    escaped = label.replace("_", "__").replace(" ", "_").replace("-", "--")
    escaped = urllib.parse.quote(escaped, safe="-_")
    return f"https://img.shields.io/badge/{escaped}-555555?style=flat-square"


def _is_blank(line):
    return line.strip() == ""


def _find_separator(line):
    """Índice de ':' num token de trailer, ou -1. Espelha o Git (separator ':')."""
    whitespace_found = False
    for i, char in enumerate(line):
        if char == ":":
            return i
        if not whitespace_found and (char.isalnum() or char == "-"):
            continue
        if i != 0 and char in " \t":
            whitespace_found = True
            continue
        break
    return -1


def _without_patch(text, lines):
    """Corta o diff introduzido por uma linha '---' (convenção do Git)."""
    for i, line in enumerate(lines):
        if not line.startswith("---"):
            continue
        rest = line[3:]
        if rest == "":
            followed_by_newline = i < len(lines) - 1 or text.endswith("\n")
            if followed_by_newline:
                return lines[:i]
        elif rest[0].isspace():
            return lines[:i]
    return lines


def parse_trailers(message):
    """Devolve [(chave, valor), ...] do bloco de trailers do commit.

    Mesma regra do ``git interpret-trailers --parse``: o bloco é o parágrafo
    final, precisa vir depois de uma linha em branco, e ou é só trailers ou
    (se tiver um trailer gerado pelo Git, como Signed-off-by) tem pelo menos
    25% de linhas de trailer. Linhas de continuação começam com espaço.
    """
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    lines = _without_patch(text, lines)
    if not lines:
        return []

    end_of_title = None
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        if _is_blank(line):
            end_of_title = i
            break
    if end_of_title is None:
        return []

    trailer_lines = 0
    non_trailer_lines = 0
    possible_continuation = 0
    recognized = False
    only_spaces = True
    start = None
    for i in range(len(lines) - 1, end_of_title - 1, -1):
        bol = lines[i]
        if bol.startswith("#"):
            non_trailer_lines += possible_continuation
            possible_continuation = 0
            continue
        if _is_blank(bol):
            if only_spaces:
                continue
            non_trailer_lines += possible_continuation
            enough = recognized and trailer_lines * 3 >= non_trailer_lines
            if enough or (trailer_lines and non_trailer_lines == 0):
                start = i + 1
                break
            return []
        only_spaces = False
        if bol.startswith(_TRAILER_PREFIXES):
            trailer_lines += 1
            possible_continuation = 0
            recognized = True
            continue
        if _find_separator(bol) >= 1 and bol[:1] not in " \t":
            trailer_lines += 1
            possible_continuation = 0
            continue
        if bol[:1] in " \t":
            possible_continuation += 1
        else:
            non_trailer_lines += 1 + possible_continuation
            possible_continuation = 0
    if start is None:
        return []

    block = lines[start:]
    while block and _is_blank(block[-1]):
        block.pop()

    parsed = []
    for line in block:
        if line.startswith("#"):
            continue
        if parsed and line[:1] in " \t":
            key, value = parsed[-1]
            parsed[-1] = (key, f"{value} {line.strip()}")
            continue
        sep = _find_separator(line)
        if sep >= 1 and line[:1] not in " \t":
            parsed.append((line[:sep].strip(), line[sep + 1 :].strip()))
    return parsed


def split_model_and_agent(value):
    """Separa ``MODEL and AGENT`` pelo último ' and '.

    Sem o separador, o valor inteiro é o agente e não há modelo.
    """
    value = value.strip()
    if not value:
        return None, None
    matches = list(_AND_SEP.finditer(value))
    if not matches:
        agent = _clean_token(value)
        return None, agent or None
    last = matches[-1]
    model = _clean_token(value[: last.start()])
    agent = _clean_token(value[last.end() :])
    if not agent:
        return None, None
    return model or None, agent


def _clean_token(text):
    text = _EMAIL_SUFFIX.sub("", text.strip())
    return text.strip(" \t.,;")


def assisted_by_entries(message):
    """Lista (modelo ou None, agente) de cada trailer Assisted-by."""
    entries = []
    for key, value in parse_trailers(message):
        if key.casefold() != "assisted-by":
            continue
        model, agent = split_model_and_agent(value)
        if agent:
            entries.append((model, agent))
    return entries


def agents_from_message(message):
    """Agentes de um commit: {rótulo: {'badge', 'models'}}.

    O mesmo agente no mesmo commit conta uma vez. Modelos só entram quando
    vêm de Assisted-by.
    """
    found = {}
    for match in COAUTHOR_RE.finditer(message):
        agent = canonical_agent(match.group(1), match.group(2), allow_unknown=False)
        if not agent:
            continue
        label, badge = agent
        slot = found.setdefault(label, {"badge": badge, "models": set()})
        slot["badge"] = badge
    for model, agent_name in assisted_by_entries(message):
        label, badge = canonical_agent(agent_name, allow_unknown=True)
        slot = found.setdefault(label, {"badge": badge, "models": set()})
        slot["badge"] = badge
        if model:
            slot["models"].add(model)
    return found


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
    """Devolve (label, badge, models) por commit, sem repetir o SHA."""
    hits = []
    seen = set()
    url = f"{API}/repos/{full_name}/commits?author={user}"
    try:
        for commit in paginate(url, token, max_pages=max_pages):
            sha = commit.get("sha")
            if sha:
                if sha in seen:
                    continue
                seen.add(sha)
            message = (commit.get("commit") or {}).get("message", "")
            for label, info in agents_from_message(message).items():
                hits.append((label, info["badge"], info["models"]))
    except urllib.error.HTTPError as e:
        if e.code in (409, 404):  # repo vazio ou sem acesso
            return hits
        raise
    return hits


def _format_models(models):
    if not models:
        return "—"
    names = sorted(models, key=str.casefold)
    cells = ", ".join(f"`{name.replace('`', '')}`" for name in names)
    return cells.replace("|", "\\|")


def build_section(counts, repos_per_agent, models_per_agent):
    if not counts:
        return (
            "_Nenhum agente de IA co-autor encontrado ainda. "
            "Adicione `Co-authored-by:` ou `Assisted-by:` nos seus commits para aparecer aqui._"
        )
    order = sorted(counts, key=lambda k: (-counts[k][0], k.casefold()))
    lines = [
        "Agentes de IA que colaboraram nos meus commits "
        "(via trailers `Co-authored-by:` e `Assisted-by:`), agregados automaticamente:",
        "",
        "| Agente | Commits | Repositórios | Modelos |",
        "| --- | :---: | :---: | --- |",
    ]
    for label in order:
        badge, total = counts[label][1], counts[label][0]
        nrepos = len(repos_per_agent.get(label, ()))
        models = _format_models(models_per_agent.get(label, ()))
        lines.append(f"| ![{label}]({badge}) | {total} | {nrepos} | {models} |")
    lines.append("")
    lines.append(
        "<sub>Atualizado automaticamente por GitHub Actions. "
        "O trailer <code>Assisted-by:</code> usa o formato "
        "<code>modelo and agente</code>.</sub>"
    )
    return "\n".join(lines)


def inject(readme_path, section):
    start, end = "<!-- AI-COAUTHORS:START -->", "<!-- AI-COAUTHORS:END -->"
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()
    block = f"{start}\n{section}\n{end}"
    if start in content and end in content:
        new = re.sub(
            re.escape(start) + r".*?" + re.escape(end),
            lambda _: block,
            content,
            count=1,
            flags=re.DOTALL,
        )
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
    models_per_agent = {}  # label -> set(modelos)
    for full_name in list_repos(args.user, token):
        for label, badge, models in scan_repo(full_name, args.user, token):
            slot = counts.setdefault(label, [0, badge])
            slot[0] += 1
            slot[1] = badge
            repos_per_agent.setdefault(label, set()).add(full_name)
            models_per_agent.setdefault(label, set()).update(models)
        print(f"scanned {full_name}", file=sys.stderr)

    if counts:
        for label in sorted(counts, key=lambda k: (-counts[k][0], k.casefold())):
            mods = ", ".join(sorted(models_per_agent.get(label, ()), key=str.casefold)) or "-"
            print(
                f"{label}: {counts[label][0]} commits, "
                f"{len(repos_per_agent[label])} repos, models: {mods}",
                file=sys.stderr,
            )
    else:
        print("nenhum agente encontrado", file=sys.stderr)

    inject(args.readme, build_section(counts, repos_per_agent, models_per_agent))


if __name__ == "__main__":
    main()
