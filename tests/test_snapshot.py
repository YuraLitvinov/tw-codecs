import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ci"))
import snapshot  # noqa: E402

MEDIA = "openSUSE - openSUSE-20261024-x86_64-Build6.174\nopenSUSE-20261024-x86_64-Build6.174\n1\n"
ID = "openSUSE-20261024-x86_64-Build6.174"


def fake_fetch(text):
    return lambda url: text


class ParseMedia(unittest.TestCase):
    def test_second_line_is_the_id(self):
        self.assertEqual(snapshot.parse_media(MEDIA), ID)

    def test_html_error_page_is_rejected(self):
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.parse_media("<html>\nNot found\n</html>")

    def test_empty_text_is_rejected(self):
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.parse_media("")


class State(unittest.TestCase):
    def test_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(snapshot.read_state(os.path.join(d, "snapshot.txt")), "")

    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snapshot.txt")
            snapshot.write_state(path, ID)
            self.assertEqual(open(path).read(), ID + "\n")
            self.assertEqual(snapshot.read_state(path), ID)

    def test_write_refuses_something_that_is_not_an_id(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.write_state(os.path.join(d, "snapshot.txt"), "garbage")
            self.assertFalse(os.path.exists(os.path.join(d, "snapshot.txt")))


class Check(unittest.TestCase):
    def test_same_id_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snapshot.txt")
            snapshot.write_state(path, ID)
            self.assertEqual(snapshot.check(path, fake_fetch(MEDIA)), {"snapshot": ID, "changed": "false"})

    def test_new_id_is_changed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snapshot.txt")
            snapshot.write_state(path, "openSUSE-20261020-x86_64-Build1.1")
            self.assertEqual(snapshot.check(path, fake_fetch(MEDIA))["changed"], "true")

    def test_missing_file_is_changed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(snapshot.check(os.path.join(d, "snapshot.txt"), fake_fetch(MEDIA))["changed"], "true")


class Cli(unittest.TestCase):
    def run_cli(self, argv, media=MEDIA):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(snapshot, "fetch_text", fake_fetch(media)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = snapshot.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_prints_github_output_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snapshot.txt")
            code, out, _ = self.run_cli(["check", path])
            self.assertEqual((code, out), (0, f"snapshot={ID}\nchanged=true\n"))
            self.assertEqual(self.run_cli(["write", path, ID])[0], 0)
            code, out, _ = self.run_cli(["check", path])
            self.assertEqual(out, f"snapshot={ID}\nchanged=false\n")

    def test_bad_arguments_print_usage_and_exit_2(self):
        code, _, err = self.run_cli(["check"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_unparsable_media_is_an_error_not_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.run_cli(["check", os.path.join(d, "snapshot.txt")], media="<html>oops</html>")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("error:", err)


class Sources(unittest.TestCase):
    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "built-sources.txt")
            self.assertEqual(snapshot.read_sources(path), {})
            data = {"mesa": ("Mesa-26.2.5-1.1.src.rpm", "aaaa"), "faac": ("faac-1.50-1.src.rpm", "bbbb")}
            snapshot.write_sources(path, data)
            self.assertEqual(snapshot.read_sources(path), data)

    def test_bad_line_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "built-sources.txt")
            open(path, "w").write("mesa\tonly-two-fields\n")
            with self.assertRaises(snapshot.SnapshotError):
                snapshot.read_sources(path)

    def test_config_hash_changes_with_options_and_files_not_with_order(self):
        base = snapshot.config_hash("Mesa", {"WITH": "x", "DEFINES": "A=1"}, {"Dockerfile": "d", "sed": "s"})
        self.assertEqual(base, snapshot.config_hash("mesa", {"DEFINES": "A=1", "WITH": "x"}, {"sed": "s", "Dockerfile": "d"}))
        self.assertNotEqual(base, snapshot.config_hash("mesa", {"WITH": "y", "DEFINES": "A=1"}, {"Dockerfile": "d", "sed": "s"}))
        self.assertNotEqual(base, snapshot.config_hash("mesa", {"WITH": "x", "DEFINES": "A=1"}, {"Dockerfile": "d2", "sed": "s"}))


class Dirty(unittest.TestCase):
    CURRENT = {"a": ("a-1.src.rpm", "h1"), "b": ("b-1.src.rpm", "h2"), "c": ("c-1.src.rpm", "h3")}

    def test_nothing_changed_means_nothing_dirty(self):
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, dict(self.CURRENT), {"c": {"a": "cap"}}), set())

    def test_new_source_rpm_marks_it_dirty(self):
        built = dict(self.CURRENT, a=("a-0.src.rpm", "h1"))
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, built, {}), {"a"})

    def test_new_config_hash_marks_it_dirty(self):
        built = dict(self.CURRENT, b=("b-1.src.rpm", "old"))
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, built, {}), {"b"})

    def test_package_never_built_is_dirty(self):
        built = {k: v for k, v in self.CURRENT.items() if k != "c"}
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, built, {}), {"c"})

    def test_empty_state_makes_everything_dirty(self):
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, {}, {}), {"a", "b", "c"})

    def test_dependents_of_a_dirty_package_are_dirty_transitively(self):
        built = dict(self.CURRENT, a=("a-0.src.rpm", "h1"))
        edges = {"b": {"a": "cap"}, "c": {"b": "cap"}}
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, built, edges), {"a", "b", "c"})

    def test_a_dirty_dependent_does_not_dirty_what_it_needs(self):
        built = dict(self.CURRENT, c=("c-0.src.rpm", "h3"))
        self.assertEqual(snapshot.dirty_packages(self.CURRENT, built, {"c": {"a": "cap"}}), {"c"})


if __name__ == "__main__":
    unittest.main()
