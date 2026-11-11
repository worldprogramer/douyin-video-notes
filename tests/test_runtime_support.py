"""Runtime selection tests; no downloads, subprocesses, or ASR imports are needed."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import runtime_support as runtime


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="douyin-runtime-test-")
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.project = self.root / ".douyin-notes-runtime"
        self.patches = [
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(runtime.Path, "cwd", return_value=self.root),
            mock.patch.object(runtime.shutil, "which", return_value=None),
            mock.patch.object(runtime, "_version", side_effect=lambda path: "verified-test-version" if path else None),
            mock.patch.object(runtime, "_gpu_info", return_value={"devices": [], "inference_tested": False}),
            mock.patch.object(runtime.importlib.metadata, "version", return_value="1.0"),
        ]
        self.mocks = [patch.start() for patch in self.patches]

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        # All fixtures are plain files under this verified temporary directory.
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture; never executed")
        return path.resolve()

    def model(self, path):
        for filename in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
            target = path / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture; never loaded")
        return path.resolve()

    def test_cli_overrides_environment_and_path(self):
        explicit = self.executable("explicit/ffmpeg.exe")
        environment = self.executable("environment/ffmpeg.exe")
        path_tool = self.executable("path/ffmpeg.exe")
        explicit_model = self.model(self.root / "chosen-model")
        os.environ.update(DOUYIN_NOTES_FFMPEG=str(environment), DOUYIN_NOTES_MODEL=str(self.root / "other-model"))
        self.mocks[2].side_effect = lambda name: str(path_tool) if name == "ffmpeg" else None
        report = runtime.discover_runtime(ffmpeg=explicit, model=explicit_model)
        self.assertEqual(report["ffmpeg"], str(explicit))
        self.assertEqual(report["model"], str(explicit_model))

    def test_environment_overrides_path(self):
        chosen = self.executable("configured/ffmpeg.exe")
        path_tool = self.executable("path/ffmpeg.exe")
        chosen_model = self.model(self.root / "environment-model")
        os.environ.update(DOUYIN_NOTES_FFMPEG=str(chosen), DOUYIN_NOTES_MODEL=str(chosen_model))
        self.mocks[2].side_effect = lambda name: str(path_tool) if name == "ffmpeg" else None
        report = runtime.discover_runtime()
        self.assertEqual(report["ffmpeg"], str(chosen))
        self.assertEqual(report["model"], str(chosen_model))

    def test_invalid_explicit_path_never_falls_back(self):
        fallback = self.executable("fallback/ffmpeg.exe")
        os.environ["DOUYIN_NOTES_FFMPEG"] = str(fallback)
        self.mocks[2].side_effect = lambda name: str(fallback) if name == "ffmpeg" else None
        report = runtime.discover_runtime(ffmpeg=self.root / "missing/ffmpeg.exe")
        self.assertIsNone(report["ffmpeg"])
        self.assertIn("ffmpeg", report["missing"])
        self.assertTrue(any("argument:" in message for message in report["configuration_errors"]))

    def test_invalid_environment_path_never_falls_back(self):
        fallback = self.executable("path/ffmpeg.exe")
        os.environ["DOUYIN_NOTES_FFMPEG"] = str(self.root / "missing/ffmpeg.exe")
        self.mocks[2].side_effect = lambda name: str(fallback) if name == "ffmpeg" else None
        report = runtime.discover_runtime()
        self.assertIsNone(report["ffmpeg"])
        self.assertTrue(any("DOUYIN_NOTES_FFMPEG:" in message for message in report["configuration_errors"]))

    def test_path_precedes_project_tools(self):
        self.executable(".douyin-notes-runtime/ffmpeg/build/bin/ffmpeg.exe")
        path_tool = self.executable("path/ffmpeg.exe")
        self.mocks[2].side_effect = lambda name: str(path_tool) if name == "ffmpeg" else None
        self.assertEqual(runtime.discover_runtime()["ffmpeg"], str(path_tool))

    def test_nested_project_build_and_default_model_are_discovered(self):
        ffmpeg = self.executable(".douyin-notes-runtime/ffmpeg/build/bin/ffmpeg.exe")
        ffprobe = self.executable(".douyin-notes-runtime/ffmpeg/build/bin/ffprobe.exe")
        model = self.model(self.project / "models/whisper-small")
        report = runtime.discover_runtime()
        self.assertEqual(report["ffmpeg"], str(ffmpeg))
        self.assertEqual(report["ffprobe"], str(ffprobe))
        self.assertEqual(report["model"], str(model))
        self.assertEqual(report["recommended_python"], sys.executable)
        self.assertTrue(report["ready"])
        self.assertFalse(report["model_inference_tested"])

    def test_runtime_environment_and_argument_select_project_root(self):
        environment_root = self.root / "environment-runtime"
        os.environ["DOUYIN_NOTES_RUNTIME"] = str(environment_root)
        self.assertEqual(runtime.discover_runtime()["model"], str(environment_root / "models/whisper-small"))
        explicit_root = self.root / "explicit-runtime"
        report = runtime.discover_runtime(runtime_dir=explicit_root)
        self.assertEqual(report["runtime_dir"], str(explicit_root))
        self.assertEqual(report["model"], str(explicit_root / "models/whisper-small"))

    def test_multiple_project_builds_require_explicit_selection(self):
        first = self.executable(".douyin-notes-runtime/ffmpeg/first/bin/ffmpeg.exe")
        self.executable(".douyin-notes-runtime/ffmpeg/second/bin/ffmpeg.exe")
        report = runtime.discover_runtime()
        self.assertIsNone(report["ffmpeg"])
        self.assertTrue(any("Multiple project ffmpeg" in message for message in report["configuration_errors"]))
        self.assertEqual(runtime.discover_runtime(ffmpeg=first)["ffmpeg"], str(first))

    def test_missing_explicit_model_is_not_replaced_by_environment_model(self):
        os.environ["DOUYIN_NOTES_MODEL"] = str(self.model(self.root / "valid-model"))
        missing = self.root / "missing-model"
        report = runtime.discover_runtime(model=missing)
        self.assertEqual(report["model"], str(missing))
        self.assertFalse(report["model_present"])
        self.assertIn("local_model", report["missing"])

    def test_empty_model_file_is_reported(self):
        model = self.model(self.project / "models/whisper-small")
        (model / "tokenizer.json").write_bytes(b"")
        report = runtime.discover_runtime()
        self.assertIn("tokenizer.json", report["missing_model_files"])
        self.assertFalse(report["ready"])


if __name__ == "__main__":
    unittest.main()
