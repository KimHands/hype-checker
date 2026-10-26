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


if __name__ == "__main__":
    unittest.main()
