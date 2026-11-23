"""R3 review regression tests: search (no dev or holdout questions)."""
import json
import math
import os
import sys
from io import StringIO
from unittest.mock import patch

from mesa_lore import cli, mcp_server
from mesa_lore.store import Store
from mesa_lore.text import mask_secrets


def test_word_forms_match(tmp_path):
    st = Store(tmp_path / "lore.db")
    st.save_note("p", "The request handler retries on timeout.", "fact")
    st.save_note("p", "Kullanıcı veritabanı yedeği her gece alınıyor.", "fact")
    st.save_note("p", "refresh_token lifetime is 7 days", "fact")
    assert "retries" in st.search("requests retried", "p")["results"][0]["text"]
    assert "yedeği" in st.search("VERİTABANINI yedekler", "p")["results"][0]["text"]
    assert "7 days" in st.search("token lifetime", "p")["results"][0]["text"]
    st.close()
