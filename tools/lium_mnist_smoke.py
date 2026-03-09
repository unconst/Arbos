#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from lium_sdk import Config, Lium

WORKING_DIR = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _pick_ssh_target(pod: dict[str, Any]) -> tuple[str, int]:
    host = pod.get("host") or pod.get("ip") or pod.get("public_ip")
    port = pod.get("port") or pod.get("ssh_port") or pod.get("sshPort")
    ssh = pod.get("ssh") or pod.get("ssh_cmd")
    if (not host or not port) and isinstance(ssh, str) and "@" in ssh:
        if " -p " in ssh:
            # Format: ssh root@1.2.3.4 -p 40070
            after_at = ssh.split("@", 1)[1]
            host = after_at.split(" ", 1)[0]
            try:
                port = int(ssh.rsplit(" -p ", 1)[1].strip())
            except ValueError:
                pass
        else:
            # Format: root@1.2.3.4:40070
            target = ssh.split("@", 1)[1]
            if ":" in target:
                host, port_raw = target.rsplit(":", 1)
                try:
                    port = int(port_raw)
                except ValueError:
                    pass
    if not host or not port:
        raise RuntimeError(f"Could not parse SSH target from pod: {pod}")
    return str(host), int(port)


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    names = [n for n in dir(obj) if not n.startswith("_")]
    out: dict[str, Any] = {}
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if callable(v):
            continue
        out[n] = v
    return out


def _select_executor(
    executors: list[dict[str, Any]],
    *,
    executor_id: str | None,
    gpu_type: str | None,
) -> dict[str, Any]:
    if executor_id:
        hit = next((e for e in executors if str(e.get("id")) == executor_id), None)
        if hit is None:
            raise RuntimeError(f"executor_id '{executor_id}' not found in ls() results")
        return hit
    if gpu_type:
        hit = next(
            (
                e
                for e in executors
                if str(e.get("gpu_type", "")).lower() == gpu_type.lower()
                or str(e.get("machine_name", "")).lower() == gpu_type.lower()
            ),
            None,
        )
        if hit is None:
            raise RuntimeError(f"gpu_type '{gpu_type}' not found in ls() results")
        return hit
    if not executors:
        raise RuntimeError("No executors available from ls()")
    return executors[0]


def _find_pod(li: Lium, pod_name: str) -> dict[str, Any] | None:
    pods = [_obj_to_dict(x) for x in li.ps()]
    return next((p for p in pods if p.get("name") == pod_name), None)


def _wait_for_pod(li: Lium, pod_name: str, timeout_s: int, poll_s: int) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pod = _find_pod(li, pod_name)
        if pod is not None:
            try:
                _pick_ssh_target(pod)
                return pod
            except Exception:
                pass
        time.sleep(poll_s)
    raise RuntimeError(f"Timed out waiting for pod '{pod_name}' to become SSH-ready")


