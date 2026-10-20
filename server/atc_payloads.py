"""Payload policies established from the pinned published source.

See docs/payload-guarantees.md for exact sources and remaining runtime evidence.
This table does not claim that a gateway acceptance test has run.
"""

SOURCE_SHA = "8433d22fdf86027d62a34e9923501775225813ad"
BUILD_ID = ("0.1.9", 2908, "8433d22f")
PAYLOAD_BUILDS = {BUILD_ID: {
    "work-items": "strict-version", "sessions": "strict-version",
    "attests": "immutable", "decision requests": "strict-version",
    "artifacts": "strict-version",
}}

WORK_ITEM_FIELDS = frozenset("""
    id title specRefName specRefSha256 isBug ownerUserId state failReason routingWakeId
    slateWakeId createdByUser createdBySession createdInTurnSeq createdContextKnown
    createdAt priority rowVersion
""".split())
SESSION_FIELDS = frozenset("""
    sessionKey displayName kind orderIndex isBuiltIn adopted ownerUserId origin spawnedBy
    topologyParent handle archetype overrides identityName identityRevision harness provider
    model thinkingLevel modelContext host clearedThroughSeq state createdAt updatedAt
    mechanicalStatus capabilities rowVersion
""".split())
DECISION_FIELDS = frozenset("""
    id kind raiserId raiserSessionKey ownerUserId assignmentId expecterSessionKey expecterUserId
    lineageRung effortGeneration deadlineWakeId raisedAt deadlineAt statuteName question options
    context status decision rationale ruledBy ruledAt consumedAt withdrawnBy withdrawnReason
    withdrawnAt askedOfRole answer answeredBy answeredAt rowVersion
""".split())
ARTIFACT_FIELDS = frozenset("""
    artifactId kind title description createdBySession workItemId parentSession originPath
    contentSha256 recordedMessageId recordedTurnEvidence state home createdAt updatedAt rowVersion
""".split())

CLASSES = {
    "work-items": {"work_item." + name for name in
                   ("created", "updated", "iceboxed", "reopened", "closed", "failed")},
    "sessions": {"session." + name for name in
                 ("spawned", "updated", "harness_changed", "retired")},
    "attests": {"attest.filed"},
    "decision requests": {"decision_request." + name for name in
                          ("opened", "updated", "answered", "returned", "ruled", "consumed",
                           "withdrawn", "superseded")},
    "artifacts": {"artifact.recorded", "artifact.archived", "artifact.released"},
}


def required_fields(resource, payload, attest_fields):
    if resource == "attests":
        return frozenset(attest_fields)
    if resource == "decision requests":
        # Operator rows use a different, open public projection. The closed R7
        # shape and trigger cover these three kinds; other kinds use a read.
        if payload.get("kind") not in {"statute", "agent", "effort"}:
            return None
        return DECISION_FIELDS | ({"returnedBy", "returnReason", "returnedAt"}
                                  if payload["kind"] == "agent" else set())
    return {"work-items": WORK_ITEM_FIELDS, "sessions": SESSION_FIELDS,
            "artifacts": ARTIFACT_FIELDS}.get(resource)


def policies_for(version):
    return PAYLOAD_BUILDS.get((version.get("version"), version.get("build"), version.get("sha")), {})
