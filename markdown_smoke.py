import os

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from gui import render_markdown


SOL_MARKDOWN = (
    "**I would scale the hot-key path gradually, using measured skew to guide "
    "partitioning.**\n\n"
    "- Track per-key traffic and p95/p99 latency before choosing a partition key.\n"
    "- Test a shadow rebalancing plan to estimate how much data will move.\n"
    "- Move keys in small batches and monitor replication lag and tail latency.\n\n"
    "```python\n"
    "for batch in plan.rebalance_batches():\n"
    "    move(batch)\n"
    "    verify_lag(batch)\n"
    "```\n"
)


def _blocks(document):
    block = document.begin()
    while block.isValid():
        yield block
        block = block.next()


def _render(text):
    document = QTextDocument()
    render_markdown(document, text)
    return document


def _has_code_background(block):
    return block.blockFormat().background().color().name() == "#15171a"


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert app is not None

    literal = _render("Use `List<int>` as the collection type.")
    assert "List<int>" in literal.toPlainText()

    unclosed = _render("```python\nprint('still code')")
    unclosed_blocks = list(_blocks(unclosed))
    assert len(unclosed_blocks) == 1
    assert _has_code_background(unclosed_blocks[0])

    for label, markdown in (
        ("adjacent fence", "- Keep this bullet\n```python\nprint('ok')\n```"),
        ("exact Sol answer", SOL_MARKDOWN),
    ):
        document = _render(markdown)
        blocks = list(_blocks(document))
        list_block = next(
            block for block in blocks
            if block.text().startswith(
                "Keep this bullet" if label == "adjacent fence"
                else "Move keys in small batches"))
        assert list_block.textList() is not None, (
            f"{label}: list item lost its QTextList")
        assert not _has_code_background(list_block), (
            f"{label}: list item received code styling")

        if label == "adjacent fence":
            code_blocks = [
                block for block in blocks if block.text().startswith("print(")]
        else:
            code_blocks = [
                block for block in blocks
                if block.text().startswith(
                    ("for batch", "    move(", "    verify_lag("))]
        assert code_blocks, f"{label}: code fence was not parsed"
        assert all(_has_code_background(block) for block in code_blocks), (
            f"{label}: code lines lost their background")

    print(
        "OK: literal List<int>, unclosed fence, adjacent list/fence, "
        "and exact Sol markdown.")


if __name__ == "__main__":
    main()
