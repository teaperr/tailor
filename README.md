# tailor

CLI tool that converts any media file to a target file size using ffmpeg
two-pass bitrate encoding.

```
tailor input.mkv 25M -o clip.mp4
tailor input.mp4 8M --codec h265 --audio-bitrate 96k
tailor input.mov 50M --dry-run
```

It works out the bitrate budget from the target size and the clip's
duration, reserves some of that for audio, then runs a standard ffmpeg
two-pass encode with the remainder as the video bitrate. A 2% safety
margin (`--tolerance`) is subtracted by default to leave room for
container/muxing overhead.

## Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `<input>_<size>.<ext>` | output path |
| `--codec` | `h264` | `h264`, `h265`, `vp9`, `av1` |
| `--preset` | `medium` | ffmpeg encoder preset |
| `--audio-bitrate` | `128k` | audio bitrate (ignored with `--no-audio`) |
| `--no-audio` | off | strip audio entirely |
| `--tolerance` | `2.0` | percent safety margin subtracted from target |
| `--min-video-bitrate` | `100k` | floor for computed video bitrate |
| `--dry-run` | off | print the bitrate plan without encoding |
| `--keep-logs` | off | keep the ffmpeg two-pass log files |

Sizes accept `K`/`M`/`G` suffixes (binary, 1024-based, e.g. `25M` = 25 MiB).

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
