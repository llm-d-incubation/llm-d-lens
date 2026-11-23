"""Standalone stdlib-only vLLM probe, mounted into model containers by Prism.

Run as a script for begin/end commands. No prompts or token IDs leave the engine.
The hook observes full prompt blocks held at successful request completion, not
placement changes, allocation attempts, or per-token decode memory accesses.
"""

import fcntl
import functools
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(os.environ.get("PRISM_KV_TRACE_DIR", "/tmp/prism-kv-trace"))  # noqa: S108 - pod-local scratch, configurable by env
MAX_BYTES = 128 * 1024 * 1024


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def engines():
    result = []
    for path in sorted(ROOT.glob("engine-*.json")):
        item = json.loads(path.read_text())
        try:
            os.kill(item["pid"], 0)
        except ProcessLookupError:
            continue
        result.append(item)
    return result


def command(action, session):
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", session):
        raise ValueError("invalid trace session")
    if action.startswith("transfer-"):
        return transfer_command(action, session)
    with locked():
        active = ROOT / "active.json"
        if action == "begin":
            if active.exists():
                raise ValueError("another KV capture is active")
            current = engines()
            if not current or any(not item["supported"] for item in current):
                raise ValueError("compatible vLLM KV manager probe is not ready")
            control = {"session": session, "start": time.time(), "engines": current}
            (ROOT / f"{session}.jsonl").write_text("")
            active.write_text(json.dumps(control))
            return control
        if not active.exists():
            raise ValueError("KV capture is not active")
        control = json.loads(active.read_text())
        if control["session"] != session:
            raise ValueError("KV capture session mismatch")
        active.unlink()
        path = ROOT / f"{session}.jsonl"
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            return {**control, "end": time.time(), "engines_end": engines(), "requests": rows}
        finally:
            path.unlink(missing_ok=True)


def inspect_request(manager, request):
    groups = manager.kv_cache_config.kv_cache_groups
    if len(groups) != 1 or type(groups[0].kv_cache_spec).__name__ != "FullAttentionSpec":
        raise ValueError("requires one FullAttention KV cache group")
    if not manager.enable_caching:
        raise ValueError("prefix caching disabled")
    spec = groups[0].kv_cache_spec
    size = spec.block_size
    # Reject context-parallel layouts whose logical block size differs.
    effective = getattr(manager, "block_size", None)
    if effective is not None and effective != size:
        raise ValueError("context-parallel block layout unsupported")
    if (
        getattr(request, "lora_request", None)
        or getattr(request, "mm_features", None)
        or getattr(request, "multi_modal_inputs", None)
    ):
        raise ValueError("LoRA and multimodal KV identities are not supported")
    tokens = request.prompt_token_ids
    if not isinstance(tokens, list) or any(type(t) is not int or t < 0 for t in tokens):
        raise ValueError("engine prompt token IDs unavailable")
    if request.num_computed_tokens < len(tokens):
        raise ValueError("prompt computation not complete")
    blocks = manager.get_blocks(request.request_id).blocks
    count = len(tokens) // size
    if len(blocks) != 1 or len(blocks[0]) < count:
        raise ValueError("prompt block coverage incomplete")
    namespace = os.environ.get("PRISM_KV_TRACE_NAMESPACE", "")
    if not namespace:
        raise ValueError("model/cache namespace missing")
    # Canonical chained hashes avoid process-local native hash seeds while
    # retaining engine tokenization and the full parent-prefix context.
    parent = hashlib.sha256(json.dumps([namespace, size, str(getattr(request, "cache_salt", None))]).encode()).digest()
    accesses = []
    for index, block in enumerate(blocks[0][:count]):
        if getattr(block, "is_null", False) or block.block_hash is None:
            raise ValueError("prompt block is not a reusable computed block")
        parent = hashlib.sha256(
            parent + json.dumps(tokens[index * size : (index + 1) * size], separators=(",", ":")).encode()
        ).digest()
        accesses.append({"namespace": namespace, "block_hash": parent.hex(), "valid_tokens": size})
    return accesses


def capture(manager, request):
    if getattr(getattr(request, "status", None), "name", "") not in {"FINISHED_STOPPED", "FINISHED_LENGTH_CAPPED"}:
        return
    if not (ROOT / "active.json").exists():
        return
    try:
        with locked():
            if not (ROOT / "active.json").exists():
                return
            control = json.loads((ROOT / "active.json").read_text())
            path = ROOT / f"{control['session']}.jsonl"
            item = {"request_id": request.request_id, "timestamp": time.time(), "pid": os.getpid()}
            try:
                item["accesses"] = inspect_request(manager, request)
            except Exception as error:
                item["error"] = str(error)
            if path.stat().st_size > MAX_BYTES:
                return  # Collector's client/engine count parity rejects truncation.
            with path.open("a") as output:
                output.write(json.dumps(item, separators=(",", ":")) + "\n")
                output.flush()
    except Exception:  # noqa: S110 - trace capture must never disrupt inference
        # Instrumentation must not break inference; missing records fail parity.
        pass


