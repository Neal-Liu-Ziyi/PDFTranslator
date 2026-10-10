"""Structured PDF extraction for the paper translation pipeline."""

from .parser import parse_document, parse_pdf

__all__ = ["parse_document", "parse_pdf"]
__version__ = "0.1.0"
