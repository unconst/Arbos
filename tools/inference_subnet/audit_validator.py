#!/usr/bin/env python3
"""
Audit Validator — Async miner auditor that reads R2 logs and verifies hidden states.

This is the verification half of the validator split. It:
1. Reads inference logs from R2 (written by proxy_gateway.py)
2. Randomly samples recent requests for auditing
3. Requests hidden state proofs from miners for those requests
4. Verifies proofs against its own model's hidden states
5. Scores miners based on audit results + speed metrics
6. Sets weights on chain via commit-reveal

It does NOT:
- Serve inference requests (that's proxy_gateway.py)
- Interact with end users

Architecture:
  R2 bucket ← proxy_gateway writes audit records
  audit_validator reads R2 → picks random records → challenges miners
    → computes reference hidden states (GPU) → scores → set_weights on chain
"""

import argparse
import asyncio
import json
import logging
import os
import math
import secrets
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
import numpy as np

from hardened_scoring import (
    HardenedScoringEngine, RequestScore, ChallengeResult,
    cosine_similarity, compute_speed_score, compute_output_quality,
    compute_verification_score,
    COSINE_THRESHOLD, CHALLENGE_TIMEOUT_MS, CHALLENGE_TIMEOUT_HARD_MS,
    REINFERENCE_THRESHOLD_MS,
)
from challenge_engine import ChallengeEngine
from r2_publisher import R2Publisher, AuditRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("audit_validator")

VALIDATOR_VERSION = "1.0.0"


# ── Chain Weight Setter ──────────────────────────────────────────────────────

class ChainWeightSetter:
    """Sets weights on chain via subprocess to avoid blocking asyncio."""

    def __init__(self, wallet_name: str, hotkey: str, netuid: int, network: str, wallet_path: str = None):
        self.wallet_name = wallet_name
        self.hotkey = hotkey
        self.netuid = netuid
        self.network = network
        self.wallet_path = wallet_path or os.path.expanduser("~/.bittensor/wallets")
        self.last_set_time = 0.0
        self.total_sets = 0
        self.total_failures = 0

    async def set_weights(self, weights: dict[int, float], retries: int = 3) -> bool:
        if not weights:
            log.warning("[CHAIN] No weights to set")
            return False

        uids = list(weights.keys())
        weight_values = [weights[uid] for uid in uids]

        script = f"""
import sys
import numpy as np
try:
    import bittensor as bt
    wallet = bt.Wallet(name="{self.wallet_name}", hotkey="{self.hotkey}", path="{self.wallet_path}")
    sub = bt.Subtensor(network="{self.network}")
    response = sub.set_weights(
        wallet=wallet,
        netuid={self.netuid},
        uids=np.array({uids}, dtype=np.int64),
        weights=np.array({weight_values}, dtype=np.float32),
        wait_for_finalization=True,
        period=None,
    )
    success = response[0] if isinstance(response, tuple) else bool(response)
    if success:
        print(f"OK:{{response}}")
    else:
        msg = response[1] if isinstance(response, tuple) and len(response) > 1 else str(response)
        print(f"RATE_LIMIT:{{msg}}", file=sys.stderr)
        sys.exit(2)
except Exception as e:
    print(f"ERR:{{e}}", file=sys.stderr)
    sys.exit(1)
"""
        for attempt in range(1 + retries):
            if attempt > 0:
                log.info(f"[CHAIN] Retry {attempt}/{retries}...")
                await asyncio.sleep(5)

            log.info(f"[CHAIN] Setting weights for {len(uids)} miners: {dict(zip(uids, [f'{w:.4f}' for w in weight_values]))}")

            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    log.info(f"[CHAIN] Weights set: {stdout.decode().strip()}")
                    self.last_set_time = time.time()
                    self.total_sets += 1
                    return True
                else:
                    err_msg = stderr.decode().strip()
                    log.error(f"[CHAIN] Failed: {err_msg}")
                    self.total_failures += 1
                    # Don't retry rate limits — cooldown is ~20min, retrying is futile
                    if "RATE_LIMIT" in err_msg or "too soon" in err_msg.lower():
                        log.warning("[CHAIN] Rate-limited — skipping retries, will try next epoch")
                        return False
            except asyncio.TimeoutError:
                log.error(f"[CHAIN] Timeout (120s), attempt {attempt + 1}")
                self.total_failures += 1
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception as e:
                log.error(f"[CHAIN] Error: {e}")
                self.total_failures += 1

        return False


# ── Metagraph Discovery ──────────────────────────────────────────────────────

class MetagraphDiscovery:
    """Discovers miners from the Bittensor metagraph."""

    def __init__(self, netuid: int, network: str, validator_hotkey: str = None):
        self.netuid = netuid
        self.network = network
        self.validator_hotkey = validator_hotkey
        self.last_sync = 0.0
        self.sync_interval = 120

    async def discover_miners(self) -> list[dict]:
        script = f"""
import json, sys
try:
    import bittensor as bt
    import numpy as np
    sub = bt.Subtensor(network="{self.network}")
    meta = bt.Metagraph(netuid={self.netuid}, network=sub.network, sync=True)
    dividends = np.array([float(d) for d in meta.dividends])
    miners = []
    for uid in range(meta.n):
        if dividends[uid] > 0:
            continue
        hotkey = str(meta.hotkeys[uid])
        if hotkey == "{self.validator_hotkey or ''}":
            continue
        axon = meta.axons[uid]
        ip = str(getattr(axon, 'ip', ''))
        port = int(getattr(axon, 'port', 0))
        if ip and port and ip != '0.0.0.0':
            miners.append({{"uid": int(uid), "endpoint": f"http://{{ip}}:{{port}}", "hotkey": hotkey}})
    print(json.dumps(miners))
except Exception as e:
    print(f"ERR:{{e}}", file=sys.stderr)
    sys.exit(1)
"""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0:
                miners = json.loads(stdout.decode().strip())
                self.last_sync = time.time()
                log.info(f"[METAGRAPH] Discovered {len(miners)} miners")
                return miners
            else:
                log.error(f"[METAGRAPH] Failed: {stderr.decode().strip()}")
                return []
        except asyncio.TimeoutError:
            log.error(f"[METAGRAPH] Discovery timed out (60s)")
            try:
                proc.kill()
            except Exception:
                pass
        except Exception as e:
            log.error(f"[METAGRAPH] Error: {e}")
            return []


# ── Real Validator Model ─────────────────────────────────────────────────────

class RealValidatorModel:
    """HuggingFace model for hidden state verification."""

    def __init__(self, model_name: str, device: str = "auto"):
        import threading
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._lock = threading.Lock()
        trust_remote = os.getenv("TRUST_REMOTE_CODE", "0") == "1"
        log.info(f"Loading validator model: {model_name} (device={device})")

        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=trust_remote)

        if device == "cpu":
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name, torch_dtype=torch.float32, trust_remote_code=trust_remote,
            )
            self.model = self.model.to("cpu")
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name, torch_dtype=torch.float16, device_map="auto", trust_remote_code=trust_remote,
            )
        self.model.eval()
        self._torch = torch

        class _Config:
            name = model_name
            num_layers = self.model.config.num_hidden_layers
            hidden_dim = self.model.config.hidden_size
            vocab_size = self.model.config.vocab_size
            max_seq_len = getattr(self.model.config, "max_position_embeddings", 2048)

        self.config = _Config()
        log.info(f"Model loaded: {self.config.num_layers} layers, hidden_dim={self.config.hidden_dim}")

    @property
    def _device(self):
        return next(self.model.parameters()).device

    def compute_hidden_state_at(self, tokens: list[int], layer: int, position: int) -> np.ndarray:
        """Compute hidden state at (layer, position) for verification.
        Thread-safe: serialized via lock to prevent concurrent forward pass corruption."""
        with self._lock:
            input_ids = self._torch.tensor([tokens[:position + 1]], device=self._device)
            with self._torch.no_grad():
                outputs = self.model(input_ids, output_hidden_states=True)
            vec = outputs.hidden_states[layer + 1][0][-1].cpu().float().numpy()
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    def compute_hidden_states_batch(self, tokens: list[int], points: list[tuple[int, int]]) -> dict:
        """Compute hidden states at multiple points in one forward pass.
        Thread-safe: serialized via lock to prevent concurrent forward pass corruption."""
        if not points:
            return {}
        with self._lock:
            max_pos = max(pos for _, pos in points)
            input_ids = self._torch.tensor([tokens[:max_pos + 1]], device=self._device)
            with self._torch.no_grad():
                outputs = self.model(input_ids, output_hidden_states=True)
            result = {}
            for layer, pos in points:
                vec = outputs.hidden_states[layer + 1][0][pos].cpu().float().numpy()
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                result[(layer, pos)] = vec
        return result


# ── R2 Reader ────────────────────────────────────────────────────────────────

class R2AuditReader:
    """Reads audit records from R2 or local filesystem."""

    def __init__(
        self,
        bucket_name: str = "affine",
        endpoint_url: str = None,
        access_key: str = None,
        secret_key: str = None,
        local_dir: str = None,
    ):
        self.bucket_name = bucket_name
        self.local_dir = local_dir
        self._client = None

        if endpoint_url and access_key and secret_key:
            try:
                import boto3
                self._client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
                self._mode = "r2"
                log.info(f"R2AuditReader: R2 mode -> {bucket_name}")
            except ImportError:
                log.warning("boto3 not installed, using local mode")
                self._mode = "local"
        else:
            self._mode = "local"
            log.info(f"R2AuditReader: local mode -> {local_dir or '/tmp/r2-audit'}")

    def list_recent_records(self, hours: int = 1, max_records: int = 500) -> list[dict]:
        """List recent audit records from the last N hours."""
        if self._mode == "r2":
            return self._list_r2_records(hours, max_records)
        else:
            return self._list_local_records(hours, max_records)

    def _list_r2_records(self, hours: int, max_records: int) -> list[dict]:
        records = []
        now = datetime.now(timezone.utc)
        for h in range(hours):
            ts = now - timedelta(hours=h)
            prefix = f"audit/{ts.strftime('%Y/%m/%d/%H')}/"
            try:
                response = self._client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=prefix, MaxKeys=max_records,
                )
                for obj in response.get("Contents", []):
                    try:
                        data = self._client.get_object(Bucket=self.bucket_name, Key=obj["Key"])
                        record = json.loads(data["Body"].read().decode())
                        records.append(record)
                    except Exception as e:
                        log.debug(f"Failed to read {obj['Key']}: {e}")
            except Exception as e:
                log.error(f"R2 list failed for {prefix}: {e}")
        return records[:max_records]

    def _list_local_records(self, hours: int, max_records: int) -> list[dict]:
        records = []
        base = Path(self.local_dir or "/tmp/r2-audit")
        now = datetime.now(timezone.utc)
        for h in range(hours):
            ts = now - timedelta(hours=h)
            day_dir = base / ts.strftime("%Y-%m-%d")
            hour_file = day_dir / f"hour-{ts.strftime('%H')}.jsonl"
            if hour_file.exists():
                try:
                    with open(hour_file) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    records.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                except Exception as e:
                    log.error(f"Failed to read {hour_file}: {e}")
        return records[:max_records]


# ── Audit Validator ──────────────────────────────────────────────────────────

@dataclass
class MinerEndpoint:
    uid: int
    endpoint: str
    hotkey: str = ""


# ── RTT Baseline Tracker ─────────────────────────────────────────────────────

