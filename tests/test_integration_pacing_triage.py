from __future__ import annotations

import copy
import json
import struct

import pytest

from bizreason.cli import compute_question_budget, main
from bizreason.contracts import AnswerType, OutputSpec, Question
from bizreason.formatting import SubmissionWriter
from bizreason.llm import Usage
from bizreason.model_ledger import count_safetensors_parameters, verify_model_ledger
from bizreason.pipeline import Solver
from bizreason.temporal import TemporalResolver
from bizreason.triage import TriageRouter


class MockPlannerModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.usage = Usage()
        self.prompts = []

    def generate_json(self, system, prompt, *, timeout):
        self.usage.calls += 1
        self.usage.tokens += 10
        self.prompts.append(prompt)
        return copy.deepcopy(self.responses.pop(0))


def test_solver_solve_to_submission_writer_rounding_preservation(business, rules, plan_factory, workspace_tmp):
    """End-to-end Solver.solve -> SubmissionWriter preserves rounding policy (money average rounded to whole dong)."""
    table = next(t for t in business.tables if t.grain == "order_line")
    plan = plan_factory(
        "generic_sql",
        rule_refs=[rules.records[0].id],
        sql=f'SELECT 125430.6666667 FROM "{table.name}" LIMIT 1',
        output=OutputSpec(type=AnswerType.NUMBER, shape="scalar", rounding="money"),
    )
    model = MockPlannerModel([plan.model_dump(mode="json")])
    question = Question(
        id="q_rounding_test_01",
        business_id=business.business_id,
        question="Doanh thu trung bình là bao nhiêu?",
        as_of_date="2026-03-04",
    )
    from bizreason.config import Settings

    solver = Solver(model, rules, TemporalResolver(), Settings(sql_timeout=10))
    prediction, trace = solver.solve(question, business)

    assert prediction.rounding == "money"
    # Money rounding policy quantizes 125430.6666667 to integer 125431
    assert prediction.answer == 125431

    out_file = workspace_tmp / "submission_rounding.jsonl"
    with SubmissionWriter(out_file) as writer:
        writer.write(prediction)

    records = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["id"] == "q_rounding_test_01"
    assert records[0]["type"] == "number"
    # 125430.6666667 rounded to money (whole dong via ROUND_HALF_UP) must equal 125431
    assert records[0]["answer"] == 125431


def test_triage_router_allows_unambiguous_whole_business_and_rejects_subgroups(business, rules):
    """TriageRouter allows unambiguous whole-business revenue (0 LLM calls) and strictly rejects queries with residual filters."""
    router = TriageRouter()
    temporal = TemporalResolver()

    # Case 1: Unambiguous whole-business question
    q_simple = Question(
        id="q_simple_rev",
        business_id=business.business_id,
        question="Doanh thu hôm qua là bao nhiêu?",
        as_of_date="2026-03-04",
    )
    plan_routed = router.route(q_simple, business, rules, temporal)
    assert plan_routed is not None
    assert plan_routed.metric == "sales_revenue"
    assert plan_routed.template_eligible is True

    # Solve with mock model that raises if called: 0 LLM calls required!
    from bizreason.config import Settings

    class ShouldNotBeCalledModel:
        def __init__(self):
            self.usage = Usage()

        def generate_json(self, system, prompt, *, timeout):
            raise AssertionError("Model backend should never be called when triage fast-path succeeds")

    solver = Solver(
        ShouldNotBeCalledModel(),
        rules,
        temporal,
        Settings(),
        triage_router=router,
    )
    pred, trace = solver.solve(q_simple, business)
    assert trace.model_calls == 0
    assert pred.type == AnswerType.NUMBER
    # 2026-03-03 (yesterday) completed order O3 has line_amount = 80000
    assert pred.answer == 80000

    # Case 2: Modified query with customer group / exclusion / branch cues
    q_filtered = Question(
        id="q_filtered_rev",
        business_id=business.business_id,
        question="Doanh thu hôm qua của khách sỉ không tính chi nhánh A",
        as_of_date="2026-03-04",
    )
    plan_rejected = router.route(q_filtered, business, rules, temporal)
    assert plan_rejected is None, "TriageRouter must reject questions containing subgroup or exclusion cues"


