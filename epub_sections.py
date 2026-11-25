"""Recognize and name the main sections of a narration script."""

from dataclasses import dataclass
import re

from clean_for_tts import normalize


NUMBER = r"[0-9〇零一二三四五六七八九十百千万兩两壹贰叁肆伍陆柒捌玖拾佰]+"
CHAPTER = re.compile(rf"^(?:第\s*{NUMBER}\s*[章回]|chapter\s+(?:\d+|[ivxlcdm]+)\b)", re.I)
MINOR = re.compile(rf"^(?:第\s*{NUMBER}\s*[节節小節]|section\s+\d|[0-9]+\.\d)", re.I)
VOLUME = re.compile(
    rf"^(?:第\s*{NUMBER}\s*[卷编編部篇册冊]|[上中下][篇编編卷部册冊](?:$|\s|[:：])|"
    r"(?:part|book|volume)\s+(?:\d+|[ivxlcdm]+)\b)", re.I
)
INTRO = re.compile(
    r"^(?:(?:重刊|再版|新版|初版|修订版|再印)?(?:序言|序章|前言|导言|導言|导读|導讀)|"
    r"(?:序|原序|作者序|自序|代序|引子|楔子|绪论|緒論)(?:$|[\s:：])|"
    r"(?:preface|foreword|prologue|introduction)\b)|"
    r"(?:译丛序|譯叢序|丛书序|叢書序|[（(](?:序|代序|序言|前言)[）)])$", re.I
)
TRANSLATOR = re.compile(
    r"^(?:[译譯]者(?:的[话話]|前言|[说說]明|[后後][记記]|序)|"
    r"[译譯]序(?:$|[\s:：])|translator[’']?s?\s+(?:note|preface|foreword|introduction)\b)", re.I
)
OUTRO = re.compile(
    r"^(?:(?:[后後][记記]|编后语|編後語|尾[声聲]|结束语|結束語)(?:$|[\s:：、（(])|"
    r"跋(?:$|[\s:：])|(?:afterword|postscript|epilogue)\b)|"
    r"(?:[（(](?:[后後][记記]|跋)[）)]|(?:珍藏版|修订版|再版|初版)?(?:编后|編後))$", re.I
)
APPENDIX = re.compile(r"^(?:附[录錄]|appendi(?:x|ces)\b)", re.I)
FRONT_EXTRA = re.compile(r"^(?:作者简介|作者簡介|内容简介|內容簡介|致[谢謝]|主要人物一[览覽]表|acknowledg[e]?ments?)$", re.I)
BACK_EXTRA = re.compile(r"^(?:参考文献|參考文獻|扩展阅读|擴展閱讀|索引|.{1,10}索引|bibliography|index|references)$", re.I)
EXTRA_ROLE = {
    "preface": "序言", "foreword": "序言", "prologue": "序言", "introduction": "序言",
    "afterword": "后记", "epilogue": "后记", "appendix": "附录",
    "bibliography": "书后内容", "index": "书后内容",
}
WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class NavEntry:
    fragment: str
    title: str
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    title: str
    kind: str


@dataclass(frozen=True)
class OutputPart:
    filename: str
    section: Section
    text: str


def named_kind(value):
    key = normalize(value)
    if TRANSLATOR.search(key):
        return "译者说明"
    if INTRO.search(key):
        return "序言"
    if OUTRO.search(key):
        return "后记"
    if APPENDIX.search(key):
        return "附录"
    if VOLUME.search(key):
        return "分部"
    if CHAPTER.search(key):
        return "正文"
    if BACK_EXTRA.fullmatch(key):
        return "书后内容"
    return None


def navigation_is_major(entry, structural_parents, roots_with_numbers):
    if MINOR.search(normalize(entry.title)) or FRONT_EXTRA.fullmatch(normalize(entry.title).rstrip("*")):
        return False
    if not entry.parents:
        return () not in roots_with_numbers or named_kind(entry.title) == "正文" or bool(
            re.match(rf"^{NUMBER}(?:[、.，]\s*|\s+)", normalize(entry.title))
        )
    parents = entry.parents
    return all(parent in structural_parents or named_kind(parent) == "分部" for parent in parents)


def split_parts(rendered, anchors, key):
    """Use retained heading offsets and preserve every rendered character."""
    cuts = list(anchors)
    sections = []
    if not cuts:
        sections.append((Section("正文", "正文"), rendered))
    else:
        opening = rendered[:cuts[0][0]]
        if opening.strip():
            sections.append((Section("书前内容", "书前内容"), opening))
        volume = ""
        for index, (start, section) in enumerate(cuts):
            end = cuts[index + 1][0] if index + 1 < len(cuts) else len(rendered)
            text = rendered[start:end]
            if not text.strip():
                continue
            if section.kind == "分部" and index + 1 < len(cuts) and key(text) == key(section.title):
                volume += text
                continue
            sections.append((section, volume + text))
            volume = ""

    width = max(3, len(str(len(sections))))
    output = []
    for index, (section, text) in enumerate(sections, 1):
        safe_title = WINDOWS_INVALID.sub(" ", section.title)
        safe_title = " ".join(safe_title.split())[:72].strip(" .")
        safe_title = safe_title or section.kind
        filename = f"{index:0{width}d}_{safe_title}.txt"
        output.append(OutputPart(filename, section, text))
    return output
