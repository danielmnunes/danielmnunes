"""Testes do gerador de AI Co-Authors (trailers Co-authored-by e Assisted-by)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_coauthors as ac

HOOK_EXAMPLES = [
    ("grok-4.5 and Grok", "grok-4.5", "Grok"),
    ("gemini-3.8-flash and Antigravity CLI", "gemini-3.8-flash", "Antigravity CLI"),
    ("gpt-6.1-sol and Codex", "gpt-6.1-sol", "OpenAI Codex"),
    ("claude-haiku-5.5 and GitHub Copilot CLI", "claude-haiku-5.5", "GitHub Copilot"),
    ("claude-sonnet-5.5 and GitHub Copilot CLI", "claude-sonnet-5.5", "GitHub Copilot"),
    ("gpt-6-luna and GitHub Copilot CLI", "gpt-6-luna", "GitHub Copilot"),
    ("qwen3.8-max and OpenCode", "qwen3.8-max", "OpenCode"),
    ("minimax-m3 and OpenCode", "minimax-m3", "OpenCode"),
    ("deepseek-v4.1-flash and OpenCode", "deepseek-v4.1-flash", "OpenCode"),
]

REAL_COMMIT = """\
test: adiciona o setup inicial de PedidoControllerTest

O primeiro teste confirma que a suíte JUnit executa.

