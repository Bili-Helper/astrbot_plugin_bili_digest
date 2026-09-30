import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from astrbot_plugin_bili_digest.config import settings
from astrbot_plugin_bili_digest.extract import triggered
from astrbot_plugin_bili_digest.store import Archive


class ReleaseTests(unittest.TestCase):
    def test_regex_timeout_is_ignored(self):
        pattern = Mock()
        pattern.search.side_effect = TimeoutError
        self.assertFalse(
            triggered("text", settings({"trigger_mode": "regex", "trigger_regex": ".*"}), pattern)
        )
        pattern.search.assert_called_once_with("text", timeout=0.05)

    def test_archive_exports_escape_html_and_preserve_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root)
            doc = archive.start(
                "test-scope", "cache-key", "https://www.bilibili.com/video/BV1xx411c7us", "sender"
            )
            doc.update(
                status="success",
                summary="<script>alert(1)</script>",
                metadata={"title": "Test"},
                segments=[{"start": 0, "end": 1, "text": "content"}],
            )
            archive.finish(doc)
            cli = Path(__file__).parents[1] / "archive_cli.py"
            for format_name, suffix in (("html", ".html"), ("jsonl", ".jsonl")):
                output = root / ("export" + suffix)
                subprocess.run(
                    [
                        sys.executable,
                        str(cli),
                        "--data-dir",
                        str(root),
                        "--format",
                        format_name,
                        "--output",
                        str(output),
                    ],
                    check=True,
                    capture_output=True,
                )
                data = output.read_text(encoding="utf-8")
                if format_name == "html":
                    self.assertNotIn("<script>", data)
                    self.assertIn("&lt;script&gt;", data)
                else:
                    self.assertEqual(json.loads(data)["id"], doc["id"])
