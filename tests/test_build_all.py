import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_all  # noqa: E402
import fetch_srpms  # noqa: E402  (build_all puts ci/ on sys.path)


class ParsePackages(unittest.TestCase):
    def test_names_options_and_comments(self):
        text = "# header\nmesa SPEC_PATCH=ci/x.sed DEFINES=A=1\n\nlibheif WITH=x264  # why\nfaac\n"
        self.assertEqual(
            fetch_srpms.parse_packages(text),
            [("mesa", {"SPEC_PATCH": "ci/x.sed", "DEFINES": "A=1"}), ("libheif", {"WITH": "x264"}), ("faac", {})],
        )

    def test_quoted_option_value_keeps_its_spaces(self):
        self.assertEqual(fetch_srpms.parse_packages('ffmpeg-9 WITH="x264 x265" DEFINES=BUILD_ORIG=1\n'),
                         [("ffmpeg-9", {"WITH": "x264 x265", "DEFINES": "BUILD_ORIG=1"})])

    def test_option_without_equals_is_rejected(self):
        with self.assertRaises(ValueError):
            fetch_srpms.parse_packages("mesa NOCHECK\n")

    def test_primary_matches_names_case_insensitively(self):
        ns = "http://linux.duke.edu/metadata/common"
        xml = (f'<metadata xmlns="{ns}">'
               '<package><name>Mesa</name><arch>src</arch><location href="m/Mesa-1.src.rpm"/></package>'
               '<package><name>Mesa-drivers</name><arch>src</arch><location href="m/Mesa-drivers-1.src.rpm"/></package>'
               '<package><name>Mesa</name><arch>x86_64</arch><location href="m/Mesa.rpm"/></package>'
               '</metadata>').encode()
        found = fetch_srpms.parse_primary(xml, {"mesa", "mesa-drivers"})
        self.assertEqual(found, {"mesa": "m/Mesa-1.src.rpm", "mesa-drivers": "m/Mesa-drivers-1.src.rpm"})