Assisted-by: grok-4.7 and Grok
"""


def _commit(body, trailer):
    return f"fix: coisa\n\n{body}\n\n{trailer}\n" if body else f"fix: coisa\n\n{trailer}\n"


class SplitTests(unittest.TestCase):
    def test_hook_examples_keep_agent_with_spaces(self):
        for value, model, label in HOOK_EXAMPLES:
            with self.subTest(value=value):
                parsed_model, agent = ac.split_model_and_agent(value)
                self.assertEqual(parsed_model, model)
                resolved, _badge = ac.canonical_agent(agent, allow_unknown=True)
                self.assertEqual(resolved, label)

    def test_last_and_is_the_separator(self):
        model, agent = ac.split_model_and_agent("foo and bar and OpenCode")
        self.assertEqual(model, "foo and bar")
        self.assertEqual(agent, "OpenCode")

    def test_commas_stay_inside_the_model(self):
        model, agent = ac.split_model_and_agent("Claude, Cursor, and Copilot")
        self.assertEqual(model, "Claude, Cursor")
        self.assertEqual(agent, "Copilot")

    def test_missing_separator_is_agent_only(self):
        model, agent = ac.split_model_and_agent("Grok")
        self.assertIsNone(model)
        self.assertEqual(agent, "Grok")


class TrailerTests(unittest.TestCase):
    def test_real_commit_is_one_grok_with_model(self):
        found = ac.agents_from_message(REAL_COMMIT)
        self.assertEqual(set(found), {"Grok"})
        self.assertEqual(found["Grok"]["models"], {"grok-4.7"})

    def test_model_name_does_not_become_its_own_agent(self):
        message = _commit("", "Assisted-by: gemini-3.8-flash and Antigravity CLI")
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"Antigravity CLI"})
        self.assertNotIn("Gemini", found)
        self.assertEqual(found["Antigravity CLI"]["models"], {"gemini-3.8-flash"})

    def test_claude_model_with_copilot_cli_is_copilot(self):
        message = _commit("", "Assisted-by: claude-haiku-5.5 and GitHub Copilot CLI")
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"GitHub Copilot"})
        self.assertNotIn("Claude (Anthropic)", found)

    def test_case_insensitive_key(self):
        message = _commit("", "assisted-by: gpt-6.1-sol and Codex")
        found = ac.agents_from_message(message)
        self.assertEqual(found["OpenAI Codex"]["models"], {"gpt-6.1-sol"})

    def test_ignores_mention_outside_the_trailer_block(self):
        message = (
            "fix: coisa\n\n"
            "O corpo cita Assisted-by: grok-4.5 and Grok no meio da frase.\n\n"
            "Conclusão sem trailer.\n"
        )
        self.assertEqual(ac.assisted_by_entries(message), [])
        self.assertEqual(ac.agents_from_message(message), {})

    def test_requires_blank_line_before_the_trailer(self):
        message = "fix: coisa\nAssisted-by: grok-4.5 and Grok\n"
        self.assertEqual(ac.assisted_by_entries(message), [])

    def test_key_must_start_the_line(self):
        message = _commit("", "Usei Assisted-by: grok-4.5 and Grok ontem.")
        self.assertEqual(ac.assisted_by_entries(message), [])

    def test_folded_continuation(self):
        message = "fix: coisa\n\nAssisted-by: grok-4.7\n and Grok\n"
        self.assertEqual(
            ac.assisted_by_entries(message),
            [("grok-4.7", "Grok")],
        )

    def test_patch_divider_hides_what_follows(self):
        message = "fix: coisa\n\nAssisted-by: grok-4.5 and Grok\n---\nAssisted-by: gpt-6.1-sol and Codex\n"
        entries = ac.assisted_by_entries(message)
        self.assertEqual(entries, [("grok-4.5", "Grok")])

    def test_coauthor_still_detected_and_deduped_with_assisted_by(self):
        message = _commit(
            "",
            "Co-authored-by: OpenAI Codex <codex@openai.com>\n"
            "Assisted-by: gpt-6.1-sol and Codex",
        )
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"OpenAI Codex"})
        self.assertEqual(found["OpenAI Codex"]["models"], {"gpt-6.1-sol"})

    def test_same_commit_two_models_one_count(self):
        message = _commit(
            "",
            "Assisted-by: grok-4.5 and Grok\nAssisted-by: grok-4.7 and Grok",
        )
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"Grok"})
        self.assertEqual(found["Grok"]["models"], {"grok-4.5", "grok-4.7"})

    def test_two_agents_in_one_commit(self):
        message = _commit(
            "",
            "Assisted-by: grok-4.5 and Grok\n"
            "Assisted-by: deepseek-v4.1-flash and OpenCode",
        )
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"Grok", "OpenCode"})

    def test_coauthor_without_email_style_still_matches_known_agents(self):
        message = "fix: coisa\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n"
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"Cursor"})
        self.assertEqual(found["Cursor"]["models"], set())

    def test_unknown_human_coauthor_is_ignored(self):
        message = "fix: coisa\n\nCo-authored-by: Ada Lovelace <ada@example.com>\n"
        self.assertEqual(ac.agents_from_message(message), {})

    def test_unknown_assisted_by_agent_is_kept(self):
        message = _commit("", "Assisted-by: some-model and Brand New Agent")
        found = ac.agents_from_message(message)
        self.assertEqual(set(found), {"Brand New Agent"})
        self.assertIn("Brand_New_Agent", found["Brand New Agent"]["badge"])


class GitParityTests(unittest.TestCase):
    samples = {
        "example": REAL_COMMIT,
        "case": "fix: thing\n\nassisted-by: Claude and Cursor\n",
        "midbody": "fix: thing\n\nThis line mentions Assisted-by: NotATool in the body.\n\nDone.\n",
        "not_start": "fix: thing\n\nI was Assisted-by: Grok yesterday.\n",
        "with_other": (
            "fix: thing\n\n"
            "Co-authored-by: Cursor <cursoragent@cursor.com>\n"
            "Assisted-by: grok-4.7, Grok\n"
        ),
        "folded": "fix: thing\n\nAssisted-by: grok-4.7\n and Grok\n",
        "no_blank": "fix: thing\nAssisted-by: Grok\n",
        "prose_same_para": "fix: thing\n\nThanks to the team\nAssisted-by: Grok\n",
        "dashes": "fix: thing\n\nAssisted-by: Grok\n---\ndiff --git a/file\n",
        "multiple": "fix: thing\n\nAssisted-by: Cursor\nAssisted-by: Claude\n",
        "ws_colon": "fix: thing\n\nAssisted-by : Grok\n",
        "signed_mix": (
            "fix: thing\n\n"
            "not a trailer\n"
            "Assisted-by: Grok\n"
            "Signed-off-by: Me <me@me.com>\n"
            "Also thanks\n"
        ),
    }

    def test_matches_git_interpret_trailers(self):
        env = os.environ.copy()
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        for name, message in self.samples.items():
            with self.subTest(name=name):
                proc = subprocess.run(
                    ["git", "interpret-trailers", "--parse"],
                    input=message,
                    text=True,
                    capture_output=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                expected = []
                for line in proc.stdout.splitlines():
                    key, value = line.split(":", 1)
                    expected.append((key.strip(), value.strip()))
                self.assertEqual(ac.parse_trailers(message), expected)


class SectionTests(unittest.TestCase):
    def test_table_is_portuguese_and_lists_models(self):
        counts = {
            "OpenAI Codex": [2, ac.BADGES["OpenAI Codex"]],
            "Grok": [1, ac.BADGES["Grok"]],
            "Cursor": [4, ac.BADGES["Cursor"]],
        }
        repos = {
            "OpenAI Codex": {"danielmnunes/pedidos-api"},
            "Grok": {"danielmnunes/pedidos-api"},
            "Cursor": {"danielmnunes/a", "danielmnunes/b"},
        }
        models = {
            "OpenAI Codex": {"gpt-6.1-sol", "gpt-6-luna"},
            "Grok": {"grok-4.7"},
            "Cursor": set(),
        }
        section = ac.build_section(counts, repos, models)
        self.assertIn("Co-authored-by:", section)
        self.assertIn("Assisted-by:", section)
        self.assertIn("| Agente | Commits | Repositórios | Modelos |", section)
        self.assertIn("`gpt-6-luna`, `gpt-6.1-sol`", section)
        self.assertIn("`grok-4.7`", section)
        self.assertIn("| — |", section)
        self.assertIn("modelo and agente", section)
        # Cursor tem mais commits, fica primeiro; Codex (2) antes de Grok (1).
        cursor_at = section.index("Cursor-000000")
        codex_at = section.index("OpenAI%20Codex")
        grok_at = section.index("badge/Grok-")
        self.assertLess(cursor_at, codex_at)
        self.assertLess(codex_at, grok_at)

    def test_empty_state_mentions_both_trailers(self):
        section = ac.build_section({}, {}, {})
        self.assertIn("Co-authored-by:", section)
        self.assertIn("Assisted-by:", section)

    def test_inject_replaces_marker_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text(
                "antes\n<!-- AI-COAUTHORS:START -->\nantigo\n<!-- AI-COAUTHORS:END -->\ndepois\n",
                encoding="utf-8",
            )
            ac.inject(path, "novo bloco\ncom Assisted-by:")
            text = path.read_text(encoding="utf-8")
        self.assertIn("novo bloco", text)
        self.assertIn("Assisted-by:", text)
        self.assertNotIn("antigo", text)
        self.assertTrue(text.startswith("antes\n"))
        self.assertTrue(text.endswith("depois\n"))

    def test_every_alias_and_needle_has_a_badge(self):
        for label in AGENT_LABELS():
            self.assertIn(label, ac.BADGES)


def AGENT_LABELS():
    labels = set(ac.AGENT_ALIASES.values())
    labels.update(label for _needle, label in ac.AI_AGENT_NEEDLES)
    return labels


if __name__ == "__main__":
    unittest.main()
