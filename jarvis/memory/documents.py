"""Programmatic document conversion with original embedded media and inline references."""

from __future__ import annotations
import base64
import io
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote, urlparse, unquote
from urllib.request import urlopen


def file_link(agent_id, session_id, item):
    return (
        f"memory://{quote(agent_id)}/{quote(session_id)}/{item['file_id']}"
        f"?filename={quote(item['filename'])}"
    )


class Documents:
    def __init__(self, store, agent_id, session_id):
        self.store, self.agent_id, self.session_id = store, agent_id, session_id
        self._parent = None

    def media(self, raw, filename, mime=None, source=None):
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        modality = mime.split("/")[0]
        kind = {"image": "image_url", "audio": "input_audio", "video": "video_url"}.get(
            modality
        )
        if kind is None:
            raise ValueError(f"Unsupported embedded media: {filename} ({mime})")
        item = self.store.register_file(
            self.agent_id,
            raw,
            filename,
            kind,
            mime,
            source=f"document:{self._parent['file_id']}:{source or filename}",
        )
        return f"[Media: {item['filename']}]({file_link(self.agent_id, self.session_id, item)})"

    def convert(self, item):
        self._parent = item
        path = self.store.instances / self.agent_id / "files" / item["filename"]
        suffix = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "text/html": ".html",
        }.get(item.get("mime_type"), path.suffix.lower())
        if suffix == ".docx":
            body = self.docx(path)
        elif suffix == ".pptx":
            body = self.pptx(path)
        elif suffix == ".pdf":
            body = self.pdf(path)
        elif suffix == ".xlsx":
            body = self.xlsx(path)
        elif suffix in {".html", ".htm", ".xhtml"}:
            body = self.html(path.read_bytes(), path.parent)
        else:
            from markitdown import MarkItDown

            body = MarkItDown(enable_plugins=False).convert_local(str(path)).markdown
        if not isinstance(body, str) or not body.strip():
            raise ValueError(
                f"Document contains no convertible content: {item['filename']}"
            )
        return (
            f"[Original document: {item['filename']}]({file_link(self.agent_id, self.session_id, item)})\n\n"
            + body
        )

    def docx(self, path):
        import mammoth
        from markdownify import markdownify
        from zipfile import ZipFile
        import hashlib
        import xml.etree.ElementTree as ET

        names = {}
        with ZipFile(path) as original:
            for name in original.namelist():
                if name.startswith("word/media/"):
                    names[hashlib.sha256(original.read(name)).hexdigest()] = Path(
                        name
                    ).name
            # Mammoth handles image positions. Insert audio/video links into the same
            # XML positions before conversion; do not modify the original document.
            xml = ET.fromstring(original.read("word/document.xml"))
            rels = ET.fromstring(original.read("word/_rels/document.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib.get("Target", "") for r in rels}
            w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            parent_map = {child: parent for parent in xml.iter() for child in parent}
            audio_links = {}
            for element in list(xml.iter()):
                refs = [
                    value
                    for key, value in element.attrib.items()
                    if key.endswith(("}embed", "}link", "}id")) and value in targets
                ]
                for ref in refs:
                    target = targets[ref]
                    mime = mimetypes.guess_type(target)[0] or ""
                    if mime.split("/")[0] not in {"audio", "video"}:
                        continue
                    import posixpath

                    name = posixpath.normpath("word/" + target)
                    link = self.media(
                        original.read(name), Path(name).name, mime, source=name
                    )
                    owner = element
                    while owner is not None and owner.tag not in {w + "r", w + "p"}:
                        owner = parent_map.get(owner)
                    if owner is None:
                        raise ValueError(
                            "Cannot preserve the position of embedded document media"
                        )
                    run = (
                        owner if owner.tag == w + "r" else ET.SubElement(owner, w + "r")
                    )
                    token = "JARVISMEDIATOKEN" + str(len(audio_links))
                    audio_links[token] = link
                    ET.SubElement(run, w + "t").text = token
                    parent = parent_map.get(element)
                    if parent is not None and element in list(parent):
                        parent.remove(element)
            converted = io.BytesIO()
            with ZipFile(converted, "w") as output:
                for name in original.namelist():
                    output.writestr(
                        name,
                        ET.tostring(xml)
                        if name == "word/document.xml"
                        else original.read(name),
                    )
        converted.seek(0)

        def image(image):
            with image.open() as stream:
                raw = stream.read()
            name = names.get(
                hashlib.sha256(raw).hexdigest(),
                "image" + (mimetypes.guess_extension(image.content_type) or ".bin"),
            )
            link = self.media(
                raw, name, image.content_type, source=hashlib.sha256(raw).hexdigest()
            )
            return {"src": re.search(r"\]\((.*)\)$", link).group(1)}

        html = mammoth.convert_to_html(
            converted, convert_image=mammoth.images.img_element(image)
        ).value
        markdown = markdownify(html, heading_style="ATX")
        for token, link in audio_links.items():
            markdown = markdown.replace(token, link)
        return markdown

    def pptx(self, path):
        from markitdown.converters import PptxConverter
        from markitdown import StreamInfo
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        documents = self

        class ArchivePptxConverter(PptxConverter):
            def _is_picture(self, shape):
                return (
                    super()._is_picture(shape)
                    or shape.shape_type == MSO_SHAPE_TYPE.MEDIA
                )

            def _convert_picture_to_markdown(self, shape, **kwargs):
                # Keep MarkItDown's text/table/chart/group/note conversion. Only
                # substitute binary media with durable references at their positions.
                links, seen = [], set()
                for node in shape._element.iter():
                    for key, value in node.attrib.items():
                        if (
                            not key.endswith(("}embed", "}link"))
                            or value not in shape.part.rels
                        ):
                            continue
                        rel = shape.part.rels[value]
                        if rel.is_external:
                            continue
                        part = rel.target_part
                        mime = part.content_type
                        name = str(part.partname)
                        if (
                            mime.split("/")[0] in {"image", "audio", "video"}
                            and name not in seen
                        ):
                            links.append(
                                documents.media(
                                    part.blob, Path(name).name, mime, source=name
                                )
                            )
                            seen.add(name)
                if not links:
                    raise ValueError("Cannot extract original PowerPoint media")
                return "\n" + "\n".join(links) + "\n"

        with path.open("rb") as stream:
            return (
                ArchivePptxConverter()
                .convert(stream, StreamInfo(extension=".pptx"))
                .markdown
            )

    def pdf(self, path):
        import pymupdf

        output = []
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("Encrypted PDF requires a password")
            for number, page in enumerate(document, 1):
                output.append(f"## Page {number}\n\n")
                tables = page.find_tables().tables
                entries = [
                    (table.bbox[1], table.bbox[0], table.to_markdown() + "\n\n")
                    for table in tables
                ]
                for block in page.get_text("blocks"):
                    if block[6] == 0 and not any(
                        pymupdf.Rect(t.bbox).contains(pymupdf.Rect(block[:4]))
                        for t in tables
                    ):
                        entries.append((block[1], block[0], block[4].strip() + "\n\n"))
                for info in page.get_image_info(xrefs=True):
                    xref = info.get("xref")
                    if xref:
                        image = document.extract_image(xref)
                        link = self.media(
                            image["image"],
                            f"image_{xref}.{image['ext']}",
                            source=f"pdf:{xref}",
                        )
                    else:
                        # Inline PDF images have no standalone encoded stream; PyMuPDF
                        # exposes their original decoded image, rather than a page render.
                        block = next(
                            (
                                b
                                for b in page.get_text("dict")["blocks"]
                                if b["type"] == 1
                                and tuple(b["bbox"]) == tuple(info["bbox"])
                            ),
                            None,
                        )
                        if block is None:
                            raise ValueError("Cannot extract an inline PDF image")
                        link = self.media(
                            block["image"],
                            f"inline_{number}_{info['number']}.{block['ext']}",
                            source=f"pdf:{number}:{info['number']}",
                        )
                    entries.append((info["bbox"][1], info["bbox"][0], link + "\n\n"))
                for annotation in page.annots() or []:
                    if annotation.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                        info = annotation.file_info
                        name = (
                            info.get("ufilename")
                            or info.get("filename")
                            or "attachment.bin"
                        )
                        mime = mimetypes.guess_type(name)[0] or ""
                        if mime.split("/")[0] in {"image", "audio", "video"}:
                            link = self.media(
                                annotation.get_file(),
                                name,
                                mime,
                                source=f"pdf-attachment:{annotation.xref}",
                            )
                            entries.append(
                                (annotation.rect.y0, annotation.rect.x0, link + "\n\n")
                            )
                output.extend(
                    text for _, _, text in sorted(entries, key=lambda e: (e[0], e[1]))
                )
        return "".join(output)

    def xlsx(self, path):
        import openpyxl
        from markdownify import markdownify
        from html import escape

        book = openpyxl.load_workbook(path, data_only=False)
        output = []
        for sheet in book:
            output.append("# " + sheet.title + "\n\n")
            images = {}
            for i, image in enumerate(sheet._images):
                anchor = image.anchor._from
                images.setdefault(anchor.row + 1, []).append(
                    self.media(
                        image._data(),
                        Path(image.path).name,
                        source=f"xlsx:{sheet.title}:{i}",
                    )
                )
            for row in sheet.iter_rows():
                for link in images.pop(row[0].row, []):
                    output.append(link + "\n\n")
                output.append(
                    markdownify(
                        "<table><tr>"
                        + "".join(
                            "<td>"
                            + escape("" if cell.value is None else str(cell.value))
                            + "</td>"
                            for cell in row
                        )
                        + "</tr></table>"
                    )
                    + "\n"
                )
            for links in images.values():
                output.extend(link + "\n\n" for link in links)
        book.close()
        return "".join(output)

    def html(self, html, base):
        from bs4 import BeautifulSoup
        from markdownify import markdownify

        soup = BeautifulSoup(html, "html.parser")
        for i, node in enumerate(soup.find_all(["img", "audio", "video"])):
            source = node.get("src") or (node.find("source") or {}).get("src")
            if not source:
                raise ValueError("Embedded media has no source")
            if source.startswith("data:"):
                header, data = source.split(",", 1)
                mime = header[5:].split(";")[0]
                raw = base64.b64decode(data, validate=True)
                name = f"media_{i}" + (mimetypes.guess_extension(mime) or ".bin")
            elif source.startswith(("https://", "http://")):
                with urlopen(source, timeout=60) as response:
                    raw, mime = response.read(), response.headers.get_content_type()
                name = Path(urlparse(source).path).name
            else:
                path = base / unquote(source)
                raw = path.read_bytes()
                name, mime = path.name, mimetypes.guess_type(path.name)[0]
            link = self.media(raw, name, mime, source=f"html:{i}")
            anchor = soup.new_tag("a", href=re.search(r"\]\((.*)\)$", link).group(1))
            anchor.string = "Media: " + name
            node.replace_with(anchor)
        return markdownify(str(soup), heading_style="ATX")
