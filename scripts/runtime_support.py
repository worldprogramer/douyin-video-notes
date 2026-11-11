"""Cheap runtime discovery; importing this module never loads or downloads ASR models."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PACKAGES = ("faster-whisper", "ctranslate2", "numpy", "Pillow", "yt-dlp", "requests")


def _runtime_dir(explicit: str | Path | None = None) -> Path:
    value = explicit or os.environ.get("DOUYIN_NOTES_RUNTIME") or Path.cwd() / ".douyin-notes-runtime"
    return Path(value).expanduser().resolve()


def _command_path(value: str | Path | None) -> str | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(str(value))
    return str(Path(found).resolve()) if found else None


def _project_executable(name: str, runtime_dir: Path) -> tuple[str | None, str | None]:
    """Discover an extracted FFmpeg build without selecting arbitrary duplicates."""
    candidates = []
    for directory in (runtime_dir / "bin", runtime_dir / "ffmpeg"):
        if not directory.is_dir():
            continue
        for filename in (name, name + ".exe"):
            for path in directory.rglob(filename):
                if path.is_file() and path.resolve().is_relative_to(runtime_dir):
                    candidates.append(str(path.resolve()))
    candidates = sorted(set(candidates))
    if len(candidates) > 1:
        return None, f"Multiple project {name} executables; select one with --{name} or DOUYIN_NOTES_{name.upper()}."
    return (candidates[0] if candidates else None), None


def _executable(name: str, explicit: str | Path | None, runtime_dir: Path) -> tuple[str | None, str | None]:
    # An explicitly configured but invalid path is an error, never a silent fallback.
    for label, value in (("argument", explicit),
                         (f"DOUYIN_NOTES_{name.upper()}", os.environ.get(f"DOUYIN_NOTES_{name.upper()}"))):
        if value:
            resolved = _command_path(value)
            return resolved, None if resolved else f"{label}: executable does not exist: {value}"
    path_value = shutil.which(name)
    if path_value:
        return str(Path(path_value).resolve()), None
    return _project_executable(name, runtime_dir)


def _version(executable: str | None) -> str | None:
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = (result.stdout or result.stderr).splitlines()
        return lines[0] if result.returncode == 0 and lines else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gpu_info() -> dict:
    executable = shutil.which("nvidia-smi")
    result = {"devices": [], "inference_tested": False,
              "note": "GPU presence does not verify CUDA/cuBLAS/cuDNN; default to CPU until a real inference probe passes."}
    if not executable:
        return result
    try:
        command = subprocess.run([executable, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if command.returncode == 0:
            for line in command.stdout.splitlines():
                fields = [item.strip() for item in line.split(",")]
                if len(fields) == 3:
                    result["devices"].append({"name": fields[0], "memory_mib": fields[1], "driver": fields[2]})
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def discover_runtime(ffmpeg: str | Path | None = None, ffprobe: str | Path | None = None,
                     model: str | Path | None = None,
                     runtime_dir: str | Path | None = None) -> dict:
    """Return JSON-safe paths and diagnostics for the current Python, without mutations."""
    project_runtime = _runtime_dir(runtime_dir)
    ffmpeg_path, ffmpeg_error = _executable("ffmpeg", ffmpeg, project_runtime)
    ffprobe_path, ffprobe_error = _executable("ffprobe", ffprobe, project_runtime)
    model_value = model or os.environ.get("DOUYIN_NOTES_MODEL") or project_runtime / "models" / "whisper-small"
    model_path = Path(model_value).expanduser().resolve()
    # This is a structural check only. Loading and inference belong to the ASR command.
    missing_model_files = [name for name in ("model.bin", "config.json", "tokenizer.json")
                           if not (model_path / name).is_file() or (model_path / name).stat().st_size == 0]
    if not any((model_path / name).is_file() for name in ("vocabulary.json", "vocabulary.txt")):
        missing_model_files.append("vocabulary.json or vocabulary.txt")
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    versions = {"ffmpeg": _version(ffmpeg_path), "ffprobe": _version(ffprobe_path)}
    missing = [name for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)) if not path]
    missing += [name for name in ("faster-whisper", "ctranslate2", "numpy", "Pillow") if not packages[name]]
    if missing_model_files:
        missing.append("local_model")
    for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        if path and not versions[name]:
            missing.append(name + "_execution")
    return {
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "recommended_python": sys.executable, "runtime_dir": str(project_runtime),
        "ffmpeg": ffmpeg_path, "ffprobe": ffprobe_path, "versions": versions,
        "model": str(model_path), "model_present": not missing_model_files,
        "missing_model_files": missing_model_files, "model_inference_tested": False,
        "packages": packages, "dependencies": packages, "gpu": _gpu_info(),
        "default_device": "cpu", "default_compute_type": "int8",
        "ready": not missing, "missing": missing,
        "configuration_errors": [item for item in (ffmpeg_error, ffprobe_error) if item],
        "download_backend_ready": bool(packages["yt-dlp"]),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Inspect the local video-note runtime without downloading files.")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--model")
    parser.add_argument("--runtime-dir")
    args = parser.parse_args()
    print(json.dumps(discover_runtime(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, model=args.model,
                                     runtime_dir=args.runtime_dir),
                     ensure_ascii=False, indent=2))
