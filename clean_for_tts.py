"""Prepare EPUB-derived plain text for narration, preserving unclassified text."""

import argparse
from collections import defaultdict
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import unicodedata


NUMBER = r"[0-9〇零一二三四五六七八九十百千万兩两壹贰叁肆伍陆柒捌玖拾佰]+"
CHAPTER = re.compile(
    rf"(?:第\s*{NUMBER}\s*[章节節回卷部篇册冊集](?:.{0,70})?|"
    r"(?:chapter|part|book|volume)\s+(?:\d+|[ivxlcdm]+)(?:.{0,70})?)", re.I
)
OPTIONAL = re.compile(
    r"(?:自序|代序|序|序言|序章|导言|導言|导读|導讀|前言|引言|引子|楔子|"
    r"后记|後記|跋|尾声|尾聲|终章|終章|附录|附錄|致谢|致謝|译序|譯序|"
    r"译者(?:序|前言|后记|後記|的话|的話|说明|說明)|"
    r"preface|foreword|prologue|introduction|afterword|epilogue|appendix|translator['’]s note)"
    r"(?:\s.{0,55}|[:：].{0,55})?", re.I
)
TOC = re.compile(r"(?:目\s*[录錄]|总目录|總目錄|章节目录|章節目錄|contents|table of contents)\s*:?", re.I)
TOC_PAGES = re.compile(r"\s*(?:[.·…]{2,}|[-–—]{2,}|\t+| {2,})\s*[0-9]+\s*$")
TOC_PLAIN_PAGE = re.compile(r"\s+[0-9]+\s*$")
NAV_ENTRY = re.compile(r"\d+\.\s{2,}(.+)")
FRONT_LABEL = re.compile(
    r"(?:书名|書名|作者|译者|譯者|著者|责任编辑|責任編輯|"
    r"出版社|出版者|出版发行|出版發行|出版单位|出版單位|"
    r"出版日期|出版时间|出版時間|出版地|印刷|发行|發行|开本|開本|"
    r"版次|印次|定价|定價|版权|版權|书号|書號|"
    r"publisher|published by|copyright|title|author|translator)\s*:\s*\S.*", re.I
)
FRONT_PAGE = re.compile(r"(?:扉页|扉頁|书名页|書名頁|版权页|版權頁|版权信息|版權資訊|title page|copyright page)\s*:?", re.I)
ISBN = re.compile(r"(?:isbn(?:[- ]?(?:10|13))?|(?:统一|統一)?[书書]号)\s*[:(]?\s*[\dXx][\dXx \-–—)]{7,32}", re.I)
CIP = re.compile(r"(?:中国版本图书馆|中國版本圖書館)?(?:图书在版编目|圖書在版編目)?\s*\(?CIP\)?\s*(?:数据|數據).{0,60}", re.I)
BYLINE = re.compile(r"[\w·・ .]{1,30}[\s　]+(?:著|编著|編著|编|編|译|譯)")
PUBLISHER = re.compile(r"[\w·・ ]{1,35}(?:出版社|出版集团|出版集團|出版公司)")
RIGHTS = re.compile(
    r"(?:版权所有|版權所有|"
    r"未经.{0,28}(?:许可|授权|同意)|未經.{0,28}(?:許可|授權|同意)|"
    r"copyright\s*©|©\s*\d{4}|all rights reserved\b)", re.I
)
NOTE_HEADING = re.compile(r"(?:注\s*[释釋]|註\s*[释釋]|脚\s*注|腳\s*註|尾\s*注|尾\s*註|"
                          r"本章注释|本章註釋|注\s*解|footnotes?|endnotes?|notes)\s*:?", re.I)
