"""run_submission.py and serve/local_server.py: server lifecycle, connection file, budget, submission fallback.

No torch, transformers or network. The model server is a stdlib fake run as a real subprocess (it answers
/v1/models and chat completions with fixed JSON), and run_v2 is replaced by a stub that writes its checkpoint."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_submission as rs

LOCAL_SERVER_PATH = rs.REPO_ROOT / "serve" / "local_server.py"


def _load_local_server():
    spec = importlib.util.spec_from_file_location("local_server_under_test", LOCAL_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks the module up by name while it is being executed
    spec.loader.exec_module(module)
    return module


LS = _load_local_server()

FAKE_SERVER = r'''
"""Stand-in for serve/local_server.py: FAKE_MODE=ok serves, sleep never listens, exit exits with code 3."""
import http.server, json, os, sys, time

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "exit":
    sys.exit(3)
if mode == "sleep":
    time.sleep(600)
port = int(sys.argv[sys.argv.index("--port") + 1])
token = os.environ["DATAMATE_SERVER_TOKEN"]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("Authorization", "") == "Bearer " + token

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": {"message": "unauthorized"}})
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if not self._authorized():
            return self._send(401, {"error": {"message": "unauthorized"}})
        if self.path == "/v1/chat/completions":
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "ABSTAIN"}}]})
        self._send(404, {})


http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

QUESTIONS = [
    {"id": "q1", "business_id": "b1", "question": "Doanh thu tháng 1?", "as_of_date": "2026-01-31"},
    {"id": "q2", "business_id": "b1", "question": "Đơn hàng tháng 1?", "as_of_date": "2026-01-31"},
    {"id": "q3", "business_id": "b2", "question": "Tồn kho?", "as_of_date": "2026-01-31"},
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _request(url: str, token: str | None, method: str = "GET", body: dict | None = None,
             raw: bytes | None = None) -> tuple[int, bytes]:
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def fake_server(tmp_path: Path) -> Path:
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


def _start(tmp_path: Path, fake: Path, token: str = "tok-123") -> rs.ModelServer:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return rs.start_model_server(model_dir=tmp_path, port=_free_port(), token=token, out_dir=out, gpus="0",
                                 batch_max=6, server_script=fake)


# ---------------------------------------------------------------------------------------------------------------
# Server lifecycle.
# ---------------------------------------------------------------------------------------------------------------

def test_server_starts_answers_models_and_stops(tmp_path, fake_server) -> None:
    server = _start(tmp_path, fake_server)
    try:
        assert rs.wait_until_ready(server, timeout=30, poll=0.2) == "fake-model"
        assert rs.probe_models(server.port, "wrong-token") is None
    finally:
        rs.stop_model_server(server)
    assert server.proc.poll() is not None
    assert not _port_open(server.port)


def test_wait_raises_when_server_never_listens(tmp_path, fake_server, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    server = _start(tmp_path, fake_server)
    try:
        with pytest.raises(rs.ServerError, match="did not answer"):
            rs.wait_until_ready(server, timeout=1.5, poll=0.2)
    finally:
        rs.stop_model_server(server, grace=5)
    assert server.proc.poll() is not None


def test_wait_fails_fast_when_server_exits(tmp_path, fake_server, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "exit")
    server = _start(tmp_path, fake_server)
    started = time.monotonic()
    try:
        with pytest.raises(rs.ServerError, match="exited with code 3"):
            rs.wait_until_ready(server, timeout=60, poll=0.2)
        assert time.monotonic() - started < 30
    finally:
        rs.stop_model_server(server)


def test_stop_terminates_a_running_process_group() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    rs._stop_process_group(proc, grace=10)
    assert proc.poll() is not None


def test_run_pipeline_returns_the_real_exit_code_of_run_v2() -> None:
    # run_v2 rejects an unknown flag with argparse's exit code 2; this checks the module path and working directory.
    assert rs.run_pipeline(["--no-such-flag"], hard_timeout=120) == 2


# ---------------------------------------------------------------------------------------------------------------
# Connection file and budget.
# ---------------------------------------------------------------------------------------------------------------

def test_connection_file_is_mode_600_and_replaces_a_wider_file(tmp_path) -> None:
    path = tmp_path / "connection.json"
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o644)
    rs.write_connection_file(path, "Some/Model", "secret-token")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "Some/Model", "bearer_token": "secret-token"}
    assert not path.with_name(path.name + ".tmp").exists()


def test_budget_is_total_minus_server_start_minus_margin() -> None:
    assert rs.SAFETY_MARGIN_SECONDS == 300.0
    assert rs.pipeline_budget_seconds(21000, 120.5) == pytest.approx(21000 - 120.5 - 300)
    assert rs.pipeline_budget_seconds(1000, 900) < 0


def test_run_v2_argv_carries_budget_base_url_and_aliases_off(tmp_path) -> None:
    argv = rs.build_run_v2_argv(
        data=tmp_path / "data", out=tmp_path / "out", connection=tmp_path / "c.json",
        base_url="http://127.0.0.1:8000/v1", budget_seconds=20579.6, workers=12, fewshot_bank=tmp_path / "bank.jsonl",
    )
    assert argv[argv.index("--total-seconds") + 1] == "20580"
    assert argv[argv.index("--base-url") + 1] == "http://127.0.0.1:8000/v1"
    assert argv[argv.index("--aliases") + 1] == "off"
    assert argv[argv.index("--workers") + 1] == "12"
    assert argv[argv.index("--fewshot-bank") + 1] == str(tmp_path / "bank.jsonl")
    assert "--fewshot-bank" not in rs.build_run_v2_argv(
        data=tmp_path, out=tmp_path, connection=tmp_path, base_url="u", budget_seconds=1, workers=1, fewshot_bank=None)


# ---------------------------------------------------------------------------------------------------------------
# run_submission.main: the whole run with a fake server and a stub run_v2.
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def run_env(tmp_path: Path, monkeypatch, fake_server: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "questions.jsonl").write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in QUESTIONS) + "\n", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(rs, "SERVER_SCRIPT", fake_server)
    monkeypatch.delenv("FAKE_MODE", raising=False)
    monkeypatch.delenv(rs.TOKEN_ENV, raising=False)
    return SimpleNamespace(data=data, model=model, out=tmp_path / "out",
                           submission=tmp_path / "deliver" / "submission.jsonl", port=_free_port())


def _argv(env, *extra: str) -> list[str]:
    return ["--data", str(env.data), "--model-dir", str(env.model), "--out", str(env.out),
            "--submission", str(env.submission), "--port", str(env.port), "--server-timeout", "30", *extra]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _arg(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_main_copies_pipeline_submission_and_stops_server(run_env, monkeypatch) -> None:
    seen: dict = {}

    def fake_pipeline(argv, *, hard_timeout):
        conn_path = Path(_arg(argv, "--connection"))
        seen["mode"] = stat.S_IMODE(conn_path.stat().st_mode)
        conn = json.loads(conn_path.read_text(encoding="utf-8"))
        seen["probe"] = rs.probe_models(run_env.port, conn["bearer_token"])
        seen["base_url"] = _arg(argv, "--base-url")
        seen["budget"] = int(_arg(argv, "--total-seconds"))
        seen["hard_timeout"] = hard_timeout
        out = Path(_arg(argv, "--out"))
        final = [json.dumps({"id": q["id"], "type": "abstain", "reason": "stub"}) for q in QUESTIONS]
        (out / "submission.final.jsonl").write_text("\n".join(final) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(rs, "run_pipeline", fake_pipeline)
    assert rs.main(_argv(run_env)) == 0

    assert seen["base_url"] == f"http://127.0.0.1:{run_env.port}/v1"
    assert seen["mode"] == 0o600
    assert seen["probe"] == "fake-model"  # the token in connection.json is accepted by the live server
    assert 20000 < seen["budget"] <= 21000 - 300
    assert seen["hard_timeout"] > 0
    assert run_env.submission.read_text(encoding="utf-8") == (run_env.out / "submission.final.jsonl").read_text(encoding="utf-8")
    assert not (run_env.out / "connection.json").exists()
    assert not _port_open(run_env.port)


@pytest.mark.parametrize("failure", [RuntimeError("model endpoint exploded"), rs._Terminated("signal 15")])
def test_pipeline_exception_still_writes_submission_and_stops_server(run_env, monkeypatch, failure) -> None:
    def fake_pipeline(argv, *, hard_timeout):
        out = Path(_arg(argv, "--out"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "submission.jsonl").write_text(json.dumps({"id": "q1", "type": "number", "answer": 12}) + "\n",
                                              encoding="utf-8")
        raise failure

    monkeypatch.setattr(rs, "run_pipeline", fake_pipeline)
    assert rs.main(_argv(run_env)) == 0

    rows = _rows(run_env.submission)
    assert [r["id"] for r in rows] == ["q1", "q2", "q3"]
    assert rows[0] == {"id": "q1", "type": "number", "answer": 12}
    assert rows[1] == {"id": "q2", "type": "abstain", "reason": "not answered"}
    assert not _port_open(run_env.port)
    assert not (run_env.out / "connection.json").exists()


def test_pipeline_nonzero_without_final_rebuilds_from_checkpoint(run_env, monkeypatch) -> None:
    def fake_pipeline(argv, *, hard_timeout):
        out = Path(_arg(argv, "--out"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "submission.jsonl").write_text(
            json.dumps({"id": "q2", "type": "number", "answer": 3}) + "\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(rs, "run_pipeline", fake_pipeline)
    assert rs.main(_argv(run_env)) == 0
    rows = {r["id"]: r for r in _rows(run_env.submission)}
    assert rows["q2"]["answer"] == 3
    assert rows["q1"]["type"] == "abstain" and rows["q3"]["type"] == "abstain"


def test_server_failure_skips_pipeline_and_still_writes_submission(run_env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "exit")
    calls: list = []
    monkeypatch.setattr(rs, "run_pipeline", lambda argv, *, hard_timeout: calls.append(argv) or 0)
    assert rs.main(_argv(run_env)) == 0
    assert calls == []
    assert [r["type"] for r in _rows(run_env.submission)] == ["abstain"] * 3


def test_no_time_left_skips_pipeline(run_env, monkeypatch) -> None:
    monkeypatch.setattr(rs, "pipeline_budget_seconds", lambda *a, **k: -5.0)
    calls: list = []
    monkeypatch.setattr(rs, "run_pipeline", lambda argv, *, hard_timeout: calls.append(argv) or 0)
    assert rs.main(_argv(run_env)) == 0
    assert calls == []
    assert len(_rows(run_env.submission)) == 3


def test_no_submission_possible_exits_non_zero(run_env, monkeypatch) -> None:
    (run_env.data / "questions.jsonl").write_text("this is not json\n", encoding="utf-8")
    monkeypatch.setattr(rs, "run_pipeline", lambda argv, *, hard_timeout: 0)  # completes without a final file
    assert rs.main(_argv(run_env)) == 1
    assert not run_env.submission.exists()


def test_missing_inputs_are_rejected_before_any_server_starts(run_env, monkeypatch) -> None:
    started: list = []
    monkeypatch.setattr(rs, "start_model_server", lambda **kw: started.append(kw))
    (run_env.data / "questions.jsonl").unlink()
    assert rs.main(_argv(run_env)) == 2
    assert started == []


def test_total_seconds_too_small_is_an_argument_error(run_env) -> None:
    with pytest.raises(SystemExit):
        rs.parse_args(_argv(run_env, "--total-seconds", "400"))


# ---------------------------------------------------------------------------------------------------------------
# serve/local_server.py: compile, token handling, gateway, and the pure logic.
# ---------------------------------------------------------------------------------------------------------------

def test_local_server_compiles() -> None:
    result = subprocess.run([sys.executable, "-m", "py_compile", str(LOCAL_SERVER_PATH)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_token_comes_from_argument_then_env_then_generated_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(LS.TOKEN_ENV, raising=False)
    assert LS.resolve_token("from-arg", None) == "from-arg"
    monkeypatch.setenv(LS.TOKEN_ENV, "from-env")
    assert LS.resolve_token(None, str(tmp_path / "t.txt")) == "from-env"
    monkeypatch.delenv(LS.TOKEN_ENV)
    token_file = tmp_path / "t.txt"
    generated = LS.resolve_token(None, str(token_file))
    assert len(generated) >= 32
    assert token_file.read_text(encoding="utf-8") == generated
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        LS.resolve_token(None, None)


def test_gpu_list_and_defaults() -> None:
    assert LS.parse_gpus("0,1") == [0, 1]
    assert LS.parse_gpus(" 1 , 0 , 1 ") == [1, 0]
    with pytest.raises(ValueError):
        LS.parse_gpus(" , ")
    args = LS.parse_args(["--model-dir", "/models/gemma-x"])
    assert (args.port, args.gpus, args.batch_max, args.served_model_name) == (8000, "0,1", 6, "gemma-x")


def test_think_block_and_eos_handling() -> None:
    assert LS.strip_think("<think>reasoning</think>\nSELECT 1") == "SELECT 1"
    assert LS.strip_think("<think>cut off") == ""
    assert LS.strip_think("SELECT 2") == "SELECT 2"
    assert LS.merge_eos_ids("gemma", None, 2, [], None) == [1, 106, 50, 2]
    assert LS.merge_eos_ids("causal", None, 7, [151645, None], 7) == [151645]  # 7 is unk, skipped
    assert LS.merge_eos_ids("gemma", [5], 2, [], None) == [5, 2]
    assert LS.cut_new_tokens([5, 6, 1, 9], 10, {1}) == [5, 6, 1]
    assert LS.cut_new_tokens([5, 6, 7, 8], 2, {1}) == [5, 6]


def test_model_class_selection_by_config() -> None:
    assert LS.select_model_classes({"model_type": "gemma4"})["tokenizer"] == "AutoProcessor"
    causal = LS.select_model_classes({"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]})
    assert causal["tokenizer"] == "AutoTokenizer" and causal["model_chain"] == ["AutoModelForCausalLM"]
    cond = LS.select_model_classes({"model_type": "qwen3_5", "architectures": ["Qwen3_5ForConditionalGeneration"]})
    assert cond["model_chain"][0] == "AutoModelForImageTextToText"
    assert LS.select_model_classes(None)["family"] == "legacy"


class _FakeOps:
    """Deterministic 'model': the next token depends only on the ids. bad=True corrupts the cached path."""

    def __init__(self, bad: bool = False):
        self.bad = bad

    @staticmethod
    def _gen(ids, max_new):
        seed = sum(ids) % 97
        return [(seed + k) % 50 + 2 for k in range(max_new)]

    def make_entry(self, prefix_ids):
        return tuple(prefix_ids)

    def plain_batch(self, seqs, max_new):
        return [self._gen(s, max_new) for s in seqs]

    def single(self, ids, max_new, entry):
        out = self._gen(ids, max_new)
        if self.bad and entry is not None:
            out[0] += 1
        return out

    def batch_prefix(self, seqs, max_new, entry):
        return None  # forces the one-by-one path


def test_prefix_self_check_passes_for_matching_cache_and_fails_for_mismatch() -> None:
    shared = list(range(10, 30))
    a, b = shared + [1, 2, 3], shared + [4, 5]
    assert LS.prefix_self_check(_FakeOps(), [a, b], len(shared), {1}) is None
    assert LS.prefix_self_check(_FakeOps(bad=True), [a, b], len(shared), {1}) == "mismatch"


def test_prefix_cache_reuses_entry_for_a_shared_prefix() -> None:
    store = LS.PrefixCacheStore(lambda ids: tuple(ids), slots=2, min_tokens=2)
    shared = [7, 8, 9, 10]
    first, reused = store.acquire(shared + [11], boundary=4)
    assert reused == 0 and store.created == 1
    second, reused = store.acquire(shared + [12, 13], boundary=None)
    assert reused == 4 and second is first and store.hits == 1


def test_micro_batcher_returns_each_request_its_own_rows() -> None:
    def generate_batch(seqs, max_new, boundaries):
        # Each row: first token = 10 + the prompt's first id, then eos (1), then junk that must be cut.
        return [[10 + s[0], 1, 99] for s in seqs]

    batcher = LS.MicroBatcher(generate_batch, threading.Lock(), {1}, max_batch=4, wait_ms=20)
    batcher.start()
    results: dict[int, list[int]] = {}

    def submit(i):
        results[i] = batcher.submit([i, 5, 6], max_tokens=8)[0]

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert results == {i: [10 + i, 1] for i in range(4)}


def _gateway(tmp_path: Path, fake: Path, monkeypatch, worker_ports_extra: list[int] | None = None):
    token = "gw-token"
    worker_port = _free_port()
    env = dict(os.environ, DATAMATE_SERVER_TOKEN=token)
    worker = subprocess.Popen([sys.executable, str(fake), "--port", str(worker_port)], env=env)
    for _ in range(100):
        if LS.probe_models(worker_port, token):
            break
        time.sleep(0.1)
    stop = threading.Event()
    gateway_port = _free_port()
    ports = list(worker_ports_extra or []) + [worker_port]
    httpd, state = LS.start_gateway(gateway_port, ports, token, "served/name", stop)
    return SimpleNamespace(worker=worker, httpd=httpd, state=state, stop=stop, token=token,
                           base=f"http://127.0.0.1:{gateway_port}")


def test_gateway_auth_completion_stream_and_shutdown(tmp_path, fake_server, monkeypatch) -> None:
    gw = _gateway(tmp_path, fake_server, monkeypatch)
    try:
        status, body = _request(gw.base + "/v1/models", gw.token)
        assert status == 200
        assert json.loads(body)["data"][0]["id"] == "served/name"
        assert _request(gw.base + "/v1/models", "bad-token")[0] == 401

        payload = {"messages": [{"role": "user", "content": "SELECT 1;"}], "max_tokens": 8}
        status, body = _request(gw.base + "/v1/chat/completions", gw.token, "POST", payload)
        assert status == 200
        assert json.loads(body)["choices"][0]["message"]["content"] == "ABSTAIN"

        status, body = _request(gw.base + "/v1/chat/completions", gw.token, "POST", {**payload, "stream": True})
        assert status == 200
        text = body.decode("utf-8")
        assert '"content": "ABSTAIN"' in text and text.rstrip().endswith("data: [DONE]")

        assert _request(gw.base + "/admin/shutdown", "bad-token", "POST", {})[0] == 401
        assert not gw.stop.is_set()
        status, body = _request(gw.base + "/admin/shutdown", gw.token, "POST", {})
        assert status == 200 and json.loads(body) == {"ok": True}
        assert gw.stop.is_set()
    finally:
        gw.httpd.shutdown()
        gw.httpd.server_close()
        gw.worker.kill()
        gw.worker.wait(timeout=10)


def test_gateway_drops_a_refusing_worker_and_uses_the_next(tmp_path, fake_server, monkeypatch) -> None:
    dead_port = _free_port()  # nothing listens here
    gw = _gateway(tmp_path, fake_server, monkeypatch, worker_ports_extra=[dead_port])
    try:
        payload = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 4}
        for _ in range(3):
            status, body = _request(gw.base + "/v1/chat/completions", gw.token, "POST", payload)
            assert status == 200, body
        assert dead_port not in gw.state.pool.live()
    finally:
        gw.httpd.shutdown()
        gw.httpd.server_close()
        gw.worker.kill()
        gw.worker.wait(timeout=10)


@pytest.mark.parametrize("end", ["shutdown", "worker_dies"])
def test_orchestrator_starts_gateway_and_stops_on_shutdown_or_worker_loss(tmp_path, fake_server, monkeypatch, end) -> None:
    """serve_main with the GPU worker replaced by the fake server: wait for health, serve, then stop."""
    spawned: list = []

    def fake_spawn(args, gpu, token):
        port = _free_port()
        proc = subprocess.Popen([sys.executable, str(fake_server), "--port", str(port)],
                                env=dict(os.environ, DATAMATE_SERVER_TOKEN=token))
        spawned.append(proc)
        return LS.WorkerProc(gpu=gpu, port=port, proc=proc)

    monkeypatch.setattr(LS, "spawn_worker", fake_spawn)
    gateway_port = _free_port()
    args = LS.parse_args(["--model-dir", str(tmp_path), "--port", str(gateway_port), "--gpus", "0",
                          "--token", "orch-token", "--startup-timeout", "60"])

    def driver() -> None:
        for _ in range(300):
            if LS.probe_models(gateway_port, "orch-token"):
                break
            time.sleep(0.1)
        if end == "shutdown":
            _request(f"http://127.0.0.1:{gateway_port}/admin/shutdown", "orch-token", "POST", {})
        else:
            spawned[0].kill()

    thread = threading.Thread(target=driver, daemon=True)
    thread.start()
    rc = LS.serve_main(args)
    thread.join(timeout=30)

    assert rc == (0 if end == "shutdown" else 1)
    assert spawned and spawned[0].poll() is not None  # every worker is stopped on the way out
    assert not _port_open(gateway_port)
