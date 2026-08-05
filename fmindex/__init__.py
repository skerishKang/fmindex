"""FMIndex package — FM-Index data structure + Integration Slice 1 pipeline."""

from .index import FMIndex
from .bwt import bwt_from_suffix_array, inverse_bwt, build_suffix_array

__all__ = [
    "FMIndex",
    "bwt_from_suffix_array",
    "inverse_bwt",
    "build_suffix_array",
]

__version__ = "2.0.0"
