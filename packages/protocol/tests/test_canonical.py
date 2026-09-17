"""Canonical bytes, hashing and manifest signature."""

from __future__ import annotations

import math

import pytest
from autolab_protocol import (
    ManifestV1,
    PlanSpecV1,
    canonical_bytes,
    manifest_signature,
    payload_hash,
    sha256_file,
    spec_hash,
    verify_manifest_signature,
)
from pydantic import ValidationError


def test_canonical_bytes_sorts_keys_and_compacts():
    a = canonical_bytes({"b": 1, "a": {"d": 2, "c": [3, 4]}})
    b = canonical_bytes({"a": {"c": [3, 4], "d": 2}, "b": 1})
    assert a == b == b'{"a":{"c":[3,4],"d":2},"b":1}'


def test_canonical_bytes_rejects_nan():
    with pytest.raises(ValueError):
        canonical_bytes({"x": math.nan})


def test_spec_hash_stable_for_same_input(plan_example):
    spec_a = PlanSpecV1.model_validate(plan_example)
    spec_b = PlanSpecV1.model_validate(plan_example)
    assert spec_hash(spec_a) == spec_hash(spec_b)
    # round-trip through dump/parse must not change the hash
    spec_c = PlanSpecV1.model_validate(spec_a.model_dump(mode="json"))
    assert spec_hash(spec_c) == spec_hash(spec_a)


def test_spec_hash_changes_when_semantics_change(plan_example):
    spec_a = PlanSpecV1.model_validate(plan_example)
    changed = {**plan_example, "hypothesis": "a different hypothesis"}
    spec_b = PlanSpecV1.model_validate(changed)
    assert spec_hash(spec_a) != spec_hash(spec_b)


def test_payload_hash_deterministic(manifest_example):
    manifest = ManifestV1.model_validate(manifest_example)
    again = ManifestV1.model_validate(manifest_example)
    assert payload_hash(manifest) == payload_hash(again)


def test_manifest_signature_roundtrip(manifest_example):
    manifest = ManifestV1.model_validate(manifest_example)
    signature = manifest_signature("unit-test-key", manifest)
    assert verify_manifest_signature("unit-test-key", manifest, signature)
    assert not verify_manifest_signature("other-key", manifest, signature)
    tampered = manifest.model_copy(deep=True)
    tampered.execution.timeout_seconds += 1
    assert not verify_manifest_signature("unit-test-key", tampered, signature)
    assert not verify_manifest_signature("unit-test-key", manifest, "")


def test_manifest_signature_domain_separated(manifest_example):
    manifest = ManifestV1.model_validate(manifest_example)
    signature = manifest_signature("k", manifest)
    # signature covers more than the bare JCS bytes: different domain prefix -> different tag
    import hashlib
    import hmac as hmac_mod

    from autolab_protocol.canonical import model_canonical_bytes

    bare = hmac_mod.new(b"k", model_canonical_bytes(manifest), hashlib.sha256).hexdigest()
    assert bare != signature


def test_sha256_file(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello autolab\n")
    digest, size = sha256_file(target)
    import hashlib

    assert digest == hashlib.sha256(b"hello autolab\n").hexdigest()
    assert size == 14


def test_manifest_model_requires_utc_ish_datetime(manifest_example):
    broken = {**manifest_example, "created_at": "not-a-datetime"}
    with pytest.raises(ValidationError):
        ManifestV1.model_validate(broken)
