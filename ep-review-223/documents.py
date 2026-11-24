"""Extract addressable passages. Sources are treated as data throughout."""
import base64
import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_FILE = 14 * 1024 * 1024


def clean(text):
    return " ".join(text.split())


def unpack(upload):
    if not isinstance(upload, dict) or not isinstance(upload.get("data"), str):
        raise ValueError("Файл не передан.")
    data = base64.b64decode(upload["data"], validate=True)
    if not 0 < len(data) <= MAX_FILE:
        raise ValueError("Допускаются файлы размером до 14 МБ.")
    return Path(str(upload.get("name", ""))).name[:200], data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def word_paragraphs(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        item = archive.getinfo("word/document.xml")
        if item.file_size > 24_000_000:
            raise ValueError("Документ слишком велик после распаковки.")
        root = ET.fromstring(archive.read(item))
        numbering = {}
        if "word/numbering.xml" in archive.namelist():
            if archive.getinfo("word/numbering.xml").file_size > 4_000_000:
                raise ValueError("Список нумерации слишком велик.")
            nums = ET.fromstring(archive.read("word/numbering.xml"))
            abstracts = {}
            for group in nums.findall(W + "abstractNum"):
                levels = {}
                for lvl in group.findall(W + "lvl"):
                    fmt = lvl.find(W + "numFmt")
                    if fmt is not None and fmt.get(W + "val") == "decimal":
                        start, pattern = lvl.find(W + "start"), lvl.find(W + "lvlText")
                        if pattern is not None:
                            levels[int(lvl.get(W + "ilvl"))] = (int(start.get(W + "val")) if start is not None else 1, pattern.get(W + "val"))
                abstracts[group.get(W + "abstractNumId")] = levels
            for num in nums.findall(W + "num"):
                ref = num.find(W + "abstractNumId")
                levels = dict(abstracts.get(ref.get(W + "val"), {})) if ref is not None else {}
                for override in num.findall(W + "lvlOverride"):
                    idx = int(override.get(W + "ilvl"))
                    start = override.find(W + "startOverride")
                    if idx in levels and start is not None:
                        levels[idx] = (int(start.get(W + "val")), levels[idx][1])
                numbering[num.get(W + "numId")] = levels, {}
        for n, p in enumerate(root.iter(W + "p"), 1):
            body = clean("".join(t.text or "" for t in p.iter(W + "t")))
            if not body:
                continue
            spec = p.find(W + "pPr/" + W + "numPr")
            if spec is not None and spec.find(W + "numId") is not None:
                idx = spec.find(W + "ilvl")
                i = int(idx.get(W + "val")) if idx is not None else 0
                levels, counts = numbering.get(spec.find(W + "numId").get(W + "val"), ({}, {}))
                if i in levels:
                    counts[i] = counts.get(i, levels[i][0] - 1) + 1
                    for j in list(counts):
                        if j > i:
                            del counts[j]
                    prefix = re.sub(r"%([1-9])", lambda m: str(counts.get(int(m[1])-1, levels.get(int(m[1])-1,(1,""))[0])), levels[i][1])
                    body = prefix + " " + body
            yield "абз. " + str(n), body


def passages(name, data, prefix):
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        rows = list(word_paragraphs(data))
    elif suffix == ".pdf":
        import pymupdf
        rows = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or len(doc) > 350:
                raise ValueError("Нужен открытый PDF до 350 страниц.")
            for n, page in enumerate(doc, 1):
                txt = page.get_text()
                if len(clean(txt)) < 35 and page.get_images():
                    raise ValueError(f"Страница {n} файла {name} содержит скан. Загрузите PDF с распознанным текстом.")
                for j in range(0, len(txt), 5500):
                    # Keep a small overlap for clauses which cross a block boundary.
                    part = clean(txt[max(0,j-400):j+5500])
                    if part:
                        rows.append((f"стр. {n}", part))
    elif suffix == ".txt":
        try:
            txt = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            txt = data.decode("cp1251")
        rows = []
        for n, line in enumerate(txt.splitlines(), 1):
            for j in range(0, len(line), 5500):
                if clean(line[j:j+5500]):
                    rows.append((f"строка {n}", clean(line[j:j+5500])))
    else:
        raise ValueError("Поддерживаются PDF с текстом, DOCX и TXT.")
    if not rows or not any(re.search("[А-Яа-яЁёA-Za-z]{2}", body) for _,body in rows):
        raise ValueError(f"В файле {name} не найден читаемый текст.")
    if sum(len(body) for _, body in rows) > 1_400_000:
        raise ValueError("Документ превышает лимит прототипа в 1,4 млн знаков.")
    return [dict(id=f"{prefix}.{i}", file=name, place=place, text=txt) for i,(place,txt) in enumerate(rows,1)]


def proof(block, quote=None):
    quote = clean(quote or block["text"])
    if not quote or quote not in block["text"]:
        raise ValueError("Не удалось подтвердить цитату в оригинале.")
    return dict(id=block["id"], file=block["file"], place=block["place"], quote=quote)