def install(module):
    cls = module.KVCacheManager
    if getattr(cls, "_prism_kv_trace", False):
        return
    original_init, original_free = cls.__init__, cls.free

    @functools.wraps(original_init)
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            groups = self.kv_cache_config.kv_cache_groups
            supported = (
                len(groups) == 1
                and type(groups[0].kv_cache_spec).__name__ == "FullAttentionSpec"
                and self.enable_caching
                and callable(getattr(self, "get_blocks", None))
            )
            try:
                engine_version = version("vllm")
            except PackageNotFoundError:
                engine_version = "unknown"
            with locked():
                (ROOT / f"engine-{os.getpid()}.json").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "instance": str(time.time_ns()),
                            "supported": bool(supported),
                            "probe_version": 1,
                            "vllm_version": engine_version,
                            "source": "vllm.KVCacheManager.free",
                        }
                    )
                )
        except Exception:  # noqa: S110 - tracing is optional and must not disrupt inference
            pass

    @functools.wraps(original_free)
    def free(self, request, *args, **kwargs):
        capture(self, request)
        return original_free(self, request, *args, **kwargs)

    cls.__init__, cls.free, cls._prism_kv_trace = init, free, True


def transfer_engines():
    result = []
    for path in sorted(ROOT.glob("transfer-engine-*.json")):
        item = json.loads(path.read_text())
        try:
            os.kill(item["pid"], 0)
        except ProcessLookupError:
            continue
        result.append(item)
    return result


def transfer_command(action, session):
    with locked():
        active = ROOT / "transfer-active.json"
        if action == "transfer-begin":
            if active.exists():
                previous = json.loads(active.read_text())
                if time.time() - previous["start"] <= 300:
                    raise ValueError("another transfer capture is active")
                (ROOT / f"transfer-{previous['session']}.jsonl").unlink(missing_ok=True)
                active.unlink()
            current = transfer_engines()
            if not current or any(not item["supported"] for item in current):
                raise ValueError("vLLM 0.26.0 NIXL pull probe is unavailable")
            control = {"session": session, "start": time.time(), "engines": current}
            (ROOT / f"transfer-{session}.jsonl").write_text("")
            active.write_text(json.dumps(control))
            return control
        if action != "transfer-end" or not active.exists():
            raise ValueError("transfer capture is not active")
        control = json.loads(active.read_text())
        if control["session"] != session:
            raise ValueError("transfer capture session mismatch")
        if time.time() - control["start"] > 300:
            control["incomplete"] = True
        active.unlink()
        path = ROOT / f"transfer-{session}.jsonl"
        try:
            return {
                **control,
                "end": time.time(),
                "engines_end": transfer_engines(),
                "events": [json.loads(line) for line in path.read_text().splitlines()],
            }
        finally:
            path.unlink(missing_ok=True)


def transfer_event(worker, event, request_id, **values):
    if not (ROOT / "transfer-active.json").exists():
        return
    try:
        with locked():
            active = ROOT / "transfer-active.json"
            if not active.exists():
                return
            control = json.loads(active.read_text())
            if time.time() - control["start"] > 300:
                return
            path = ROOT / f"transfer-{control['session']}.jsonl"
            if path.stat().st_size >= min(MAX_BYTES, 2 * 1024 * 1024):
                control["incomplete"] = True
                active.write_text(json.dumps(control))
                return
            row = {
                "event": event,
                "request_id": request_id,
                "timestamp": time.time(),
                "engine_id": worker.engine_id,
                "rank": worker.tp_rank,
                "world_size": worker.world_size,
                **values,
            }
            with path.open("a") as output:
                output.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:  # noqa: S110
        pass


def install_transfer(module):
    cls = module.NixlPullConnectorWorker
    if getattr(cls, "_lens_transfer_probe", False):
        return
    original_init = cls.__init__
    original_finished = cls.get_finished
    original_failure = cls._handle_failed_transfer
    original_notifs = cls._get_new_notifs

    @functools.wraps(original_init)
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._lens_failed_transfers = set()
        try:
            engine_version = version("vllm")
            with locked():
                (ROOT / f"transfer-engine-{os.getpid()}-{self.tp_rank}.json").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "instance": str(time.time_ns()),
                            "engine_id": self.engine_id,
                            "rank": self.tp_rank,
                            "world_size": self.world_size,
                            "vllm_version": engine_version,
                            "supported": engine_version.split("+")[0] == "0.26.0",
                            "probe_version": 1,
                        }
                    )
                )
        except Exception:  # noqa: S110
            pass

    @functools.wraps(original_failure)
    def failed(self, req_id, *args, **kwargs):
        self._lens_failed_transfers.add(req_id)
        transfer_event(self, "failed", req_id)
        return original_failure(self, req_id, *args, **kwargs)

    @functools.wraps(original_notifs)
    def notified(self, *args, **kwargs):
        result = original_notifs(self, *args, **kwargs)
        for request_id in result:
            transfer_event(self, "send-acknowledged", request_id)
        return result

    @functools.wraps(original_finished)
    def finished(self, *args, **kwargs):
        observing = (ROOT / "transfer-active.json").exists()
        metadata = dict(self._recving_metadata) if observing else {}
        active = (
            {request_id for request_id, handles in self._recving_transfers.items() if handles} if observing else set()
        )
        result = original_finished(self, *args, **kwargs)
        for request_id in result[1]:
            meta = metadata.get(request_id)
            if request_id in self._lens_failed_transfers:
                transfer_event(self, "failed", request_id)
                self._lens_failed_transfers.discard(request_id)
            elif request_id in active and meta and meta.remote:
                transfer_event(
                    self,
                    "received",
                    request_id,
                    remote_engine_id=meta.remote.engine_id,
                    remote_request_id=meta.remote.request_id,
                    blocks=sum(len(group) for group in meta.local_block_ids),
                )
        return result

    cls.__init__, cls.get_finished = init, finished
    cls._handle_failed_transfer, cls._get_new_notifs = failed, notified
    cls._lens_transfer_probe = True


if __name__ == "__main__":
    try:
        print(json.dumps(command(sys.argv[1], sys.argv[2])))
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
