#!/usr/bin/env python3
"""
Run multiple devices in parallel by launching one Phone Agent process per device.

This repo's runtime model is: 1 device == 1 process. Parallelism is achieved by
running multiple processes, each with a different --device-id.

Examples:
  # Same task on 3 devices (comma-separated), max 2 in parallel
  python scripts/run_multi_devices.py --devices "A,B,C" --task "打开美团搜索附近的火锅店" --max-parallel 2

  # Devices from file (one per line) + per-device tasks from JSON mapping
  python scripts/run_multi_devices.py --devices-file devices.txt --tasks-json tasks.json

Notes:
  - Inherit model config from env: PHONE_AGENT_BASE_URL / PHONE_AGENT_MODEL / PHONE_AGENT_API_KEY
  - Do NOT run two agents against the same device concurrently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional


def _parse_devices(devices: Optional[str], devices_file: Optional[str]) -> List[str]:
    if devices and devices_file:
        raise ValueError("Provide only one of --devices or --devices-file")
    if devices:
        return [d.strip() for d in devices.split(",") if d.strip()]
    if devices_file:
        lines = Path(devices_file).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    raise ValueError("You must provide --devices or --devices-file")


def _parse_tasks(task: Optional[str], tasks_json: Optional[str]) -> Dict[str, str]:
    if task and tasks_json:
        raise ValueError("Provide only one of --task or --tasks-json")
    if task:
        return {"*": task}
    if tasks_json:
        raw = json.loads(Path(tasks_json).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("--tasks-json must be a JSON object: {\"device_id\": \"task\", ...}")
        return {str(k): str(v) for k, v in raw.items()}
    raise ValueError("You must provide --task or --tasks-json")


async def _pipe_lines(stream: asyncio.StreamReader, prefix: str, is_stderr: bool = False) -> None:
    """
    Stream subprocess output line-by-line to our stdout with a device prefix.
    This makes concurrency visible and avoids "looks serial" buffering artifacts.
    """
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:
            text = str(line).rstrip("\n")
        # Keep everything on stdout for stable ordering; mark stderr explicitly.
        tag = "ERR" if is_stderr else "OUT"
        print(f"[{prefix}][{tag}] {text}", flush=True)


async def _run_one(device_id: str, task: str, sem: asyncio.Semaphore, extra_args: List[str]) -> int:
    async with sem:
        # -u: unbuffered stdout/stderr so concurrent runs are visible in real time.
        cmd = [sys.executable, "-u", "main.py", "-d", device_id, *extra_args, task]
        started = time.time()
        print(f"[{device_id}][SYS] starting: {' '.join(cmd)}", flush=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream output concurrently until process exits.
        await asyncio.gather(
            _pipe_lines(proc.stdout, device_id, is_stderr=False),
            _pipe_lines(proc.stderr, device_id, is_stderr=True),
        )
        code = await proc.wait()
        dur = time.time() - started
        print(f"[{device_id}][SYS] exit={code} duration={dur:.1f}s", flush=True)
        return int(code)


async def _amain(args: argparse.Namespace) -> int:
    devices = _parse_devices(args.devices, args.devices_file)
    tasks_map = _parse_tasks(args.task, args.tasks_json)

    # Basic validation
    if len(set(devices)) != len(devices):
        raise ValueError("Duplicate device IDs detected; each device should appear only once.")

    # Build extra args passed to main.py
    extra_args: List[str] = []
    if args.base_url:
        extra_args += ["--base-url", args.base_url]
    if args.model:
        extra_args += ["--model", args.model]
    if args.adb_delay is not None:
        extra_args += ["--adb-delay", str(args.adb_delay)]
    if args.screenshot_timeout is not None:
        extra_args += ["--screenshot-timeout", str(args.screenshot_timeout)]
    if args.quiet:
        extra_args += ["--quiet"]
    if args.lang:
        extra_args += ["--lang", args.lang]

    sem = asyncio.Semaphore(max(1, int(args.max_parallel)))
    tasks = []
    for d in devices:
        t = tasks_map.get(d) or tasks_map.get("*")
        if not t:
            raise ValueError(f"No task specified for device '{d}' (and no '*' default task).")
        tasks.append(_run_one(d, t, sem, extra_args))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    exit_codes: List[int] = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[ERROR] {r}", file=sys.stderr)
            exit_codes.append(1)
        else:
            exit_codes.append(int(r))

    # Non-zero if any failed
    return 0 if all(c == 0 for c in exit_codes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run multiple Phone Agent processes in parallel (one per device)."
    )
    parser.add_argument("--devices", type=str, default=None, help="Comma-separated device IDs")
    parser.add_argument("--devices-file", type=str, default=None, help="File with one device ID per line")

    parser.add_argument("--task", type=str, default=None, help="Same task for all devices")
    parser.add_argument(
        "--tasks-json",
        type=str,
        default=None,
        help='JSON file mapping device_id->task, e.g. {"id1":"task1","id2":"task2","*":"default"}',
    )

    parser.add_argument("--max-parallel", type=int, default=2, help="Max concurrent processes")

    # Optional overrides (otherwise inherit PHONE_AGENT_* env vars via main.py defaults)
    parser.add_argument("--base-url", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--adb-delay", type=float, default=None)
    parser.add_argument("--screenshot-timeout", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--lang", choices=["cn", "en"], default=None)

    args = parser.parse_args()

    # Run from repo root regardless of current working dir
    repo_root = Path(__file__).resolve().parents[1]
    os.chdir(repo_root)

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


