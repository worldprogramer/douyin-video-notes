"""Offline downloader regression tests; every network operation is mocked."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import urllib.parse


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "douyin_download.py"
SPEC = importlib.util.spec_from_file_location("douyin_download_under_test", SCRIPT)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)

VIDEO_ID = "1234567890123456789"
SOURCE = f"https://www.douyin.com/video/{VIDEO_ID}"
MEDIA = "https://v-test.douyinvod.com/video?signature=TEST_ONLY_VIDEO"
AUDIO = "https://v-test.douyinvod.com/audio?signature=TEST_ONLY_AUDIO"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypisom" + b"synthetic-video" * 100


class Response(io.BytesIO):
    def __init__(self, body=b"", url=SOURCE, declared_length=None):
        super().__init__(body)
        self.url = url
        self.headers = {"Content-Length": str(len(body) if declared_length is None else declared_length)}

    def geturl(self):
        return self.url


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job = Path(self.temporary.name)
        network_guard = mock.patch("urllib.request.OpenerDirector.open",
                                   side_effect=AssertionError("Tests must not make network requests"))
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def test_share_text_and_source_id_forms(self):
        self.assertEqual(download.extract_input("分享文字 https://v.douyin.com/test-share/ 复制此链接"),
                         "https://v.douyin.com/test-share/")
        for url in (SOURCE, f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/",
                    f"https://www.douyin.com/user/example?modal_id={VIDEO_ID}"):
            with self.subTest(url=url):
                self.assertEqual(download.extract_id(url), VIDEO_ID)

    def test_untrusted_hosts_and_credentials_rejected(self):
        for url in ("https://douyin.com.example.org/video/1", "http://localhost/video",
                    "https://user:password@douyin.com/video/1", "https://douyin.com:invalid/video/1",
                    "file:///tmp/video.mp4", "blob:https://www.douyin.com/example"):
            with self.subTest(url=url):
                self.assertFalse(download._allowed(url, download.SOURCE_DOMAINS))

    def test_embedded_json_handles_nested_braces_in_strings(self):
        item = {"aweme_id": VIDEO_ID, "desc": 'quoted " } text', "video": {"play_addr": {"url_list": [MEDIA]}}}
        state = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}
        page = "<script>window._ROUTER_DATA = " + json.dumps(state) + ";</script>"
        self.assertEqual(download.find_video(download.parse_page(page), VIDEO_ID), item)
        self.assertIsNone(download.find_video(download.parse_page(page), "9876543210987654321"))

    def test_encoded_render_data(self):
        item = {"aweme_id": VIDEO_ID, "video": {}}
        encoded = urllib.parse.quote(json.dumps({"item": item}))
        self.assertEqual(download.find_video(download.parse_page(f'<script id="RENDER_DATA">{encoded}</script>'), VIDEO_ID), item)

    def test_provenance_removes_tracking_and_transport_parameters(self):
        self.assertEqual(download._public_source(SOURCE + "?share_sign=TEST_ONLY#fragment"), SOURCE)
        self.assertEqual(download._public_source("https://v.douyin.com/test-share/?token=TEST_ONLY"),
                         "https://v.douyin.com/test-share/")
        error = urllib.error.HTTPError(MEDIA, 403, "Forbidden", {}, None)
        self.assertNotIn("TEST_ONLY", download._error_message(error))

    def test_download_publishes_complete_media_atomically(self):
        dest = self.job / "video.mp4"
        with mock.patch.object(download, "_open", return_value=Response(VIDEO_BYTES)):
            self.assertEqual(download._download(MEDIA, dest), len(VIDEO_BYTES))
        self.assertEqual(dest.read_bytes(), VIDEO_BYTES)
        self.assertFalse(dest.with_suffix(".mp4.part").exists())

    def test_incomplete_download_preserves_existing_file(self):
        dest = self.job / "video.mp4"
        dest.write_bytes(b"previous file")
        with mock.patch.object(download, "_open", return_value=Response(VIDEO_BYTES, declared_length=len(VIDEO_BYTES) + 1)):
            with self.assertRaises(download.DownloadError):
                download._download(MEDIA, dest)
        self.assertEqual(dest.read_bytes(), b"previous file")
        self.assertFalse(dest.with_suffix(".mp4.part").exists())

    def test_html_download_is_not_published_as_video(self):
        dest = self.job / "video.mp4"
        with mock.patch.object(download, "_open", return_value=Response(b"<html>" * 500)):
            with self.assertRaises(download.DownloadError):
                download._download(MEDIA, dest)
        self.assertFalse(dest.exists())

    def fake_ytdlp(self, actual_id=VIDEO_ID):
        test = self

        class Client:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def extract_info(self, url, download=True):
                self.path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                test.assertFalse(self.path.exists(), "Each attempt must use fresh staging")
                self.path.write_bytes(VIDEO_BYTES)
                return {"id": actual_id, "title": "Synthetic video", "uploader": "Test author",
                        "webpage_url": SOURCE, "duration": 10}

            def prepare_filename(self, info):
                return str(self.path)

        return types.SimpleNamespace(YoutubeDL=Client)

    def test_ytdlp_does_not_reuse_an_orphan_video(self):
        source_dir = self.job / "source"
        source_dir.mkdir()
        orphan = source_dir / "video.mp4"
        orphan.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"old-video" * 200)
        with mock.patch.dict(sys.modules, {"yt_dlp": self.fake_ytdlp()}):
            result = download._fetch_ytdlp(SOURCE, self.job)
        self.assertEqual(Path(result["path"]).read_bytes(), VIDEO_BYTES)
        self.assertFalse(list(source_dir.glob(".download-*")))

    def test_ytdlp_wrong_id_does_not_replace_existing_file(self):
        source_dir = self.job / "source"
        source_dir.mkdir()
        existing = source_dir / "video.mp4"
        existing.write_bytes(b"previous file")
        with mock.patch.dict(sys.modules, {"yt_dlp": self.fake_ytdlp("9876543210987654321")}):
            with self.assertRaises(download.DownloadError):
                download._fetch_ytdlp(SOURCE, self.job)
        self.assertEqual(existing.read_bytes(), b"previous file")
        self.assertFalse(list(source_dir.glob(".download-*")))

    def test_browser_import_checks_source_id_before_downloading(self):
        with mock.patch.object(download, "_download") as fetch:
            with self.assertRaises(download.DownloadError):
                download.import_browser_media(self.job, MEDIA, AUDIO,
                    {"source_url": SOURCE, "id": "9876543210987654321"}, "ffmpeg")
        fetch.assert_not_called()

    @staticmethod
    def fake_download(url, destination):
        destination.write_bytes(VIDEO_BYTES)
        return len(VIDEO_BYTES)

    def test_browser_import_requires_audio_for_a_video_only_stream(self):
        with mock.patch.object(download, "_download", side_effect=self.fake_download), \
             mock.patch.object(download, "_probe_local_media", return_value={"video": True, "audio": False, "duration": 10}), \
             mock.patch.object(download.subprocess, "run") as mux:
            with self.assertRaises(download.DownloadError):
                download.import_browser_media(self.job, MEDIA, None, {"source_url": SOURCE, "id": VIDEO_ID}, "ffmpeg")
        mux.assert_not_called()

    def test_browser_import_rejects_mismatched_stream_durations(self):
        probes = [{"video": True, "audio": False, "duration": 10},
                  {"video": False, "audio": True, "duration": 30}]
        with mock.patch.object(download, "_download", side_effect=self.fake_download), \
             mock.patch.object(download, "_probe_local_media", side_effect=probes), \
             mock.patch.object(download.subprocess, "run") as mux:
            with self.assertRaises(download.DownloadError):
                download.import_browser_media(self.job, MEDIA, AUDIO, {"source_url": SOURCE, "id": VIDEO_ID}, "ffmpeg")
        mux.assert_not_called()

    def test_browser_import_keeps_signed_urls_out_of_metadata_and_commands(self):
        commands = []

        def fake_mux(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(VIDEO_BYTES)
            return types.SimpleNamespace(returncode=0)

        probes = [{"video": True, "audio": False, "duration": 10},
                  {"video": False, "audio": True, "duration": 10.02},
                  {"video": True, "audio": True, "duration": 10.02}]
        with mock.patch.object(download, "_download", side_effect=self.fake_download), \
             mock.patch.object(download, "_probe_local_media", side_effect=probes), \
             mock.patch.object(download.subprocess, "run", side_effect=fake_mux):
            result = download.import_browser_media(self.job, MEDIA, AUDIO,
                {"source_url": SOURCE + "?tracking=TEST_ONLY", "id": VIDEO_ID,
                 "title": "Synthetic video", "duration": 10, "unexpected_secret": "TEST_ONLY_PRIVATE"}, "ffmpeg")
        serialized = (self.job / "source" / "metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("TEST_ONLY", serialized)
        self.assertNotIn("TEST_ONLY", str(commands))
        self.assertEqual(result["source_url"], SOURCE)
        self.assertTrue(result["has_audio"])
        self.assertTrue(result["has_video"])
        self.assertFalse((self.job / "source" / "browser-video.mp4").exists())
        self.assertFalse((self.job / "source" / "browser-audio.m4a").exists())


if __name__ == "__main__":
    unittest.main()
