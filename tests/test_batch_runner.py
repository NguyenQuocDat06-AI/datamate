from concurrent.futures import ThreadPoolExecutor

import pytest

from bizreason.config import Settings
from bizreason.contracts import Prediction, Question
from bizreason.llm import Usage
from bizreason.temporal import TemporalResolver
from scripts.run_public_test import Checkpoints, Experiment, write_predictions


def test_checkpoints_resume_and_preserve_question_order(workspace_tmp):
    questions = [
        Question(id=f"q{i}", business_id="b", question="count", as_of_date="2026-03-04")
        for i in range(3)
    ]
    store = Checkpoints(workspace_tmp / "run", "signature", {"model": "test"})
    store.save(Prediction(id="q2", type="number", answer=2), {"done": True})
    store.save(Prediction(id="q0", type="number", answer=0), {"done": True})
    resumed = store.load(questions)
    assert set(resumed) == {"q0", "q2"}
    output = workspace_tmp / "result.jsonl"
    write_predictions(output, questions, resumed)
    from bizreason.utils import load_jsonl

    assert [item["id"] for item in load_jsonl(output)] == ["q0", "q2"]
    with pytest.raises(ValueError, match="changed"):
        Checkpoints(workspace_tmp / "run", "different", {"model": "other"})


def test_parallel_questions_have_separate_clients_and_real_sql_results(
    monkeypatch, business, rules, plan_factory
):
    table = next(table for table in business.tables if table.grain == "order_line")
    plan = plan_factory(
        "generic_sql", rule_refs=[rules.records[0].id], sql=f'SELECT COUNT(*) FROM "{table.name}"'
    )

    class StubClient:
        def __init__(self, settings):
            self.usage = Usage()

        def generate_json(self, system, prompt, *, timeout):
            self.usage.calls += 1
            return plan.model_dump(mode="json")

        def close(self):
            pass

    monkeypatch.setattr("scripts.run_public_test.TrammayClient", StubClient)
    experiment = Experiment(
        {business.business_id: business}, rules, TemporalResolver(), Settings(), "api"
    )
    questions = [
        Question(
            id=f"q{i}", business_id=business.business_id, question="count", as_of_date="2026-03-04"
        )
        for i in range(2)
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(experiment.solve, questions))
        assert [result[0].answer for result in results] == [6, 6]
        assert all(result[2] is None for result in results)
        assert len(experiment.clients) == 2
        assert experiment.usage()[0] == 2
    finally:
        experiment.close()
