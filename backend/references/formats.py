from __future__ import annotations

import re

from pydantic import ValidationError as SchemaError

from backend.core.errors import ValidationError
from backend.references.schemas import ReferenceFields


FIELDS = {
    "title": "title", "author": "authors", "year": "year", "doi": "doi",
    "eprint": "arxiv_id", "journal": "journal", "volume": "volume", "number": "number",
    "pages": "pages", "publisher": "publisher", "url": "url", "abstract": "abstract",
}
RIS_FIELDS = {
    "TI": "title", "AU": "authors", "PY": "year", "DO": "doi", "AN": "arxiv_id",
    "JO": "journal", "VL": "volume", "IS": "number", "SP": "pages",
    "PB": "publisher", "UR": "url", "AB": "abstract", "ID": "citation_key",
}
RIS_TYPES = {"JOUR": "article", "BOOK": "book", "CONF": "inproceedings", "GEN": "misc",
             "THES": "phdthesis", "RPRT": "techreport"}
ESCAPES = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&",
           "_": r"\_", "#": r"\#", "$": r"\$", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def _escape(value: str) -> str:
    return "".join(ESCAPES.get(char, char) for char in value)


def _decode(value: str) -> str:
    reverse = {encoded: plain for plain, encoded in ESCAPES.items()}
    result = ""
    while value:
        if value.startswith("\\"):
            token = next((key for key in reverse if value.startswith(key)), None)
            if token is None:
                raise ValidationError("Unsupported BibTeX LaTeX command; use Unicode text or escaped punctuation.")
            result += reverse[token]
            value = value[len(token):]
        else:
            result += value[0]
            value = value[1:]
    return result


def export_references(records: list[ReferenceFields], format: str) -> str:
    if format == "bibtex":
        entries = []
        for record in records:
            fields = []
            for tag, field in FIELDS.items():
                value = getattr(record, field)
                if field == "authors":
                    value = " and ".join(value)
                if value:
                    export_tag = "booktitle" if tag == "journal" and record.entry_type == "inproceedings" else tag
                    fields.append(f"  {export_tag} = {{{_escape(value)}}}")
            entries.append(f"@{record.entry_type}{{{record.citation_key},\n" + ",\n".join(fields) + "\n}")
        return "\n\n".join(entries) + ("\n" if entries else "")
    if format != "ris":
        raise ValidationError("Supported formats: bibtex, ris.")
    entries = []
    for record in records:
        lines = [f"TY  - {next(key for key, value in RIS_TYPES.items() if value == record.entry_type)}"]
        for tag, field in RIS_FIELDS.items():
            value = getattr(record, field)
            for item in value if isinstance(value, list) else [value]:
                if "\n" in item or "\r" in item:
                    raise ValidationError("RIS export supports single-line fields only; use BibTeX for multiline text.")
                if item:
                    if tag == "SP" and "--" in item:
                        start, end = item.split("--", 1)
                        lines.extend([f"SP  - {start}", f"EP  - {end}"])
                    else:
                        lines.append(f"{tag}  - {item}")
        entries.append("\n".join(lines + ["ER  - "]))
    return "\n\n".join(entries) + ("\n" if entries else "")


def parse_references(content: str, format: str) -> list[ReferenceFields]:
    try:
        values = _bibtex(content) if format == "bibtex" else _ris(content) if format == "ris" else None
        if not values:
            raise ValidationError("No supported reference entries found.")
        return [ReferenceFields.model_validate(value) for value in values]
    except SchemaError as exc:
        raise ValidationError(f"Invalid reference metadata: {exc}") from exc


