from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class ReferenceFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    citation_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_:-]*$", max_length=120)
    title: str = Field(min_length=1, max_length=4000)
    authors: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=200)
    year: str = Field(default="", max_length=4)
    doi: str = Field(default="", max_length=2048)
    arxiv_id: str = Field(default="", max_length=200)
    journal: str = Field(default="", max_length=2000)
    volume: str = Field(default="", max_length=100)
    number: str = Field(default="", max_length=100)
    pages: str = Field(default="", max_length=200)
    publisher: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=4096)
    abstract: str = Field(default="", max_length=100_000)
    entry_type: Literal["article", "book", "inproceedings", "misc", "phdthesis", "techreport"] = "article"
    document_id: str | None = Field(default=None, min_length=1, max_length=100)
    related_reference_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=100)

    @field_validator("*", mode="before")
    @classmethod
    def safe_text(cls, value, info: ValidationInfo):
        values = value if isinstance(value, list) else [value]
        allowed = "\n\r\t" if info.field_name == "abstract" else ""
        for item in values:
            if isinstance(item, str) and any(
                (ord(char) < 32 or 127 <= ord(char) <= 159) and char not in allowed for char in item
            ):
                raise ValueError("Control characters are not allowed in bibliographic fields.")
        return value

    @field_validator("authors")
    @classmethod
    def valid_authors(cls, value: list[str]) -> list[str]:
        if any(not name.strip() or re.search(r"\s+and\s+", name, re.I) for name in value):
            raise ValueError("Authors must be nonempty individual names (no 'and' separators).")
        return [name.strip() for name in value]

    @field_validator("year")
    @classmethod
    def valid_year(cls, value: str) -> str:
        if value and not re.fullmatch(r"\d{4}", value):
            raise ValueError("Year must be four digits or empty.")
        return value

    @field_validator("doi")
    @classmethod
    def valid_doi(cls, value: str) -> str:
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I).lower()
        if value and not re.fullmatch(r"10\.\d{4,9}/[^\s]+", value):
            raise ValueError("DOI must be a DOI identifier or doi.org URL.")
        return value

    @field_validator("arxiv_id")
    @classmethod
    def valid_arxiv(cls, value: str) -> str:
        value = re.sub(r"^(?:https?://arxiv\.org/abs/|arxiv:\s*)", "", value, flags=re.I)
        if value and not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", value):
            raise ValueError("Invalid arXiv identifier.")
        return value


class Reference(ReferenceFields):
    id: str = Field(min_length=1, max_length=100)


class ReferenceLibrary(BaseModel):
    revision: int = Field(default=0, ge=0)
    references: list[Reference] = Field(default_factory=list)


class ReferenceWrite(BaseModel):
    expected_revision: int = Field(ge=0)
    reference: ReferenceFields


class ReferenceImport(BaseModel):
    expected_revision: int = Field(ge=0)
    format: Literal["bibtex", "ris"]
    content: str = Field(min_length=1, max_length=2_000_000)


class ReferenceExport(BaseModel):
    format: Literal["bibtex", "ris"]
    reference_ids: list[str] | None = None


class ReferenceExportResult(BaseModel):
    content: str
