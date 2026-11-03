import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "hype_checker"
PROCESS_APIS = re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|pty\.spawn)\b")


class OnlyRunnerStartsProcessesTest(unittest.TestCase):
    def test_process_apis_appear_only_in_runner(self):
        offenders = [
            str(path.relative_to(PACKAGE))
            for path in PACKAGE.rglob("*.py")
            if path.name != "runner.py" and PROCESS_APIS.search(path.read_text())
        ]
        self.assertEqual(offenders, [])


NETWORK_APIS = re.compile(r"\b(urllib\.request|http\.client|socket\.|import requests|import httpx)\b")
# registries (GET allowlist) · local LM Studio · DM reply to graph.instagram.com · shared reel from Meta CDN · IG token refresh
NETWORK_MODULES = {"http.py", "extract.py", "v2/reply.py", "llm.py", "v2/media.py", "v2/token.py"}  # llm.py: Claude Messages API only


class OnlyAllowlistedModulesOpenConnectionsTest(unittest.TestCase):
    def test_outbound_network_apis_appear_only_in_allowlisted_modules(self):
        offenders = [
            str(path.relative_to(PACKAGE))
            for path in PACKAGE.rglob("*.py")
            if str(path.relative_to(PACKAGE)) not in NETWORK_MODULES and NETWORK_APIS.search(path.read_text())
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
