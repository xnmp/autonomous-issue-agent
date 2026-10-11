import pytest

from issue_agent.domain.terminal_output import strip_ansi


@pytest.mark.parametrize("text,expected", [
    ("\x1b[32mgreen\x1b[0m \x1b[2mdim\x1b[0m", "green dim"),
    ("\x1b[?25lready\x1b[2K", "ready"),
    ("\x1b]0;window title\x07result", "result"),
    ("\x1b]8;;https://example.test\x1b\\链接\x1b]8;;\x1b\\", "链接"),
    ("\x1b7saved\x1b8\x1b(B", "saved"),
    ("résultat: 日本語 ✓\x1b[32m é\x1b[0m", "résultat: 日本語 ✓ é"),
    ("done\x1b[32;", "done[32;"),
    ("done\x1b]unfinished", "done]unfinished"),
    ("done\x1b(", "done("),
    ("done\x1b", "done"),
    ("", ""),
])
def test_terminal_controls_are_removed_without_changing_text(text, expected):
    assert strip_ansi(text) == expected
    assert "\x1b" not in strip_ansi(text)


def test_huge_colored_output_remains_lossless():
    assert strip_ansi("\x1b[32mé\x1b[0m" * 100000) == "é" * 100000


@pytest.mark.parametrize("opener", ["]", "P", "_", "^", "X"])
@pytest.mark.parametrize("terminator", ["\x07", "\x1b\\"])
def test_complete_control_strings_remove_only_their_payload(opener, terminator):
    assert strip_ansi(f"before\x1b{opener}1;2|PAYLOAD{terminator}after") == "beforeafter"


@pytest.mark.parametrize("opener", ["]", "P", "_", "^", "X"])
def test_unfinished_control_strings_preserve_payload(opener):
    assert strip_ansi(f"before\x1b{opener}1;2|PAYLOAD") == f"before{opener}1;2|PAYLOAD"


@pytest.mark.parametrize("opener", ["]", "P", "_", "^", "X"])
@pytest.mark.parametrize("terminator", ["\x07", "\x1b\\"])
def test_control_strings_cannot_consume_output_across_newline(opener, terminator):
    text = f"\x1b{opener}unfinished\nFAIL stderr test case\n{terminator}AFTER\n"
    expected = f"{opener}unfinished\nFAIL stderr test case\n"
    expected += "\x07" if terminator == "\x07" else ""
    assert strip_ansi(text) == expected + "AFTER\n"


def test_nested_escape_cannot_extend_control_string_payload():
    assert strip_ansi("\x1b]unfinished\x1b[31mFAIL\x1b\\") == "]unfinishedFAIL"


@pytest.mark.parametrize("fragment", ["[32m", "2;1H", "[?25l"])
def test_only_truncated_tail_loses_a_leading_csi_remainder(fragment):
    text = fragment + "FAIL\n" + fragment
    assert strip_ansi(text) == text
    assert strip_ansi(text, truncated=True) == "FAIL\n" + fragment
