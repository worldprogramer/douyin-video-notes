#!/usr/bin/env python3
"""Download one public Douyin video using its official mobile share page.

No paid parsing service, browser-cookie extraction, or request-signature code.
The optional yt-dlp fallback runs only if that package is already installed.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
)
SOURCE_DOMAINS = ("douyin.com", "iesdouyin.com")
MEDIA_DOMAINS = SOURCE_DOMAINS + (
    "douyinvod.com", "douyincdn.com", "bytecdn.cn", "byteimg.com",
    "amemv.com", "snssdk.com", "ixigua.com", "bytedance.com", "bytedance.net",
    "zjcdn.com",
)
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


def _allowed(url: str, domains: tuple[str, ...]) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        return (p.scheme in ("https", "http") and p.port in (None, 80, 443)
                and not p.username and not p.password
                and any(host == d or host.endswith("." + d) for d in domains))
    except ValueError:
        return False


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains):
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl, self.domains):
            raise DownloadError("下载跳转到了不支持的域名，已停止。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, media=False):
    domains = MEDIA_DOMAINS if media else SOURCE_DOMAINS
    if not _allowed(url, domains):
        raise DownloadError("链接不是受支持的抖音来源或媒体地址。")
    req = urllib.request.Request(url, headers={
        "User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/",
        "Accept": "*/*" if media else "text/html,application/xhtml+xml",
    })
    opener = urllib.request.build_opener(_Redirects(domains))
    return opener.open(req, timeout=30)


def extract_input(text: str) -> str:
    if re.fullmatch(r"\d{15,24}", text.strip()):
        return "https://www.douyin.com/video/" + text.strip()
    for m in re.finditer(r"https?://[^\s<>\"'，。；]+", text):
        value = m.group().rstrip(").,，。；）")
        if _allowed(value, SOURCE_DOMAINS):
            return value
    raise DownloadError("未找到有效的抖音链接；请保留分享文本中的完整链接。")


def extract_id(url: str) -> str | None:
    p = urllib.parse.urlsplit(url)
    m = re.search(r"/(?:video|note)/(\d{15,24})(?:/|$)", p.path)
    if m:
        return m.group(1)
    for key in ("modal_id", "aweme_id", "item_id"):
        val = urllib.parse.parse_qs(p.query).get(key, [""])[0]
        if re.fullmatch(r"\d{15,24}", val):
            return val
    return None


def _public_source(url: str) -> str:
    """Keep provenance without tracking or temporary share parameters."""
    video_id = extract_id(url)
    if video_id:
        return f"https://www.douyin.com/video/{video_id}"
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_page(page: str) -> list[dict]:
    """Decode embedded JSON without evaluating page JavaScript."""
    roots = []
    decoder = json.JSONDecoder()
    for marker in re.finditer(r"(?:window\.)?_ROUTER_DATA\s*=\s*", page):
        try:
            roots.append(decoder.raw_decode(page[marker.end():].lstrip())[0])
        except (ValueError, TypeError):
            pass
    for m in re.finditer(r"<script\b[^>]*\bid=[\"'](RENDER_DATA|__UNIVERSAL_DATA_FOR_REHYDRATION__)[\"'][^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            raw = html.unescape(m.group(2)).strip()
            roots.append(json.loads(urllib.parse.unquote(raw) if m.group(1) == "RENDER_DATA" else raw))
        except (ValueError, TypeError):
            pass
    return roots


def find_video(roots: list, video_id: str) -> dict | None:
    queue = list(roots)
    seen = 0
    while queue and seen < 100000:
        value = queue.pop()
        seen += 1
        if isinstance(value, dict):
            if str(value.get("aweme_id", "")) == video_id and isinstance(value.get("video"), dict):
                return value
            queue.extend(value.values())
        elif isinstance(value, list):
            queue.extend(value)
    return None


def _addresses(item: dict) -> list[str]:
    video = item.get("video") or {}
    variants = sorted(video.get("bit_rate") or [], key=lambda x: x.get("bit_rate") or 0, reverse=True)
    groups = [v.get("play_addr") for v in variants]
    groups += [video.get("play_addr"), video.get("play_addr_h264"), video.get("download_addr")]
    result = []
    for group in groups:
        for url in (group or {}).get("url_list") or []:
            if isinstance(url, str) and _allowed(url, MEDIA_DOMAINS) and url not in result:
                result.append(url)
    return result


def _media_valid(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    with path.open("rb") as f:
        head = f.read(64)
    return b"ftyp" in head or head.startswith(b"\x1aE\xdf\xa3")


def _download(url: str, dest: Path) -> int:
    part = dest.with_suffix(dest.suffix + ".part")
    total = 0
    start = time.monotonic()
    try:
        with _open(url, media=True) as response, part.open("wb") as output:
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > MAX_VIDEO_BYTES:
                raise DownloadError("视频超过 2 GB 下载上限，请使用本地文件。")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES or time.monotonic() - start > 600:
                    raise DownloadError("视频下载达到大小或时间上限。")
                output.write(chunk)
            if declared and declared != total:
                raise DownloadError("视频下载不完整。")
        if not _media_valid(part):
            raise DownloadError("返回内容不是可识别的视频文件。")
        part.replace(dest)
        return total
    finally:
        if part.exists():
            part.unlink()


def _error_message(exc: Exception) -> str:
    # Network exceptions can contain signed media URLs; never expose those.
    if isinstance(exc, urllib.error.HTTPError):
        return f"抖音请求返回 HTTP {exc.code}"
    if isinstance(exc, DownloadError):
        return str(exc)
    return f"网络或解析失败（{type(exc).__name__}）"


def _fetch_share(source: str, job_dir: Path) -> dict:
    video_id = extract_id(source)
    page = ""
    if not video_id:
        with _open(source) as response:
            video_id = extract_id(response.geturl())
            page = response.read(MAX_PAGE_BYTES + 1).decode("utf-8", errors="replace")
    if not video_id:
        raise DownloadError("分享链接没有跳转到可识别的单条视频。")
    item = find_video(parse_page(page), video_id) if page else None
    if not item:
        with _open(f"https://www.iesdouyin.com/share/video/{video_id}/") as response:
            raw = response.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                raise DownloadError("分享页面超过读取上限。")
            page = raw.decode("utf-8", errors="replace")
        item = find_video(parse_page(page), video_id)
    if not item:
        raise DownloadError("公开分享页没有返回该视频数据，可能需要登录或页面结构已改变。")
    if item.get("aweme_type") in (68, 150) or item.get("images"):
        raise DownloadError("这是图文作品，当前流程只处理视频。")
    urls = _addresses(item)
    if not urls:
        raise DownloadError("分享页未提供可下载的视频地址。")
    dest = job_dir / "source" / "video.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for url in urls[:4]:
        try:
            size = _download(url, dest)
            break
        except Exception as exc:
            failures.append(_error_message(exc))
    else:
        raise DownloadError("；".join(dict.fromkeys(failures)))
    return {
        "path": str(dest.resolve()), "id": video_id,
        "title": str(item.get("desc") or item.get("item_title") or "抖音视频"),
        "author": str((item.get("author") or {}).get("nickname") or ""),
        "source_url": _public_source(source), "resolved_url": f"https://www.douyin.com/video/{video_id}",
        "method": "douyin-mobile-share", "bytes": size,
        "duration": (item.get("video", {}).get("duration") or item.get("duration") or 0) / 1000,
        "published_timestamp": item.get("create_time"),
    }


def _fetch_ytdlp(source: str, job_dir: Path) -> dict:
    try:
        import yt_dlp
    except ImportError:
        raise DownloadError("可选备用下载器 yt-dlp 尚未安装。") from None
    class QuietLogger:
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass
    directory = job_dir / "source"
    directory.mkdir(parents=True, exist_ok=True)
    # Never let yt-dlp's existing-file optimization adopt an orphan from a
    # previous request. Download in a new private directory on every attempt,
    # verify the returned source ID, then publish only that new media file.
    with tempfile.TemporaryDirectory(prefix=".download-", dir=directory) as staging:
        options = {"format": "best[ext=mp4]/best", "outtmpl": str(Path(staging) / "video.%(ext)s"),
                   "noplaylist": True, "quiet": True, "no_warnings": True, "cachedir": False,
                   "socket_timeout": 20, "retries": 1, "fragment_retries": 1,
                   "logger": QuietLogger(), "overwrites": False}
        try:
            with yt_dlp.YoutubeDL(options) as client:
                info = client.extract_info(source, download=True)
                staged_path = Path(client.prepare_filename(info)).resolve()
        except Exception:
            raise DownloadError("yt-dlp 未能下载；该视频可能需要当前有效的登录状态或平台校验。") from None
        expected_id = extract_id(source)
        actual_id = str(info.get("id") or "")
        if expected_id and actual_id != expected_id:
            raise DownloadError("备用下载器返回了不同作品，已停止。")
        if staged_path.parent != Path(staging).resolve() or not _media_valid(staged_path):
            raise DownloadError("备用下载器未生成有效视频。")
        path = directory / ("video" + staged_path.suffix)
        staged_path.replace(path)
    return {"path": str(path.resolve()), "id": str(info.get("id") or ""),
            "title": info.get("title") or "抖音视频", "author": info.get("uploader") or "",
            "source_url": _public_source(source), "resolved_url": _public_source(info.get("webpage_url") or source),
            "method": "yt-dlp", "bytes": path.stat().st_size,
            "duration": info.get("duration"), "published_timestamp": info.get("timestamp")}


def fetch_video(input_text: str, job_dir: str | Path, *, allow_ytdlp=True) -> dict:
    job_dir = Path(job_dir).resolve()
    source = extract_input(input_text)
    metadata_path = job_dir / "source" / "metadata.json"
    if metadata_path.is_file():
        try:
            cached = json.loads(metadata_path.read_text(encoding="utf-8"))
            cached_path = Path(cached.get("path", ""))
            same_source = cached.get("source_url") == _public_source(source)
            if not same_source and extract_id(source):
                same_source = str(cached.get("id")) == extract_id(source)
            if not same_source and not extract_id(source):
                # A short URL can be an alias of a browser-imported canonical URL.
                # Resolve that URL once; never trust a caller-supplied ID mapping.
                with _open(source) as response:
                    resolved_id = extract_id(response.geturl())
                same_source = bool(resolved_id and resolved_id == str(cached.get("id")))
            if same_source and cached_path.parent == metadata_path.parent and _media_valid(cached_path):
                return {**cached, "cached": True}
        except (ValueError, OSError):
            pass
        raise DownloadError("此工作目录已有另一来源或不完整记录，请使用新的工作目录。")
    try:
        result = _fetch_share(source, job_dir)
    except Exception as exc:
        primary_error = _error_message(exc)
        if not allow_ytdlp:
            raise DownloadError(primary_error) from None
        try:
            result = _fetch_ytdlp(source, job_dir)
        except Exception as fallback:
            raise DownloadError(primary_error + "；" + _error_message(fallback)) from None
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temp = metadata_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(metadata_path)
    return result


def _probe_local_media(path: Path, ffmpeg: str) -> dict:
    """Inspect local files only; never place transport URLs in process arguments."""
    executable = Path(ffmpeg)
    sibling = executable.with_name("ffprobe.exe" if executable.suffix.lower() == ".exe" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if ffprobe:
        process = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                  "-of", "json", str(path)], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        if process.returncode:
            raise DownloadError("本地媒体检查失败。")
        data = json.loads(process.stdout)
        streams = data.get("streams") or []
        return {"duration": float(data.get("format", {}).get("duration") or 0),
                "video": any(s.get("codec_type") == "video" for s in streams),
                "audio": any(s.get("codec_type") == "audio" for s in streams)}
    # imageio-ffmpeg installations may have no ffprobe. FFmpeg's input report
    # still identifies stream types and duration without decoding the full file.
    process = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    report = process.stderr
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", report)
    return {"duration": sum(float(v) * mult for v, mult in zip(match.groups(), (3600, 60, 1))) if match else 0,
            "video": bool(re.search(r"Stream #.*Video:", report)),
            "audio": bool(re.search(r"Stream #.*Audio:", report))}


def import_browser_media(job_dir: str | Path, video_url: str, audio_url: str | None,
                         metadata: dict, ffmpeg: str) -> dict:
    """Import URLs already observed in an authorized browser's media inventory.

    Callers must pass the actual supplied video/audio stream pair. No URL is
    reconstructed from hidden state, identifiers, signatures, or blob values.
    Only allowlisted provenance fields are persisted; transport URLs stay in memory.
    """
    job_dir = Path(job_dir).resolve()
    source_dir = job_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = source_dir / "metadata.json"
    source = extract_input(str(metadata.get("source_url") or metadata.get("resolved_url") or ""))
    source_id = extract_id(source)
    video_id = str(metadata.get("id") or source_id or "")
    if not re.fullmatch(r"\d{15,24}", video_id):
        raise DownloadError("浏览器来源记录缺少有效视频 ID。")
    if source_id and source_id != video_id:
        raise DownloadError("浏览器来源链接与视频 ID 不一致，已停止。")
    if not _allowed(video_url, MEDIA_DOMAINS) or (audio_url and not _allowed(audio_url, MEDIA_DOMAINS)):
        raise DownloadError("浏览器清单含不支持的媒体域名，或提供了 blob 地址。")
    if metadata_path.is_file():
        cached = json.loads(metadata_path.read_text(encoding="utf-8"))
        cached_path = Path(cached.get("path", ""))
        if (str(cached.get("id")) == video_id and cached_path.parent == source_dir
                and _media_valid(cached_path)):
            return {**cached, "cached": True}
        raise DownloadError("工作目录已存在其他来源记录，请使用新的工作目录。")
    video_path = source_dir / "browser-video.mp4"
    audio_path = source_dir / "browser-audio.m4a"
    output = source_dir / "video.mp4"
    temporary = source_dir / "video.muxing.mp4"
    try:
        _download(video_url, video_path)
        if audio_url:
            _download(audio_url, audio_path)
        video_info = _probe_local_media(video_path, ffmpeg)
        if not video_info["video"]:
            raise DownloadError("选中的视频流没有视频轨，请重新匹配浏览器媒体清单。")
        if audio_url:
            audio_info = _probe_local_media(audio_path, ffmpeg)
            if not audio_info["audio"]:
                raise DownloadError("选中的音频流没有音轨。")
            if abs(video_info["duration"] - audio_info["duration"]) > max(2, video_info["duration"] * 0.02):
                raise DownloadError("视频与音频时长不匹配，可能选中了其他作品的音频。")
        elif not video_info["audio"] and not metadata.get("silent_video", False):
            raise DownloadError("这是分离视频流，必须同时提供浏览器清单中的对应音频地址。")
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)]
        if audio_url:
            command += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a?"]
        command += ["-c", "copy", "-movflags", "+faststart", str(temporary)]
        mux = subprocess.run(command, capture_output=True, timeout=180)
        if mux.returncode or not _media_valid(temporary):
            raise DownloadError("视频音频合并失败，原始流已保留供排查。")
        final_info = _probe_local_media(temporary, ffmpeg)
        expected = float(metadata.get("duration") or video_info["duration"] or 0)
        if not final_info["video"] or (not metadata.get("silent_video", False) and not final_info["audio"]):
            raise DownloadError("合并结果缺少必要的视频或音频轨。")
        if expected and abs(final_info["duration"] - expected) > max(2, expected * 0.02):
            raise DownloadError("合并后的时长与当前视频不匹配。")
        temporary.replace(output)
        result = {"path": str(output), "id": video_id,
                  "title": str(metadata.get("title") or "抖音视频"),
                  "author": str(metadata.get("author") or ""),
                  "source_url": _public_source(source),
                  "resolved_url": f"https://www.douyin.com/video/{video_id}",
                  "method": "browser-observed-streams" if audio_url else "browser-observed-media",
                  "bytes": output.stat().st_size, "duration": final_info["duration"],
                  "has_audio": final_info["audio"], "has_video": final_info["video"],
                  "published_timestamp": metadata.get("published_timestamp")}
        temp_meta = metadata_path.with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_meta.replace(metadata_path)
        video_path.unlink()
        if audio_path.exists():
            audio_path.unlink()
        return result
    except (DownloadError, KeyboardInterrupt):
        raise
    except Exception as exc:
        raise DownloadError(_error_message(exc)) from None
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "browser-import":
        parser = argparse.ArgumentParser(description="导入浏览器中已观察到的视频与音频地址。")
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--job-dir", required=True)
        parser.add_argument("--ffmpeg", required=True)
        args = parser.parse_args(sys.argv[2:])
        try:
            data = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
            result = import_browser_media(args.job_dir, data["video_url"], data.get("audio_url"),
                                          data.get("metadata") or {}, args.ffmpeg)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"error": _error_message(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        return 0
    parser = argparse.ArgumentParser(description="下载单条抖音视频，保存净化后的来源信息。")
    parser.add_argument("input", help="分享链接或完整分享文本")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--no-ytdlp", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(fetch_video(args.input, args.job_dir, allow_ytdlp=not args.no_ytdlp), ensure_ascii=False, indent=2))
    except DownloadError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
