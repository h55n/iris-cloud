"""Ciphertext-envelope validation is independent of the hosted database."""

import pytest
from api.index import _valid_ct


def envelope(**changes):
    return dict(v=1, alg='AES-GCM', kdf='PBKDF2-SHA256', iter=600_000,
                salt='test-salt', iv='test-iv', ct='test-ciphertext', **changes)


@pytest.mark.parametrize('iterations', [None, {}, [], 'invalid', float('inf'), float('nan')])
def test_malformed_iteration_counts_are_rejected(iterations):
    data = envelope()
    data['iter'] = iterations
    assert _valid_ct(data) is False


@pytest.mark.parametrize('iterations', [100_000, 600_000, 2_000_000, '600000'])
def test_supported_iteration_counts_remain_valid(iterations):
    data = envelope()
    data['iter'] = iterations
    assert _valid_ct(data) is True


@pytest.mark.parametrize('iterations', [0, 99_999, 2_000_001, -1])
def test_out_of_range_iteration_counts_are_rejected(iterations):
    data = envelope()
    data['iter'] = iterations
    assert _valid_ct(data) is False


def test_wrong_envelope_types_and_oversized_ciphertext_remain_invalid():
    for value in [None, [], 'ciphertext', 42]:
        assert _valid_ct(value) is False
    data = envelope()
    data['ct'] = 'x' * 400_001
    assert _valid_ct(data) is False
