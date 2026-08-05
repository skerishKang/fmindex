"""Demo script for the FM-Index implementation."""

from fmindex import FMIndex, build_suffix_array, bwt_from_suffix_array, inverse_bwt


def demo_basic():
    text = "banana"
    print(f"Text: {text!r}")
    print()

    idx = FMIndex(text)

    print(f"Text length (excl. sentinel): {idx.text_length()}")
    print(f"BWT: {idx.bwt!r}")
    print(f"Sigma: {idx.sigma}")
    print(f"C array: {idx.C}")
    print()

    patterns = ["ana", "ban", "na", "a", "x", ""]
    for pat in patterns:
        cnt = idx.count(pat)
        positions = idx.locate(pat)
        print(f"Pattern {pat!r}: count={cnt}, positions={positions}")

    print()
    print("Extract from position 1, length 3:", repr(idx.extract(1, 3)))
    print("Extract from position 0, length 6:", repr(idx.extract(0, 6)))


def demo_bwt_roundtrip():
    text = "abracadabra"
    print(f"\nBWT roundtrip demo")
    print(f"Text: {text!r}")

    sa = build_suffix_array(text + "$")
    bwt = bwt_from_suffix_array(text, sa)
    print(f"BWT: {bwt!r}")

    reconstructed = inverse_bwt(bwt)
    print(f"Reconstructed: {reconstructed!r}")
    assert reconstructed == text, f"Roundtrip failed: {reconstructed!r} != {text!r}"
    print("Roundtrip OK")


def demo_large():
    text = "the quick brown fox jumps over the lazy dog the quick brown fox"
    print(f"\nLarge text demo")
    print(f"Text: {text!r}")

    idx = FMIndex(text, sample_rate=4)

    patterns = ["quick", "brown", "fox", "the", "lazy", "dog", "cat"]
    for pat in patterns:
        cnt = idx.count(pat)
        positions = idx.locate(pat)
        print(f"Pattern {pat!r}: count={cnt}, positions={positions}")


def demo_extract_all_suffixes():
    text = "banana"
    print(f"\nExtract all suffixes of {text!r}")
    idx = FMIndex(text)

    for i in range(idx.text_length()):
        suffix = idx.extract(i, idx.text_length() - i)
        print(f"  SA[{i}] -> pos={idx._sa_at(i)} -> suffix={suffix!r}")


if __name__ == "__main__":
    demo_basic()
    demo_bwt_roundtrip()
    demo_large()
    demo_extract_all_suffixes()