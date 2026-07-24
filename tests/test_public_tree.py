#!/usr/bin/env python3
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".txt"}


class PublicTreeTests(unittest.TestCase):
    def text_files(self):
        for path in ROOT.rglob("*"):
            if path.resolve() == Path(__file__).resolve():
                continue
            if ".git" in path.parts or not path.is_file():
                continue
            if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
                yield path

    def test_tree_contains_no_personal_paths_or_private_model_aliases(self):
        forbidden = (
            "/Users/mac",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "amduwcwt@",
            "codex-grok-bridge",
        )
        violations = []
        path_patterns = (
            re.compile(r"/Users/[^/\s]+"),
            re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
        )
        for path in self.text_files():
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    violations.append(f"{path.relative_to(ROOT)}: {marker}")
            for pattern in path_patterns:
                if pattern.search(text):
                    violations.append(f"{path.relative_to(ROOT)}: personal home path")
        self.assertEqual(violations, [])

    def test_skill_ui_prompts_keep_literal_trigger_names(self):
        expected = {
            "agent-cli-workers": "$agent-cli-workers",
            "grok-build-cli": "$grok-build-cli",
        }
        for skill, trigger in expected.items():
            path = ROOT / "skills" / skill / "agents" / "openai.yaml"
            self.assertIn(trigger, path.read_text(encoding="utf-8"))

    def test_skill_frontmatter_has_only_name_and_description(self):
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
            self.assertIsNotNone(match, path)
            keys = {
                line.split(":", 1)[0].strip()
                for line in match.group(1).splitlines()
                if ":" in line
            }
            self.assertEqual(keys, {"name", "description"}, path)

    def test_long_research_omits_turn_caps_by_default(self):
        shared = (ROOT / "skills" / "agent-cli-workers" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        grok = (ROOT / "skills" / "grok-build-cli" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for text in (shared, grok):
            self.assertIn("Omit `--max-turns` for multi-source research", text)
            self.assertIn("do not `followup` that native session", text)
        self.assertNotIn("--max-turns 4", shared)


if __name__ == "__main__":
    unittest.main()
