"""Greek letters as the Latin names a writer types for them, so a quote matches the paper however it is spelled.

A paper prints "ϕ∗ = argmax"; a model quoting it may write "ϕ", "φ", "\\phi" or "phi". Matching quotes on letters
and digits alone used to drop the Greek letter on one side and keep "phi" on the other, so a true quote was
reported as "not in the manuscript". Folding every Greek letter to its name makes all four spellings equal.

Micro (µ, μ) folds to "u", the way units are typed without the symbol ("50 uM"): in quotes it is a unit far more
often than a variable.
"""

from __future__ import annotations

_NAMES = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "u", "µ": "u", "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi",
    "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi", "ϕ": "phi", "χ": "chi",
    "ψ": "psi", "ω": "omega", "ϵ": "epsilon", "ϑ": "theta", "ϰ": "kappa", "ϱ": "rho", "ϖ": "pi",
}
GREEK_TABLE = {ord(letter): name for letter, name in _NAMES.items()}


def fold(char: str) -> str:
    """One lowercase character as letters a quote would use (a Greek letter becomes its name)."""
    return _NAMES.get(char, char)