MARK = r"(?:[\[［](?P<bracket>(?:注|註)?\s*\d{1,4})[\]］]|〔(?P<square>(?:注|註)?\s*\d{1,4})〕|【(?P<wide>(?:注|註)?\s*\d{1,4})】|(?P<circled>[①-⑳]))"
INLINE_MARK = re.compile(MARK)
NOTE_BOUNDARY = re.compile(r"[ \t]*(?=[，。！？；、：…）」』】”’,.!?;:\r\n]|$)")
NOTE_DEF = re.compile(rf"^{MARK}\s*[:、.]?\s*\S")
NUMBERED_NOTE = re.compile(r"^\d{1,4}[.、)]\s*\S")
EXPLICIT_NOTE = re.compile(r"(?:译注|譯註|编者注|編者註|作者注|作者註|脚注|腳註|注|註)\s*(\d{1,4})?\s*:\s*\S")
NOTE_BACKLINK = re.compile(r"[↩↵⤴]\ufe0e?\ufe0f?\s*$")
DIVIDER = re.compile(r"(?:-{5,}|—{4,}|_{5,}|\*\s+\*\s+\*)")
PAGE_LINE = re.compile(
    rf"(?:第\s*{NUMBER}\s*[页頁]|(?:page|p\.)\s*[0-9]+(?:\s*(?:of|/)\s*[0-9]+)?|"
    r"[-–—]\s*[0-9]+\s*[-–—])", re.I
)
RUNNING_LABEL = re.compile(r"(?:页眉|頁眉|页脚|頁腳|running header|running footer)\s*:\s*\S.*", re.I)
TERMINAL = re.compile(r"[。！？!?；;.]$")


def normalize(text):
    pieces = re.split(r"([①-⑳])", text)
    value = "".join(part if index % 2 else unicodedata.normalize("NFKC", part)
                    for index, part in enumerate(pieces))
    return value.strip().lstrip("\ufeff").strip()


def heading(text):
    return re.sub(r"^#{1,6}\s+", "", text).rstrip("#").strip()


def is_section(text):
    return CHAPTER.fullmatch(heading(text)) or OPTIONAL.fullmatch(heading(text))


def toc_key(text):
    return " ".join(TOC_PAGES.sub("", heading(text)).split()).casefold()


def title_key(text):
    return "".join(heading(normalize(text)).strip("《》〈〉「」『』\"“”").split()).casefold()


def note_number(match):
    tag = next(value for value in match.groupdict().values() if value is not None)
    if match.group("circled"):
        return str(int(unicodedata.numeric(tag)))
    return str(int(tag.lstrip("注註 ")))


def bracket_key(match):
    if match.group("circled"):
        return "circled", note_number(match)
    return "numbered", note_number(match)


def mark_contents(keys, active, removed):
    occurrences = defaultdict(list)
    for index in active:
        occurrences[toc_key(keys[index])].append(index)

    for offset, index in enumerate(active):
        if not TOC.fullmatch(heading(keys[index])):
            continue
        entries = []
        page_leaders = False
        for item in active[offset + 1:offset + 151]:
            label = keys[item]
            with_page = bool(TOC_PAGES.search(label))
            if len(label) > 100 or TERMINAL.search(label) and not with_page:
                break
            has_duplicate = occurrences[toc_key(label)][-1] > item
            unnumbered = TOC_PLAIN_PAGE.sub("", label)
            has_duplicate_with_page = unnumbered != label and any(
                target > item for target in occurrences.get(toc_key(unnumbered), ())
            )
            if with_page or has_duplicate or has_duplicate_with_page:
                entries.append(item)
                page_leaders |= with_page or has_duplicate_with_page
            else:
                break
        if len(entries) >= 2 or page_leaders or (
            entries and is_section(TOC_PAGES.sub("", keys[entries[0]]))
        ):
            removed[index] = "目录标题"
            removed.update(dict.fromkeys(entries, "目录条目"))

    # Pandoc may render the EPUB navigation as an unlabeled numbered list.
    nav_block = []
    has_contents = False
    for index in (index for index in active if index not in removed):
        match = NAV_ENTRY.fullmatch(keys[index])
        if not match:
            break
        entry = match.group(1)
        is_toc = TOC.fullmatch(entry)
        if not (is_toc or FRONT_PAGE.fullmatch(entry) or entry.casefold() in {"cover", "封面"}):
            break
        has_contents |= bool(is_toc)
        nav_block.append(index)
    if has_contents:
        removed.update(dict.fromkeys(nav_block, "Pandoc 导航"))


def mark_front(keys, active, removed, book_title):
    front = []
    for index in (index for index in active if index not in removed):
        key = keys[index]
        if index >= 80 or is_section(key) or TOC.fullmatch(heading(key)):
            break
        neighboring_title = front and title_key(keys[front[0]]) == book_title
        front_evidence = neighboring_title or any(i in removed for i in front)
        if not (FRONT_PAGE.fullmatch(heading(key)) or FRONT_LABEL.fullmatch(key)
                or ISBN.fullmatch(key) or RIGHTS.match(key) or CIP.fullmatch(key)
                or front_evidence and (BYLINE.fullmatch(key) or PUBLISHER.fullmatch(key))):
            if len(key) > 80 or TERMINAL.search(key) and title_key(key) != book_title:
                break
            if len(front) >= 6 and not any(i in removed for i in front):
                break
            front.append(index)
            continue
        removed[index] = "扉页或版权信息"
        front.append(index)

    # A literal title is removed only at the front, with publication evidence
    # or immediately before a recognizable chapter or a removed contents block.
    visible = [index for index in active if index not in removed]
    if len(visible) >= 2:
        title, following = visible[:2]
        evidence = any(index in removed for index in active if title < index < following)
        if (title < 80 and title_key(keys[title]) == book_title and not is_section(keys[title])
                and (evidence or is_section(keys[following]))):
            removed[title] = "与书名一致的扉页行"


