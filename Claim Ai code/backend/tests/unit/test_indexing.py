"""The upload's file-name handling (D-035): the client's name is display metadata only, reduced to a plain name that
the database accepts. It never names where the file is stored (that is a server-generated reference)."""

import pytest

from app.indexing import DEFAULT_FILENAME, MAX_FILENAME_CHARS, safe_filename


@pytest.mark.security
@pytest.mark.parametrize(
    ("name", "expected"),
    [("wording.pdf", "wording.pdf"), ("../../etc/passwd.pdf", "passwd.pdf"), ("C:\\Users\\x\\policy.pdf", "policy.pdf"),
     ("/abs/path/file.pdf", "file.pdf"), ("we\x00ird\x1b\u202ename.pdf", "weirdname.pdf"), ("  spaced.pdf  ", "spaced.pdf"),
     ("", DEFAULT_FILENAME), ("..", DEFAULT_FILENAME), ("dir/", DEFAULT_FILENAME), ("\x00\x01", DEFAULT_FILENAME)],
    ids=["plain", "traversal", "windows", "absolute", "control-and-bidi", "spaces", "empty", "dot-dot", "trailing-slash",
         "only-controls"],
)
def test_a_file_name_is_reduced_to_a_plain_display_name(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_a_long_name_is_trimmed_to_the_stored_limit_keeping_its_extension() -> None:
    long = safe_filename("p" * 400 + ".pdf")

    assert len(long) == MAX_FILENAME_CHARS and long.endswith(".pdf")
    assert len(safe_filename("q" * 400)) == MAX_FILENAME_CHARS
