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

    SOURCES = {"faac": ("faac-1.src.rpm", "h1"), "bad": ("bad-1.src.rpm", "h2")}

    def run_matrix(self, argv, edges, sources=None, state=None, order=("faac", "bad")):
        out, err = io.StringIO(), io.StringIO()
        sources = sources or self.SOURCES
        with mock.patch.object(build_all, "load_packages", return_value=[(n, {}) for n in order]), \
                mock.patch.object(build_all, "make_official", return_value=None), \
                mock.patch.object(build_all, "fetch_all", side_effect=lambda p: print("fetched noise")), \
                mock.patch.object(build_all, "resolve", return_value=(list(order), edges, {})), \
                mock.patch.object(build_all, "current_sources", return_value=sources), \
                mock.patch.object(build_all.snapshot, "read_sources", return_value=state or {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_all.main(["--matrix", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_matrix_prints_only_the_json_on_stdout(self):
        code, out, err = self.run_matrix([], {"faac": {}, "bad": {"faac": "faac-devel"}})
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["stage1"], [{"name": "faac", "carry": ""}])
        self.assertEqual(plan["stage2"], [{"name": "bad", "carry": ""}])
        self.assertEqual(plan["dirty"], ["faac", "bad"])
        self.assertIn("fetched noise", err)

    def test_matrix_with_matching_state_lists_nothing(self):
        code, out, _ = self.run_matrix([], {"faac": {}, "bad": {"faac": "c"}}, state=dict(self.SOURCES))
        self.assertEqual(json.loads(out), {"stage1": [], "stage2": [], "dirty": [], "carry": ["faac", "bad"]})

    def test_matrix_all_ignores_the_state(self):
        _, out, _ = self.run_matrix(["--all"], {"faac": {}, "bad": {}}, state=dict(self.SOURCES))
        self.assertEqual([e["name"] for e in json.loads(out)["stage1"]], ["faac", "bad"])

    def test_matrix_write_state_stores_the_current_sources(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "built-sources.txt")
            with mock.patch.object(build_all.snapshot, "write_sources") as write:
                self.run_matrix(["--write-state", path], {"faac": {}, "bad": {}})
            write.assert_called_once_with(path, self.SOURCES)

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
                mock.patch.object(build_all, "current_sources", return_value={"a": ("a", "h"), "b": ("b", "h")}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            build_all.main(["--matrix", "--state", "/nonexistent/built-sources.txt"])
        plan = json.loads(out.getvalue())
        self.assertEqual([e["name"] for e in plan["stage1"]], ["a", "b"])
        self.assertEqual(plan["stage2"], [])

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


PRIMARY = (
    '<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm">'
    '<package type="rpm"><name>libavcodec63</name><arch>x86_64</arch><location href="libavcodec63-9.0.2-4.2.x86_64.rpm"/>'
    '<format><rpm:sourcerpm>ffmpeg-9-9.0.2-4.2.src.rpm</rpm:sourcerpm></format></package>'
    '<package type="rpm"><name>libvvenc1_14</name><arch>x86_64</arch><location href="libvvenc1_14-1.14.0-1.x86_64.rpm"/>'
    '<format><rpm:sourcerpm>vvenc-1.14.0-1699.2.pm.2.src.rpm</rpm:sourcerpm></format></package>'
    '<package type="rpm"><name>Mesa-dri</name><arch>x86_64</arch><location href="Mesa-dri-26.2.5-1.1.x86_64.rpm"/>'
    '<format><rpm:sourcerpm>Mesa-drivers-26.2.5-1.1.src.rpm</rpm:sourcerpm></format></package>'
    '</metadata>'
).encode()


class PublishedSite(unittest.TestCase):
    def test_source_name_drops_version_and_release(self):
        self.assertEqual(fetch_srpms.source_name("ffmpeg-9-9.0.2-4.2.src.rpm"), "ffmpeg-9")
        self.assertEqual(fetch_srpms.source_name("libx264-20250608.b35605ac-1699.1.pm.18.src.rpm"), "libx264")

    def fake_fetch(self, url, headers=None):
        if url.endswith("repomd.xml"):
            return b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary"><location href="repodata/p.xml"/></data></repomd>'
        if url.endswith("repodata/p.xml"):
            return PRIMARY
        return b"rpm-bytes:" + url.rsplit("/", 1)[-1].encode()

    def test_published_rpms_map_each_rpm_to_its_package(self):
        with mock.patch.object(fetch_srpms, "fetch", side_effect=self.fake_fetch):
            rpms = fetch_srpms.published_rpms("https://x.github.io/repo-name/")
        self.assertEqual({r["file"]: r["source"] for r in rpms},
                         {"libavcodec63-9.0.2-4.2.x86_64.rpm": "ffmpeg-9",
                          "libvvenc1_14-1.14.0-1.x86_64.rpm": "vvenc",
                          "Mesa-dri-26.2.5-1.1.x86_64.rpm": "Mesa-drivers"})

    def test_carry_over_downloads_only_the_wanted_packages_case_insensitively(self):
        with tempfile.TemporaryDirectory() as dest, mock.patch.object(fetch_srpms, "fetch", side_effect=self.fake_fetch):
            done, missing = fetch_srpms.carry_over("https://x.github.io/r", {"mesa-drivers", "vvenc", "faac"}, dest)
            self.assertEqual(sorted(done), ["Mesa-dri-26.2.5-1.1.x86_64.rpm", "libvvenc1_14-1.14.0-1.x86_64.rpm"])
            self.assertEqual(missing, ["faac"])
            self.assertEqual(sorted(os.listdir(dest)), sorted(done))
            self.assertFalse(os.path.exists(os.path.join(dest, "libavcodec63-9.0.2-4.2.x86_64.rpm")))

    def test_carry_over_skips_files_already_there(self):
        with tempfile.TemporaryDirectory() as dest, mock.patch.object(fetch_srpms, "fetch", side_effect=self.fake_fetch):
            open(os.path.join(dest, "libvvenc1_14-1.14.0-1.x86_64.rpm"), "w").close()
            done, _ = fetch_srpms.carry_over("https://x.github.io/r", {"vvenc"}, dest)
            self.assertEqual(done, [])


class PlanMatrix(unittest.TestCase):
    CURRENT = {"vvenc": ("v-1", "h"), "ffmpeg": ("f-1", "h"), "faac": ("fa-1", "h"), "bad": ("b-1", "h")}
    EDGES = {"vvenc": {}, "faac": {}, "ffmpeg": {"vvenc": "vvenc-devel"}, "bad": {"faac": "faac-devel", "vvenc": "x"}}
    ORDER = ["vvenc", "faac", "ffmpeg", "bad"]

    def plan(self, built, everything=False):
        return build_all.plan_matrix(self.ORDER, self.EDGES, self.CURRENT, built, everything)

    def test_unchanged_builds_nothing_and_carries_everything(self):
        plan = self.plan(dict(self.CURRENT))
        self.assertEqual((plan["stage1"], plan["stage2"], plan["dirty"]), ([], [], []))
        self.assertEqual(plan["carry"], self.ORDER)

    def test_a_changed_dependency_rebuilds_its_dependents_in_stage2(self):
        built = dict(self.CURRENT, vvenc=("v-0", "h"))
        plan = self.plan(built)
        self.assertEqual([e["name"] for e in plan["stage1"]], ["vvenc"])
        self.assertEqual([e["name"] for e in plan["stage2"]], ["ffmpeg", "bad"])
        self.assertEqual(plan["carry"], ["faac"])
        # bad needs faac, which is not rebuilt, so its published RPMs are carried into the build
        self.assertEqual({e["name"]: e["carry"] for e in plan["stage2"]}, {"ffmpeg": "", "bad": "faac"})

    def test_a_changed_dependent_alone_waits_for_nothing_and_carries_its_dependency(self):
        built = dict(self.CURRENT, ffmpeg=("f-0", "h"))
        plan = self.plan(built)
        # nothing it needs is rebuilt in this run, so it is stage1; vvenc's published RPMs come along
        self.assertEqual(plan["stage1"], [{"name": "ffmpeg", "carry": "vvenc"}])
        self.assertEqual(plan["stage2"], [])
        self.assertEqual(plan["carry"], ["vvenc", "faac", "bad"])

    def test_everything_flag_rebuilds_all(self):
        plan = self.plan(dict(self.CURRENT), everything=True)
        self.assertEqual(plan["dirty"], self.ORDER)
        self.assertEqual(plan["carry"], [])

    def test_no_state_rebuilds_all(self):
        self.assertEqual(self.plan({})["dirty"], self.ORDER)

    def test_split_names_accepts_spaces_and_commas(self):
        self.assertEqual(build_all.split_names("a b,c  d"), ["a", "b", "c", "d"])
        self.assertEqual(build_all.split_names(None), [])
        self.assertEqual(build_all.split_names(""), [])

    def test_current_sources_prefers_oss_then_releases_and_hashes_the_config(self):
        packages = [("mesa", {"SPEC_PATCH": "ci/mesa-radeon.sed"}), ("vvenc", {})]
        with mock.patch.object(fetch_srpms, "oss_source_files", return_value={"mesa": "Mesa-1.src.rpm"}), \
                mock.patch.object(fetch_srpms, "release_source_files", return_value={"vvenc": "vvenc-1.src.rpm"}) as rel:
            sources = build_all.current_sources(packages)
        rel.assert_called_once_with({"vvenc"})
        self.assertEqual((sources["mesa"][0], sources["vvenc"][0]), ("Mesa-1.src.rpm", "vvenc-1.src.rpm"))
        self.assertNotEqual(sources["mesa"][1], sources["vvenc"][1])

    def test_current_sources_errors_when_a_name_is_nowhere(self):
        with mock.patch.object(fetch_srpms, "oss_source_files", return_value={}), \
                mock.patch.object(fetch_srpms, "release_source_files", return_value={}):
            with self.assertRaises(build_all.BuildError) as ctx:
                build_all.current_sources([("vvenc", {})])
        self.assertIn("vvenc", str(ctx.exception))


class CarryOver(unittest.TestCase):
    def test_stage_only_carries_into_out_all_before_staging(self):
        calls = []
        with mock.patch.object(build_all, "carry_rpms", side_effect=lambda *a: calls.append(("carry", a))), \
                mock.patch.object(build_all, "stage_site", side_effect=lambda: calls.append(("stage",))):
            code = build_all.main(["--stage-only", "--carry-over", "https://x/y", "--carry", "mesa faac"])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("carry", ("https://x/y", ["mesa", "faac"], os.path.join(build_all.ROOT, "out-all"))))
        self.assertEqual(calls[1], ("stage",))

    def test_only_carries_into_the_deps_directory(self):
        with mock.patch.object(build_all, "carry_rpms") as carry, \
                mock.patch.object(build_all, "load_packages", return_value=[("ffmpeg-9", {})]), \
                mock.patch.object(build_all, "fetch_all"), \
                mock.patch.object(build_all, "resolve", return_value=(["ffmpeg-9"], {"ffmpeg-9": {}}, {"ffmpeg-9": "x"})), \
                mock.patch.object(build_all, "build_all"), \
                contextlib.redirect_stdout(io.StringIO()):
            code = build_all.main(["--only", "ffmpeg-9", "--deps", "deps", "--carry-over", "https://x/y", "--carry", "vvenc"])
        self.assertEqual(code, 0)
        carry.assert_called_once_with("https://x/y", ["vvenc"], "deps")

    def test_carry_without_a_destination_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build_all.main(["--only", "x", "--carry-over", "https://x", "--carry", "a"]), 1)

    def test_no_carry_names_means_no_download(self):
        with mock.patch.object(build_all, "carry_rpms") as carry, mock.patch.object(build_all, "stage_site"):
            build_all.main(["--stage-only", "--carry-over", "https://x/y", "--carry", ""])
        carry.assert_not_called()

    def test_missing_published_rpms_is_an_error_naming_the_package(self):
        with mock.patch.object(build_all.fetch_srpms, "carry_over", return_value=([], ["faac"])):
            with self.assertRaises(build_all.BuildError) as ctx:
                build_all.carry_rpms("https://x", ["faac"], "d")
        self.assertIn("faac", str(ctx.exception))


class Options32(unittest.TestCase):
    def packages(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
        try:
            return build_all.load_packages(f.name)
        finally:
            os.unlink(f.name)

    def test_arch_and_src_are_accepted(self):
        pk = self.packages('vvenc ARCH="64 32"\nfdk-aac SRC=videolan ARCH=32\nfaac\n')
        self.assertEqual(pk[0][1], {"ARCH": "64 32"})
        self.assertEqual(pk[1][1], {"SRC": "videolan", "ARCH": "32"})

    def test_bad_arch_is_rejected(self):
        for bad in ("ARCH=16", 'ARCH="32 16"', 'ARCH=""'):
            with self.assertRaises(build_all.BuildError, msg=bad):
                self.packages(f"x {bad}\n")

    def test_bad_src_is_rejected(self):
        with self.assertRaises(build_all.BuildError):
            self.packages("x SRC=elsewhere\n")

    def test_baselibs_file_must_exist(self):
        with self.assertRaises(build_all.BuildError):
            self.packages("x BASELIBS=ci/baselibs/does-not-exist.conf\n")
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as conf:
            self.assertEqual(self.packages(f"x BASELIBS={conf.name}\n")[0][1], {"BASELIBS": conf.name})

    def test_container_command_maps_arch_and_hides_src(self):
        cmd = build_all.container_cmd("podman", "x", os.path.join(build_all.ROOT, "srpms/videolan/x-1.src.rpm"),
                                      {"ARCH": "64 32", "SRC": "videolan", "BASELIBS": "ci/baselibs/x264.conf"})
        self.assertIn("ARCHES=64 32", cmd)
        self.assertIn("BASELIBS=/in/ci/baselibs/x264.conf", cmd)
        self.assertFalse([c for c in cmd if c.startswith(("ARCH=", "SRC="))])
        self.assertEqual(cmd[-1], "/in/srpms/videolan/x-1.src.rpm")

    def test_find_srpm_keeps_videolan_downloads_apart(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ("srpms/fdk-aac-2.0.3-1699.2.pm.4.src.rpm", "srpms/videolan/fdk-aac-2.0.3.git.160-2.3.src.rpm"):
                os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
                open(os.path.join(root, rel), "w").close()
            self.assertEqual(os.path.basename(build_all.find_srpm("fdk-aac", root, src="videolan")),
                             "fdk-aac-2.0.3.git.160-2.3.src.rpm")
            self.assertEqual(os.path.basename(build_all.find_srpm("fdk-aac", root)),
                             "fdk-aac-2.0.3-1699.2.pm.4.src.rpm")

    def test_current_sources_reads_videolan_for_src_videolan_only(self):
        calls = []
        def fake_oss(names, base=None):
            calls.append((sorted(names), base))
            return {n: f"{n}-1.src.rpm" for n in names}
        with mock.patch.object(fetch_srpms, "oss_source_files", side_effect=fake_oss), \
                mock.patch.object(fetch_srpms, "release_source_files", return_value={}):
            sources = build_all.current_sources([("x264", {"SRC": "videolan", "ARCH": "32"}), ("mesa", {})])
        self.assertEqual(calls, [(["x264"], fetch_srpms.VIDEOLAN_BASE), (["mesa"], None)])
        self.assertEqual(sources["x264"][0], "x264-1.src.rpm")

    def test_arch_and_baselibs_change_the_config_hash(self):
        def h(env):
            with mock.patch.object(fetch_srpms, "oss_source_files", return_value={"p": "p-1.src.rpm"}), \
                    mock.patch.object(fetch_srpms, "release_source_files", return_value={}):
                return build_all.current_sources([("p", env)])["p"][1]
        base = h({})
        self.assertNotEqual(base, h({"ARCH": "32"}))
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.conf"), os.path.join(d, "b.conf")
            open(a, "w").write("libfoo1\n")
            open(b, "w").write("libbar2\n")
            self.assertNotEqual(h({"BASELIBS": a}), h({"BASELIBS": b}))

    def test_a_32bit_consumer_keeps_its_edge_to_a_32bit_provider_even_if_a_repo_has_the_64bit(self):
        reqs = {"heif": ["pkgconfig(x264)"], "x264": []}
        provs = {"heif": set(), "x264": {"pkgconfig(x264)"}}
        packages = [("heif", {"ARCH": "64 32"}), ("x264", {"ARCH": "32"})]
        with mock.patch.object(build_all, "find_srpm", return_value="s"), \
                mock.patch.object(build_all, "spec_info",
                                  side_effect=[(reqs["heif"], provs["heif"]), (reqs["x264"], provs["x264"])]):
            order, edges, _ = build_all.resolve(packages, official=lambda caps: set(caps))
        self.assertEqual(edges["heif"], {"x264": "pkgconfig(x264)"})
        self.assertEqual(order, ["x264", "heif"])

    def test_a_64bit_only_consumer_still_drops_the_edge(self):
        reqs = {"ff": ["pkgconfig(x264)"], "x264": []}
        provs = {"ff": set(), "x264": {"pkgconfig(x264)"}}
        packages = [("ff", {}), ("x264", {"ARCH": "32"})]
        with mock.patch.object(build_all, "find_srpm", return_value="s"), \
                mock.patch.object(build_all, "spec_info",
                                  side_effect=[(reqs["ff"], provs["ff"]), (reqs["x264"], provs["x264"])]):
            _, edges, _ = build_all.resolve(packages, official=lambda caps: set(caps))
        self.assertEqual(edges["ff"], {})


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
