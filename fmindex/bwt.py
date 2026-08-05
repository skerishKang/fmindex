"""Burrows-Wheeler Transform and suffix array construction."""

from typing import List, Tuple


def build_suffix_array(text: str) -> List[int]:
    """Build suffix array using prefix-doubling in O(n log^2 n).

    Args:
        text: Input string.

    Returns:
        Suffix array — list of starting positions of sorted suffixes.
    """
    n = len(text)
    if n == 0:
        return []

    sa = list(range(n))
    rank = [ord(c) for c in text]
    tmp = [0] * n

    k = 1
    while k < n:
        def key(i: int) -> Tuple[int, int]:
            return (rank[i], rank[i + k] if i + k < n else -1)

        sa.sort(key=key)

        tmp[sa[0]] = 0
        for i in range(1, n):
            tmp[sa[i]] = tmp[sa[i - 1]]
            if key(sa[i]) != key(sa[i - 1]):
                tmp[sa[i]] += 1

        rank, tmp = tmp, rank

        if rank[sa[-1]] == n - 1:
            break

        k <<= 1

    return sa


def bwt_from_suffix_array(text: str, sa: List[int]) -> str:
    """Compute Burrows-Wheeler Transform from text and its suffix array.

    Appends a sentinel '$' (lexicographically smallest) if not present,
    then derives BWT from the suffix array.

    Args:
        text: Input string (without sentinel).
        sa: Suffix array of the text with sentinel appended.

    Returns:
        The BWT string (last column of sorted rotations).
    """
    sentinel = "$"
    if sentinel in text:
        raise ValueError("Input text must not contain the sentinel character '$'")

    full_text = text + sentinel
    full_n = len(full_text)

    if len(sa) != full_n:
        full_sa = build_suffix_array(full_text)
    else:
        full_sa = sa

    if full_n == 0:
        return ""

    bwt = []
    for i in range(full_n):
        pos = full_sa[i]
        if pos == 0:
            bwt.append(full_text[full_n - 1])
        else:
            bwt.append(full_text[pos - 1])

    return "".join(bwt)


def inverse_bwt(bwt: str) -> str:
    """Reconstruct the original text from its BWT.

    Uses the standard inverse BWT algorithm (LF-mapping).

    Args:
        bwt: The Burrows-Wheeler Transform string (must include sentinel '$').

    Returns:
        The original text without the sentinel.
    """
    n = len(bwt)
    if n == 0:
        return ""

    table = [""] * n
    for _ in range(n):
        table = sorted(bwt[i] + table[i] for i in range(n))

    for row in table:
        if row.endswith("$"):
            return row.rstrip("$")

    raise ValueError("BWT does not contain sentinel '$' — cannot reconstruct")


def build_suffix_array_from_text(text: str) -> Tuple[str, List[int]]:
    """Build BWT and suffix array from raw text.

    Appends sentinel, computes suffix array and BWT.

    Returns:
        (bwt_string, suffix_array) where sa corresponds to text+sentinel.
    """
    full_text = text + "$"
    sa = build_suffix_array(full_text)
    bwt = bwt_from_suffix_array(text, sa)
    return bwt, sa