import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QTextDocument, QTextFormat
from PySide6.QtWidgets import QApplication

import gui

APP = QApplication.instance() or QApplication([])


def render(text, font_px=13):
    doc = QTextDocument()
    gui.render_markdown(doc, text, font_px)
    return doc


def blocks(doc):
    block = doc.begin()
    while block.isValid():
        yield block
        block = block.next()


class CompactMarkdownTests(unittest.TestCase):
    def test_headings_are_compact_and_bold(self):
        font_px = 13
        for level in ("#", "##", "###", "####"):
            doc = render(f"{level} Detalles\n\ncuerpo", font_px)
            heading = next(b for b in blocks(doc)
                           if b.blockFormat().headingLevel() > 0)
            fmt = heading.begin().fragment().charFormat()
            size = fmt.property(QTextFormat.Property.FontPixelSize)
            self.assertLessEqual(size, font_px + 2, level)
            self.assertGreaterEqual(size, font_px)
            self.assertEqual(
                fmt.property(QTextFormat.Property.FontSizeAdjustment), 0)
            self.assertGreaterEqual(fmt.fontWeight(), QFont.Weight.Bold)
            self.assertEqual(heading.blockFormat().topMargin(), 6)

    def test_heading_height_close_to_body(self):
        doc = render("### Detalles\n\ncuerpo", 13)
        doc.setTextWidth(400)
        layout = doc.documentLayout()
        heading, body = list(blocks(doc))[:2]
        h_heading = layout.blockBoundingRect(heading).height()
        h_body = layout.blockBoundingRect(body).height()
        self.assertLessEqual(h_heading, h_body * 1.25 + 8)

    def test_list_indent_reduced(self):
        doc = render("- uno\n- dos")
        self.assertEqual(doc.indentWidth(), 16)

    def test_code_block_still_styled(self):
        doc = render("```python\nx = 1\n```")
        block = doc.begin()
        self.assertEqual(
            block.blockFormat().background().color().name(), "#15171a")


if __name__ == "__main__":
    unittest.main()