def mark_pandoc_notes(lines, keys, active, removed):
    for offset, divider in enumerate(active):
        if not DIVIDER.fullmatch(keys[divider]):
            continue
        if offset + 1 == len(active) or not re.match(r"^1\.\s{2,}\S", keys[active[offset + 1]]):
            continue

        entries = []
        note_index = 1
        cursor = offset + 1
        while cursor < len(active):
            start = active[cursor]
            if not re.match(rf"^{note_index}\.\s{{2,}}\S", keys[start]):
                break
            entry = [start]
            while not NOTE_BACKLINK.search(keys[entry[-1]]) and cursor + 1 < len(active):
                following = active[cursor + 1]
                if not lines[following].startswith(("    ", "\t")):
                    break
                cursor += 1
                entry.append(following)
            if not NOTE_BACKLINK.search(keys[entry[-1]]):
                break
            entries.extend(entry)
            note_index += 1
            cursor += 1

        if entries:
            removed[divider] = "Pandoc 脚注分隔线"
            removed.update(dict.fromkeys(entries, "带回链的 Pandoc 脚注"))


def mark_explicit_notes(lines, keys, active, removed):
    references = {}
    paired = set()
    note_section = None
    previous_definition = None

    for index in active:
        if index in removed:
            continue
        key = keys[index]
        definition = NOTE_DEF.match(key)
        explicit = EXPLICIT_NOTE.match(key)
        if NOTE_HEADING.fullmatch(heading(key)):
            note_section = index
            previous_definition = None
            continue
        if is_section(key):
            note_section = None
            previous_definition = None
        if previous_definition is not None and lines[index].startswith(("    ", "\t")) and not (
            definition or explicit or is_section(key)
        ):
            removed[index] = "缩进的注释续行"
            continue
        previous_definition = None

        tag = bracket_key(definition) if definition else None
        numbered = NUMBERED_NOTE.match(key) if note_section is not None else None
        if (definition and (note_section is not None or tag in references)) or explicit or numbered:
            removed[index] = "脚注或注释定义"
            if note_section is not None:
                removed[note_section] = "注释标题"
            if tag:
                paired.add(tag)
            elif explicit and explicit.group(1):
                paired.add(("numbered", str(int(explicit.group(1)))))
            elif numbered:
                paired.add(("numbered", str(int(re.match(r"\d+", key).group()))))
            previous_definition = index
            continue

        note_section = None
        # A bare numeric token at the start of a paragraph may be an item label.
        for match in INLINE_MARK.finditer(key):
            if match.start():
                references.setdefault(bracket_key(match), set()).add(index)

    replacements = {}
    for tag in paired:
        for index in references.get(tag, ()):
            if index in removed:
                continue

            def substitute(match):
                previous = match.string[match.start() - 1:match.start()]
                bare = (match.group("bracket") or "").strip().isdecimal()
                # Retain ASCII array indices like values[1].
                if (not match.start() or bracket_key(match) != tag
                        or previous.isascii() and (previous.isalnum() or previous == "_")
                        or bare and not NOTE_BOUNDARY.match(match.string, match.end())):
                    return match.group()
                return ""

            original = replacements.get(index, lines[index])
            edited = INLINE_MARK.sub(substitute, original)
            if original != edited:
                replacements[index] = edited
    return replacements


