"""Align a text EPUB's chapter directory with its original reading order."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import re

from clean_for_tts import normalize
from epub_reflow import element_tag
from epub_sections import FRONT_EXTRA, MINOR, NUMBER, NavEntry, named_kind


HEADINGS = {f"h{i}" for i in range(1, 7)}
BLOCKS = HEADINGS | {"p", "blockquote", "div", "section", "article", "li", "dt", "dd"}
IMAGES = {"img", "svg"}
CHAPTER_NUMBER = re.compile(
    rf"^(第\s*{NUMBER}\s*[章讲講]|第\s*{NUMBER}\s*回(?!合)|chapter\s+[0-9ivxlcdm]+\b)", re.I
)
ORDINAL = re.compile(rf"^{NUMBER}[、\s]+\S")
PART = re.compile(rf"^(?:第\s*{NUMBER}\s*(?:部分|卷|篇|部|编|編|册)|卷\s*{NUMBER}|[上下中][篇卷编部])")
ROUND_HEADING = re.compile(rf"^第\s*{NUMBER}\s*回合")
CHAPTER_REFERENCE = re.compile(rf"^第\s*{NUMBER}\s*[章讲講回](?:的|中|里|所|已|介绍了|提到|讨论了)")
AUXILIARY = re.compile(
    r"^(?:推荐序|中文版序|总序|第.{1,9}版[前序]|(?:美国|英国)版序言|导论$|简介$|致谢|制作信息|献$|"
    r"书前内容|测一测|术语索引|参考[数据书目]|推荐阅读|延伸阅读|结语|代跋|出版后记|版[权權]信息|"
    r"获取更多|主编的话|abstract$|实用资料|历史年表|绪论$|编委会|编写组|《.+》(?:编委会|编写组)|"
    r"大事记|感悟与致谢|致谢与感悟)", re.I
)
TITLE_NOTE = re.compile(r"(?:[（(]\s*\d{1,3}\s*[)）]|[［\[]\s*\d{1,3}\s*[\]］]|注\d{1,3})$")
LITERAL_SPACE = re.compile(r"&nbsp;|&#160;|&#xa0;", re.I)
MAX_TITLE = 115


@dataclass(frozen=True)
class Block:
    node: object
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Match:
    path: str
    block: Block
    entry: NavEntry
    score: int
    reason: str
    from_image: bool = False


class ChapterDocument:
    def __init__(self, body, marked, title_key):
        self.blocks = []
        self.bookmarks = {}
        self.labels = defaultdict(list)
        self.pairs = defaultdict(list)
        nodes = []
        spans = {}
        excluded = set()
        containing_blocks = set()
        for node in body.iter():
            if not element_tag(node):
                continue
            parent = node.getparent()
            reason = marked.get(node)
            image = element_tag(node) in IMAGES and reason == "图像、隐藏或非文本元素"
            if (parent in excluded or (reason and not image)
                    or node.get("hidden") is not None or node.get("aria-hidden") == "true"):
                excluded.add(node)
                continue
            index = len(nodes)
            nodes.append(node)
            spans[node] = [index, index]
        for node in reversed(nodes):
            parent = node.getparent()
            if parent in spans:
                spans[parent][1] = max(spans[parent][1], spans[node][1])
                if element_tag(node) in BLOCKS or node in containing_blocks:
                    containing_blocks.add(parent)
        for node in nodes:
            name = element_tag(node)
            low, high = spans[node]
            for bookmark in (node.get("id"), node.get("name")):
                if bookmark:
                    self.bookmarks.setdefault(bookmark, (low, high))
            label = ""
            if name in BLOCKS and node not in containing_blocks:
                label = " ".join("".join(node.itertext()).split())
            if label or name in IMAGES:
                self.blocks.append(Block(node, low, high, label))
        self.first_text = next((i for i, block in enumerate(self.blocks) if block.text), 0)
        for i, block in enumerate(self.blocks):
            if not 0 < len(block.text) <= MAX_TITLE:
                continue
            self.labels[title_key(block.text)].append(i)
            if i + 1 < len(self.blocks):
                following = self.blocks[i + 1].text
                if following and len(following) + len(block.text) <= MAX_TITLE:
                    self.pairs[title_key(block.text + following)].append(i)


def chapter_title(value):
    return normalize(LITERAL_SPACE.sub(" ", value))


def chapter_number(value):
    match = CHAPTER_NUMBER.match(chapter_title(value))
    return match[0] if match else ""


def numbered(value):
    return bool(chapter_number(value) or ORDINAL.match(chapter_title(value)))


def volume(value):
    return bool(PART.match(chapter_title(value)) or named_kind(value) == "分部")


def auxiliary(value):
    title = chapter_title(value)
    return bool(
        AUXILIARY.match(title) or FRONT_EXTRA.fullmatch(title)
        or named_kind(title) in {"序言", "译者说明", "后记", "附录", "书后内容"}
        or title in {"封面", "书名"} or "书名页" in title
    )


def major_entries(entries, kind_of, title_key):
    def chapter_candidate(value):
        title = chapter_title(value)
        return (0 < len(title) <= MAX_TITLE and not title_key(title).isdigit() and not kind_of(title)
                and not auxiliary(title) and not volume(title) and not MINOR.match(title)
                and not ROUND_HEADING.match(title) and not CHAPTER_REFERENCE.match(title))

    children = defaultdict(list)
    for path, entry in entries:
        if entry.parents:
            children[title_key(entry.parents[-1])].append((path, entry))
    groups = {
        title_key(entry.title) for _, entry in entries if not entry.parents
        and chapter_candidate(entry.title) and not numbered(entry.title)
        and len({path for path, child in children[title_key(entry.title)] if chapter_candidate(child.title)}) >= 2
    }
    if len(groups) < 2:
        groups.clear()
    has_numbered = any(
        numbered(entry.title) and chapter_candidate(entry.title)
        and not any(numbered(parent) for parent in entry.parents)
        for _, entry in entries
    )
    for path, entry in entries:
        title = entry.title
        if volume(title):
            if not has_numbered and not entry.parents:
                if not any(chapter_candidate(child.title) for _, child in children[title_key(title)]):
                    yield path, entry
            continue
        if not chapter_candidate(title) or title_key(title) in groups:
            continue
        if any(numbered(parent) or auxiliary(parent) for parent in entry.parents):
            continue
        if not all(volume(parent) or title_key(parent) in groups for parent in entry.parents):
            continue
        siblings = children[title_key(entry.parents[-1])] if entry.parents else []
        if (numbered(title) or not has_numbered
                or (entry.parents and (title_key(entry.parents[-1]) in groups
                                       or any(numbered(sibling.title) for _, sibling in siblings)))):
            yield path, entry


def chapter_matches(path, entry, doc, key):
    label = key(entry.title)
    exact, paired = doc.labels.get(label, ()), doc.pairs.get(label, ())
    candidates = sorted(set(exact) | set(paired))
    bookmark = doc.bookmarks.get(entry.fragment)
    matches = {}

    def add(index, score, reason, image=False):
        if index not in matches or matches[index].score < score:
            matches[index] = Match(path, doc.blocks[index], entry, score, reason, image)

    if bookmark:
        low, high = bookmark

        def distance(block):
            return max(low - block.end, block.start - high, 0)

        nearby = [i for i in candidates if distance(doc.blocks[i]) <= 18]
        for i in nearby:
            gap = distance(doc.blocks[i])
            add(i, 10 - min((gap + 3) // 4, 3), "书签附近正文标题")
        number = key(chapter_number(entry.title))
        if number:
            for i in doc.labels.get(number, ()):
                if distance(doc.blocks[i]) <= 9:
                    add(i, 9, "书签与独立章号一致")
        if not nearby and numbered(entry.title):
            for i, block in enumerate(doc.blocks[:5]):
                if element_tag(block.node) in IMAGES and low <= block.start <= high:
                    add(i, 10, "书签对应章首图片", True)
                    break
    else:
        for i in candidates:
            if i <= doc.first_text + 2:
                add(i, 8, "目录目标文件内的标题")
            elif not entry.fragment and element_tag(doc.blocks[i].node) in HEADINGS:
                add(i, 5, "目录目标文件内的标题")
    for i in candidates:
        block = doc.blocks[i]
        tag = element_tag(block.node)
        structural = tag in HEADINGS or tag == "blockquote" and i <= doc.first_text + 1
        score = 7 if i <= doc.first_text + 2 else 5 if structural or i in paired else 2
        if bookmark and not bookmark[0] - 30 <= block.start <= bookmark[1] + 30:
            score -= 2
        if i in exact and len(exact) > 1 and not structural:
            score -= 2
        if score >= 5:
            add(i, score, "目录文字与正文块对齐")
    return sorted(matches.values(), key=lambda item: (-item.score, item.block.start))[:5]


def align_chapters(docs, nav, marked, printed_nav, label_key, kind_of):
    """Resolve one chapter start per title, respecting the EPUB spine and local order."""
    @lru_cache(maxsize=32768)
    def key(value):
        return label_key(TITLE_NOTE.sub("", chapter_title(value)))

    reading = {doc.path: i for i, doc in enumerate(docs)}
    entries = list(major_entries(
        [(doc.path, entry) for doc in docs if doc.body not in marked for entry in nav[doc.path]], kind_of, key
    ))
    published = [(p, e) for p, e in printed_nav if chapter_number(e.title)]
    paths = {p for p, _ in entries + published}
    evidence = {d.path: ChapterDocument(d.body, marked, key) for d in docs
                if d.path in paths and d.body not in marked}
    if not evidence:
        return [], []

    def matches(path, entry):
        return chapter_matches(path, entry, evidence[path], key)

    by_bookmark = {(p, e.fragment): e for p, e in entries if e.fragment}
    by_file_title = {(p, key(e.title)): e for p, e in entries if not e.fragment}
    for path, entry in published:
        if (path not in evidence or entry.fragment not in evidence[path].bookmarks
                or (path, key(entry.title)) in by_file_title):
            continue
        target = (path, entry.fragment)
        prior = by_bookmark.get(target)
        supported = any(m.reason == "书签附近正文标题" for m in matches(path, entry))
        if prior is None or (supported and not any(m.reason == "书签附近正文标题" for m in matches(path, prior))):
            by_bookmark[target] = entry
    ordered = [(p, e) for (p, _), e in (*by_file_title.items(), *by_bookmark.items())]
    ordered.sort(key=lambda item: (reading[item[0]], evidence[item[0]].bookmarks.get(item[1].fragment, (-1,))[0]))

    # Keep the highest scoring noncrossing sequence, including the option to skip an uncertain entry.
    states = {(-1, -1): (0, ())}
    for path, entry in ordered:
        options = matches(path, entry)
        if len(options) > 1 and options[0].score == options[1].score:
            continue
        proposals = dict(states)
        for match in options:
            position = reading[path], match.block.start
            prior = max((state for cut, state in states.items() if cut < position), key=lambda state: state[0])
            score = prior[0] + match.score
            if position not in proposals or proposals[position][0] < score:
                proposals[position] = (score, (*prior[1], match))
        states = {}
        best = -1
        for position, state in sorted(proposals.items()):
            if state[0] > best:
                states[position] = state
                best = state[0]
    resolved = list(max(states.values(), key=lambda state: state[0])[1])
    located = {(match.path, match.entry) for match in resolved}
    unresolved = [{"标题": entry.title, "路径": path} for path, entry in ordered if (path, entry) not in located]
    return resolved, unresolved
