"""Time-linked source frames and contact sheets; no remote services or vision claims."""

from __future__ import annotations

import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat


def _run(command, timeout=300):
    result = subprocess.run([str(x) for x in command], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"媒体处理失败（退出码 {result.returncode}）：{result.stderr[-2500:]}")
    return result


def probe_video(input_path, ffprobe):
    """Return display dimensions, duration, fps, audio presence and source metadata."""
    source = Path(input_path).resolve(strict=True)
    data = json.loads(_run([ffprobe, "-v", "error", "-show_format", "-show_streams",
                            "-of", "json", source]).stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("文件没有可解码的视频轨道。")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确定视频时长；请先将输入转换为完整的视频文件。")
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "25"))
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    if not math.isfinite(fps) or fps <= 0:
        fps = 25.0
    rotation = float(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    width, height = int(video["width"]), int(video["height"])
    if round(rotation) % 180:
        width, height = height, width
    return {"path": str(source), "duration": duration, "width": width, "height": height,
            "fps": fps, "rotation": rotation, "stream_index": video["index"],
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def _extract(input_path, output_path, timestamp, ffmpeg, crop=None, stream_index=0):
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{timestamp:.6f}",
               "-i", input_path, "-map", f"0:{stream_index}", "-frames:v", "1", "-an"]
    if crop is not None:
        x, y, width, height = crop
        command += ["-vf", f"crop=iw*{width}:ih*{height}:iw*{x}:ih*{y}"]
    command += ["-update", "1", dest]
    _run(command)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"时间 {timestamp:.3f} 秒没有可提取画面。")
    with Image.open(dest) as picture:
        picture.verify()
    return dest


def extract_frame(input_path, output_path, timestamp, ffmpeg, ffprobe, crop=None):
    """Extract at seconds; crop is normalized (x, y, width, height), if supplied."""
    metadata = probe_video(input_path, ffprobe)
    timestamp = float(timestamp)
    if not math.isfinite(timestamp) or not 0 <= timestamp < metadata["duration"]:
        raise ValueError(f"截图时间必须位于 0 到 {metadata['duration']:.3f} 秒之间（不含末端）。")
    if crop is not None:
        if len(crop) != 4:
            raise ValueError("裁剪范围需要 x、y、宽度、高度四个 0 到 1 之间的数值。")
        crop = tuple(float(x) for x in crop)
        x, y, width, height = crop
        if (not all(math.isfinite(v) for v in crop) or min(x, y) < 0
                or min(width, height) <= 0 or x + width > 1 or y + height > 1):
            raise ValueError("裁剪范围超出画面。")
    return str(_extract(metadata["path"], output_path, timestamp, ffmpeg, crop, metadata["stream_index"]))


def _scene_times(source, ffmpeg, threshold, duration, stream_index=0):
    # Low-resolution scene scoring is cheap; originals are decoded separately at full resolution.
    result = _run([ffmpeg, "-hide_banner", "-v", "info", "-i", source, "-map", f"0:{stream_index}", "-an",
                   "-vf", f"setpts=PTS-STARTPTS,scale=160:-2,select='gt(scene,{threshold})',showinfo",
                   "-vsync", "0", "-f", "null", "-"], timeout=max(300, int(duration * 3)))
    return [float(t) for t in re.findall(r"pts_time:([0-9.eE+-]+)", result.stderr)]


def _stamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, ms = divmod(rest, 1000)
    prefix = f"{hours:02}:" if hours else ""
    return f"{prefix}{minutes:02}:{secs:02}.{ms // 100}"


def _signature(path):
    with Image.open(path) as picture:
        return picture.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)


def _similar(a, b):
    # RGB difference retains color-only scene changes, unlike perceptual hash alone.
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 < 2.5


