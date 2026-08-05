"""Tests for the FM-Index implementation."""

import pytest

from fmindex import FMIndex, build_suffix_array, bwt_from_suffix_array, inverse_bwt


# --------------------------------------------------------------------------- #
# Suffix array tests                                                         #
# --------------------------------------------------------------------------- #


class TestSuffixArray:
    def test_empty_string(self):
        assert build_suffix_array("") == []

    def test_single_char(self):
        assert build_suffix_array("a") == [0]

    def test_two_chars(self):
        sa = build_suffix_array("ab")
        assert sa == [0, 1]

    def test_repeated_char(self):
        sa = build_suffix_array("aaa")
        assert sa == [2, 1, 0]

    def test_banana(self):
        sa = build_suffix_array("banana$")
        expected = [6, 5, 3, 1, 0, 4, 2]
        assert sa == expected

    def test_abracadabra(self):
        text = "abracadabra$"
        sa = build_suffix_array(text)
        assert len(sa) == len(text)
        assert sorted(sa) == list(range(len(text)))


# --------------------------------------------------------------------------- #
# BWT tests                                                                  #
# --------------------------------------------------------------------------- #


class TestBWT:
    def test_empty_string(self):
        bwt = bwt_from_suffix_array("", [])
        assert bwt == "$"

    def test_single_char(self):
        bwt = bwt_from_suffix_array("a", [1, 0])
        assert bwt == "a$"

    def test_banana(self):
        text = "banana"
        sa = build_suffix_array(text + "$")
        bwt = bwt_from_suffix_array(text, sa)
        assert bwt == "annb$aa"

    def test_roundtrip(self):
        text = "abracadabra"
        sa = build_suffix_array(text + "$")
        bwt = bwt_from_suffix_array(text, sa)
        assert inverse_bwt(bwt) == text

    def test_sentinel_in_text_raises(self):
        with pytest.raises(ValueError):
            bwt_from_suffix_array("a$b", [0, 1, 2, 3])


# --------------------------------------------------------------------------- #
# Inverse BWT tests                                                          #
# --------------------------------------------------------------------------- #


class TestInverseBWT:
    def test_empty(self):
        assert inverse_bwt("") == ""

    def test_banana_roundtrip(self):
        bwt = "annb$aa"
        assert inverse_bwt(bwt) == "banana"

    def test_abracadabra_roundtrip(self):
        text = "abracadabra"
        sa = build_suffix_array(text + "$")
        bwt = bwt_from_suffix_array(text, sa)
        assert inverse_bwt(bwt) == text


# --------------------------------------------------------------------------- #
# FM-Index count tests                                                       #
# --------------------------------------------------------------------------- #


class TestCount:
    def test_empty_text(self):
        idx = FMIndex("")
        assert idx.count("a") == 0

    def test_single_char_match(self):
        idx = FMIndex("a")
        assert idx.count("a") == 1

    def test_single_char_no_match(self):
        idx = FMIndex("a")
        assert idx.count("b") == 0

    def test_banana_ana(self):
        idx = FMIndex("banana")
        assert idx.count("ana") == 2

    def test_banana_ban(self):
        idx = FMIndex("banana")
        assert idx.count("ban") == 1

    def test_banana_na(self):
        idx = FMIndex("banana")
        assert idx.count("na") == 2

    def test_banana_a(self):
        idx = FMIndex("banana")
        assert idx.count("a") == 3

    def test_banana_n(self):
        idx = FMIndex("banana")
        assert idx.count("n") == 2

    def test_banana_x(self):
        idx = FMIndex("banana")
        assert idx.count("x") == 0

    def test_empty_pattern(self):
        idx = FMIndex("banana")
        assert idx.count("") == idx.text_length() + 1

    def test_overlapping_patterns(self):
        idx = FMIndex("aaaa")
        assert idx.count("aa") == 3

    def test_long_text(self):
        text = "the quick brown fox jumps over the lazy dog"
        idx = FMIndex(text)
        assert idx.count("the") == 2
        assert idx.count("quick") == 1
        assert idx.count("fox") == 1
        assert idx.count("cat") == 0