def mark_page_noise(keys, active, removed):
    candidates = defaultdict(list)
    all_occurrences = defaultdict(list)
    visible = [index for index in active if index not in removed]
    for offset, index in enumerate(visible):
        key = keys[index]
        all_occurrences[key].append(index)
        if RUNNING_LABEL.fullmatch(key):
            removed[index] = "有标签的页眉或页脚"
        if not PAGE_LINE.fullmatch(key):
            continue
        removed[index] = "有明确格式的页码"
        for shift in (-1, 1):
            if not 0 <= offset + shift < len(visible):
                continue
            nearby = visible[offset + shift]
            label = keys[nearby]
            if (abs(nearby - index) <= 3 and 2 <= len(label) <= 65
                    and not (PAGE_LINE.fullmatch(label) or is_section(label) or TERMINAL.search(label))):
                candidates[label, shift].append(nearby)
    for (label, _), indices in candidates.items():
        if len(indices) >= 3 and indices == all_occurrences[label]:
            removed.update(dict.fromkeys(indices, "固定出现在页码同侧的重复页眉或页脚"))


def clean(lines, book_title, manual_lines=()):
    keys = [normalize(line) for line in lines]
    active = [index for index, key in enumerate(keys) if key]
    removed = {index: "指定删除行" for index in manual_lines if keys[index]}
    mark_contents(keys, [index for index in active if index not in removed], removed)
    mark_front(keys, active, removed, book_title)
    mark_pandoc_notes(lines, keys, active, removed)
    replacements = mark_explicit_notes(lines, keys, active, removed)
    mark_page_noise(keys, active, removed)
    for index in removed:
        replacements.pop(index, None)
    return removed, replacements


def render(lines, removed, replacements):
    output = []
    blanks = []
    after_removal = False
    for index, line in enumerate(lines):
        if not line.strip():
            blanks.append(line)
            continue
        if index in removed:
            after_removal = True
            continue
        if output:
            output.extend(blanks[:1] if after_removal else blanks)
        output.append(replacements.get(index, line))
        blanks.clear()
        after_removal = False
    return "".join(output)


def line_range(value):
    match = re.fullmatch(r"([1-9]\d*)(?:-([1-9]\d*))?", value)
    if not match:
        raise argparse.ArgumentTypeError("请用单个行号或起止行号，例如 12 或 12-18")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start > end:
        raise argparse.ArgumentTypeError("起始行号不能大于结束行号")
    return start, end


def write_new(output, result):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", prefix=".tts-",
                                dir=output.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(result)
        if os.name == "nt":
            temporary.rename(output)
        else:
            os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="把 EPUB 转出的 TXT 清理成朗读稿。")
    parser.add_argument("input", type=Path, help="原始 TXT 路径")
    parser.add_argument("-o", "--output", type=Path, help="输出路径，默认生成同目录下的 .tts.txt")
    parser.add_argument("--title", help="扉页书名与文件名不一致时填写")
    parser.add_argument("--encoding", default="utf-8-sig", help="输入编码，默认 UTF-8，可指定 gb18030")
    parser.add_argument("--preview", action="store_true", help="显示拟删内容和原文行号")
    parser.add_argument("--drop-lines", action="append", type=line_range, default=[], metavar="12-18",
                        help="明确删除原文行号或范围，可重复指定")
    parser.add_argument("--keep-lines", action="append", type=line_range, default=[], metavar="12-18",
                        help="明确保护原文行号或范围，可重复指定")
    args = parser.parse_args()
    output = args.output or args.input.with_suffix(".tts.txt")
    try:
        with args.input.open(encoding=args.encoding, newline="") as source:
            text = source.read()
        lines = text.splitlines(keepends=True)
        if any(end > len(lines) for _, end in args.drop_lines + args.keep_lines):
            raise ValueError("指定行号超出了原文行数")
        manual_lines = {index for start, end in args.drop_lines for index in range(start - 1, end)}
        protected = {index for start, end in args.keep_lines for index in range(start - 1, end)}
        if manual_lines & protected:
            raise ValueError("同一行同时被指定为删除和保留")
        removed, replacements = clean(lines, title_key(args.title or args.input.stem), manual_lines)
        for index in protected:
            removed.pop(index, None)
            replacements.pop(index, None)
        if args.preview:
            for index in sorted(removed.keys() | replacements.keys()):
                original = lines[index].rstrip("\r\n")
                if index in removed:
                    print(f"{index + 1} [{removed[index]}] {original}")
                else:
                    print(f"{index + 1} [去除已配对的注号] {original} → {replacements[index].rstrip()}")
            print(f"拟删除 {len(removed)} 行，修改 {len(replacements)} 行。")
            return 0

        result = render(lines, removed, replacements)
        if not result.strip():
            raise ValueError("清理后的文本为空")
        write_new(output, result)
        print(f"已生成 {output}，删除 {len(removed)} 行，修改 {len(replacements)} 行。")
        return 0
    except (OSError, LookupError, ValueError) as error:
        print(f"处理失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