class RTTTracker:
    """
    Tracks network round-trip time (RTT) baseline per miner.

    Periodically pings miner /health endpoints and maintains a rolling median
    RTT. This baseline is subtracted from challenge response times to estimate
    the actual server-side extraction time, making the timing defense meaningful
    even across WAN connections.

    Defense model:
    - VRAM cache lookup: <5ms server-side
    - Re-running forward pass: 2-10s for Qwen 7B
    - Network RTT: 10-120ms depending on datacenter
    - Without RTT correction, a 50ms threshold is meaningless over WAN
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        # uid -> deque of RTT measurements in ms
        self._rtts: dict[int, deque] = defaultdict(lambda: deque(maxlen=window_size))

    def record(self, uid: int, rtt_ms: float):
        """Record a single RTT measurement for a miner."""
        if rtt_ms > 0:
            self._rtts[uid].append(rtt_ms)

    def get_baseline(self, uid: int) -> float:
        """Get 10th-percentile RTT baseline for a miner. Returns 0 if no data.

        Uses p10 instead of median to resist RTT inflation attacks: a miner
        can add artificial latency to health pings but cannot make them faster
        than the true network RTT. The fastest pings reveal the real baseline.
        """
        measurements = self._rtts.get(uid)
        if not measurements or len(measurements) < 3:
            return 0.0  # Not enough data — don't subtract anything
        return float(np.percentile(list(measurements), 10))

    def get_net_extraction_time(self, uid: int, total_latency_ms: float) -> float:
        """
        Estimate server-side extraction time = total_latency - RTT_baseline.

        Returns max(0, ...) to avoid negative values from RTT variance.
        If no baseline available, returns total_latency (conservative).
        """
        baseline = self.get_baseline(uid)
        if baseline <= 0:
            return total_latency_ms
        return max(0.0, total_latency_ms - baseline)

    def summary(self) -> dict:
        """Return RTT baselines for all known miners."""
        return {uid: self.get_baseline(uid) for uid in self._rtts}


class AuditValidator:
    """
    Reads R2 audit logs and performs async hidden state verification.
    Scores miners and sets weights on chain.
    """

    def __init__(
        self,
        model,
        chain: ChainWeightSetter = None,
        discovery: MetagraphDiscovery = None,
        r2_reader: R2AuditReader = None,
        audit_rate: float = 0.3,
        audit_interval_s: float = 15.0,
        epoch_length_s: float = 300.0,
    ):
        self.model = model
        self.chain = chain
        self.discovery = discovery
        self.r2_reader = r2_reader

        # Audit configuration
        self.audit_rate = audit_rate  # Fraction of records to audit
        self.audit_interval_s = audit_interval_s
        self.epoch_length_s = epoch_length_s

        # Scoring
        self.scoring = HardenedScoringEngine(epoch_length_s=epoch_length_s)
        self.challenge_engine = ChallengeEngine(
            cosine_threshold=COSINE_THRESHOLD,
            timing_threshold_ms=CHALLENGE_TIMEOUT_MS,
            timing_hard_cutoff_ms=CHALLENGE_TIMEOUT_HARD_MS,
        )

        # RTT baseline tracker for timing defense
        self.rtt_tracker = RTTTracker()

        # R2 publisher for audit results
        r2_endpoint = os.environ.get("R2_URL", "").rstrip("/")
        r2_access = os.environ.get("R2_ACCESS_KEY_ID", "")
        r2_secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
        r2_bucket = os.environ.get("R2_BUCKET", "affine")
        if r2_endpoint and r2_access and r2_secret:
            self.r2_publisher = R2Publisher(
                bucket_name=r2_bucket,
                endpoint_url=r2_endpoint,
                access_key=r2_access,
                secret_key=r2_secret,
                local_dir="/tmp/r2-audit-validator",
            )
        else:
            self.r2_publisher = R2Publisher(local_dir="/tmp/r2-audit-validator")

        # Known miners (populated by discovery)
        self.miners: dict[int, MinerEndpoint] = {}

        # Track consecutive connection failures per miner to skip dead ones in synthetic loop
        self._synth_consec_fails: dict[int, int] = defaultdict(int)
        _SYNTH_FAIL_THRESHOLD = 3  # Skip after 3 consecutive failures
        _SYNTH_FAIL_RETRY_INTERVAL = 300  # Re-try dead miners every 5 minutes
        self._synth_fail_threshold = _SYNTH_FAIL_THRESHOLD
        self._synth_fail_retry = _SYNTH_FAIL_RETRY_INTERVAL
        self._synth_last_retry: dict[int, float] = defaultdict(float)

        # Stats
        self.total_audits = 0
        self.total_passed = 0
        self.total_failed = 0
        self.audited_request_ids: set[str] = set()  # Avoid re-auditing
        self._max_audited_ids = 10000
        self.epoch_summaries: list[dict] = []

        # Cache miss tracking per miner (cross-epoch) — catches miners that
        # skip inline commitments to dodge challenges. Only counts inline
        # commitment misses (requested but not returned), NOT deferred challenge
        # 404s which are normal cache eviction behavior.
        self._cache_miss_counts: dict[int, int] = defaultdict(int)  # uid -> inline misses
        self._cache_challenge_counts: dict[int, int] = defaultdict(int)  # uid -> inline total
        self.CACHE_MISS_PENALTY_THRESHOLD = 0.30  # 30% miss rate triggers penalty
        self.CACHE_MISS_MAX_PENALTY = 0.50  # up to 50% weight reduction
        # Separate diagnostic counters for deferred challenge 404s (no penalty)
        self._deferred_eviction_counts: dict[int, int] = defaultdict(int)  # uid -> 404 count
        self._deferred_challenge_counts: dict[int, int] = defaultdict(int)  # uid -> total deferred

        # Inline commitment verification stats
        self.total_commitment_checks = 0
        self.total_commitment_passes = 0
        self.total_commitment_fails = 0

        # Shared HTTP session
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Gateway relay for source-IP masking (synthetic probes route through gateway)
        self.gateway_relay_url = os.environ.get("GATEWAY_RELAY_URL", "")  # e.g. http://localhost:8080
        self.gateway_relay_secret = os.environ.get("INTERNAL_RELAY_SECRET", "")

        # Deferred challenge queue — breaks timing correlation between inference
        # and challenge (Vector 29). Instead of challenging immediately after
        # inference, we queue records and process them after a random delay
        # (10-60s), so miners can't detect "inference → challenge" timing.
        self._deferred_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
            self._http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._http_session

    async def _send_via_relay(self, uid: int, payload: dict) -> Optional[dict]:
        """Send probe through gateway relay to mask auditor source IP."""
        session = await self._get_http_session()
        url = f"{self.gateway_relay_url}/internal/relay"
        headers = {"Authorization": f"Bearer {self.gateway_relay_secret}"}
        body = {"miner_uid": uid, "payload": payload}
        try:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=65)) as resp:
                if resp.status != 200:
                    log.debug(f"[RELAY] Gateway relay returned {resp.status} for miner {uid}")
                    return None
                return json.loads(await resp.read())
        except Exception as e:
            log.debug(f"[RELAY] Gateway relay error for miner {uid}: {e}")
            return None

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def verify_commitments(self, record: dict) -> Optional[dict]:
        """
        Verify inline hidden state commitments from R2 records.

        This is the fast-path verification: the miner already committed 1+ hidden
        state vectors during inference. We just compute the reference and compare.
        No network round-trip needed — the commitment data is in the R2 record.

        Returns dict with results, or None if no commitments to verify.
        """
        commitments = record.get("commitments")
        if not commitments:
            return None

        miner_uid = record.get("miner_uid")
        request_id = record.get("request_id", "")

        # Check for commitment cherry-picking (Vector 26 + Vector 31):
        # Verify miner returned commitments for ALL requested layers, not just easy ones.
        # Missing layers are treated as FAILURES — a miner that skips layers it would fail
        # on cannot game pass rates by only returning commitments for easy layers.
        cherry_pick_detected = False
        requested_layers = record.get("requested_layers")
        if requested_layers:
            returned_layers = set(c.get("layer") for c in commitments if c.get("layer") is not None)
            missing_layers = set(requested_layers) - returned_layers
            if missing_layers:
                cherry_pick_detected = True
                log.warning(
                    f"[CHERRY-PICK] Miner {miner_uid}: missing commitments for layers {sorted(missing_layers)} "
                    f"(requested={sorted(requested_layers)}, returned={sorted(returned_layers)}) — "
                    f"COUNTED AS FAILURE | request={request_id[:8]}..."
                )
        prompt = record.get("prompt", "")
        response_text = record.get("response", "")

        if not hasattr(self.model, 'tokenizer') or not self.model.tokenizer:
            return None

        # Use miner's exact token sequence if available (prevents tokenization mismatch)
        all_tokens = record.get("all_token_ids")
        template_tamper_detected = False

        if all_tokens:
            # ANTI-CHEAT: Verify miner's prompt token prefix matches expected chat template.
            # Without this check, a miner can apply a different/no template, produce
            # different tokens, and still pass hidden state verification (cosine=1.0)
            # because the validator would use the miner's self-reported tokens.
            messages = record.get("messages")
            if messages and hasattr(self.model.tokenizer, 'apply_chat_template'):
                expected_prompt = self.model.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
                expected_prompt_tokens = self.model.tokenizer.encode(expected_prompt)
                # Compare prompt prefix of miner's tokens against expected
                miner_prefix = all_tokens[:len(expected_prompt_tokens)]
                if len(miner_prefix) < len(expected_prompt_tokens) * 0.8:
                    # Miner's sequence is suspiciously shorter than expected prompt
                    template_tamper_detected = True
                    log.warning(
                        f"[TEMPLATE-TAMPER] Miner {miner_uid}: token sequence too short — "
                        f"expected >={int(len(expected_prompt_tokens)*0.8)} prompt tokens, "
                        f"got {len(miner_prefix)} total | request={request_id[:8]}..."
                    )
                elif miner_prefix != expected_prompt_tokens:
                    # Count mismatched tokens in the prompt region
                    mismatches = sum(
                        1 for a, b in zip(miner_prefix, expected_prompt_tokens) if a != b
                    )
                    mismatch_rate = mismatches / max(len(expected_prompt_tokens), 1)
                    if mismatch_rate > 0.02:
                        # >2% prompt token mismatch = different template
                        template_tamper_detected = True
                        log.warning(
                            f"[TEMPLATE-TAMPER] Miner {miner_uid}: prompt token mismatch — "
                            f"{mismatches}/{len(expected_prompt_tokens)} tokens differ "
                            f"({mismatch_rate:.1%}) | request={request_id[:8]}..."
                        )
                    elif mismatches > 0:
                        log.info(
                            f"[TEMPLATE-CHECK] Miner {miner_uid}: minor prompt mismatch — "
                            f"{mismatches}/{len(expected_prompt_tokens)} tokens differ "
                            f"({mismatch_rate:.1%}) | request={request_id[:8]}..."
                        )
                if template_tamper_detected:
                    # Override with validator's own tokenization
                    effective_prompt = expected_prompt
                    full_text = effective_prompt + response_text
                    all_tokens = self.model.tokenizer.encode(full_text)
                    log.info(
                        f"[TEMPLATE-TAMPER] Miner {miner_uid}: using validator's own "
                        f"tokenization for reference computation"
                    )

        if not all_tokens:
            # Fallback: re-tokenize from text (may produce different tokens at boundary)
            messages = record.get("messages")
            if messages and hasattr(self.model.tokenizer, 'apply_chat_template'):
                effective_prompt = self.model.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                effective_prompt = prompt
            full_text = effective_prompt + response_text
            all_tokens = self.model.tokenizer.encode(full_text)

        # Anti-relay: verify nonce-bound commitment hashes if present.
        # The gateway computes HMAC(request_id:miner_uid) and sends it as `nonce`.
        # The miner must include hash(hidden_state || nonce) in each commitment.
        # A relay miner receives nonce_A but the upstream miner computes with nonce_B.
        record_nonce = record.get("nonce")
        nonce_check_failed = False
        if record_nonce:
            for c in commitments:
                c_hash = c.get("commitment_hash")
                c_state = c.get("hidden_state")
                if c_hash and c_state:
                    # Recompute expected hash
                    import hashlib as _hashlib
                    quantized = [round(v, 4) for v in c_state]
                    payload = json.dumps(quantized, separators=(",", ":")) + record_nonce
                    expected = _hashlib.sha256(payload.encode()).hexdigest()[:32]
                    if c_hash != expected:
                        log.warning(
                            f"[NONCE-FAIL] Miner {miner_uid}: commitment hash mismatch — "
                            f"expected={expected[:8]}... got={c_hash[:8]}... | "
                            f"POSSIBLE RELAY ATTACK on request {request_id[:8]}"
                        )
                        nonce_check_failed = True

        results = []
        any_failed = nonce_check_failed or template_tamper_detected or cherry_pick_detected

        for c in commitments:
            layer = c.get("layer")
            position = c.get("position")
            miner_state_list = c.get("hidden_state")

            if layer is None or position is None or not miner_state_list:
                continue

            miner_state = np.array(miner_state_list, dtype=np.float32)
            if miner_state.ndim != 1 or miner_state.shape[0] != self.model.config.hidden_dim:
                results.append({"layer": layer, "position": position, "passed": False, "reason": "shape_mismatch"})
                any_failed = True
                continue

            if not np.all(np.isfinite(miner_state)):
                results.append({"layer": layer, "position": position, "passed": False, "reason": "nan_inf"})
                any_failed = True
                continue

            # Skip last layer (known divergence)
            if layer >= self.model.config.num_layers - 1:
                continue

            # Compute reference hidden state
            try:
                reference = await asyncio.to_thread(
                    self.model.compute_hidden_state_at,
                    all_tokens, layer, position
                )
            except Exception as e:
                log.warning(f"[COMMIT] Reference computation failed: {e}")
                continue

            cos_sim = float(np.dot(miner_state, reference) / (
                max(np.linalg.norm(miner_state), 1e-10) * max(np.linalg.norm(reference), 1e-10)
            ))

            self.total_commitment_checks += 1

            # Use relaxed threshold for commitments (same as challenge)
            if cos_sim >= COSINE_THRESHOLD:
                self.total_commitment_passes += 1
                results.append({
                    "layer": layer, "position": position,
                    "passed": True, "cosine_sim": cos_sim,
                })
            elif abs(cos_sim) >= 0.03:
                # Void range — computational divergence from concurrent GPU inference,
                # not cheating. Uses abs() to catch both positive and negative divergence
                # (negative cosine = anti-correlation from GPU interference). Cheaters
                # submit zero/null states (cosine≈0.0).
                results.append({
                    "layer": layer, "position": position,
                    "passed": None, "cosine_sim": cos_sim, "reason": "voided",
                })
            else:
                self.total_commitment_fails += 1
                any_failed = True
                results.append({
                    "layer": layer, "position": position,
                    "passed": False, "cosine_sim": cos_sim,
                })

        # Add explicit failure entries for cherry-picked (missing) layers
        if cherry_pick_detected and requested_layers:
            returned_layers_set = set(c.get("layer") for c in commitments if c.get("layer") is not None)
            for ml in set(requested_layers) - returned_layers_set:
                self.total_commitment_fails += 1
                results.append({
                    "layer": ml, "position": -1,
                    "passed": False, "reason": "cherry_pick_missing",
                })

        # Add template tamper result
        if template_tamper_detected:
            results.append({
                "layer": -1, "position": -1,
                "passed": False, "reason": "chat_template_tampered",
            })

        # Add nonce verification result
        if nonce_check_failed:
            results.append({
                "layer": -1, "position": -1,
                "passed": False, "reason": "nonce_mismatch_relay_suspected",
            })

        if not results:
            return None

        passed_count = sum(1 for r in results if r.get("passed") is True)
        failed_count = sum(1 for r in results if r.get("passed") is False)

        # Scoring is handled by the caller (audit_loop) to avoid double-counting

        tamper_tag = " TEMPLATE-TAMPER" if template_tamper_detected else ""
        nonce_tag = " NONCE-FAIL" if nonce_check_failed else ""
        cosine_vals = [f"{r.get('cosine_sim', 0):.4f}" for r in results if "cosine_sim" in r]
        cos_tag = f" cosine=[{','.join(cosine_vals)}]" if cosine_vals else ""
        log.info(
            f"[COMMIT] Miner {miner_uid}: {passed_count} pass, {failed_count} fail, "
            f"{len(results) - passed_count - failed_count} void{tamper_tag}{nonce_tag}{cos_tag} | "
            f"request={request_id[:8]}..."
        )

        return {
            "request_id": request_id,
            "miner_uid": miner_uid,
            "results": results,
            "any_failed": any_failed,
            "passed_count": passed_count,
            "failed_count": failed_count,
        }

    async def perplexity_spot_check(self, record: dict) -> Optional[dict]:
        """
        Verify the miner's output was generated by the claimed model (not a distilled
        alternative). Computes per-token log-likelihood under our reference model for a
        random window of the miner's output tokens.

        Dual-Model Attack defense: If miner generated tokens with model A (fast/distilled)
        but extracted hidden states with model B (correct), the output tokens will have
        abnormally low likelihood under model B. Legitimate inference produces tokens that
        are highly probable under the generating model.

        Only runs on ~10% of audits (expensive — full forward pass + logit extraction).
        Returns result dict or None if skipped.
        """
        miner_uid = record.get("miner_uid")
        all_tokens = record.get("all_token_ids")
        if not all_tokens or len(all_tokens) < 20:
            return None

        # Only spot-check 10% of records (expensive)
        if secrets.randbelow(10) != 0:
            return None

        if not hasattr(self.model, 'tokenizer') or not self.model.tokenizer:
            return None

        try:
            import torch

            def _compute_perplexity():
                """Compute windowed perplexity of miner's output under reference model."""
                tokens_t = torch.tensor([all_tokens], dtype=torch.long, device=self.model._device)

                # Pick a random window of 16-32 tokens in the output region
                prompt_len = record.get("input_tokens", len(all_tokens) // 2)
                output_start = max(prompt_len, 1)
                output_end = len(all_tokens)
                if output_end - output_start < 8:
                    return None  # Too short to measure

                window_size = min(32, output_end - output_start)
                window_start = output_start + secrets.randbelow(max(1, output_end - output_start - window_size))
                window_end = min(window_start + window_size, output_end)

                # Forward pass to get logits
                with torch.no_grad():
                    outputs = self.model.model(
                        input_ids=tokens_t[:, :window_end],
                        output_hidden_states=False,
                    )
                logits = outputs.logits[0]  # (seq_len, vocab_size)

                # Compute per-token log probability in the window
                log_probs = []
                for i in range(window_start, window_end - 1):
                    token_logits = logits[i]  # Logits predicting token at position i+1
                    token_id = all_tokens[i + 1]
                    log_softmax = token_logits - torch.logsumexp(token_logits, dim=0)
                    log_probs.append(float(log_softmax[token_id]))

                if not log_probs:
                    return None

                avg_log_prob = sum(log_probs) / len(log_probs)
                perplexity = float(torch.exp(torch.tensor(-avg_log_prob)))
                return {
                    "perplexity": perplexity,
                    "avg_log_prob": avg_log_prob,
                    "window_start": window_start,
                    "window_end": window_end,
                    "window_tokens": window_end - window_start,
                }

            result = await asyncio.to_thread(_compute_perplexity)
            if result is None:
                return None

            # A model generating its own tokens typically has perplexity < 10.
            # A different model's tokens will have perplexity >> 50.
            # Threshold at 30 for now (conservative — natural variance in temperature/sampling
            # can produce higher perplexity, so we only flag extremely high values).
            PERPLEXITY_ALERT_THRESHOLD = 50
            PERPLEXITY_WARN_THRESHOLD = 25

            passed = result["perplexity"] < PERPLEXITY_ALERT_THRESHOLD
            if result["perplexity"] >= PERPLEXITY_ALERT_THRESHOLD:
                log.warning(
                    f"[PERPLEXITY] Miner {miner_uid}: ALERT perplexity={result['perplexity']:.1f} "
                    f"(threshold={PERPLEXITY_ALERT_THRESHOLD}) — possible dual-model attack | "
                    f"window=[{result['window_start']}:{result['window_end']}]"
                )
            elif result["perplexity"] >= PERPLEXITY_WARN_THRESHOLD:
                log.info(
                    f"[PERPLEXITY] Miner {miner_uid}: elevated perplexity={result['perplexity']:.1f} | "
                    f"window=[{result['window_start']}:{result['window_end']}]"
                )
            else:
                log.debug(
                    f"[PERPLEXITY] Miner {miner_uid}: OK perplexity={result['perplexity']:.1f}"
                )

            return {
                "miner_uid": miner_uid,
                "request_id": record.get("request_id", ""),
                "passed": passed,
                **result,
            }
        except Exception as e:
            log.warning(f"[PERPLEXITY] Error for miner {miner_uid}: {e}")
            return None

    def _select_records_for_audit(self, records: list[dict]) -> list[dict]:
        """
        Select records for auditing using adaptive per-miner challenge rates.

        Instead of a flat audit_rate for all records, each miner has its own
        challenge probability based on reputation (clean streak, suspect status).
        This keeps validator GPU cost roughly constant as the fleet grows —
        trusted miners get challenged less, freeing budget for new/suspicious ones.
        """
        candidates = [
            r for r in records
            if r.get("request_id") not in self.audited_request_ids
            and r.get("miner_uid") is not None
            and r.get("prompt")
        ]
        if not candidates:
            return []

        selected = []
        for record in candidates:
            uid = record["miner_uid"]
            # Get adaptive rate for this miner (falls back to base_rate for unknown UIDs)
            rate = self.scoring.get_challenge_rate(uid)
            # Cryptographic coin flip at the miner's rate
            if secrets.randbelow(1000) < int(rate * 1000):
                selected.append(record)

        # Always audit at least 1 if we have candidates (prevents zero-audit epochs)
        if not selected and candidates:
            selected.append(candidates[secrets.randbelow(len(candidates))])

        return selected

    async def audit_record(self, record: dict) -> Optional[dict]:
        """
        Audit a single R2 record by requesting hidden states from the miner.

        Steps:
        1. Get the miner's endpoint from our known miners
        2. Pick a random (layer, position) to challenge
        3. Send hidden_state request to miner
        4. Compute reference hidden state locally
        5. Compare via cosine similarity
        """
        miner_uid = record.get("miner_uid")
        request_id = record.get("request_id", "")
        prompt = record.get("prompt", "")
        response_text = record.get("response", "")

        if miner_uid not in self.miners:
            log.debug(f"[AUDIT] Skipping request {request_id}: miner {miner_uid} not in known miners")
            return None

        miner = self.miners[miner_uid]
        self.audited_request_ids.add(request_id)
        # Keep set bounded
        if len(self.audited_request_ids) > self._max_audited_ids:
            # Remove oldest (approximate — sets aren't ordered, but ok for dedup)
            to_remove = list(self.audited_request_ids)[:self._max_audited_ids // 2]
            for rid in to_remove:
                self.audited_request_ids.discard(rid)

        # Tokenize the prompt + response to get all_tokens
        # When messages were provided, miner applies apply_chat_template — we must match
        if hasattr(self.model, 'tokenizer') and self.model.tokenizer:
            messages = record.get("messages")
            if messages and hasattr(self.model.tokenizer, 'apply_chat_template'):
                effective_prompt = self.model.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
            else:
                effective_prompt = prompt
            full_text = effective_prompt + response_text
            all_tokens = self.model.tokenizer.encode(full_text)
        else:
            log.warning(f"[AUDIT] No tokenizer available, skipping {request_id}")
            return None

        seq_len = len(all_tokens)
        if seq_len < 2:
            log.debug(f"[AUDIT] Sequence too short for {request_id}")
            return None

        num_layers = self.model.config.num_layers

        # Create challenge
        challenge = self.challenge_engine.create_challenge(
            request_id=request_id,
            num_layers=num_layers,
            seq_len=seq_len,
        )

        # Request hidden state from miner
        session = await self._get_http_session()
        url = f"{miner.endpoint}/hidden_state"
        payload = {
            "request_id": request_id,
            "layer_index": challenge.layer_index,
            "token_index": challenge.token_index,
        }

        challenge_result = None
        try:
            t_start = time.perf_counter()
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=CHALLENGE_TIMEOUT_HARD_MS / 1000 + 5),  # +5s for network
            ) as resp:
                t_end = time.perf_counter()
                latency_ms = (t_end - t_start) * 1000

                if resp.status == 404:
                    # Deferred challenge 404 = normal cache eviction, NOT evasion.
                    # Track separately from inline commitment misses (which drive penalties).
                    self._deferred_eviction_counts[miner_uid] += 1
                    self._deferred_challenge_counts[miner_uid] += 1
                    eviction_rate = self._deferred_eviction_counts[miner_uid] / self._deferred_challenge_counts[miner_uid]
                    log.info(
                        f"[AUDIT] VOID (HTTP 404 cache eviction) | "
                        f"layer={challenge.layer_index} pos={challenge.token_index} | "
                        f"eviction_rate={eviction_rate:.0%} ({self._deferred_eviction_counts[miner_uid]}/{self._deferred_challenge_counts[miner_uid]})"
                    )
                    return None  # Void — don't penalize cache evictions
                if resp.status != 200:
                    challenge_result = {
                        "passed": False,
                        "reason": f"HTTP {resp.status}",
                        "latency_ms": latency_ms,
                        "cosine_sim": 0.0,
                    }
                else:
                    body = await resp.read()
                    if len(body) > 1 * 1024 * 1024:
                        challenge_result = {
                            "passed": False,
                            "reason": "Response too large",
                            "latency_ms": latency_ms,
                            "cosine_sim": 0.0,
                        }
                    else:
                        data = json.loads(body)
                        if data.get("error") == "cache_miss":
                            # Track deferred cache misses.
                            self._deferred_eviction_counts[miner_uid] += 1
                            self._deferred_challenge_counts[miner_uid] += 1
                            eviction_rate = self._deferred_eviction_counts[miner_uid] / self._deferred_challenge_counts[miner_uid]
                            # Eviction FAIL threshold: 70% over 5+ attempts.
                            # With reduced delay window (10-60s), legitimate miners
                            # should evict <40%. Only persistent, high-rate eviction
                            # (likely intentional cache-dumping) gets penalized.
                            total_deferred = self._deferred_challenge_counts[miner_uid]
                            if total_deferred >= 5 and eviction_rate >= 0.70:
                                log.warning(
                                    f"[AUDIT] FAIL (persistent cache_miss) | "
                                    f"layer={challenge.layer_index} pos={challenge.token_index} — "
                                    f"eviction_rate={eviction_rate:.0%} ({self._deferred_eviction_counts[miner_uid]}/{total_deferred}) "
                                    f"≥ 70% over ≥ 5 attempts → treating as FAIL"
                                )
                                return {
                                    "passed": False,
                                    "reason": f"Persistent cache_miss (eviction_rate={eviction_rate:.0%})",
                                    "latency_ms": latency_ms,
                                    "cosine_sim": 0.0,
                                }
                            log.info(
                                f"[AUDIT] VOID (cache_miss) | "
                                f"layer={challenge.layer_index} pos={challenge.token_index} — "
                                f"miner HF extraction OOM/timeout | "
                                f"eviction_rate={eviction_rate:.0%} ({self._deferred_eviction_counts[miner_uid]}/{total_deferred})"
                            )
                            return None  # Void — don't penalize individual misses
                        if "hidden_state" not in data:
                            challenge_result = {
                                "passed": False,
                                "reason": "Missing hidden_state",
                                "latency_ms": latency_ms,
                                "cosine_sim": 0.0,
                            }
                        else:
                            miner_state = np.array(data["hidden_state"], dtype=np.float32)
                            if miner_state.ndim != 1 or miner_state.shape[0] != self.model.config.hidden_dim:
                                challenge_result = {
                                    "passed": False,
                                    "reason": f"Shape mismatch: {miner_state.shape}",
                                    "latency_ms": latency_ms,
                                    "cosine_sim": 0.0,
                                }
                            elif not np.all(np.isfinite(miner_state)):
                                challenge_result = {
                                    "passed": False,
                                    "reason": "NaN/Inf in hidden state",
                                    "latency_ms": latency_ms,
                                    "cosine_sim": 0.0,
                                }
                            else:
                                # Compute reference (run in thread to avoid blocking event loop)
                                reference = await asyncio.to_thread(
                                    self.model.compute_hidden_state_at,
                                    all_tokens, challenge.layer_index, challenge.token_index
                                )

                                # Verify
                                verification = self.challenge_engine.verify_response(
                                    challenge_id=challenge.challenge_id,
                                    miner_hidden_state=miner_state,
                                    reference_hidden_state=reference,
                                    latency_ms=latency_ms,
                                )

                                # Void divergent cosine: likely computational interference, not cheating.
                                # GPU concurrent inference can produce negative cosine (anti-correlation)
                                # as well as low-positive. Use abs(cosine) to catch both directions.
                                # Cheaters submit zero/null states (cosine≈0.0) or fail ALL layers uniformly.
                                # Deep layers (>=20) use lower threshold due to CPU/GPU float divergence.
                                if not verification.passed:
                                    if challenge.layer_index >= 20:
                                        void_threshold = 0.01  # deep layers: void anything above 0.01
                                    else:
                                        void_threshold = 0.03  # shallow layers: interference produces |cos|>0.03
                                    if abs(verification.cosine_sim) >= void_threshold:
                                        log.info(
                                            f"[AUDIT] Miner {miner_uid}: VOID ({'deep-layer' if challenge.layer_index >= 20 else 'mid-range'} "
                                            f"cosine={verification.cosine_sim:.4f}) | "
                                            f"layer={challenge.layer_index} pos={challenge.token_index} — "
                                            f"likely computational interference, not cheating"
                                        )
                                        return None  # Void — don't count this audit

                                challenge_result = {
                                    "passed": verification.passed,
                                    "reason": verification.reason,
                                    "latency_ms": verification.latency_ms,
                                    "cosine_sim": verification.cosine_sim,
                                    "miner_reported_ms": data.get("latency_ms", 0),
                                }

        except asyncio.TimeoutError:
            challenge_result = {
                "passed": False,
                "reason": "Timeout",
                "latency_ms": CHALLENGE_TIMEOUT_HARD_MS + 5000,
                "cosine_sim": 0.0,
            }
        except (aiohttp.ClientConnectorError, ConnectionError, OSError) as e:
            # Connection errors (miner unreachable/crashed) are NOT cheating — void them.
            # Only genuine challenge failures (bad cosine, wrong hidden state) should
            # be penalized. Penalizing connectivity issues causes unfair weight loss
            # during brief outages (e.g. miner restart) and slow recovery.
            log.info(
                f"[AUDIT] Miner {miner_uid}: VOID (connection error) | "
                f"reason={e}"
            )
            challenge_result = {
                "passed": None,  # Void — not counted as pass or fail
                "reason": f"connection_error: {e}",
                "latency_ms": 0.0,
                "cosine_sim": 0.0,
            }
        except Exception as e:
            # Unknown errors still fail — could be protocol-level cheating
            challenge_result = {
                "passed": False,
                "reason": str(e),
                "latency_ms": 0.0,
                "cosine_sim": 0.0,
            }

        # NOTE: Do NOT increment _cache_challenge_counts here — that counter
        # tracks commitment requests only (inline commitment misses). Incrementing
        # it on every async challenge dilutes the miss rate denominator, hiding
        # miners that skip 100% of commitments behind a low apparent miss rate.

        # Track successful deferred challenge in diagnostic counter (no penalty impact)
        self._deferred_challenge_counts[miner_uid] += 1

        # Connection-error voids: skip scoring entirely — miner was unreachable,
        # not cheating. No penalty, no audit count, no challenge rate update.
        challenge_passed = challenge_result["passed"]
        if challenge_passed is None:
            return challenge_result

        # Record score — with RTT-corrected timing defense
        ttft_ms = record.get("ttft_ms", 0)
        tps = record.get("tokens_per_sec", 0)
        cos_sim = challenge_result["cosine_sim"]
        raw_latency = challenge_result["latency_ms"]

        # Timing defense: subtract RTT baseline to estimate actual server-side extraction time.
        # VRAM cache lookup: <5ms. Re-running forward pass: 2-10s for Qwen 7B.
        # Raw latency includes network RTT (10-120ms) which is irrelevant to VRAM proof.
        rtt_baseline = self.rtt_tracker.get_baseline(miner_uid)
        net_extraction_ms = self.rtt_tracker.get_net_extraction_time(miner_uid, raw_latency)

        # RTT baseline updates come ONLY from /health pings (clean, small packets).
        # Do NOT record challenge RTT here — challenge responses include variable
        # extraction time and feeding baseline back into the tracker creates a
        # feedback loop that miners could exploit during the ramp-up phase.

        # Use RTT-corrected time for scoring if we have enough baseline data
        challenge_latency_for_scoring = net_extraction_ms if rtt_baseline > 0 else raw_latency

        # Cross-check miner-reported extraction time (informational — miners can lie)
        miner_reported_ms = challenge_result.get("miner_reported_ms", 0)

        # Re-inference detection: if net extraction time > 1000ms, the miner likely
        # had to re-run inference from scratch (Qwen 7B takes 2-10s on GPU).
        # This is a strong signal of not having VRAM cache, distinct from network latency.
        if net_extraction_ms > REINFERENCE_THRESHOLD_MS and challenge_passed:
            log.warning(
                f"[TIMING] Miner {miner_uid}: net extraction {net_extraction_ms:.0f}ms "
                f"(raw={raw_latency:.0f}ms - rtt={rtt_baseline:.0f}ms) > {REINFERENCE_THRESHOLD_MS}ms — "
                f"possible re-inference from scratch"
            )

        medians_ttft, medians_tps = self.scoring.get_miner_medians()
        speed = compute_speed_score(ttft_ms, tps, miner_medians_ttft=medians_ttft, miner_medians_tps=medians_tps)
        verification = compute_verification_score(challenge_passed, cos_sim, challenge_latency_for_scoring)
        quality = compute_output_quality(response_text)

        score = RequestScore(
            request_id=request_id,
            miner_uid=miner_uid,
            timestamp=time.time(),
            is_synthetic=record.get("type") == "synthetic",
            speed_score=speed,
            verification_score=verification,
            quality_score=quality,
            ttft_ms=ttft_ms,
            tokens_per_sec=tps,
            cosine_sim=cos_sim,
            challenge_latency_ms=challenge_latency_for_scoring,
            challenge_passed=challenge_passed,
        )
        self.scoring.record_request(score)

        # Update adaptive challenge rate tracking
        self.scoring.record_challenge_outcome(miner_uid, challenge_passed)

        # Update stats
        self.total_audits += 1
        if challenge_passed:
            self.total_passed += 1
        else:
            self.total_failed += 1

        rate = self.scoring.get_challenge_rate(miner_uid)
        streak = self.scoring._clean_streak.get(miner_uid, 0)
        status = "PASS" if challenge_passed else "FAIL"
        log.info(
            f"[AUDIT] Miner {miner_uid}: {status} | "
            f"cosine={cos_sim:.4f} raw_latency={raw_latency:.0f}ms "
            f"net_extraction={net_extraction_ms:.0f}ms (rtt_base={rtt_baseline:.0f}ms) "
            f"layer={challenge.layer_index} pos={challenge.token_index} "
            f"reason={challenge_result['reason']} "
            f"challenge_rate={rate:.0%} streak={streak}"
        )

        return {
            "request_id": request_id,
            "miner_uid": miner_uid,
            "challenge_passed": challenge_passed,
            "cosine_sim": cos_sim,
            "latency_ms": raw_latency,
            "net_extraction_ms": net_extraction_ms,
            "rtt_baseline_ms": rtt_baseline,
            "reason": challenge_result["reason"],
            "layer": challenge.layer_index,
            "position": challenge.token_index,
        }

    async def audit_loop(self):
        """Main audit loop — reads R2, samples records, audits miners."""
        while True:
            try:
                if not self.r2_reader:
                    log.warning("[AUDIT] No R2 reader configured")
                    await asyncio.sleep(self.audit_interval_s)
                    continue

                # Read recent records
                records = self.r2_reader.list_recent_records(hours=1, max_records=200)
                if not records:
                    log.debug("[AUDIT] No records found in R2")
                    await asyncio.sleep(self.audit_interval_s)
                    continue

                # RED-TEAM FIX (step 14): Track cache miss rates on ALL records
                # BEFORE the probabilistic audit filter. Previously, only records
                # selected by _select_records_for_audit were tracked, meaning many
                # cache misses went uncounted (especially for low-challenge-rate miners).
                # A 100% cache-miss miner could have its miss data undercounted.
                for record in records:
                    if record.get("request_id") in self.audited_request_ids:
                        continue  # Already counted in a previous cycle
                    if not record.get("commitment_requested"):
                        continue
                    uid = record.get("miner_uid")
                    if uid is None:
                        continue
                    if record.get("commitments"):
                        # Commitment returned — count as attempt (no miss)
                        self._cache_challenge_counts[uid] = self._cache_challenge_counts.get(uid, 0) + 1
                    else:
                        # Commitment requested but NOT returned — cache miss
                        self._cache_miss_counts[uid] = self._cache_miss_counts.get(uid, 0) + 1
                        self._cache_challenge_counts[uid] = self._cache_challenge_counts.get(uid, 0) + 1
                        miss_rate = self._cache_miss_counts[uid] / self._cache_challenge_counts[uid]
                        log.warning(
                            f"[AUDIT] Miner {uid}: commitment requested but NOT returned — "
                            f"tracked as cache miss (rate={miss_rate:.0%})"
                        )

                # Sample for audit
                to_audit = self._select_records_for_audit(records)
                if not to_audit:
                    log.debug("[AUDIT] No new records to audit")
                    await asyncio.sleep(self.audit_interval_s)
                    continue

                # Split: records with inline commitments vs those needing async challenge
                with_commits = [r for r in to_audit if r.get("commitments")]
                without_commits = [r for r in to_audit if not r.get("commitments")]

                log.info(
                    f"[AUDIT] Auditing {len(to_audit)}/{len(records)} records "
                    f"({len(with_commits)} inline, {len(without_commits)} async)"
                )

                # Fast path: verify inline commitments (no network calls needed)
                for record in with_commits:
                    # Mark as audited to prevent re-auditing next cycle
                    rid = record.get("request_id", "")
                    if rid:
                        self.audited_request_ids.add(rid)
                    try:
                        result = await self.verify_commitments(record)
                        if result:
                            uid = result["miner_uid"]
                            if result["any_failed"]:
                                # Commitment failed — record fail + async challenge for confirmation
                                self.scoring.record_challenge_outcome(uid, False)
                                medians_ttft, medians_tps = self.scoring.get_miner_medians()
                                speed = compute_speed_score(
                                    record.get("ttft_ms", 0), record.get("tokens_per_sec", 0),
                                    miner_medians_ttft=medians_ttft, miner_medians_tps=medians_tps,
                                )
                                score = RequestScore(
                                    request_id=record.get("request_id", ""),
                                    miner_uid=uid, timestamp=time.time(),
                                    is_synthetic=record.get("type") == "synthetic",
                                    speed_score=speed, verification_score=0.0,
                                    quality_score=compute_output_quality(record.get("response", "")),
                                    ttft_ms=record.get("ttft_ms", 0),
                                    tokens_per_sec=record.get("tokens_per_sec", 0),
                                    cosine_sim=0.0, challenge_latency_ms=0.0,
                                    challenge_passed=False,
                                )
                                self.scoring.record_request(score)
                                self.total_audits += 1
                                self.total_failed += 1
                                await self.audit_record(record)  # Double-check
                            elif result["passed_count"] > 0:
                                # Commitment passed — record as verified
                                self.scoring.record_challenge_outcome(uid, True)
                                passed_cosines = [
                                    r["cosine_sim"] for r in result["results"]
                                    if r.get("passed") is True and "cosine_sim" in r
                                ]
                                avg_cos = float(np.mean(passed_cosines)) if passed_cosines else 1.0
                                medians_ttft, medians_tps = self.scoring.get_miner_medians()
                                speed = compute_speed_score(
                                    record.get("ttft_ms", 0), record.get("tokens_per_sec", 0),
                                    miner_medians_ttft=medians_ttft, miner_medians_tps=medians_tps,
                                )
                                verification = compute_verification_score(True, avg_cos, 0.0)
                                score = RequestScore(
                                    request_id=record.get("request_id", ""),
                                    miner_uid=uid, timestamp=time.time(),
                                    is_synthetic=record.get("type") == "synthetic",
                                    speed_score=speed, verification_score=verification,
                                    quality_score=compute_output_quality(record.get("response", "")),
                                    ttft_ms=record.get("ttft_ms", 0),
                                    tokens_per_sec=record.get("tokens_per_sec", 0),
                                    cosine_sim=avg_cos, challenge_latency_ms=0.0,
                                    challenge_passed=True,
                                )
                                self.scoring.record_request(score)
                                self.total_audits += 1
                                self.total_passed += 1

                                # Perplexity spot-check (10% of passing records)
                                # Catches dual-model attacks where output tokens come
                                # from a different model than the hidden states
                                try:
                                    ppl_result = await self.perplexity_spot_check(record)
                                    if ppl_result and not ppl_result["passed"]:
                                        # High perplexity — flag as suspicious but don't fail yet
                                        # (could be high-temperature sampling). Log for monitoring.
                                        log.warning(
                                            f"[PERPLEXITY] Miner {uid}: suspicious output PPL={ppl_result['perplexity']:.1f}"
                                        )
                                except Exception as e:
                                    log.debug(f"[PERPLEXITY] Check error: {e}")
                            else:
                                # All commitments voided (none passed, none failed)
                                # Log but don't count as pass or fail — voided results
                                # are computational divergence, not cheating evidence
                                log.info(
                                    f"[COMMIT] Miner {uid}: all {len(result['results'])} commitments voided "
                                    f"(0 pass, 0 fail) | request={record.get('request_id', '')[:8]}..."
                                )

                    except Exception as e:
                        log.error(f"[COMMIT] Error verifying {record.get('request_id', '?')}: {e}")

                # Slow path: async hidden state challenges (requires HTTP to miner)
                for record in without_commits:
                    try:
                        await self.audit_record(record)
                    except Exception as e:
                        log.error(f"[AUDIT] Error auditing {record.get('request_id', '?')}: {e}")

                # RED-TEAM FIX (step 15): Push cache miss rates to scoring engine
                # after each audit cycle, not just at epoch end. This ensures that
                # live weight queries (via compute_weights()) reflect cache miss
                # penalties mid-epoch, closing the window where evading miners
                # show inflated weights for the entire epoch duration.
                for uid in set(self._cache_challenge_counts.keys()):
                    self.scoring.set_cache_miss_rate(
                        uid, self._cache_miss_counts.get(uid, 0),
                        self._cache_challenge_counts[uid],
                    )

                # Jittered sleep
                jitter = self.audit_interval_s * 0.3
                delay = self.audit_interval_s + (secrets.randbelow(int(jitter * 2000)) - int(jitter * 1000)) / 1000.0
                await asyncio.sleep(max(5, delay))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[AUDIT] Loop error: {e}")
                await asyncio.sleep(10)

    async def epoch_loop(self):
        """Background loop for epoch management and weight setting."""
        while True:
            try:
                if self.scoring.should_end_epoch():
                    # Push cache miss rates to scoring before epoch ends
                    for uid in set(self._cache_challenge_counts.keys()):
                        self.scoring.set_cache_miss_rate(
                            uid, self._cache_miss_counts[uid],
                            self._cache_challenge_counts[uid],
                        )
                    summary = self.scoring.end_epoch()

                    # Reset cache miss counters for the new epoch.
                    # Each epoch should evaluate misses independently — a miner
                    # that fixes a caching bug should not be permanently penalized
                    # by historical misses from previous epochs.
                    self._cache_miss_counts.clear()
                    self._cache_challenge_counts.clear()
                    # Also reset deferred eviction counters per epoch
                    self._deferred_eviction_counts.clear()
                    self._deferred_challenge_counts.clear()
                    self.epoch_summaries.append(summary)
                    if len(self.epoch_summaries) > 100:
                        self.epoch_summaries = self.epoch_summaries[-100:]

                    # Publish epoch summary
                    self.r2_publisher.publish_epoch_summary(summary)

                    log.info(f"\n{'='*60}")
                    log.info(f"EPOCH {summary['epoch']} COMPLETE")
                    log.info(f"  Total audits this epoch: {self.total_audits}")
                    log.info(f"  Pass rate: {self.total_passed}/{self.total_audits} ({self.total_passed/max(self.total_audits,1)*100:.1f}%)")
                    for uid, info in summary["miners"].items():
                        suspect = " SUSPECT" if info.get("is_suspect") else ""
                        log.info(
                            f"  Miner {uid}: net_pts={info['net_points']:.3f} "
                            f"weight={info['weight']:.4f} "
                            f"pass_rate={info['pass_rate']:.1%}{suspect}"
                        )
                    log.info(f"{'='*60}\n")

                    # Set weights on chain
                    if self.chain and summary["weights"]:
                        success = await self.chain.set_weights(summary["weights"])
                        summary["weights_committed"] = success
                        if success:
                            log.info(f"[EPOCH {summary['epoch']}] Weights committed to chain")
                        else:
                            log.error(f"[EPOCH {summary['epoch']}] Failed to commit weights")

                # Cleanup expired challenges
                self.challenge_engine.cleanup_expired()
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Epoch check error: {e}")
                await asyncio.sleep(5)

    async def _ping_miner_rtt(self, uid: int, endpoint: str):
        """Measure raw network RTT to a miner via /health. Updates RTT tracker."""
        session = await self._get_http_session()
        try:
            t0 = time.perf_counter()
            async with session.get(
                f"{endpoint}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                await resp.read()
                t1 = time.perf_counter()
                rtt_ms = (t1 - t0) * 1000
                self.rtt_tracker.record(uid, rtt_ms)
                log.debug(f"[RTT] Miner {uid}: {rtt_ms:.1f}ms (baseline={self.rtt_tracker.get_baseline(uid):.1f}ms)")
        except Exception:
            pass  # Don't let RTT failures disrupt anything

    async def _ping_all_miners(self):
        """Ping all known miners to update RTT baselines."""
        tasks = []
        for uid, miner in self.miners.items():
            tasks.append(self._ping_miner_rtt(uid, miner.endpoint))
        if tasks:
            await asyncio.gather(*tasks)

    async def discovery_loop(self):
        """Background loop for metagraph-based miner discovery."""
        if not self.discovery:
            return
        while True:
            try:
                discovered = await self.discovery.discover_miners()
                if discovered:
                    for m in discovered:
                        uid = m["uid"]
                        self.miners[uid] = MinerEndpoint(
                            uid=uid,
                            endpoint=m["endpoint"],
                            hotkey=m.get("hotkey", ""),
                        )
                        # Register hotkey for Sybil tracking
                        if m.get("hotkey"):
                            self.scoring.register_hotkey(uid, m["hotkey"])
                    # Remove stale
                    active_uids = {m["uid"] for m in discovered}
                    stale = [uid for uid in self.miners if uid not in active_uids]
                    for uid in stale:
                        del self.miners[uid]
                        log.info(f"Miner {uid}: removed (stale)")

                # Ping all miners to update RTT baselines
                await self._ping_all_miners()

                await asyncio.sleep(self.discovery.sync_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Discovery error: {e}")
                await asyncio.sleep(30)

    async def synthetic_audit_loop(self):
        """
        Generate synthetic probes and send them through a known miner,
        then immediately audit the result. This provides a constant
        stream of audit data even when organic traffic is low.
        """
        _TOPICS = [
            "quantum computing", "merge sort", "transformer architecture",
            "writing a compelling opening paragraph", "how the stock market works",
            "how to make sourdough bread", "dealing with procrastination",
            "photosynthesis", "REST API design", "Python async",
            "neural networks", "climate change", "cryptocurrency mining",
            "machine learning optimization", "database indexing", "operating systems",
            "compiler design", "network protocols", "distributed systems",
            "functional programming", "cybersecurity fundamentals", "data structures",
            "calculus concepts", "linear algebra", "probability theory",
            "evolutionary biology", "microeconomics", "ancient Roman history",
            "Renaissance art", "jazz improvisation", "film noir",
            "cognitive behavioral therapy", "supply chain management",
            "renewable energy", "CRISPR gene editing", "satellite communication",
            "game theory", "natural language processing", "reinforcement learning",
            "modern architecture", "organic chemistry", "astrophysics",
            "behavioral economics", "DNA sequencing", "volcanic eruptions",
            "ancient Greek philosophy", "social media algorithms",
            "electric vehicle batteries", "container orchestration",
            "memory management", "garbage collection algorithms",
        ]
        _STYLES = [
            "Explain {topic} in simple terms.",
            "What are the key trade-offs in {topic}?",
            "Give a practical example of {topic}.",
            "What is {topic} and why does it matter?",
            "Describe the history of {topic} briefly.",
            "Compare two approaches to {topic}.",
            "What are common misconceptions about {topic}?",
            "How would you teach {topic} to a beginner?",
            "What recent advances have been made in {topic}?",
            "Write a short summary of {topic} for an expert audience.",
            "What problems does {topic} solve?",
            "List the main challenges in {topic} today.",
        ]
        # Add random prefix/suffix variation to prevent fingerprinting
        _PREFIXES = [
            "", "Hey, ", "Quick question: ", "I'm curious about this: ",
            "Can you help me understand ", "I'd like to know about ",
            "Tell me about ", "Please explain ", "I need help with ",
            "", "", "",  # Weight toward no prefix
        ]

        while True:
            try:
                if not self.miners:
                    await asyncio.sleep(10)
                    continue

                # Pick a random miner, skipping dead ones (retry every 5 min)
                now_synth = time.time()
                miner_uids = [
                    u for u in self.miners
                    if self._synth_consec_fails[u] < self._synth_fail_threshold
                    or (now_synth - self._synth_last_retry[u]) > self._synth_fail_retry
                ]
                if not miner_uids:
                    miner_uids = list(self.miners.keys())
                uid = secrets.choice(miner_uids)
                miner = self.miners[uid]
                # Mark retry timestamp for dead-miner retries
                if self._synth_consec_fails[uid] >= self._synth_fail_threshold:
                    self._synth_last_retry[uid] = now_synth

                # Generate prompt with randomized structure to prevent fingerprinting
                topic = secrets.choice(_TOPICS)
                style = secrets.choice(_STYLES)
                prefix = secrets.choice(_PREFIXES)
                base_prompt = style.format(topic=topic)
                prompt = prefix + base_prompt if not prefix.endswith(" ") or base_prompt[0].isupper() else prefix + base_prompt[0].lower() + base_prompt[1:]
                max_tokens = 32 + secrets.randbelow(225)  # 32-256, continuous range
                request_id = str(uuid.uuid4())

                # Send inference to miner with inline commitment request
                session = await self._get_http_session()
                url = f"{miner.endpoint}/inference"

                # Generate multi-layer commitment request (2-3 layers, random positions)
                num_layers = getattr(self.model.config, 'num_layers', 28)
                eligible_layers = [l for l in range(num_layers) if l < num_layers - 1]
                n_commit = 3 if secrets.randbelow(5) == 0 else 2
                n_commit = min(n_commit, len(eligible_layers))
                pool = list(eligible_layers)
                commit_layers_list = []
                for _ in range(n_commit):
                    idx = secrets.randbelow(len(pool))
                    commit_layers_list.append(pool.pop(idx))
                commit_positions_list = []
                for _ in commit_layers_list:
                    roll = secrets.randbelow(10)
                    if roll < 4:
                        commit_positions_list.append("last")
                    elif roll < 7:
                        commit_positions_list.append(-(secrets.randbelow(20) + 1))
                    else:
                        commit_positions_list.append(secrets.randbelow(50) + 5)

                # Generate nonce for anti-relay binding
                import hashlib as _hl, hmac as _hm
                if not hasattr(self, '_synth_nonce_secret'):
                    self._synth_nonce_secret = secrets.token_hex(32)
                nonce = _hm.new(
                    self._synth_nonce_secret.encode(),
                    f"{request_id}:{uid}".encode(),
                    _hl.sha256
                ).hexdigest()[:32]

                # Include messages field to match organic request format (anti-fingerprinting)
                # ~30% of probes use multi-turn format with system prompt to exercise chat template
                if secrets.randbelow(10) < 3:
                    _SYSTEM_PROMPTS = [
                        "You are a helpful assistant.",
                        "Answer concisely and accurately.",
                        "You are an expert tutor. Explain clearly.",
                        "Respond in a direct, factual manner.",
                    ]
                    sys_prompt = secrets.choice(_SYSTEM_PROMPTS)
                    # Occasionally add a multi-turn context
                    if secrets.randbelow(2) == 0:
                        messages = [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": prompt},
                        ]
                    else:
                        _FOLLOWUPS = [
                            "Can you elaborate?",
                            "Tell me more about that.",
                            "What are the key points?",
                            "Give me an example.",
                        ]
                        messages = [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": "Sure, I'd be happy to help with that."},
                            {"role": "user", "content": secrets.choice(_FOLLOWUPS)},
                        ]
                else:
                    messages = [{"role": "user", "content": prompt}]
                payload = {
                    "prompt": prompt,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "request_id": request_id,
                    "nonce": nonce,
                    "commit_layers": commit_layers_list,
                    "commit_positions": commit_positions_list,
                }

                try:
                    wall_start = time.perf_counter()
                    if self.gateway_relay_url and self.gateway_relay_secret:
                        # Route through gateway to mask source IP
                        result = await self._send_via_relay(uid, payload)
                        if result is None:
                            log.info(f"[SYNTH] Miner {uid}: relay failed, falling back to direct")
                            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                                if resp.status != 200:
                                    log.info(f"[SYNTH] Miner {uid}: HTTP {resp.status}")
                                    await asyncio.sleep(10)
                                    continue
                                result = json.loads(await resp.read())
                    else:
                        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            if resp.status != 200:
                                log.info(f"[SYNTH] Miner {uid}: HTTP {resp.status}")
                                await asyncio.sleep(10)
                                continue
                            result = json.loads(await resp.read())
                    wall_time_ms = (time.perf_counter() - wall_start) * 1000.0
                except Exception as e:
                    self._synth_consec_fails[uid] += 1
                    if self._synth_consec_fails[uid] == self._synth_fail_threshold:
                        log.info(f"[SYNTH] Miner {uid}: {self._synth_fail_threshold} consecutive failures, skipping until retry")
                    elif self._synth_consec_fails[uid] < self._synth_fail_threshold:
                        log.info(f"[SYNTH] Miner {uid} error: {e}")
                    await asyncio.sleep(2)
                    continue

                # Success — reset failure counter
                if self._synth_consec_fails[uid] > 0:
                    log.info(f"[SYNTH] Miner {uid}: recovered after {self._synth_consec_fails[uid]} failures")
                    self._synth_consec_fails[uid] = 0

                response_text = result.get("text", "")
                reported_ttft = result.get("ttft_ms", 0)
                reported_tps = result.get("tokens_per_sec", 0)
                input_tokens = result.get("input_tokens", 0) or len(prompt.split())
                output_tokens = result.get("output_tokens", 0) or len(response_text.split())

                # TTFT validation: clamp miner-reported ttft_ms using wall-clock bounds.
                # Miners can fabricate ttft — apply same defense as organic path.
                if wall_time_ms > 0:
                    min_ttft = wall_time_ms / max(1 + output_tokens, 2)
                    ttft_ms = max(min_ttft, min(reported_ttft, wall_time_ms))
                else:
                    ttft_ms = reported_ttft

                all_token_ids = result.get("all_token_ids")
                commitments = result.get("commitments")

                # TPS validation: Always validate reported_tps against wall-clock.
                # Commitments prove the miner ran the model but NOT the claimed speed.
                # Committing miners have hidden state extraction overhead (~100-500ms)
                # so we allow 1.5x wall-clock for them. Non-committing miners get 1.1x.
                if wall_time_ms > 0 and output_tokens > 0:
                    wall_tps = output_tokens / (wall_time_ms / 1000.0)
                    tps_cap = 1.5 if commitments else 1.1
                    tps = min(reported_tps, wall_tps * tps_cap)
                else:
                    tps = reported_tps

                commit_tag = f" +{len(commitments)} commits" if commitments else ""
                log.info(f"[SYNTH] Miner {uid}: inference OK | ttft={ttft_ms:.0f}ms tps={tps:.1f}{commit_tag}")

                # Publish to R2 so it appears in the dataset
                try:
                    audit_rec = AuditRecord(
                        request_id=request_id,
                        miner_uid=uid,
                        miner_hotkey=miner.hotkey,
                        is_synthetic=True,
                        prompt=prompt,
                        response_text=response_text,
                        ttft_ms=ttft_ms,
                        tokens_per_sec=tps,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        messages=messages,
                        commitments=commitments,
                        commitment_requested=True,
                        nonce=nonce,
                        all_token_ids=all_token_ids,
                        requested_layers=commit_layers_list,
                        requested_positions=commit_positions_list,
                    )
                    await asyncio.to_thread(self.r2_publisher.publish, audit_rec)
                except Exception as e:
                    log.warning(f"[SYNTH] R2 publish failed: {e}")

                # Adaptive challenge: use per-miner rate to decide whether to audit.
                # If yes, DEFER the challenge (Vector 29) — push to queue so the
                # /hidden_state request arrives 30-180s later, breaking the
                # temporal correlation that lets miners detect auditor probes.
                rate = self.scoring.get_challenge_rate(uid)
                should_challenge = secrets.randbelow(1000) < int(rate * 1000)

                if should_challenge:
                    record = {
                        "request_id": request_id,
                        "miner_uid": uid,
                        "prompt": prompt,
                        "messages": messages,
                        "response": response_text,
                        "ttft_ms": ttft_ms,
                        "tokens_per_sec": tps,
                        "type": "synthetic",
                    }
                    try:
                        self._deferred_queue.put_nowait({
                            "record": record,
                            "queued_at": time.time(),
                        })
                        log.info(f"[SYNTH] Miner {uid}: challenge deferred")
                    except asyncio.QueueFull:
                        # Queue full — fall back to immediate challenge
                        log.warning(f"[SYNTH] Deferred queue full, challenging miner {uid} immediately")
                        await self.audit_record(record)
                else:
                    # Still record the speed/quality score even without a challenge
                    medians_ttft, medians_tps = self.scoring.get_miner_medians()
                    speed = compute_speed_score(ttft_ms, tps, miner_medians_ttft=medians_ttft, miner_medians_tps=medians_tps)
                    verification = compute_verification_score(None, 0.0, 0.0)  # unchallenged
                    quality = compute_output_quality(response_text)
                    score = RequestScore(
                        request_id=request_id,
                        miner_uid=uid,
                        timestamp=time.time(),
                        is_synthetic=True,
                        speed_score=speed,
                        verification_score=verification,
                        quality_score=quality,
                        ttft_ms=ttft_ms,
                        tokens_per_sec=tps,
                        cosine_sim=0.0,
                        challenge_latency_ms=0.0,
                        challenge_passed=None,
                    )
                    self.scoring.record_request(score)
                    log.info(f"[SYNTH] Miner {uid}: skipped challenge (rate={rate:.0%}, trusted)")

                # Exponential jitter to prevent timing fingerprinting (Vector 27).
                # Mean ~15s, range [5, 45]. Exponential distribution mimics
                # organic Poisson-process traffic better than uniform.
                _u = max(1e-9, secrets.randbelow(10000) / 10000.0)
                delay = min(45, max(5, -15.0 * math.log(_u)))
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[SYNTH] Error: {e}")
                await asyncio.sleep(10)

    async def bandwidth_probe_loop(self):
        """
        Periodic bandwidth probes — send concurrent burst requests to measure
        sustained throughput under load. Incentivizes miners to optimize for
        concurrency (continuous batching, efficient KV cache management).

        Runs every ~5 minutes, picks one miner per probe cycle.
        Sends CONCURRENCY simultaneous requests and measures aggregate TPS.
        """
        _BW_TOPICS = [
            "hash tables", "TCP vs UDP", "recursion", "garbage collection",
            "binary search", "linked lists", "graph traversal", "caching strategies",
            "load balancing", "encryption basics", "API rate limiting", "memory allocation",
            "thread synchronization", "event-driven architecture", "message queues",
            "consensus algorithms", "bloom filters", "B-trees", "virtual memory",
            "process scheduling", "deadlock prevention", "HTTP/2 features",
        ]
        _BW_STYLES = [
            "Explain how {t} works in simple terms.",
            "What are the main benefits of {t}?",
            "Describe {t} with a practical example.",
            "Compare different approaches to {t}.",
        ]

        while True:
            try:
                if not self.miners:
                    await asyncio.sleep(30)
                    continue

                # Pick a random miner, skipping dead ones
                now_bw = time.time()
                miner_uids = [
                    u for u in self.miners
                    if self._synth_consec_fails[u] < self._synth_fail_threshold
                    or (now_bw - self._synth_last_retry[u]) > self._synth_fail_retry
                ]
                if not miner_uids:
                    miner_uids = list(self.miners.keys())
                uid = secrets.choice(miner_uids)
                miner = self.miners[uid]
                if self._synth_consec_fails[uid] >= self._synth_fail_threshold:
                    self._synth_last_retry[uid] = now_bw

                # Re-randomize per cycle to prevent parameter fingerprinting
                CONCURRENCY = 2 + secrets.randbelow(4)  # 2-5 simultaneous requests
                MAX_TOKENS = 32 + secrets.randbelow(97)  # 32-128 tokens

                session = await self._get_http_session()

                async def _single_probe(prompt_text: str) -> Optional[dict]:
                    request_id = str(uuid.uuid4())
                    # Include nonce, commit_layers, commit_positions, and messages
                    # to match organic/synthetic request format — prevents fingerprinting.
                    import hashlib as _hl_bw, hmac as _hm_bw
                    if not hasattr(self, '_bw_nonce_secret'):
                        self._bw_nonce_secret = secrets.token_hex(32)
                    bw_nonce = _hm_bw.new(
                        self._bw_nonce_secret.encode(),
                        f"{request_id}:{uid}".encode(),
                        _hl_bw.sha256
                    ).hexdigest()[:32]
                    num_layers = getattr(self.model.config, 'num_layers', 28) if self.model else 28
                    eligible = [l for l in range(num_layers) if l < num_layers - 1]
                    n_bw = 3 if secrets.randbelow(5) == 0 else 2
                    n_bw = min(n_bw, len(eligible))
                    pool_bw = list(eligible)
                    bw_layers = []
                    for _ in range(n_bw):
                        idx = secrets.randbelow(len(pool_bw))
                        bw_layers.append(pool_bw.pop(idx))
                    bw_positions = []
                    for _ in bw_layers:
                        roll = secrets.randbelow(10)
                        if roll < 4:
                            bw_positions.append("last")
                        elif roll < 7:
                            bw_positions.append(-(secrets.randbelow(20) + 1))
                        else:
                            bw_positions.append(secrets.randbelow(50) + 5)
                    messages = [{"role": "user", "content": prompt_text}]
                    payload = {
                        "prompt": prompt_text,
                        "messages": messages,
                        "max_tokens": MAX_TOKENS,
                        "request_id": request_id,
                        "nonce": bw_nonce,
                        "commit_layers": bw_layers,
                        "commit_positions": bw_positions,
                    }
                    try:
                        t0 = time.perf_counter()
                        if self.gateway_relay_url and self.gateway_relay_secret:
                            data = await self._send_via_relay(uid, payload)
                            t1 = time.perf_counter()
                            if data is None:
                                return None
                        else:
                            async with session.post(
                                f"{miner.endpoint}/inference",
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=60),
                            ) as resp:
                                t1 = time.perf_counter()
                                if resp.status != 200:
                                    return None
                                data = json.loads(await resp.read())
                        # TPS calculation applies to both relay and direct paths
                        wt_ms = (t1 - t0) * 1000
                        reported_tps_bw = data.get("tokens_per_sec", 0)
                        out_tok = data.get("output_tokens", 0)
                        if isinstance(out_tok, list):
                            out_tok = len(out_tok)
                        # Validate output_tokens against response text length.
                        # Miners can inflate output_tokens to game aggregate TPS.
                        # Use all_token_ids if available, else approximate from text.
                        resp_text = data.get("text", "")
                        all_ids = data.get("all_token_ids")
                        if all_ids and isinstance(all_ids, list):
                            verified_tokens = len(all_ids)
                        elif resp_text:
                            # ~1.3 tokens per word is a conservative estimate
                            verified_tokens = max(1, int(len(resp_text.split()) * 1.3))
                        else:
                            verified_tokens = out_tok
                        # Cap at 2x verified to allow tokenizer variance
                        if verified_tokens > 0 and out_tok > verified_tokens * 2:
                            out_tok = verified_tokens
                        # Wall-clock TPS cap for bandwidth probes.
                        # Concurrent load causes queuing, inflating wall time.
                        # Use 1.3x cap (tightened from 2.0x — reduces gaming surface).
                        if wt_ms > 0 and out_tok > 0:
                            wall_tps_bw = out_tok / (wt_ms / 1000.0)
                            validated_tps = min(reported_tps_bw, wall_tps_bw * 1.3)
                        else:
                            validated_tps = reported_tps_bw
                        return {
                            "wall_time_ms": wt_ms,
                            "tps": validated_tps,
                            "output_tokens": out_tok,
                            "request_id": request_id,
                        }
                    except Exception:
                        return None

                # Send concurrent burst
                t_burst_start = time.perf_counter()
                # Generate random prompts per burst to prevent fingerprinting
                burst_prompts = [
                    secrets.choice(_BW_STYLES).format(t=secrets.choice(_BW_TOPICS))
                    for _ in range(CONCURRENCY)
                ]
                tasks = [_single_probe(p) for p in burst_prompts]
                results = await asyncio.gather(*tasks)
                t_burst_end = time.perf_counter()

                successful = [r for r in results if r is not None]
                if not successful:
                    self._synth_consec_fails[uid] += 1
                    if self._synth_consec_fails[uid] == self._synth_fail_threshold:
                        log.info(f"[BANDWIDTH] Miner {uid}: {self._synth_fail_threshold} consecutive failures, skipping")
                    else:
                        log.info(f"[BANDWIDTH] Miner {uid}: all {CONCURRENCY} probes failed")
                    await asyncio.sleep(30)
                    continue
                # Reset failure counter on any success
                if self._synth_consec_fails[uid] > 0:
                    self._synth_consec_fails[uid] = 0

                # Compute aggregate metrics
                total_tokens = sum(r["output_tokens"] for r in successful)
                burst_wall_s = t_burst_end - t_burst_start
                aggregate_tps = total_tokens / max(burst_wall_s, 0.001)
                avg_individual_tps = sum(r["tps"] for r in successful) / len(successful)

                # The ratio of aggregate TPS under load vs single-request TPS reveals
                # how well the miner handles concurrency. Good continuous batching
                # should maintain near-linear scaling up to GPU saturation.
                concurrency_ratio = aggregate_tps / max(avg_individual_tps, 1)

                log.info(
                    f"[BANDWIDTH] Miner {uid}: {len(successful)}/{CONCURRENCY} ok | "
                    f"aggregate={aggregate_tps:.0f} tps | "
                    f"avg_individual={avg_individual_tps:.0f} tps | "
                    f"concurrency_ratio={concurrency_ratio:.2f} | "
                    f"burst_time={burst_wall_s:.1f}s"
                )

                # Feed concurrency ratio to scoring engine for weight bonuses
                # Use each miner's median TPS as baseline (from prior epoch data)
                stats = self.scoring.miner_stats.get(uid)
                baseline_tps = float(np.median(stats.tps_values)) if stats and stats.tps_values else avg_individual_tps
                self.scoring.record_bandwidth_probe(uid, aggregate_tps, baseline_tps)

                # Bandwidth probe results are already tracked via record_bandwidth_probe().
                # Do NOT record individual sub-requests as RequestScores — that would
                # inflate request counts and mix unchecked quality_score=1.0 into the
                # scoring pool. The concurrency ratio is the only signal we need.

                # Wide jitter to prevent interval fingerprinting (Vector 28).
                # Range [180, 480] with exponential distribution, mean ~300s.
                _u = max(1e-9, secrets.randbelow(10000) / 10000.0)
                delay = min(480, max(180, -300.0 * math.log(_u)))
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[BANDWIDTH] Error: {e}")
                await asyncio.sleep(60)

    async def deferred_challenge_worker(self):
        """
        Process deferred challenge queue — breaks timing correlation between
        inference and challenge requests (Vector 29).

        Instead of sending /hidden_state challenges immediately after
        synthetic inference (which creates a detectable temporal pattern),
        challenges are queued here and processed after a random delay.

        The delay uses an exponential distribution with mean ~60s, clamped
        to [30, 180]s. This ensures miners cannot correlate inference
        requests with subsequent challenge requests based on timing.

        Combined with inline commitments (COMMIT_RATE=1.0), this gives us
        two independent verification paths:
        1. Commitments verified later in audit_loop (already deferred)
        2. /hidden_state challenges sent via this worker (now also deferred)
        """
        while True:
            try:
                # Block until a record is queued
                item = await self._deferred_queue.get()
                record = item["record"]
                queued_at = item["queued_at"]

                # Random delay before issuing challenge — exponential
                # distribution mimics natural spacing.
                # Adaptive delay: high-TPS miners get shorter delays because
                # their LRU caches fill faster (200 slots at 200 TPS = 1s/slot).
                # Low-TPS miners can tolerate longer delays.
                uid = record.get("miner_uid")
                miner_tps = record.get("tokens_per_sec", 50)
                if miner_tps > 100:
                    # Fast miner: delay [1, 8]s (mean ~2s)
                    # High-TPS miners cycle LRU cache slots in < 1s at 200 TPS.
                    # 87% eviction rate at [3,15]s was too long; reduced to
                    # improve deferred challenge coverage to ~40-50%.
                    _u = max(1e-9, secrets.randbelow(10000) / 10000.0)
                    target_delay = min(8, max(1, -2.0 * math.log(_u)))
                elif miner_tps > 50:
                    # Medium miner: delay [3, 20]s (mean ~6s)
                    _u = max(1e-9, secrets.randbelow(10000) / 10000.0)
                    target_delay = min(20, max(3, -6.0 * math.log(_u)))
                else:
                    # Slow miner: delay [5, 40]s (mean ~12s)
                    _u = max(1e-9, secrets.randbelow(10000) / 10000.0)
                    target_delay = min(40, max(5, -12.0 * math.log(_u)))
                elapsed = time.time() - queued_at
                remaining = max(0, target_delay - elapsed)
                if remaining > 0:
                    await asyncio.sleep(remaining)

                # Now issue the challenge
                uid = record.get("miner_uid")
                log.info(f"[DEFERRED] Challenging miner {uid} (delayed {time.time() - queued_at:.0f}s from inference)")
                try:
                    await self.audit_record(record)
                except Exception as e:
                    log.error(f"[DEFERRED] Challenge error for miner {uid}: {e}")

                self._deferred_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[DEFERRED] Worker error: {e}")
                await asyncio.sleep(5)


# ── FastAPI Health/Status API ────────────────────────────────────────────────

def create_validator_app(validator: AuditValidator) -> "FastAPI":
    from fastapi import FastAPI as FA, Request
    app = FA(title="Audit Validator", version=VALIDATOR_VERSION)

    @app.get("/health")
    @app.get("/v1/health")
    async def health(request: Request = None):
        # Basic health for external consumers — no operational details
        basic = {
            "status": "ok",
            "version": VALIDATOR_VERSION,
            "mode": "audit_validator",
            "miners_known": len(validator.miners),
            "total_audits": validator.total_audits,
            "pass_rate": validator.total_passed / max(validator.total_audits, 1),
        }
        # Full details only for localhost requests (proxy_gateway, monitoring)
        client_ip = request.client.host if request and request.client else ""
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            basic.update({
                "model": validator.model.config.name,
                "total_passed": validator.total_passed,
                "total_failed": validator.total_failed,
                "epoch": validator.scoring.epoch_number,
                "epoch_elapsed_s": int(time.time() - validator.scoring.current_epoch_start),
                "chain": {
                    "enabled": validator.chain is not None,
                    "total_weight_sets": validator.chain.total_sets if validator.chain else 0,
                    "last_set": validator.chain.last_set_time if validator.chain else 0,
                },
                "challenge_rates": validator.scoring.get_all_challenge_rates(),
                "rtt_baselines_ms": validator.rtt_tracker.summary(),
                "inline_commitments": {
                    "checks": validator.total_commitment_checks,
                    "passes": validator.total_commitment_passes,
                    "fails": validator.total_commitment_fails,
                },
                "cache_miss_rates": {
                    uid: {
                        "misses": validator._cache_miss_counts[uid],
                        "total": validator._cache_challenge_counts[uid],
                        "rate": round(validator._cache_miss_counts[uid] / max(validator._cache_challenge_counts[uid], 1), 2),
                    }
                    for uid in validator._cache_challenge_counts
                },
                "deferred_evictions": {
                    uid: {
                        "evictions": validator._deferred_eviction_counts[uid],
                        "total": validator._deferred_challenge_counts[uid],
                        "rate": round(validator._deferred_eviction_counts[uid] / max(validator._deferred_challenge_counts[uid], 1), 2),
                    }
                    for uid in validator._deferred_challenge_counts
                },
                # deferred_queue_size intentionally hidden — exposes timing sidechannel
            })
        return basic

    @app.get("/v1/scoreboard")
    async def scoreboard():
        return {
            "epoch": validator.scoring.epoch_number,
            "total_audits": validator.total_audits,
            "miners": validator.scoring.get_scoreboard(),
        }

    @app.get("/v1/epochs")
    async def epochs():
        return validator.epoch_summaries[-20:]

    return app


# ── CLI ──────────────────────────────────────────────────────────────────────

async def run_validator(args):
    # Load model
    model = RealValidatorModel(args.model, device=args.device)

    # Chain
    chain = None
    if args.wallet:
        chain = ChainWeightSetter(
            wallet_name=args.wallet,
            hotkey=args.hotkey,
            netuid=args.netuid,
            network=args.network,
            wallet_path=args.wallet_path,
        )
        log.info(f"Chain: wallet={args.wallet} netuid={args.netuid}")

    # Discovery
    discovery = None
    if args.discover:
        discovery = MetagraphDiscovery(
            netuid=args.netuid,
            network=args.network,
        )

    # R2 reader
    r2_endpoint = os.environ.get("R2_URL", "").rstrip("/")
    r2_access = os.environ.get("R2_ACCESS_KEY_ID", "")
    r2_secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    r2_bucket = os.environ.get("R2_BUCKET", "affine")

    r2_reader = R2AuditReader(
        bucket_name=r2_bucket,
        endpoint_url=r2_endpoint or None,
        access_key=r2_access or None,
        secret_key=r2_secret or None,
        local_dir=args.r2_dir,
    )

    # Static miners (if provided)
    static_miners = {}
    if args.miners:
        for i, endpoint in enumerate(args.miners):
            static_miners[i] = MinerEndpoint(uid=i, endpoint=endpoint)

    validator = AuditValidator(
        model=model,
        chain=chain,
        discovery=discovery,
        r2_reader=r2_reader,
        audit_rate=args.audit_rate,
        audit_interval_s=args.audit_interval,
        epoch_length_s=args.epoch_length,
    )
    validator.miners = static_miners

    # Optional status API
    import uvicorn
    app = create_validator_app(validator)
    uvi_config = uvicorn.Config(app, host="0.0.0.0", port=args.status_port, log_level="warning", log_config=None)
    server = uvicorn.Server(uvi_config)

    async def server_task():
        await server.serve()

    async def main_loop():
        await asyncio.sleep(2)
        log.info(f"Audit Validator v{VALIDATOR_VERSION} running")
        log.info(f"Model: {args.model} on {args.device}")
        log.info(f"Audit rate: {args.audit_rate:.0%}, interval: {args.audit_interval}s")
        log.info(f"Epoch: {args.epoch_length}s")
        log.info(f"Status API: port {args.status_port}")

        tasks = [
            asyncio.create_task(validator.audit_loop()),
            asyncio.create_task(validator.epoch_loop()),
            asyncio.create_task(validator.discovery_loop()),
            asyncio.create_task(validator.synthetic_audit_loop()),
            asyncio.create_task(validator.bandwidth_probe_loop()),
            # 3 parallel workers drain the deferred queue faster and add
            # more timing noise (overlapping delays for different miners)
            asyncio.create_task(validator.deferred_challenge_worker()),
            asyncio.create_task(validator.deferred_challenge_worker()),
            asyncio.create_task(validator.deferred_challenge_worker()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await validator.close()

    try:
        await asyncio.gather(server_task(), main_loop())
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.should_exit = True
        await validator.close()


def main():
    parser = argparse.ArgumentParser(description="Audit Validator — async miner auditor")
    parser.add_argument("--model", required=True, help="HuggingFace model name (e.g. Qwen/Qwen2.5-7B-Instruct)")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="Model device")
    parser.add_argument("--miners", nargs="+", default=[], help="Static miner endpoints")
    parser.add_argument("--discover", action="store_true", help="Enable metagraph discovery")
    parser.add_argument("--audit-rate", type=float, default=0.3, help="Fraction of records to audit (0.0-1.0)")
    parser.add_argument("--audit-interval", type=float, default=15.0, help="Seconds between audit rounds")
    parser.add_argument("--epoch-length", type=int, default=1500, help="Epoch length (seconds, must exceed weights_rate_limit ~1200s)")
    parser.add_argument("--r2-dir", default="/tmp/r2-audit", help="Local R2 audit directory (for reading)")
    parser.add_argument("--status-port", type=int, default=8082, help="Status API port")
    # Chain
    parser.add_argument("--wallet", default=None, help="Bittensor wallet name")
    parser.add_argument("--hotkey", default="default", help="Bittensor hotkey name")
    parser.add_argument("--netuid", type=int, default=97, help="Subnet UID")
    parser.add_argument("--network", default="finney", help="Bittensor network")
    parser.add_argument("--wallet-path", default=None, help="Wallet directory path")
    args = parser.parse_args()

    if not args.miners and not args.discover:
        parser.error("Must specify --miners or --discover")

    asyncio.run(run_validator(args))


if __name__ == "__main__":
    main()
