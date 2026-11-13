#!/usr/bin/env python3
"""
tailor - shrink any media file to a target size with the best quality that fits.

Works out the bitrate budget from the target size and duration, then picks the
output resolution and frame rate that budget can actually support (the same idea
as 8mb.video), and runs a two-pass ffmpeg encode. If the result still lands over
the target, it re-plans with a lower bitrate and tries again.

By default the input file is replaced with the compressed version. The original is
only overwritten once the new file has fit under the target; pass -o to write a
separate file instead.

Usage:
    tailor INPUT SIZE [-o OUTPUT] [options]

Examples:
    tailor input.mp4 8M              # replaces input.mp4 with a <=8 MB version
    tailor input.mkv 25M -o clip.mp4 # keeps the original
    tailor input.mp4 8M --codec h265
    tailor input.mov 50M --max-res 720 --dry-run
    tailor podcast.wav 5M            # .wav can't be replaced in place -> podcast_5M.opus
"""

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# min_bpp: minimum "bits per pixel per frame" the encoder needs to look decent.
# Below it, tailor lowers resolution / frame rate instead of smearing the picture.
CODECS = {
    "h264": {"vcodec": "libx264", "ext": "mp4", "min_bpp": 0.060, "preset": "slow"},
    "h265": {"vcodec": "libx265", "ext": "mp4", "min_bpp": 0.045, "preset": "medium"},
    "vp9": {"vcodec": "libvpx-vp9", "ext": "webm", "min_bpp": 0.045, "preset": None},
    "av1": {"vcodec": "libaom-av1", "ext": "webm", "min_bpp": 0.035, "preset": None},
}

# Short-side resolutions tried, largest first (1080 = 1920x1080 or 1080x1920).
RES_LADDER = [2160, 1440, 1080, 720, 540, 480, 360, 270, 240, 180, 144]

# Containers that can hold a re-encode under the same name (so a file can be replaced
# in place). Anything else, e.g. a .wav, gets a new file instead.
IN_PLACE_VIDEO = (".mp4", ".m4v", ".mov", ".mkv", ".webm")
IN_PLACE_AUDIO = (".mp3", ".m4a", ".opus", ".ogg", ".mka")

AUDIO_MIN_BPS = 40_000
AUDIO_MAX_BPS = 128_000
AUDIO_ONLY_MAX_BPS = 320_000

SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]?)(i?)B?$", re.IGNORECASE)
BITRATE_RE = re.compile(r"^([\d.]+)\s*([KM]?)$", re.IGNORECASE)


def log(*a):
    print(*a, file=sys.stderr)


def die(msg):
    log(f"error: {msg}")
    sys.exit(1)


def parse_size(s):
    """
    Parse '25M', '25MB' (decimal, 1 MB = 1,000,000 bytes) or '25MiB' (binary).
    Decimal is the default because it's the conservative reading: a file that fits
    under 25 MB also fits under 25 MiB, which is what upload limits usually mean.
    """
    m = SIZE_RE.match(s.strip())
    if not m:
        die(f"could not parse size '{s}' (expected e.g. 25M, 8MB, 1.5G, 10MiB)")
    value, unit, binary = m.groups()
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[unit.upper()]
    base = 1024 if binary else 1000
    return float(value) * base**power


def parse_bitrate_bits(s):
    m = BITRATE_RE.match(s.strip())
    if not m:
        die(f"could not parse bitrate '{s}' (expected e.g. 128k, 2M)")
    value, unit = m.groups()
    mult = {"": 1, "K": 1000, "M": 1000**2}[unit.upper()]
    return float(value) * mult


def fmt_bitrate(bps):
    return f"{max(1, int(round(bps / 1000)))}k"


def fmt_size(b):
    return f"{b / 1e6:.2f} MB ({b / 1024**2:.2f} MiB)"


def parse_rate(s):
    try:
        num, den = s.split("/")
        den = float(den)
        return float(num) / den if den else 0.0
    except (ValueError, AttributeError):
        return 0.0


def even(n):
    return max(2, int(round(n / 2)) * 2)


