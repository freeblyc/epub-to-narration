# EPUB 转朗读稿

把 EPUB 转成可以直接交给文字转语音工具的 UTF-8 文本。适合想批量处理中文电子书、先检查章节和注释，再安排朗读的人。

## Windows 便携版

到 **Releases** 选择最新版本，下载名称以 **EPUB-to-Narration-Win10-x64** 开头的 ZIP。解压后双击 EPUB转朗读稿.exe。程序可离线使用，内置 Python、Tk、lxml、Pillow 和 Pandoc。发行包在 Windows 10 22H2 x64 上验证。[查看最新版本](https://github.com/freeblyc/epub-to-narration/releases/latest)

1. 添加一本或多本 EPUB，也可以添加文件夹当前层的全部 EPUB。
2. 选择输出目录，选择分章输出或整本输出。分章前可选中一本书预览。
3. 开始转换。分章的 TXT 按文件名编号排序，可同时保存清洗记录。

## 清洗和分章

- 按 EPUB 阅读顺序提取文字，尽量清理可识别的目录、扉页、版权信息、页眉页脚、脚注和注号。
- 转换时自动修复正文段内的硬换行，也会根据原书的行宽和缩进还原逐行排版的段落。诗歌、列表、表格和真正的段落边界会保留。清洗记录会列出修复次数。
- 保留正文标题和内容。可识别的序言、译者说明、后记和附录能分别导出，章内小节随所属大章输出。
- 文字版用电子导航和书内目录定位大章，核对书签周边和阅读顺序。章名仅在章首图片上时，可用目录题名补成文本。清洗记录列出章界的定位依据和未能确认的目录项。
- 自动检测成组的全页扫描图，并使用 Windows 10/11 系统 OCR 离线识别。扫描中文书时，系统须安装简体中文或对应的中文 OCR 语言功能。清洗记录会列出扫描页、识别结果及使用目录定位的标题。扫描质量会影响文字识别与注释清理。
- 原 EPUB 以只读方式打开。清洗记录中的输入书籍只显示文件名。批量转换会跳过重名输出。无法定位章节边界时，程序会保留一份完整朗读稿。

## 从源码运行

需要 Python 3.9 或更新版本、Tkinter 和 Pandoc。Pandoc 可放在 PATH 中。从仓库根目录运行以下命令。

~~~powershell
python -m pip install -r requirements.txt
python -X utf8 .\epub_gui.py
~~~

CLI 可用于脚本或批量处理。

~~~powershell
python -X utf8 .\epub_to_tts.py ".\books" --split -d ".\narration" --report
python -X utf8 .\epub_to_tts.py ".\book.epub" --split --preview
~~~

## 验证与第三方组件

安装上述开发依赖后，可以运行 python -m unittest -v test_epub_to_tts.py。测试使用程序生成的合成 EPUB。

Pandoc 和其他第三方组件遵循各自的许可证。发行包附有组件来源、许可文本和 Pandoc 2.12 源码归档。
