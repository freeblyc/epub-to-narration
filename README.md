# EPUB 转朗读稿

把 EPUB 转成可以直接交给文字转语音工具的 UTF-8 文本。适合想批量处理中文电子书、先检查章节和注释，再安排朗读的人。

## Windows 便携版

从 [GitHub Releases](https://github.com/freeblyc/epub-to-narration/releases/latest) 下载 **EPUB-to-Narration-Win10-x64.zip**。解压后双击 EPUB转朗读稿.exe。程序可离线使用，内置 Python、Tk、lxml 和 Pandoc。发行包在 Windows 10 22H2 x64 上验证。

1. 添加一本或多本 EPUB，也可以添加文件夹当前层的全部 EPUB。
2. 选择输出目录，选择分章输出或整本输出。分章前可选中一本书预览。
3. 开始转换。分章的 TXT 按文件名编号排序，可同时保存清洗记录。

## 清洗和分章

- 按 EPUB 阅读顺序提取文字，尽量清理可识别的目录、扉页、版权信息、页眉页脚、脚注和注号。
- 保留正文标题和内容。可识别的序言、译者说明、后记和附录能分别导出，章内小节随所属大章输出。
- 原 EPUB 以只读方式打开。批量转换会跳过重名输出。图片扫描版需要先做 OCR。章节边界缺失时，程序会保留一份完整朗读稿。

## 从源码运行

需要 Python 3.9 或更新版本、Tkinter 和 Pandoc。Pandoc 可放在 PATH 中。从仓库根目录运行以下命令。

~~~powershell
python -m pip install -r requirements.txt
python -X utf8 .\epub_gui.py
~~~

CLI 可用于脚本或批量处理。

~~~powershell
python -X utf8 .\epub_to_tts.py "D:\Books" --split -d "D:\Narration" --report
python -X utf8 .\epub_to_tts.py ".\book.epub" --split --preview
~~~

## 验证与第三方组件

安装上述开发依赖后，可以运行 python -m unittest -v test_epub_to_tts.py。测试使用程序生成的合成 EPUB。

Pandoc 和其他第三方组件遵循各自的许可证。发行包附有组件来源、许可文本和 Pandoc 2.12 源码归档。