def probe(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate"
        ":stream_tags=rotate:stream_side_data=rotation:stream_disposition=attached_pic",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        die("ffprobe not found (is ffmpeg installed and on PATH?)")
    except subprocess.CalledProcessError as e:
        die(f"ffprobe failed on '{path}':\n{e.stderr}")

    data = json.loads(out.stdout)
    streams = data.get("streams", [])

    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        die(f"could not determine duration of '{path}'")
    if duration <= 0:
        die(f"'{path}' has no duration")

    video = None
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        if s.get("disposition", {}).get("attached_pic"):
            continue  # cover art, not real video
        w, h = s.get("width"), s.get("height")
        if not w or not h:
            continue

        rot = 0
        for sd in s.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = sd["rotation"]
        if not rot:
            try:
                rot = float(s.get("tags", {}).get("rotate", 0))
            except ValueError:
                rot = 0
        if abs(int(rot)) % 180 == 90:
            w, h = h, w  # ffmpeg autorotates, so work in the displayed orientation

        fps = parse_rate(s.get("avg_frame_rate")) or parse_rate(s.get("r_frame_rate")) or 30.0
        video = {"width": w, "height": h, "fps": fps}
        break

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return {"duration": duration, "video": video, "has_audio": has_audio}


def choose_plan(v, vbits, codec, args):
    """Pick the output resolution and fps this video bitrate can support."""
    w, h, src_fps = v["width"], v["height"], v["fps"]
    base_fps = args.fps or src_fps
    short = min(w, h)

    def dims(s):
        k = s / short
        return even(w * k), even(h * k)

    def plan_for(s, f):
        pw, ph = dims(s) if s != short else (even(w), even(h))
        return {
            "short": s, "width": pw, "height": ph, "fps": f,
            "bpp": vbits / (pw * ph * f),
        }

    if args.no_scale:
        return plan_for(short, base_fps)

    top = min(short, args.max_res) if args.max_res else short
    ladder = [top] + [s for s in RES_LADDER if s < top]
    min_bpp = args.min_bpp or CODECS[codec]["min_bpp"]

    plan = None
    for s in ladder:
        fps_opts = [base_fps]
        if not args.fps:
            # Prefer dropping resolution over dropping below 30 fps; only go lower
            # once the picture is already small.
            for f, max_short in ((30, None), (24, 360), (15, 240)):
                if f < base_fps - 0.5 and (max_short is None or s <= max_short):
                    fps_opts.append(f)
        for f in fps_opts:
            plan = plan_for(s, f)
            if plan["bpp"] >= min_bpp:
                return plan
    return plan  # nothing met the threshold: smallest option


def video_filter(v, plan):
    parts = []
    if plan["width"] != v["width"] or plan["height"] != v["height"]:
        parts.append(f"scale={plan['width']}:{plan['height']}:flags=lanczos")
    if abs(plan["fps"] - v["fps"]) > 0.01:
        parts.append(f"fps={plan['fps']:g}")
    return ",".join(parts)


def video_args(codec, preset, vbits, pass_no):
    info = CODECS[codec]
    a = ["-c:v", info["vcodec"], "-b:v", fmt_bitrate(vbits), "-pix_fmt", "yuv420p"]
    if codec == "h264":
        a += ["-preset", preset, "-profile:v", "high", "-x264-params", "aq-mode=3"]
    elif codec == "h265":
        a += ["-preset", preset, "-tag:v", "hvc1",
              "-x265-params", "aq-mode=3:log-level=error"]
    elif codec == "vp9":
        a += ["-deadline", "good", "-cpu-used", "4" if pass_no == 1 else "2",
              "-row-mt", "1", "-auto-alt-ref", "1", "-lag-in-frames", "25"]
    elif codec == "av1":
        a += ["-cpu-used", "4", "-row-mt", "1"]
    return a


def audio_codec_for(suffix):
    s = suffix.lower()
    if s in (".webm", ".ogg", ".opus", ".mka"):
        return "libopus"
    if s == ".mp3":
        return "libmp3lame"
    return "aac"


def audio_args(acodec, abits):
    a = ["-c:a", acodec, "-b:a", fmt_bitrate(abits), "-ac", "1" if abits < 64_000 else "2"]
    if acodec == "libopus":
        a += ["-vbr", "constrained"]  # default VBR can drift well off the target bitrate
    return a


def run(cmd):
    log("+ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(f"ffmpeg exited with code {result.returncode}")


def parse_args():
    p = argparse.ArgumentParser(
        prog="tailor",
        description="Shrink a media file to a target size, picking the resolution and "
                    "frame rate the bitrate budget can support.",
    )
    p.add_argument("input", type=Path, help="input media file")
    p.add_argument("size", help="target size: 8M / 8MB (decimal) or 8MiB (binary)")
    p.add_argument("-o", "--output", type=Path,
                   help="write to this path instead of replacing the input in place")
    p.add_argument("--codec", choices=CODECS.keys(),
                   help="video codec (default: h264, or vp9 for .webm)")
    p.add_argument("--preset", help="x264/x265 preset (default: slow for h264, medium for h265)")
    p.add_argument("--audio-bitrate", default="auto",
                   help="audio bitrate, e.g. 96k (default: auto, ~12%% of budget, 40k-128k)")
    p.add_argument("--no-audio", action="store_true", help="strip audio")
    p.add_argument("-r", "--max-res", type=int, metavar="N",
                   help="cap the short side of the output, e.g. 720 for 720p")
    p.add_argument("--fps", type=float, help="force an output frame rate")
    p.add_argument("--no-scale", action="store_true",
                   help="keep source resolution and frame rate (quality drops instead)")
    p.add_argument("--min-bpp", type=float, metavar="X",
                   help="bits per pixel per frame needed before downscaling stops "
                        "(raise for sharper/smaller output, lower for bigger/softer)")
    p.add_argument("--tolerance", type=float, default=2.0,
                   help="percent of the budget held back for container overhead (default: 2)")
    p.add_argument("--min-video-bitrate", default="100k",
                   help="floor for the video bitrate (default: 100k)")
    p.add_argument("--retries", type=int, default=2,
                   help="re-encode this many times if the file lands over the target (default: 2)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    return p.parse_args()


def main():
    args = parse_args()

    if not args.input.exists():
        die(f"input file '{args.input}' does not exist")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        die("ffmpeg/ffprobe not found on PATH")

    target_bytes = parse_size(args.size)
    info = probe(args.input)
    duration, v = info["duration"], info["video"]
    audio_present = info["has_audio"] and not args.no_audio

    if v is None and not audio_present:
        die("input has no usable video or audio streams")

    # No -o: replace the input with the compressed version. The encode goes to a hidden
    # temp file beside it and is only swapped in once it has fit under the target.
    in_place = args.output is None
    suffix = args.input.suffix.lower()
    if in_place and suffix not in (IN_PLACE_VIDEO if v else IN_PLACE_AUDIO):
        log(f"note: a '{args.input.suffix}' file can't be re-encoded in place; "
            f"writing a new file next to it instead (use -o to choose the name).")
        in_place = False

    target_suffix = suffix if in_place else (args.output.suffix.lower() if args.output else "")
    codec = args.codec or ("vp9" if target_suffix == ".webm" else "h264")

    if in_place:
        final = args.input
        output = args.input.with_name(f".{args.input.stem}.tailor-tmp{args.input.suffix}")
        atexit.register(lambda: output.unlink(missing_ok=True))
        src_size = args.input.stat().st_size
        if src_size <= target_bytes and not args.dry_run:
            log(f"{args.input} is already {fmt_size(src_size)}, under the "
                f"{fmt_size(target_bytes)} target; nothing to do.")
            return
    else:
        ext = CODECS[codec]["ext"] if v else "opus"
        final = args.output or args.input.with_name(f"{args.input.stem}_{args.size}.{ext}")
        output = final
        if final.resolve() == args.input.resolve():
            die("output would overwrite the input file; omit -o to replace it in place")

    if v and output.suffix.lower() == ".webm" and codec in ("h264", "h265"):
        die(f"{codec} can't go in a .webm file; use --codec vp9/av1 or a .mp4/.mkv output")

    def commit(size):
        """In-place mode: swap the finished temp file over the input, if it fits."""
        if not in_place:
            return
        if size > target_bytes:
            output.unlink(missing_ok=True)
            log("original left untouched.")
            return
        shutil.copymode(args.input, output)
        os.replace(output, args.input)

    acodec = audio_codec_for(output.suffix)
    preset = args.preset or CODECS[codec]["preset"]
    min_vbits = parse_bitrate_bits(args.min_video_bitrate)

    total_bps = (target_bytes * 8 / duration) * (1 - args.tolerance / 100.0)

    # Audio budget.
    if not audio_present:
        abits = 0
    elif v is None:
        abits = min(total_bps, AUDIO_ONLY_MAX_BPS)
    elif args.audio_bitrate != "auto":
        abits = parse_bitrate_bits(args.audio_bitrate)
    else:
        abits = min(AUDIO_MAX_BPS, max(AUDIO_MIN_BPS, total_bps * 0.12))

    if v is not None:
        vbits = total_bps - abits
        if vbits < min_vbits:
            log(f"warning: only {fmt_bitrate(vbits)} left for video; using the "
                f"{fmt_bitrate(min_vbits)} floor, so the output will likely run over the target.")
            vbits = min_vbits

    def describe(vbits, plan):
        log(f"input:        {args.input}  ({duration:.1f}s)")
        log(f"target:       {fmt_size(target_bytes)}")
        if v is not None:
            log(f"source:       {v['width']}x{v['height']} @ {v['fps']:.2f} fps")
            log(f"output:       {plan['width']}x{plan['height']} @ {plan['fps']:g} fps "
                f"({plan['bpp']:.3f} bits/pixel)")
            log(f"video:        {codec} ({CODECS[codec]['vcodec']}) @ {fmt_bitrate(vbits)}")
        if audio_present:
            log(f"audio:        {acodec} @ {fmt_bitrate(abits)}"
                f"{' mono' if abits < 64_000 else ''}")
        elif info["has_audio"]:
            log("audio:        stripped")
        log(f"file:         {final}{'  (replacing the original)' if in_place else ''}")

    if args.dry_run:
        if v is not None:
            describe(vbits, choose_plan(v, vbits, codec, args))
        else:
            describe(0, None)
        return

    if v is None:
        # Audio-only: single pass, the bitrate is the whole budget.
        floor = 8_000
        for attempt in range(args.retries + 1):
            if attempt:
                log(f"\nretry {attempt}/{args.retries}:")
            describe(0, None)
            run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-stats",
                 "-i", str(args.input), "-vn", "-map", "0:a:0",
                 *audio_args(acodec, abits), str(output)])
            size = output.stat().st_size
            if size <= target_bytes:
                break
            log(f"\nover target by {(size - target_bytes) / 1e6:.2f} MB ({fmt_size(size)})")
            new_abits = abits - (size - target_bytes) * 8 / duration * 1.1
            if new_abits < floor:
                log("can't lower the audio bitrate any further.")
                break
            abits = new_abits
        if size > target_bytes:
            log(f"warning: output is over the {fmt_size(target_bytes)} target.")
            commit(size)
            sys.exit(2)
        commit(size)
        log(f"\ndone: {final}  {fmt_size(size)}")
        return

    movflags = ["-movflags", "+faststart"] if output.suffix.lower() in (".mp4", ".m4v", ".mov") else []
    final_size = None

    with tempfile.TemporaryDirectory() as tmpdir:
        passlog = str(Path(tmpdir) / "tailor2pass")

        for attempt in range(args.retries + 1):
            plan = choose_plan(v, vbits, codec, args)
            if attempt:
                log(f"\nretry {attempt}/{args.retries}:")
            describe(vbits, plan)

            vf = video_filter(v, plan)
            base = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-stats",
                    "-i", str(args.input), "-map", "0:v:0"]
            if audio_present:
                base += ["-map", "0:a:0"]
            base += ["-sn", "-dn"]
            if vf:
                base += ["-vf", vf]

            pass1 = base + video_args(codec, preset, vbits, 1) + [
                "-pass", "1", "-passlogfile", passlog,
                "-an", "-f", "null", "/dev/null" if sys.platform != "win32" else "NUL",
            ]
            pass2 = base + video_args(codec, preset, vbits, 2) + [
                "-pass", "2", "-passlogfile", passlog]
            pass2 += audio_args(acodec, abits) if audio_present else ["-an"]
            pass2 += movflags + [str(output)]

            run(pass1)
            run(pass2)

            final_size = output.stat().st_size
            if final_size <= target_bytes:
                break

            excess_bps = (final_size - target_bytes) * 8 / duration
            log(f"\nover target by {(final_size - target_bytes) / 1e6:.2f} MB "
                f"({fmt_size(final_size)})")
            new_vbits = vbits - excess_bps * 1.1
            if new_vbits < min_vbits:
                log("can't lower the video bitrate any further.")
                break
            vbits = new_vbits

    if final_size > target_bytes:
        log(f"\nwarning: output is {fmt_size(final_size)}, over the {fmt_size(target_bytes)} "
            f"target. Try a lower --max-res, --no-audio, or a smaller --audio-bitrate.")
        commit(final_size)
        sys.exit(2)
    commit(final_size)
    log(f"\ndone: {final}  {fmt_size(final_size)}")


if __name__ == "__main__":
    main()
