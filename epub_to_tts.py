"""Extract local EPUB text in reading order and prepare it for narration."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

try:
    from lxml import etree, html
except ImportError:
    raise SystemExit("缺少 lxml。请运行 python -m pip install lxml。")

from clean_for_tts import mark_contents, mark_front, mark_page_noise, normalize, render, title_key, write_new
from epub_sections import (
    EXTRA_ROLE, FRONT_EXTRA, MINOR, NavEntry, Section, named_kind, navigation_is_major, split_parts,
)


OPS = "{http://www.idpf.org/2007/ops}type"
HEADINGS = {f"h{i}" for i in range(1, 7)}
ROWS = HEADINGS | {"p", "li", "dt", "dd", "blockquote"}
NOTE_TYPES = {"footnote", "footnotes", "endnote", "endnotes", "rearnote", "rearnotes"}
REF_TYPES = {"noteref", "backlink"}
PAGE_TYPES = {"pagebreak", "pageheader", "pagefooter"}
FRONT_TYPES = {"cover", "titlepage", "halftitlepage", "copyright-page", "colophon"}
NAV_TYPES = {"toc", "landmarks", "page-list", "pagelist"}
INVISIBLE_TAGS = {"script", "style", "link", "noscript", "template", "svg", "img", "audio", "video",
                  "iframe", "object", "canvas", "picture"}
IGNORED_TOKENS = {
    "nav": NAV_TYPES,
    "front": FRONT_TYPES | {"title-page", "copyrightpage", "cover-page", "front-cover"},
    "page": PAGE_TYPES | {"page-break", "page-header", "page-footer", "running-header", "running-footer",
                          "running-head", "running-foot", "runningheader", "runningfooter", "mbp-pagebreak"},
}
SPECIFIC_NOTE_CLASSES = NOTE_TYPES | {
    "foot-note", "foot-notes", "end-note", "end-notes", "duokan-footnote",
    "fnote", "footnote-text", "endnote-text", "footnote-content",
}
SPECIFIC_REF_CLASSES = REF_TYPES | {"note-ref", "footnote-ref", "footnote-back", "duokan-footnote-ref"}
LABELS = {
    "目录": {"目录", "目录content", "目录contents", "总目录", "章节目录", "contents", "tableofcontents",
           "documentoutline", "封面目录"},
    "扉页或版权页": {"封面", "书名页", "扉页", "版权页", "版权信息", "版权声明", "出版信息", "出版说明",
                  "cover", "coverpage", "titlepage", "halftitlepage", "copyright", "copyrightpage", "colophon"},
    "注释": {"注", "脚注", "尾注", "注释", "注解", "译注", "本章注释", "本书注释", "notes", "footnotes",
           "endnotes", "rearnotes", "chapterendnotes"},
}
TRADITIONAL_LABELS = str.maketrans("錄總節書頁權註釋譯聲", "录总节书页权注释译声")
NAV_FILE = re.compile(r"(?:toc|nav|catalog|contents)(?:[_-]?\d+)?", re.I)
FRONT_FILE = re.compile(r"(?:cover|coverpage|titlepage|halftitlepage|copyright|copyrightpage|colophon)", re.I)
NOTE_FILE = re.compile(r"(?:footnotes?|endnotes?|rearnotes?|annotations?|notes?)(?:[_-]?\d+)?", re.I)
NOTE_MARKER = re.compile(
    r"(?:[\[(〔【]\s*(?:注|註)?\s*(\d{1,4})\s*[\])〕】]|([①-⑳])|([*†‡]{1,3}))"
)
LINK_MARKER = re.compile(r"(?:\d{1,4}|[a-z*†‡]{1,3}|[ivxlcdm]{1,8})", re.I)
NOTE_LEAD = re.compile(r"^(?:[\[(〔【]\s*(?:注|註)?\s*(\d{1,4})\s*[\])〕】]|([①-⑳])|\d{1,3}[.、)]\s)")
BRACKET_REF = re.compile(r"[\[［]\s*(\d{1,4})\s*[\]］]")
OCR_NOTE_LEAD = re.compile(r"^\[\s*(\d{1,4})\s*[Jj]\s+\S")
CHAPTER_KEY = re.compile(r"^(?:第\s*)?([一二三四五六七八九十百零〇两兩0-9]{1,5})(?:[章节節回]|(?=$|[\s、.，,:：]))")
APPENDIX_REF = re.compile(r"[（(]附[录錄]\s*\d+\s*[:：][^）)]+[）)]$")
SECTION_TITLE = re.compile(r"第[0-9一二三四五六七八九十百零〇两兩]+[章节節回卷编編部篇]|chapter\s+\d+", re.I)
EDITION = re.compile(r"^(?:出版人|出版发行|出版發行|出版社|责任编辑|責任編輯|责任校对|責任校對|"
                     r"封面设计|版次|印次|社址|邮编|網址|网址|经销|發行|定价|定價|published by|all rights reserved)", re.I)
CIP = re.compile(r"图书在版编目|圖書在版編目|\bCIP\b.{0,12}(?:数据|數據)|"
                 r"cataloging.in.publication", re.I)
ISBN_LINE = re.compile(r"^ISBN(?:[- ]?(?:10|13))?\s*[:(]?\s*[0-9][-0-9 ]{7,}", re.I)


@dataclass
class Document:
    path: str
    body: etree._Element
    properties: frozenset[str]


def tag(node):
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def words(value):
    return " ".join(value.replace("\ufeff", "").split())


def content(node):
    return words("".join(node.itertext()))


def label_key(value):
    normalized = normalize(value).casefold().translate(TRADITIONAL_LABELS)
    return re.sub(r"[\s\W_]+", "", normalized)


def kind_of(value):
    key = label_key(value)
    return next((kind for kind, labels in LABELS.items() if key in labels), None)


def resolve(base, href):
    if not href:
        return None
    try:
        location = urlsplit(href)
    except ValueError:
        return None
    if location.scheme or location.netloc:
        return None
    requested = unquote(location.path)
    if not requested:
        path = base
    elif requested.startswith("/"):
        path = posixpath.normpath(requested.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(base), requested))
    if path in {".", ".."} or path.startswith("../") or "\\" in path:
        return None
    return path, unquote(location.fragment)


def xml(raw):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    return etree.fromstring(raw, parser)


def parse_document(raw):
    try:
        return xml(raw)
    except etree.XMLSyntaxError:
        try:
            raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            encoding = None
        else:
            encoding = "utf-8"
        parser = html.HTMLParser(encoding=encoding, no_network=True)
        return html.document_fromstring(raw, parser=parser)


def children(node, name):
    return [child for child in node if tag(child) == name]


def first(node, name):
    return next((child for child in node if tag(child) == name), None)


def semantics(node):
    epub_types = (node.get(OPS) or node.get("epub:type") or "").lower().split()
    roles = (node.get("role") or "").lower().split()
    return set(epub_types) | {role.removeprefix("doc-") for role in roles if role.startswith("doc-")}


def classes(node):
    return {value.lower().replace("_", "-") for value in (node.get("class") or "").split()}


def read_package(archive):
    container = xml(archive.read("META-INF/container.xml"))
    rootfile = next((entry for entry in container.iter() if tag(entry) == "rootfile"), None)
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB 容器缺少 OPF 路径。")
    package_path = rootfile.get("full-path")
    package = xml(archive.read(package_path))
    metadata = first(package, "metadata")
    title = next((content(item) for item in children(metadata, "title") if content(item)), "") if metadata is not None else ""
    manifest = first(package, "manifest")
    spine = first(package, "spine")
    if manifest is None or spine is None:
        raise ValueError("EPUB 缺少清单或阅读顺序。")
    items = {item.get("id"): item for item in children(manifest, "item")}
    nav = defaultdict(list)
    for item in items.values():
        properties = (item.get("properties") or "").split()
        if "nav" not in properties and item.get("media-type") != "application/x-dtbncx+xml":
            continue
        locator = resolve(package_path, item.get("href"))
        if not locator:
            continue
        nav_path, _ = locator
        if "nav" in properties:
            root = parse_document(archive.read(nav_path))
            for section in root.iter():
                if tag(section) != "nav" or "toc" not in semantics(section) and "toc" not in classes(section):
                    continue
                for link in section.iter():
                    point = resolve(nav_path, link.get("href")) if tag(link) == "a" else None
                    if point and content(link):
                        parents = []
                        items_in_toc = (parent for parent in link.iterancestors() if tag(parent) == "li")
                        for item in list(items_in_toc)[1:]:
                            label = next((child for child in item if tag(child) in {"a", "span"}), None)
                            if label is not None and content(label):
                                parents.append(content(label))
                        nav[point[0]].append(NavEntry(point[1], content(link), tuple(reversed(parents))))
        else:
            if spine.get("toc") and item.get("id") != spine.get("toc"):
                continue
            root = xml(archive.read(nav_path))
            for node in root.iter():
                if tag(node) != "navpoint":
                    continue
                text = first(node, "navlabel")
                target = first(node, "content")
                point = resolve(nav_path, target.get("src")) if target is not None else None
                if point and text is not None and content(text):
                    parents = [first(parent, "navlabel") for parent in node.iterancestors()
                               if tag(parent) == "navpoint"]
                    nav[point[0]].append(NavEntry(point[1], content(text),
                                                 tuple(content(parent) for parent in reversed(parents)
                                                       if parent is not None and content(parent))))

    guide = defaultdict(set)
    guide_node = first(package, "guide")
    if guide_node is not None:
        for reference in children(guide_node, "reference"):
            point = resolve(package_path, reference.get("href"))
            if point:
                label = reference.get("title", "")
                type_ = reference.get("type", "").lower()
                if type_ in {"cover", "title-page", "copyright-page", "colophon"}:
                    guide[point[0]].add(("扉页或版权页", point[1]))
                elif type_ in {"toc", "loi", "lot"}:
                    guide[point[0]].add(("目录", point[1]))
                elif kind_of(label):
                    guide[point[0]].add((kind_of(label), point[1]))

    docs = []
    skipped = []
    for itemref in children(spine, "itemref"):
        item = items.get(itemref.get("idref"))
        followed = set()
        while item is not None and item.get("media-type", "") not in {"application/xhtml+xml", "text/html"}:
            fallback = item.get("fallback")
            if not fallback or fallback in followed:
                item = None
                break
            followed.add(fallback)
            item = items.get(fallback)
        if item is None:
            skipped.append(itemref.get("idref") or "未知 spine 条目")
            continue
        point = resolve(package_path, item.get("href"))
        if not point:
            raise ValueError("EPUB 阅读顺序含有不可读取的资源路径。")
        data = parse_document(archive.read(point[0]))
        body = next((element for element in data.iter() if tag(element) == "body"), None)
        if body is None:
            skipped.append(point[0])
            continue
        docs.append(Document(point[0], body, frozenset((item.get("properties") or "").split())))
    return title, docs, nav, guide, skipped


def text_rows(body):
    return [(node, content(node)) for node in body.iter() if tag(node) in ROWS and content(node)
            and not any(tag(child) in ROWS for child in node)]


def metadata_page(rows):
    if not rows or len(rows) > 45 or any(len(row) > 235 for row in rows):
        return False
    has_cip = any(CIP.search(row) for row in rows)
    isbn = any(ISBN_LINE.search(row) for row in rows)
    editorial = sum(bool(EDITION.search(row)) for row in rows)
    if (has_cip and (isbn or editorial >= 2)) or (isbn and editorial >= 2):
        return True
    return (len(rows) <= 10 and max(map(len, rows)) <= 80
            and any(re.search(r"\b(?:University|Belknap)\s+Press\b", row, re.I) for row in rows)
            and any(re.fullmatch(r"(?:19|20)\d{2}", row) for row in rows)
            and any(re.match(r"根据.{0,55}出版社.{0,35}译出", row) for row in rows))


def toc_block(node):
    if tag(node) not in ROWS | {"nav", "div", "ul", "ol", "section", "dl"}:
        return False
    labels = [content(link) for link in node.iter() if tag(link) == "a" and link.get("href")]
    return bool(labels) and len(content(node)) - sum(map(len, labels)) <= 16


def nav_only(doc, rows, designated):
    anchors = [n for n in doc.body.iter() if tag(n) == "a" and n.get("href") and content(n)]
    if len(anchors) < 2:
        return False
    unlinked = "".join(content(doc.body).split())
    for anchor in anchors:
        label = "".join(content(anchor).split())
        unlinked = unlinked.replace(label, "", 1)
    if len(unlinked) > max(35, len(anchors) * 7):
        return False
    if any(len(value) > 26 and not toc_block(node) for node, value in rows):
        return False
    return designated or len(rows) >= 2


def whole_document(doc, rows, labels, guide):
    values = [value for _, value in rows]
    display = values[0] if values else content(doc.body)[:150]
    first_kind = kind_of(display)
    stem = PurePosixPath(doc.path).stem
    label_kinds = {(kind_of(entry.title), entry.fragment) for entry in labels} | guide
    types = semantics(doc.body)
    if types & NOTE_TYPES:
        return "注释"
    if types & FRONT_TYPES:
        return "扉页或版权页"
    dedicated_front = ("扉页或版权页", "") in label_kinds or first_kind == "扉页或版权页" or FRONT_FILE.fullmatch(stem)
    later_heading = any(tag(node) in HEADINGS and not kind_of(value) for node, value in rows[1:])
    if metadata_page(values) or (dedicated_front and len(values) <= 20
                                 and not later_heading
                                 and not any(len(row) > 130 for row in values)
                                 and not any(SECTION_TITLE.search(row) for row in values)):
        return "扉页或版权页"
    notes = sum(bool(NOTE_LEAD.match(normalize(value))) for value in values)
    notes_indicated = (first_kind == "注释" or NOTE_FILE.fullmatch(stem)
                       or ("注释", "") in label_kinds)
    if notes_indicated and not later_heading and notes and (notes >= len(values) / 2 or len(values) <= 3):
        return "注释"
    nav_candidate = ("nav" in doc.properties or NAV_FILE.fullmatch(stem) or first_kind == "目录"
                     or any(kind == "目录" for kind, _ in label_kinds))
    if nav_candidate and nav_only(doc, rows, True):
        return "目录"
    if not content(doc.body).strip("\ufeff\u200b "):
        return "仅图像或空白"
    return None


def note_marker(text, link=False):
    raw = text.strip()
    converted = normalize(raw)
    match = NOTE_MARKER.fullmatch(raw) or NOTE_MARKER.fullmatch(converted)
    if match:
        raw_number, circled, star = match.groups()
        if raw_number:
            return str(int(raw_number))
        return normalize(circled or star)
    if link and LINK_MARKER.fullmatch(converted):
        return converted
    return None


def note_container(node, body):
    candidates = ("p", "li", "dd", "aside", "div", "section")
    while node is not None and node is not body:
        if tag(node) in candidates:
            return node
        node = node.getparent()
    return None


def row_anchor_ids(node):
    output = {value for value in (node.get("id"), node.get("name")) if value}
    parent = node.getparent()
    if parent is not None and parent.get("id"):
        output.add(parent.get("id"))
    previous = node.getprevious()
    if previous is not None and tag(previous) == "a" and not content(previous):
        output.update(value for value in (previous.get("id"), previous.get("name")) if value)
    return output


def covered(node, marked):
    return node in marked or any(parent in marked for parent in node.iterancestors())


def remove_node(node):
    parent = node.getparent()
    if parent is None:
        return
    tail = node.tail or ""
    previous = node.getprevious()
    if tail:
        if previous is None:
            parent.text = (parent.text or "") + tail
        else:
            previous.tail = (previous.tail or "") + tail
    parent.remove(node)


def classify_nodes(doc, marked):
    rows = text_rows(doc.body)
    for node in doc.body.iterdescendants():
        tag_name = tag(node)
        tokens = semantics(node)
        styles = classes(node)
        if tag_name in INVISIBLE_TAGS or node.get("hidden") is not None or node.get("aria-hidden") == "true":
            marked[node] = "图像、隐藏或非文本元素"
        elif tokens & NOTE_TYPES or styles & SPECIFIC_NOTE_CLASSES:
            marked[node] = "脚注或注释块"
        elif tokens & REF_TYPES or styles & SPECIFIC_REF_CLASSES:
            marked[node] = "注号或回链"
        elif (tokens | styles) & IGNORED_TOKENS["page"]:
            marked[node] = "页码或页眉页脚"
        elif tokens & FRONT_TYPES or styles & IGNORED_TOKENS["front"]:
            marked[node] = "扉页或版权信息块"
        elif tokens & NAV_TYPES or styles & IGNORED_TOKENS["nav"] or any(
            style.startswith("kindle-cn-toc-") for style in styles
        ):
            marked[node] = "目录或导航块"
        elif "note" in styles and tag_name in {"p", "li", "aside", "div", "dd"} and NOTE_LEAD.match(normalize(content(node))):
            marked[node] = "脚注或注释块"

    for node, value in rows:
        kind = kind_of(value)
        if kind == "注释":
            notes = []
            for candidate in node.itersiblings():
                text = content(candidate)
                if not text:
                    continue
                if tag(candidate) in HEADINGS:
                    break
                if (NOTE_LEAD.match(normalize(text)) or marked.get(candidate) == "脚注或注释块"
                        or tag(candidate) == "ol" and children(candidate, "li")):
                    notes.append(candidate)
                else:
                    break
            if notes:
                marked[node] = "脚注或注释块"
                marked.update(dict.fromkeys(notes, "脚注或注释块"))
        if kind != "目录":
            continue
        links = []
        for candidate in node.itersiblings():
            if toc_block(candidate):
                links.append(candidate)
            elif content(candidate):
                break
        if sum(1 for candidate in links for child in candidate.iter() if tag(child) == "a") >= 2:
            marked[node] = "目录或导航块"
            marked.update(dict.fromkeys(links, "目录或导航块"))


def mark_note_links(docs, marked):
    docs_by_path = {doc.path: doc for doc in docs}
    ids = {}
    for doc in docs:
        for node in doc.body.iter():
            for value in (node.get("id"), node.get("name")):
                if value:
                    ids.setdefault((doc.path, value), node)
    for doc in docs:
        for link in doc.body.iter():
            if tag(link) != "a":
                continue
            target = resolve(doc.path, link.get("href"))
            if target is None or not target[1]:
                continue
            if not note_marker(content(link), link=True) and marked.get(link) != "注号或回链":
                continue
            selected = ids.get(target)
            if selected is None:
                # Some editors rename a chapter without updating its internal links.
                # Use the same document only when the fragment still identifies a real note.
                local = ids.get((doc.path, target[1]))
                if local is not None and any(marked.get(node) == "脚注或注释块"
                                             for node in (local, *local.iterancestors())):
                    marked.setdefault(link, "注号或回链")
                continue
            if any(marked.get(node) in {"脚注或注释块", "整份注释文档"}
                   for node in (selected, *selected.iterancestors())):
                marked.setdefault(link, "注号或回链")
                continue
            if covered(link, marked):
                continue
            block = note_container(selected, docs_by_path[target[0]].body)
            source_ids = row_anchor_ids(link)
            if block is None or not source_ids or covered(block, marked) or tag(block) in HEADINGS:
                continue
            if not (NOTE_LEAD.match(normalize(content(block))) or content(block).endswith(("↩", "↩︎"))):
                continue
            reverse = any(resolve(target[0], candidate.get("href")) in {(doc.path, value) for value in source_ids}
                          for candidate in block.iter() if tag(candidate) == "a")
            if reverse:
                marked[block] = "脚注或注释块"
                marked[link] = "注号或回链"


def chapter_key(value):
    key = normalize(value)
    match = CHAPTER_KEY.match(key)
    if match:
        return match.group(1)
    english = re.fullmatch(r"chapter\s+([0-9]+|[ivxlcdm]+)(?:\b.*)?", key, re.I)
    if english:
        return english.group(1).casefold()
    return key if key in {"引子", "序章", "前言", "后记", "後記", "尾声", "尾聲"} else None


def endnote_groups(rows, grouped, all_numbers):
    current = None
    for _, text in rows:
        value = normalize(text)
        match = NOTE_LEAD.match(value) or OCR_NOTE_LEAD.match(value)
        if match and match.group(1):
            number = str(int(match.group(1)))
            all_numbers.add(number)
            if current is not None:
                grouped[current].add(number)
        elif re.fullmatch(r"[一二三四五六七八九十百零〇两兩0-9]{1,5}|引子|序章|前言|后记|後記|尾声|尾聲", value):
            current = chapter_key(text) or current


def mark_unlinked_superscripts(doc, marked, note_numbers, scoped):
    if not note_numbers:
        return
    known = sorted(map(int, note_numbers))
    # A gap in a confirmed chapter's endnote numbering can leave the
    # corresponding upper marker intact even when its note label is corrupt.
    can_fill_gap = scoped and len(known) >= 3
    for parent in doc.body.iter():
        if covered(parent, marked):
            continue
        nodes = list(parent)
        cursor = 0
        while cursor < len(nodes):
            node = nodes[cursor]
            cursor += 1
            if tag(node) != "sup" or covered(node, marked):
                continue
            text = content(node)
            group = [node]
            # Calibre may split a single [8] into two adjacent <sup> nodes.
            while (text[:1] in "[［" and text[-1:] not in "]］" and cursor < len(nodes)
                   and len(group) < 4 and len(text) < 12 and not (group[-1].tail or "").strip()
                   and tag(nodes[cursor]) == "sup" and not covered(nodes[cursor], marked)):
                group.append(nodes[cursor])
                text += content(nodes[cursor])
                cursor += 1
            match = BRACKET_REF.fullmatch(text)
            if match:
                number = int(match.group(1))
                if str(number) in note_numbers or can_fill_gap and known[0] < number < known[-1]:
                    marked.update(dict.fromkeys(group, "注号或回链"))


def mark_circled_notes(doc, marked, has_endnotes):
    rows = [(node, value) for node, value in text_rows(doc.body) if not covered(node, marked)]
    recent = {}
    matched_styles = Counter()
    matched = {}
    remaining = []
    raw_refs = 0
    for index, (node, value) in enumerate(rows):
        if value[0] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳":
            reference = recent.pop(value[0], None)
            layout = (tag(node), node.get("class"))
            if reference is not None:
                matched[node] = value[0]
                matched_styles[layout] += 1
                ref_row, superscript = reference
                if superscript is not None:
                    marked[superscript] = "注号或回链"
                else:
                    for element in ref_row.iter():
                        if not covered(element, marked):
                            if element.text:
                                raw_refs += element.text.count(value[0])
                                element.text = element.text.replace(value[0], "")
                            if element is not ref_row and element.tail:
                                raw_refs += element.tail.count(value[0])
                                element.tail = element.tail.replace(value[0], "")
            else:
                remaining.append((index, node, value[0], layout))
            continue
        for upper in node.iter():
            symbol = content(upper) if tag(upper) == "sup" else ""
            if re.fullmatch(r"[①-⑳]", symbol):
                recent[symbol] = (node, upper)
        if has_endnotes:
            for symbol in set(re.findall(r"[①-⑳]", value[1:])):
                if symbol not in recent or recent[symbol][0] is not node:
                    recent[symbol] = (node, None)
    for node in matched:
        marked[node] = "脚注或注释块"
    for index, node, symbol, layout in remaining:
        if not has_endnotes or index == 0 or matched_styles[layout] < 2:
            continue
        previous, previous_text = rows[index - 1]
        if matched.get(previous) == symbol or APPENDIX_REF.search(previous_text):
            marked[node] = "脚注或注释块"
    return raw_refs


def strip_bracketed_refs(doc, marked, numbers):
    if not numbers:
        return 0
    count = 0
    for node, _ in text_rows(doc.body):
        if covered(node, marked):
            continue
        for element in node.iter():
            for field in ("text", "tail"):
                if field == "text" and covered(element, marked) or field == "tail" and element is node:
                    continue
                raw = getattr(element, field)
                if not raw:
                    continue

                def replace(match):
                    if str(int(match.group(1))) not in numbers:
                        return match.group()
                    prefix = match.string[:match.start()]
                    if not prefix.strip() or re.search(r"[a-z0-9_]$", prefix, re.I):
                        return match.group()
                    return ""

                edited, _ = BRACKET_REF.subn(replace, raw)
                if edited != raw:
                    count += len(BRACKET_REF.findall(raw)) - len(BRACKET_REF.findall(edited))
                    setattr(element, field, edited)
    return count


def append_document(destination, doc):
    group = etree.SubElement(destination, "div")
    group.text = doc.body.text
    for child in list(doc.body):
        for element in child.iter():
            if tag(element):
                element.tag = tag(element)
        group.append(child)
    etree.cleanup_namespaces(group)


def pandoc_plain(markup, executable):
    result = subprocess.run([executable, "-f", "html", "-t", "plain", "--wrap=none"],
                            input=etree.tostring(markup, method="html", encoding="utf-8"),
                            capture_output=True, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode:
        raise ValueError(f"Pandoc 退出码 {result.returncode}。{stderr}")
    return result.stdout.decode("utf-8-sig"), stderr


def clean_rendered(raw, title, marker=None):
    lines = raw.splitlines(keepends=True)
    positions = {}
    if marker is not None:
        for index, line in enumerate(lines):
            found = marker.search(line)
            if found is not None:
                positions[index] = found.group()
                after = line[found.end():]
                lines[index] = line[:found.start()] + (after[1:] if after.startswith(" ") else after)
    keys = [normalize(line) for line in lines]
    active = [index for index, key in enumerate(keys) if key]
    removed = {}
    mark_contents(keys, active, removed)
    mark_front(keys, active, removed, title_key(title))
    mark_page_noise(keys, active, removed)
    restored = {index: prefix + lines[index] for index, prefix in positions.items()}
    return render(lines, removed, restored).strip(), Counter(removed.values())


def split_navigation(docs, nav, marked, title):
    locations = [(doc.path, entry) for doc in docs if doc.body not in marked
                 for entry in nav[doc.path] if not kind_of(entry.title)]
    active = [entry for _, entry in locations]
    title_token = label_key(title)
    groups = {parent for entry in active for parent in entry.parents
              if named_kind(parent) == "分部" or (label_key(parent) and title_token.startswith(label_key(parent)))}
    targets = defaultdict(set)
    for path, entry in locations:
        if entry.parents:
            targets[entry.parents].add(path)
    anthology = {entry.title for path, entry in locations if not entry.parents
                 and not named_kind(entry.title) and not chapter_key(entry.title)
                 and len(targets.get((entry.title,), ())) > 1
                 and path in targets[(entry.title,)]}
    if len(anthology) >= 2:
        groups.update(anthology)
    numbered_roots = {entry.parents for entry in active if named_kind(entry.title) == "正文"}
    has_major = any(navigation_is_major(entry, groups, numbered_roots) for entry in active
                    if not named_kind(entry.title))
    explicit_chapters = any(
        any(named_kind(value) == "正文" for node, value in text_rows(doc.body) if tag(node) in HEADINGS)
        or any("chapter" in semantics(node) for node in doc.body.iter())
        for doc in docs if doc.body not in marked
    )
    return groups, numbered_roots, not (has_major or numbered_roots or explicit_chapters)


def unmarked_title(value):
    return re.sub(r"\s*(?:[\[［]\s*\d{1,3}\s*[\]］]|[(（]\s*\d{1,2}\s*[)）])\s*$", "", value)


def mark_sections(doc, rows, entries, policy, marker_prefix, decisions):
    groups, numbered_roots, use_heading_fallback = policy
    by_label = defaultdict(list)
    for entry in entries:
        by_label[label_key(unmarked_title(entry.title))].append(entry)
    used_entries = set()
    containers = set()
    minimum_level = min((tag(node) for node, _ in rows if tag(node) in HEADINGS), default=None)
    for node, value in rows:
        name = tag(node)
        if name not in HEADINGS | {"p"} or any(
            tag(parent) in {"li", "table", "blockquote", "aside"} for parent in node.iterancestors()
        ):
            continue
        roles = semantics(node)
        for parent in node.iterancestors():
            if tag(parent) in {"body", "section", "article", "div"} and parent not in containers:
                roles.update(semantics(parent))
                containers.add(parent)
        semantic_kind = next((kind for role, kind in EXTRA_ROLE.items() if role in roles), None)
        if semantic_kind is None and "chapter" in roles:
            semantic_kind = "正文"
        if semantic_kind is None and "part" in roles:
            semantic_kind = "分部"

        title = unmarked_title(value)
        label = label_key(title)
        row_id = node.get("id") or node.get("name")
        candidates = [entry for entry in by_label.get(label, ()) if entry not in used_entries]
        entry = next((item for item in candidates if item.fragment and item.fragment == row_id), None)
        if entry is None:
            entry = next((item for item in candidates if not item.fragment or name in HEADINGS), None)
        if entry is None and row_id:
            entry = next((item for item in entries if item.fragment == row_id
                          and item not in used_entries and name in HEADINGS), None)
        if entry is not None:
            used_entries.add(entry)
        major_nav = entry is not None and navigation_is_major(entry, groups, numbered_roots)
        heading = name in HEADINGS or entry is not None
        source_kind = named_kind(title) if heading or title in {
            "序", "序言", "前言", "译者说明", "译者前言", "后记", "後記", "附录", "附錄",
        } else None
        book_level = entry is not None and all(parent in groups for parent in entry.parents)
        if source_kind == "书后内容" and not book_level and semantic_kind != "书后内容":
            source_kind = None
        if source_kind == "正文" and not heading:
            source_kind = None

        kind = source_kind or semantic_kind
        if kind is None and heading and not kind_of(value) and len(value) <= 120:
            if entry is not None and entry.title in groups:
                kind = "分部"
            elif major_nav or use_heading_fallback and name == minimum_level:
                if not MINOR.search(normalize(value)) and not FRONT_EXTRA.fullmatch(normalize(value).rstrip("*")):
                    kind = "正文"
        if kind is not None:
            display = title if heading or len(title) <= 32 else kind
            token = f"{marker_prefix}{len(decisions)}z"
            decisions[token] = Section(display, kind)
            node.text = token + " " + (node.text or "")


def remove_split_markers(text, marker, decisions):
    chunks = []
    anchors = []
    offset = 0
    cursor = 0
    for match in marker.finditer(text):
        plain = text[cursor:match.start()]
        chunks.append(plain)
        offset += len(plain)
        anchors.append((offset, decisions[match.group()]))
        cursor = match.end()
    chunks.append(text[cursor:])
    return "".join(chunks), anchors


def build_narration(source, pandoc, book_title=None, split=False):
    with ZipFile(source) as archive:
        title, docs, nav, guide, skipped = read_package(archive)
    title = book_title or title or source.stem
    chapters = []
    decisions = []
    marked = {}
    note_numbers = set()
    notes_by_chapter = defaultdict(set)
    for doc in docs:
        rows = text_rows(doc.body)
        reason = whole_document(doc, rows, nav[doc.path], guide[doc.path])
        if reason is not None:
            decisions.append({"路径": doc.path, "原因": reason})
            marked[doc.body] = "整份注释文档" if reason == "注释" else reason
            if reason == "注释":
                endnote_groups(rows, notes_by_chapter, note_numbers)
            continue
        classify_nodes(doc, marked)

    mark_note_links(docs, marked)
    printable = etree.Element("html")
    body = etree.SubElement(printable, "body")
    counts = Counter()
    retained_files = 0
    sections = {}
    if split:
        prefix = f"epubtts{uuid4().hex}p"
        marker = re.compile(rf"{prefix}\d+z")
        split_policy = split_navigation(docs, nav, marked, title)
    else:
        marker = None
    for doc in docs:
        if doc.body in marked:
            continue
        heading = next((entry.title for entry in nav[doc.path] if not entry.fragment), "")
        scoped_notes = notes_by_chapter.get(chapter_key(heading), set())
        mark_unlinked_superscripts(doc, marked, scoped_notes or note_numbers, bool(scoped_notes))
        stripped = mark_circled_notes(doc, marked, bool(scoped_notes))
        stripped += strip_bracketed_refs(doc, marked, scoped_notes)
        if stripped:
            counts["注号或回链"] += stripped
        for node in list(doc.body.iterdescendants()):
            if node in marked and not any(parent in marked for parent in node.iterancestors()):
                counts[marked[node]] += 1
                remove_node(node)
        if not content(doc.body).strip("\ufeff\u200b "):
            continue
        labels = {label_key(entry.title) for entry in nav[doc.path] if not kind_of(entry.title)}
        rows = text_rows(doc.body)
        titles = [value for node, value in rows
                  if tag(node) in HEADINGS or label_key(value) in labels]
        if not titles:
            first_title = next((entry.title for entry in nav[doc.path]
                                if not entry.fragment and not kind_of(entry.title)), None)
            if first_title:
                head = etree.Element("h2")
                head.text = first_title
                doc.body.insert(0, head)
                titles.append(first_title)
                rows.insert(0, (head, first_title))
        if split:
            mark_sections(doc, rows, nav[doc.path], split_policy, prefix, sections)
        chapters.extend(titles)
        retained_files += 1
        append_document(body, doc)

    if not retained_files:
        raise ValueError("没有可提取的正文，可能是图片版 EPUB。")
    raw, pandoc_warning = pandoc_plain(printable, pandoc)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("\ufeff", "").replace("\u200b", "").replace("\u00ad", "")
    text, removed = clean_rendered(raw, title, marker)
    anchors = []
    if split:
        text, anchors = remove_split_markers(text, marker, sections)
    if not text:
        raise ValueError("清洗后的正文为空。")
    report = {
        "文件": str(source),
        "书名": title,
        "阅读顺序文件数": len(docs),
        "保留文件数": retained_files,
        "跳过文件": decisions,
        "未读入的条目": skipped,
        "删除元素": dict(sorted(counts.items())),
        "纯文本补充清洗": dict(removed),
        "可定位标题": chapters,
        "输出字符数": len(text),
        "Pandoc 提示": pandoc_warning,
    }
    text += "\n"
    parts = []
    if split:
        parts = split_parts(text, anchors, label_key)
        report["分段"] = [{"文件": part.filename, "标题": part.section.title,
                         "类型": part.section.kind, "字符数": len(part.text.rstrip())} for part in parts]
        if not anchors:
            report["分段提示"] = "没有识别到可分割的章节标题，整本正文收在一份 TXT 中。"
    return text, report, parts


def extract(source, pandoc, book_title=None):
    text, report, _ = build_narration(source, pandoc, book_title)
    return text, report


def write_split_output(destination, parts, report):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tts-", dir=destination.parent) as temporary:
        book_folder = Path(temporary) / "book"
        book_folder.mkdir()
        for part in parts:
            write_new(book_folder / part.filename, part.text)
        if report is not None:
            write_new(book_folder / "清洗记录.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if destination.exists():
            raise FileExistsError(f"输出已存在：{destination}")
        book_folder.rename(destination)


def narration_path(source, directory, split):
    suffix = ".tts" if split else ".tts.txt"
    return directory / f"{source.stem}{suffix}"


def existing_output(target, split, with_report):
    if target.exists():
        return target
    if not split and with_report:
        audit = target.with_suffix(".cleanup.json")
        if audit.exists():
            return audit
    return None


def write_narration(target, text, parts, report):
    if parts is not None:
        write_split_output(target, parts, report)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_new(target, text)
        if report is not None:
            write_new(target.with_suffix(".cleanup.json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="把本地 EPUB 转成 UTF-8 朗读稿。")
    parser.add_argument("input", type=Path, help="一份 EPUB 或包含 EPUB 的文件夹")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", type=Path, help="单本 EPUB 的输出 TXT；分段时为该书的输出目录")
    output.add_argument("-d", "--output-dir", type=Path, help="输出目录")
    parser.add_argument("--split", action="store_true", help="按主要章节和序言、后记等分成多个有序 TXT")
    parser.add_argument("--report", action="store_true", help="同时生成每本书的 JSON 清洗记录")
    parser.add_argument("--preview", action="store_true", help="显示清洗记录，不写文件")
    parser.add_argument("--title", help="为单本书指定扉页书名")
    parser.add_argument("--pandoc", default="pandoc", help="Pandoc 命令或可执行文件路径")
    args = parser.parse_args(argv)
    source = args.input.resolve()
    batch = source.is_dir()
    if batch and (args.output or args.title):
        parser.error("文件夹模式仅支持 --output-dir，书名采用各 EPUB 的元数据。")
    if args.output is not None and not args.split and args.output.suffix.lower() != ".txt":
        parser.error("--output 必须使用 .txt 扩展名。")
    if not source.exists():
        parser.error(f"输入不存在：{source}")
    if not batch and source.suffix.lower() != ".epub":
        parser.error("输入文件必须以 .epub 结尾。")
    sources = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() == ".epub") if batch else [source]
    if not sources:
        parser.error("文件夹中没有 EPUB 文件。")
    executable = shutil.which(args.pandoc)
    if executable is None:
        parser.error("找不到 Pandoc。请安装 Pandoc 或通过 --pandoc 指定位置。")

    failures = 0
    for book in sources:
        target = args.output or narration_path(book, args.output_dir or book.parent, args.split)
        try:
            collision = None if args.preview else existing_output(target, args.split, args.report)
            if collision is not None:
                if batch:
                    print(f"跳过已有输出 {target}")
                    continue
                raise FileExistsError(f"输出已存在：{collision}")
            text, report, parts = build_narration(book, executable, args.title, args.split)
            if args.preview:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                continue
            write_narration(target, text, parts if args.split else None, report if args.report else None)
            if args.split:
                print(f"已生成 {target}（{len(parts)} 个 TXT）")
            else:
                print(f"已生成 {target}（{report['输出字符数']:,} 字符）")
        except (OSError, ValueError, BadZipFile, etree.LxmlError, KeyError) as error:
            failures += 1
            print(f"转换失败 {book.name}：{error}", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
