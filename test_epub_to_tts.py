"""Regression tests for mixed pages and mislabeled footnotes."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from zipfile import ZipFile

from epub_to_tts import extract, main


XHTML = """<html xmlns="http://www.w3.org/1999/xhtml"
xmlns:epub="http://www.idpf.org/2007/ops"><body>{}</body></html>"""
CONTAINER = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>"""


def make_book(archive, documents, navigation=None):
    manifest = "".join(f'<item id="{id_}" href="{href}" media-type="application/xhtml+xml"/>'
                       for id_, href, _ in documents)
    if navigation is not None:
        manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    spine = "".join(f'<itemref idref="{id_}"/>' for id_, _, _ in documents)
    package = f"""<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>样本</dc:title></metadata>
<manifest>{manifest}</manifest><spine>{spine}</spine></package>"""
    with ZipFile(archive, "w") as book:
        book.writestr("META-INF/container.xml", CONTAINER)
        book.writestr("OPS/book.opf", package)
        if navigation is not None:
            book.writestr("OPS/nav.xhtml", XHTML.format(navigation))
        for _, href, markup in documents:
            book.writestr("OPS/" + href.replace("%20", " "), XHTML.format(markup))


class EpubCleanupTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("pandoc") is None:
            self.skipTest("Pandoc is required")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.book = Path(directory.name) / "样本.epub"

    def test_keeps_body_and_lists_when_cleaning_shared_document(self):
        make_book(self.book, [("chapter", "Text/Chapter%201.xhtml", """
<section epub:type="titlepage"><p>扉页内容</p></section>
<nav epub:type="toc"><h1>目录</h1><ol>
<li><a href="#chapter">第一章</a></li><li><a href="#appendix">附录</a></li>
</ol></nav>
<h1>译者说明</h1><p>译者写在开头的话。</p>
<h2 id="chapter">第一章</h2>
<p>第一句<a href="Old%20File.xhtml#n1"><sup>[1]</sup></a>，正文继续。</p>
<p>第二句<a epub:type="noteref" href="#n2">②</a>，还是正文。</p>
<p class="note">普通提示段落属于正文。</p>
<p>①第一条正文分项。</p><p>②第二条正文分项。</p>
<p>数组下标 values[1] 和正文数字 3.14。</p>
<aside epub:type="footnote" id="n1"><p>第一份脚注正文。</p></aside>
<p class="note" id="n2">[2] 第二份脚注正文。</p>
<span role="doc-pagebreak" id="page1">第 12 页</span>
<p class="running-footer">刊行页脚</p>
<h2>注释</h2><p>[3] 混排脚注正文。</p>
<h2 id="appendix">附录</h2><p>这是附录正文。</p>
""")])
        original = self.book.read_bytes()
        text, report = extract(self.book, "pandoc")
        for kept in ("译者说明", "第一句，正文继续。", "第二句，还是正文。", "普通提示段落",
                     "①第一条正文分项。", "②第二条正文分项。", "values[1]", "3.14", "这是附录正文。"):
            self.assertIn(kept, text)
        for removed in ("目录", "扉页内容", "份脚注正文", "混排脚注正文", "刊行页脚", "第 12 页", "^([1])"):
            self.assertNotIn(removed, text)
        self.assertEqual(text.count("第一章"), 1)
        self.assertEqual(report["删除元素"]["注号或回链"], 2)
        self.assertEqual(original, self.book.read_bytes())

    def test_empty_image_only_book_returns_a_clear_error(self):
        make_book(self.book, [("image", "page.xhtml", '<img src="page.png" alt="扫描页"/>')])
        with self.assertRaisesRegex(ValueError, "没有可提取的正文"):
            extract(self.book, "pandoc")

    def test_repairs_prose_breaks_and_keeps_structural_line_breaks(self):
        make_book(self.book, [("chapter", "chapter.xhtml", """
<h1>第一章</h1>
<p>作者在这句话的中<br />间换了一行。<br />但仍然属于同一个段落。</p>
<p>Sentence one.<br /><em>Sentence two.</em></p>
<p>这里是另一个段落。</p>
<p class="poem">风从江上来<br />月向山中落</p>
<ul><li>列表第一行<br />列表第二行</li></ul>
<table><tr><td>上半格<br />下半格</td></tr></table>
<pre>alpha
beta</pre>
""")])
        text, report = extract(self.book, "pandoc")
        self.assertIn("作者在这句话的中间换了一行。但仍然属于同一个段落。", text)
        self.assertIn("Sentence one. Sentence two.", text)
        self.assertIn("Sentence two.\n\n这里是另一个段落。", text)
        self.assertIn("风从江上来\n月向山中落", text)
        self.assertNotIn("列表第一行列表第二行", text)
        self.assertNotIn("上半格下半格", text)
        self.assertRegex(text, r"alpha\n[ \t]*beta")
        self.assertEqual(report["硬换行修复"]["段内换行"], 3)

    def test_repairs_printed_lines_without_gluing_indented_paragraphs(self):
        one = "甲乙丙丁" * 9
        two = "天地玄黄" * 9
        first = f"<p>    {one[:-2]}。</p>" + f"<p>{one}</p>" * 5 + "<p>第一段结束。</p>"
        second = f"<p>    {two[:-2]}</p>" + f"<p>{two}</p>" * 5 + "<p>第二段结束。</p>"
        make_book(self.book, [("chapter", "chapter.xhtml",
                              "<h1>第一章</h1>" + first + second)])
        text, report = extract(self.book, "pandoc")
        self.assertIn(f"{one[:-2]}。{one}", text)
        self.assertIn(f"第一段结束。\n\n{two[:-2]}{two}", text)
        self.assertIn(f"{two}第二段结束。", text)
        self.assertEqual(report["硬换行修复"]["逐行段落"], 12)

    def test_folder_continues_after_a_broken_book_and_skips_existing_output(self):
        make_book(self.book, [("chapter", "chapter.xhtml", "<h1>第一章</h1><p>这是正文。</p>")])
        (self.book.parent / "000损坏.epub").write_bytes(b"invalid zip data")
        destination = self.book.parent / "输出目录"
        log = StringIO()
        with redirect_stdout(log), redirect_stderr(log):
            self.assertEqual(main([str(self.book.parent), "-d", str(destination), "--report"]), 1)
            self.assertEqual(main([str(self.book.parent), "-d", str(destination), "--report"]), 1)
        self.assertIn("转换失败 000损坏.epub", log.getvalue())
        self.assertIn("跳过已有输出", log.getvalue())
        self.assertIn("这是正文。", (destination / "样本.tts.txt").read_text(encoding="utf-8"))
        self.assertTrue((destination / "样本.tts.cleanup.json").is_file())

    def test_split_preserves_order_and_keeps_subsections_with_their_parent(self):
        make_book(self.book, [("shared", "reader.xhtml", """
<p>献给读者。</p>
<h1 id="preface">序言</h1><p>一段序言。</p>
<h1 id="translator">译者说明</h1><p>一段译者说明。</p>
<h1 id="volume">第一编 田野</h1>
<p>原书举例时提到以下词语。</p>
<section epub:type="part"><h1 id="second-volume">第二编 河流</h1></section>
<h2 id="river">河边</h2><p>第一大章。</p>
<h3 id="bridge">木桥</h3><p>河边章的小节正文。</p>
<h2 id="chapter2">第二章 集市</h2><p>第二大章。</p>
<h2 id="postscript">后记</h2><p>一段后记。</p>
<h2 id="appendix">附录：地图 / 草图</h2><p>一段附录。</p>
""")], """<nav epub:type="toc"><ol>
<li><a href="reader.xhtml#preface">序言</a></li>
<li><a href="reader.xhtml#translator">译者说明</a></li>
<li><a href="reader.xhtml#volume">第一编 田野</a></li>
<li><span>第二编 河流</span><ol>
<li><a href="reader.xhtml#river">河边</a><ol>
<li><a href="reader.xhtml#bridge">木桥</a></li></ol></li>
<li><a href="reader.xhtml#chapter2">第二章 集市</a></li>
</ol></li>
<li><a href="reader.xhtml#postscript">后记</a></li>
<li><a href="reader.xhtml#appendix">附录：地图 / 草图</a></li>
</ol></nav>""")
        destination = self.book.parent / "分段输出"
        with redirect_stdout(StringIO()):
            self.assertEqual(main([str(self.book), "-o", str(destination), "--split", "--report", "--preview"]), 0)
            self.assertFalse(destination.exists())
            self.assertEqual(main([str(self.book), "-o", str(destination), "--split", "--report"]), 0)
        log = json.loads((destination / "清洗记录.json").read_text(encoding="utf-8"))
        self.assertEqual(log["文件"], self.book.name)
        self.assertEqual([part["类型"] for part in log["分段"]],
                         ["书前内容", "序言", "译者说明", "分部", "正文", "正文", "后记", "附录"])
        files = sorted(destination.glob("*.txt"))
        self.assertEqual([path.name for path in files], [part["文件"] for part in log["分段"]])
        self.assertEqual(files[-1].name, "008_附录：地图 草图.txt")
        river = files[4].read_text(encoding="utf-8")
        self.assertIn("第二编 河流", river)
        self.assertIn("河边章的小节正文。", river)
        self.assertNotIn("第二大章。", river)
        combined, _ = extract(self.book, "pandoc")
        self.assertEqual(b"".join(path.read_bytes() for path in files), combined.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
