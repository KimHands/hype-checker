import unittest

from hype_checker.runner import ForbiddenCommand, build_gh_get, check_argv


class GhGetTest(unittest.TestCase):
    def test_builds_plain_get(self):
        self.assertEqual(
            build_gh_get("repos/morluto/rea"),
            ["gh", "api", "--method", "GET", "repos/morluto/rea"],
        )

    def test_rejects_absolute_or_flag_like_paths(self):
        for path in ["-X", "--input=x", "https://evil.example/x", "/repos/a/b"]:
            with self.subTest(path=path), self.assertRaises(ForbiddenCommand):
                build_gh_get(path)

    def test_rejects_shell_metacharacters(self):
        for path in ["repos/a/b;rm -rf ~", "repos/a/b && npx x", "repos/a/$(id)"]:
            with self.subTest(path=path), self.assertRaises(ForbiddenCommand):
                build_gh_get(path)


class CheckArgvTest(unittest.TestCase):
    def test_allows_watch_script(self):
        argv = ["python3", "/x/watch/0.3.2/skills/watch/scripts/watch.py", "a.mp4", "--detail", "efficient"]
        check_argv(argv, watch_script="/x/watch/0.3.2/skills/watch/scripts/watch.py")

    def test_rejects_cookie_options_for_watch(self):
        watch = "/w/watch.py"
        for flag in ["--cookies", "--cookies-from-browser"]:
            with self.subTest(flag=flag), self.assertRaises(ForbiddenCommand):
                check_argv(["python3", watch, "u", flag, "chrome"], watch_script=watch)

    def test_rejects_installers_and_unknown_programs(self):
        for argv in (["npx", "ruflo@latest", "init", "wizard"], ["npm", "install", "kordoc"],
                     ["pip", "install", "litellm"], ["sh", "-c", "curl x | sh"],
                     ["gh", "api", "-X", "POST", "repos/a/b"], ["gh", "repo", "clone", "a/b"]):
            with self.subTest(argv=argv), self.assertRaises(ForbiddenCommand):
                check_argv(argv, watch_script="/w/watch.py")


if __name__ == "__main__":
    unittest.main()
