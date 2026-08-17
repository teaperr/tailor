#!/usr/bin/env python3
"""
tailor - convert any media file to a target file size using ffmpeg two-pass encoding.

Usage:
    tailor INPUT SIZE [-o OUTPUT] [options]

Example:
    tailor input.mkv 25M -o clip.mp4
    tailor input.mp4 8M --codec h265 --audio-bitrate 96k
    tailor input.mov 50M --dry-run
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODECS = {
    "h264": {"vcodec": "libx264", "extra": ["-pix_fmt", "yuv420p"]},
    "h265": {"vcodec": "libx265", "extra": ["-pix_fmt", "yuv420p", "-tag:v", "hvc1"]},
    "vp9": {"vcodec": "libvpx-vp9", "extra": ["-row-mt", "1", "-deadline", "good"]},
    "av1": {"vcodec": "libaom-av1", "extra": ["-row-mt", "1", "-cpu-used", "4"]},
}

DEFAULT_EXT = {
    "h264": "mp4",
    "h265": "mp4",
    "vp9": "webm",
    "av1": "webm",
}

SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]?)i?B?$", re.IGNORECASE)
BITRATE_RE = re.compile(r"^([\d.]+)\s*([KM]?)$", re.IGNORECASE)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_size(s):
    """Parse a human size like '25M', '1.5G', '800K' into bytes (binary, 1024-based)."""
    m = SIZE_RE.match(s.strip())
    if not m:
        die(f"could not parse size '{s}' (expected e.g. 25M, 1.5G, 800K)")
    value, unit = m.groups()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unit.upper()]
    return float(value) * mult


def parse_bitrate_bits(s):
    """Parse a bitrate like '128k' or '2M' into bits/sec (decimal, ffmpeg convention)."""
    m = BITRATE_RE.match(s.strip())
    if not m:
        die(f"could not parse bitrate '{s}' (expected e.g. 128k, 2M)")
    value, unit = m.groups()
    mult = {"": 1, "K": 1000, "M": 1000**2}[unit.upper()]
    return float(value) * mult


def fmt_bitrate(bits_per_sec):
    """Format bits/sec as an ffmpeg-friendly kbps string, e.g. '2500k'."""
    kbps = max(1, int(bits_per_sec / 1000))
    return f"{kbps}k"


def ffprobe_duration(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        die("ffprobe not found (is ffmpeg installed and on PATH?)")
    except subprocess.CalledProcessError as e:
        die(f"ffprobe failed on '{path}':\n{e.stderr}")
    data = json.loads(out.stdout)
    dur = data.get("format", {}).get("duration")
    if dur is None:
        die(f"could not determine duration of '{path}'")
    return float(dur)


def has_audio_stream(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return len(data.get("streams", [])) > 0


def run(cmd, quiet=False):
    if not quiet:
        print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(f"ffmpeg exited with code {result.returncode}")


def main():
    p = argparse.ArgumentParser(
        prog="tailor",
        description="Convert a media file to a target file size using ffmpeg two-pass encoding.",
    )
    p.add_argument("input", type=Path, help="input media file")
    p.add_argument("size", help="target size, e.g. 25M, 8M, 1.5G")
    p.add_argument("-o", "--output", type=Path, default=None, help="output file path")
    p.add_argument("--codec", choices=CODECS.keys(), default="h264", help="video codec (default: h264)")
    p.add_argument("--preset", default="medium", help="ffmpeg encoder preset (default: medium)")
    p.add_argument("--audio-bitrate", default="128k", help="audio bitrate, e.g. 128k (default: 128k)")
    p.add_argument("--no-audio", action="store_true", help="strip audio entirely")
    p.add_argument(
        "--tolerance", type=float, default=2.0,
        help="percent safety margin subtracted from target size to account for "
             "container/muxing overhead (default: 2.0)",
    )
    p.add_argument("--min-video-bitrate", default="100k", help="floor for computed video bitrate (default: 100k)")
    p.add_argument("--dry-run", action="store_true", help="print the computed bitrate plan and exit")
    p.add_argument("--keep-logs", action="store_true", help="keep ffmpeg two-pass log files")
    args = p.parse_args()

    if not args.input.exists():
        die(f"input file '{args.input}' does not exist")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        die("ffmpeg/ffprobe not found on PATH")

    target_bytes = parse_size(args.size)
    duration = ffprobe_duration(args.input)
    audio_present = has_audio_stream(args.input) and not args.no_audio

    safety = 1 - (args.tolerance / 100.0)
    total_bitrate = (target_bytes * 8 / duration) * safety

    audio_bits = parse_bitrate_bits(args.audio_bitrate) if audio_present else 0
    video_bitrate = total_bitrate - audio_bits
    min_video_bitrate = parse_bitrate_bits(args.min_video_bitrate)

    if video_bitrate < min_video_bitrate:
        print(
            f"warning: computed video bitrate ({fmt_bitrate(video_bitrate)}) is below the "
            f"floor of {fmt_bitrate(min_video_bitrate)}. Using the floor instead; "
            f"output will likely exceed the target size.",
            file=sys.stderr,
        )
        video_bitrate = min_video_bitrate

    ext = DEFAULT_EXT[args.codec]
    output = args.output or args.input.with_name(f"{args.input.stem}_{args.size}.{ext}")

    print(f"input duration:    {duration:.2f}s", file=sys.stderr)
    print(f"target size:        {args.size} (~{target_bytes / 1024**2:.2f} MiB)", file=sys.stderr)
    print(f"safety margin:      {args.tolerance}%", file=sys.stderr)
    print(f"audio:              {'copy at ' + args.audio_bitrate if audio_present else 'none'}", file=sys.stderr)
    print(f"video bitrate:      {fmt_bitrate(video_bitrate)}", file=sys.stderr)
    print(f"codec:              {args.codec} ({CODECS[args.codec]['vcodec']})", file=sys.stderr)
    print(f"output:             {output}", file=sys.stderr)

    if args.dry_run:
        return

    codec_info = CODECS[args.codec]
    vbitrate_str = fmt_bitrate(video_bitrate)

    with tempfile.TemporaryDirectory() as tmpdir:
        passlog = str(Path(tmpdir) / "tailor2pass")

        pass1 = [
            "ffmpeg", "-y", "-i", str(args.input),
            "-c:v", codec_info["vcodec"],
            "-b:v", vbitrate_str,
            "-preset", args.preset,
            *codec_info["extra"],
            "-pass", "1", "-passlogfile", passlog,
            "-an", "-f", "null", "/dev/null" if sys.platform != "win32" else "NUL",
        ]

        pass2 = [
            "ffmpeg", "-y", "-i", str(args.input),
            "-c:v", codec_info["vcodec"],
            "-b:v", vbitrate_str,
            "-preset", args.preset,
            *codec_info["extra"],
            "-pass", "2", "-passlogfile", passlog,
        ]
        if audio_present:
            pass2 += ["-c:a", "aac", "-b:a", args.audio_bitrate]
        else:
            pass2 += ["-an"]
        pass2.append(str(output))

        run(pass1)
        run(pass2)

    actual = output.stat().st_size
    print(
        f"\ndone: {output} ({actual / 1024**2:.2f} MiB, target was "
        f"{target_bytes / 1024**2:.2f} MiB)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
