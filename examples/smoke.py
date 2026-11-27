"""pywgc 冒烟示例：列窗口 → 抓一帧 → 打印健康度 → 可选存图。

用法::

    python examples/smoke.py --list
    python examples/smoke.py --title 记事本
    python examples/smoke.py --title 记事本 --backend gdi --out frame.png
    python examples/smoke.py --title 记事本 --fps 5 --seconds 3

不需要 OpenCV 也能跑；只有 ``--out`` 存图时才需要（``pip install pywgc[fast]``）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pywgc smoke test")
    ap.add_argument("--list", action="store_true", help="只列出可见窗口然后退出")
    ap.add_argument("--title", help="窗口标题前缀")
    ap.add_argument("--hwnd", type=lambda s: int(s, 0), help="窗口句柄（支持 0x 前缀）")
    ap.add_argument("--region", help="客户区 ROI，格式 x,y,w,h")
    ap.add_argument("--backend", default="auto", choices=["auto", "wgc", "gdi"])
    ap.add_argument("--out", help="把一帧存成 png（需要 opencv）")
    ap.add_argument("--fps", type=float, default=0.0, help="循环抓屏的帧率，0 = 只抓一帧")
    ap.add_argument("--seconds", type=float, default=3.0, help="配合 --fps 的持续时间")
    ap.add_argument("-v", "--verbose", action="store_true", help="打开 INFO 日志")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from pywgc import WindowGrabber, available_backends, list_windows

    print("available backends:", available_backends())

    if args.list:
        for w in sorted(list_windows(), key=lambda d: -(d["w"] * d["h"]))[:25]:
            print(
                f'0x{w["hwnd"]:08X}  {w["w"]:5d}x{w["h"]:<5d} '
                f'@({w["x"]},{w["y"]})  {"vis" if w["visible"] else "hid"}  {w["title"]!r}'
            )
        return 0

    if not args.title and args.hwnd is None:
        ap.error("需要 --title 或 --hwnd（先用 --list 看看有哪些窗口）")

    region = tuple(int(v) for v in args.region.split(",")) if args.region else None
    if region is not None and len(region) != 4:
        ap.error("--region 需要 4 个数字: x,y,w,h")

    with WindowGrabber(
        title=args.title, hwnd=args.hwnd, region=region, backend=args.backend
    ) as g:
        print(f"backend = {g.backend}, hwnd = 0x{g.hwnd:X}, roi = {g.roi}")

        frame = g.grab()
        print(f"frame: shape={frame.shape} dtype={frame.dtype} mean={frame.mean(axis=(0, 1)).round(1)}")
        print(f"stats: {g.stats}")

        if args.out:
            try:
                import cv2
            except Exception:  # noqa: BLE001
                print("存图需要 opencv: pip install pywgc[fast]", file=sys.stderr)
                return 2
            cv2.imwrite(args.out, frame)
            print(f"saved -> {args.out}")

        if args.fps > 0:
            period = 1.0 / args.fps
            t_end = time.time() + args.seconds
            n = 0
            worst = 0.0
            while time.time() < t_end:
                t0 = time.perf_counter()
                g.grab()
                worst = max(worst, time.perf_counter() - t0)
                n += 1
                rest = period - (time.perf_counter() - t0)
                if rest > 0:
                    time.sleep(rest)
            print(f"looped {n} frames in {args.seconds:.1f}s, worst grab {worst * 1000:.2f} ms")
            print(f"stats: {g.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