def test_compute_question_budget_pacing():
    """compute_question_budget prevents early question starvation on large test runs without clipping small test runs."""
    # 1000 questions, 6h total (21600s), configured_timeout = 900s
    budget_early = compute_question_budget(
        remaining=21600.0,
        questions_left=1000,
        configured_timeout=900.0,
        safety_reserve_ratio=0.02,
        burst_factor=4.0,
    )
    # usable = 21600 * 0.98 = 21168; fair_share = 21.168; burst = 84.672
    assert 84.0 < budget_early < 85.0
    assert budget_early < 900.0, "Early question must be dynamically capped to prevent 15-minute starvation"

    # Small run: 5 questions left, 3600s remaining, configured_timeout = 150s
    budget_small = compute_question_budget(
        remaining=3600.0,
        questions_left=5,
        configured_timeout=150.0,
    )
    # Dynamic cap is high (~2822s), so configured 150s is completely unaffected
    assert budget_small == 150.0

    # Last question gets all remaining time up to configured_timeout
    budget_last = compute_question_budget(remaining=45.0, questions_left=1, configured_timeout=150.0)
    assert budget_last == 45.0

    # Zero or negative remaining returns 0.0
    assert compute_question_budget(remaining=0.0, questions_left=10, configured_timeout=150.0) == 0.0
    assert compute_question_budget(remaining=-10.0, questions_left=10, configured_timeout=150.0) == 0.0


def test_verify_model_ledger_compliance_rules():
    """verify_model_ledger enforces parameter limits, integer counts, and pre-Oct-1 third-party cutoffs."""
    base_model_entry = {
        "model_id": "base_llm",
        "origin": "pretrained",
        "weights_revision": "v1.0",
        "weights_sha256": "sha256_base",
        "release_date": "2026-09-15",
        "licence": "Apache-2.0",
        "total_parameters_including_moe": 14_000_000_000,
        "quantization": "none",
        "runtime": "vllm",
        "purpose": "base reasoning",
    }

    # Case 1: Compliant self-finetuned LoRA released in Nov 2026 based on pre-Oct-1 base
    lora_entry = {
        "model_id": "adapter_lora",
        "origin": "self_finetuned",
        "weights_revision": "r1",
        "weights_sha256": "sha256_lora",
        "release_date": "2026-11-05",
        "base_model_release_date": "2026-09-15",
        "training_data_compliant": True,
        "licence": "internal",
        "total_parameters_including_moe": 200_000_000,
        "quantization": "none",
        "runtime": "vllm",
        "purpose": "planner adapter",
    }
    compliant_ledger = {
        "offline_models": [base_model_entry, lora_entry],
        "offline_total_parameters": 14_200_000_000,
    }
    violations = verify_model_ledger(compliant_ledger)
    assert violations == [], f"Expected compliant ledger, got: {violations}"

    # Case 2: Rejecting Oct-2026 third-party checkpoint
    post_cutoff_third_party = {
        **base_model_entry,
        "model_id": "illegal_third_party",
        "origin": "third_party",
        "release_date": "2026-10-15",
    }
    violations_post = verify_model_ledger({"offline_models": [post_cutoff_third_party]})
    assert any("THIRD_PARTY_RELEASE_AFTER_CUTOFF" in v for v in violations_post)

    # Case 3: Rejecting 14.8B + 0.3B > 15.0B
    m1 = {**base_model_entry, "model_id": "m1", "total_parameters_including_moe": 14_800_000_000}
    m2 = {**base_model_entry, "model_id": "m2", "total_parameters_including_moe": 300_000_000}
    violations_exceed = verify_model_ledger({"offline_models": [m1, m2]})
    assert any("EXCEEDS_MAX_PARAMETERS" in v for v in violations_exceed)

    # Case 4: Rejecting non-integer / string parameter counts
    invalid_params = {**base_model_entry, "total_parameters_including_moe": "14.8B"}
    violations_type = verify_model_ledger({"offline_models": [invalid_params]})
    assert any("INVALID_PARAMETER_COUNT" in v for v in violations_type)