class ChoosingSrpms(unittest.TestCase):
    def test_name_does_not_match_longer_package_name(self):
        self.assertTrue(build_all.matches_name("Mesa-26.2.5-1.1.src.rpm", "mesa"))
        self.assertFalse(build_all.matches_name("Mesa-drivers-26.2.5-1.1.src.rpm", "mesa"))
        self.assertTrue(build_all.matches_name("Mesa-drivers-26.2.5-1.1.src.rpm", "mesa-drivers"))

    def test_newest_version_wins_numerically(self):
        files = ["d/x265-4.1-1699.4.pm.10.src.rpm", "d/x265-4.1-1699.4.pm.27.src.rpm"]
        self.assertEqual(build_all.pick_srpm("x265", files), "d/x265-4.1-1699.4.pm.27.src.rpm")

    def test_none_when_no_file_matches(self):
        self.assertIsNone(build_all.pick_srpm("vlc", ["d/x265-4.1-1.src.rpm"]))

    def test_find_srpm_looks_only_in_srpms_and_takes_the_newest(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ("srpms/Mesa-26.2.4-1.1.src.rpm", "srpms/Mesa-26.2.5-1.1.src.rpm",
                        "srpms/old/Mesa-99-1.src.rpm", "vendor/srpms/Mesa-98-1.src.rpm"):
                os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
                open(os.path.join(root, rel), "w").close()
            found = build_all.find_srpm("mesa", root)
            self.assertEqual(os.path.relpath(found, root), "srpms/Mesa-26.2.5-1.1.src.rpm")

    def test_find_srpm_is_none_without_an_srpms_directory(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(build_all.find_srpm("mesa", root))


def asset(name):
    return {"name": name, "url": f"https://api.github.com/assets/{name}"}


class ReleaseSrpms(unittest.TestCase):
    def test_newest_asset_wins_across_releases(self):
        assets = [asset("faac-1.50-1699.3.pm.6.src.rpm"), asset("faac-1.50-1699.3.pm.11.src.rpm"),
                  asset("faac-devel-9-1.src.rpm"), asset("x265-4.1-1.src.rpm")]
        picked = fetch_srpms.pick_assets({"faac", "vlc"}, assets)
        self.assertEqual(list(picked), ["faac"])
        self.assertEqual(picked["faac"]["name"], "faac-1.50-1699.3.pm.11.src.rpm")

    def test_assets_come_from_every_release_and_only_srpms(self):
        releases = [{"assets": [{"name": "a-1.src.rpm"}, {"name": "notes.txt"}]}, {"assets": [{"name": "b-2.src.rpm"}]}]
        with mock.patch.object(fetch_srpms, "fetch", return_value=json.dumps(releases).encode()):
            names = [a["name"] for a in fetch_srpms.release_assets("o/r")]
        self.assertEqual(names, ["a-1.src.rpm", "b-2.src.rpm"])

    def test_authorization_header_only_when_token_is_set(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}):
            self.assertEqual(fetch_srpms.release_headers()["Authorization"], "Bearer tok")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Authorization", fetch_srpms.release_headers())

    def test_download_skips_files_already_present_and_reports_missing(self):
        assets = [asset("faac-1.50-1.src.rpm"), asset("libde265-1.1-1.src.rpm")]
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(fetch_srpms, "release_assets", return_value=assets), \
                mock.patch.object(fetch_srpms, "fetch", return_value=b"rpm") as fetch:
            open(os.path.join(out, "faac-1.50-1.src.rpm"), "w").close()
            done, missing = fetch_srpms.fetch_release_srpms({"faac", "libde265", "vvenc"}, out, "o/r")
            self.assertEqual(done, ["libde265-1.1-1.src.rpm"])
            self.assertEqual(missing, ["vvenc"])
            self.assertEqual(open(os.path.join(out, "libde265-1.1-1.src.rpm"), "rb").read(), b"rpm")
            fetch.assert_called_once()

    def test_fetch_all_errors_when_a_name_is_nowhere(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(build_all, "ROOT", root), \
                mock.patch.object(fetch_srpms, "fetch_srpms", return_value=([], ["vvenc"])), \
                mock.patch.object(fetch_srpms, "fetch_release_srpms", return_value=([], ["vvenc"])), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(build_all.BuildError) as ctx:
                build_all.fetch_all([("vvenc", {})])
        self.assertIn("vvenc", str(ctx.exception))
        self.assertIn("release", str(ctx.exception))

    def test_fetch_all_tolerates_an_unreachable_api_when_the_file_is_cached(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "srpms"))
            open(os.path.join(root, "srpms", "vvenc-1.14-1.src.rpm"), "w").close()
            with mock.patch.object(build_all, "ROOT", root), \
                    mock.patch.object(fetch_srpms, "fetch_srpms", return_value=([], ["vvenc"])), \
                    mock.patch.object(fetch_srpms, "fetch_release_srpms", side_effect=OSError("offline")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                build_all.fetch_all([("vvenc", {})])


class BuildOrder(unittest.TestCase):
    def test_capability_strips_version_and_skips_rich_deps(self):
        self.assertEqual(build_all.capability("pkgconfig(x264) >= 1.2"), "pkgconfig(x264)")
        self.assertIsNone(build_all.capability("(a or b)"))

    def test_provider_builds_before_consumer(self):
        edges = build_all.build_edges(
            {"libheif": ["pkgconfig(x264)", "cmake"], "libx264": []},
            {"libheif": {"libheif1"}, "libx264": {"pkgconfig(x264)"}},
        )
        self.assertEqual(edges["libheif"], {"libx264": "pkgconfig(x264)"})
        self.assertEqual(build_all.order_packages(["libheif", "libx264"], edges), ["libx264", "libheif"])

    def test_ties_keep_file_order(self):
        self.assertEqual(build_all.order_packages(["b", "a", "c"], {"a": {}, "b": {}, "c": {}}), ["b", "a", "c"])

    def test_wildcard_pc_package_matched_by_name_prefix(self):
        edges = build_all.build_edges(
            {"libheif": ["pkgconfig(SvtAv1Enc)"], "SVT-AV1": []},
            {"libheif": set(), "SVT-AV1": {build_all.PC_WILDCARD}},
        )
        self.assertEqual(edges["libheif"], {"SVT-AV1": "pkgconfig(SvtAv1Enc)"})

    def test_cycle_is_reported_with_the_buildrequires(self):
        edges = {"a": {"b": "cap-b"}, "b": {"a": "cap-a"}}
        with self.assertRaises(build_all.BuildError) as ctx:
            build_all.order_packages(["a", "b"], edges)
        self.assertIn("a needs b because of BuildRequires cap-b", str(ctx.exception))

    def test_pkgconfig_caps_reads_files_and_flags_globs(self):
        spec = "%files devel\n/usr/lib64/pkgconfig/x264.pc\n/usr/lib64/pkgconfig/*.pc\n"
        self.assertEqual(build_all.pkgconfig_caps(spec), {"pkgconfig(x264)", build_all.PC_WILDCARD})


class Stages(unittest.TestCase):
    def test_satisfied_capability_drops_the_edge(self):
        reqs = {"libheif": ["pkgconfig(x264)", "pkgconfig(de265)"], "libx264": [], "libde265": []}
        provs = {"libheif": set(), "libx264": {"pkgconfig(x264)"}, "libde265": {"pkgconfig(de265)"}}
        edges = build_all.build_edges(reqs, provs, satisfied=lambda cap: cap == "pkgconfig(x264)")
        self.assertEqual(edges["libheif"], {"libde265": "pkgconfig(de265)"})

    def test_edge_matches_lists_every_capability(self):
        reqs = {"a": ["cap1", "cap2"], "b": []}
        provs = {"a": set(), "b": {"cap1", "cap2"}}
        self.assertEqual(sorted(build_all.edge_matches(reqs, provs)), [("a", "b", "cap1"), ("a", "b", "cap2")])

    def test_split_stages_puts_dependents_in_stage2(self):
        edges = {"x": {}, "y": {}, "z": {"x": "cap"}}
        order = build_all.order_packages(["z", "x", "y"], edges)
        self.assertEqual(build_all.split_stages(order, edges), (["x", "y"], ["z"]))

    def test_split_stages_without_edges_has_empty_stage2(self):
        self.assertEqual(build_all.split_stages(["a", "b"], {"a": {}, "b": {}}), (["a", "b"], []))

    def test_chain_deeper_than_two_stages_is_an_error(self):
        edges = {"a": {}, "b": {"a": "c1"}, "c": {"b": "c2"}}
        with self.assertRaises(build_all.BuildError) as ctx:
            build_all.split_stages(["a", "b", "c"], edges)
        self.assertIn("deeper than two stages", str(ctx.exception))

    def test_matrix_prints_only_the_json_on_stdout(self):
        edges = {"faac": {}, "bad": {"faac": "faac-devel"}}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(build_all, "load_packages", return_value=[("bad", {}), ("faac", {})]), \
                mock.patch.object(build_all, "make_official", return_value=None), \
                mock.patch.object(build_all, "fetch_all", side_effect=lambda p: print("fetched noise")), \
                mock.patch.object(build_all, "resolve", return_value=(["faac", "bad"], edges, {})), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_all.main(["--matrix"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"stage1": ["faac"], "stage2": ["bad"]})
        self.assertIn("fetched noise", err.getvalue())

    def test_image_build_keeps_podman_stdout_off_our_stdout(self):
        with mock.patch.object(build_all.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            build_all.build_image("podman")
        self.assertIs(run.call_args.kwargs["stdout"], sys.stderr)

    def test_matrix_builds_the_image_inside_the_redirect(self):
        out, err = io.StringIO(), io.StringIO()
        def fake_make_official(require_image):
            print("== building image")  # what build_image prints
            return None
        with mock.patch.object(build_all, "load_packages", return_value=[("a", {}), ("b", {})]), \
                mock.patch.object(build_all, "make_official", side_effect=fake_make_official), \
                mock.patch.object(build_all, "fetch_all"), \
                mock.patch.object(build_all, "resolve", return_value=(["a", "b"], {"a": {}, "b": {}}, {})), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            build_all.main(["--matrix"])
        self.assertEqual(json.loads(out.getvalue()), {"stage1": ["a", "b"], "stage2": []})

    def test_only_selects_one_package_case_insensitively(self):
        seen = {}
        def fake_build_all(order, envs, srpms, deps=None):
            seen.update(order=order, deps=deps)
        with mock.patch.object(build_all, "load_packages", return_value=[("Mesa", {}), ("faac", {})]), \
                mock.patch.object(build_all, "fetch_all"), \
                mock.patch.object(build_all, "resolve", return_value=(["Mesa"], {"Mesa": {}}, {"Mesa": "x"})), \
                mock.patch.object(build_all, "build_all", side_effect=fake_build_all), \
                mock.patch.object(build_all, "stage_site") as stage, \
                contextlib.redirect_stdout(io.StringIO()):
            code = build_all.main(["--only", "mesa", "--deps", "d"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"order": ["Mesa"], "deps": "d"})
        stage.assert_not_called()

    def test_only_unknown_package_is_an_error(self):
        with mock.patch.object(build_all, "load_packages", return_value=[("faac", {})]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_all.main(["--only", "nope"]), 1)

    def test_container_command_mounts_deps_read_only(self):
        cmd = build_all.container_cmd("docker", "bad", os.path.join(build_all.ROOT, "srpms/bad-1.src.rpm"), {}, "deps")
        self.assertIn(f"{os.path.abspath('deps')}:/deps:ro,Z", cmd)
        self.assertIn("DEPS_REPO=/deps", cmd)
        self.assertNotIn("DEPS_REPO=/deps", build_all.container_cmd("docker", "x", build_all.ROOT + "/s.src.rpm", {}))


class PublicKey(unittest.TestCase):
    PUB = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n"

    def test_signing_key_wins_over_the_committed_file(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "RPM-GPG-KEY"), "wb").write(self.PUB.replace(b"abc", b"old"))
            self.assertEqual(build_all.public_key_for_site(self.PUB, root), self.PUB)

    def test_committed_file_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "RPM-GPG-KEY"), "wb").write(self.PUB)
            self.assertEqual(build_all.public_key_for_site(None, root), self.PUB)

    def test_no_key_at_all_is_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(build_all.public_key_for_site(None, root))

    def test_private_key_block_is_refused(self):
        private = b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nsecret\n-----END PGP PRIVATE KEY BLOCK-----\n"
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "RPM-GPG-KEY"), "wb").write(private)
            with self.assertRaises(build_all.BuildError) as ctx:
                build_all.public_key_for_site(None, root)
        self.assertIn("RPM-GPG-KEY", str(ctx.exception))

    def test_a_public_block_that_also_holds_a_private_one_is_refused(self):
        with self.assertRaises(build_all.BuildError):
            build_all.check_public_key(self.PUB + b"-----BEGIN PGP PRIVATE KEY BLOCK-----\n", "x")

    def test_something_that_is_not_a_key_is_refused(self):
        with self.assertRaises(build_all.BuildError):
            build_all.check_public_key(b"hello", "x")


class Building(unittest.TestCase):
    def test_container_command_maps_spec_patch_into_the_container(self):
        cmd = build_all.container_cmd("podman", "mesa", os.path.join(build_all.ROOT, "srpms/Mesa-1.src.rpm"),
                                      {"SPEC_PATCH": "ci/mesa-radeon.sed", "WITH": "x264"})
        self.assertIn("SPEC_PATCH=/in/ci/mesa-radeon.sed", cmd)
        self.assertIn("WITH=x264", cmd)
        self.assertEqual(cmd[-2:], [build_all.IMAGE, "/in/srpms/Mesa-1.src.rpm"])

    def test_debug_rpms_are_recognised(self):
        self.assertTrue(build_all.is_debug_rpm("libheif1-debuginfo-1.23.6-1.1.x86_64.rpm"))
        self.assertTrue(build_all.is_debug_rpm("libheif-debugsource-1.23.6-1.1.x86_64.rpm"))
        self.assertFalse(build_all.is_debug_rpm("libheif1-1.23.6-1.1.x86_64.rpm"))

    def test_unknown_option_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("mesa FOO=1\n")
        try:
            with self.assertRaises(build_all.BuildError):
                build_all.load_packages(f.name)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
