"""Recognize and reassemble page images from a scanned EPUB."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from statistics import median
from tempfile import TemporaryDirectory

from PIL import Image

from clean_for_tts import normalize
from epub_reflow import join_separator
from epub_sections import CHAPTER, Section, named_kind
from windows_ocr import run_windows_ocr


XLINK = "{http://www.w3.org/1999/xlink}href"
RASTER = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff"}
HAN = r"\u3400-\u9fff\uf900-\ufaff"
SPACING = f"{HAN}，。！？；：、（）《》〈〉【】「」『』“”‘’…—·"
OCR_SPACES = re.compile(rf"(?<=[{SPACING}]) +(?=[{SPACING}])")
OCR_PUNCTUATION = re.compile(r"\s+([,.!?;:)\]，。！？；：、）》」』”’])")
PAGE_NUMBER = re.compile(r"(?:第\s*)?[-·—•(（ ]*\d{1,4}[-·—•)） ]*(?:页)?|[IVXLCDM]{1,8}", re.I)
PAGE_NOTE = re.compile(r"^(?:[①-⑳]|[\[［〔]\s*\d{1,3}\s*[\]］〕]|[＊*†‡])")
IGNORED_SECTIONS = {"封面", "封底", "书名", "扉页", "版权", "版权页", "目录", "总目录", "注释"}
EDITION_INTRO = re.compile(r"^.{0,18}(?:版|本)(?:新)?(?:序言|前言|自序)$")
OTHER_SECTIONS = {
    "引言": "序言", "英文版自序": "序言", "结论": "正文", "结语": "正文",
    "书目提要": "书后内容", "译名对照表": "附录",
}
FRONT_SECTIONS = {"序言", "译者说明"}


@dataclass(frozen=True)
class ScanPage:
    image: str
    skip: str | None
    sections: tuple[Section, ...]
    subtitles: tuple[str, ...]


def page_images(archive, doc, tag, resolve, sizes):
    anchors = set()
    for node in doc.body.iter():
        anchors.update(value for value in (node.get("id"), node.get("name")) if value)
        if tag(node) not in {"img", "image"}:
            continue
        href = node.get("src") or node.get(XLINK) or node.get("href")
        point = resolve(doc.path, href)
        if not point or PurePosixPath(point[0]).suffix.lower() not in RASTER:
            continue
        image = point[0]
        if image not in sizes:
            try:
                with archive.open(image) as stream, Image.open(stream) as raster:
                    sizes[image] = raster.size
            except (KeyError, OSError, Image.DecompressionBombError):
                sizes[image] = (0, 0)
        width, height = sizes[image]
        if min(width, height) >= 300 and 0.46 <= width / height <= 1.35:
            yield image, anchors, width / height
            anchors = set()


def scan_kind(title):
    clean = normalize(title)
    return named_kind(clean) or OTHER_SECTIONS.get(clean) or (
        "序言" if EDITION_INTRO.fullmatch(clean) else None
    )


def find_scan_pages(archive, docs, nav, guide, tag, content, resolve, kind_of):
    if not any(tag(node) in {"img", "image"} for doc in docs for node in doc.body.iter()):
        return []
    letters = sum(len(content(doc.body)) for doc in docs)
    sizes = {}
    readings = []
    for doc in docs:
        if "nav" in doc.properties or re.fullmatch(r"(?:cover|titlepage|copyrightpage)",
                                                   PurePosixPath(doc.path).stem, re.I):
            continue
        readings.extend((doc.path, *row) for row in page_images(archive, doc, tag, resolve, sizes))
    if len({row[1] for row in readings}) < 3 or letters > max(650, 12 * len(readings)):
        return []
    typical_ratio = median(row[3] for row in readings)
    if sum(abs(ratio - typical_ratio) <= typical_ratio * 0.15 for *_, ratio in readings) < len(readings) * 0.7:
        return []

    bookmarks = defaultdict(dict)
    for path in {row[0] for row in readings}:
        entries = nav[path]
        explicit = any(CHAPTER.match(normalize(entry.title)) for entry in entries)
        by_anchor = defaultdict(list)
        for entry in entries:
            by_anchor[entry.fragment].append(entry)
        guide_labels = dict((anchor, label) for label, anchor in sorted(guide[path]))
        for anchor in dict.fromkeys((*by_anchor, *guide_labels)):
            excluded = guide_labels.get(anchor)
            sections = []
            subtitles = []
            for entry in by_anchor[anchor]:
                title = normalize(entry.title)
                ignored = kind_of(title)
                if ignored or title in IGNORED_SECTIONS:
                    excluded = ignored or title
                    continue
                kind = scan_kind(title)
                if not kind and not explicit and not entry.parents:
                    kind = "正文"
                if kind:
                    section = Section(title, kind)
                    if section not in sections:
                        sections.append(section)
                else:
                    subtitles.append(title)
            bookmarks[path][anchor] = excluded, tuple(sections), tuple(subtitles)

    pages = []
    exclusion = None
    previous_path = None
    for path, image, anchors, _ in readings:
        if path != previous_path:
            anchors = anchors | {""}
            previous_path = path
        markers = [bookmark for anchor, bookmark in bookmarks[path].items() if anchor in anchors]
        sections = tuple(dict.fromkeys(section for _, items, _ in markers for section in items))
        subtitles = tuple(dict.fromkeys(title for _, _, items in markers for title in items))
        excluded = next((reason for reason, _, _ in markers if reason), None)
        if sections or subtitles:
            exclusion = None
        elif excluded:
            exclusion = excluded
        pages.append(ScanPage(image, exclusion, sections, subtitles))
    return pages


def normalize_line(text):
    clean = OCR_SPACES.sub("", " ".join(text.split()))
    return OCR_PUNCTUATION.sub(r"\1", clean)


def edge_key(line, height, label_key):
    if line["top"] > height * 0.12 and line["bottom"] < height * 0.89:
        return ""
    text = re.sub(r"^\d+\s*|\s*\d+$", "", line["text"])
    return label_key(text) if 3 <= len(text) <= 26 else ""


def page_layout(record, recurring, label_key):
    height = record["height"]
    lines = sorted(record["lines"], key=lambda line: (line["top"], line["left"]))
    filtered = []
    for original in lines:
        line = dict(original, text=normalize_line(original["text"]))
        edge = line["top"] <= height * 0.12 or line["bottom"] >= height * 0.89
        if not line["text"] or (edge and (PAGE_NUMBER.fullmatch(line["text"])
                                              or edge_key(line, height, label_key) in recurring)):
            continue
        filtered.append(line)
    return filtered


def find_heading(lines, title, label_key, numbered=False):
    target = label_key(title)
    for index, line in enumerate(lines):
        current = label_key(line["text"])
        if current == target:
            return index, 1
        if current and index + 1 < len(lines) and current + label_key(lines[index + 1]["text"]) == target:
            return index, 2
    number = CHAPTER.match(normalize(title)) if numbered else None
    if number is not None:
        prefix = label_key(number.group())
        for index, line in enumerate(lines[:12]):
            if label_key(line["text"]) == prefix:
                return index, 1
    return None


def printed_front_heading(lines, width, height):
    if len(lines) < 2:
        return None
    font = median(line["bottom"] - line["top"] for line in lines)
    for index, line in enumerate(lines[:min(3, len(lines) - 1)]):
        title = line["text"]
        if len(title) > 32 or re.search(r"[。！？.!?]$", title):
            continue
        kind = scan_kind(title)
        if (kind in FRONT_SECTIONS and height * 0.10 < line["top"] < height * 0.6
                and abs((line["left"] + line["right"]) / 2 - width / 2) < width * 0.17
                and (line["bottom"] - line["top"] > font * 1.18
                     or lines[index + 1]["top"] - line["bottom"] > font * 1.65)):
            return index, Section(title, kind)
    return None


def readable_ocr(archive, pages, language, progress):
    with TemporaryDirectory(prefix="epub-ocr-") as temporary:
        folder = Path(temporary)
        todo = []
        for number, page in enumerate(pages, 1):
            if page.skip:
                continue
            image = folder / f"{number}{PurePosixPath(page.image).suffix.lower()}"
            with archive.open(page.image) as source, image.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            todo.append({"number": number, "file": str(image)})
        if not todo:
            raise ValueError("扫描版 EPUB 没有定位到可识别的正文页。")
        manifest = folder / "pages.json"
        manifest.write_text(json.dumps(todo, ensure_ascii=False), encoding="utf-8")
        if progress:
            progress(f"正在识别扫描页　0/{len(todo)}")
        results = {}
        ocr_language = language
        for record in run_windows_ocr(manifest, language):
            if "language" in record:
                ocr_language = record["language"]
            if "number" in record:
                results[record["number"]] = record
                count = len(results)
                if progress and (count % 20 == 0 or count == len(todo)):
                    progress(f"正在识别扫描页　{count}/{len(todo)}")
        return ocr_language, {row["number"]: results.get(row["number"], {"error": "未返回识别结果"})
                              for row in todo}


def paragraphs(lines):
    if not lines:
        return [], 0, 0
    font = median(max(1, line["bottom"] - line["top"]) for line in lines)
    widths = sorted(line["right"] - line["left"] for line in lines)
    width = widths[int((len(widths) - 1) * 0.8)]
    full_lines = [line for line in lines if line["right"] - line["left"] >= width * 0.88]
    left = min(line["left"] for line in full_lines)
    right = median(line["right"] for line in full_lines)
    body = []
    previous = None
    joined = removed = 0
    footnote = False
    for line in lines:
        value = line["text"]
        is_small = line["bottom"] - line["top"] < font * 0.8
        if is_small and (footnote or (previous is not None and PAGE_NOTE.match(value)
                                     and line["top"] - previous["bottom"] > font * 0.7)):
            footnote = True
            removed += 1
            continue
        footnote = False
        if (previous is not None and previous["right"] >= right - 1.65 * font
                and abs(line["left"] - left) < 0.85 * font
                and previous["left"] <= left + 2.7 * font
                and 0 <= line["top"] - previous["bottom"] <= 1.05 * font
                and min(len(previous["text"]), len(value)) > 4):
            body[-1] += join_separator(body[-1], value) + value
            joined += 1
        else:
            body.append(value)
        previous = line
    return body, joined, removed


def render_scan(archive, pages, language, label_key, progress=None):
    ocr_language, results = readable_ocr(archive, pages, language, progress)
    clean = [(number, record) for number, record in results.items() if "lines" in record]
    edge_counts = Counter()
    for _, record in clean:
        edge_counts.update({edge_key(dict(line, text=normalize_line(line["text"])), record["height"], label_key)
                            for line in record["lines"]})
    recurring = {key for key, count in edge_counts.items() if key and count >= 4}
    chunks = []
    cuts = []
    offset = joined = removed = body_chars = 0
    fallback = []
    unmatched = []
    empty = []
    chapter_started = False
    for number, record in clean:
        page = pages[number - 1]
        lines = page_layout(record, recurring, label_key)
        if not lines:
            empty.append(number)
        headings = {}
        printed = printed_front_heading(lines, record["width"], record["height"]) if not chapter_started else None
        if printed:
            index, section = printed
            headings[index] = (1, section, section.title)
        for section in page.sections:
            match = find_heading(lines, section.title, label_key, section.kind == "正文")
            if match and match[0] not in headings:
                headings[match[0]] = (match[1], section, section.title)
            elif section.kind in FRONT_SECTIONS and (printed or chapter_started):
                if not printed:
                    unmatched.append({"图像页": number, "标题": section.title})
            elif not match and len(page.sections) == 1:
                headings[0] = (0, section, section.title)
                fallback.append({"图像页": number, "标题": section.title})
            else:
                unmatched.append({"图像页": number, "标题": section.title})
        for subtitle in page.subtitles:
            match = find_heading(lines, subtitle, label_key)
            if match and match[0] not in headings:
                headings[match[0]] = (match[1], None, subtitle)
        index = 0
        for stop in sorted(headings.keys() | {len(lines)}):
            if stop < index:
                continue
            body, merged, footnotes = paragraphs(lines[index:stop])
            if body:
                chunk = "\n\n".join(body) + "\n\n"
                chunks.append(chunk)
                offset += len(chunk)
                body_chars += len(chunk)
            joined += merged
            removed += footnotes
            if stop in headings:
                span, section, display = headings[stop]
                if section is not None:
                    cuts.append((offset, section))
                    chapter_started |= section.kind == "正文"
                chunk = display + "\n\n"
                chunks.append(chunk)
                offset += len(chunk)
                index = stop + span
    if body_chars < 120:
        raise ValueError("系统 OCR 没有识别出足够的正文，请检查扫描清晰度和 OCR 语言。")
    failed = [{"图像页": number, "错误": record["error"]} for number, record in results.items() if "error" in record]
    skipped = Counter(page.skip for page in pages if page.skip)
    report = {
        "系统语言": ocr_language, "图像页数": len(pages), "识别有字页数": len(clean) - len(empty),
        "跳过图像页": dict(skipped), "未识别出文字的图像页": empty, "识别失败页": failed,
        "按目录页定位的标题": fallback, "未确认的目录标题": unmatched,
        "合并印刷行": joined, "删除疑似页脚注释行": removed,
    }
    return "".join(chunks).rstrip() + "\n", cuts, report
