"""Language guard for generated explanations, separate from original evidence."""

from unicodedata import name


def contains_non_latin_text(value) -> bool:
    if isinstance(value, str):
        return any(char.isalpha() and not char.isascii() and not name(char, "").startswith("LATIN ")
                   for char in value)
    if isinstance(value, dict):
        return any(contains_non_latin_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_non_latin_text(item) for item in value)
    return False
