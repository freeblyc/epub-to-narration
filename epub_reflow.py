"""Repair wrapped prose while the EPUB's paragraph structure is still available."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import unicodedata

from clean_for_tts import (
    CIP, FRONT_LABEL, FRONT_PAGE, ISBN, NOTE_HEADING, OPTIONAL, PAGE_LINE, RUNNING_LABEL, TOC, normalize,
)


STRUCTURE = re.compile(
    r"^(?:#{1,6}\s|[-*+•]\s|\d+[.)、]\s|[①-⑳]|[一二三四五六七八九十]+、|"
    r"第[一二三四五六七八九十百0-9]+[章节節回编編卷篇部]|>\s|\|.*\||\x60{3}|~~~|"
    r"[［\[]\s*\d+[］\]])"
)
CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
SENTENCE_END = re.compile(r"""[。！？.!?…」』”’"）》】]$""")
FORMATTED = re.compile(
    r"(?:^|[-_\s:])(?:verse|poem|poetry|stanza|lyrics?|song|epigraph|"
    r"code|address|signature|caption|center|right|footnote|endnote)(?:$|[-_\s])", re.I
)
PRE_SPACE = re.compile(r"white-space\s*:\s*(?:pre|break-spaces)", re.I)
PROTECTED_TAGS = {
    "address", "aside", "blockquote", "code", "dd", "dl", "dt", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "li", "nav", "ol", "pre",
    "table", "td", "th", "ul",
}
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"


@dataclass
class Line:
    node: object
    text: str
    indent: int
    width: int


@dataclass(frozen=True)
class Layout:
    width: int
    paragraph_indent: int


def element_tag(node):
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def has_layout_role(node):
    return any(FORMATTED.search(node.get(attr, "")) for attr in ("class", "role", EPUB_TYPE, "epub:type")) or (
        PRE_SPACE.search(node.get("style", "")) is not None
        or re.search(r"text-align\s*:\s*(?:center|right)", node.get("style", ""), re.I) is not None
    )


def is_structure(line):
    if STRUCTURE.match(line):
        return True
    value = normalize(line)
    return any(rule.fullmatch(value) for rule in (
        CIP, FRONT_LABEL, FRONT_PAGE, ISBN, NOTE_HEADING, OPTIONAL, PAGE_LINE, RUNNING_LABEL, TOC,
    ))


def indentation(text):
    lead = re.match(r"[ \t\u00a0\u3000]*", text).group()
    return sum(4 if c == "\t" else 2 if c == "\u3000" else 1 for c in lead)


def display_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
               for c in text)


def join_separator(previous, following):
    # In Chinese text a break can fall inside a word or between a number and its unit.
    # Latin prose still needs a space after ordinary sentence punctuation.
    left, right = previous[-1], following[0]
    if (CJK.search(left) or CJK.search(right)
            or left in "，。！？；：、）》」』】”’([{/-\u00ad\u2011"
            or right in ",.!?;:)]}，。！？；：、）》」』】”’"
            or (left.isdecimal() and right.isdecimal())):
        return ""
    return " "


def flows(node, runs, breaks):
    if node.text:
        runs[-1].append((node, "text"))
    for child in node:
        child_tag = element_tag(child)
        if child_tag == "br":
            breaks.append(child)
            runs.append([])
        elif child_tag:
            flows(child, runs, breaks)
        if child.tail:
            runs[-1].append((child, "tail"))


def trim_runs(slots, leading):
    sequence = slots if leading else reversed(slots)
    for node, field in sequence:
        value = getattr(node, field) or ""
        trimmed = value.lstrip() if leading else value.rstrip()
        setattr(node, field, trimmed)
        if trimmed:
            break


def append_text(node, text):
    if len(node):
        node[-1].tail = (node[-1].tail or "") + text
    else:
        node.text = (node.text or "") + text


def replace_break(br, separator):
    parent = br.getparent()
    previous = br.getprevious()
    tail = separator + (br.tail or "")
    if previous is None:
        parent.text = (parent.text or "") + tail
    else:
        previous.tail = (previous.tail or "") + tail
    parent.remove(br)