def test_count_safetensors_parameters_pure_python(workspace_tmp):
    """count_safetensors_parameters accurately computes tensor shapes from binary 8-byte header and respects index maps."""
    # Create synthetic safetensors file
    header_dict = {
        "embedding.weight": {"dtype": "F16", "shape": [1000, 64], "data_offsets": [0, 128000]},
        "layer.0.weight": {"dtype": "F16", "shape": [64, 128], "data_offsets": [128000, 144384]},
        "__metadata__": {"format": "pt", "author": "test"},
    }
    header_json = json.dumps(header_dict).encode("utf-8")
    header_len = len(header_json)

    file_a = workspace_tmp / "model-00001-of-00002.safetensors"
    with file_a.open("wb") as f:
        f.write(struct.pack("<Q", header_len))
        f.write(header_json)
        f.write(b"\x00" * 100)

    # Parameters in file_a: 1000 * 64 + 64 * 128 = 64,000 + 8,192 = 72,192
    assert count_safetensors_parameters(file_a) == 72192

    # Test directory with index map
    shard_dir = workspace_tmp / "sharded_weights"
    shard_dir.mkdir()
    file_shard1 = shard_dir / "model-00001.safetensors"
    with file_shard1.open("wb") as f:
        f.write(struct.pack("<Q", header_len))
        f.write(header_json)
        f.write(b"\x00" * 10)

    header_dict2 = {
        "layer.1.weight": {"dtype": "F16", "shape": [128, 10], "data_offsets": [0, 2560]},
    }
    header_json2 = json.dumps(header_dict2).encode("utf-8")
    file_shard2 = shard_dir / "model-00002.safetensors"
    with file_shard2.open("wb") as f:
        f.write(struct.pack("<Q", len(header_json2)))
        f.write(header_json2)
        f.write(b"\x00" * 10)

    index_data = {
        "metadata": {"total_size": 200000},
        "weight_map": {
            "embedding.weight": "model-00001.safetensors",
            "layer.0.weight": "model-00001.safetensors",
            "layer.1.weight": "model-00002.safetensors",
        },
    }
    (shard_dir / "model.safetensors.index.json").write_text(json.dumps(index_data), encoding="utf-8")

    total_sharded = count_safetensors_parameters(shard_dir)
    assert total_sharded == 72192 + (128 * 10)


def test_cli_competition_mode_path_policy_and_isolation(workspace_tmp, rules):
    """CLI run in competition mode writes cleanly to external result directory and strictly refuses writing to data directory."""
    data_dir = workspace_tmp / "mock_data"
    data_dir.mkdir()
    (data_dir / "rulebook.md").write_text(rules.text, encoding="utf-8")
    (data_dir / "questions.jsonl").write_text(
        json.dumps({
            "id": "q_cli_test_1",
            "business_id": "test_biz",
            "question": "Doanh thu?",
            "as_of_date": "2026-03-04",
        }) + "\n",
        encoding="utf-8",
    )
    (data_dir / "data" / "test_biz").mkdir(parents=True)
    (data_dir / "data" / "test_biz" / "README.md").write_text("No data", encoding="utf-8")

    external_result_dir = workspace_tmp / "external_result"
    external_result_dir.mkdir()
    out_file = external_result_dir / "submission.jsonl"
    artifacts_dir = workspace_tmp / "artifacts_run"
    cache_dir = workspace_tmp / "cache_run"

    # Case A: External output allowed in competition mode
    cmd_args = [
        "run",
        "--data", str(data_dir),
        "--output", str(out_file),
        "--artifacts", str(artifacts_dir),
        "--cache", str(cache_dir),
        "--backend", "none",
        "--runtime-mode", "competition",
    ]
    main(cmd_args)
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8").strip()
    assert "q_cli_test_1" in content

    # Case B: Refusing to write inside data directory
    illegal_out = data_dir / "illegal_submission.jsonl"
    illegal_cmd = [
        "run",
        "--data", str(data_dir),
        "--output", str(illegal_out),
        "--artifacts", str(artifacts_dir),
        "--cache", str(cache_dir),
        "--backend", "none",
        "--runtime-mode", "competition",
    ]
    with pytest.raises(SystemExit) as excinfo:
        main(illegal_cmd)
    assert excinfo.value.code == 2, "Writing inside data directory must be rejected by PathPolicy"
