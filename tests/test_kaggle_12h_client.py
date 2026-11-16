from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from scripts.kaggle_12h_client import cmd_eval


def test_cmd_eval_worker_selection_tier2():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run:
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": 2, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}
        ret = cmd_eval(cfg, limit=50)

        assert ret == 0
        assert mock_run.called
        run_args, run_kwargs = mock_run.call_args_list[0]
        cmd = run_args[0]
        env = run_kwargs["env"]

        # Check workers for Tier 2 defaults to 1
        workers_idx = cmd.index("--workers")
        assert cmd[workers_idx + 1] == "1"

        # Check pacing and timeouts
        assert "--no-fair-pacing" in cmd
        q_timeout_idx = cmd.index("--question-timeout")
        assert cmd[q_timeout_idx + 1] == "300"
        timeout_idx = cmd.index("--request-timeout")
        assert cmd[timeout_idx + 1] == "300"
        assert env["BIZREASON_TIMEOUT"] == "300"

        # Check max output tokens, calls, repairs, and compact context characters
        out_tokens_idx = cmd.index("--max-output-tokens")
        assert cmd[out_tokens_idx + 1] in ("400", "768")
        calls_idx = cmd.index("--max-model-calls")
        assert cmd[calls_idx + 1] == "1"
        repairs_idx = cmd.index("--max-repairs")
        assert cmd[repairs_idx + 1] == "0"
        ctx_chars_idx = cmd.index("--context-characters")
        assert cmd[ctx_chars_idx + 1] == "11000"
        assert env["BIZREASON_CONTEXT_CHARS"] == "11000"


def test_cmd_eval_worker_selection_tier1_limit50():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run:
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": 1, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}
        ret = cmd_eval(cfg, limit=50)

        assert ret == 0
        cmd = mock_run.call_args_list[0][0][0]
        workers_idx = cmd.index("--workers")
        assert cmd[workers_idx + 1] == "6"
        assert "--no-fair-pacing" in cmd
        assert "--context-characters" in cmd


def test_cmd_eval_worker_selection_tier1_limit1000():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run:
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": 1, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}
        ret = cmd_eval(cfg, limit=1000)

        assert ret == 0
        cmd = mock_run.call_args_list[0][0][0]
        workers_idx = cmd.index("--workers")
        assert cmd[workers_idx + 1] == "8"


def test_cmd_eval_workers_override():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run:
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": 2, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}
        ret = cmd_eval(cfg, limit=50, workers_override=5)

        assert ret == 0
        cmd = mock_run.call_args_list[0][0][0]
        workers_idx = cmd.index("--workers")
        assert cmd[workers_idx + 1] == "5"


def test_cmd_eval_worker_selection_nf4_dp():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run:
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": "nf4_dp", "gpu_count": 2, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}
        ret = cmd_eval(cfg, limit=50)

        assert ret == 0
        run_args, run_kwargs = mock_run.call_args_list[0]
        cmd = run_args[0]
        env = run_kwargs["env"]

        # Check workers for nf4_dp defaults to 4
        workers_idx = cmd.index("--workers")
        assert cmd[workers_idx + 1] == "4"

        # Check context characters defaults to 30000
        ctx_chars_idx = cmd.index("--context-characters")
        assert cmd[ctx_chars_idx + 1] == "30000"
        assert env["BIZREASON_CONTEXT_CHARS"] == "30000"

        # Check max output tokens defaults to 768
        out_tokens_idx = cmd.index("--max-output-tokens")
        assert cmd[out_tokens_idx + 1] in ("320", "768")


def test_cmd_eval_scoring_subset_predicted():
    with patch("scripts.kaggle_12h_client.resolve_live_base_url") as mock_resolve, \
         patch("scripts.kaggle_12h_client.subprocess.run") as mock_run, \
         patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.read_text", return_value="{}"):
        mock_resolve.return_value = ("https://test-cf.trycloudflare.com/v1", {"tier": "nf4_dp", "gpu_count": 2, "base_url": "https://test-cf.trycloudflare.com/v1"})
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        cfg = {"bearer_token": "mock-token", "model": "test-model"}

        # Limit 20 (< 3000) should pass --subset-predicted to score command
        ret = cmd_eval(cfg, limit=20)
        assert ret == 0
        assert mock_run.call_count == 2
        score_cmd = mock_run.call_args_list[1][0][0]
        assert "score" in score_cmd
        assert "--subset-predicted" in score_cmd

        # Limit 3000 should NOT pass --subset-predicted
        mock_run.reset_mock()
        ret3000 = cmd_eval(cfg, limit=3000)
        assert ret3000 == 0
        assert mock_run.call_count == 2
        score_cmd_3000 = mock_run.call_args_list[1][0][0]
        assert "score" in score_cmd_3000
        assert "--subset-predicted" not in score_cmd_3000


def test_cli_flags_parsing():
    from scripts.kaggle_12h_client import main

    with patch("scripts.kaggle_12h_client.load_connection_config") as mock_cfg, \
         patch("scripts.kaggle_12h_client.get_authenticated_api") as mock_api, \
         patch("scripts.kaggle_12h_client.cmd_eval") as mock_eval:
        mock_cfg.return_value = {"bearer_token": "tok"}
        mock_api.return_value = MagicMock()
        mock_eval.return_value = 0

        # Test --eval-limit 10 --workers 3
        test_args = ["kaggle_12h_client.py", "--eval-limit", "10", "--workers", "3"]
        with patch.object(sys, "argv", test_args):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            mock_eval.assert_called_with(mock_cfg.return_value, limit=10, workers_override=3)

        # Test --eval-20
        test_args_20 = ["kaggle_12h_client.py", "--eval-20"]
        with patch.object(sys, "argv", test_args_20):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            mock_eval.assert_called_with(mock_cfg.return_value, limit=20, workers_override=None)
