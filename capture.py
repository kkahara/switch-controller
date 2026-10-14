#!/usr/bin/env python3
"""Mac-side HDMI capture-card helper (ffmpeg + AVFoundation).

    ./capture.py list
    ./capture.py snap captures/now.jpg
    ./capture.py record --duration 330 captures/replay.mp4

The capture device defaults to the first AVFoundation video device that is not
the built-in camera, an iPhone, or a screen; override with --video / --audio
(name or index) or CAPTURE_VIDEO / CAPTURE_AUDIO.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple

NOT_A_CARD = re.compile(r"facetime|iphone|desk view|capture screen|macbook", re.I)


def list_devices() -> Tuple[List[str], List[str]]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    video: List[str] = []
    audio: List[str] = []
    bucket = video
    for line in proc.stderr.splitlines():
        if "video devices" in line:
            bucket = video
        elif "audio devices" in line:
            bucket = audio
        else:
            m = re.search(r"\]\s\[(\d+)\]\s(.+)$", line)
            if m:
                bucket.append(m.group(2).strip())
    return video, audio


def pick_card(names: List[str]) -> Optional[str]:
    for name in names:
        if not NOT_A_CARD.search(name):
            return name
    return None


def resolve(args: argparse.Namespace) -> Tuple[str, Optional[str]]:
    video, audio = list_devices()
    v = args.video or os.environ.get("CAPTURE_VIDEO") or pick_card(video)
    if not v:
        sys.exit("no capture card found; video devices: %s" % ", ".join(video))
    a = args.audio or os.environ.get("CAPTURE_AUDIO") or pick_card(audio)
    return v, a


def input_args(args: argparse.Namespace, with_audio: bool) -> List[str]:
    video, audio = resolve(args)
    spec = video + (":" + audio if with_audio and audio else ":none")
    print("capture input: %s" % spec, file=sys.stderr)
    return [
        "-f", "avfoundation",
        "-framerate", str(args.fps),
        "-video_size", args.size,
        "-pixel_format", "nv12",
        "-i", spec,
    ]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video")
    parser.add_argument("--audio")
    parser.add_argument("--fps", default="60")
    parser.add_argument("--size", default="1920x1080")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    snap = sub.add_parser("snap")
    snap.add_argument("out")
    rec = sub.add_parser("record")
    rec.add_argument("--duration", type=float, required=True, help="seconds")
    rec.add_argument("out")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        video, audio = list_devices()
        print("video:", *video, sep="\n  ")
        print("audio:", *audio, sep="\n  ")
        print("picked:", pick_card(video), "/", pick_card(audio))
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.cmd == "snap":
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        cmd += input_args(args, with_audio=False)
        cmd += ["-ss", "1", "-frames:v", "1", args.out]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
        cmd += input_args(args, with_audio=True)
        cmd += [
            "-t", str(args.duration),
            "-c:v", "h264_videotoolbox", "-b:v", "12M",
            "-c:a", "aac", "-b:a", "192k",
            args.out,
        ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
