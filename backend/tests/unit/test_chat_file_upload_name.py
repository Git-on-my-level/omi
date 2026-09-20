import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import _chat_router_test_harness as harness
from tests.unit.test_chat_file_upload_unsupported import _make_chat_client


@pytest.fixture
def chat_client():
    client, module, saved = _make_chat_client()
    try:
        yield client, module
    finally:
        harness.cleanup(saved)


@pytest.mark.parametrize('route', ['/v2/files', '/v1/files'])
def test_uploaded_chat_file_keeps_the_name_the_user_picked(chat_client, route, monkeypatch):
    client, module = chat_client
    chat_file = sys.modules['utils.other.chat_file']
    monkeypatch.setattr(
        chat_file.openai,
        'files',
        SimpleNamespace(create=lambda *, file, purpose: SimpleNamespace(id='file-doc', filename=Path(file.name).name)),
    )

    response = client.post(
        route,
        files={'files': ('quarterly-report.pdf', b'%PDF-1.1\n%%EOF\n', 'application/pdf')},
    )

    assert response.status_code == 200
    assert response.json()[0]['name'] == 'quarterly-report.pdf'
    saved_file = module.chat_db.add_multi_files.call_args.args[1][0]
    assert saved_file['name'] == 'quarterly-report.pdf'
