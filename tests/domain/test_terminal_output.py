import pytest

from issue_agent.domain.terminal_output import strip_ansi


@pytest.mark.parametrize("text,expected", [
    ("\x1b[32mgreen\x1b[0m \x1b[2mdim\x1b[0m", "green dim"),
    ("\x1b[?25lready\x1b[2K", "ready"),
    ("\x1b]0;window title\x07result", "result"),
    ("\x1b]8;;https://example.test\x1b\\链接\x1b]8;;\x1b\\", "链接"),
    ("\x1b7saved\x1b8\x1b(B", "saved"),
    ("résultat: 日本語 ✓\x1b[32m é\x1b[0m", "résultat: 日本語 ✓ é"),
    ("done\x1b[32;", "done"),
    ("done\x1b]unfinished", "done"),
    ("done\x1b(", "done"),
    ("done\x1b", "done"),
    ("", ""),
])
def test_terminal_controls_are_removed_without_changing_text(text, expected):
    assert strip_ansi(text) == expected
    assert "\x1b" not in strip_ansi(text)


def test_huge_colored_output_remains_lossless():
    assert strip_ansi("\x1b[32mé\x1b[0m" * 100000) == "é" * 100000