def _font(size):
    for name in ("DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _sheets(rows, folder, portrait):
    per_sheet = 6 if portrait else 12
    columns, padding, label_height = 3, 10, 30
    tile_width, tile_height = (360, 640) if portrait else (480, 300)
    font = _font(22)
    sheets = []
    for first in range(0, len(rows), per_sheet):
        group = rows[first:first + per_sheet]
        grid_rows = math.ceil(len(group) / columns)
        sheet = Image.new("RGB", (columns * (tile_width + padding) + padding,
                                   grid_rows * (tile_height + label_height + padding) + padding), "#eeeeee")
        draw = ImageDraw.Draw(sheet)
        relative = f"contact-sheets/sheet-{len(sheets) + 1:03}.jpg"
        for index, row in enumerate(group):
            left = padding + (index % columns) * (tile_width + padding)
            top = padding + (index // columns) * (tile_height + label_height + padding)
            with Image.open(folder / row["image"]) as original:
                thumb = original.convert("RGB")
                thumb.thumbnail((tile_width, tile_height), Image.Resampling.LANCZOS)
                sheet.paste(thumb, (left + (tile_width - thumb.width) // 2,
                                    top + (tile_height - thumb.height) // 2))
            draw.text((left + 6, top + tile_height + 2), row["timestamp"], fill="#222222", font=font)
            row["sheet"] = relative
            row["sheet_cell"] = index + 1
        sheet.save(folder / relative, quality=92, subsampling=0)
        sheets.append({"image": relative, "frame_count": len(group),
                       "start": group[0]["time"], "end": group[-1]["time"]})
    return sheets


def prepare_frames(input_path, output_dir, ffmpeg, ffprobe, transcript_segments=None,
                   interval=5.0, max_frames=180, scene_threshold=0.25):
    """Save full-size frames, small time-labelled contact sheets and an evidence index.

    Uniform anchors always survive deduplication; only redundant scene additions are
    removed. This preserves temporal coverage even for a long unchanging slide.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("截图间隔必须为正数。")
    if max_frames < 2:
        raise ValueError("截图上限至少为 2，以覆盖开头和结尾。")
    if not math.isfinite(scene_threshold) or not 0 <= scene_threshold <= 1:
        raise ValueError("场景阈值必须在 0 到 1 之间。")
    folder = Path(output_dir).resolve()
    (folder / "frames").mkdir(parents=True, exist_ok=True)
    (folder / "contact-sheets").mkdir(exist_ok=True)
    metadata = probe_video(input_path, ffprobe)
    duration = metadata["duration"]
    end = max(0.0, duration - max(1 / metadata["fps"], 0.05))
    requested_count = math.ceil(end / interval) + 1 if end > 0 else 1
    warnings = []
    if requested_count > max_frames:
        anchors = [end * i / (max_frames - 1) for i in range(max_frames)]
        warnings.append("视频较长，已在全片范围均匀取样至截图上限；需要细节时按时间补图。")
    else:
        anchors = [i * interval for i in range(math.floor(end / interval) + 1)]
        if end - anchors[-1] > 0.001:
            anchors.append(end)
    # Uniform coverage consumes at most max_frames. Extra slots emphasize visual transitions.
    times = [(t, "uniform") for t in anchors]
    scene_count = 0
    if len(times) < max_frames:
        try:
            scenes = _scene_times(metadata["path"], ffmpeg, scene_threshold, duration, metadata["stream_index"])
            scene_count = len(scenes)
            candidates = []
            for t in scenes:
                t = min(end, max(0.0, t + min(0.15, 1 / metadata["fps"])))
                if all(abs(t - p[0]) > 0.35 for p in times + candidates):
                    candidates.append((t, "scene"))
            room = max_frames - len(times)
            if len(candidates) > room:
                warnings.append("场景变化较多，仅保留均匀分布的部分变化画面；关键动作可另行补图。")
                if room == 1:
                    candidates = [candidates[len(candidates) // 2]]
                elif room > 1:
                    candidates = [candidates[round(i * (len(candidates) - 1) / (room - 1))] for i in range(room)]
                else:
                    candidates = []
            times += candidates
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            warnings.append(f"场景检测未完成，已保留全片定时截图：{str(error)[:180]}")
    rows, signatures = [], []
    segments = transcript_segments or []
    for time, reason in sorted(times):
        path = folder / "frames" / f"frame-{round(time * 1000):010d}ms.png"
        _extract(metadata["path"], path, time, ffmpeg, stream_index=metadata["stream_index"])
        signature = _signature(path)
        if reason == "scene" and signatures and _similar(signature, signatures[-1]):
            path.unlink()
            continue
        signatures.append(signature)
        nearby = [s for s in segments if float(s.get("start", 0)) <= time + 1.5
                  and float(s.get("end", 0)) >= time - 1.5]
        rows.append({"time": round(time, 3), "timestamp": _stamp(time), "kind": reason,
                     "image": path.relative_to(folder).as_posix(),
                     "transcript": [{"start": s.get("start"), "end": s.get("end"),
                                     "text": s.get("text", "")} for s in nearby]})
    sheets = _sheets(rows, folder, metadata["height"] > metadata["width"])
    gaps = [b["time"] - a["time"] for a, b in zip(rows, rows[1:])]
    manifest = {"version": 1, "source": metadata, "requested_interval": interval,
                "max_frames": max_frames, "frame_count": len(rows), "scene_candidates": scene_count,
                "effective_interval": round(max(gaps, default=0.0), 3),
                "coverage": {"first": rows[0]["time"], "last": rows[-1]["time"],
                             "strategy": "uniform_anchors_plus_deduplicated_scene_changes"},
                "warnings": warnings, "sheets": sheets, "frames": rows}
    (folder / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