def repair_inside(node):
    if not any(element_tag(child) == "br" for child in node.iterdescendants()):
        return 0
    if any(element_tag(child) in PROTECTED_TAGS or has_layout_role(child)
           for child in node.iterdescendants() if element_tag(child)):
        return 0
    runs, breaks = [[]], []
    flows(node, runs, breaks)
    raw = ["".join(getattr(element, field) or "" for element, field in run) for run in runs]
    lines = [line.strip() for line in raw]
    if any(is_structure(line) for line in lines if line):
        return 0
    nonempty = [line for line in lines if line]
    if len(nonempty) >= 3 and all(display_width(line) <= 19 for line in nonempty):
        return 0

    repaired = 0
    # A break can own the tail of the line to its right. Start at the end so
    # the preceding breaks still own their text while each pair is repaired.
    for br, left, right, left_slots, right_slots, source_right in reversed(list(zip(
        breaks, lines, lines[1:], runs, runs[1:], raw[1:]
    ))):
        if not left or not right or indentation(source_right) >= 4:
            continue
        trim_runs(left_slots, leading=False)
        trim_runs(right_slots, leading=True)
        replace_break(br, join_separator(left, right))
        repaired += 1
    return repaired


def infer_layout(lines):
    meaningful = [line for line in lines if line.indent < 8]
    if len(meaningful) < 10:
        return None
    widths = Counter(line.width for line in meaningful if 40 <= line.width <= 180)
    if not widths:
        return None
    width = max(widths, key=lambda n: (sum(widths[x] for x in range(n - 3, n + 4)), widths[n], n))
    full = [line for line in meaningful if width - 6 <= line.width <= width + 6]
    indent_count = sum(line.indent >= 4 for line in full)
    no_indent = sum(line.indent < 4 for line in full)
    indent = 4 if indent_count >= 2 and no_indent >= max(4, indent_count) else 0
    minimum_full = 0.46 if indent else 0.56
    if len(full) < 8 or len(full) < len(meaningful) * minimum_full:
        return None
    if sum(not SENTENCE_END.search(line.text) for line in full) < len(full) * 0.45:
        return None
    return Layout(width, indent)


def merge_lines(lines, layout):
    repaired = 0
    previous = None
    target = None
    for line in lines:
        node = line.node
        if (previous is not None and node.getprevious() is target
                and not (target.tail or "").strip()
                and previous.indent < 8 and line.indent < 4
                and layout.width - 8 <= previous.width <= layout.width + 7
                and line.width <= layout.width + 12
                and (layout.paragraph_indent or not SENTENCE_END.search(previous.text))):
            left_slots, right_slots = [[]], [[]]
            flows(target, left_slots, [])
            flows(node, right_slots, [])
            trim_runs(left_slots[-1], leading=False)
            trim_runs(right_slots[0], leading=True)
            append_text(target, join_separator(previous.text, line.text) + (node.text or ""))
            for child in list(node):
                target.append(child)
            target.tail = node.tail
            node.getparent().remove(node)
            repaired += 1
        else:
            target = node
        previous = line
    return repaired


def reflow_document(body, titles, fragments, title_key):
    inside = 0
    groups = defaultdict(list)
    for node in list(body.iter()):
        name = element_tag(node)
        if name not in {"p", "div"}:
            continue
        if name == "div" and (not node.text or not node.text.strip() or any(
            element_tag(child) in {"p", "div", "section", "article", "ol", "ul", "table"}
            for child in node
        )):
            continue
        if has_layout_role(node) or any(
            element_tag(parent) in PROTECTED_TAGS or has_layout_role(parent) for parent in node.iterancestors()
        ):
            continue
        raw = "".join(node.itertext())
        line = " ".join(raw.split())
        if (not line or is_structure(line)
                or (len(line) <= 130 and title_key(line) in titles)
                or (node.get("id") or node.get("name")) in fragments
                or (fragments and any((child.get("id") or child.get("name")) in fragments
                                      for child in node.iter() if element_tag(child)))):
            continue

        inside += repair_inside(node)
        if name != "p" or node.get("id") or any(element_tag(child) in {
            "br", "div", "img", "p", "code", "pre", "table", "ul", "ol",
        } or has_layout_role(child) for child in node.iterdescendants() if element_tag(child)):
            continue
        if len(line) < 2:
            continue
        text = " ".join("".join(node.itertext()).split())
        signature = (node.getparent(), node.get("class"), node.get("style"), node.get("align"))
        groups[signature].append(Line(node, text, indentation(raw), display_width(text)))

    merged = 0
    for lines in groups.values():
        layout = infer_layout(lines)
        if layout is not None:
            merged += merge_lines(lines, layout)
    return inside, merged
