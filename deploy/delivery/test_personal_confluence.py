import json
import os
import tempfile
import unittest
from pathlib import Path

from enable_personal_confluence import KEY_PATH, SOCKET_DIR, provision


class PersonalSiteTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.site = Path(temporary.name)
        self.transport = self.site / "transport"
        self.transport.mkdir(mode=0o700)
        self.secure = self.site / "secure.json"

    def configure(self):
        provision(self.site, "https://wiki.example.test", self.transport, os.getgid())

    def test_merge_site_and_reuse_key_after_restart(self):
        original = {"services": {"web": {"read_only": True},
                                 "api": {"environment": ["OTHER=a=b", "INHERITED", "EMPTY="],
                                         "volumes": ["/srv/certs:/certs:ro"]}}}
        self.secure.write_text(json.dumps(original))
        self.configure()
        key = self.site / "confluence-personal.key"
        before = key.read_bytes()
        secure = self.secure.read_bytes()
        self.configure()
        self.assertEqual(key.read_bytes(), before)
        self.assertEqual(key.stat().st_mode & 0o777, 0o440)
        self.assertEqual(self.secure.read_bytes(), secure)
        self.assertEqual(json.loads((self.site / "secure.before-personal.json").read_bytes()), original)
        config = json.loads(secure)["services"]
        self.assertEqual(config["web"], original["services"]["web"])
        env = config["api"]["environment"]
        self.assertEqual((env["OTHER"], env["INHERITED"], env["EMPTY"]), ("a=b", None, ""))
        self.assertEqual(env["CONFLUENCE_PERSONAL_KEY_FILE"], KEY_PATH)
        volumes = config["api"]["volumes"]
        self.assertEqual(len(volumes), 3)
        self.assertEqual(volumes[0], "/srv/certs:/certs:ro")
        self.assertTrue(all(v["read_only"] for v in volumes[1:]))
        self.assertEqual(volumes[1]["target"], SOCKET_DIR)
        self.assertNotIn(before.strip(), secure)

    def test_missing_previously_mounted_key_is_not_replaced(self):
        self.secure.write_text('{"services": {"api": {}}}')
        self.configure()
        key = self.site / "confluence-personal.key"
        key.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.configure()
        self.assertFalse(key.exists())

    def test_conflicting_mount_preserves_override(self):
        original = json.dumps({"services": {"api": {"environment": {"EXTRA": "real"},
                                                     "volumes": [f"/other:{SOCKET_DIR}:ro"]}}})
        self.secure.write_text(original)
        with self.assertRaisesRegex(ValueError, "mount conflicts"):
            self.configure()
        self.assertEqual(self.secure.read_text(), original)
        self.assertFalse((self.site / "confluence-personal.key").exists())


if __name__ == "__main__":
    unittest.main()
