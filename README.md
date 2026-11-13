# tailor

Shrink any media file to a target size with the best quality that fits,
in the spirit of 8mb.video.

```
tailor input.mp4 8M              # replaces input.mp4 with a version under 8 MB
tailor input.mkv 25M -o clip.mp4 # writes a separate file, keeps the original
tailor input.mp4 8M --codec h265
tailor input.mov 50M --max-res 720 --dry-run
tailor podcast.wav 5M            # .wav can't be replaced in place -> podcast_5M.opus
```

## In-place replacement

Without `-o`, tailor **replaces the input file** with the compressed version.
The encode is written to a hidden temp file next to the input
(`.input.tailor-tmp.mp4`) and only swapped in once it's under the target, so
the original is never touched until the new file is ready:

- If the encode fails or can't get under the target, the original is left
  as it was and the temp file is deleted (exit status 2 for "too big").
- If the file is already under the target, nothing happens.
- File permissions are kept.
- Works for `.mp4 .m4v .mov .mkv .webm` video and `.mp3 .m4a .opus .ogg .mka`
  audio. Other formats (`.wav`, `.avi`, …) can't hold the re-encode under the
  same name, so tailor writes `<name>_<size>.<ext>` beside them instead.
- A `.webm` input uses VP9 automatically; everything else defaults to H.264.

Use `-o` whenever you want to keep the original.

## How it works

1. Works out the total bitrate the target size allows for the clip's
   duration, minus a small safety margin for container overhead.
2. Splits it between audio and video. Audio is automatic by default
   (about 12% of the budget, clamped to 40–128 kbps, mono when very low).
3. Picks the resolution and frame rate that the video bitrate can
   actually support. Each codec has a minimum "bits per pixel per frame"
   it needs to look decent; if the source is too big for the budget,
   tailor steps the picture down (1080 → 720 → 540 → 480 → 360 → …) and
   only drops below 30 fps once the picture is already small. This is
   what keeps small files sharp instead of blocky.
4. Runs a two-pass encode with tuned settings (slow preset and adaptive
   quantisation for x264, `+faststart` for mp4, Opus for webm).
5. If the file still lands over the target, it lowers the bitrate by the
   measured excess, re-plans, and encodes again (`--retries`, default 2).

Use `--dry-run` to see the plan (output resolution, fps, bitrates) without
encoding anything.

## Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | replace the input | write here instead, keeping the original |
| `--codec` | `h264` (`vp9` for `.webm`) | `h264`, `h265`, `vp9`, `av1` |
| `--preset` | `slow` (h264), `medium` (h265) | x264/x265 preset; ignored for vp9/av1 |
| `--audio-bitrate` | `auto` | e.g. `96k`; `auto` scales with the budget |
| `--no-audio` | off | strip audio entirely |
| `-r, --max-res` | none | cap the short side, e.g. `720` for 720p (also works for vertical video) |
| `--fps` | source | force an output frame rate |
| `--no-scale` | off | keep source resolution/fps and let quality drop instead |
| `--min-bpp` | per codec | bits/pixel/frame needed before downscaling stops; raise for sharper but smaller pictures |
| `--tolerance` | `2.0` | percent of the budget held back for container overhead |
| `--min-video-bitrate` | `100k` | floor for the video bitrate |
| `--retries` | `2` | re-encodes allowed if the result is over the target |
| `--dry-run` | off | print the plan and exit |

Sizes: `8M` / `8MB` are decimal (8,000,000 bytes) and `8MiB` is binary. Decimal
is the default on purpose: a file under 8 MB is also under 8 MiB, so it fits
whichever way an upload limit is measured.

If the output can't be brought under the target, tailor exits with status 2
and says so. Try a lower `--max-res`, `--no-audio`, or a smaller
`--audio-bitrate`.

## Installing via your nix flake

This directory is itself a flake exposing a `packages.<system>.tailor`
output. Add it as an input to your own flake:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    tailor.url = "github:teaperr/tailor";
  };
}
```

### Option A — plain package, add to home.packages

```nix
{ inputs, pkgs, ... }:
{
  home.packages = [
    inputs.tailor.packages.${pkgs.system}.default
  ];
}
```

### Option B — home-manager module

The flake also exports `homeManagerModules.default`, so you can import it
and toggle it with an option instead:

In your flake's home-manager configuration:

```nix
{
  imports = [ inputs.tailor.homeManagerModules.default ];
  programs.tailor.enable = true;
}
```

Either way, run `home-manager switch` (or rebuild however you normally
apply your flake) and `tailor` will be on your `$PATH`, with `ffmpeg`
wrapped in automatically — no need to have ffmpeg installed separately.

## Notes

- `av1` (libaom-av1) two-pass encoding is slow; expect it to take much
  longer than `h264`/`h265` for the same footage.
- `vp9`/`av1` default to a `.webm` output extension, `h264`/`h265`
  default to `.mp4`, unless you pass `-o` with your own extension.
- Very short/simple clips can undershoot the target since two-pass
  bitrate control is statistical, not exact — this is normal.
- Output extension decides the container: `.webm` needs `vp9`/`av1`, and
  audio-only inputs get Opus by default (`-o out.m4a` for AAC).
