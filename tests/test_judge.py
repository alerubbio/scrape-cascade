import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cascade  # noqa: E402


RUBRIC = {
    "name": "unit",
    "description": "Unit-test rubric",
    "positive_label": "capacity_signal",
    "negative_label": "no_capacity_signal",
    "judge_instructions": "Prefer unknown when evidence is thin.",
}


def fake_cli(source):
    tmp = tempfile.NamedTemporaryFile("w", delete=False)
    try:
        tmp.write(textwrap.dedent(source).lstrip())
        tmp.close()
        os.chmod(tmp.name, 0o755)
        return tmp.name
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


class JudgeTests(unittest.TestCase):
    def tearDown(self):
        for attr in ("_fake_claude", "_fake_codex"):
            path = getattr(self, attr, None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def test_claude_envelope_is_unwrapped(self):
        text, errored = cascade._extract_claude_result_text(json.dumps({
            "type": "result",
            "is_error": False,
            "result": '{"label":"capacity_signal","confidence":0.8,"reason":"matched"}',
        }))
        self.assertFalse(errored)
        self.assertIn("capacity_signal", text)

    def test_claude_provider_uses_json_envelope(self):
        self._fake_claude = fake_cli(
            """
            #!/usr/bin/env python3
            import json
            print(json.dumps({
                "type": "result",
                "is_error": False,
                "result": json.dumps({
                    "label": "capacity_signal",
                    "confidence": 0.88,
                    "reason": "strong evidence"
                })
            }))
            """
        )
        got = cascade.judge(
            "We are hiring platform engineers and device administrators.",
            RUBRIC,
            provider="claude",
            judge_bin=self._fake_claude,
            model=None,
        )
        self.assertEqual(got["label"], "capacity_signal")
        self.assertEqual(got["confidence"], 0.88)

    def test_codex_provider_reads_output_last_message(self):
        self._fake_codex = fake_cli(
            """
            #!/usr/bin/env python3
            import json
            import sys
            out = sys.argv[sys.argv.index("-o") + 1]
            with open(out, "w", encoding="utf-8") as f:
                json.dump({
                    "label": "no_capacity_signal",
                    "confidence": 0.74,
                    "reason": "generic site"
                }, f)
            """
        )
        got = cascade.judge(
            "Welcome to our neighborhood restaurant. View our menu.",
            RUBRIC,
            provider="codex",
            judge_bin=self._fake_codex,
            model=None,
        )
        self.assertEqual(got["label"], "no_capacity_signal")
        self.assertEqual(got["confidence"], 0.74)

    def test_label_drift_is_coerced(self):
        self.assertEqual(cascade.coerce_label("Capacity Signal", RUBRIC), "capacity_signal")
        self.assertEqual(cascade.coerce_label("definitely a maybe", RUBRIC), "unknown")


WINDOW_RUBRIC = {"positive": ["soc 2"], "negative": ["consulting"]}


class SelectJudgeTextTests(unittest.TestCase):
    """Pure-function tests for select_judge_text — no LLM required."""

    def test_short_text_returned_unchanged(self):
        short = "This is a short page about our product."
        result = cascade.select_judge_text(short, WINDOW_RUBRIC, max_chars=6000)
        self.assertEqual(result, short)

    def test_buried_term_included(self):
        # Build a long text with the positive term buried past the 6000-char head
        prefix = "x" * 9000
        term = "soc 2"
        text = prefix + term + " certified platform for enterprise security"
        # Sanity: the term is past the head-truncation window
        self.assertNotIn(term, text[:6000])
        result = cascade.select_judge_text(text, WINDOW_RUBRIC, max_chars=6000)
        self.assertIn(term, result)

    def test_ssr_marker_included(self):
        # Long text with no rubric terms but with an SSR marker near the end
        filler = "Welcome to our company. " * 300  # ~7200 chars
        ssr_block = "Embedded job postings (SSR JSON, 12 open roles):\n- Senior Engineer"
        text = filler + ssr_block
        self.assertGreater(len(text), 6000)
        # No rubric terms in filler or ssr_block, so only the head + marker path fires
        result = cascade.select_judge_text(text, WINDOW_RUBRIC, max_chars=6000)
        self.assertIn("Embedded job postings (SSR JSON", result)
        self.assertIn("Senior Engineer", result)

    def test_no_signal_falls_back_to_head(self):
        # Long text with no rubric terms and no synthetic markers
        text = "Generic homepage content without relevant terms. " * 200
        self.assertGreater(len(text), 6000)
        result = cascade.select_judge_text(text, WINDOW_RUBRIC, max_chars=6000)
        self.assertEqual(result, text[:6000])

    def test_length_cap_respected(self):
        # Any long input should not exceed the budget (excluding separator chars)
        text = "soc 2 compliance is critical. " * 500
        self.assertGreater(len(text), 6000)
        result = cascade.select_judge_text(text, WINDOW_RUBRIC, max_chars=6000)
        # Strip the "\n…\n" joiners and assert the character budget is respected
        self.assertLessEqual(len(result.replace("\n…\n", "")), 6000)


if __name__ == "__main__":
    unittest.main()
