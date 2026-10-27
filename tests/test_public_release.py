import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "public_release", Path(__file__).resolve().parents[1] / "scripts" / "public_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PublicReleaseTests(unittest.TestCase):
    def write(self, root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_export_uses_allowlist_without_private_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "README.md", "public documentation")
            self.write(root, ".env.example", "OSA_MODEL_API_KEY=\n")
            self.write(root, ".env", "OSA_MODEL_API_KEY=private-value-for-test-only\n")
            self.write(root, "src/osa/example.py", "value = 'public'\n")
            for name in ("data/private/file.py", "exports/private.txt", "tmp/probe.py",
                         ".codex-remote-attachments/card.txt", "docs/private-report.md",
                         "scripts/private_probe.py", "tests/__pycache__/cache.py",
                         "src/osa/logs/raw.py", "tests/private/file.py"):
                self.write(root, name, "private-value-for-test-only")
            output = root / "dist" / "public"
            result = release.export_source(root, output)
            manifest = json.loads((output / "public-source-manifest.json").read_text())
            self.assertEqual(result["files"], 3)
            self.assertEqual(set(manifest["files"]), {"README.md", ".env.example", "src/osa/example.py"})
            self.assertNotIn("private-value-for-test-only", (output / "public-source-manifest.json").read_text())

    def test_known_credentials_block_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "private-model-credential-fixture"
            self.write(root, ".env", "OSA_MODEL_API_KEY=" + secret + "\n")
            self.write(root, "README.md", "accidentally pasted " + secret)
            output = root / "dist" / "public"
            with self.assertRaises(ValueError) as error:
                release.export_source(root, output)
            self.assertIn("known-private-value", str(error.exception))
            self.assertNotIn(secret, str(error.exception))
            self.assertFalse(output.exists())

    def test_custom_private_identifiers_block_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, ".public-release-private.json", json.dumps(["private-identity-fixture"]))
            self.write(root, "README.md", "private-identity-fixture")
            with self.assertRaisesRegex(ValueError, "known-private-value"):
                release.export_source(root, root / "dist" / "public")

    def test_manifest_hashes_use_canonical_utf8_lf_content(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "README.md", "temporary")
            (root / "README.md").write_bytes(b"\xef\xbb\xbfpublic\r\nsource\r\n")
            output = root / "dist" / "public"
            release.export_source(root, output)
            raw = (output / "README.md").read_bytes()
            self.assertEqual(raw, b"public\nsource\n")
            manifest = json.loads((output / "public-source-manifest.json").read_text())
            self.assertEqual(manifest["files"]["README.md"], hashlib.sha256(raw).hexdigest())

    def test_credential_patterns_report_rule_names_only(self):
        cases = (
            ("-----BEGIN " + "PRIVATE KEY-----", "private-key"),
            ("ghp_" + "A" * 36, "github-token"),
            ("sk-" + "B" * 30, "api-key"),
            ("AKIA" + "C" * 16, "aws-access-key"),
            ("eyJ" + "D" * 15 + "." + "E" * 15 + "." + "F" * 15, "session-jwt"),
            ("100000001" + "@qq.com", "personal-qq-email"),
            ("C:/" + "Users/private-fixture/Desktop", "windows-user-path"),
            ("/vol9/" + "1234/private", "nas-volume-path"),
        )
        for text, rule in cases:
            with self.subTest(rule=rule):
                self.assertIn(rule, release.findings(text, set()))
                self.assertNotIn(text, release.findings(text, set()))

    def test_existing_and_source_output_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "README.md", "public")
            for output in (root, root / "src", root / "dist"):
                output.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):
                    release.export_source(root, output)

    def test_encrypted_credentials_are_checked_without_exporting_keys(self):
        from cryptography.fernet import Fernet

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "encrypted-private-fixture-credential"
            self.write(root, "README.md", secret)
            document = {"providers": [{"api_key": secret}], "persona": {"owner_qq_ids": ["100000007"]}}
            key = Fernet.generate_key()
            self.write(root, "data/private/runtime-config.key", key.decode())
            (root / "data/private/runtime-config.enc").write_bytes(Fernet(key).encrypt(json.dumps(document).encode()))
            with self.assertRaisesRegex(ValueError, "known-private-value"):
                release.export_source(root, root / "dist" / "public")
