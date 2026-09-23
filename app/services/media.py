from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .vtt import parse_vtt

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def download_url(url: str, temp_dir: Path, source_lang: str) -> tuple[Path, str, list[dict]]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    template = str(temp_dir / "input.%(ext)s")
    info_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--print",
        "%(title)s",
        "--skip-download",
        url,
    ]
    try:
        title = run(info_cmd).stdout.strip().splitlines()[-1]
    except Exception:
        title = "Imported video"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format",
        "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format",
        "mp4",
        "--output",
        template,
        url,
    ]
    run(cmd)

    candidates = [p for p in temp_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
    if not candidates:
        raise RuntimeError("yt-dlp completed but no video file was found")
    input_path = max(candidates, key=lambda p: p.stat().st_size)

    subtitle_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-format",
        "vtt",
        "--sub-langs",
        f"{source_lang},{source_lang}.*",
        "--output",
        str(temp_dir / "subs.%(ext)s"),
        url,
    ]
    try:
        run(subtitle_cmd)
    except Exception:
        pass

    phrases: list[dict] = []
    vtts = sorted(temp_dir.glob("subs*.vtt"))
    if vtts:
        try:
            phrases = parse_vtt(vtts[0])
        except Exception:
            phrases = []
    return input_path, title, phrases


def transcode(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_suffix(".partial.mp4")
    if temp_output.exists():
        temp_output.unlink()

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        r"scale=-2:min(720\,ih)",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-af",
        "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    run(cmd)
    temp_output.replace(output_path)


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(run(cmd).stdout)
    out = {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": None,
        "height": None,
        "video_codec": None,
        "audio_codec": None,
    }
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not out["video_codec"]:
            out["video_codec"] = stream.get("codec_name")
            out["width"] = stream.get("width")
            out["height"] = stream.get("height")
        elif stream.get("codec_type") == "audio" and not out["audio_codec"]:
            out["audio_codec"] = stream.get("codec_name")
    return out


def extract_audio(video_path: Path, start: float, end: float, output_path: Path) -> None:
    duration = max(0.05, end - start)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(output_path),
        ]
    )


def copy_uploaded(input_path: Path, temp_dir: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / f"upload{input_path.suffix.lower() or '.mp4'}"
    shutil.copy2(input_path, target)
    return target