def _remote_python(host: str, port: int, script_text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(script_text)
        local_file = Path(tf.name)
    remote_file = f"/tmp/mnist_smoke_{os.getpid()}.py"
    try:
        _run(["scp", "-P", str(port), "-o", "StrictHostKeyChecking=no", str(local_file), f"root@{host}:{remote_file}"])
        result = _run(["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no", f"root@{host}", f"python {shlex.quote(remote_file)}"])
        return result.stdout
    finally:
        _run(
            ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no", f"root@{host}", f"rm -f {shlex.quote(remote_file)}"],
            check=False,
        )
        local_file.unlink(missing_ok=True)


def _mnist_code(train_samples: int, test_samples: int, epochs: int) -> str:
    return f"""import json
import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import datasets, transforms

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
transform = transforms.ToTensor()
train_ds = datasets.MNIST("/tmp/mnist", train=True, download=True, transform=transform)
test_ds = datasets.MNIST("/tmp/mnist", train=False, download=True, transform=transform)
train_ds.data = train_ds.data[:{train_samples}]
train_ds.targets = train_ds.targets[:{train_samples}]
test_ds.data = test_ds.data[:{test_samples}]
test_ds.targets = test_ds.targets[:{test_samples}]
train_loader = torch.utils.data.DataLoader(train_ds, batch_size=128, shuffle=True)
test_loader = torch.utils.data.DataLoader(test_ds, batch_size=256)

model = nn.Sequential(
    nn.Flatten(),
    nn.Linear(28 * 28, 256),
    nn.ReLU(),
    nn.Linear(256, 10),
).to(device)
loss_fn = nn.CrossEntropyLoss()
opt = optim.Adam(model.parameters(), lr=1e-3)

losses = []
for _ in range({epochs}):
    model.train()
    running_loss = 0.0
    count = 0
    for x, y in train_loader:
        x, y = x.to(device), y.to(device)
        opt.zero_grad()
        logits = model(x)
        loss = loss_fn(logits, y)
        loss.backward()
        opt.step()
        running_loss += loss.item() * x.size(0)
        count += x.size(0)
    losses.append(running_loss / max(count, 1))

model.eval()
correct = 0
total = 0
with torch.no_grad():
    for x, y in test_loader:
        x, y = x.to(device), y.to(device)
        pred = model(x).argmax(dim=1)
        correct += (pred == y).sum().item()
        total += y.size(0)

print(json.dumps({{
    "device": str(device),
    "epoch_losses": losses,
    "test_accuracy": correct / max(total, 1),
}}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LIUM MNIST smoke test over SSH.")
    parser.add_argument("--pod-name", default="arbos-agent")
    parser.add_argument("--create-if-missing", action="store_true")
    parser.add_argument("--executor-id")
    parser.add_argument("--gpu-type")
    parser.add_argument("--wait-timeout-s", type=int, default=240)
    parser.add_argument("--wait-poll-s", type=int, default=5)
    parser.add_argument("--down-after", action="store_true")
    parser.add_argument("--train-samples", type=int, default=10000)
    parser.add_argument("--test-samples", type=int, default=2000)
    parser.add_argument("--epochs", type=int, default=2)
    args = parser.parse_args()

    load_dotenv(WORKING_DIR / ".env")
    api_key = os.getenv("LIUM_KEY")
    if not api_key:
        print("LIUM_KEY not set", file=sys.stderr)
        return 1

    li = Lium(config=Config(api_key=api_key))
    executors = [_obj_to_dict(x) for x in li.ls()]
    print(f"executors={len(executors)}")

    created_this_run = False
    pod = _find_pod(li, args.pod_name)
    if pod is None:
        if not args.create_if_missing:
            print(f"pod '{args.pod_name}' not found", file=sys.stderr)
            return 1
        chosen = _select_executor(executors, executor_id=args.executor_id, gpu_type=args.gpu_type)
        chosen_id = str(chosen.get("id"))
        print(f"creating pod={args.pod_name} on executor_id={chosen_id}")
        li.up(executor_id=chosen_id, pod_name=args.pod_name)
        created_this_run = True
        pod = _wait_for_pod(li, args.pod_name, args.wait_timeout_s, args.wait_poll_s)

    host, port = _pick_ssh_target(pod)
    executor_info = _obj_to_dict(pod.get("executor")) if pod.get("executor") is not None else {}
    executor_label = executor_info.get("huid") or executor_info.get("id")
    gpu_label = executor_info.get("gpu_type") or executor_info.get("machine_name")
    print(f"pod={pod.get('name')} executor={executor_label} gpu={gpu_label} ssh=root@{host}:{port}")
    result = _remote_python(host, port, _mnist_code(args.train_samples, args.test_samples, args.epochs))
    metrics = json.loads(result.strip().splitlines()[-1])

    payload = {
        "pod": pod.get("name"),
        "executor": executor_info.get("huid") or executor_info.get("id"),
        "gpu": executor_info.get("gpu_type") or executor_info.get("machine_name"),
        "ssh": f"root@{host}:{port}",
        "epoch_losses": metrics.get("epoch_losses"),
        "test_accuracy": metrics.get("test_accuracy"),
        "device": metrics.get("device"),
    }
    print("RESULT_JSON")
    print(json.dumps(payload))
    if args.down_after and (created_this_run or args.create_if_missing):
        print(f"tearing_down pod={args.pod_name}")
        li.down(args.pod_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
