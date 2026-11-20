from time import perf_counter_ns

from app.codec import digest
from app.errors import DomainError
from app.pipeline import evaluate
from app.records import token
from app.source import validate
from app.workflow import TABLES
from .delta import build_delta, merge_sources
from .manifest import create


def execute(ledger, source):
    start = perf_counter_ns()
    result = evaluate(source)
    manifest = create(source, result, (perf_counter_ns() - start) // 1_000_000)
    snapshot = {"id": manifest["id"], "manifest": manifest, "result": result}
    saved, reused = ledger.save(manifest["id"], source, snapshot)
    if saved["manifest"]["digest"] != manifest["digest"]:
        raise DomainError("REPLAY_MISMATCH", "重算摘要不一致，请复核规则版本", 409)
    return {**saved, "reused": reused}


def amend(ledger, ident, supplement):
    """把迟到的补扫记录并入已签收基线，沿同一条核量链重估并形成交班差额。"""
    base_source, base = ledger.read(ident)
    if not base["signature"]:
        raise DomainError("BASE_UNSIGNED", "只能以已签收凭证为基线并入补扫", 409)
    merged, added = merge_sources(base_source, validate(supplement, TABLES))
    start = perf_counter_ns()
    result = evaluate(merged)
    amendment = {"base": ident, "batch": supplement["batch"], "at": supplement["at"], "added": added}
    delta = build_delta(base, result, amendment)
    manifest = create(merged, result, (perf_counter_ns() - start) // 1_000_000,
                      {**amendment, "verified": delta["verified"]})
    snapshot = {"id": manifest["id"], "manifest": manifest, "result": result}
    saved, reused = ledger.save(manifest["id"], merged, snapshot)
    if saved["manifest"]["digest"] != manifest["digest"]:
        raise DomainError("REPLAY_MISMATCH", "重算摘要不一致，请复核规则版本", 409)
    return {**saved, "reused": reused, "delta": {"id": manifest["id"], **delta}}


def delta(ledger, ident):
    _, snapshot = ledger.read(ident)
    amendment = snapshot["manifest"].get("amendment")
    if not amendment:
        raise DomainError("NOT_AMENDMENT", "此运行没有补扫修订")
    _, base = ledger.read(amendment["base"])
    return {"id": ident, **build_delta(base, snapshot["result"], amendment)}


def rerun(ledger, ident):
    source, previous = ledger.read(ident)
    result = evaluate(source)
    if digest(result) != previous["manifest"]["digest"]:
        raise DomainError("REPLAY_MISMATCH", "重跑结果与归档不一致", 409)
    return {**previous, "reused": True}


def sign(ledger, ident, body):
    _, previous = ledger.read(ident)
    if not token(body.get("reviewer")):
        raise DomainError("REVIEWER_REQUIRED", "请输入签收人")
    if previous["manifest"]["digest"] != body.get("digest"):
        raise DomainError("STALE_DIGEST", "摘要已变化，请重新打开凭证", 409)
    amendment = previous["manifest"].get("amendment")
    if amendment and not amendment["verified"]:
        raise DomainError("SUPPLEMENT_UNVERIFIED", "补扫记录未与线路登记核清，差额不得签收", 409)
    receipt = {"reviewer": body["reviewer"].strip(), "digest": body["digest"],
               "id": ident, "balance": previous["result"]["reconcile"]["status"]}
    return ledger.sign(ident, receipt)
