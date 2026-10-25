from types import SimpleNamespace

from backend.conversations.turns import ConversationTurnService


class Documents:
    def __init__(self):
        meta = {"folder_id": "f1"}
        self.papers = [
            SimpleNamespace(id="p1", title="Quasar survey", metadata_json=meta),
            SimpleNamespace(id="p2", title="Galaxy rotation", metadata_json=meta),
            SimpleNamespace(id="p3", title="Elsewhere", metadata_json={}),
        ]

    def get_document(self, document_id):
        return next((paper for paper in self.papers if paper.id == document_id), None)

    def list_documents(self):
        return self.papers

    def list_folders(self):
        return [SimpleNamespace(id="f1", name="Cosmology")]


def test_reading_context_is_built_on_the_server():
    service = ConversationTurnService.__new__(ConversationTurnService)
    service._documents = Documents()
    text = service._with_reading_context(
        "How was redshift measured?",
        {"document_id": "p1", "page": 4, "selection": "We used spectra."},
    )
    assert text.startswith("How was redshift measured?\n\n<reading-context>\ntitle: Quasar survey\n")
    assert '"Quasar survey" (paper ID: p1), page 4, filed in folder "Cosmology" (folder ID: f1)' in text
    assert "> We used spectra." in text
    assert 'other papers in folder "Cosmology": "Galaxy rotation" (paper ID: p2).' in text
    assert "Elsewhere" not in text
    assert text.index("this paper first") < text.index("rest of my library")
    assert service._with_reading_context("Plain", None) == "Plain"
