from __future__ import annotations

import numpy as np
import pytest

from app.vector_index import VectorIndex

DIM = 8


def unit(*vals):
    v = np.zeros(DIM, dtype=np.float32)
    v[: len(vals)] = vals
    return v / np.linalg.norm(v)


@pytest.fixture
def idx():
    return VectorIndex(DIM, max_entries=100)


class TestSearch:
    def test_returns_best_match_in_partition_with_cosine_score(self, idx):
        idx.add("a", "p", unit(1, 0), expires_at=1e12)
        idx.add("b", "p", unit(1, 1), expires_at=1e12)
        (key, score), second = idx.search("p", unit(1, 0.1), now=0)[:2]
        assert key == "a" and score > second[1]
        assert score == pytest.approx(float(unit(1, 0) @ unit(1, 0.1)))

    def test_partitions_are_isolated(self, idx):
        idx.add("a", "p1", unit(1, 0), 1e12)
        assert idx.search("p2", unit(1, 0), now=0) == []

    def test_expired_entries_are_invisible(self, idx):
        idx.add("a", "p", unit(1), expires_at=100)
        assert idx.search("p", unit(1), now=99)
        assert idx.search("p", unit(1), now=101) == []

    def test_replacing_a_key_keeps_one_entry(self, idx):
        idx.add("a", "p", unit(1, 0), 1e12)
        idx.add("a", "p", unit(0, 1), 1e12)
        assert len(idx) == 1
        assert idx.search("p", unit(0, 1), now=0)[0][1] == pytest.approx(1.0)

    def test_wrong_dimension_is_rejected(self, idx):
        with pytest.raises(ValueError):
            idx.add("a", "p", np.ones(3, dtype=np.float32), 1e12)

    def test_empty_index(self, idx):
        assert idx.search("p", unit(1), now=0) == []


class TestLifecycle:
    def test_remove(self, idx):
        idx.add("a", "p", unit(1), 1e12)
        assert idx.remove("a") and not idx.remove("a")
        assert len(idx) == 0 and idx.search("p", unit(1), now=0) == []

    def test_remove_expired_reports_count(self, idx):
        idx.add("old", "p", unit(1), 50)
        idx.add("new", "p", unit(0, 1), 500)
        assert idx.remove_expired(now=100) == 1
        assert len(idx) == 1

    def test_flush(self, idx):
        idx.add("a", "p", unit(1), 1e12)
        assert idx.flush() == 1 and len(idx) == 0

    def test_capacity_evicts_oldest_first(self):
        small = VectorIndex(DIM, max_entries=3)
        for i in range(5):
            small.add(f"k{i}", "p", unit(1, i), 1e12)
        assert len(small) == 3
        keys = {k for k, _ in small.search("p", unit(1, 0), now=0, k=10)}
        assert keys == {"k2", "k3", "k4"}

    def test_grows_past_initial_capacity_and_compacts(self):
        big = VectorIndex(DIM, max_entries=10_000)
        for i in range(2500):
            big.add(f"k{i}", f"p{i % 3}", unit(1, i % 7), 1e12)
        for i in range(0, 2500, 2):
            big.remove(f"k{i}")
        for i in range(2500, 3600):
            big.add(f"k{i}", "p0", unit(1, 2), 1e12)
        assert len(big) == 1250 + 1100
        assert big.search("p0", unit(1, 2), now=0, k=1)


class TestPersistence:
    def test_roundtrip(self, idx, tmp_path):
        idx.add("a", "p", unit(1, 0), 1e12)
        idx.add("b", "q", unit(0, 1), 1e12)
        path = str(tmp_path / "v.npz")
        idx.save(path)
        fresh = VectorIndex(DIM)
        assert fresh.load(path, now=0) == 2
        assert fresh.search("p", unit(1, 0), now=0)[0][0] == "a"

    def test_expired_entries_are_dropped_on_load(self, idx, tmp_path):
        idx.add("a", "p", unit(1), 50)
        idx.add("b", "p", unit(0, 1), 500)
        path = str(tmp_path / "v.npz")
        idx.save(path)
        assert VectorIndex(DIM).load(path, now=100) == 1

    def test_missing_corrupt_or_mismatched_files_do_not_stop_startup(self, idx, tmp_path):
        assert idx.load(str(tmp_path / "nope.npz")) == 0
        bad = tmp_path / "bad.npz"
        bad.write_bytes(b"not an npz")
        assert idx.load(str(bad)) == 0
        other = VectorIndex(4)
        other.add("a", "p", np.ones(4, dtype=np.float32) / 2, 1e12)
        other.save(str(tmp_path / "d4.npz"))
        assert idx.load(str(tmp_path / "d4.npz")) == 0

    def test_save_is_atomic_no_temp_files_left(self, idx, tmp_path):
        idx.add("a", "p", unit(1), 1e12)
        idx.save(str(tmp_path / "v.npz"))
        assert [p.name for p in tmp_path.iterdir()] == ["v.npz"]

    def test_dirty_flag(self, idx, tmp_path):
        assert not idx.dirty
        idx.add("a", "p", unit(1), 1e12)
        assert idx.dirty
        idx.save(str(tmp_path / "v.npz"))
        assert not idx.dirty