def _bibtex(content: str) -> list[dict]:
    entries: list[dict] = []
    position = 0
    while content[position:].strip():
        match = re.match(r"\s*@([A-Za-z]+)\s*\{\s*([^,\s{}]+)\s*,", content[position:])
        if not match:
            raise ValidationError("Expected @type{citation_key, fields}. Macros, comments and concatenation are unsupported.")
        value: dict = {"entry_type": match[1].lower(), "citation_key": match[2]}
        position += match.end()
        while True:
            whitespace = re.match(r"\s*", content[position:])
            position += whitespace.end()
            if position < len(content) and content[position] == "}":
                position += 1
                break
            field = re.match(r"([A-Za-z]+)\s*=\s*", content[position:])
            field_names = {**FIELDS, "booktitle": "journal"}
            if not field or field[1].lower() not in field_names:
                raise ValidationError("Unsupported or malformed BibTeX field. Supported: " + ", ".join(FIELDS))
            name = field_names[field[1].lower()]
            if name in value:
                raise ValidationError(f"Repeated BibTeX field: {field[1]}")
            position += field.end()
            if position >= len(content):
                raise ValidationError("Unterminated BibTeX entry.")
            delimiter = content[position]
            if delimiter in '{"':
                position += 1
                start = position
                depth = 1
                while position < len(content):
                    char = content[position]
                    if char == "\\":
                        position += 2
                        continue
                    if delimiter == "{" and char == "{":
                        depth += 1
                    if char == ("}" if delimiter == "{" else '"'):
                        depth -= 1
                        if depth == 0:
                            break
                    position += 1
                if depth:
                    raise ValidationError("Unterminated BibTeX field.")
                text = _decode(content[start:position])
                position += 1
            else:
                number = re.match(r"\d+", content[position:])
                if not number:
                    raise ValidationError("BibTeX values must be braced, quoted, or numeric.")
                text = number[0]
                position += number.end()
            value[name] = re.split(r"\s+and\s+", text, flags=re.I) if name == "authors" else text
            tail = re.match(r"\s*", content[position:])
            position += tail.end()
            if position < len(content) and content[position] == ",":
                position += 1
            elif position >= len(content) or content[position] != "}":
                raise ValidationError("Expected comma or closing brace after BibTeX value.")
        entries.append(value)
    return entries


def _ris(content: str) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None
    for line in content.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Z0-9]{2})  - ?(.*)", line)
        if not match:
            raise ValidationError("RIS lines must use 'TAG  - value'; continuation lines are unsupported.")
        tag, text = match.groups()
        tag = {"T1": "TI", "A1": "AU", "Y1": "PY", "JF": "JO", "T2": "JO"}.get(tag, tag)
        if tag == "TY":
            if current is not None or text not in RIS_TYPES:
                raise ValidationError("Nested RIS entry or unsupported TY type.")
            current = {"entry_type": RIS_TYPES[text]}
        elif current is None:
            raise ValidationError("RIS entry must start with TY.")
        elif tag == "ER":
            if text:
                raise ValidationError("RIS ER must be empty.")
            if "citation_key" not in current:
                slug = re.sub(r"[^A-Za-z0-9]+", "_", current.get("title", "")).strip("_")[:70]
                current["citation_key"] = "ref_" + (slug or str(len(entries) + 1)) + ("_" + current["year"] if current.get("year") else "")
            if "_end_page" in current:
                if not current.get("pages"):
                    raise ValidationError("RIS EP requires a starting page (SP).")
                current["pages"] += "--" + current.pop("_end_page")
            entries.append(current)
            current = None
        elif tag == "EP":
            if "_end_page" in current or not text:
                raise ValidationError("RIS EP must be nonempty and occur once.")
            current["_end_page"] = text
        elif tag not in RIS_FIELDS:
            raise ValidationError("Unsupported RIS tag: " + tag)
        else:
            field = RIS_FIELDS[tag]
            if field == "authors":
                current.setdefault(field, []).append(text)
            elif field in current:
                raise ValidationError("Repeated RIS tag: " + tag)
            else:
                current[field] = text
    if current is not None:
        raise ValidationError("RIS entry is missing ER.")
    return entries
