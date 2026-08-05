"""FM-Index: Full-text index in Minute space.

Core data structure supporting pattern counting, locating, and
substring extraction over the Burrows-Wheeler Transform.
"""

from typing import Dict, List, Optional, Tuple


class FMIndex:
    """FM-Index built on top of the Burrows-Wheeler Transform.

    Attributes:
        text: Original text with sentinel '$' appended.
        bwt: Burrows-Wheeler Transform of the text.
        sa: Suffix array (full, for locate/extract).
        n: Length of the text including sentinel.
        sigma: Sorted list of distinct characters in the text.
        C: C[c] = number of characters lexicographically smaller than c.
        Occ: Occ[c][i] = number of occurrences of c in bwt[0:i].
        sample_rate: Interval for SA sampling (every k-th entry).
        sa_sample: Dictionary mapping sampled SA positions to SA values.
    """

    def __init__(
        self,
        text: str,
        sa: Optional[List[int]] = None,
        bwt: Optional[str] = None,
        sample_rate: int = 16,
    ):
        """Build the FM-Index from text.

        Args:
            text: Input string (sentinel '$' will be appended if absent).
            sa: Optional precomputed suffix array for text+'$'.
            bwt: Optional precomputed BWT string.
            sample_rate: Sampling interval for suffix array (powers of 2 recommended).
        """
        if "$" in text and not text.endswith("$"):
            raise ValueError("Sentinel '$' must appear only at the end")

        self.text = text if text.endswith("$") else text + "$"
        self.n = len(self.text)
        self.sample_rate = sample_rate

        if bwt is not None:
            self.bwt = bwt
        else:
            from .bwt import bwt_from_suffix_array

            if sa is not None:
                self.bwt = bwt_from_suffix_array(self.text[:-1], sa)
            else:
                from .bwt import build_suffix_array

                full_sa = build_suffix_array(self.text)
                self.bwt = bwt_from_suffix_array(self.text[:-1], full_sa)
                sa = full_sa

        if sa is None:
            from .bwt import build_suffix_array

            sa = build_suffix_array(self.text)

        self.sa = sa

        self.sigma = sorted(set(self.text))
        self.C = self._build_C()
        self.Occ = self._build_Occ()
        self.sa_sample = self._build_sa_sample()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_C(self) -> Dict[str, int]:
        """C[c] = number of characters in text strictly smaller than c."""
        counts: Dict[str, int] = {}
        for ch in self.text:
            counts[ch] = counts.get(ch, 0) + 1

        C: Dict[str, int] = {}
        cumulative = 0
        for ch in self.sigma:
            C[ch] = cumulative
            cumulative += counts[ch]
        return C

    def _build_Occ(self) -> Dict[str, List[int]]:
        """Precomputed rank table.

        Occ[c][i] = number of occurrences of character c in bwt[0:i].
        """
        Occ: Dict[str, List[int]] = {}
        for ch in self.sigma:
            Occ[ch] = [0] * (self.n + 1)

        for i in range(self.n):
            c = self.bwt[i]
            for ch in self.sigma:
                Occ[ch][i + 1] = Occ[ch][i] + (1 if c == ch else 0)

        return Occ

    def _build_sa_sample(self) -> Dict[int, int]:
        """Sample every sample_rate-th suffix array entry for locate."""
        sample: Dict[int, int] = {}
        for i in range(0, self.n, self.sample_rate):
            sample[i] = self.sa[i]
        return sample

    # ------------------------------------------------------------------ #
    # Rank / LF-mapping                                                    #
    # ------------------------------------------------------------------ #

    def rank(self, ch: str, i: int) -> int:
        """Number of occurrences of ch in bwt[0:i]."""
        if ch not in self.Occ:
            return 0
        if i < 0:
            return 0
        if i > self.n:
            i = self.n
        return self.Occ[ch][i]

    def lf(self, i: int) -> int:
        """LF-mapping: map BWT position i to the corresponding position
        in the first column of the BWT matrix (i.e., the SA value).

        LF(i) = C[BWT[i]] + Occ(BWT[i], i)
        """
        c = self.bwt[i]
        return self.C[c] + self.rank(c, i)

    # ------------------------------------------------------------------ #
    # Backward search — count                                              #
    # ------------------------------------------------------------------ #

    def count(self, pattern: str) -> int:
        """Count occurrences of pattern in the original text.

        Uses backward search over the FM-Index.

        Args:
            pattern: Pattern string to search for.

        Returns:
            Number of occurrences of pattern in the text (excluding sentinel).
        """
        sp, ep = 0, self.n

        for ch in reversed(pattern):
            if ch not in self.C:
                return 0
            c_count = self.rank(ch, ep) - self.rank(ch, sp)
            if c_count == 0:
                return 0
            sp = self.C[ch] + self.rank(ch, sp)
            ep = sp + c_count

        return ep - sp

    def count_and_range(
        self, pattern: str
    ) -> Tuple[int, int, int]:
        """Count and return the SA-range for pattern.

        Returns:
            (count, sp, ep) where [sp, ep) is the range in the suffix array.
        """
        sp, ep = 0, self.n

        for ch in reversed(pattern):
            if ch not in self.C:
                return 0, sp, ep
            c_count = self.rank(ch, ep) - self.rank(ch, sp)
            if c_count == 0:
                return 0, sp, ep
            sp = self.C[ch] + self.rank(ch, sp)
            ep = sp + c_count

        return ep - sp, sp, ep

    # ------------------------------------------------------------------ #
    # Locate — find all positions                                         #
    # ------------------------------------------------------------------ #

    def locate(self, pattern: str) -> List[int]:
        """Find all starting positions of pattern in the original text.

        Uses backward search to find the SA range, then reconstructs
        positions via sampled SA entries and LF-mapping.

        Args:
            pattern: Pattern string to search for.

        Returns:
            Sorted list of starting positions (0-indexed, excluding sentinel).
        """
        cnt, sp, ep = self.count_and_range(pattern)
        if cnt == 0:
            return []

        positions: List[int] = []
        for i in range(sp, ep):
            pos = self._sa_at(i)
            if pos < self.n - 1:
                positions.append(pos)

        positions.sort()
        return positions

    def _sa_at(self, i: int) -> int:
        """Return SA[i] directly from the stored suffix array."""
        return self.sa[i]

    # ------------------------------------------------------------------ #
    # Extract — retrieve substring from a SA position                    #
    # ------------------------------------------------------------------ #

    def extract(self, pos: int, length: int) -> str:
        """Extract a substring starting at text position `pos` with given `length`.

        Args:
            pos: Starting position in the original text (0-indexed).
            length: Number of characters to extract.

        Returns:
            The extracted substring.
        """
        if pos < 0 or pos >= self.n - 1:
            raise ValueError(f"Position {pos} out of range [0, {self.n - 2}]")
        if length <= 0:
            return ""

        end = min(pos + length, self.n - 1)
        return self.text[pos:end]

    def extract_at_sa(self, sa_idx: int, length: int) -> str:
        """Extract substring from the suffix starting at SA[sa_idx].

        Args:
            sa_idx: Index into the suffix array.
            length: Maximum number of characters to extract.

        Returns:
            The substring of the original text.
        """
        if sa_idx < 0 or sa_idx >= self.n:
            raise ValueError(f"SA index {sa_idx} out of range [0, {self.n - 1}]")

        pos = self._sa_at(sa_idx)
        return self.extract(pos, length)

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def text_length(self) -> int:
        """Length of the original text (excluding sentinel)."""
        return self.n - 1

    def __len__(self) -> int:
        return self.n