# --------------------------------------------------------------------------- #
# FM-Index locate tests                                                      #
# --------------------------------------------------------------------------- #


class TestLocate:
    def test_banana_ana(self):
        idx = FMIndex("banana")
        positions = idx.locate("ana")
        assert sorted(positions) == [1, 3]

    def test_banana_ban(self):
        idx = FMIndex("banana")
        positions = idx.locate("ban")
        assert positions == [0]

    def test_banana_na(self):
        idx = FMIndex("banana")
        positions = idx.locate("na")
        assert sorted(positions) == [2, 4]

    def test_banana_a(self):
        idx = FMIndex("banana")
        positions = idx.locate("a")
        assert sorted(positions) == [1, 3, 5]

    def test_no_match(self):
        idx = FMIndex("banana")
        assert idx.locate("xyz") == []

    def test_long_text(self):
        text = "abracadabra"
        idx = FMIndex(text)
        positions = idx.locate("abra")
        assert sorted(positions) == [0, 7]


# --------------------------------------------------------------------------- #
# FM-Index extract tests                                                     #
# --------------------------------------------------------------------------- #


class TestExtract:
    def test_banana_position_0(self):
        idx = FMIndex("banana")
        assert idx.extract(0, 6) == "banana"

    def test_banana_position_1(self):
        idx = FMIndex("banana")
        assert idx.extract(1, 5) == "anana"

    def test_banana_position_2(self):
        idx = FMIndex("banana")
        assert idx.extract(2, 4) == "nana"

    def test_banana_position_5(self):
        idx = FMIndex("banana")
        assert idx.extract(5, 1) == "a"

    def test_extract_zero_length(self):
        idx = FMIndex("banana")
        assert idx.extract(0, 0) == ""

    def test_extract_out_of_range(self):
        idx = FMIndex("banana")
        with pytest.raises(ValueError):
            idx.extract(100, 1)

    def test_extract_negative_length(self):
        idx = FMIndex("banana")
        assert idx.extract(0, -1) == ""


# --------------------------------------------------------------------------- #
# FM-Index count_and_range tests                                             #
# --------------------------------------------------------------------------- #


class TestCountAndRange:
    def test_banana_ana(self):
        idx = FMIndex("banana")
        cnt, sp, ep = idx.count_and_range("ana")
        assert cnt == 2
        assert 0 <= sp < ep <= idx.n

    def test_no_match(self):
        idx = FMIndex("banana")
        cnt, sp, ep = idx.count_and_range("xyz")
        assert cnt == 0


# --------------------------------------------------------------------------- #
# LF-mapping tests                                                           #
# --------------------------------------------------------------------------- #


class TestLFMapping:
    def test_lf_valid(self):
        idx = FMIndex("banana")
        for i in range(idx.n):
            lf_pos = idx.lf(i)
            assert 0 <= lf_pos < idx.n

    def test_lf_consistency(self):
        idx = FMIndex("banana")
        for i in range(idx.n):
            bwt_char = idx.bwt[i]
            sa_i = idx._sa_at(i)
            expected = idx.text[sa_i - 1] if sa_i > 0 else idx.text[idx.n - 1]
            assert bwt_char == expected


# --------------------------------------------------------------------------- #
# Rank tests                                                                 #
# --------------------------------------------------------------------------- #


class TestRank:
    def test_rank_monotonic(self):
        idx = FMIndex("banana")
        for ch in idx.sigma:
            prev = 0
            for i in range(idx.n + 1):
                r = idx.rank(ch, i)
                assert r >= prev
                prev = r

    def test_rank_total(self):
        idx = FMIndex("banana")
        for ch in idx.sigma:
            assert idx.rank(ch, idx.n) == idx.text.count(ch)